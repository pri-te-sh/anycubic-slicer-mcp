"""Filament materials, and turning what's printed on a spool label into a slicer filament preset."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presets import PresetLibrary, as_list, first

# Typical ranges used when a label leaves something out. (nozzle min, max, typical), bed, density g/cm³,
# max volumetric speed mm³/s, cooling fan %.
MATERIALS: dict[str, dict[str, Any]] = {
    "PLA":      {"nozzle": (190, 230, 210), "bed": 60, "density": 1.24, "mvs": 15, "fan": 100},
    "PLA+":     {"nozzle": (200, 230, 215), "bed": 60, "density": 1.24, "mvs": 15, "fan": 100},
    "PLA-CF":   {"nozzle": (200, 240, 220), "bed": 60, "density": 1.30, "mvs": 12, "fan": 100},
    "SILK PLA": {"nozzle": (200, 230, 215), "bed": 60, "density": 1.24, "mvs": 10, "fan": 100},
    "PETG":     {"nozzle": (220, 260, 240), "bed": 75, "density": 1.27, "mvs": 10, "fan": 40},
    "PETG-CF":  {"nozzle": (230, 270, 250), "bed": 75, "density": 1.30, "mvs": 9, "fan": 40},
    "ABS":      {"nozzle": (230, 270, 250), "bed": 100, "density": 1.04, "mvs": 14, "fan": 20},
    "ASA":      {"nozzle": (240, 270, 255), "bed": 100, "density": 1.07, "mvs": 12, "fan": 20},
    "TPU":      {"nozzle": (200, 240, 220), "bed": 45, "density": 1.21, "mvs": 3.6, "fan": 70},
    "PA":       {"nozzle": (250, 290, 270), "bed": 90, "density": 1.14, "mvs": 8, "fan": 0},
    "PA-CF":    {"nozzle": (260, 300, 280), "bed": 90, "density": 1.20, "mvs": 8, "fan": 0},
    "PC":       {"nozzle": (260, 300, 280), "bed": 110, "density": 1.20, "mvs": 10, "fan": 0},
    "HIPS":     {"nozzle": (220, 250, 235), "bed": 100, "density": 1.04, "mvs": 12, "fan": 20},
    "PVA":      {"nozzle": (190, 220, 205), "bed": 55, "density": 1.23, "mvs": 6, "fan": 100},
}
ALIASES = {"NYLON": "PA", "PLA PLUS": "PLA+", "PLA PRO": "PLA+", "SILK": "SILK PLA", "PLA SILK": "SILK PLA",
           "TPU 95A": "TPU", "FLEX": "TPU", "PETG HF": "PETG", "HS PETG": "PETG", "HS PLA": "PLA",
           "PLA HS": "PLA", "HIGH SPEED PLA": "PLA", "PLA MATTE": "PLA", "MATTE PLA": "PLA", "PA6": "PA",
           "PA12": "PA", "PC-ABS": "PC"}
# slicer filament_type values per material family
SLICER_TYPE = {"PLA+": "PLA", "SILK PLA": "PLA", "PLA-CF": "PLA-CF", "PETG-CF": "PETG-CF", "PA-CF": "PA-CF"}
PLATE_KEYS = ("hot_plate_temp", "textured_plate_temp", "eng_plate_temp", "cool_plate_temp",
              "textured_cool_plate_temp", "supertack_plate_temp")


def normalize_material(name: str) -> str:
    n = re.sub(r"\s+", " ", name.strip().upper().replace("_", " "))
    n = ALIASES.get(n, n)
    if n in MATERIALS:
        return n
    for key in sorted(MATERIALS, key=len, reverse=True):  # "eSUN PLA+ Basic" -> "PLA+"
        if re.search(rf"(^|[^A-Z]){re.escape(key)}($|[^A-Z])", n):
            return key
    raise ValueError(f"Unknown material '{name}'. Known: {', '.join(MATERIALS)}")


@dataclass
class LabelInfo:
    """What a spool label says. Only material is required; missing values come from typical ranges."""
    material: str
    brand: str | None = None
    product: str | None = None
    colour_name: str | None = None
    colour_hex: str | None = None
    nozzle_min: float | None = None
    nozzle_max: float | None = None
    nozzle_recommended: float | None = None
    bed_min: float | None = None
    bed_max: float | None = None
    diameter_mm: float | None = None
    weight_g: float | None = None
    price: float | None = None
    density: float | None = None
    max_speed_mm_s: float | None = None
    drying: str | None = None


def _check(name: str, value: float | None, lo: float, hi: float) -> None:
    if value is not None and not lo <= value <= hi:
        raise ValueError(f"{name} {value} looks wrong (expected {lo}–{hi}); re-check the label.")


def pick_base(lib: PresetLibrary, printer: str, material: str) -> str:
    """The closest existing filament preset for this printer and material family to inherit from."""
    stype = SLICER_TYPE.get(material, material)
    rows = lib.listing("filament", printer=printer)
    scored = []
    for row in rows:
        settings = lib.resolve("filament", row["name"])
        ftype = str(first(settings.get("filament_type"), "")).upper()
        if ftype != stype:
            continue
        name = row["name"].lower()
        score = (0 if row["source"] == "system" else 1, 0 if "generic" in name else 1 if "anycubic" in name else 2)
        scored.append((score, row["name"]))
    if not scored:  # fall back to any PLA for this printer, then anything
        if stype != "PLA":
            return pick_base(lib, printer, "PLA")
        if rows:
            return rows[0]["name"]
        raise ValueError(f"No filament presets found for {printer}.")
    # prefer the vendor's own preset for this printer (tuned), then generic
    scored.sort(key=lambda s: (s[0][1] != 1, s[0]))
    return scored[0][1]


def build_profile(lib: PresetLibrary, printer: str, info: LabelInfo, name: str | None = None) -> dict[str, Any]:
    material = normalize_material(info.material)
    m = MATERIALS[material]
    _check("Nozzle min", info.nozzle_min, 150, 350)
    _check("Nozzle max", info.nozzle_max, 150, 350)
    _check("Bed min", info.bed_min, 0, 130)
    _check("Bed max", info.bed_max, 0, 130)
    _check("Diameter", info.diameter_mm, 1.6, 3.1)
    _check("Density", info.density, 0.8, 2.5)
    _check("Spool weight", info.weight_g, 50, 10000)
    if info.colour_hex and not re.fullmatch(r"#?[0-9A-Fa-f]{6}", info.colour_hex):
        raise ValueError("colour_hex must look like #RRGGBB")
    lo = info.nozzle_min or m["nozzle"][0]
    hi = info.nozzle_max or m["nozzle"][1]
    if lo > hi:
        lo, hi = hi, lo
    nozzle = info.nozzle_recommended or round(((lo + hi) / 2) / 5) * 5
    nozzle = min(max(nozzle, lo), hi)
    bed_lo, bed_hi = info.bed_min, info.bed_max
    bed = round(((bed_lo + bed_hi) / 2) / 5) * 5 if bed_lo is not None and bed_hi is not None else (bed_lo or bed_hi or m["bed"])
    base_name = pick_base(lib, printer, material)
    base = lib.resolve("filament", base_name)
    brand = (info.brand or "Generic").strip()
    pieces = [brand, info.product or (material if material not in (info.product or "") else ""), info.colour_name]
    label = " ".join(p.strip() for p in pieces if p and p.strip())
    pname = name or f"{label} @{printer}"
    pname = re.sub(r'[<>:"/\\|?*\x00-\x1f;]', "", pname).strip()[:120]
    colour = ("#" + info.colour_hex.lstrip("#").upper()) if info.colour_hex else None

    def nozzle_list(v: float) -> list[str]:
        return [str(int(v))] * max(1, len(as_list(base.get("nozzle_temperature"))) or 1)

    p: dict[str, Any] = {
        "name": pname, "inherits": base_name, "from": "User", "instantiation": "true",
        "filament_settings_id": [pname], "filament_vendor": [brand],
        "filament_type": [SLICER_TYPE.get(material, material)],
        "nozzle_temperature": nozzle_list(nozzle),
        "nozzle_temperature_initial_layer": nozzle_list(min(nozzle + 5, hi)),
        "nozzle_temperature_range_low": nozzle_list(lo), "nozzle_temperature_range_high": nozzle_list(hi),
        "compatible_printers": [printer],
        "filament_notes": [_notes(info, material)],
    }
    for variant in ("_HS", "_BRASS"):
        if "nozzle_temperature" + variant in base:
            p["nozzle_temperature" + variant] = nozzle_list(nozzle)
            p["nozzle_temperature_initial_layer" + variant] = nozzle_list(min(nozzle + 5, hi))
    for key in PLATE_KEYS:
        if key in base:
            cool = "cool" in key
            if cool and material not in ("PLA", "PLA+", "SILK PLA", "PLA-CF", "TPU", "PVA"):
                p[key] = ["0"]  # plate not suitable for this material
                p[key + "_initial_layer"] = ["0"]
            else:
                t = min(bed, 45) if cool else bed
                p[key] = [str(int(t))]
                p[key + "_initial_layer"] = [str(int(t + 5 if t + 5 <= (bed_hi or t + 5) else t))]
    if colour:
        p["default_filament_colour"] = [colour]
    if info.diameter_mm:
        p["filament_diameter"] = [f"{info.diameter_mm:g}"]
    p["filament_density"] = [f"{info.density or m['density']:g}"]
    if info.price is not None and info.weight_g:
        p["filament_cost"] = [f"{info.price / (info.weight_g / 1000):.2f}"]
    if info.max_speed_mm_s and (info.diameter_mm or 1.75):
        # max print speed at a standard 0.42 x 0.2 mm line → volumetric limit (keep the base's if lower)
        mvs = round(info.max_speed_mm_s * 0.42 * 0.2, 1)
        base_mvs = float(first(base.get("filament_max_volumetric_speed"), mvs) or mvs)
        p["filament_max_volumetric_speed"] = [f"{min(mvs, max(base_mvs, mvs)):g}"]
    return {"profile": p, "base": base_name, "material": material}


def _notes(info: LabelInfo, material: str) -> str:
    parts = [f"Created by anycubic-slicer-mcp from a spool label ({material})."]
    if info.drying:
        parts.append(f"Drying: {info.drying}.")
    return " ".join(parts)


def save_profile(lib: PresetLibrary, profile: dict[str, Any], overwrite: bool = False) -> Path:
    """Write the filament preset where the slicer window will list it (see write_user_preset)."""
    return lib.write_user_preset("filament", profile, overwrite=overwrite)[0]
