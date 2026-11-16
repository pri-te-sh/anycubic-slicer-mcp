"""Compare slice variants, estimate cost, and cut waste in multi-colour prints."""
from __future__ import annotations

import colorsys
from copy import deepcopy
from typing import Any

from .gcode import format_duration, read_toolpaths
from .settings import SettingError
from .slicer import SliceRequest, Slicer


def plate_row(label: str, result: dict[str, Any]) -> dict[str, Any]:
    plates = result["plates"]
    secs = sum(p.get("time_normal_s") or 0 for p in plates)
    grams = sum(p.get("total_filament_g") or 0 for p in plates)
    return {
        "variant": label, "time": format_duration(secs), "time_s": round(secs), "filament_g": round(grams, 2),
        "cost": round(sum(p.get("total_cost") or 0 for p in plates), 2),
        "layers": sum(p.get("layers") or 0 for p in plates),
        "support_used": any(p.get("support_used") for p in plates),
        "tool_changes": sum(p.get("tool_changes") or 0 for p in plates),
        "gcode": [p["gcode"] for p in plates], "warnings": result.get("warnings", []),
    }


def compare(slicer: Slicer, base: SliceRequest, variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Slice the same model several ways. Each variant: label + any of overrides, process, filaments,
    rotation, scale (merged over the base request)."""
    if not 1 <= len(variants) <= 8:
        raise ValueError("Give 1 to 8 variants.")
    rows = []
    for i, v in enumerate(variants):
        req = deepcopy(base)
        label = str(v.get("label") or f"variant {i + 1}")
        req.overrides = {**(base.overrides or {}), **(v.get("overrides") or {})}
        for key in ("process", "filaments", "rotation", "scale", "printer"):
            if v.get(key) is not None:
                setattr(req, key, v[key])
        req.output_name = f"{base.output_name or 'compare'}_{_slug(label)}"
        rows.append(plate_row(label, slicer.slice(req)))
    ref = rows[0]
    for r in rows[1:]:
        r["vs_first"] = {
            "time": _pct(r["time_s"], ref["time_s"]), "filament": _pct(r["filament_g"], ref["filament_g"]),
        }
    best_time = min(rows, key=lambda r: r["time_s"])["variant"]
    best_mat = min(rows, key=lambda r: r["filament_g"])["variant"]
    return {"variants": rows, "fastest": best_time, "least_filament": best_mat}


def estimate_cost(grams: float, seconds: float, price_per_kg: float, power_w: float = 150,
                  kwh_price: float = 0.15, extra: float = 0.0, currency: str = "USD") -> dict[str, Any]:
    if grams < 0 or seconds < 0 or price_per_kg < 0 or power_w < 0 or kwh_price < 0:
        raise ValueError("Values must not be negative.")
    material = grams / 1000 * price_per_kg
    energy_kwh = power_w / 1000 * seconds / 3600
    energy = energy_kwh * kwh_price
    total = material + energy + extra
    return {
        "currency": currency, "material": round(material, 2), "energy": round(energy, 2),
        "energy_kwh": round(energy_kwh, 3), "extra": round(extra, 2), "total": round(total, 2),
        "assumptions": {"filament_price_per_kg": price_per_kg, "printer_power_w": power_w, "electricity_per_kwh": kwh_price},
    }


STRATEGIES = [
    ("Purge into the prime tower, then into infill", {"purge_in_prime_tower": True, "flush_into_infill": True}),
    ("Flush into infill and support", {"flush_into_infill": True, "flush_into_support": True}),
]


def multicolor(slicer: Slicer, req: SliceRequest) -> dict[str, Any]:
    """Measure colour-change waste and test waste-cutting strategies. A strategy is only recommended
    if it keeps every colour change (so the print still has its colours) and uses less filament."""
    if not req.filaments or len(req.filaments) < 2 or not req.object_filaments:
        raise ValueError("Give at least two filaments and object_filaments (which filament each model uses).")
    name = req.output_name or "multicolour"
    base = deepcopy(req); base.output_name = f"{name}_original"
    before = plate_row("as sliced", slicer.slice(base))
    single = deepcopy(req); single.object_filaments = [1] * len(req.object_filaments); single.output_name = f"{name}_model_only"
    model_g = plate_row("model only", slicer.slice(single))["filament_g"]
    waste = max(before["filament_g"] - model_g, 0.0)
    tested, best = [], None
    for label, overrides in STRATEGIES:
        r = deepcopy(req); r.overrides = {**(req.overrides or {}), **overrides}
        r.output_name = f"{name}_{_slug(label)}"
        try:
            row = plate_row(label, slicer.slice(r))
        except SettingError:
            continue  # this slicer version doesn't have one of the strategy's settings
        valid = row["tool_changes"] == before["tool_changes"] and before["tool_changes"] > 0
        row["valid"] = valid
        row["overrides"] = overrides
        row["vs_original"] = {"filament": _pct(row["filament_g"], before["filament_g"]), "time": _pct(row["time_s"], before["time_s"])}
        if not valid:
            row["note"] = "Changed the number of colour changes, so the colours wouldn't print as intended."
        tested.append(row)
        if valid and row["filament_g"] < before["filament_g"] - 0.5 and (best is None or row["filament_g"] < best["filament_g"]):
            best = row
    tips = []
    changes = before["tool_changes"]
    if changes:
        tips.append(f"Each colour change costs about {waste / changes:.2f} g here. Fewer layers that mix colours "
                    "is the biggest lever: e.g. keep accent colours to the top few layers, or make inlays shallow.")
        tips.append("Printing several copies on one plate shares every colour change between them.")
        tips.append("If parts can be glued or snapped together, print each colour as its own plate: zero purge waste.")
    if req.filament_colours and len(req.filament_colours) >= 2:
        tips.append("Purging is heaviest going from dark to light; " + _order_tip(req.filament_colours))
    return {
        "as_sliced": before,
        "model_only_g": model_g,
        "colour_change_waste_g": round(waste, 2),
        "waste_share": f"{waste / before['filament_g'] * 100:.0f}%" if before["filament_g"] else None,
        "strategies_tested": tested,
        "recommended": ({"strategy": best["variant"], "overrides": best["overrides"],
                         "saves_g": round(before["filament_g"] - best["filament_g"], 2)} if best else None),
        "verdict": ("Use the recommended strategy." if best else
                    "Your printer's default purge handling is already the least wasteful option tested; "
                    "cut waste with the layout tips below instead."),
        "tips": tips,
    }


def _order_tip(colours: list[str]) -> str:
    def lum(c: str) -> float:
        c = c.lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
        return colorsys.rgb_to_hls(r, g, b)[1]
    ranked = sorted(range(len(colours)), key=lambda i: -lum(colours[i]))
    return ("lightest-to-darkest slot order is " + " → ".join(f"filament {i + 1} ({colours[i]})" for i in ranked)
            + "; models that share a layer flush less when lighter colours go first.")


def _pct(new: float, old: float) -> str | None:
    if not old:
        return None
    return f"{(new - old) / old * 100:+.0f}%"


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")[:30] or "v"
