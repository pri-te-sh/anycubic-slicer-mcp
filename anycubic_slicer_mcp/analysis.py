"""Geometry-based print planning: best orientation, strength advice and build-volume fit."""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .mesh import Mesh, euler_xyz, rotation, rotation_to_down

AXES = {"x": np.array([1.0, 0, 0]), "y": np.array([0, 1.0, 0]), "z": np.array([0, 0, 1.0])}


def parse_axis(axis: str | list[float] | None) -> np.ndarray | None:
    if axis is None or axis == "":
        return None
    if isinstance(axis, str):
        a = axis.strip().lower().lstrip("+-")
        if a not in AXES:
            raise ValueError("load_axis must be 'x', 'y', 'z' or a [x, y, z] vector (model coordinates)")
        return AXES[a]
    v = np.asarray(axis, float)
    if v.shape != (3,) or np.linalg.norm(v) == 0:
        raise ValueError("load_axis vector must have three numbers, not all zero")
    return v / np.linalg.norm(v)


# ------------------------------------------------------------------ orientations
def candidate_rotations(mesh: Mesh, max_faces: int = 6) -> list[tuple[str, np.ndarray]]:
    cands: list[tuple[str, np.ndarray]] = [
        ("as modelled", np.eye(3)),
        ("upside down", rotation(x_deg=180)),
        ("on its front (X +90°)", rotation(x_deg=90)),
        ("on its back (X −90°)", rotation(x_deg=-90)),
        ("on its left side (Y +90°)", rotation(y_deg=90)),
        ("on its right side (Y −90°)", rotation(y_deg=-90)),
    ]
    normals, areas = mesh.face_normals_and_areas()
    if len(areas):
        key = np.round(normals / 0.08) * 0.08
        uniq, inv = np.unique(key, axis=0, return_inverse=True)
        sums = np.bincount(inv.ravel(), weights=areas)
        total = areas.sum()
        order = np.argsort(-sums)
        added = 0
        for idx in order:
            if sums[idx] < 0.03 * total or added >= max_faces:
                break
            n = normals[inv.ravel() == idx]
            w = areas[inv.ravel() == idx]
            avg = (n * w[:, None]).sum(0)
            if np.linalg.norm(avg) == 0:
                continue
            cands.append((f"flat face {added + 1} down ({sums[idx]:.0f} mm²)", rotation_to_down(avg)))
            added += 1
    # drop duplicates (same direction ends up pointing down)
    unique, seen = [], []
    for name, R in cands:
        down = R.T @ np.array([0, 0, -1.0])
        if any(np.dot(down, s) > 0.995 for s in seen):
            continue
        seen.append(down)
        unique.append((name, R))
    return unique


def neck_ratio(areas: list[float]) -> float:
    """How narrow the narrowest 'waist' is: a layer smaller than material both above and below it.
    1.0 = no waist. A small top feature (like a hub) is not a waist."""
    a = np.asarray(areas, float)
    worst = 1.0
    for i in range(1, len(a) - 1):
        above, below = a[i + 1:].max(), a[:i].max()
        ref = min(above, below)
        if ref > 0:
            worst = min(worst, a[i] / ref)
    return float(max(worst, 0.0))


def evaluate(mesh: Mesh, R: np.ndarray, bed: tuple[float, float, float] | None,
             load_axis: np.ndarray | None, threshold_deg: float) -> dict[str, Any]:
    m = mesh.transformed(R).on_bed()
    size = m.size()
    ov = m.overhangs(threshold_deg)
    prof = m.section_profile(z_samples=24, y_samples=32)
    weak = neck_ratio([a for _, a in prof])
    fits = None
    if bed:
        bx, by, bz = bed
        fits = bool(size[2] <= bz + 1e-6 and (
            (size[0] <= bx and size[1] <= by) or (size[1] <= bx and size[0] <= by)))
    align = float(abs((R @ load_axis)[2])) if load_axis is not None else None
    return {
        "height_mm": round(float(size[2]), 2),
        "footprint_mm": [round(float(size[0]), 1), round(float(size[1]), 1)],
        "support_area_mm2": round(ov["overhang_area_mm2"], 1),
        "bed_contact_mm2": round(ov["bed_contact_mm2"], 1),
        "weakest_layer_ratio": round(min(weak, 1.0), 3),
        "load_along_layers": None if align is None else round(1 - align, 3),
        "fits_bed": fits,
    }


GOALS = ("balanced", "least_support", "fastest", "strongest", "best_bed_adhesion")


def rank_orientations(mesh: Mesh, goal: str = "balanced", bed: tuple[float, float, float] | None = None,
                      load_axis: np.ndarray | None = None, threshold_deg: float = 45.0,
                      top: int = 5) -> list[dict[str, Any]]:
    if goal not in GOALS:
        raise ValueError(f"goal must be one of {', '.join(GOALS)}")
    rows = []
    for name, R in candidate_rotations(mesh):
        rows.append({"orientation": name, "rotation": euler_xyz(R), **evaluate(mesh, R, bed, load_axis, threshold_deg)})
    sup = max(max(r["support_area_mm2"] for r in rows), 1.0)
    hgt = max(max(r["height_mm"] for r in rows), 1.0)
    bed_c = max(max(r["bed_contact_mm2"] for r in rows), 1.0)
    w = {
        "balanced": (1.0, 0.6, 0.5, 0.6),
        "least_support": (2.0, 0.2, 0.3, 0.2),
        "fastest": (0.8, 2.0, 0.2, 0.1),
        "strongest": (0.6, 0.2, 0.3, 2.0),
        "best_bed_adhesion": (0.6, 0.3, 2.0, 0.2),
    }[goal]
    for r in rows:
        strength = (1 - r["weakest_layer_ratio"]) if r["load_along_layers"] is None else (1 - r["load_along_layers"])
        cost = (w[0] * r["support_area_mm2"] / sup + w[1] * r["height_mm"] / hgt
                + w[2] * (1 - r["bed_contact_mm2"] / bed_c) + w[3] * strength)
        if r["fits_bed"] is False:
            cost += 10
        r["score"] = round(100 / (1 + cost), 1)
    rows.sort(key=lambda r: -r["score"])
    return rows[:top]


# ------------------------------------------------------------------ strength
MATERIAL_NOTES = {
    "PLA": "Stiff but brittle; softens above ~55 °C. Fine for brackets indoors, not for snap-fits or heat.",
    "PETG": "Tougher and more flexible than PLA, good layer bonding; a solid default for functional parts.",
    "ABS": "Tough and heat-resistant (~95 °C) but warps; needs an enclosure.",
    "ASA": "Like ABS with UV resistance for outdoor parts; needs an enclosure.",
    "TPU": "Flexible and impact-absorbing; print slowly.",
    "PA": "Nylon: very tough and wear-resistant; must be dried.",
    "PC": "Very strong and heat-resistant; needs high temperatures and an enclosure.",
}


def strength_plan(mesh: Mesh, load: str = "general", load_axis: np.ndarray | None = None,
                  priority: str = "balanced", material: str | None = None, nozzle_mm: float = 0.4,
                  bed: tuple[float, float, float] | None = None) -> dict[str, Any]:
    loads = ("general", "tension", "compression", "bending", "impact", "torsion")
    priorities = ("max_strength", "balanced", "light")
    if load not in loads:
        raise ValueError(f"load must be one of {', '.join(loads)}")
    if priority not in priorities:
        raise ValueError(f"priority must be one of {', '.join(priorities)}")
    best = rank_orientations(mesh, "strongest", bed, load_axis, top=3)
    walls = {"max_strength": 6, "balanced": 4, "light": 3}[priority]
    shells = {"max_strength": 6, "balanced": 5, "light": 4}[priority]
    density = {"max_strength": 45, "balanced": 25, "light": 15}[priority]
    pattern = "gyroid"
    reasons = [f"{walls} walls: walls carry most of the load in FDM parts, so they add strength faster than infill."]
    if load in ("compression",):
        pattern, density = "cubic", density + 10
        reasons.append("Compression: denser cubic infill resists crushing in every direction.")
    elif load in ("bending", "torsion"):
        walls += 1
        reasons.append(f"{load.title()}: stress concentrates at the surface, so an extra wall beats more infill.")
    elif load == "impact":
        pattern = "gyroid"
        reasons.append("Impact: gyroid infill spreads shocks evenly; tougher filament (PETG, ABS, PA) matters more than density.")
    elif load == "tension":
        reasons.append("Tension: keep the pull direction along the layers (in X/Y), never across them.")
    reasons.append(f"{shells} top/bottom layers close the part solidly; {density}% {pattern} infill supports the walls.")
    overrides: dict[str, Any] = {
        "wall_loops": walls, "top_shell_layers": shells, "bottom_shell_layers": shells,
        "sparse_infill_density": f"{density}%", "sparse_infill_pattern": pattern,
    }
    layer = round(min(0.2, 0.5 * nozzle_mm), 2)
    overrides["layer_height"] = layer
    reasons.append(f"{layer} mm layers keep layer bonding strong for a {nozzle_mm} mm nozzle.")
    weak = best[0]["weakest_layer_ratio"] if best else 1.0
    findings = []
    if weak < 0.35:
        findings.append(f"There's a narrow section (about {weak:.0%} of the typical cross-section) where layers "
                        "could split; consider a fillet or thicker section there, or the orientation below.")
    if load_axis is not None and best and best[0]["load_along_layers"] is not None and best[0]["load_along_layers"] < 0.7:
        findings.append("No orientation puts the load fully along the layers; expect the part to be weaker along Z.")
    mat = (material or "").upper()
    note = next((v for k, v in MATERIAL_NOTES.items() if mat.startswith(k)), None)
    return {
        "recommended_orientation": best[0] if best else None,
        "other_orientations": best[1:],
        "overrides": overrides,
        "why": reasons,
        "findings": findings,
        "material_note": note,
        "also_consider": [
            "Raise nozzle temperature 5–10 °C and reduce cooling fan for better layer bonding (filament settings).",
            "Add fillets to sharp inside corners in the CAD model; they're the usual crack starters.",
        ],
    }


# ------------------------------------------------------------------ build volume
def bed_size(machine_settings: dict[str, Any]) -> tuple[float, float, float]:
    area = machine_settings.get("printable_area") or ["0x0", "220x0", "220x220", "0x220"]
    pts = [tuple(float(v) for v in str(p).split("x")) for p in area]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    height = float(str(machine_settings.get("printable_height", 250)))
    return max(xs) - min(xs), max(ys) - min(ys), height


def fit_report(mesh: Mesh, bed: tuple[float, float, float], margin: float = 2.0) -> dict[str, Any]:
    sx, sy, sz = (float(v) for v in mesh.size())
    bx, by, bz = bed[0] - 2 * margin, bed[1] - 2 * margin, bed[2]
    fits_flat = (sx <= bx and sy <= by) or (sy <= bx and sx <= by)
    diag_ok = False
    if not fits_flat and sz <= bz:
        footprint = mesh.transformed(rotation(z_deg=45)).size()
        diag_ok = footprint[0] <= bx and footprint[1] <= by
    report: dict[str, Any] = {
        "model_mm": [round(sx, 1), round(sy, 1), round(sz, 1)],
        "build_volume_mm": [round(bed[0], 1), round(bed[1], 1), round(bed[2], 1)],
        "fits": bool(fits_flat and sz <= bz),
        "headroom_mm": [round(bx - min(sx, sy), 1), round(by - max(sx, sy), 1), round(bz - sz, 1)],
    }
    if not report["fits"]:
        tips = []
        if diag_ok:
            tips.append("Rotate 45° around Z: it fits diagonally (rotation={'z': 45}).")
        scale = min(bx / max(sx, 1e-6), by / max(sy, 1e-6), bz / max(sz, 1e-6), by / max(sx, 1e-6) if sx > sy else 1)
        tips.append(f"Scale to {math.floor(min(scale, 1) * 100)}% or less to fit as-is.")
        tips.append("Or split the model into parts in CAD and join them after printing.")
        if sz > bz and max(sx, sy) <= bz:
            tips.append("Lay it on its side: it's taller than the printer but would fit lying down "
                        "(see find_best_orientation).")
        report["suggestions"] = tips
    return report
