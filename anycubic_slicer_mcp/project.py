"""Whole multi-part projects: find every model in a folder or zip, work out what's unclear, pack the
parts onto plates, then slice each plate.

The planner never guesses silently. Anything it can't settle from the files goes into `questions`
(blocking ones must be answered before slicing) or `assumptions` (safe defaults it states openly).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import analysis, gcode as gc, render
from .install import state_dir
from .mesh import load_mesh, rotation
from .slicer import MODEL_SUFFIXES, SliceRequest, Slicer, scan_3mf

MAX_ZIP_BYTES = 2 * 1024 ** 3
MAX_ZIP_ENTRIES = 5000
MAX_PARTS = 300
MAX_INSTANCES = 500
DOC_NAMES = re.compile(r"^(readme|instructions?|print[_ -]?(guide|settings|instructions)|bom|parts?[_ -]?list)", re.I)
DOC_SUFFIXES = {".md", ".txt", ".markdown"}
COLOUR_WORDS = ["black", "white", "red", "blue", "green", "yellow", "orange", "purple", "violet", "pink",
                "grey", "gray", "silver", "gold", "brown", "clear", "transparent", "translucent", "natural",
                "cyan", "magenta", "teal", "beige", "glow"]
ROLE_WORDS = {"accent": "accent", "primary": "primary", "secondary": "secondary", "main": "primary", "base": None}
TEST_WORDS = re.compile(r"(^|[_\-\s.])(test|tests|calib|calibration|tolerance|sample|benchy|fit[_\-\s]?check|"
                        r"fit[_\-\s]?test|clearance)([_\-\s.]|$)", re.I)
VERSION_MARK = re.compile(r"([_\-\s.]?(v|ver|rev|version)[_\-\s]?\d+([._]\d+)*|[_\-\s](final|old|new|copy|backup|"
                          r"bak|latest|fixed|updated)|\s?\(\d+\))$", re.I)
QTY_NAME = [
    re.compile(r"(?:^|[_\-\s.\[(])x\s?(\d{1,3})(?=$|[_\-\s.\])])", re.I),
    re.compile(r"(?:^|[_\-\s.\[(])(\d{1,3})\s?x(?=$|[_\-\s.\])])", re.I),
    re.compile(r"(?:qty|quantity|count)[_\-\s]?(\d{1,3})", re.I),
    re.compile(r"(\d{1,3})[_\-\s]?(?:pcs|pc|pieces|copies)(?=$|[_\-\s.])", re.I),
]
QTY_DOC = re.compile(r"(?:\b(\d{1,3})\s?(?:x|×|pcs|pieces|copies|off)\b|\b(?:x|×)\s?(\d{1,3})\b|\bqty[:\s]*(\d{1,3})\b|"
                     r"\bprint\s+(\d{1,3})\b)", re.I)
GOALS = ("fewest_plates", "by_colour")
SUPPORT_FRACTION = 0.04       # overhang area / surface area above this means the part needs supports


class ProjectError(ValueError):
    pass


# ------------------------------------------------------------------ files
def projects_dir() -> Path:
    d = state_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:40] or "project"


def safe_extract(archive: Path, dest: Path) -> list[Path]:
    """Extract a zip without letting entries escape `dest`, with size and count limits."""
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ProjectError(f"{archive.name} has {len(infos)} entries; the limit is {MAX_ZIP_ENTRIES}.")
        total = sum(i.file_size for i in infos)
        if total > MAX_ZIP_BYTES:
            raise ProjectError(f"{archive.name} unpacks to {total / 1e9:.1f} GB; the limit is 2 GB.")
        out = []
        root = dest.resolve()
        for info in infos:
            name = info.filename.replace("\\", "/")
            target = (root / name).resolve()
            if name.startswith("/") or ".." in name.split("/") or not target.is_relative_to(root) or ":" in name:
                raise ProjectError(f"{archive.name} contains an unsafe path ({info.filename}); refusing.")
            if info.is_dir() or name.startswith("__MACOSX/") or "/." in "/" + name:
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:   # symlink entries
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
        return out


def collect(source: Path) -> tuple[Path, list[Path], list[Path], str]:
    """(root folder, model files, doc files, kind) for a folder or a zip."""
    raw = str(source)
    if raw.replace("/", "\\").startswith("\\\\"):
        import os
        if os.environ.get("ANYCUBIC_MCP_ALLOW_NETWORK_PATHS") != "1":
            raise ProjectError("Network paths aren't allowed (set ANYCUBIC_MCP_ALLOW_NETWORK_PATHS=1 to permit).")
    source = source.expanduser()
    if not source.exists():
        raise ProjectError(f"Not found: {source}")
    source = source.resolve()
    if source.is_file() and source.suffix.lower() == ".zip":
        digest = hashlib.sha1(f"{source}:{source.stat().st_mtime}:{source.stat().st_size}".encode()).hexdigest()[:10]
        root = projects_dir() / f"{_slug(source.stem)}-{digest}" / "files"
        if not root.exists():
            root.mkdir(parents=True)
            try:
                safe_extract(source, root)
            except Exception:
                shutil.rmtree(root.parent, ignore_errors=True)
                raise
        kind = "zip"
        entries = [e for e in root.iterdir() if not e.name.startswith(".") and e.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():   # most zips wrap everything in one folder
            root = entries[0]
    elif source.is_dir():
        root, kind = source, "folder"
    elif source.is_file() and source.suffix.lower() in MODEL_SUFFIXES:
        root, kind = source.parent, "file"
    else:
        raise ProjectError("Point at a folder, a .zip, or a model file (STL, OBJ, 3MF, STEP).")
    if kind == "file":
        models = [source]
    else:
        models = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in MODEL_SUFFIXES
                        and not any(part.startswith(".") or part == "__MACOSX" for part in p.relative_to(root).parts))
    docs = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in DOC_SUFFIXES
                  and (DOC_NAMES.match(p.stem) or p.parent == root) and p.stat().st_size < 256 * 1024)
    if not models:
        raise ProjectError(f"No model files (STL, OBJ, 3MF, STEP) found in {source}.")
    if len(models) > MAX_PARTS:
        raise ProjectError(f"{len(models)} model files found; the limit is {MAX_PARTS}. Point at a smaller folder.")
    return root, models, docs, kind


# ------------------------------------------------------------------ name hints
def qty_from_name(stem: str) -> int | None:
    for rx in QTY_NAME:
        m = rx.search(stem)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 200:
                return n
    return None


def colour_from_name(stem: str) -> str | None:
    s = stem.lower()
    if re.match(r"^\[a\]", s):
        return "accent"
    tokens = re.split(r"[^a-z]+", s)
    for w in COLOUR_WORDS:
        if w in tokens:
            return {"gray": "grey", "transparent": "clear", "translucent": "clear"}.get(w, w)
    for w, role in ROLE_WORDS.items():
        if w in tokens and role:
            return role
    return None


def version_key(stem: str) -> tuple[str, int]:
    """('arm', 3) for 'arm_v3'; the number orders versions (0 when there is none)."""
    s = stem
    num = 0
    for _ in range(3):
        m = VERSION_MARK.search(s)
        if not m:
            break
        digits = re.findall(r"\d+", m.group(0))
        if digits and num == 0:
            num = int(digits[0])
        s = s[:m.start()]
    return re.sub(r"[_\-\s.]+", " ", s).strip().lower(), num


def qty_from_docs(stem: str, docs: dict[str, str]) -> tuple[int, str] | None:
    """A count for this part mentioned next to its name in a README, with the line as evidence."""
    keys = {stem.lower(), re.sub(r"[_\-]+", " ", stem.lower())}
    for name, text in docs.items():
        for line in text.splitlines():
            low = line.lower()
            if not any(k and k in low for k in keys):
                continue
            m = QTY_DOC.search(re.sub(re.escape(stem), " ", line, flags=re.I))
            if m:
                n = int(next(g for g in m.groups() if g))
                if 1 <= n <= 200:
                    return n, f"{name}: {line.strip()[:120]}"
    return None


# ------------------------------------------------------------------ parts
@dataclass
class Part:
    id: str
    path: str
    name: str
    qty: int = 1
    qty_source: str = "default"
    qty_evidence: str | None = None
    colour: str | None = None
    colour_source: str | None = None
    test_like: bool = False
    version_group: str | None = None
    version: int = 0
    scale: float = 1.0
    excluded: bool = False
    exclude_reason: str | None = None
    # geometry, filled by analyse()
    size_mm: list[float] = field(default_factory=list)
    volume_cm3: float = 0.0
    triangles: int = 0
    open_edges: int = 0
    rotation: dict[str, float] = field(default_factory=dict)
    orientation: str = ""
    oriented_mm: list[float] = field(default_factory=list)
    support_area_mm2: float = 0.0
    needs_support: bool = False
    fits: bool = True
    est_g: float = 0.0
    est_min: float = 0.0
    error: str | None = None


def estimate(volume_mm3: float, area_mm2: float, height_mm: float, support_mm2: float,
             density: float = 1.24) -> tuple[float, float]:
    """Rough grams and minutes before slicing, fitted to real slices of small and flat parts with a default
    0.2 mm process (within about 25%); slice_project reports the slicer's own numbers."""
    used = min(volume_mm3, 0.62 * area_mm2 + 0.14 * volume_mm3) + 0.3 * support_mm2
    grams = used / 1000 * density
    minutes = 1.9 * grams + 0.04 * height_mm / 0.2
    return round(grams, 1), round(minutes, 1)


def analyse(part: Part, slicer: Slicer, work: Path, bed: tuple[float, float, float]) -> None:
    src = Path(part.path)
    if src.suffix.lower() == ".3mf":
        scan_3mf(src)
    if src.suffix.lower() in {".step", ".stp"}:
        step_work = work / "step" / _slug(part.id)
        step_work.mkdir(parents=True, exist_ok=True)
        src = slicer.to_stl(src, step_work)
    mesh = load_mesh(src)
    if part.scale != 1.0:
        mesh = mesh.transformed(np.eye(3) * part.scale).on_bed()
    part.size_mm = [round(float(v), 1) for v in mesh.size()]
    part.volume_cm3 = round(abs(mesh.volume()) / 1000, 2)
    part.triangles = int(len(mesh.faces))
    part.open_edges = mesh.edge_report()["open_edges"]
    rows = analysis.rank_orientations(mesh, "balanced", bed, top=8)
    best = next((r for r in rows if r["fits_bed"]), rows[0])
    part.rotation, part.orientation = best["rotation"], best["orientation"]
    part.oriented_mm = [*best["footprint_mm"], best["height_mm"]]
    part.support_area_mm2 = best["support_area_mm2"]
    area = mesh.surface_area()
    part.needs_support = area > 0 and best["support_area_mm2"] / area > SUPPORT_FRACTION
    part.fits = bool(best["fits_bed"])
    part.est_g, part.est_min = estimate(abs(mesh.volume()), area, best["height_mm"], best["support_area_mm2"])


# ------------------------------------------------------------------ packing
class _Bin:
    """MaxRects bin with best-short-side-fit placement."""

    def __init__(self, w: float, d: float):
        self.w, self.d = w, d
        self.free = [(0.0, 0.0, w, d)]
        self.used: list[tuple[float, float, float, float]] = []

    def fit(self, w: float, d: float) -> tuple[float, float, float, float, float] | None:
        best = None
        for fx, fy, fw, fd in self.free:
            for rw, rd in ((w, d), (d, w)):
                if rw <= fw + 1e-6 and rd <= fd + 1e-6:
                    short = min(fw - rw, fd - rd)
                    if best is None or short < best[0]:
                        best = (short, fx, fy, rw, rd)
        return best

    def place(self, x: float, y: float, w: float, d: float) -> None:
        new = []
        for fx, fy, fw, fd in self.free:
            if x >= fx + fw or x + w <= fx or y >= fy + fd or y + d <= fy:
                new.append((fx, fy, fw, fd))
                continue
            if x > fx:
                new.append((fx, fy, x - fx, fd))
            if x + w < fx + fw:
                new.append((x + w, fy, fx + fw - x - w, fd))
            if y > fy:
                new.append((fx, fy, fw, y - fy))
            if y + d < fy + fd:
                new.append((fx, y + d, fw, fy + fd - y - d))
        self.free = [r for r in new if not any(
            r != o and r[0] >= o[0] and r[1] >= o[1] and r[0] + r[2] <= o[0] + o[2] and r[1] + r[3] <= o[1] + o[3]
            for o in new)]
        self.used.append((x, y, w, d))

    def fill(self) -> float:
        return sum(w * d for _, _, w, d in self.used) / (self.w * self.d)


def pack(parts: list[Part], bed: tuple[float, float, float], goal: str, spacing: float,
         max_minutes: float | None, separate_supported: bool) -> list[dict[str, Any]]:
    """Plates as {group, items: [part ids, one per copy], est_min, est_g, fill}."""
    usable_w, usable_d = bed[0] - 4, bed[1] - 4
    groups: dict[tuple, list[Part]] = {}
    for p in parts:
        key = ((p.colour or "main") if goal == "by_colour" else "all",
               p.needs_support if separate_supported else False)
        groups.setdefault(key, []).append(p)
    plates: list[dict[str, Any]] = []
    for key, members in sorted(groups.items(), key=lambda kv: (kv[0][0] != "main", str(kv[0]))):
        instances = [p for p in members for _ in range(p.qty)]
        instances.sort(key=lambda p: -(p.oriented_mm[0] + spacing) * (p.oriented_mm[1] + spacing))
        group_plates: list[tuple[_Bin, dict[str, Any]]] = []
        for p in instances:
            w, d = p.oriented_mm[0] + spacing, p.oriented_mm[1] + spacing
            if (w > usable_w + spacing and w > usable_d + spacing) or not p.fits:
                continue
            w, d = min(w, usable_w), min(d, usable_d)
            placed = False
            for b, plate in group_plates:
                if max_minutes and plate["est_min"] + p.est_min > max_minutes:
                    continue
                spot = b.fit(w, d)
                if spot:
                    b.place(spot[1], spot[2], spot[3], spot[4])
                    plate["items"].append(p.id)
                    plate["est_min"] += p.est_min
                    plate["est_g"] += p.est_g
                    placed = True
                    break
            if not placed:
                b = _Bin(usable_w, usable_d)
                spot = b.fit(w, d)
                b.place(spot[1], spot[2], spot[3], spot[4])
                plate = {"colour": key[0] if goal == "by_colour" else None, "supported": key[1] if separate_supported else None,
                         "items": [p.id], "est_min": p.est_min + 3, "est_g": p.est_g}   # 3 min: heat-up and homing
                group_plates.append((b, plate))
        for b, plate in group_plates:
            plate["fill"] = round(b.fill(), 2)
            plates.append(plate)
    for i, plate in enumerate(plates, 1):
        plate["plate"] = i
        plate["est_min"] = round(plate["est_min"], 1)
        plate["est_g"] = round(plate["est_g"], 1)
    return plates


# ------------------------------------------------------------------ questions
def _q(qid: str, question: str, why: str, options: list[str], default: str | None, blocking: bool,
       answer_with: str, parts: list[str] | None = None) -> dict[str, Any]:
    q = {"id": qid, "blocking": blocking, "question": question, "why": why, "options": options,
         "default": default, "answer_with": answer_with}
    if parts:
        q["parts"] = parts
    return q


def find_questions(parts: list[Part], inputs: dict[str, Any], bed: tuple[float, float, float]) -> tuple[list, list]:
    questions: list[dict[str, Any]] = []
    assumptions: list[str] = []
    active = [p for p in parts if not p.excluded]
    answered = set(inputs.get("answered") or [])

    # versions of the same part
    groups: dict[str, list[Part]] = {}
    for p in parts:
        if p.version_group:
            groups.setdefault(p.version_group, []).append(p)
    for key, members in groups.items():
        live = [m for m in members if not m.excluded]
        if len(live) > 1 and f"versions:{key}" not in answered:
            newest = max(live, key=lambda m: (m.version, Path(m.path).stat().st_mtime))
            questions.append(_q(
                f"versions:{key}", f"These look like versions of the same part: {', '.join(m.id for m in live)}. Which should be printed?",
                "Printing every version wastes filament and plate space.",
                [m.id for m in live] + ["all of them"], newest.id, True,
                "exclude=[the ones not to print], or answered=['versions:" + key + "'] to print all", [m.id for m in live]))

    for p in active:
        big = max(p.size_mm) if p.size_mm else 0
        if p.error:
            questions.append(_q(f"unreadable:{p.id}", f"{p.id} couldn't be read ({p.error}). Skip it?",
                                "The slicer can't print a file the planner can't load.", ["skip it"], "skip it", True,
                                f"exclude=['{p.id}']", [p.id]))
            continue
        if big and big < 5 and f"scale:{p.id}" not in answered and p.scale == 1.0:
            opts = []
            if big * 25.4 <= max(bed):
                opts.append("scale 25.4 (it was exported in inches)")
            opts += ["scale 10 (it was in cm)", "keep it this small"]
            questions.append(_q(f"scale:{p.id}", f"{p.id} is only {big:.1f} mm across. Was it exported in other units?",
                                "Tiny parts usually mean the file was saved in inches or cm.", opts, opts[0], True,
                                f"scales={{'{p.id}': 25.4}} (or 10), or answered=['scale:{p.id}'] to keep it", [p.id]))
        elif big > 2000 and f"scale:{p.id}" not in answered and p.scale == 1.0:
            questions.append(_q(f"scale:{p.id}", f"{p.id} is {big / 1000:.1f} m across. Was it exported in micrometres or wrong units?",
                                "No desktop printer can print that size.", ["scale 0.1", "scale 0.01", "skip it"], "scale 0.1",
                                True, f"scales={{'{p.id}': 0.1}}, or exclude=['{p.id}']", [p.id]))
        elif not p.fits:
            fit = min((bed[0] - 4) / max(p.oriented_mm[0], 1e-6), (bed[1] - 4) / max(p.oriented_mm[1], 1e-6),
                      bed[2] / max(p.oriented_mm[2], 1e-6))
            pct = max(1, math.floor(min(fit, 1) * 100))
            questions.append(_q(f"too_big:{p.id}", f"{p.id} ({' × '.join(f'{v:g}' for v in p.size_mm)} mm) doesn't fit the "
                                f"{bed[0]:g} × {bed[1]:g} × {bed[2]:g} mm build volume in any orientation. What should I do?",
                                "It can't be printed in one piece on this printer.",
                                [f"scale it to {pct}%", "skip it (I'll split it in CAD)"], "skip it (I'll split it in CAD)",
                                True, f"scales={{'{p.id}': {pct / 100:g}}} or exclude=['{p.id}']", [p.id]))
        if p.open_edges and f"mesh:{p.id}" not in answered:
            questions.append(_q(f"mesh:{p.id}", f"{p.id} has {p.open_edges} holes in its mesh. Slice it anyway?",
                                "Open meshes can slice with missing walls; the slicer usually repairs small gaps.",
                                ["slice it anyway", "skip it"], "slice it anyway", False,
                                f"exclude=['{p.id}'] to skip", [p.id]))

    tests = [p for p in parts if p.test_like and p.excluded and p.exclude_reason == "test piece"]
    if tests and "tests" not in answered:
        questions.append(_q("tests", f"These look like test or calibration pieces: {', '.join(p.id for p in tests)}. "
                            "I've left them out. Print them too?", "Test pieces are usually printed once, before the project.",
                            ["leave them out", "print them too"], "leave them out", False,
                            "include=[ids] to print them", [p.id for p in tests]))

    no_count = [p for p in active if p.qty_source == "default"]
    counted = [p for p in active if p.qty_source != "default"]
    if len(no_count) >= 2 and "quantities" not in answered:
        questions.append(_q("quantities", f"{len(no_count)} parts have no count in their name or the README, so I'm printing one of each"
                            + (f" ({', '.join(p.id for p in no_count[:6])}{'…' if len(no_count) > 6 else ''})." if no_count else ".")
                            + " Need more of any?", "Kits often need several of the same clip, hinge or foot.",
                            ["one of each is right", "give counts"], "one of each is right",
                            False, "quantities={'part.stl': 4}, or answered=['quantities']",
                            [p.id for p in no_count]))
    for p in counted:
        assumptions.append(f"{p.id}: printing {p.qty} ({'from its name' if p.qty_source == 'name' else p.qty_evidence}).")

    colours = sorted({p.colour for p in active if p.colour})
    if not inputs.get("goal") and "goal" not in answered:
        groups_n = len(colours) + (1 if any(not p.colour for p in active) else 0)
        default = "by_colour" if groups_n >= 3 else "fewest_plates"
        hint = ""
        if colours:
            hint = (f" Some file names mention {', '.join(colours)}" + (", the rest have no colour." if any(not p.colour for p in active) else "."))
        questions.append(_q("goal", "How should I group the plates?" + hint,
                            "By colour means one spool per plate; fewest plates mixes colours on a plate.",
                            ["fewest_plates", "by_colour"], default, False, "goal='fewest_plates' or 'by_colour'"))
    if colours and (inputs.get("goal") == "by_colour" or (not inputs.get("goal") and len(colours) >= 2)):
        plain = [p.id for p in active if not p.colour]
        if plain and "colours" not in answered:
            questions.append(_q("colours", f"{len(plain)} parts have no colour in their name. Print them in the main colour?",
                                "Grouping by colour needs a colour for every part.", ["yes, main colour", "give colours"],
                                "yes, main colour", False, "colours={'part.stl': 'red'}, or answered=['colours']", plain))

    supported = [p for p in active if p.needs_support and p.fits]
    if supported and len(active) - len(supported) >= 3 and inputs.get("separate_supported") is None and "supports" not in answered:
        questions.append(_q("supports", f"{len(supported)} parts need supports ({', '.join(p.id for p in supported[:5])}"
                            f"{'…' if len(supported) > 5 else ''}). Put them on their own plates?",
                            "Supports turn on for a whole plate, so mixing adds support time to plates that don't need it.",
                            ["keep them together with the rest", "separate plates for supported parts"],
                            "keep them together with the rest", False, "separate_supported=true or false"))
    total_min = sum(p.est_min * p.qty for p in active if p.fits)
    if total_min > 12 * 60 and not inputs.get("max_hours_per_plate") and "time_limit" not in answered:
        questions.append(_q("time_limit", f"The whole project is roughly {total_min / 60:.0f} hours of printing. Cap each plate at a number of hours (say, overnight)?",
                            "Long plates are riskier; a cap makes more, shorter plates.", ["no cap", "8 hours", "12 hours"],
                            "no cap", False, "max_hours_per_plate=8"))
    questions.sort(key=lambda q: (not q["blocking"], q["id"]))
    return questions, assumptions


# ------------------------------------------------------------------ plan
INPUT_KEYS = ("goal", "max_hours_per_plate", "separate_supported", "quantities", "exclude", "include", "scales",
              "colours", "answered", "printer", "process", "filaments", "overrides", "spacing_mm")


def merge_inputs(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out = dict(old)
    for k, v in new.items():
        if v is None:
            continue
        if k in ("quantities", "scales", "colours") and isinstance(v, dict):
            out[k] = {**(old.get(k) or {}), **v}
        elif k in ("exclude", "include", "answered") and isinstance(v, list):
            out[k] = sorted(set(old.get(k) or []) | set(v))
        else:
            out[k] = v
    return out


def build_plan(slicer: Slicer, source: str | None, plan_id: str | None, inputs: dict[str, Any]) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    if plan_id:
        previous = load_plan(plan_id)
        source = source or previous["source"]
        inputs = merge_inputs(previous.get("inputs", {}), inputs)
    if not source:
        raise ProjectError("Give a folder or .zip (path) or the plan_id of an earlier plan.")
    goal = inputs.get("goal") or "fewest_plates"
    if goal not in GOALS:
        raise ProjectError(f"goal must be one of {', '.join(GOALS)}")
    root, models, docs, kind = collect(Path(source))
    doc_text = {d.name: d.read_text(encoding="utf-8", errors="replace") for d in docs}
    slicer.library.reload()
    chosen = slicer.choose(inputs.get("printer"), inputs.get("process"), inputs.get("filaments"))
    bed = analysis.bed_size(slicer.library.resolve("machine", chosen["printer"]))
    pid = plan_id or f"{_slug(Path(source).stem)}-{hashlib.sha1(f'{source}{time.time()}'.encode()).hexdigest()[:8]}"
    work = projects_dir() / pid
    work.mkdir(parents=True, exist_ok=True)
    cache = {p["id"]: p for p in previous.get("parts", [])}

    parts: list[Part] = []
    for f in models:
        rel = f.relative_to(root).as_posix() if kind != "file" else f.name
        stem = f.stem
        p = Part(id=rel, path=str(f), name=stem)
        q = qty_from_name(stem)
        if q:
            p.qty, p.qty_source = q, "name"
        else:
            hit = qty_from_docs(stem, doc_text)
            if hit:
                p.qty, p.qty_source, p.qty_evidence = hit[0], "readme", hit[1]
        p.colour = colour_from_name(stem)
        p.colour_source = "name" if p.colour else None
        p.test_like = bool(TEST_WORDS.search(stem))
        vkey, vnum = version_key(stem)
        folder = Path(rel).parent.as_posix() if kind != "file" else ""
        p.version_group, p.version = f"{folder}/{vkey}".lstrip("./"), vnum
        # user answers
        if rel in (inputs.get("quantities") or {}):
            p.qty, p.qty_source = int(inputs["quantities"][rel]), "answer"
        if rel in (inputs.get("colours") or {}):
            p.colour, p.colour_source = str(inputs["colours"][rel]).lower(), "answer"
        p.scale = float((inputs.get("scales") or {}).get(rel, 1.0))
        if not 0.001 <= p.scale <= 1000:
            raise ProjectError(f"scale for {rel} must be between 0.001 and 1000")
        included = rel in (inputs.get("include") or [])
        if p.test_like and not included:
            p.excluded, p.exclude_reason = True, "test piece"
        if rel in (inputs.get("exclude") or []) and not included:
            p.excluded, p.exclude_reason = True, "you asked to leave it out"
        if not 1 <= p.qty <= 200:
            raise ProjectError(f"quantity for {rel} must be between 1 and 200")
        # reuse geometry from the earlier plan when nothing about the file changed
        old = cache.get(rel)
        if old and old.get("scale") == p.scale and old.get("size_mm") and not old.get("error"):
            for k in ("size_mm", "volume_cm3", "triangles", "open_edges", "rotation", "orientation", "oriented_mm",
                      "support_area_mm2", "needs_support", "fits", "est_g", "est_min"):
                setattr(p, k, old[k])
        elif not p.excluded or p.exclude_reason == "test piece" or len(models) <= 40:
            try:
                analyse(p, slicer, work, bed)
            except Exception as exc:  # keep planning; the question list will ask what to do
                p.error = str(exc)[:200]
        parts.append(p)
    # version groups only matter when more than one file shares a base name
    counts: dict[str, int] = {}
    for p in parts:
        counts[p.version_group or ""] = counts.get(p.version_group or "", 0) + 1
    for p in parts:
        if counts.get(p.version_group or "", 0) < 2:
            p.version_group = None

    questions, assumptions = find_questions(parts, inputs, bed)
    printable = [p for p in parts if not p.excluded and p.fits and not p.error and p.oriented_mm]
    if sum(p.qty for p in printable) > MAX_INSTANCES:
        raise ProjectError(f"More than {MAX_INSTANCES} pieces in total; lower some quantities.")
    max_min = float(inputs["max_hours_per_plate"]) * 60 if inputs.get("max_hours_per_plate") else None
    plates = pack(printable, bed, goal, float(inputs.get("spacing_mm") or 6), max_min, bool(inputs.get("separate_supported")))
    by_id = {p.id: p for p in parts}
    for plate in plates:
        counted: dict[str, int] = {}
        for i in plate["items"]:
            counted[i] = counted.get(i, 0) + 1
        plate["parts"] = [{"id": k, "count": v} for k, v in counted.items()]
        colours = sorted({by_id[i].colour or "main" for i in counted})
        if goal == "fewest_plates" and len(colours) > 1:
            plate["note"] = f"Mixes colours ({', '.join(colours)}); it's sliced with one filament."
        if any(by_id[i].needs_support for i in counted):
            plate["supports"] = True
        if max_min and plate["est_min"] > max_min:
            plate["note"] = (plate.get("note", "") + f" Over your {inputs['max_hours_per_plate']:g} h cap: "
                             f"{plate['parts'][0]['id']} alone takes about that long.").strip()
    if goal == "fewest_plates":
        assumptions.append("Grouping for the fewest plates; colours may share a plate.")
    if any(p.needs_support for p in printable):
        assumptions.append("Supports are switched on for plates that hold a part that needs them.")
    assumptions.append("Times and weights in the plan are rough estimates; slice_project reports the slicer's numbers.")
    plan = {
        "plan_id": pid, "source": str(source), "root": str(root), "kind": kind, "created": time.time(),
        "printer": chosen["printer"], "process": chosen["process"], "filaments": chosen["filaments"],
        "bed_mm": list(bed), "goal": goal, "inputs": inputs,
        "parts": [asdict(p) for p in parts], "plates": plates,
        "questions": questions, "assumptions": assumptions,
        "docs_read": list(doc_text),
    }
    (work / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    return plan


def load_plan(plan_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]{3,60}", plan_id or ""):
        raise ProjectError("Unknown plan_id.")
    f = projects_dir() / plan_id / "plan.json"
    if not f.is_file():
        raise ProjectError(f"No plan called {plan_id}. Run plan_project first.")
    return json.loads(f.read_text(encoding="utf-8"))


def summary(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as the agent should show it: short part list, plates, questions first."""
    parts = plan["parts"]
    blocking = [q for q in plan["questions"] if q["blocking"]]
    return {
        "plan_id": plan["plan_id"],
        "printer": plan["printer"], "goal": plan["goal"], "bed_mm": plan["bed_mm"],
        "ready_to_slice": not blocking,
        "questions": plan["questions"],
        "assumptions": plan["assumptions"],
        "parts": [{"id": p["id"], "qty": p["qty"], "size_mm": p["size_mm"], "orientation": p["orientation"],
                   "needs_support": p["needs_support"], "colour": p["colour"],
                   **({"excluded": p["exclude_reason"]} if p["excluded"] else {}),
                   **({"error": p["error"]} if p["error"] else {})} for p in parts],
        "plates": [{k: v for k, v in pl.items() if k != "items"} for pl in plan["plates"]],
        "totals_estimate": {"plates": len(plan["plates"]),
                            "minutes": round(sum(pl["est_min"] for pl in plan["plates"])),
                            "grams": round(sum(pl["est_g"] for pl in plan["plates"]), 1)},
        "next_step": ("Ask the user the blocking questions (a few at a time, most important first), then call plan_project "
                      "again with plan_id and the answers." if blocking else
                      "Show the user the plates and assumptions; ask the non-blocking questions if they matter; "
                      "when they approve, call slice_project(plan_id)."),
    }


# ------------------------------------------------------------------ slicing
def slice_plan(slicer: Slicer, plan_id: str, plates: list[int] | None, output_dir: str | None) -> dict[str, Any]:
    plan = load_plan(plan_id)
    blocking = [q for q in plan["questions"] if q["blocking"]]
    if blocking:
        raise ProjectError("These questions need answers before slicing: "
                           + "; ".join(q["question"] for q in blocking)
                           + " Ask the user, then call plan_project(plan_id=..., ...) with the answers.")
    if not plan["plates"]:
        raise ProjectError("The plan has no plates to slice (every part was excluded or doesn't fit).")
    by_id = {p["id"]: p for p in plan["parts"]}
    work = projects_dir() / plan_id
    oriented_dir = work / "oriented"
    oriented_dir.mkdir(exist_ok=True)
    out = Path(output_dir).expanduser() if output_dir else Path.home() / "Documents" / "Anycubic Slicer MCP" / plan_id
    out.mkdir(parents=True, exist_ok=True)

    def oriented(pid: str) -> str:
        p = by_id[pid]
        dest = oriented_dir / f"{_slug(Path(pid).with_suffix('').as_posix())}.stl"
        if not dest.exists():
            src = Path(p["path"])
            if src.suffix.lower() in {".step", ".stp"}:
                step_work = work / "step" / _slug(pid)
                step_work.mkdir(parents=True, exist_ok=True)
                src = slicer.to_stl(src, step_work)
            r = p["rotation"] or {}
            m = rotation(r.get("x", 0), r.get("y", 0), r.get("z", 0)) * float(p["scale"])
            load_mesh(src).transformed(m).on_bed().save_stl(dest)
        return str(dest)

    queue = [list(pl["items"]) for pl in plan["plates"]]
    wanted = set(plates or range(1, len(queue) + 1))
    results, moved = [], []
    n = 0
    while n < len(queue):
        items = queue[n]
        n += 1
        if n not in wanted and n <= len(plan["plates"]):
            continue
        attempt = 0
        while True:
            attempt += 1
            supports = any(by_id[i]["needs_support"] for i in items)
            overrides = dict(plan["inputs"].get("overrides") or {})
            if supports:
                overrides.setdefault("enable_support", True)
            req = SliceRequest(models=[oriented(i) for i in items], printer=plan["printer"], process=plan["process"],
                               filaments=plan["filaments"], overrides=overrides, output_dir=str(out),
                               output_name=f"plate_{n}", arrange=True)
            res = slicer.slice(req)
            plate = res["plates"][0]
            outside = plate.get("outside_bed") or any("outside" in w for w in res["warnings"])
            if outside and len(items) > 1 and attempt < 8:
                spill = items.pop()                      # the smallest piece moves to the next plate
                moved.append({"part": spill, "from_plate": n})
                if n >= len(queue):
                    queue.append([])
                queue[n].insert(0, spill)
                wanted.add(n + 1)
                continue
            break
        counted: dict[str, int] = {}
        for i in items:
            counted[i] = counted.get(i, 0) + 1
        preview = out / f"plate_{n}.png"
        try:
            png, _ = render.top_view(gc.read_toolpaths(plate["gcode"]), None, 700, f"plate {n}")
            preview.write_bytes(png)
        except Exception:
            preview = None
        results.append({
            "plate": n, "parts": [{"id": k, "count": v} for k, v in counted.items()],
            "time": plate.get("time_normal"), "time_s": plate.get("time_normal_s"),
            "filament_g": plate.get("total_filament_g"), "layers": plate.get("layers"),
            "supports": supports, "gcode": plate["gcode"], "gcode_3mf": res["gcode_3mf"],
            "preview": str(preview) if preview else None,
            "warnings": res["warnings"],
        })
    cap = plan["inputs"].get("max_hours_per_plate")
    if cap:
        for r in results:
            if (r["time_s"] or 0) > cap * 3600:
                r["warnings"].append(f"Takes {r['time']}, over your {cap:g} h cap. Re-plan with a lower cap or split this plate.")
    total_s = sum(r["time_s"] or 0 for r in results)
    total_g = round(sum(r["filament_g"] or 0 for r in results), 2)
    report = {
        "plan_id": plan_id, "printer": plan["printer"], "output_dir": str(out),
        "plates": results, "moved_to_fit": moved,
        "totals": {"plates": len(results), "time": gc.format_duration(total_s), "filament_g": total_g},
        "note": "Each plate is a separate .gcode.3mf you can send to the printer or open in the slicer.",
    }
    (out / "project_summary.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report
