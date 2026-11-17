"""Read Anycubic Slicer Next's printer (machine), process and filament presets.

Presets are JSON files. System presets live in <data>/system/<vendor>/<kind>/, user presets in
<data>/user/<account folder>/<kind>/. A preset may name a parent in "inherits"; its effective
settings are the parent's settings overlaid with its own. Hidden base presets have
"instantiation": "false".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .install import Install

KINDS = ("machine", "process", "filament")
# Kobra X presets keep per-nozzle copies of some settings (nozzle_temperature_HS, ..._BRASS) and the
# slicer uses those copies, so a value set on the plain key has to reach them too.
NOZZLE_VARIANTS = ("_HS", "_BRASS")
_META_KEYS = {"inherits", "instantiation", "from", "name", "type", "setting_id", "version",
              "is_custom_defined", "base_id", "filament_id", "user_id", "updated_time"}


class PresetError(ValueError):
    """A preset couldn't be found, was ambiguous, or doesn't fit the printer."""


@dataclass
class Preset:
    name: str
    kind: str
    source: str            # "system" or "user"
    path: Path
    raw: dict[str, Any] = field(repr=False)

    @property
    def parent(self) -> str | None:
        p = self.raw.get("inherits")
        return p or None

    @property
    def selectable(self) -> bool:
        return str(self.raw.get("instantiation", "true")).lower() != "false"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class PresetLibrary:
    """An index of every preset, rebuilt with reload() so GUI changes are picked up."""

    def __init__(self, install: Install):
        self.install = install
        self.conf: dict[str, Any] = {}
        self.user_folder = "default"
        self.system: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        self.user: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        self.reload()

    # ------------------------------------------------------------------ loading
    def reload(self) -> None:
        conf = _read_json(self.install.config_file) or {}
        app = conf.get("app") if isinstance(conf.get("app"), dict) else {}
        folder = str(app.get("preset_folder") or "default")
        system: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        user: dict[str, dict[str, Preset]] = {k: {} for k in KINDS}
        for kind in KINDS:
            for vendor in self._system_roots():
                self._scan(system[kind], vendor / kind, kind, "system")
            self._scan(user[kind], self.install.data_dir / "user" / folder / kind, kind, "user")
        self.conf, self.user_folder, self.system, self.user = conf, folder, system, user

    def _system_roots(self) -> list[Path]:
        data_system = self.install.data_dir / "system"
        roots = [p for p in sorted(data_system.iterdir()) if p.is_dir()] if data_system.is_dir() else []
        if not roots:  # slicer never opened: fall back to the profiles inside the app
            bundled = self.install.bundled_profiles
            roots = [p for p in sorted(bundled.iterdir()) if p.is_dir()] if bundled.is_dir() else []
        return roots

    @staticmethod
    def _scan(index: dict[str, Preset], folder: Path, kind: str, source: str) -> None:
        if not folder.is_dir():
            return
        for path in sorted(folder.rglob("*.json")):
            data = _read_json(path)
            if not data:
                continue
            name = str(data.get("name") or path.stem)
            index.setdefault(name, Preset(name, kind, source, path, data))

    # ------------------------------------------------------------------ lookup
    def all(self, kind: str) -> dict[str, Preset]:
        merged = dict(self.system[_kind(kind)])
        merged.update(self.user[_kind(kind)])  # user presets win on a name clash
        return merged

    def get(self, kind: str, query: str, printer: str | None = None) -> Preset:
        """Exact name, else a unique case-insensitive substring among selectable presets
        (restricted to presets compatible with `printer` when given)."""
        kind = _kind(kind)
        presets = self.all(kind)
        if query in presets:
            return presets[query]
        q = query.lower().strip()
        pool = [p for p in presets.values() if p.selectable and q in p.name.lower()]
        if printer and kind != "machine":
            fitting = [p for p in pool if self.fits(p, printer)]
            pool = fitting or pool
            # "Anycubic PLA" + a printer usually means "Anycubic PLA @<printer>"
            exact = [p for p in pool if p.name.lower().startswith(q + " @")]
            if len(exact) == 1:
                return exact[0]
        if len(pool) == 1:
            return pool[0]
        if len(pool) > 1:  # several match: the one the GUI has selected is what the user means
            sel = self.selection()
            wanted = {"machine": [sel["machine"]], "process": [sel["process"]], "filament": sel["filaments"]}[kind]
            chosen = [p for p in pool if p.name in wanted]
            if len(chosen) == 1:
                return chosen[0]
        if not pool:
            raise PresetError(f"No {kind} preset matches '{query}'. Use list_presets to see the names.")
        names = ", ".join(sorted(p.name for p in pool)[:8])
        raise PresetError(f"'{query}' matches {len(pool)} {kind} presets ({names}…); be more specific.")

    def resolve(self, kind: str, name: str) -> dict[str, Any]:
        """Effective settings: the inheritance chain merged root-first."""
        kind = _kind(kind)
        chain: list[Preset] = []
        seen: set[tuple[str, str]] = set()
        current = self.all(kind).get(name)
        if current is None:
            raise PresetError(f"Unknown {kind} preset '{name}'.")
        while current is not None:
            if (current.source, current.name) in seen:  # a real cycle
                break
            seen.add((current.source, current.name))
            chain.append(current)
            parent = current.parent
            if not parent:
                break
            # a user preset may share its parent's name; then the parent is the system one
            nxt = self.user[kind].get(parent) if current.source == "system" else None
            if parent == current.name or nxt is None:
                nxt = self.system[kind].get(parent) or self.user[kind].get(parent)
            if nxt is None or len(chain) > 32:
                break
            current = nxt
        merged: dict[str, Any] = {}
        for preset in reversed(chain):
            merged.update(preset.raw)
            if preset.source != "system":
                spread_to_variants(merged, preset.raw)
        merged["name"] = name
        return merged

    # ------------------------------------------------------------------ compatibility
    def printer_lineage(self, printer: str) -> list[str]:
        """The printer and its ancestors' names (user printers inherit a system printer)."""
        names, current, guard = [], self.all("machine").get(printer), 0
        while current is not None and guard < 16:
            names.append(current.name)
            parent = current.parent
            current = self.system["machine"].get(parent) if parent else None
            guard += 1
        return names or [printer]

    def fits(self, preset: Preset | str, printer: str, kind: str | None = None) -> bool:
        if isinstance(preset, str):
            preset = self.get(kind or "filament", preset)
        if preset.kind == "machine":
            return True
        settings = self.resolve(preset.kind, preset.name)
        allowed = settings.get("compatible_printers") or []
        if isinstance(allowed, str):
            allowed = [allowed]
        allowed = [a for a in allowed if a]
        if not allowed:
            return True
        return any(n in allowed for n in self.printer_lineage(printer))

    def listing(self, kind: str, printer: str | None = None, search: str | None = None,
                include_incompatible: bool = False) -> list[dict[str, Any]]:
        kind = _kind(kind)
        out = []
        for preset in self.all(kind).values():
            if not preset.selectable:
                continue
            if search and search.lower() not in preset.name.lower():
                continue
            fits = True if not printer or kind == "machine" else self.fits(preset, printer)
            if not fits and not include_incompatible:
                continue
            row = {"name": preset.name, "source": preset.source}
            if include_incompatible and printer:
                row["fits_printer"] = fits
            out.append(row)
        out.sort(key=lambda r: (r["source"] != "user", r["name"].lower()))
        return out

    # ------------------------------------------------------------------ GUI selection
    def selection(self) -> dict[str, Any]:
        """What the GUI last had selected: printer, process, filaments, colours, plate type."""
        sel: dict[str, Any] = {"machine": None, "process": None, "filaments": [], "colours": [], "bed_type": None}
        blocks = self.conf.get("anycubic_presets")
        block = blocks[0] if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) else {}
        presets = self.conf.get("presets") if isinstance(self.conf.get("presets"), dict) else {}
        sel["machine"] = block.get("machine") or presets.get("machine")
        sel["process"] = block.get("process") or presets.get("process")
        fil = block.get("filament") or presets.get("filament")
        if isinstance(fil, str):
            sel["filaments"] = [f for f in fil.split(";") if f]
        elif isinstance(fil, list):
            sel["filaments"] = [f for f in fil if isinstance(f, str) and f and not f.isdigit()]
        colours = block.get("filament_colors") or ""
        sel["colours"] = [c for c in str(colours).split(";") if c] if colours else []
        sel["bed_type"] = block.get("curr_bed_type")
        return sel

    def defaults_for(self, printer: str) -> dict[str, Any]:
        """The printer preset's own default process and filament."""
        settings = self.resolve("machine", printer)
        fil = settings.get("default_filament_profile") or []
        if isinstance(fil, str):
            fil = [fil]
        return {"process": settings.get("default_print_profile") or None, "filaments": [f for f in fil if f]}

    # ------------------------------------------------------------------ writing
    def user_preset_dir(self, kind: str) -> Path:
        return self.install.data_dir / "user" / self.user_folder / _kind(kind)

    def user_folders(self) -> list[str]:
        """Every preset folder the slicer window might read: the active one first, then 'default' and each
        signed-in account's folder. The window switches between them when the user signs in or out."""
        root = self.install.data_dir / "user"
        found = [p.name for p in sorted(root.iterdir()) if p.is_dir()] if root.is_dir() else []
        names = [self.user_folder, "default", *[n for n in found if n.isdigit()]]
        return list(dict.fromkeys(names))

    def system_root(self, kind: str, name: str) -> Preset | None:
        """The system preset a preset ultimately inherits from (itself if it's a system preset)."""
        kind = _kind(kind)
        current = self.all(kind).get(name)
        for _ in range(32):
            if current is None or current.source == "system":
                return current
            parent = current.parent
            current = self.system[kind].get(parent) or self.user[kind].get(parent) if parent else None
        return None

    def write_user_preset(self, kind: str, data: dict[str, Any], overwrite: bool = False) -> list[Path]:
        """Write a user preset the way the slicer window saves its own, so the window lists it.

        The window skips a user preset without "version", and one whose .info file has no base_id. The
        preset must inherit straight from a system preset. Values set on a plain key are copied to the
        per-nozzle variants the parent has. It's written into every account folder (see user_folders).
        """
        kind = _kind(kind)
        name = str(data["name"])
        parent = self.system[kind].get(str(data.get("inherits") or ""))
        if parent is None:
            raise PresetError(f"A user {kind} preset has to inherit from a system preset.")
        base = self.resolve(kind, parent.name)
        out = dict(data)
        for key in list(data):
            for suffix in NOZZLE_VARIANTS:
                if key + suffix in base and key + suffix not in data:
                    out[key + suffix] = data[key]
        out.setdefault("version", parent.raw.get("version") or "1.0.0.0")
        out.setdefault("is_custom_defined", "0")
        base_id = str(parent.raw.get("setting_id") or parent.name)
        folders = self.user_folders()
        first_path = self.install.data_dir / "user" / folders[0] / kind / f"{name}.json"
        if first_path.exists() and not overwrite:
            raise FileExistsError(f"A {kind} preset named '{name}' already exists (pass overwrite=True).")
        written = []
        for folder in folders:
            d = self.install.data_dir / "user" / folder / kind
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{name}.json"
            path.write_text(json.dumps(out, indent=4), encoding="utf-8")
            user_id = folder if folder.isdigit() else ""
            path.with_suffix(".info").write_text(
                f"sync_info = \nuser_id = {user_id}\nsetting_id = \nbase_id = {base_id}\n"
                f"updated_time = {int(time.time())}\n", encoding="utf-8")
            written.append(path)
        self.reload()
        return written

    def known_keys(self, kind: str) -> set[str]:
        keys: set[str] = set()
        for preset in self.all(_kind(kind)).values():
            keys.update(preset.raw.keys())
        return keys - _META_KEYS


def _kind(kind: str) -> str:
    k = kind.lower().strip()
    aliases = {"printer": "machine", "print": "process", "profile": "process", "material": "filament"}
    k = aliases.get(k, k)
    if k not in KINDS:
        raise PresetError(f"kind must be one of {', '.join(KINDS)} (got '{kind}')")
    return k


def flatten_for_cli(settings: dict[str, Any]) -> dict[str, Any]:
    """A self-contained preset the command-line slicer accepts (no parent lookups)."""
    flat = {k: v for k, v in settings.items() if k not in {"inherits", "base_id", "user_id", "updated_time"}}
    flat["inherits"] = ""
    flat.setdefault("from", "system")
    flat["instantiation"] = "true"
    if flat.get("default_filament_colour") and not flat.get("filament_colour"):
        flat["filament_colour"] = flat["default_filament_colour"]  # otherwise the slicer paints it orange
    return flat


def spread_to_variants(merged: dict[str, Any], layer: dict[str, Any]) -> None:
    """Copy each plain key a preset layer sets onto the per-nozzle variants it didn't set itself."""
    for key, value in layer.items():
        for suffix in NOZZLE_VARIANTS:
            if key + suffix in merged and key + suffix not in layer:
                merged[key + suffix] = value


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def first(value: Any, default: Any = None) -> Any:
    items = as_list(value)
    return items[0] if items else default


def to_float(value: Any, default: float | None = None) -> float | None:
    v = first(value, None)
    if v is None:
        return default
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return default


def names(items: Iterable[Preset]) -> list[str]:
    return [p.name for p in items]
