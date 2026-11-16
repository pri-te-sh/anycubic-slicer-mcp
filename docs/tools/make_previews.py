"""Slice examples/gear.stl with the installed Anycubic Slicer Next and render the screenshots the site uses.

    uv run --python 3.13 --with-editable . python docs/tools/make_previews.py

Writes docs/assets/preview-*.png, layers.gif, print-card.png and docs/tools/facts.json (the real numbers
quoted on the site). Needs Anycubic Slicer Next installed and opened once.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from anycubic_slicer_mcp import analysis, gcode as gc, render
from anycubic_slicer_mcp.install import find_install
from anycubic_slicer_mcp.mesh import load_mesh
from anycubic_slicer_mcp.presets import PresetLibrary
from anycubic_slicer_mcp.slicer import SliceRequest, Slicer

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "docs" / "assets"
GEAR = ROOT / "examples" / "gear.stl"


def main() -> None:
    install = find_install()
    lib = PresetLibrary(install)
    slicer = Slicer(install, lib)
    work = Path(tempfile.mkdtemp(prefix="acmcp-art-"))
    try:
        r = slicer.slice(SliceRequest(models=[str(GEAR)], output_dir=str(work), output_name="gear",
                                      overrides={"sparse_infill_density": "20%", "sparse_infill_pattern": "gyroid",
                                                 "wall_loops": 3}))
        plate = r["plates"][0]
        path = Path(plate["gcode"])
        tp = gc.read_toolpaths(path)
        s = gc.summarize(path)

        def save(name: str, png: bytes) -> None:
            (ASSETS / name).write_bytes(png)

        mid = min(12, tp.count)
        save("preview-layer-12.png", render.top_view(tp, mid, 900, "gear")[0])
        save("preview-layer-1.png", render.top_view(tp, 1, 900, "gear")[0])
        save("preview-all-layers.png", render.top_view(tp, None, 900, "gear")[0])
        save("preview-side.png", render.side_view(tp, 900, "gear")[0])
        render.layer_animation(tp, ASSETS / "layers.gif", 520, 45, 110)
        st = s.get("settings", {})
        rows = [("Print time", s.get("time_normal") or "?"),
                ("Filament", f"{s['total_filament_g']:g} g {st.get('filament_type', '')}".strip()),
                ("Layers", f"{s.get('layers')} × {st.get('layer_height')} mm"),
                ("Infill", f"{st.get('sparse_infill_density')} {st.get('sparse_infill_pattern')}"),
                ("Walls", str(st.get("wall_loops"))),
                ("Printer", (st.get("printer_settings_id") or "?").replace("Anycubic ", ""))]
        render.print_card(render.top_view(tp, None, 800, "")[0], "gear", rows, ASSETS / "print-card.png")

        mesh = load_mesh(GEAR)
        bed = analysis.bed_size(lib.resolve("machine", plate["settings"].get("printer_settings_id")))
        ranked = analysis.rank_orientations(mesh, "balanced", bed)
        facts = {
            "printer": st.get("printer_settings_id"), "filament": st.get("filament_settings_id"),
            "time": s.get("time_normal"), "grams": s.get("total_filament_g"), "layers": s.get("layers"),
            "layer_height": st.get("layer_height"), "infill": st.get("sparse_infill_density"),
            "pattern": st.get("sparse_infill_pattern"), "walls": st.get("wall_loops"),
            "max_z": s.get("max_z_mm"), "best_orientation": ranked[0] if ranked else None,
        }
        (ROOT / "docs" / "tools" / "facts.json").write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
        print(json.dumps(facts, indent=2, default=str))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
