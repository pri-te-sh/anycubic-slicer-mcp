"""MCP server: Anycubic Slicer Next for AI agents."""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP, Image

from . import __version__
from . import analysis, calibration, doctor, gcode as gc, materials, optimize, project, render
from .install import find_install, state_dir
from .inventory import Inventory
from .mesh import describe, load_mesh, rotation
from .presets import PresetLibrary, first, flatten_for_cli, to_float
from .settings import SettingRouter, format_value
from .slicer import MODEL_SUFFIXES, SliceRequest, Slicer, checked_file

INSTRUCTIONS = """\
Anycubic Slicer Next, headless. Typical flows:
- Slice: slicer_status → list_presets (printer/process/filament) → slice_model → preview_gcode to check.
  Omitted printer/process/filaments fall back to what the slicer GUI has selected.
- Find setting names with get_preset(search=...) before passing overrides
  (e.g. sparse_infill_density="20%", wall_loops=3, enable_support=true, support_type="tree(auto)").
- Plan: find_best_orientation, analyze_strength, check_bed_fit, compare_slices, estimate_cost.
- Filament from a spool label photo: read the label yourself, then create_filament_profile with what it says.
- Failed print photos: look at them, match to diagnose_print's issue list, call diagnose_print, re-slice with
  its overrides. Calibration: make_calibration_print. Multi-colour waste: optimize_multicolor.
- Whole multi-part project (folder or .zip): plan_project → ask the user its `questions` (every blocking one must be
  answered; ask a few at a time, most important first, and say what you're assuming for the rest) → plan_project
  again with plan_id and the answers → show the plates and get the user's OK → slice_project. Never answer a
  question for the user or slice while one is open. If you have your own doubts (which spool, which parts matter
  most), ask those too.
Never claim a print is safe without the user checking the preview. This server never sends anything to a printer.
"""

mcp = FastMCP("anycubic-slicer", instructions=INSTRUCTIONS)
INSTALL = find_install()
LIB = PresetLibrary(INSTALL)
SLICER = Slicer(INSTALL, LIB)
INVENTORY = Inventory()


def threaded(fn):
    """Run blocking work in a worker thread so one long slice doesn't stall other requests."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    return wrapper


def _bounded(name: str, value: float | int | None, lo: float, hi: float) -> None:
    if value is not None and not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")


def _default_out() -> Path:
    p = Path.home() / "Documents" / "Anycubic Slicer MCP"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _printer(printer: str | None) -> str:
    LIB.reload()
    name = LIB.get("machine", printer).name if printer else LIB.selection()["machine"]
    if not name:
        raise ValueError("No printer given and none selected in the slicer. Pass printer=...")
    return name


def _bed(printer: str) -> tuple[float, float, float]:
    return analysis.bed_size(LIB.resolve("machine", printer))


def _model_mesh(path: str):
    p = checked_file(path, MODEL_SUFFIXES)
    if p.suffix.lower() in {".step", ".stp"}:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            return load_mesh(SLICER.to_stl(p, Path(tmp)))
    return load_mesh(p)


# ======================================================================== status & presets
@mcp.tool()
@threaded
def slicer_status() -> dict:
    """Is Anycubic Slicer Next installed, which version, and what printer/process/filament does its GUI have
    selected? Call this first."""
    LIB.reload()
    conf_app = LIB.conf.get("app") if isinstance(LIB.conf.get("app"), dict) else {}
    sel = LIB.selection()
    bed = None
    if sel["machine"] and sel["machine"] in LIB.all("machine"):
        bx, by, bz = _bed(sel["machine"])
        bed = f"{bx:g} x {by:g} x {bz:g} mm"
    return {
        "server_version": __version__, "os": INSTALL.os_name,
        "slicer_found": INSTALL.executable.is_file(), "executable": str(INSTALL.executable),
        "slicer_version": conf_app.get("version"), "data_dir": str(INSTALL.data_dir),
        "preset_counts": {k: len([p for p in LIB.all(k).values() if p.selectable]) for k in ("machine", "process", "filament")},
        "gui_selection": {**sel, "build_volume": bed},
        "state_dir": str(state_dir()),
    }


@mcp.tool()
@threaded
def list_presets(kind: str, printer: str | None = None, search: str | None = None,
                 include_incompatible: bool = False) -> dict:
    """List presets. kind: machine (printers) | process | filament. With printer, only process/filament
    presets made for it are listed (your own presets first). search filters names (e.g. 'PETG', '0.16mm')."""
    LIB.reload()
    p = _printer(printer) if printer else None
    rows = LIB.listing(kind, p, search, include_incompatible)
    return {"count": len(rows), "presets": rows[:300], "truncated": len(rows) > 300}


@mcp.tool()
@threaded
def get_preset(name: str, kind: str, search: str | None = None, keys: list[str] | None = None,
               printer: str | None = None) -> dict:
    """Resolved settings of a preset (inheritance merged). Use search to find setting names by substring
    (e.g. 'infill', 'support', 'temp', 'retract'); keys returns exact ones. Long G-code templates are elided."""
    LIB.reload()
    preset = LIB.get(kind, name, _printer(printer) if printer else None)
    cfg = LIB.resolve(preset.kind, preset.name)
    if keys:
        return {"preset": preset.name, "settings": {k: cfg.get(k) for k in keys}}
    items = {k: v for k, v in cfg.items() if not search or search.lower() in k.lower()}
    trimmed = {k: (v if len(json.dumps(v)) <= 200 else f"<{len(json.dumps(v))} chars; ask for it via keys>")
               for k, v in sorted(items.items())}
    return {"preset": preset.name, "source": preset.source, "inherits": preset.parent, "settings": trimmed}


@mcp.tool()
@threaded
def save_user_preset(kind: str, name: str, inherits: str, settings: dict, printer: str | None = None,
                     overwrite: bool = False) -> dict:
    """Save your own process/filament/printer preset that appears in the slicer GUI (restart the GUI to see it).
    inherits: the preset to start from; settings: only the values that differ. Ask the user before saving."""
    LIB.reload()
    parent = LIB.get(kind, inherits)
    if not re.fullmatch(r"[^<>:\"/\\|?*;\x00-\x1f]{1,120}", name):
        raise ValueError("Preset name must be 1–120 characters without <>:\"/\\|?*;")
    base = LIB.resolve(parent.kind, parent.name)
    router = SettingRouter(LIB)
    protected = {"name", "inherits", "from", "instantiation", "compatible_printers", "compatible_printers_condition",
                 "setting_id", "filament_id", "type"}
    data: dict[str, Any] = {"name": name, "inherits": parent.name, "from": "User", "instantiation": "true"}
    for key, value in settings.items():
        if key in protected:
            raise ValueError(f"'{key}' can't be set here.")
        router.check_key(key)
        data[key] = format_value(value, base.get(key, [] if parent.kind == "filament" else ""))
    if printer and parent.kind != "machine":
        data["compatible_printers"] = [_printer(printer)]
    if parent.kind == "filament":
        data["filament_settings_id"] = [name]
    elif parent.kind == "process":
        data["print_settings_id"] = name
    folder = LIB.user_preset_dir(parent.kind)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    if path.exists() and not overwrite:
        raise FileExistsError(f"'{name}' already exists; pass overwrite=True to replace it.")
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    LIB.reload()
    return {"saved": str(path), "kind": parent.kind, "inherits": parent.name,
            "note": "Restart Anycubic Slicer Next to see it in the GUI."}


# ======================================================================== models & slicing
@mcp.tool()
@threaded
def model_info(path: str, printer: str | None = None) -> dict:
    """Size, volume, surface area, triangle count, pieces and watertight check of an STL/OBJ/3MF/STEP model,
    plus whether it fits the printer's build volume."""
    mesh = _model_mesh(path)
    info = describe(mesh)
    try:
        info["bed_fit"] = analysis.fit_report(mesh, _bed(_printer(printer)))
    except ValueError:
        pass
    if not info["watertight"]:
        info["note"] = "Not watertight: the slicer may repair it, but check the result (holes, missing walls)."
    return info


@mcp.tool()
@threaded
def slice_model(models: list[str], printer: str | None = None, process: str | None = None,
                filaments: list[str] | None = None, overrides: dict | None = None,
                output_dir: str | None = None, output_name: str | None = None, plate: int = 0,
                arrange: bool = True, orient: bool = False, rotation_deg: dict | None = None,
                scale: float | None = None, copies: int = 1, object_filaments: list[int] | None = None,
                filament_colours: list[str] | None = None, bed_type: str | None = None,
                timeout_s: int = 900) -> dict:
    """Slice STL/3MF/OBJ/STEP files to G-code plus a printer-ready .gcode.3mf.
    printer/process/filaments: preset names or unique parts of them (omitted → the GUI's selection).
    overrides: setting → value, routed to the right preset (find names with get_preset search).
    rotation_deg: {"x":..,"y":..,"z":..} degrees (use find_best_orientation's suggestion); scale: factor.
    copies: instances of each model. Multi-colour: filaments=[...], object_filaments=[1,2,...] (filament per model),
    filament_colours=["#RRGGBB", ...] matching the loaded spools. bed_type e.g. "Textured PEI Plate".
    Returns time, grams, cost, layers, per-filament use and warnings. Output goes next to the first model."""
    _bounded("plate", plate, 0, 99); _bounded("copies", copies, 1, 200); _bounded("timeout_s", timeout_s, 30, 7200)
    return SLICER.slice(SliceRequest(
        models=models, printer=printer, process=process, filaments=filaments, overrides=overrides,
        output_dir=output_dir, output_name=output_name, plate=plate, arrange=arrange, orient=orient,
        rotation=rotation_deg, scale=scale, copies=copies, object_filaments=object_filaments,
        filament_colours=filament_colours, bed_type=bed_type, timeout_s=timeout_s))


@mcp.tool()
@threaded
def gcode_summary(path: str) -> dict:
    """Print time, filament (g, m, cost), layers, first-layer time, colour changes, key settings and the
    length of each extrusion type, read from an existing .gcode file."""
    p = checked_file(path, gc.GCODE_SUFFIXES)
    s = gc.summarize(p)
    s["extrusion_m_by_type"] = gc.read_toolpaths(p).feature_lengths()
    return s


@mcp.tool()
@threaded
def preview_gcode(path: str, layer: int | None = None, view: str = "top", size: int = 900) -> list:
    """Render G-code as an image. view="top": layer=None shows every layer shaded by height (placement check);
    layer=N shows that layer coloured by feature (walls orange/yellow, infill red, top pink, bottom blue,
    bridges cyan, support teal). view="side": front elevation of the whole print (overhangs, supports)."""
    _bounded("size", size, 200, 2000)
    p = checked_file(path, gc.GCODE_SUFFIXES)
    tp = gc.read_toolpaths(p)
    if view == "side":
        png, meta = render.side_view(tp, size, p.stem)
    elif view == "top":
        png, meta = render.top_view(tp, layer, size, p.stem)
    else:
        raise ValueError("view must be 'top' or 'side'")
    return [Image(data=png, format="png"), json.dumps(meta)]


@mcp.tool()
@threaded
def animate_layers(path: str, size: int = 520, max_frames: int = 60) -> list:
    """Animated GIF of the print building up layer by layer. Saved next to the G-code; returns its path and the GIF."""
    _bounded("size", size, 200, 1000); _bounded("max_frames", max_frames, 5, 200)
    p = checked_file(path, gc.GCODE_SUFFIXES)
    out = p.with_name(p.stem.replace(".gcode", "") + "_layers.gif")
    render.layer_animation(gc.read_toolpaths(p), out, size, max_frames)
    return [Image(data=out.read_bytes(), format="gif"), json.dumps({"gif": str(out)})]


@mcp.tool()
@threaded
def print_card(path: str, title: str | None = None, price_per_kg: float | None = None,
               currency_symbol: str = "$") -> list:
    """A shareable 1200x630 image summarising a sliced print: preview, time, filament, cost, layers, settings.
    Saved next to the G-code."""
    p = checked_file(path, gc.GCODE_SUFFIXES)
    s = gc.summarize(p)
    tp = gc.read_toolpaths(p)
    png, _ = render.top_view(tp, None, 800, "")
    st = s.get("settings", {})
    grams = s.get("total_filament_g")
    cost = s.get("total_cost")
    if price_per_kg is not None and grams is not None:
        cost = grams / 1000 * price_per_kg
    rows = [("Print time", s.get("time_normal") or "?"),
            ("Filament", f"{grams:g} g {st.get('filament_type', '')}".strip() if grams else "?"),
            ("Cost", f"{currency_symbol}{cost:.2f}" if cost is not None else "?"),
            ("Layers", f"{s.get('layers', '?')} × {st.get('layer_height', '?')} mm"),
            ("Infill", f"{st.get('sparse_infill_density', '?')} {st.get('sparse_infill_pattern', '')}".strip()),
            ("Walls", str(st.get("wall_loops", "?"))),
            ("Printer", (st.get("printer_settings_id") or "?").replace("Anycubic ", ""))]
    out = p.with_name(p.stem + "_card.png")
    render.print_card(png, title or p.stem, rows, out)
    return [Image(data=out.read_bytes(), format="png"), json.dumps({"card": str(out)})]


@mcp.tool()
@threaded
def open_in_slicer(path: str) -> str:
    """Open a model, project or G-code in the Anycubic Slicer Next window. Only call this when the user asks."""
    p = checked_file(path, MODEL_SUFFIXES | gc.GCODE_SUFFIXES | {".gcode.3mf"})
    if p.suffix.lower() == ".3mf":
        from .slicer import scan_3mf
        scan_3mf(p)
    SLICER.open_in_gui(p)
    return f"Opened {p.name} in Anycubic Slicer Next."


# ======================================================================== planning
@mcp.tool()
@threaded
def compare_slices(models: list[str], variants: list[dict], printer: str | None = None,
                   process: str | None = None, filaments: list[str] | None = None,
                   output_dir: str | None = None, output_name: str | None = None) -> dict:
    """Slice the same model several ways and compare time, filament and cost side by side.
    variants: up to 8 of {"label": "...", "overrides": {...}, "process": "...", "filaments": [...],
    "rotation": {...}, "scale": 1.0}. The first variant is the reference."""
    base = SliceRequest(models=models, printer=printer, process=process, filaments=filaments,
                        output_dir=output_dir, output_name=output_name or Path(models[0]).stem)
    return optimize.compare(SLICER, base, variants)


@mcp.tool()
@threaded
def find_best_orientation(model: str, goal: str = "balanced", printer: str | None = None,
                          load_axis: str | list[float] | None = None, support_angle: float = 45.0,
                          top: int = 5) -> dict:
    """Rank ways to lay the model on the bed. goal: balanced | least_support | fastest | strongest |
    best_bed_adhesion. load_axis ('x','y','z' or a vector, in model coordinates) is the direction the part will
    be pulled or pushed in use; strong orientations keep it along the layers. Pass the winner's `rotation`
    to slice_model as rotation_deg."""
    _bounded("support_angle", support_angle, 10, 80); _bounded("top", top, 1, 12)
    mesh = _model_mesh(model)
    bed = _bed(_printer(printer))
    rows = analysis.rank_orientations(mesh, goal, bed, analysis.parse_axis(load_axis), support_angle, top)
    return {"goal": goal, "build_volume_mm": list(bed), "ranked": rows,
            "use": "slice_model(..., rotation_deg=ranked[0].rotation)"}


@mcp.tool()
@threaded
def analyze_strength(model: str, load: str = "general", load_axis: str | list[float] | None = None,
                     priority: str = "balanced", printer: str | None = None, filament: str | None = None) -> dict:
    """Analyse the part's shape and recommend orientation + settings for strength.
    load: general | tension | compression | bending | impact | torsion. load_axis: direction of the load in model
    coordinates ('x','y','z'). priority: max_strength | balanced | light. Returns overrides and rotation to slice with."""
    mesh = _model_mesh(model)
    p = _printer(printer)
    mach = LIB.resolve("machine", p)
    nozzle = to_float(mach.get("nozzle_diameter"), 0.4) or 0.4
    material = None
    if filament:
        material = first(LIB.resolve("filament", LIB.get("filament", filament, p).name).get("filament_type"))
    else:
        sel = LIB.selection()["filaments"]
        if sel and sel[0] in LIB.all("filament"):
            material = first(LIB.resolve("filament", sel[0]).get("filament_type"))
    plan = analysis.strength_plan(mesh, load, analysis.parse_axis(load_axis), priority, material, nozzle, _bed(p))
    plan["next_step"] = ("slice_model(models=[model], overrides=overrides, rotation_deg=recommended_orientation.rotation); "
                         "compare against the current settings with compare_slices to see the time/filament cost.")
    return plan


@mcp.tool()
@threaded
def check_bed_fit(model: str, printer: str | None = None, rotation_deg: dict | None = None,
                  scale: float | None = None) -> dict:
    """Does the model fit the printer's build volume (optionally after rotating/scaling)? If not, suggests a
    45° turn, a scale factor, lying it down, or splitting."""
    mesh = _model_mesh(model)
    r = rotation_deg or {}
    m = rotation(r.get("x", 0), r.get("y", 0), r.get("z", 0)) * (scale or 1.0)
    return analysis.fit_report(mesh.transformed(m).on_bed(), _bed(_printer(printer)))


@mcp.tool()
@threaded
def estimate_cost(gcode: str | None = None, grams: float | None = None, print_time_s: float | None = None,
                  price_per_kg: float | None = None, printer_power_w: float = 150, electricity_per_kwh: float = 0.15,
                  extra: float = 0.0, currency: str = "USD") -> dict:
    """Cost of a print: filament + electricity (+ extra, e.g. labour or markup). Give a .gcode file, or grams
    and print_time_s. price_per_kg defaults to the filament preset's cost; printer_power_w is the average draw."""
    if gcode:
        s = gc.summarize(checked_file(gcode, gc.GCODE_SUFFIXES))
        grams = s.get("total_filament_g")
        print_time_s = s.get("time_normal_s")
        if price_per_kg is None and grams:
            cost = s.get("total_cost")
            price_per_kg = cost / grams * 1000 if cost else None
    if grams is None or print_time_s is None:
        raise ValueError("Give a gcode file, or both grams and print_time_s.")
    if price_per_kg is None:
        raise ValueError("price_per_kg is needed (the G-code has no filament cost).")
    return optimize.estimate_cost(grams, print_time_s, price_per_kg, printer_power_w, electricity_per_kwh, extra, currency)


# ======================================================================== filament & spools
@mcp.tool()
@threaded
def create_filament_profile(material: str, printer: str | None = None, brand: str | None = None,
                            product: str | None = None, colour_name: str | None = None, colour_hex: str | None = None,
                            nozzle_min: float | None = None, nozzle_max: float | None = None,
                            nozzle_recommended: float | None = None, bed_min: float | None = None,
                            bed_max: float | None = None, diameter_mm: float | None = None,
                            weight_g: float | None = None, price: float | None = None, density: float | None = None,
                            max_speed_mm_s: float | None = None, drying: str | None = None, name: str | None = None,
                            save: bool = False, overwrite: bool = False, add_to_inventory: bool = False) -> dict:
    """Turn a spool label into a filament preset. Read the label photo yourself and pass what it says
    (material is required; anything missing uses typical values for that material). The profile inherits from
    the closest existing preset for the printer. With save=False (default) it's a preview: show the user, then
    call again with save=True. add_to_inventory also records the spool."""
    p = _printer(printer)
    info = materials.LabelInfo(material=material, brand=brand, product=product, colour_name=colour_name,
                               colour_hex=colour_hex, nozzle_min=nozzle_min, nozzle_max=nozzle_max,
                               nozzle_recommended=nozzle_recommended, bed_min=bed_min, bed_max=bed_max,
                               diameter_mm=diameter_mm, weight_g=weight_g, price=price, density=density,
                               max_speed_mm_s=max_speed_mm_s, drying=drying)
    built = materials.build_profile(LIB, p, info, name)
    prof = built["profile"]
    summary = {k: prof.get(k) for k in ("name", "filament_type", "filament_vendor", "nozzle_temperature",
                                        "nozzle_temperature_initial_layer", "nozzle_temperature_range_low",
                                        "nozzle_temperature_range_high", "textured_plate_temp", "hot_plate_temp",
                                        "default_filament_colour", "filament_density", "filament_cost") if k in prof}
    result: dict[str, Any] = {"preview": summary, "inherits_from": built["base"], "material": built["material"], "saved": None}
    if save:
        path = materials.save_profile(LIB, prof, overwrite)
        result["saved"] = str(path)
        result["note"] = "Restart Anycubic Slicer Next to see it; slice_model can use it right away."
    else:
        result["note"] = "Preview only. Confirm with the user, then call again with save=True."
    if add_to_inventory and save:
        result["spool"] = INVENTORY.add(built["material"], weight_g or 1000, brand=brand, colour_name=colour_name,
                                        colour_hex=colour_hex, filament_preset=prof["name"], price=price)
    return result


@mcp.tool()
@threaded
def add_spool(material: str, total_g: float = 1000, remaining_g: float | None = None, brand: str | None = None,
              colour_name: str | None = None, colour_hex: str | None = None, filament_preset: str | None = None,
              price: float | None = None, notes: str | None = None) -> dict:
    """Add a spool to the local inventory (kept on this computer)."""
    return INVENTORY.add(material, total_g, remaining_g, brand, colour_name, colour_hex, filament_preset, price, notes)


@mcp.tool()
@threaded
def list_spools(need_g: float | None = None, material: str | None = None) -> dict:
    """List spools and how much is left. need_g: also say which spools have enough for a print of that weight."""
    return INVENTORY.list(need_g, material)


@mcp.tool()
@threaded
def update_spool(spool_id: str, remaining_g: float | None = None, use_g: float | None = None,
                 remove: bool = False, notes: str | None = None, filament_preset: str | None = None) -> dict:
    """Update a spool: set remaining_g (e.g. after weighing), subtract use_g after a print, or remove it."""
    return INVENTORY.update(spool_id, remaining_g, use_g, remove, notes=notes, filament_preset=filament_preset)


# ======================================================================== troubleshooting & calibration
@mcp.tool()
@threaded
def diagnose_print(issues: list[str] | None = None, severity: str = "moderate", printer: str | None = None,
                   process: str | None = None, filament: str | None = None) -> dict:
    """Print doctor. Call with no issues to get the catalogue of defects and what each looks like; look at the
    user's photos of the failed print, pick the matching issue ids (e.g. stringing, warping, layer_adhesion),
    then call again with them. Returns causes, checks and concrete setting changes based on the presets in use,
    plus an overrides dict to re-slice with. Conflicting fixes are reported, not applied."""
    if not issues:
        return {"issues": doctor.catalog(),
                "how": "Compare the user's photos with 'looks_like', then call diagnose_print(issues=[...])."}
    chosen = SLICER.choose(printer, process, [filament] if filament else None)
    cfg = {"machine": LIB.resolve("machine", chosen["printer"]), "process": LIB.resolve("process", chosen["process"]),
           "filament": LIB.resolve("filament", chosen["filaments"][0])}
    report = doctor.diagnose(issues, cfg, severity)
    report["based_on"] = {"printer": chosen["printer"], "process": chosen["process"], "filament": chosen["filaments"][0]}
    return report


@mcp.tool()
@threaded
def make_calibration_print(kind: str, printer: str | None = None, process: str | None = None,
                           filament: str | None = None, values: list[float] | None = None,
                           output_dir: str | None = None) -> dict:
    """Generate and slice a calibration print. kind:
    temperature_tower: one tower, a block per nozzle temperature (values = temperatures, default from the filament range);
    flow: one small cube per flow ratio (values = ratios, default ±6% around the filament's);
    retraction: two towers per retraction length (values = mm, default 0.4–2.0);
    first_layer: five single-layer squares across the bed to check levelling and squish.
    Returns the files and how to read the result."""
    chosen = SLICER.choose(printer, process, [filament] if filament else None)
    fil = LIB.resolve("filament", chosen["filaments"][0])
    out = Path(output_dir).expanduser() if output_dir else _default_out() / "calibration"
    out.mkdir(parents=True, exist_ok=True)
    common = dict(printer=chosen["printer"], process=chosen["process"], filaments=chosen["filaments"], output_dir=str(out))
    if kind == "temperature_tower":
        lo = to_float(fil.get("nozzle_temperature_range_low"), 190) or 190
        hi = to_float(fil.get("nozzle_temperature_range_high"), 230) or 230
        temps = [int(v) for v in (values or list(range(int(hi), int(lo) - 1, -5))[:8])]
        if any(not 150 <= t <= 320 for t in temps):
            raise ValueError("Temperatures must be between 150 and 320 °C.")
        tower, cmds = calibration.temperature_tower(temps)
        res = SLICER.slice(SliceRequest(models=[], extra_meshes=[("temperature_tower", tower)], output_name="temperature_tower",
                                        overrides={"nozzle_temperature": temps[0], "nozzle_temperature_initial_layer": temps[0]}, **common))
        g = Path(res["plates"][0]["gcode"])
        tmp = g.with_suffix(".tmp")
        n = gc.inject_at_heights(g, tmp, cmds)
        tmp.replace(g)
        if res.get("gcode_3mf"):
            calibration.replace_gcode_in_3mf(Path(res["gcode_3mf"]), g)
        return {"files": [str(g), res.get("gcode_3mf")], "temperatures_bottom_to_top": temps, "changes_inserted": n,
                "time": res["plates"][0].get("time_normal"),
                "read_it": "Each block is 8 mm tall, hottest at the bottom. Pick the block with the least stringing "
                           "between the pillars, a clean bridge, and smooth walls; use that temperature."}
    if kind in ("flow", "retraction"):
        if kind == "flow":
            f0 = to_float(fil.get("filament_flow_ratio"), 0.98) or 0.98
            vals = values or [round(f0 + d, 3) for d in (-0.06, -0.03, 0, 0.03, 0.06)]
            key, mesh, label = "filament_flow_ratio", calibration.flow_cube(), "flow"
            if any(not 0.7 <= v <= 1.3 for v in vals):
                raise ValueError("Flow ratios must be between 0.7 and 1.3.")
        else:
            vals = values or [0.4, 0.8, 1.2, 1.6, 2.0]
            key, mesh, label = "filament_retraction_length", calibration.retraction_towers(), "retraction"
            if any(not 0 <= v <= 8 for v in vals):
                raise ValueError("Retraction lengths must be between 0 and 8 mm.")
        if len(vals) > 8:
            raise ValueError("Give at most 8 values.")
        files = []
        for v in vals:
            r = SLICER.slice(SliceRequest(models=[], extra_meshes=[(f"{label}_{v:g}", mesh)], output_name=f"{label}_{v:g}",
                                          overrides={key: v}, **common))
            files.append({"value": v, "gcode": r["plates"][0]["gcode"], "gcode_3mf": r.get("gcode_3mf"),
                          "time": r["plates"][0].get("time_normal")})
        how = ("Print each cube; pick the one whose top surface is smooth with no gaps and no ridges."
               if kind == "flow" else "Print each pair of towers; pick the shortest retraction with no strings between them.")
        return {"setting": key, "files": files, "read_it": how}
    if kind == "first_layer":
        bed = analysis.bed_size(LIB.resolve("machine", chosen["printer"]))
        res = SLICER.slice(SliceRequest(models=[], extra_meshes=[("first_layer_test", calibration.first_layer_test(bed))],
                                        output_name="first_layer_test", arrange=False, **common))
        return {"files": [res["plates"][0]["gcode"], res.get("gcode_3mf")], "time": res["plates"][0].get("time_normal"),
                "read_it": "All five squares should be evenly squished, lines just touching. Gaps = nozzle too high there; "
                           "rough, ridged or see-through-thin = too low. Adjust levelling or Z offset."}
    raise ValueError("kind must be temperature_tower, flow, retraction or first_layer")


@mcp.tool()
@threaded
def optimize_multicolor(models: list[str], filaments: list[str], object_filaments: list[int],
                        filament_colours: list[str] | None = None, printer: str | None = None,
                        process: str | None = None, output_dir: str | None = None,
                        output_name: str | None = None) -> dict:
    """Measure how much filament a multi-colour print wastes on colour changes, test waste-cutting strategies,
    and recommend one only if it keeps every colour and really saves filament. Plus layout tips."""
    return optimize.multicolor(SLICER, SliceRequest(
        models=models, printer=printer, process=process, filaments=filaments, object_filaments=object_filaments,
        filament_colours=filament_colours, output_dir=output_dir, output_name=output_name or Path(models[0]).stem))


# ======================================================================== whole projects
@mcp.tool()
@threaded
def plan_project(path: str | None = None, plan_id: str | None = None, goal: str | None = None,
                 max_hours_per_plate: float | None = None, separate_supported: bool | None = None,
                 quantities: dict[str, int] | None = None, exclude: list[str] | None = None,
                 include: list[str] | None = None, scales: dict[str, float] | None = None,
                 colours: dict[str, str] | None = None, answered: list[str] | None = None,
                 printer: str | None = None, process: str | None = None, filaments: list[str] | None = None,
                 overrides: dict | None = None, spacing_mm: float | None = None) -> dict:
    """Plan a multi-part project: every model in a folder or .zip (or one model file), each laid in its best
    orientation, packed onto as few plates as the goal allows. Reads counts from file names (clip_x4.stl) and READMEs.
    Returns `questions` for anything unclear (duplicate versions, odd scale, parts too big, missing counts, colours,
    grouping) plus stated `assumptions`. Blocking questions must be put to the user and answered before slicing.
    Re-plan with plan_id plus answers: quantities={"clip.stl": 4}, exclude=[ids], include=[ids], scales={id: 25.4},
    colours={id: "red"}, goal="fewest_plates"|"by_colour", max_hours_per_plate=8, separate_supported=true,
    answered=[question ids the user settled by keeping the default]. Ids are paths relative to the project folder."""
    if spacing_mm is not None:
        _bounded("spacing_mm", spacing_mm, 1, 50)
    if max_hours_per_plate is not None:
        _bounded("max_hours_per_plate", max_hours_per_plate, 0.25, 200)
    inputs = {k: v for k, v in dict(goal=goal, max_hours_per_plate=max_hours_per_plate,
                                    separate_supported=separate_supported, quantities=quantities, exclude=exclude,
                                    include=include, scales=scales, colours=colours, answered=answered, printer=printer,
                                    process=process, filaments=filaments, overrides=overrides,
                                    spacing_mm=spacing_mm).items() if v is not None}
    return project.summary(project.build_plan(SLICER, path, plan_id, inputs))


@mcp.tool()
@threaded
def slice_project(plan_id: str, plates: list[int] | None = None, output_dir: str | None = None) -> list:
    """Slice every plate of an approved plan (or just `plates`). Refuses while the plan has unanswered blocking
    questions. Each plate becomes its own .gcode and .gcode.3mf; parts that the slicer can't fit are moved to the
    next plate. Returns real times and weights per plate, totals, and a preview of each plate."""
    if plates:
        for n in plates:
            _bounded("plate", n, 1, 500)
    report = project.slice_plan(SLICER, plan_id, plates, output_dir)
    out: list = [json.dumps(report, indent=1)]
    for r in report["plates"][:6]:
        if r.get("preview") and Path(r["preview"]).is_file():
            out.append(Image(data=Path(r["preview"]).read_bytes(), format="png"))
    return out


# ======================================================================== prompts
@mcp.prompt()
def setup_check() -> str:
    """Check the slicer MCP is installed and working."""
    return ("Call slicer_status. If the slicer isn't found, tell me how to install Anycubic Slicer Next or set "
            "ANYCUBIC_SLICER_EXE. Otherwise summarise my printer, process and filament, and slice a 20 mm test cube "
            "to confirm everything works (create it with any CAD tool, or use make_calibration_print kind=flow with one value).")


@mcp.prompt()
def filament_from_label(photo_path: str = "") -> str:
    """Create a filament profile from a photo of the spool label."""
    return (f"Look at the spool label photo{(' at ' + photo_path) if photo_path else ''}. Read the brand, material, "
            "colour, nozzle and bed temperature ranges, diameter, weight and anything else printed on it. If something is "
            "unreadable, say so rather than guessing. Call create_filament_profile with what you read (save=False), show me "
            "the preview, and save it only after I confirm. Offer to add the spool to my inventory.")


@mcp.prompt()
def diagnose_from_photos(photo_paths: str = "") -> str:
    """Work out what went wrong with a print from photos."""
    return ("Call diagnose_print with no issues to get the defect catalogue. Then look carefully at my photos"
            f"{(' (' + photo_paths + ')') if photo_paths else ''}: describe what you see, match it to issue ids, and ask me "
            "about anything ambiguous (e.g. which side faced the bed, the filament type). Call diagnose_print with the "
            "issues, explain the likely causes, and offer to re-slice with the suggested overrides.")


@mcp.prompt()
def make_it_stronger(model_path: str = "", how_it_is_loaded: str = "") -> str:
    """Make a part stronger for how it will be used."""
    return (f"I need {model_path or 'this part'} to be strong. {how_it_is_loaded} Ask me how it will be loaded if I "
            "haven't said (pulled, pushed, bent, hit) and in which direction. Then call analyze_strength, explain the "
            "recommendations, and use compare_slices to show the time and filament cost of the stronger settings "
            "against my current ones before slicing.")


@mcp.prompt()
def multicolor_waste() -> str:
    """Reduce waste in a multi-colour print."""
    return ("Ask me which model files use which colours and the spool colours loaded. Call optimize_multicolor and "
            "explain how much filament goes to colour changes, whether any setting helps on my printer, and which "
            "layout changes would cut waste the most.")


@mcp.prompt()
def print_this_project(project_path: str = "") -> str:
    """Plan and slice a whole multi-part project from a folder or .zip."""
    return (f"Plan the project at {project_path or '(ask me for the folder or .zip)'} with plan_project. Before anything else, "
            "go through its questions with me: blocking ones first, at most three at a time, each with the options and "
            "your suggested default. Also ask me anything else you're unsure about (which goal matters, which spool is "
            "loaded, whether some parts are optional). Tell me the assumptions you're making. Re-plan with my answers, "
            "show me the plates (parts, rough time and filament), and only call slice_project once I say go. Afterwards, "
            "give me the real time and filament per plate and the order to print them in.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
