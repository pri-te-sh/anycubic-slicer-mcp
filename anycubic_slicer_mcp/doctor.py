"""Print doctor: map visible print defects (from photos the agent looks at) to concrete fixes,
computed from the presets actually in use."""
from __future__ import annotations

from typing import Any, Callable

from .presets import first, to_float

# Each issue: what it looks like (so an agent can recognise it in a photo), likely causes, setting
# changes (functions of the current settings), and checks that aren't settings.
Rule = Callable[[dict[str, Any]], tuple[Any, str] | None]

ISSUES: dict[str, dict[str, Any]] = {
    "stringing": {
        "name": "Stringing / oozing",
        "looks_like": "Thin hair-like threads or webs between separate parts of the print, often between towers or across gaps.",
        "causes": ["Too little retraction", "Nozzle too hot", "Slow travel moves", "Wet filament"],
        "checks": ["Dry the filament (wet PETG/TPU/nylon strings badly)", "Clean the nozzle tip"],
    },
    "warping": {
        "name": "Warping / corners peeling off the bed",
        "looks_like": "Corners or edges of the first layers curl upward and lift off the bed; the base isn't flat.",
        "causes": ["Too little bed adhesion area", "Bed too cool", "Cooling fan too strong early", "Drafts / no enclosure (ABS, ASA, PC, PA)"],
        "checks": ["Wash the plate with dish soap and water", "Use an enclosure or block drafts for ABS/ASA/PC/nylon",
                   "Glue stick on smooth plates helps PETG release and adhesion"],
    },
    "layer_adhesion": {
        "name": "Weak layer adhesion / layers splitting",
        "looks_like": "Cracks or gaps between layers, parts snap cleanly along a layer line, visible separation on tall walls.",
        "causes": ["Nozzle too cool", "Too much cooling", "Printing too fast for the hotend", "Layer too thick for the nozzle"],
        "checks": ["Dry the filament", "Check for drafts on the print"],
    },
    "first_layer": {
        "name": "First layer not sticking",
        "looks_like": "Lines on the first layer drag around, ball up, or don't stick; the print detaches early.",
        "causes": ["Nozzle too high (Z offset)", "Dirty or cold bed", "First layer too fast"],
        "checks": ["Re-run bed levelling / adjust Z offset so the first layer is slightly squished",
                   "Wash the plate; avoid touching it with fingers"],
    },
    "under_extrusion": {
        "name": "Under-extrusion",
        "looks_like": "Thin, gappy or missing lines; walls you can see through; weak, sparse top surfaces.",
        "causes": ["Flow too low", "Printing faster than the hotend can melt", "Partial clog", "Nozzle too cool"],
        "checks": ["Check for a partial clog (cold pull)", "Check the filament isn't tangled on the spool"],
    },
    "over_extrusion": {
        "name": "Over-extrusion / blobs",
        "looks_like": "Bulging, rough walls; blobs and zits; top surfaces with ridges; dimensions too large.",
        "causes": ["Flow too high", "Nozzle too hot"],
        "checks": ["Measure the filament diameter (should be ~1.75 mm)"],
    },
    "elephant_foot": {
        "name": "Elephant foot",
        "looks_like": "The bottom few layers bulge outward past the walls above them.",
        "causes": ["First layer squished too much", "Bed too hot"],
        "checks": ["Raise the Z offset slightly if the first layer looks very flat and shiny"],
    },
    "ringing": {
        "name": "Ringing / ghosting",
        "looks_like": "Faint repeating ripples on walls just after corners or edges, like echoes of the shape.",
        "causes": ["Outer walls printed too fast", "High acceleration", "Loose belts or wobbly printer"],
        "checks": ["Run the printer's resonance / input-shaping calibration", "Check belt tension and that the printer stands firmly"],
    },
    "overhang": {
        "name": "Drooping or curling overhangs",
        "looks_like": "Undersides of angled or horizontal features sag, curl up, or look rough and stringy.",
        "causes": ["No support where needed", "Overhangs printed too fast", "Not enough cooling"],
        "checks": ["Re-orient the part so steep overhangs face up (find_best_orientation)"],
    },
    "bridging": {
        "name": "Saggy bridges",
        "looks_like": "Strands spanning a gap droop or break; holes' tops look messy.",
        "causes": ["Bridges printed too fast or with too much flow", "Weak cooling"],
        "checks": [],
    },
    "top_surface": {
        "name": "Gaps or pillowing on top surfaces",
        "looks_like": "Holes, bumps or a quilted look on flat top surfaces; infill pattern showing through.",
        "causes": ["Too few top layers", "Low infill under the top", "Under-extrusion"],
        "checks": [],
    },
    "layer_shift": {
        "name": "Layer shift",
        "looks_like": "Part of the print is suddenly offset sideways, like a staircase step through the whole part.",
        "causes": ["Travel too fast / collisions with curled parts", "Loose belt or pulley", "Nozzle hitting the print"],
        "checks": ["Tighten belts and pulley set screws", "Look for curled edges the nozzle could catch"],
    },
    "seam": {
        "name": "Visible seam / zits",
        "looks_like": "A vertical line or row of blobs where each layer starts and ends.",
        "causes": ["Seam placed on a visible face", "Ooze at layer start"],
        "checks": [],
    },
}


def _val(cfg: dict[str, Any], key: str, default: float | None = None) -> float | None:
    v = to_float(cfg.get(key), None)
    if v is None and key.startswith("filament_retraction_length"):
        return None
    return default if v is None else v


def _nozzle_range(fil: dict[str, Any]) -> tuple[float, float]:
    return (_val(fil, "nozzle_temperature_range_low", 190) or 190, _val(fil, "nozzle_temperature_range_high", 260) or 260)


def suggestions(issue: str, cfg: dict[str, dict[str, Any]], severity: str) -> list[dict[str, Any]]:
    """Concrete setting changes for one issue given current flat configs {machine, process, filament}."""
    fil, proc, mach = cfg["filament"], cfg["process"], cfg["machine"]
    strong = severity == "severe"
    lo, hi = _nozzle_range(fil)
    temp = _val(fil, "nozzle_temperature", 210)
    ftype = str(first(fil.get("filament_type"), "PLA")).upper()
    out: list[dict[str, Any]] = []

    def add(key: str, new: Any, why: str, current: Any = None, direction: str | None = None):
        cur = current if current is not None else first(fil.get(key, proc.get(key, mach.get(key))))
        try:
            c, n = float(str(cur).rstrip("%")), float(str(new).rstrip("%"))
            if (direction == "up" and n <= c) or (direction == "down" and n >= c) or (direction is None and n == c):
                return  # already there: nothing to change
        except (TypeError, ValueError):
            if str(cur) == str(new):
                return
        out.append({"setting": key, "current": cur, "suggested": new, "why": why, "direction": direction})

    retract = _val(fil, "filament_retraction_length") or _val(mach, "retraction_length", 0.8)
    travel = _val(proc, "travel_speed", 200)
    outer = _val(proc, "outer_wall_speed", 150)
    fan_max = _val(fil, "fan_max_speed", 100)
    bed = _val(fil, "textured_plate_temp", None) or _val(fil, "hot_plate_temp", 60)

    if issue == "stringing":
        add("filament_retraction_length", round(min(retract + (0.8 if strong else 0.4), 3.0), 2),
            "More retraction pulls molten plastic back before travel moves.", retract, "up")
        add("nozzle_temperature", int(max(lo, temp - (10 if strong else 5))), "A cooler nozzle oozes less.", temp, "down")
        add("travel_speed", int(min(travel * 1.2, 500)), "Faster travel leaves less time to ooze.", travel, "up")
    elif issue == "warping":
        add("brim_type", "outer_only", "A brim adds grip around the edges where warping starts.")
        brim = _val(proc, "brim_width", 0) or 0
        add("brim_width", int(max(brim + 3, 8 if strong else 5)), "Wider brim = more holding area.", brim, "up")
        bed_max = 115 if ftype in ("ABS", "ASA", "PC", "PA") else 80 if ftype.startswith("PETG") else 65
        add("textured_plate_temp", int(min(bed + 5, bed_max)), "A warmer bed keeps the base from shrinking.", bed, "up")
        nofan = _val(fil, "close_fan_the_first_x_layers", 1) or 1
        add("close_fan_the_first_x_layers", int(max(nofan + 1, 3 if strong else 2)),
            "No fan on the first layers keeps them warm.", nofan, "up")
    elif issue == "layer_adhesion":
        add("nozzle_temperature", int(min(hi, temp + (15 if strong else 10))), "Hotter plastic fuses better with the layer below.", temp, "up")
        new_fan = max(30 if ftype.startswith("PLA") else 0, fan_max - (40 if strong else 25))
        add("fan_max_speed", int(new_fan), "Less cooling lets layers bond while still warm.", fan_max, "down")
        add("outer_wall_speed", int(outer * 0.8), "Slower walls get more heat into each layer.", outer, "down")
    elif issue == "first_layer":
        add("textured_plate_temp", int(bed + 5), "A warmer bed helps the first layer grip.", bed)
        add("initial_layer_speed", int(max(15, (_val(proc, "initial_layer_speed", 50) or 50) * 0.7)),
            "A slower first layer gives plastic time to stick.")
        add("initial_layer_print_height", 0.25, "A slightly thicker first layer forgives small levelling errors.")
    elif issue == "under_extrusion":
        flow = _val(fil, "filament_flow_ratio", 0.98)
        add("filament_flow_ratio", round(min(flow + (0.05 if strong else 0.03), 1.1), 3), "Push a little more plastic.", flow)
        mvs = _val(fil, "filament_max_volumetric_speed", 15)
        add("filament_max_volumetric_speed", round(mvs * 0.8, 1), "Cap flow to what the hotend can melt.", mvs)
        add("nozzle_temperature", int(min(hi, temp + 5)), "Hotter plastic flows more easily.", temp)
    elif issue == "over_extrusion":
        flow = _val(fil, "filament_flow_ratio", 0.98)
        add("filament_flow_ratio", round(max(flow - (0.05 if strong else 0.03), 0.85), 3), "Push a little less plastic.", flow)
        add("nozzle_temperature", int(max(lo, temp - 5)), "Slightly cooler = less ooze.", temp)
    elif issue == "elephant_foot":
        add("elefant_foot_compensation", 0.2 if strong else 0.15, "Shrinks the first layer to cancel the bulge.")
        add("textured_plate_temp", int(max(bed - 5, 40)), "A slightly cooler bed keeps the base firmer.", bed)
    elif issue == "ringing":
        add("outer_wall_speed", int(outer * (0.6 if strong else 0.75)), "Slower outer walls ring less.", outer)
        acc = _val(proc, "outer_wall_acceleration", None)
        if acc:
            add("outer_wall_acceleration", int(acc * 0.6), "Gentler acceleration excites fewer vibrations.", acc)
    elif issue == "overhang":
        add("enable_support", True, "Support the steepest overhangs.")
        add("support_type", "tree(auto)", "Tree supports are easy to remove and touch less of the part.")
        add("enable_overhang_speed", True, "Slow down on overhangs so they cool in place.")
        add("overhang_fan_speed", 100, "Full cooling on overhangs.")
    elif issue == "bridging":
        bs = _val(proc, "bridge_speed", 50)
        add("bridge_speed", int(max(15, bs * 0.7)), "Slower bridges sag less.", bs)
        add("bridge_flow", 0.85, "Thinner bridge strands cool and tighten faster.")
    elif issue == "top_surface":
        top = _val(proc, "top_shell_layers", 4)
        add("top_shell_layers", int(top + (3 if strong else 2)), "More top layers cover the infill completely.", top)
        dens = _val(proc, "sparse_infill_density", 15)
        if dens < 15:
            add("sparse_infill_density", "15%", "Denser infill supports the top skin.", f"{dens:g}%")
    elif issue == "layer_shift":
        add("travel_speed", int(travel * 0.75), "Gentler travel moves are less likely to skip steps.", travel)
        add("z_hop", 0.4, "Lift the nozzle over the print so it can't catch curled edges.")
    elif issue == "seam":
        add("seam_position", "back", "Hide the seam on the back of the part.")
        add("wipe", True, "Wipe the nozzle at the end of each loop to avoid zits.")
    return out


def diagnose(issues: list[str], cfg: dict[str, dict[str, Any]], severity: str = "moderate") -> dict[str, Any]:
    unknown = [i for i in issues if i not in ISSUES]
    if unknown:
        raise ValueError(f"Unknown issue(s): {', '.join(unknown)}. Known: {', '.join(ISSUES)}")
    if severity not in ("mild", "moderate", "severe"):
        raise ValueError("severity must be mild, moderate or severe")
    report, proposals = [], {}
    for issue in issues:
        sug = suggestions(issue, cfg, severity)
        for s in sug:
            proposals.setdefault(s["setting"], []).append((issue, s))
        meta = ISSUES[issue]
        report.append({"issue": issue, "name": meta["name"], "likely_causes": meta["causes"],
                       "setting_changes": [{k: v for k, v in s.items() if k != "direction"} for s in sug],
                       "also_check": meta["checks"]})
    overrides, conflicts = {}, []
    for key, items in proposals.items():
        dirs = {s["direction"] for _, s in items if s["direction"]}
        if len(dirs) > 1:
            names = " and ".join(ISSUES[i]["name"].lower() for i, _ in items)
            conflicts.append(f"{names} pull '{key}' in opposite directions, so it's left unchanged. "
                             "Fix the others first, then adjust it in small steps.")
            continue
        overrides[key] = items[0][1]["suggested"]
    return {
        "diagnosis": report,
        "overrides": overrides,
        "conflicts": conflicts,
        "next_step": ("Re-slice with these overrides (slice_model overrides=...) and print a small test first. "
                      "Change one thing at a time if you want to know which fix worked."),
    }


def catalog() -> list[dict[str, Any]]:
    return [{"issue": k, "name": v["name"], "looks_like": v["looks_like"]} for k, v in ISSUES.items()]
