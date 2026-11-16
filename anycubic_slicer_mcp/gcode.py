"""Read sliced G-code: estimates, filament use, tool changes, and per-layer toolpaths for previews."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

GCODE_SUFFIXES = {".gcode", ".gco", ".g"}

# Feature names the slicer writes in ";TYPE:" comments, mapped to stable ids.
FEATURES = [
    "Outer wall", "Inner wall", "Overhang wall", "Sparse infill", "Internal solid infill",
    "Solid infill", "Top surface", "Bottom surface", "Bridge", "Internal Bridge", "Gap infill",
    "Ironing", "Support", "Support interface", "Support transition", "Brim", "Skirt",
    "Prime tower", "Wipe tower", "Custom", "Other",
]
FEATURE_ID = {name.lower(): i for i, name in enumerate(FEATURES)}

_SUMMARY = [
    ("time_normal", r"estimated printing time \(normal mode\)\s*=\s*([^=]+)"),
    ("time_silent", r"estimated printing time \(silent mode\)\s*=\s*([^=]+)"),
    ("time_sport", r"estimated printing time \(sport mode\)\s*=\s*([^=]+)"),
    ("filament_mm", r"^; filament used \[mm\]\s*=\s*(.+)"),
    ("filament_cm3", r"^; filament used \[cm3\]\s*=\s*(.+)"),
    ("filament_g", r"^; filament used \[g\]\s*=\s*(.+)"),
    ("filament_cost", r"^; filament cost\s*=\s*(.+)"),
    ("total_filament_g", r"^; total filament used \[g\]\s*=\s*(.+)"),
    ("total_cost", r"^; total filament cost\s*=\s*(.+)"),
    ("layers", r"^; total layers count\s*=\s*(\d+)"),
    ("max_z_mm", r"^; max_z_height:\s*([\d.]+)"),
]
SETTINGS_OF_INTEREST = [
    "printer_settings_id", "print_settings_id", "filament_settings_id", "filament_type", "layer_height",
    "initial_layer_print_height", "sparse_infill_density", "sparse_infill_pattern", "wall_loops",
    "top_shell_layers", "bottom_shell_layers", "enable_support", "support_type", "brim_type",
    "nozzle_temperature", "hot_plate_temp", "textured_plate_temp", "curr_bed_type", "enable_prime_tower",
    "filament_colour", "nozzle_diameter",
]


def parse_duration(text: str) -> float | None:
    """'1d 2h 3m 4s' -> seconds."""
    total, found = 0.0, False
    for amount, unit in re.findall(r"([\d.]+)\s*([dhms])", text):
        total += float(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
        found = True
    return total if found else None


def format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d"] if d else []
    parts += [f"{h}h"] if h else []
    parts += [f"{m}m"] if m else []
    parts.append(f"{s}s")
    return " ".join(parts[:3])


def _numbers(text: str) -> list[float]:
    out = []
    for part in re.split(r"[,;]", text):
        try:
            out.append(float(part.strip()))
        except ValueError:
            pass
    return out


def summarize(path: str | Path) -> dict:
    """Estimates and key settings from the comments the slicer writes, plus tool-change and
    first-layer measurements taken from the moves themselves."""
    path = Path(path)
    raw: dict[str, str] = {}
    settings: dict[str, str] = {}
    tool_changes = 0
    first_tool_seen = False
    layer_r: list[float] = []   # minutes remaining (M73 R) at each layer change
    pending_layer = False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(";"):
                if line.startswith(";LAYER_CHANGE"):
                    pending_layer = True
                    continue
                for key, pattern in _SUMMARY:  # last occurrence wins: multi-colour files repeat running totals
                    m = re.search(pattern, line)
                    if m:
                        raw[key] = m.group(1).strip()
                m = re.match(r"^; ([a-z_]+) = (.*)$", line)
                if m and m.group(1) in SETTINGS_OF_INTEREST:
                    settings[m.group(1)] = m.group(2).strip().strip('"')
                continue
            if pending_layer and line.startswith("M73"):
                m = re.search(r"\bR([\d.]+)", line)
                if m:
                    layer_r.append(float(m.group(1)))
                    pending_layer = False
            elif re.match(r"^T\d+\b", line):
                if first_tool_seen:
                    tool_changes += 1
                first_tool_seen = True
    result: dict = {"gcode": str(path)}
    for key in ("time_normal", "time_silent", "time_sport"):
        if key in raw:
            secs = parse_duration(raw[key])
            result[key] = format_duration(secs)
            result[key + "_s"] = secs
    for key in ("filament_mm", "filament_cm3", "filament_g", "filament_cost"):
        if key in raw:
            vals = _numbers(raw[key])
            result[key] = vals[0] if len(vals) == 1 else vals
    for key in ("total_filament_g", "total_cost"):
        if key in raw:
            vals = _numbers(raw[key])
            if vals:
                result[key] = vals[0]
    if "filament_g" in result and "total_filament_g" not in result:
        fg = result["filament_g"]
        result["total_filament_g"] = round(sum(fg), 2) if isinstance(fg, list) else fg
    if "layers" in raw:
        result["layers"] = int(raw["layers"])
    if "max_z_mm" in raw:
        result["max_z_mm"] = float(raw["max_z_mm"])
    if len(layer_r) >= 2:
        result["first_layer_time"] = format_duration((layer_r[0] - layer_r[1]) * 60)
    result["tool_changes"] = tool_changes
    result["settings"] = settings
    return result


@dataclass
class Toolpaths:
    """Extrusion segments grouped by layer: arrays of x0, y0, x1, y1, feature id."""
    layer_z: list[float] = field(default_factory=list)
    layers: list[np.ndarray] = field(default_factory=list)   # each (n, 5) float32
    travel: list[np.ndarray] = field(default_factory=list)   # each (n, 4) float32

    @property
    def count(self) -> int:
        return len(self.layers)

    def bounds(self) -> tuple[float, float, float, float]:
        pts = [l[:, :4] for l in self.layers if len(l)]
        if not pts:
            return (0, 0, 1, 1)
        allp = np.vstack(pts)
        xs = np.concatenate([allp[:, 0], allp[:, 2]]); ys = np.concatenate([allp[:, 1], allp[:, 3]])
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())

    def feature_lengths(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for layer in self.layers:
            if not len(layer):
                continue
            lengths = np.hypot(layer[:, 2] - layer[:, 0], layer[:, 3] - layer[:, 1])
            for fid in np.unique(layer[:, 4]).astype(int):
                totals[FEATURES[fid]] = totals.get(FEATURES[fid], 0.0) + float(lengths[layer[:, 4] == fid].sum())
        return {k: round(v / 1000, 2) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}  # metres


_WORD = re.compile(r"([GXYZEIJF])(-?\d*\.?\d+)")


def read_toolpaths(path: str | Path, keep_travel: bool = False) -> Toolpaths:
    tp = Toolpaths()
    x = y = z = e = 0.0
    absolute_xyz, absolute_e = True, False
    feature = FEATURE_ID["other"]
    current: list[tuple] = []
    travels: list[tuple] = []
    layer_started = False

    def close_layer():
        if layer_started or current:
            tp.layers.append(np.array(current, dtype=np.float32).reshape(-1, 5))
            tp.travel.append(np.array(travels, dtype=np.float32).reshape(-1, 4))
            tp.layer_z.append(z)

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line[0] == ";":
                if line.startswith(";LAYER_CHANGE"):
                    if layer_started:
                        close_layer()
                        current, travels = [], []
                    layer_started = True
                elif line.startswith(";TYPE:"):
                    feature = FEATURE_ID.get(line[6:].strip().lower(), FEATURE_ID["other"])
                elif line.startswith(";Z:"):
                    try:
                        z = float(line[3:])
                    except ValueError:
                        pass
                continue
            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            head = code.split(None, 1)[0]
            if head in ("G90",):
                absolute_xyz = True; continue
            if head in ("G91",):
                absolute_xyz = False; continue
            if head == "M82":
                absolute_e = True; continue
            if head == "M83":
                absolute_e = False; continue
            if head == "G92":
                w = dict(_WORD.findall(code))
                if "E" in w:
                    e = float(w["E"])
                continue
            if head not in ("G0", "G1", "G2", "G3"):
                continue
            w = dict(_WORD.findall(code))
            nx = float(w["X"]) if "X" in w else None
            ny = float(w["Y"]) if "Y" in w else None
            if not absolute_xyz:
                nx = x + nx if nx is not None else x
                ny = y + ny if ny is not None else y
            nx = x if nx is None else nx
            ny = y if ny is None else ny
            if "Z" in w:
                z_new = float(w["Z"]) if absolute_xyz else z + float(w["Z"])
                if not layer_started:
                    z = z_new
            extruding = False
            if "E" in w:
                ev = float(w["E"])
                extruding = (ev > 0) if not absolute_e else (ev > e)
                if absolute_e:
                    e = ev
            if (nx, ny) != (x, y):
                if head in ("G2", "G3") and ("I" in w or "J" in w):
                    pts = _arc_points(x, y, nx, ny, float(w.get("I", 0)), float(w.get("J", 0)), head == "G2")
                else:
                    pts = [(x, y), (nx, ny)]
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    if extruding:
                        current.append((ax, ay, bx, by, feature))
                    elif keep_travel:
                        travels.append((ax, ay, bx, by))
            x, y = nx, ny
    close_layer()
    return tp


def _arc_points(x0, y0, x1, y1, i, j, clockwise, step_deg=10.0):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if clockwise:
        while a1 >= a0:
            a1 -= 2 * math.pi
    else:
        while a1 <= a0:
            a1 += 2 * math.pi
    n = max(2, int(abs(a1 - a0) / math.radians(step_deg)) + 1)
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]


def inject_at_heights(src: Path, dst: Path, commands: list[tuple[float, str]]) -> int:
    """Copy G-code, inserting each command right after the first layer change at or above its
    height (used by calibration towers). Returns how many were inserted."""
    pending = sorted(commands)
    inserted = 0
    with open(src, encoding="utf-8", errors="replace") as fin, open(dst, "w", encoding="utf-8") as fout:
        at_layer = False
        for line in fin:
            fout.write(line)
            if line.startswith(";LAYER_CHANGE"):
                at_layer = True
                continue
            if at_layer and line.startswith(";Z:"):
                at_layer = False
                try:
                    z = float(line[3:])
                except ValueError:
                    continue
                while pending and z + 1e-6 >= pending[0][0]:
                    fout.write(pending.pop(0)[1].rstrip() + " ; anycubic-slicer-mcp calibration\n")
                    inserted += 1
    return inserted
