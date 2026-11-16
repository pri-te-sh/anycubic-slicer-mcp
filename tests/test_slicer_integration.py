"""Real slices against an installed Anycubic Slicer Next. Skipped when the slicer isn't installed (e.g. CI)."""
from pathlib import Path

import pytest

from anycubic_slicer_mcp import calibration, gcode as gc, optimize
from anycubic_slicer_mcp.install import find_install
from anycubic_slicer_mcp.presets import PresetLibrary
from anycubic_slicer_mcp.slicer import SliceRequest, Slicer

INSTALL = find_install()
pytestmark = pytest.mark.skipif(not (INSTALL.executable.is_file() and INSTALL.config_file.is_file()),
                                reason="Anycubic Slicer Next isn't installed / hasn't been opened")


@pytest.fixture(scope="module")
def slicer():
    return Slicer(INSTALL, PresetLibrary(INSTALL))


def test_slice_with_overrides(slicer, tmp_path):
    cube = calibration.box(0, 0, 0, 20, 20, 10)
    r = slicer.slice(SliceRequest(models=[], extra_meshes=[("cube", cube)], output_dir=str(tmp_path),
                                  overrides={"wall_loops": 3, "sparse_infill_density": "25%"}))
    p = r["plates"][0]
    assert r["ok"] and Path(p["gcode"]).exists() and Path(r["gcode_3mf"]).exists()
    assert p["layers"] >= 40 and p["total_filament_g"] > 0 and p["time_normal_s"] > 0
    assert p["settings"]["wall_loops"] == "3" and p["settings"]["sparse_infill_density"] == "25%"


def test_rotation_and_copies(slicer, tmp_path):
    bar = calibration.box(0, 0, 0, 40, 10, 4)
    r = slicer.slice(SliceRequest(models=[], extra_meshes=[("bar", bar)], output_dir=str(tmp_path),
                                  rotation={"x": 90}, copies=2))
    p = r["plates"][0]
    assert p["max_z_mm"] == pytest.approx(10, abs=0.3)  # stood up on its 10 mm side
    assert len(gc.read_toolpaths(p["gcode"]).layers[0]) > 0


def test_multicolour_waste_report(slicer, tmp_path):
    a = calibration.box(0, 0, 0, 15, 15, 6).save_stl(tmp_path / "a.stl")
    b = calibration.box(0, 0, 0, 15, 15, 6).save_stl(tmp_path / "b.stl")
    lib = slicer.library
    fil = lib.selection()["filaments"][:1] * 2 or None
    r = optimize.multicolor(slicer, SliceRequest(models=[str(a), str(b)], filaments=fil, object_filaments=[1, 2],
                                                 filament_colours=["#000000", "#FFFFFF"], output_dir=str(tmp_path)))
    assert r["as_sliced"]["tool_changes"] > 0
    assert r["colour_change_waste_g"] >= 0 and r["model_only_g"] > 0


def test_project_plan_and_slice(slicer, tmp_path, monkeypatch):
    from anycubic_slicer_mcp import project
    monkeypatch.setenv("ANYCUBIC_MCP_STATE_DIR", str(tmp_path / "state"))
    d = tmp_path / "kit"
    d.mkdir()
    calibration.box(0, 0, 0, 120, 80, 4).save_stl(d / "base.stl")
    calibration.box(0, 0, 0, 20, 10, 6).save_stl(d / "clip_x4.stl")
    plan = project.build_plan(slicer, str(d), None, {"answered": ["goal"]})
    assert not [q for q in plan["questions"] if q["blocking"]]
    report = project.slice_plan(slicer, plan["plan_id"], None, str(tmp_path / "out"))
    assert report["totals"]["plates"] == 1 and report["totals"]["filament_g"] > 0
    plate = report["plates"][0]
    assert {p["id"]: p["count"] for p in plate["parts"]} == {"base.stl": 1, "clip_x4.stl": 4}
    assert Path(plate["gcode_3mf"]).exists() and Path(plate["preview"]).exists()
