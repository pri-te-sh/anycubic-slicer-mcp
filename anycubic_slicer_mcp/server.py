"""MCP server: Anycubic Slicer Next for AI agents."""
from __future__ import annotations

import functools
import tempfile
import json
import re
import time
from pathlib import Path
from typing import Any

import anyio
from typing import Annotated

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from . import analysis, calibration, doctor, gcode as gc, materials, optimize, printer as lan, project, render
from . import cloud
from . import window as win
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
- Live printer, two ways. The Kobra X family runs either in LAN mode or on Anycubic Cloud, never both; ask the user
  which if unsure (find_printers finds nothing when LAN mode is off).
  LAN mode: find_printers → printer_status (state, job, temperatures, errors with their meaning, loaded slots) →
  printer_camera_snapshot. control_print pauses, resumes or stops a print. send_print uploads and starts a file.
  Cloud (LAN mode off, the phone app keeps working): everything goes through the slicer window, which is signed in
  to the user's account. cloud_printer_status reads its Workbench screen; print_from_slicer starts the plate open in
  the window through its Start Print dialog. Pause/stop on cloud: slicer_click on the Workbench's Pause or End,
  which needs confirm.
  Use them to triage a failed or paused print together with diagnose_print.
- Anything that starts, pauses, resumes or stops a print (send_print, print_from_slicer, control_print, slicer_click
  with confirm): call it without confirm first, tell the user exactly what will happen and ask them to check the bed
  when starting one, and only confirm after an explicit yes in this conversation. Never on your own initiative.
- Send a print: slice → preview_gcode → show the user → LAN: send_print. Cloud: restart_slicer with open_path and the
  same presets, slice in the window (slicer_click text='Slice plate'), check the window's time matches yours, then
  print_from_slicer.
- Slicer window: open_in_slicer shows a file; restart_slicer reopens the window with chosen presets selected (and makes
  it load newly saved presets); slicer_screenshot shows you the window and its text; slicer_click clicks a button by
  its text (macOS, needs the user's Accessibility permission). Use them when the user wants to see or do something
  in the window itself.
Never claim a print is safe without the user checking the preview. Nothing reaches a printer except send_print,
control_print, the camera start/stop of printer_camera_snapshot, print_from_slicer and confirmed slicer_click
presses, each as described above.
"""

mcp = FastMCP("anycubic-slicer", instructions=INSTRUCTIONS)
mcp._mcp_server.version = __version__  # report this package's version to clients, not the MCP library's
INSTALL = find_install()
LIB = PresetLibrary(INSTALL)
SLICER = Slicer(INSTALL, LIB)
INVENTORY = Inventory()


def _ann(title: str, read_only: bool = False, destructive: bool = False, idempotent: bool = True,
         open_world: bool = False) -> ToolAnnotations:
    """MCP behaviour hints. Only the printer tools reach beyond this computer (to a printer on the local network)."""
    return ToolAnnotations(title=title, readOnlyHint=read_only, destructiveHint=False if read_only else destructive,
                           idempotentHint=True if read_only else idempotent, openWorldHint=open_world)


# Parameter types shared by several tools; the descriptions end up in each tool's input schema.
Printer = Annotated[str | None, Field(description="Printer preset name or a unique part of it, e.g. 'Kobra X'. Omit to use the printer selected in the slicer window.")]
Process = Annotated[str | None, Field(description="Process preset (layer height, speeds), e.g. '0.20mm Standard'. Omit to use the slicer window's selection.")]
Filaments = Annotated[list[str] | None, Field(description="Filament preset names, one per filament slot, e.g. ['Anycubic PLA']. Omit to use the slicer window's selection.")]
ModelPath = Annotated[str, Field(description="Path to an STL, OBJ, 3MF or STEP file on this computer.")]
GcodePath = Annotated[str, Field(description="Path to a .gcode file on this computer, e.g. one written by slice_model.")]
OutputDir = Annotated[str | None, Field(description="Folder for the output files. Default: next to the first model.")]
PrinterHost = Annotated[str | None, Field(description="The printer's IP address on your network (shown on the printer under Settings > Network), e.g. '192.168.1.84'. Omit to use the last printer that worked, or ANYCUBIC_PRINTER_HOST.")]


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
@mcp.tool(title="Slicer status", annotations=_ann("Slicer status", read_only=True))
@threaded
def slicer_status() -> dict:
    """Report whether Anycubic Slicer Next is installed and what its window currently has selected.

    Call this first in a session, and whenever a slice fails with "not found": it returns the server version,
    operating system, slicer executable and version, data folder, how many printer/process/filament presets
    exist, the selected printer, process, filaments and plate type, and the build volume. Read-only; it only
    reads the slicer's config files and starts nothing."""
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


@mcp.tool(title="List presets", annotations=_ann("List presets", read_only=True))
@threaded
def list_presets(
    kind: Annotated[str, Field(description="Which presets: 'machine' (printers), 'process' (layer height, speeds) or 'filament'.")],
    printer: Printer = None,
    search: Annotated[str | None, Field(description="Case-insensitive part of the name to filter by, e.g. 'PETG' or '0.16mm'.")] = None,
    include_incompatible: Annotated[bool, Field(description="Also list process/filament presets made for other printers, marked fits_printer=false.")] = False,
) -> dict:
    """List preset names so you can pass exact ones to slice_model, compare_slices or get_preset.

    Use this to discover names; use get_preset to read the settings inside one preset. With a printer, only
    process and filament presets made for that printer are listed, the user's own presets first. Returns
    {count, presets: [{name, source}], truncated} with at most 300 rows. Read-only."""
    LIB.reload()
    p = _printer(printer) if printer else None
    rows = LIB.listing(kind, p, search, include_incompatible)
    return {"count": len(rows), "presets": rows[:300], "truncated": len(rows) > 300}


@mcp.tool(title="Read a preset's settings", annotations=_ann("Read a preset's settings", read_only=True))
@threaded
def get_preset(
    name: Annotated[str, Field(description="Preset name, or a unique part of it, e.g. 'Anycubic PLA'.")],
    kind: Annotated[str, Field(description="'machine', 'process' or 'filament'.")],
    search: Annotated[str | None, Field(description="Only return settings whose key contains this text, e.g. 'infill', 'support', 'temp', 'retract'.")] = None,
    keys: Annotated[list[str] | None, Field(description="Exact setting keys to return in full, including long G-code templates.")] = None,
    printer: Printer = None,
) -> dict:
    """Read the effective settings of one preset, with its inheritance chain merged.

    Use it to find the exact setting names to pass as overrides to slice_model, compare_slices or
    save_user_preset (search='infill' finds sparse_infill_density and friends). Values longer than 200
    characters are elided unless you ask for them by keys. Returns {preset, source, inherits, settings}. Read-only."""
    LIB.reload()
    preset = LIB.get(kind, name, _printer(printer) if printer else None)
    cfg = LIB.resolve(preset.kind, preset.name)
    if keys:
        return {"preset": preset.name, "settings": {k: cfg.get(k) for k in keys}}
    items = {k: v for k, v in cfg.items() if not search or search.lower() in k.lower()}
    trimmed = {k: (v if len(json.dumps(v)) <= 200 else f"<{len(json.dumps(v))} chars; ask for it via keys>")
               for k, v in sorted(items.items())}
    return {"preset": preset.name, "source": preset.source, "inherits": preset.parent, "settings": trimmed}


@mcp.tool(title="Save a user preset", annotations=_ann("Save a user preset", destructive=True))
@threaded
def save_user_preset(
    kind: Annotated[str, Field(description="'process', 'filament' or 'machine'.")],
    name: Annotated[str, Field(description="Name for the new preset as it will appear in the slicer, 1-120 characters, no <>:\"/\\|?*;")],
    inherits: Annotated[str, Field(description="Existing preset to start from; the new preset stores only what differs.")],
    settings: Annotated[dict, Field(description="Setting key to value, only the values that differ, e.g. {'wall_loops': 3}. Find keys with get_preset.")],
    printer: Annotated[str | None, Field(description="Limit the new process/filament preset to this printer. Omit to keep the parent's printers.")] = None,
    overwrite: Annotated[bool, Field(description="Replace an existing user preset with the same name. Default false refuses.")] = False,
) -> dict:
    """Write a new process, filament or printer preset into the user's Anycubic Slicer Next presets.

    Use it when the user wants settings kept for future prints; for a one-off print pass overrides to slice_model
    instead, and for fixing a failed print use diagnose_print first (which only suggests changes and saves
    nothing). Writes one JSON file to the slicer's user preset folder; with overwrite=true it replaces an existing
    one. Ask the user before calling. The slicer window shows it after a restart. Returns {saved, kind, inherits}."""
    LIB.reload()
    parent = LIB.get(kind, inherits)
    if not re.fullmatch(r"[^<>:\"/\\|?*;\x00-\x1f]{1,120}", name):
        raise ValueError("Preset name must be 1–120 characters without <>:\"/\\|?*;")
    base = LIB.resolve(parent.kind, parent.name)
    router = SettingRouter(LIB)
    protected = {"name", "inherits", "from", "instantiation", "compatible_printers", "compatible_printers_condition",
                 "setting_id", "filament_id", "type"}
    root = LIB.system_root(parent.kind, parent.name)
    if root is None:
        raise ValueError(f"'{parent.name}' doesn't lead back to a system preset, so the slicer window couldn't load it.")
    data: dict[str, Any] = {"name": name, "inherits": root.name, "from": "User", "instantiation": "true"}
    if root.name != parent.name:  # the window only loads user presets whose parent is a system preset
        root_cfg = LIB.resolve(root.kind, root.name)
        meta = protected | {"version", "is_custom_defined", "base_id", "user_id", "updated_time",
                            "filament_settings_id", "print_settings_id", "printer_settings_id"}
        data.update({k: v for k, v in base.items() if k not in meta and root_cfg.get(k) != v})
    for key, value in settings.items():
        if key in protected:
            raise ValueError(f"'{key}' can't be set here.")
        router.check_key(key)
        data[key] = format_value(value, base.get(key, [] if parent.kind == "filament" else ""))
    from .presets import NOZZLE_VARIANTS
    for key in settings:  # per-nozzle copies carried over from a user parent would outrank the new value
        for suffix in NOZZLE_VARIANTS:
            if key + suffix not in settings:
                data.pop(key + suffix, None)
    if printer and parent.kind != "machine":
        data["compatible_printers"] = [_printer(printer)]
    if parent.kind == "filament":
        data["filament_settings_id"] = [name]
    elif parent.kind == "process":
        data["print_settings_id"] = name
    paths = LIB.write_user_preset(parent.kind, data, overwrite=overwrite)
    return {"saved": str(paths[0]), "also_in": [str(x.parent.parent.name) for x in paths[1:]],
            "kind": parent.kind, "inherits": root.name,
            "note": "The slicer window lists it after a restart (restart_slicer)."}


# ======================================================================== models & slicing
@mcp.tool(title="Model info", annotations=_ann("Model info", read_only=True))
@threaded
def model_info(path: ModelPath, printer: Printer = None) -> dict:
    """Measure a 3D model before slicing: size, volume, surface area, triangles, separate pieces, watertightness
    and whether it fits the printer's build volume.

    Use it first on an unfamiliar file. For which way up to print it use find_best_orientation, and to test a
    rotation or scale against the bed use check_bed_fit. STEP files are converted with the slicer first.
    Read-only; it never changes the file."""
    mesh = _model_mesh(path)
    info = describe(mesh)
    try:
        info["bed_fit"] = analysis.fit_report(mesh, _bed(_printer(printer)))
    except ValueError:
        pass
    if not info["watertight"]:
        info["note"] = "Not watertight: the slicer may repair it, but check the result (holes, missing walls)."
    return info


@mcp.tool(title="Slice a model", annotations=_ann("Slice a model"))
@threaded
def slice_model(
    models: Annotated[list[str], Field(description="Paths to STL, 3MF, OBJ or STEP files, all placed on one plate.")],
    printer: Printer = None,
    process: Process = None,
    filaments: Filaments = None,
    overrides: Annotated[dict | None, Field(description="Setting key to value for this slice only, e.g. {'sparse_infill_density': '20%', 'wall_loops': 3, 'enable_support': true}. Keys come from get_preset.")] = None,
    output_dir: OutputDir = None,
    output_name: Annotated[str | None, Field(description="Base name for the output files. Default: the first model's name.")] = None,
    plate: Annotated[int, Field(description="Which plate of a multi-plate 3MF to slice; 0 slices all plates.")] = 0,
    arrange: Annotated[bool, Field(description="Let the slicer arrange parts on the plate. Turn off to keep 3MF positions.")] = True,
    orient: Annotated[bool, Field(description="Let the slicer auto-orient parts. Prefer find_best_orientation plus rotation_deg.")] = False,
    rotation_deg: Annotated[dict | None, Field(description="Rotation in degrees applied before slicing, e.g. {'x': 90, 'y': 0, 'z': 0}.")] = None,
    scale: Annotated[float | None, Field(description="Uniform scale factor, e.g. 25.4 for a model exported in inches. 0.01 to 100.")] = None,
    copies: Annotated[int, Field(description="Copies of each model on the plate, 1 to 200.")] = 1,
    object_filaments: Annotated[list[int] | None, Field(description="Multi-colour: filament slot (1-based) for each model, in order, e.g. [1, 2].")] = None,
    filament_colours: Annotated[list[str] | None, Field(description="Multi-colour: '#RRGGBB' for each filament slot, matching the loaded spools.")] = None,
    bed_type: Annotated[str | None, Field(description="Plate type, e.g. 'Textured PEI Plate', 'Smooth PEI Plate', 'Cool Plate'. Default: the selected plate.")] = None,
    timeout_s: Annotated[int, Field(description="Give up after this many seconds, 30 to 7200.")] = 900,
) -> dict:
    """Slice 3D models into printer-ready G-code with Anycubic Slicer Next, running locally.

    The main tool for a single plate. For several variants of one model use compare_slices; for a whole folder
    of parts use plan_project and slice_project. Writes <name>.gcode and <name>.gcode.3mf (send the .gcode.3mf to
    the printer) to output_dir, replacing files of the same name; takes seconds to a few minutes. Returns print
    time, grams, cost, layers, per-filament use, the output paths and slicer warnings, which flag overrides the
    slicer clamped or ignored. It never sends anything to a printer."""
    _bounded("plate", plate, 0, 99); _bounded("copies", copies, 1, 200); _bounded("timeout_s", timeout_s, 30, 7200)
    return SLICER.slice(SliceRequest(
        models=models, printer=printer, process=process, filaments=filaments, overrides=overrides,
        output_dir=output_dir, output_name=output_name, plate=plate, arrange=arrange, orient=orient,
        rotation=rotation_deg, scale=scale, copies=copies, object_filaments=object_filaments,
        filament_colours=filament_colours, bed_type=bed_type, timeout_s=timeout_s))


@mcp.tool(title="G-code summary", annotations=_ann("G-code summary", read_only=True))
@threaded
def gcode_summary(path: GcodePath) -> dict:
    """Read the numbers out of an existing .gcode file: print time, filament grams, metres and cost, layers,
    first-layer time, colour changes, key settings and metres extruded per feature type.

    Use it on G-code you already have (from slice_model or another slicer); slice_model already returns these for
    new slices. Read-only."""
    p = checked_file(path, gc.GCODE_SUFFIXES)
    s = gc.summarize(p)
    s["extrusion_m_by_type"] = gc.read_toolpaths(p).feature_lengths()
    return s


@mcp.tool(title="Preview G-code", annotations=_ann("Preview G-code", read_only=True))
@threaded
def preview_gcode(
    path: GcodePath,
    layer: Annotated[int | None, Field(description="1-based layer to draw coloured by feature. Omit to draw every layer shaded by height.")] = None,
    view: Annotated[str, Field(description="'top' (from above) or 'side' (front elevation of the whole print).")] = "top",
    size: Annotated[int, Field(description="Image size in pixels, 200 to 2000.")] = 900,
) -> list:
    """Draw G-code toolpaths as a PNG so you and the user can check a slice before printing.

    view='top' without a layer shows placement on the bed; with a layer it colours walls orange/yellow, infill
    red, top surfaces pink, bottom blue, bridges cyan and support teal. view='side' shows overhangs and support.
    For a build-up animation use animate_layers; for a shareable summary use print_card. Read-only: returns the
    image and layer metadata without writing files."""
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


@mcp.tool(title="Animate layers", annotations=_ann("Animate layers"))
@threaded
def animate_layers(
    path: GcodePath,
    size: Annotated[int, Field(description="Frame size in pixels, 200 to 1000.")] = 520,
    max_frames: Annotated[int, Field(description="Most frames in the GIF, 5 to 200; layers are skipped evenly to fit.")] = 60,
) -> list:
    """Make an animated GIF of the print building up layer by layer.

    Use it when the user wants to watch the build or share it; for checking one layer use preview_gcode instead.
    Writes <name>_layers.gif next to the G-code, replacing an older one. Returns the GIF and its path."""
    _bounded("size", size, 200, 1000); _bounded("max_frames", max_frames, 5, 200)
    p = checked_file(path, gc.GCODE_SUFFIXES)
    out = p.with_name(p.stem.replace(".gcode", "") + "_layers.gif")
    render.layer_animation(gc.read_toolpaths(p), out, size, max_frames)
    return [Image(data=out.read_bytes(), format="gif"), json.dumps({"gif": str(out)})]


@mcp.tool(title="Print card", annotations=_ann("Print card"))
@threaded
def print_card(
    path: GcodePath,
    title: Annotated[str | None, Field(description="Heading on the card. Default: the G-code file name.")] = None,
    price_per_kg: Annotated[float | None, Field(description="Filament price per kg for the cost line. Default: the cost stored in the G-code.")] = None,
    currency_symbol: Annotated[str, Field(description="Symbol shown before the cost, e.g. '$' or '€'.")] = "$",
) -> list:
    """Make a 1200x630 PNG summary card of a sliced print: toolpath preview, time, filament, cost, layers, infill,
    walls and printer.

    Use it to share or quote a print; for inspecting toolpaths use preview_gcode. Writes <name>_card.png next to
    the G-code, replacing an older one. Returns the image and its path."""
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


@mcp.tool(title="Open in slicer", annotations=_ann("Open in slicer", idempotent=False))
@threaded
def open_in_slicer(
    path: Annotated[str, Field(description="A model (STL, OBJ, 3MF, STEP), a .gcode or a .gcode.3mf on this computer.")],
) -> str:
    """Open a file in the Anycubic Slicer Next window on the user's screen.

    Only call it when the user asks to see or edit something in the slicer; every other tool works without the
    window. 3MF files are checked for post-processing scripts first and refused if they have one. Changes no
    files."""
    p = checked_file(path, MODEL_SUFFIXES | gc.GCODE_SUFFIXES | {".gcode.3mf"})
    if p.suffix.lower() == ".3mf":
        from .slicer import scan_3mf
        scan_3mf(p)
    SLICER.open_in_gui(p)
    return f"Opened {p.name} in Anycubic Slicer Next."


# ======================================================================== planning
@mcp.tool(title="Compare slices", annotations=_ann("Compare slices"))
@threaded
def compare_slices(
    models: Annotated[list[str], Field(description="Model paths sliced the same way in every variant.")],
    variants: Annotated[list[dict], Field(description="Up to 8 variants, each {'label': str, 'overrides': {...}, 'process': str, 'filaments': [...], 'rotation': {...}, 'scale': float}; all keys optional except label. The first is the reference.")],
    printer: Printer = None,
    process: Process = None,
    filaments: Filaments = None,
    output_dir: OutputDir = None,
    output_name: Annotated[str | None, Field(description="Base name for the output files. Default: the first model's name.")] = None,
) -> dict:
    """Slice the same model several ways and compare print time, filament and cost side by side.

    Use it to show the price of a choice (stronger settings, finer layers, another orientation) before
    committing; for a single slice use slice_model. Runs the slicer once per variant, so it takes longer, and
    writes each variant's G-code to output_dir. Returns each variant's numbers, the change against the first,
    and the fastest and lightest variant."""
    base = SliceRequest(models=models, printer=printer, process=process, filaments=filaments,
                        output_dir=output_dir, output_name=output_name or Path(models[0]).stem)
    return optimize.compare(SLICER, base, variants)


@mcp.tool(title="Find the best orientation", annotations=_ann("Find the best orientation", read_only=True))
@threaded
def find_best_orientation(
    model: ModelPath,
    goal: Annotated[str, Field(description="'balanced', 'least_support', 'fastest', 'strongest' or 'best_bed_adhesion'.")] = "balanced",
    printer: Printer = None,
    load_axis: Annotated[str | list[float] | None, Field(description="Direction the part is pushed or pulled in use, in model coordinates: 'x', 'y', 'z' or [x, y, z]. Used by goal='strongest'.")] = None,
    support_angle: Annotated[float, Field(description="Overhang angle in degrees from vertical that needs support, 10 to 80.")] = 45.0,
    top: Annotated[int, Field(description="How many ranked orientations to return, 1 to 12.")] = 5,
) -> dict:
    """Rank the ways a model can lie on the bed by support area, height, bed contact, weak layers and fit.

    Use it before slicing a part whose orientation isn't obvious; pass the winner's rotation to slice_model as
    rotation_deg. For load-bearing parts, analyze_strength also recommends walls and infill. Works on the mesh
    alone without running the slicer. Read-only."""
    _bounded("support_angle", support_angle, 10, 80); _bounded("top", top, 1, 12)
    mesh = _model_mesh(model)
    bed = _bed(_printer(printer))
    rows = analysis.rank_orientations(mesh, goal, bed, analysis.parse_axis(load_axis), support_angle, top)
    return {"goal": goal, "build_volume_mm": list(bed), "ranked": rows,
            "use": "slice_model(..., rotation_deg=ranked[0].rotation)"}


@mcp.tool(title="Analyze strength", annotations=_ann("Analyze strength", read_only=True))
@threaded
def analyze_strength(
    model: ModelPath,
    load: Annotated[str, Field(description="How the part is loaded: 'general', 'tension', 'compression', 'bending', 'impact' or 'torsion'.")] = "general",
    load_axis: Annotated[str | list[float] | None, Field(description="Direction of the load in model coordinates: 'x', 'y', 'z' or [x, y, z]. For a gear pushed at its teeth, use an axis in the gear's plane.")] = None,
    priority: Annotated[str, Field(description="'max_strength', 'balanced' or 'light' (less filament).")] = "balanced",
    printer: Printer = None,
    filament: Annotated[str | None, Field(description="Filament preset, used for material-specific advice. Default: the selected filament.")] = None,
) -> dict:
    """Recommend an orientation and slicer settings (walls, top/bottom layers, infill, layer height) that make a
    part strong for how it will be used.

    Use it when the user says a part must hold, bend or survive impact; find_best_orientation only ranks
    orientations. Also flags thin waists in the shape. Returns overrides and a rotation to pass to slice_model,
    with reasons; check the cost with compare_slices. Read-only."""
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


@mcp.tool(title="Check bed fit", annotations=_ann("Check bed fit", read_only=True))
@threaded
def check_bed_fit(
    model: ModelPath,
    printer: Printer = None,
    rotation_deg: Annotated[dict | None, Field(description="Rotation to test first, e.g. {'z': 45}.")] = None,
    scale: Annotated[float | None, Field(description="Scale factor to test first, e.g. 0.8.")] = None,
) -> dict:
    """Check whether a model fits the printer's build volume, optionally after a rotation or scale.

    If it doesn't fit it suggests a 45-degree turn, a scale percentage, laying it down, or splitting it in CAD.
    model_info gives the same fit check without trying transforms. Read-only."""
    mesh = _model_mesh(model)
    r = rotation_deg or {}
    m = rotation(r.get("x", 0), r.get("y", 0), r.get("z", 0)) * (scale or 1.0)
    return analysis.fit_report(mesh.transformed(m).on_bed(), _bed(_printer(printer)))


@mcp.tool(title="Estimate cost", annotations=_ann("Estimate cost", read_only=True))
@threaded
def estimate_cost(
    gcode: Annotated[str | None, Field(description="Path to a sliced .gcode; supplies grams, time and filament price. Or give grams and print_time_s.")] = None,
    grams: Annotated[float | None, Field(description="Filament used in grams, when no gcode is given.")] = None,
    print_time_s: Annotated[float | None, Field(description="Print time in seconds, when no gcode is given.")] = None,
    price_per_kg: Annotated[float | None, Field(description="Filament price per kg. Default: the price stored in the G-code.")] = None,
    printer_power_w: Annotated[float, Field(description="Average printer power draw in watts.")] = 150,
    electricity_per_kwh: Annotated[float, Field(description="Electricity price per kWh.")] = 0.15,
    extra: Annotated[float, Field(description="Fixed amount added, e.g. labour or markup.")] = 0.0,
    currency: Annotated[str, Field(description="Currency label for the result, e.g. 'USD'.")] = "USD",
) -> dict:
    """Work out what a print costs: filament plus electricity plus any extra such as labour or markup.

    Use it for quotes or when the user asks what something costs; compare_slices already reports filament cost
    per variant. Returns each part of the cost and the total. Read-only; pure arithmetic."""
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
@mcp.tool(title="Create filament profile", annotations=_ann("Create filament profile", destructive=True))
@threaded
def create_filament_profile(
    material: Annotated[str, Field(description="Material printed on the label, e.g. 'PLA', 'PETG', 'PLA+', 'ABS', 'TPU', 'PA-CF'.")],
    printer: Printer = None,
    brand: Annotated[str | None, Field(description="Brand on the label.")] = None,
    product: Annotated[str | None, Field(description="Product line on the label, e.g. 'Silk' or 'Matte'.")] = None,
    colour_name: Annotated[str | None, Field(description="Colour name on the label, e.g. 'Galaxy Black'.")] = None,
    colour_hex: Annotated[str | None, Field(description="Colour as '#RRGGBB', estimated from the photo if the label has none.")] = None,
    nozzle_min: Annotated[float | None, Field(description="Lowest nozzle temperature on the label, °C.")] = None,
    nozzle_max: Annotated[float | None, Field(description="Highest nozzle temperature on the label, °C.")] = None,
    nozzle_recommended: Annotated[float | None, Field(description="Recommended nozzle temperature if the label gives one, °C.")] = None,
    bed_min: Annotated[float | None, Field(description="Lowest bed temperature on the label, °C.")] = None,
    bed_max: Annotated[float | None, Field(description="Highest bed temperature on the label, °C.")] = None,
    diameter_mm: Annotated[float | None, Field(description="Filament diameter, usually 1.75.")] = None,
    weight_g: Annotated[float | None, Field(description="Net filament weight in grams, e.g. 1000.")] = None,
    price: Annotated[float | None, Field(description="Price paid for the spool, used to set cost per kg.")] = None,
    density: Annotated[float | None, Field(description="Density in g/cm³ if printed on the label.")] = None,
    max_speed_mm_s: Annotated[float | None, Field(description="Maximum print speed in mm/s if printed on the label.")] = None,
    drying: Annotated[str | None, Field(description="Drying instructions from the label, kept in the profile notes.")] = None,
    name: Annotated[str | None, Field(description="Preset name. Default: 'Brand Product Colour @printer'.")] = None,
    save: Annotated[bool, Field(description="False (default) only previews. True writes the preset; do it after the user confirms the preview.")] = False,
    overwrite: Annotated[bool, Field(description="With save=true, replace an existing preset of the same name.")] = False,
    add_to_inventory: Annotated[bool, Field(description="With save=true, also record the spool in the local inventory.")] = False,
) -> dict:
    """Turn the details printed on a filament spool label into a filament preset for the slicer.

    Read the label photo yourself and pass what it says; only material is required, and anything missing gets a
    typical value for that material (listed in the result). The preset inherits from the closest existing preset
    for the printer. With save=false it only returns a preview; show it to the user, then call again with
    save=true to write it to the slicer's user presets (overwrite=true replaces an existing one). For other preset
    edits use save_user_preset."""
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


@mcp.tool(title="Add spool", annotations=_ann("Add spool", idempotent=False))
@threaded
def add_spool(
    material: Annotated[str, Field(description="Material, e.g. 'PLA' or 'PETG'.")],
    total_g: Annotated[float, Field(description="Net weight of a full spool in grams.")] = 1000,
    remaining_g: Annotated[float | None, Field(description="Grams left now, if not full.")] = None,
    brand: Annotated[str | None, Field(description="Brand.")] = None,
    colour_name: Annotated[str | None, Field(description="Colour name.")] = None,
    colour_hex: Annotated[str | None, Field(description="Colour as '#RRGGBB'.")] = None,
    filament_preset: Annotated[str | None, Field(description="Filament preset to print it with.")] = None,
    price: Annotated[float | None, Field(description="Price paid.")] = None,
    notes: Annotated[str | None, Field(description="Free-text notes, e.g. where it's stored or when it was dried.")] = None,
) -> dict:
    """Record a new spool in the user's local filament inventory.

    Use it when the user buys or opens a spool; create_filament_profile can also add one when saving a profile.
    Each call adds another spool with a new id, stored in a JSON file on this computer. Returns the spool."""
    return INVENTORY.add(material, total_g, remaining_g, brand, colour_name, colour_hex, filament_preset, price, notes)


@mcp.tool(title="List spools", annotations=_ann("List spools", read_only=True))
@threaded
def list_spools(
    need_g: Annotated[float | None, Field(description="Grams a planned print needs; spools with enough left (plus 5%) are marked.")] = None,
    material: Annotated[str | None, Field(description="Only list spools of this material.")] = None,
) -> dict:
    """List the spools in the local inventory with how much filament each has left.

    Use it before a long print to pick a spool with enough filament. Read-only."""
    return INVENTORY.list(need_g, material)


@mcp.tool(title="Update spool", annotations=_ann("Update spool", destructive=True, idempotent=False))
@threaded
def update_spool(
    spool_id: Annotated[str, Field(description="Spool id from list_spools or add_spool.")],
    remaining_g: Annotated[float | None, Field(description="Set grams left, e.g. after weighing the spool.")] = None,
    use_g: Annotated[float | None, Field(description="Subtract grams used by a finished print.")] = None,
    remove: Annotated[bool, Field(description="Delete the spool from the inventory.")] = False,
    notes: Annotated[str | None, Field(description="Replace the spool's notes.")] = None,
    filament_preset: Annotated[str | None, Field(description="Set the filament preset to print it with.")] = None,
) -> dict:
    """Change a spool in the local inventory: set what's left, subtract a finished print, edit notes, or remove it.

    use_g subtracts each time it is called, so don't repeat it for the same print. remove=true deletes the spool
    record permanently. Returns the updated spool."""
    return INVENTORY.update(spool_id, remaining_g, use_g, remove, notes=notes, filament_preset=filament_preset)


# ======================================================================== troubleshooting & calibration
@mcp.tool(title="Diagnose a failed print", annotations=_ann("Diagnose a failed print", read_only=True))
@threaded
def diagnose_print(
    issues: Annotated[list[str] | None, Field(description="Defect ids seen in the photos, e.g. ['stringing', 'warping']. Omit to get the catalogue of 13 defects with what each looks like.")] = None,
    severity: Annotated[str, Field(description="'mild', 'moderate' or 'severe'; scales how big the suggested changes are.")] = "moderate",
    printer: Printer = None,
    process: Process = None,
    filament: Annotated[str | None, Field(description="Filament preset the print used. Default: the selected filament.")] = None,
) -> dict:
    """Explain what went wrong with a failed 3D print and which settings to change, from defects you identify in
    the user's photos.

    First call it with no issues to get the defect catalogue, compare the photos with each 'looks_like', then call
    again with the matching ids. Suggestions are computed from the presets actually in use; fixes that conflict
    are reported, not applied. Read-only: it changes no presets (save them with save_user_preset if the user wants
    to keep them). Returns causes, checks and an overrides dict for slice_model."""
    if not issues:
        return {"issues": doctor.catalog(),
                "how": "Compare the user's photos with 'looks_like', then call diagnose_print(issues=[...])."}
    chosen = SLICER.choose(printer, process, [filament] if filament else None)
    cfg = {"machine": LIB.resolve("machine", chosen["printer"]), "process": LIB.resolve("process", chosen["process"]),
           "filament": LIB.resolve("filament", chosen["filaments"][0])}
    report = doctor.diagnose(issues, cfg, severity)
    report["based_on"] = {"printer": chosen["printer"], "process": chosen["process"], "filament": chosen["filaments"][0]}
    return report


@mcp.tool(title="Make a calibration print", annotations=_ann("Make a calibration print"))
@threaded
def make_calibration_print(
    kind: Annotated[str, Field(description="'temperature_tower', 'flow', 'retraction' or 'first_layer'.")],
    printer: Printer = None,
    process: Process = None,
    filament: Annotated[str | None, Field(description="Filament preset to calibrate. Default: the selected filament.")] = None,
    values: Annotated[list[float] | None, Field(description="Values to test: temperatures in °C, flow ratios, or retraction lengths in mm; at most 8. Default: a sensible range for the filament.")] = None,
    output_dir: Annotated[str | None, Field(description="Folder for the files. Default: Documents/Anycubic Slicer MCP/calibration.")] = None,
) -> dict:
    """Generate and slice a calibration print for tuning a filament or the printer.

    temperature_tower: one tower with a block per nozzle temperature, with the temperature changes written into
    the G-code. flow: one small cube per flow ratio. retraction: two towers per retraction length. first_layer:
    five single-layer squares across the bed to check levelling. Use it when a filament is new or prints look off
    and diagnose_print points to temperature, flow or retraction. Writes the G-code and .gcode.3mf files and
    returns their paths plus how to read the result."""
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


@mcp.tool(title="Optimize multi-colour waste", annotations=_ann("Optimize multi-colour waste"))
@threaded
def optimize_multicolor(
    models: Annotated[list[str], Field(description="Model paths, one per colour part, in the same order as object_filaments.")],
    filaments: Annotated[list[str], Field(description="Filament preset for each slot, e.g. two entries for a two-colour print.")],
    object_filaments: Annotated[list[int], Field(description="Filament slot (1-based) for each model, e.g. [1, 2].")],
    filament_colours: Annotated[list[str] | None, Field(description="'#RRGGBB' for each slot; colour order affects how much is flushed.")] = None,
    printer: Printer = None,
    process: Process = None,
    output_dir: OutputDir = None,
    output_name: Annotated[str | None, Field(description="Base name for the output files.")] = None,
) -> dict:
    """Measure how much filament a multi-colour print wastes on colour changes and try settings that might
    reduce it.

    Recommends a setting only if every colour survives and it saves filament; otherwise says the default is best
    and gives layout tips such as fewer colour changes per layer. Slices several times, so it takes a while, and
    writes each test's G-code to output_dir. For single-colour prints use slice_model or compare_slices."""
    return optimize.multicolor(SLICER, SliceRequest(
        models=models, printer=printer, process=process, filaments=filaments, object_filaments=object_filaments,
        filament_colours=filament_colours, output_dir=output_dir, output_name=output_name or Path(models[0]).stem))


# ======================================================================== whole projects
@mcp.tool(title="Plan a multi-part project", annotations=_ann("Plan a multi-part project", idempotent=False))
@threaded
def plan_project(
    path: Annotated[str | None, Field(description="Folder, .zip, or single model file of the project. Required unless plan_id is given.")] = None,
    plan_id: Annotated[str | None, Field(description="Re-plan an earlier plan with new answers; earlier answers are kept.")] = None,
    goal: Annotated[str | None, Field(description="'fewest_plates' or 'by_colour' (one colour per plate).")] = None,
    max_hours_per_plate: Annotated[float | None, Field(description="Keep each plate under this many hours (rough estimate), 0.25 to 200.")] = None,
    separate_supported: Annotated[bool | None, Field(description="True puts parts that need supports on their own plates.")] = None,
    quantities: Annotated[dict[str, int] | None, Field(description="Copies per part id, e.g. {'clip.stl': 4}.")] = None,
    exclude: Annotated[list[str] | None, Field(description="Part ids to leave out.")] = None,
    include: Annotated[list[str] | None, Field(description="Part ids to put back in, such as test pieces left out by default.")] = None,
    scales: Annotated[dict[str, float] | None, Field(description="Scale per part id, e.g. {'bracket.stl': 25.4} for a part exported in inches.")] = None,
    colours: Annotated[dict[str, str] | None, Field(description="Colour per part id, e.g. {'knob.stl': 'red'}, used by goal='by_colour'.")] = None,
    answered: Annotated[list[str] | None, Field(description="Question ids the user settled by keeping the suggested default.")] = None,
    printer: Printer = None,
    process: Process = None,
    filaments: Filaments = None,
    overrides: Annotated[dict | None, Field(description="Setting overrides applied to every plate, as for slice_model.")] = None,
    spacing_mm: Annotated[float | None, Field(description="Gap between parts on a plate in mm, 1 to 50. Default 6.")] = None,
) -> dict:
    """Plan a whole multi-part project from a folder or .zip: find every model, lay each in its best orientation
    and pack them onto plates.

    Reads counts from names like clip_x4.stl and from READMEs. Returns plates with rough time and filament, stated
    assumptions, and questions about anything unclear (duplicate versions, odd scale, parts too big, missing
    counts, colours, grouping). Put the blocking questions to the user, then call again with plan_id and the
    answers; part ids are paths relative to the project folder. Slicing happens only in slice_project. Saves the
    plan, and unpacks zips, under the server's state folder; each call without plan_id starts a new plan."""
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


@mcp.tool(title="Slice a planned project", annotations=_ann("Slice a planned project"))
@threaded
def slice_project(
    plan_id: Annotated[str, Field(description="Plan id from plan_project.")],
    plates: Annotated[list[int] | None, Field(description="Only slice these plate numbers. Default: all.")] = None,
    output_dir: Annotated[str | None, Field(description="Folder for the plate files. Default: Documents/Anycubic Slicer MCP/<plan id>.")] = None,
) -> list:
    """Slice every plate of a plan from plan_project once the user has approved it.

    Refuses while any blocking question is unanswered. Each plate is sliced separately into its own .gcode and
    .gcode.3mf; parts the slicer can't fit move to the next plate. Takes about as long as slicing each plate,
    writes the files plus project_summary.json to output_dir (replacing earlier runs), and returns real times and
    weights per plate, totals, and a preview image of up to six plates."""
    if plates:
        for n in plates:
            _bounded("plate", n, 1, 500)
    report = project.slice_plan(SLICER, plan_id, plates, output_dir)
    out: list = [json.dumps(report, indent=1)]
    for r in report["plates"][:6]:
        if r.get("preview") and Path(r["preview"]).is_file():
            out.append(Image(data=Path(r["preview"]).read_bytes(), format="png"))
    return out


# ======================================================================== live printer (LAN mode)
def _lan(fn):
    """Turn printer errors into plain messages for the agent."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except lan.PrinterError as e:
            raise ValueError(str(e)) from None
    return wrapper


@mcp.tool(title="Find printers", annotations=_ann("Find printers", read_only=True, open_world=True))
@threaded
@_lan
def find_printers(
    subnet: Annotated[str | None, Field(description="Private IPv4 range to search, e.g. '192.168.1.0/24' (at most 1024 addresses). Default: this computer's own /24 network.")] = None,
    host: PrinterHost = None,
) -> dict:
    """Look for Anycubic printers on the local network and report whether their LAN mode is on.

    Use it before the other printer tools when no printer address is known. Probes port 18910 on every address in
    the range (a few seconds) and reads each printer's public info: address, model, name and LAN-mode state. With
    host, checks just that address. Read-only: it logs in to nothing and changes nothing. If LAN mode is off, tell
    the user to switch it on at Settings > Network > LAN Mode (not possible while a print is running)."""
    if host:
        h = lan.check_host(host)
        return {"printers": [lan.describe_info(h, lan.fetch_info(h))]}
    return lan.find(subnet)


@mcp.tool(title="Printer status", annotations=_ann("Printer status", read_only=True, open_world=True))
@threaded
@_lan
def printer_status(
    host: PrinterHost = None,
    wait_seconds: Annotated[float, Field(description="How long to collect the printer's reports, 3 to 30 seconds.")] = 8,
) -> dict:
    """Read a printer's live state over its LAN mode: what it's doing, the current job, temperatures, errors and
    loaded filament.

    Use it to triage a print that paused, failed or looks wrong, before diagnose_print, or to check progress.
    Returns state (printing, paused, idle...), job (file, progress, layer, minutes left), nozzle/bed temperatures and
    targets, fans, lights, filament in the external spool and each ACE or built-in unit slot (material, colour,
    remaining), and errors with Anycubic's meaning and a wiki link. Read-only: it only sends status queries. Needs
    LAN mode on the printer; remembers the printer's address (no passwords) for next time."""
    _bounded("wait_seconds", wait_seconds, 3, 30)
    h = lan.resolve_host(host)
    out = lan.status(h, wait_seconds)
    lan.remember_host(h)
    return out


@mcp.tool(title="Printer camera snapshot", annotations=_ann("Printer camera snapshot", open_world=True))
@threaded
@_lan
def printer_camera_snapshot(
    host: PrinterHost = None,
    output_dir: Annotated[str | None, Field(description="Folder for the JPEG. Default: Documents/Anycubic Slicer MCP/printer.")] = None,
) -> list:
    """Take one photo with the printer's built-in camera and return it, so you can look at the print.

    Use it with printer_status when triaging: look for spaghetti, lifted corners, blobs, a detached part or an empty
    bed. Starts the camera stream, grabs one frame with ffmpeg (needs ffmpeg installed), stops the stream and turns
    the camera light back off if it was off. Takes up to about 20 seconds; writes printer-<time>.jpg to output_dir.
    Doesn't affect the print. Needs LAN mode, and the slicer's camera view closed (the printer serves one viewer)."""
    h = lan.resolve_host(host)
    folder = Path(output_dir).expanduser() if output_dir else _default_out() / "printer"
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / time.strftime("printer-%Y%m%d-%H%M%S.jpg")
    meta = lan.snapshot(h, out)
    lan.remember_host(h)
    return [Image(data=out.read_bytes(), format="jpeg"), json.dumps(meta)]


@mcp.tool(title="Pause, resume or stop a print",
          annotations=_ann("Pause, resume or stop a print", destructive=True, idempotent=False, open_world=True))
@threaded
@_lan
def control_print(
    action: Annotated[str, Field(description="'pause', 'resume' or 'stop'. Stop ends the print for good.")],
    confirm: Annotated[bool, Field(description="False (default) only previews what would happen. Set True only after the user has said yes in this conversation.")] = False,
    host: PrinterHost = None,
) -> dict:
    """Pause, resume or stop the print running on the printer, after the user confirms.

    Call it first without confirm: it checks the printer's state and returns what would happen (or why it can't, e.g.
    nothing is printing). Tell the user, wait for an explicit yes, then call again with confirm=True. It sends the
    command over LAN mode and waits up to 20 seconds for the printer to report the new state. Stop can't be undone;
    never stop or resume a print on your own initiative. Resume after a filament pause only once the user says the
    filament is fixed."""
    h = lan.resolve_host(host)
    return lan.control(h, action.strip().lower(), bool(confirm))


@mcp.tool(title="Send a print to the printer",
          annotations=_ann("Send a print to the printer", destructive=True, idempotent=False, open_world=True))
@threaded
@_lan
def send_print(
    file: Annotated[str, Field(description="A sliced .gcode.3mf on this computer, e.g. the gcode_3mf path slice_model returned.")],
    confirm: Annotated[bool, Field(description="False (default) only previews. Set True only after the user has said yes to this print in this conversation.")] = False,
    slots: Annotated[list[int] | None, Field(description="Colour-unit slot (1-4) for each filament in the file, in order, e.g. [4]. Omit to pick by material and nearest colour.")] = None,
    host: PrinterHost = None,
) -> dict:
    """Upload a sliced .gcode.3mf to the printer over LAN mode and start printing it, after the user confirms.

    Call it first without confirm: it reads the file (printer model, time, weight, filaments), checks the printer is
    idle and is the model the file was sliced for, and maps each filament to a loaded slot of the colour unit,
    refusing a material mismatch. Show the user that preview, ask them to make sure the bed is empty, and only on an
    explicit yes call again with confirm=True. Then it uploads the file (up to a few minutes for big files), sends the
    start command and waits for the printer to report that it's heating or printing. Starting a print heats the nozzle
    and bed and moves the head; never send one on your own initiative. Supports one colour unit (the Kobra X's own
    4-slot unit or a single ACE)."""
    h = lan.resolve_host(host)
    p = Path(file).expanduser().resolve()
    if p.is_file():
        from .slicer import scan_3mf
        scan_3mf(p)
    return lan.send_job(h, p, slots, bool(confirm))


# ======================================================================== slicer window
_LAST_SHOT: dict[str, Any] = {}
_LAST_CLOUD_PREVIEW: dict[str, Any] = {}


def _remember(meta: dict, items: list) -> None:
    _LAST_SHOT.update(size=tuple(meta["image_size"]), items=items)


def _window_shot(extra: dict | None = None) -> list:
    meta, items = cloud.capture(INSTALL)
    _remember(meta, items)
    meta = {**meta, "text": win.compact(items), **(extra or {})}
    meta.pop("image", None)
    return [Image(data=cloud.SHOT.read_bytes(), format="png"), json.dumps(meta)]


@mcp.tool(title="Restart the slicer window", annotations=_ann("Restart the slicer window", destructive=True, idempotent=False))
@threaded
def restart_slicer(
    open_path: Annotated[str | None, Field(description="A model, .3mf or .gcode.3mf to open when it starts. Omit to just restart.")] = None,
    printer: Annotated[str | None, Field(description="Printer preset to have selected, e.g. 'Kobra X'. Needed to choose a process or filaments.")] = None,
    process: Process = None,
    filaments: Filaments = None,
    colours: Annotated[list[str] | None, Field(description="'#RRGGBB' per filament for the window's colour swatches.")] = None,
) -> dict:
    """Close Anycubic Slicer Next the normal way and open it again, optionally with presets selected and a file open.

    Use it after saving presets (the window only reads them at start-up) or to open a model with exact presets picked,
    so the user can slice and send from the window with your settings. It never force-quits: if the window is asking
    whether to save the current project, nothing changes and you get told to ask the user to answer that dialog.
    Selecting presets edits the slicer's settings file while it is closed (a backup is kept next to it)."""
    LIB.reload()
    path = None
    if open_path:
        path = checked_file(open_path, MODEL_SUFFIXES | gc.GCODE_SUFFIXES | {".gcode.3mf"})
        if path.suffix.lower() == ".3mf":
            from .slicer import scan_3mf
            scan_3mf(path)
    chosen = None
    if printer or process or filaments:
        if not printer:
            raise ValueError("Give printer= too, so the process and filaments are selected for the right printer.")
        machine = _printer(printer)
        chosen = {"printer": machine,
                  "process": LIB.get("process", process, machine).name if process else None,
                  "filaments": [LIB.get("filament", f, machine).name for f in filaments] if filaments else None}
    q = win.quit_slicer(INSTALL)
    if not q["quit"]:
        return {"restarted": False, **q}
    backup = win.select_presets(INSTALL, chosen["printer"], chosen["process"], chosen["filaments"], colours) if chosen else None
    win.launch(INSTALL, path)
    LIB.reload()
    return {"restarted": True, "opened": path.name if path else None, "selected": chosen,
            "settings_backup": str(backup) if backup else None,
            "note": "It takes about 15 seconds to load; slicer_screenshot shows what it looks like."}


@mcp.tool(title="Screenshot the slicer window", annotations=_ann("Screenshot the slicer window", read_only=True))
@threaded
def slicer_screenshot() -> list:
    """Bring the Anycubic Slicer Next window to the front and return a picture of just that window, plus its text.

    Use it to see what the user sees: which presets are selected, the sliced time and weight, a dialog that's waiting
    for an answer. The text list gives each line read from the window as [text, x, y] in the image's pixels, which is
    what slicer_click takes. macOS only for now; needs Screen Recording permission for the app running this server."""
    return _window_shot()


@mcp.tool(title="Click in the slicer window", annotations=_ann("Click in the slicer window", destructive=True, idempotent=False))
@threaded
def slicer_click(
    text: Annotated[str | None, Field(description="Click the button or label with this text, e.g. 'Preview' or 'Cancel'. Preferred over x/y.")] = None,
    occurrence: Annotated[int | None, Field(description="Which match to click when the text appears more than once, 1 = first in reading order.")] = None,
    x: Annotated[float | None, Field(description="Or: X in pixels of the latest slicer_screenshot image.")] = None,
    y: Annotated[float | None, Field(description="Or: Y in pixels of the latest slicer_screenshot image.")] = None,
    double: Annotated[bool, Field(description="Double-click instead of a single click.")] = False,
    key: Annotated[str | None, Field(description="Instead of clicking, press one key: return, escape, tab, space or delete.")] = None,
    confirm: Annotated[bool, Field(description="Needed to click a button that prints, sends, pauses, stops, deletes or discards (e.g. 'Start Print', 'End', 'No' in a save dialog). Set True only after the user said yes to that exact click in this conversation.")] = False,
) -> list:
    """Click a button by its text (or at a point, or press one key) in the Anycubic Slicer Next window, then return a
    fresh screenshot and text of it.

    Take slicer_screenshot first. Clicks only land inside the slicer's window, only when it's the frontmost window,
    and never in its title bar. Buttons that start, send, pause, stop, delete or discard something are refused unless
    confirm=True, which needs the user's explicit yes to that click. macOS only; needs Accessibility permission for the
    app running this server, which is the user's decision."""
    if key:
        k = key.strip().lower()
        if k in ("return", "enter", "delete") and not confirm:
            raise ValueError(f"'{k}' can answer a dialog or delete a selection, so it needs confirm=True after the user's yes.")
        done = win.press(INSTALL, k)
        return [*_window_shot({"done": done})]
    if "size" not in _LAST_SHOT:
        raise ValueError("Take slicer_screenshot first, then click on something in it.")
    items = _LAST_SHOT["items"]
    if text:
        hit = win.find_text(items, text, occurrence)
        px, py = hit["point"]
    elif x is not None and y is not None:
        px, py = x, y
    else:
        raise ValueError("Give text, or both x and y, or key.")
    guarded = win.guarded_at(items, px, py)
    if guarded and not confirm:
        raise ValueError(f"That's the '{guarded}' button, which starts, sends, stops or discards something. Ask the user; "
                         "click it with confirm=True only after they say yes to exactly that.")
    done = win.click(INSTALL, px, py, _LAST_SHOT["size"], bool(double))
    return [*_window_shot({"done": done})]


@mcp.tool(title="Cloud printer status (through the slicer window)",
          annotations=_ann("Cloud printer status (through the slicer window)", open_world=True))
@threaded
def cloud_printer_status() -> list:
    """Read a printer that's on Anycubic Cloud (LAN mode off) from the slicer window's Workbench screen.

    Use it instead of printer_status when the printer's LAN mode is off, or when printer_status can't reach it. It
    switches the window to Workbench > Cloud Printer and reads it: printer and state (busy, idle, offline), job name,
    status and progress, layer, time left and total, estimated finish, nozzle and bed temperatures and targets, part
    fan, speed mode, firmware and the loaded slots. Returns the screenshot too: read values off it when a field is
    missing. Changes nothing on the printer; it only switches the window's tab. macOS only, needs the slicer open and
    signed in, plus Screen Recording and Accessibility permission for the app running this server."""
    meta, items, data = cloud.workbench(INSTALL)
    _remember(meta, items)
    return [Image(data=cloud.SHOT.read_bytes(), format="png"), json.dumps(data)]


@mcp.tool(title="Print through the slicer window (Anycubic Cloud)",
          annotations=_ann("Print through the slicer window (Anycubic Cloud)", destructive=True, idempotent=False,
                           open_world=True))
@threaded
def print_from_slicer(
    confirm: Annotated[bool, Field(description="False (default) opens and reads the Start Print dialog. Set True only after the user has said yes to this print in this conversation.")] = False,
) -> list:
    """Start the plate that's open in the slicer window on a cloud printer, through the window's own Start Print
    dialog, after the user confirms.

    Use it when the printer's LAN mode is off (send_print needs LAN mode). First open the file with the right presets
    (restart_slicer with open_path and presets, or open_in_slicer) and slice it in the window. Then call this without
    confirm: it presses Remote Print and reads the dialog (task name, time, weight, filaments, printer and its state,
    whether Start Print is enabled) and leaves the dialog open for the user to see. Show the user, ask them to check
    the bed is empty, and only on an explicit yes call again with confirm=True: it checks the dialog still shows the
    same task and time, presses Start Print, and waits for the upload to finish (up to 2 minutes). The slicer does the
    upload through the user's Anycubic account; this server never sees its login. Starting a print heats the nozzle
    and bed and moves the head; never do it on your own initiative. Filament-to-slot mapping is whatever the dialog
    shows: check it in the screenshot, and change it with slicer_click before confirming if it's wrong."""
    meta, items, out = cloud.print_from_slicer(INSTALL, bool(confirm), _LAST_CLOUD_PREVIEW or None)
    if not confirm:
        _LAST_CLOUD_PREVIEW.clear()
        _LAST_CLOUD_PREVIEW.update(task=out["dialog"].get("task"), time=out["dialog"].get("time"))
    else:
        _LAST_CLOUD_PREVIEW.clear()
    _remember(meta, items)
    out.setdefault("text", win.compact(items))
    return [Image(data=cloud.SHOT.read_bytes(), format="png"), json.dumps(out)]


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


@mcp.prompt()
def check_my_printer(problem: str = "") -> str:
    """Look at the printer live and work out what's wrong with a print."""
    return (f"{problem + ' ' if problem else ''}Call printer_status (use find_printers first if you don't know the printer's "
            "address). Tell me its state, the job's progress, temperatures against their targets, which filament is "
            "loaded where, and explain any error codes in plain words. Then take a printer_camera_snapshot and describe "
            "what you see on the bed. If something is wrong, match it to diagnose_print's issues and suggest fixes for "
            "now and for the next slice. If the print should be paused, resumed or stopped, say why and ask me first; "
            "use control_print only after I say yes.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
