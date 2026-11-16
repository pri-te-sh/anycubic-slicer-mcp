"""Apply setting overrides safely: find which preset owns a key, format the value the way the
slicer stores it, and refuse anything that could run programs or inject G-code."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .presets import KINDS, PresetLibrary

# Settings that make the slicer run external programs. Never settable through this server.
FORBIDDEN = {"post_process", "post_process_script", "filename_format_post_process"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,80}$")
NOZZLE_VARIANTS = ("_HS", "_BRASS")


class SettingError(ValueError):
    pass


def gcode_templates_allowed() -> bool:
    return os.environ.get("ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES") == "1"


def is_gcode_template(key: str) -> bool:
    return key.endswith("_gcode") and key not in {"emit_machine_limits_to_gcode", "use_relative_e_distances_gcode"}


@lru_cache(maxsize=4)
def _compiled_names(path: str, mtime: float) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def slicer_knows(names_file: Path, key: str) -> bool:
    """True if the slicer binary contains this setting name as a whole word."""
    if not KEY_RE.match(key):
        return False
    try:
        mtime = names_file.stat().st_mtime
    except OSError:
        return False
    return (b"\0" + key.encode() + b"\0") in _compiled_names(str(names_file), mtime)


def format_value(value: Any, like: Any) -> Any:
    """Match the slicer's storage format: strings, "0"/"1" for booleans, lists for per-extruder keys."""
    def scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "1" if v else "0"
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        s = str(v)
        if any(ord(ch) < 32 for ch in s):
            raise SettingError("setting values can't contain line breaks or control characters")
        return s

    if isinstance(like, list):
        if isinstance(value, list):
            return [scalar(v) for v in value]
        return [scalar(value)] * max(1, len(like))
    if isinstance(value, list):
        return ";".join(scalar(v) for v in value)
    return scalar(value)


class SettingRouter:
    def __init__(self, library: PresetLibrary):
        self.library = library
        self._keys = {kind: library.known_keys(kind) for kind in KINDS}

    def owner(self, key: str) -> str | None:
        for kind in ("process", "filament", "machine"):
            if key in self._keys[kind]:
                return kind
        return None

    def check_key(self, key: str) -> None:
        if key in FORBIDDEN:
            raise SettingError(f"'{key}' makes the slicer run external programs, so it can't be set here.")
        if is_gcode_template(key) and not gcode_templates_allowed():
            raise SettingError(f"'{key}' is a raw G-code template. Set ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES=1 "
                               "in the server's environment to allow editing templates.")

    def apply(self, configs: dict[str, dict[str, Any]], overrides: dict[str, Any] | None) -> dict[str, str]:
        """Write overrides into the flat machine/process/filament configs. Returns key -> owning kind.
        configs["filament"] may be a list of filament configs (multi-material); filament keys apply to all."""
        placed: dict[str, str] = {}
        unknown: list[str] = []
        for key, value in (overrides or {}).items():
            self.check_key(key)
            kind = self.owner(key)
            if kind is None:
                if slicer_knows(self.library.install.settings_names_file, key):
                    kind = "process"
                else:
                    unknown.append(key)
                    continue
            targets = configs[kind] if isinstance(configs[kind], list) else [configs[kind]]
            for cfg in targets:
                cfg[key] = format_value(value, cfg.get(key, [] if kind == "filament" else ""))
                for suffix in NOZZLE_VARIANTS:  # per-nozzle copies win over the plain key
                    if key + suffix in cfg:
                        cfg[key + suffix] = format_value(value, cfg[key + suffix])
            placed[key] = kind
        if unknown:
            raise SettingError(f"Unknown setting(s): {', '.join(unknown)}. Find real names with "
                               "get_preset(search=...).")
        return placed
