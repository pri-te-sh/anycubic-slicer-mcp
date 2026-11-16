"""Run Anycubic Slicer Next headless and collect the results."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np

from . import gcode as gc
from .install import Install
from .mesh import MESH_SUFFIXES, load_mesh, rotation
from .presets import PresetError, PresetLibrary, flatten_for_cli
from .settings import SettingRouter

MODEL_SUFFIXES = MESH_SUFFIXES | {".step", ".stp"}
BED_TYPES = ["Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate",
             "Textured Cool Plate", "Supertack Plate", "Smooth PEI Plate"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SliceError(RuntimeError):
    pass


# ------------------------------------------------------------------ input safety
def checked_file(path: str, allowed: set[str]) -> Path:
    """An existing local file with an allowed extension. Network (UNC) paths are refused before
    touching the filesystem, because opening one makes Windows send credentials to that host."""
    raw = str(path).strip().strip('"')
    if raw.replace("/", "\\").startswith("\\\\") and not _allow_network():
        raise ValueError("Network paths aren't allowed (set ANYCUBIC_MCP_ALLOW_NETWORK_PATHS=1 to permit).")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.suffix.lower() not in allowed and "".join(p.suffixes[-2:]).lower() not in allowed:
        raise ValueError(f"{p.name}: expected one of {', '.join(sorted(allowed))}")
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    return p.resolve()


def _allow_network() -> bool:
    import os
    return os.environ.get("ANYCUBIC_MCP_ALLOW_NETWORK_PATHS") == "1"


_PP_JSON = re.compile(rb'"post_process"\s*:\s*(\[[^\]]*\]|"[^"]*")')
_PP_XML = re.compile(rb'key="post_process"\s+value="([^"]*)"')
_PP_INI = re.compile(rb"^;?\s*post_process\s*=\s*(.*)$", re.M)


def scan_3mf(path: Path) -> None:
    """Refuse 3MF projects that would make the slicer run a post-processing program."""
    try:
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if info.file_size > 64 * 1024 * 1024 or info.filename.lower().endswith((".model", ".png", ".gcode")):
                    continue
                data = z.read(info)
                if b"post_process" not in data and b"post\\u005fprocess" not in data:
                    continue
                if b"post\\u005fprocess" in data:
                    raise ValueError(f"{path.name}: obfuscated post-processing setting found; refusing.")
                values = [m.group(1) for r in (_PP_JSON, _PP_XML, _PP_INI) for m in r.finditer(data)]
                if not values:
                    raise ValueError(f"{path.name}: couldn't verify its post-processing setting; refusing.")
                for v in values:
                    if v.strip(b' []"\r') not in (b"", b'""'):
                        raise ValueError(f"{path.name} runs a post-processing script after slicing; refusing.")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path.name} isn't a valid 3MF file.") from exc


# ------------------------------------------------------------------ request/result
@dataclass
class SliceRequest:
    models: list[str]
    printer: str | None = None
    process: str | None = None
    filaments: list[str] | None = None
    overrides: dict[str, Any] | None = None
    output_dir: str | None = None
    output_name: str | None = None
    plate: int = 0
    arrange: bool = True
    orient: bool = False
    rotation: dict[str, float] | None = None
    scale: float | None = None
    copies: int = 1
    object_filaments: list[int] | None = None
    filament_colours: list[str] | None = None
    bed_type: str | None = None
    timeout_s: int = 900
    extra_meshes: list[tuple[str, Any]] = field(default_factory=list)  # (name, Mesh) generated in-process


class Slicer:
    def __init__(self, install: Install, library: PresetLibrary):
        self.install = install
        self.library = library

    # -------------------------------------------------------------- presets
    def choose(self, printer: str | None, process: str | None, filaments: list[str] | None) -> dict[str, Any]:
        lib, notes = self.library, []
        sel = lib.selection()
        machine = lib.get("machine", printer).name if printer else sel["machine"]
        if not machine:
            raise PresetError("No printer given and none selected in the slicer. Pass printer=...")
        machine = lib.get("machine", machine).name
        defaults = lib.defaults_for(machine)
        if process:
            proc = lib.get("process", process, machine).name
        elif sel["process"] and sel["machine"] == machine and sel["process"] in lib.all("process"):
            proc = sel["process"]
        else:
            proc = defaults["process"] or lib.get("process", "", machine).name
        if not lib.fits(lib.get("process", proc), machine):
            notes.append(f"Process '{proc}' was made for another printer.")
        if filaments:
            fils = [lib.get("filament", f, machine).name for f in filaments]
        elif sel["filaments"] and sel["machine"] == machine:
            fils = [f for f in sel["filaments"] if f in lib.all("filament")] or defaults["filaments"]
        else:
            fils = defaults["filaments"]
        if not fils:
            raise PresetError("No filament given and the printer has no default. Pass filaments=[...]")
        for f in fils:
            if not lib.fits(lib.get("filament", f), machine):
                notes.append(f"Filament '{f}' was made for another printer.")
        return {"printer": machine, "process": proc, "filaments": fils, "notes": notes}

    def configs(self, chosen: dict[str, Any], overrides: dict[str, Any] | None,
                bed_type: str | None = None, colours: list[str] | None = None) -> tuple[dict, dict]:
        lib = self.library
        cfg = {
            "machine": flatten_for_cli(lib.resolve("machine", chosen["printer"])),
            "process": flatten_for_cli(lib.resolve("process", chosen["process"])),
            "filament": [flatten_for_cli(lib.resolve("filament", f)) for f in chosen["filaments"]],
        }
        # The command-line slicer only accepts a process/filament whose compatible_printers names this
        # printer (or the system printer it derives from); list them so any choice is accepted.
        lineage = lib.printer_lineage(chosen["printer"])
        for c in [cfg["process"], *cfg["filament"]]:
            c["compatible_printers"] = lineage
            c["compatible_printers_condition"] = ""
        if bed_type:
            match = next((b for b in BED_TYPES if b.lower() == bed_type.lower()), None)
            if not match:
                raise ValueError(f"bed_type must be one of: {', '.join(BED_TYPES)}")
            cfg["process"]["curr_bed_type"] = match
        for i, colour in enumerate(colours or []):
            if i < len(cfg["filament"]):
                if not re.fullmatch(r"#[0-9A-Fa-f]{6}", colour):
                    raise ValueError(f"Colour '{colour}' must look like #RRGGBB.")
                cfg["filament"][i]["default_filament_colour"] = [colour.upper()]
                cfg["filament"][i]["filament_colour"] = [colour.upper()]
        placed = SettingRouter(lib).apply(cfg, overrides)
        return cfg, placed

    # -------------------------------------------------------------- run
    def _run(self, args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        exe = self.install.executable
        if not exe.is_file():
            raise SliceError(f"Anycubic Slicer Next not found at {exe}. Install it, or set ANYCUBIC_SLICER_EXE.")
        try:
            return subprocess.run([str(exe), *args], cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise SliceError(f"The slicer didn't finish within {timeout}s.") from exc

    def to_stl(self, model: Path, work: Path) -> Path:
        """STEP → STL via the slicer, so it can be measured and transformed."""
        out = work / "step_export"
        out.mkdir(exist_ok=True)
        proc = self._run(["--export-stl", "--outputdir", str(out), str(model)], work, 300)
        stls = sorted(out.glob("*.stl"))
        if proc.returncode != 0 or not stls:
            raise SliceError(f"Couldn't convert {model.name} to STL: {_tail(proc)}")
        return stls[0]

    def prepare_models(self, req: SliceRequest, work: Path) -> list[Path]:
        paths = []
        for m in req.models:
            p = checked_file(m, MODEL_SUFFIXES)
            if p.suffix.lower() == ".3mf":
                scan_3mf(p)
            paths.append(p)
        for name, mesh in req.extra_meshes:
            paths.append(mesh.save_stl(work / f"{name}.stl"))
        if req.rotation or req.scale:
            transformed = []
            for p in paths:
                src = self.to_stl(p, work) if p.suffix.lower() in {".step", ".stp"} else p
                mesh = load_mesh(src)
                r = req.rotation or {}
                matrix = rotation(r.get("x", 0), r.get("y", 0), r.get("z", 0))
                if req.scale:
                    if not 0.01 <= req.scale <= 100:
                        raise ValueError("scale must be between 0.01 and 100")
                    matrix = matrix * req.scale
                transformed.append(mesh.transformed(matrix).on_bed().save_stl(work / f"{p.stem}.stl"))
            paths = transformed
        if req.copies and req.copies > 1:
            if req.copies > 200:
                raise ValueError("copies must be 200 or fewer")
            paths = [p for p in paths for _ in range(req.copies)]
        return paths

    def slice(self, req: SliceRequest) -> dict[str, Any]:
        t0 = time.time()
        if not req.models and not req.extra_meshes:
            raise ValueError("Give at least one model file.")
        self.library.reload()
        chosen = self.choose(req.printer, req.process, req.filaments)
        cfg, placed = self.configs(chosen, req.overrides, req.bed_type, req.filament_colours)
        first = Path(req.models[0]) if req.models else None
        out_dir = Path(req.output_dir).expanduser() if req.output_dir else (first.expanduser().resolve().parent if first else Path.cwd())
        out_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_name(req.output_name or (first.stem if first else req.extra_meshes[0][0]))
        with tempfile.TemporaryDirectory(prefix="acmcp-") as tmp:
            work = Path(tmp)
            models = self.prepare_models(req, work)
            (work / "machine.json").write_text(json.dumps(cfg["machine"]), encoding="utf-8")
            (work / "process.json").write_text(json.dumps(cfg["process"]), encoding="utf-8")
            fil_files = []
            for i, f in enumerate(cfg["filament"]):
                fp = work / f"filament_{i + 1}.json"
                fp.write_text(json.dumps(f), encoding="utf-8")
                fil_files.append(fp.name)
            outdir = work / "out"
            outdir.mkdir()
            args = ["--slice", str(int(req.plate)), "--arrange", "1" if req.arrange else "0",
                    "--orient", "1" if req.orient else "0",
                    "--load-settings", "machine.json;process.json",
                    "--load-filaments", ";".join(fil_files),
                    "--outputdir", str(outdir), "--export-3mf", f"{name}.gcode.3mf"]
            if req.object_filaments:
                ids = [int(i) for i in req.object_filaments]
                if any(i < 1 or i > len(fil_files) for i in ids):
                    raise ValueError(f"object_filaments entries must be between 1 and {len(fil_files)}")
                per_model = ids * (req.copies if req.copies and req.copies > 1 else 1)
                args += ["--load-filament-ids", ",".join(str(i) for i in per_model)]
            proc = self._run(args + [str(m) for m in models], work, req.timeout_s)
            gcodes = sorted(outdir.glob("plate_*.gcode"), key=lambda p: int(re.findall(r"\d+", p.stem)[0]))
            three_mf = outdir / f"{name}.gcode.3mf"
            if proc.returncode != 0 or not gcodes:
                raise SliceError(f"Slicing failed (exit {proc.returncode}): {_tail(proc)}")
            info = read_slice_info(three_mf) if three_mf.exists() else {}
            plates = []
            for g in gcodes:
                n = int(re.findall(r"\d+", g.stem)[0])
                dest = out_dir / (f"{name}.gcode" if len(gcodes) == 1 else f"{name}_plate{n}.gcode")
                shutil.move(str(g), dest)
                summary = gc.summarize(dest)
                plate_info = info.get(n, {})
                plates.append({"plate": n, **{k: v for k, v in summary.items() if k != "settings"},
                               "settings": summary.get("settings", {}), **plate_info})
            final_3mf = None
            if three_mf.exists():
                final_3mf = out_dir / three_mf.name
                shutil.move(str(three_mf), final_3mf)
        warnings = [w for p in plates for w in p.pop("warnings", [])]
        for p in plates:
            if p.get("outside_bed"):
                warnings.append(f"Plate {p['plate']}: part of the model is outside the printable area.")
        return {
            "ok": True,
            "printer": chosen["printer"], "process": chosen["process"], "filaments": chosen["filaments"],
            "overrides_applied": placed, "plates": plates,
            "gcode_3mf": str(final_3mf) if final_3mf else None,
            "warnings": warnings + chosen["notes"],
            "elapsed_s": round(time.time() - t0, 1),
        }

    def open_in_gui(self, path: Path) -> None:
        if self.install.os_name == "macos" and self.install.app_bundle:
            subprocess.Popen(["open", "-a", str(self.install.app_bundle), str(path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([str(self.install.executable), str(path)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=NO_WINDOW)


def read_slice_info(three_mf: Path) -> dict[int, dict[str, Any]]:
    """Per-plate prediction, weight, filament use and warnings from the sliced 3MF."""
    out: dict[int, dict[str, Any]] = {}
    try:
        with zipfile.ZipFile(three_mf) as z:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError, OSError):
        return out
    for plate in root.findall("plate"):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        n = int(meta.get("index", "1"))
        out[n] = {
            "outside_bed": meta.get("outside") == "true",
            "support_used": meta.get("support_used") == "true",
            "filaments_used": [
                {"slot": int(f.get("id", "0")), "type": f.get("type"), "colour": f.get("color"),
                 "used_g": float(f.get("used_g") or 0), "used_m": float(f.get("used_m") or 0)}
                for f in plate.findall("filament")
            ],
            "warnings": [w.get("msg", "").replace("_", " ") for w in plate.findall("warning")],
        }
    return out


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:100] or "model"


def _tail(proc: subprocess.CompletedProcess, n: int = 8) -> str:
    text = (proc.stderr or "") + (proc.stdout or "")
    lines = [l for l in text.splitlines() if l.strip() and "[trace]" not in l]
    errors = [l for l in lines if "error" in l.lower() or "fail" in l.lower()]
    picked = (errors or lines)[-n:]
    return " | ".join(re.sub(r"^\[[^\]]+\]\s*(\[[^\]]+\]\s*)*", "", l) for l in picked) or "no output"
