"""Project planning without a slicer: name hints, zip safety, packing, questions."""
import json
import zipfile
from pathlib import Path

import pytest

from anycubic_slicer_mcp import project
from anycubic_slicer_mcp.calibration import box, combine
from anycubic_slicer_mcp.slicer import Slicer


def test_quantity_from_name():
    assert project.qty_from_name("clip_x4") == 4
    assert project.qty_from_name("4x_foot") == 4
    assert project.qty_from_name("hinge qty2") == 2
    assert project.qty_from_name("spacer_6pcs") == 6
    assert project.qty_from_name("box_200x100") is None or project.qty_from_name("box_200x100") <= 200
    assert project.qty_from_name("bracket (1)") is None
    assert project.qty_from_name("mx_switch") is None


def test_version_and_colour_hints():
    assert project.version_key("arm_v3")[0] == project.version_key("arm_v2")[0] == "arm"
    assert project.version_key("arm_v3")[1] == 3
    assert project.version_key("lid (1)")[0] == "lid"
    assert project.version_key("arm_left")[0] != project.version_key("arm_right")[0]
    assert project.colour_from_name("knob_red") == "red"
    assert project.colour_from_name("[a]_skirt") == "accent"
    assert project.colour_from_name("panel_gray") == "grey"
    assert project.colour_from_name("bracket") is None


def test_quantity_from_readme():
    docs = {"README.md": "Corners: print 4x foot.stl\nOne base."}
    assert project.qty_from_docs("foot", docs)[0] == 4
    assert project.qty_from_docs("base", docs) is None


def test_zip_slip_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escape.stl", b"solid x")
    with pytest.raises(project.ProjectError):
        project.safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "escape.stl").exists()


def test_pack_respects_bed_and_goal():
    def part(pid, w, d, qty=1, colour=None):
        p = project.Part(id=pid, path=pid, name=pid, qty=qty, colour=colour)
        p.oriented_mm, p.est_min, p.est_g = [w, d, 5], 10, 3
        return p
    parts = [part("big.stl", 200, 200), part("plate.stl", 150, 120), part("clip.stl", 20, 10, qty=6, colour="red")]
    plates = project.pack(parts, (260, 260, 260), "fewest_plates", 6, None, False)
    assert len(plates) == 2 and sum(len(p["items"]) for p in plates) == 8
    by_colour = project.pack(parts, (260, 260, 260), "by_colour", 6, None, False)
    assert any(p["colour"] == "red" and set(p["items"]) == {"clip.stl"} for p in by_colour)
    capped = project.pack(parts, (260, 260, 260), "fewest_plates", 6, 25, False)
    assert all(p["est_min"] <= 25 + 3 or len(p["items"]) == 1 for p in capped)


def _project(tmp_path: Path) -> Path:
    d = tmp_path / "kit"
    (d / "parts").mkdir(parents=True)
    box(0, 0, 0, 60, 40, 4).save_stl(d / "base.stl")
    box(0, 0, 0, 30, 20, 5).save_stl(d / "lid_v1.stl")
    box(0, 0, 0, 31, 21, 5).save_stl(d / "lid_v2.stl")
    box(0, 0, 0, 10, 5, 3).save_stl(d / "parts" / "clip_x3.stl")
    box(0, 0, 0, 1.2, 0.6, 0.3).save_stl(d / "tiny.stl")
    box(0, 0, 0, 400, 20, 20).save_stl(d / "rail.stl")
    box(0, 0, 0, 20, 20, 2).save_stl(d / "tolerance_test.stl")
    return d


def test_plan_asks_before_slicing(tmp_path, fake_install, lib, monkeypatch):
    monkeypatch.setenv("ANYCUBIC_MCP_STATE_DIR", str(tmp_path / "state"))
    slicer = Slicer(fake_install, lib)
    plan = project.build_plan(slicer, str(_project(tmp_path)), None, {})
    ids = {q["id"]: q for q in plan["questions"]}
    assert ids["versions:lid"]["blocking"] and ids["versions:lid"]["default"] == "lid_v2.stl"
    assert ids["scale:tiny.stl"]["blocking"]
    assert ids["too_big:rail.stl"]["blocking"]
    assert "tests" in ids and not ids["tests"]["blocking"]
    clip = next(p for p in plan["parts"] if p["id"] == "parts/clip_x3.stl")
    assert clip["qty"] == 3
    with pytest.raises(project.ProjectError, match="need answers"):
        project.slice_plan(slicer, plan["plan_id"], None, str(tmp_path / "out"))
    again = project.build_plan(slicer, None, plan["plan_id"],
                               {"exclude": ["lid_v1.stl", "rail.stl"], "scales": {"tiny.stl": 25.4}})
    assert not [q for q in again["questions"] if q["blocking"]]
    tiny = next(p for p in again["parts"] if p["id"] == "tiny.stl")
    assert tiny["size_mm"][0] == pytest.approx(30.5, abs=0.2)
    placed = [i for pl in again["plates"] for i in pl["items"]]
    assert placed.count("parts/clip_x3.stl") == 3 and "rail.stl" not in placed and "tolerance_test.stl" not in placed
    saved = json.loads((tmp_path / "state" / "projects" / plan["plan_id"] / "plan.json").read_text())
    assert saved["inputs"]["exclude"] == ["lid_v1.stl", "rail.stl"]


def test_unknown_plan_id_is_rejected():
    with pytest.raises(project.ProjectError):
        project.load_plan("../../etc")
