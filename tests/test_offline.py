"""Tests that need no slicer install: presets, settings safety, geometry, G-code, planning, doctor, materials."""
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from anycubic_slicer_mcp import analysis, calibration, doctor, gcode as gc, install, materials, optimize
from anycubic_slicer_mcp.inventory import Inventory
from anycubic_slicer_mcp.mesh import euler_xyz, load_mesh, rotation, rotation_to_down
from anycubic_slicer_mcp.presets import PresetError, flatten_for_cli
from anycubic_slicer_mcp.settings import SettingError, SettingRouter, format_value
from anycubic_slicer_mcp.slicer import Slicer, checked_file, scan_3mf
from conftest import PRINTER

# ------------------------------------------------------------------ install paths

def test_macos_defaults(monkeypatch):
    monkeypatch.setattr(install.sys, "platform", "darwin")
    assert install.default_executable() == Path("/Applications/AnycubicSlicerNext.app/Contents/MacOS/AnycubicSlicerNext")
    assert install.default_data_dir().parts[-3:] == ("Library", "Application Support", "AnycubicSlicerNext")
    i = install.Install(install.default_executable(), install.default_data_dir(), "macos")
    assert i.bundled_profiles == Path("/Applications/AnycubicSlicerNext.app/Contents/Resources/profiles")
    assert i.settings_names_file == i.executable
    assert i.app_bundle == Path("/Applications/AnycubicSlicerNext.app")


def test_windows_defaults(monkeypatch):
    monkeypatch.setattr(install.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "/appdata")
    monkeypatch.setenv("ProgramFiles", "/pf")
    exe = install.default_executable()
    assert exe == Path("/pf") / "AnycubicSlicerNext" / "AnycubicSlicerNext.exe"
    assert install.default_data_dir() == Path("/appdata") / "AnycubicSlicerNext"
    i = install.Install(exe, install.default_data_dir(), "windows")
    assert i.settings_names_file == exe.parent / "AnycubicSlicer.dll"
    assert i.bundled_profiles == exe.parent / "resources" / "profiles"


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYCUBIC_SLICER_EXE", str(tmp_path / "x"))
    monkeypatch.setenv("ANYCUBIC_SLICER_DATA", str(tmp_path / "d"))
    i = install.find_install()
    assert i.executable == tmp_path / "x" and i.data_dir == tmp_path / "d"

# ------------------------------------------------------------------ presets

def test_inheritance_and_selection(lib):
    proc = lib.resolve("process", "0.20mm Standard @Test")
    assert proc["outer_wall_speed"] == "120" and proc["wall_loops"] == "2"  # own value wins, parent fills in
    sel = lib.selection()
    assert sel["machine"] == PRINTER and sel["filaments"] == ["Vendor PLA @Test"]
    assert [r["name"] for r in lib.listing("process", PRINTER)] == ["0.20mm Standard @Test"]  # hidden base excluded


def test_fuzzy_lookup(lib):
    assert lib.get("filament", "PETG", PRINTER).name == "Vendor PETG @Test"
    assert lib.get("filament", "Vendor PLA", PRINTER).name == "Vendor PLA @Test"
    with pytest.raises(PresetError):
        lib.get("filament", "nylon")


def test_user_preset_overrides_system(lib, fake_install):
    user = fake_install.data_dir / "user" / "123" / "filament" / "Vendor PLA @Test.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"name": "Vendor PLA @Test", "inherits": "Vendor PLA @Test", "nozzle_temperature": ["215"]}))
    lib.reload()
    f = lib.resolve("filament", "Vendor PLA @Test")
    assert f["nozzle_temperature"] == ["215"] and f["filament_type"] == ["PLA"]


def test_flatten():
    flat = flatten_for_cli({"name": "x", "inherits": "y", "instantiation": "false", "a": "1"})
    assert flat["inherits"] == "" and flat["instantiation"] == "true" and flat["a"] == "1"

# ------------------------------------------------------------------ settings safety

def test_format_value():
    assert format_value(True, "") == "1"
    assert format_value(3.0, "") == "3"
    assert format_value(215, ["210"]) == ["215"]
    assert format_value(1.2, ["0.8", "0.8"]) == ["1.2", "1.2"]
    with pytest.raises(SettingError):
        format_value("a\nb", "")


def test_router_routes_and_blocks(lib):
    cfg = {"machine": lib.resolve("machine", PRINTER), "process": lib.resolve("process", "0.20mm Standard @Test"),
           "filament": [lib.resolve("filament", "Vendor PLA @Test")]}
    placed = SettingRouter(lib).apply(cfg, {"wall_loops": 4, "nozzle_temperature": 220, "z_hop": 0.2})
    assert placed == {"wall_loops": "process", "nozzle_temperature": "filament", "z_hop": "machine"}
    assert cfg["filament"][0]["nozzle_temperature_HS"] == ["220"]  # per-nozzle copy follows
    for bad in ({"post_process": "rm"}, {"machine_start_gcode": "G28"}):
        with pytest.raises(SettingError):
            SettingRouter(lib).apply(cfg, bad)
    with pytest.raises(SettingError, match="Unknown"):
        SettingRouter(lib).apply(cfg, {"wal_loops": 3})


def test_gcode_templates_opt_in(lib, monkeypatch):
    monkeypatch.setenv("ANYCUBIC_MCP_ALLOW_GCODE_TEMPLATES", "1")
    SettingRouter(lib).check_key("machine_start_gcode")

# ------------------------------------------------------------------ input files

def _3mf(tmp_path, entries):
    p = tmp_path / "x.3mf"
    with zipfile.ZipFile(p, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return p


def test_scan_3mf(tmp_path):
    scan_3mf(_3mf(tmp_path, {"Metadata/project_settings.config": b'{"post_process": []}'}))
    for evil in (b'{"post_process": ["calc.exe"]}', b'<metadata key="post_process" value="x.bat"/>',
                 b'{"post\\u005fprocess": ["x"]}', b"; post_process = /bin/sh"):
        with pytest.raises(ValueError):
            scan_3mf(_3mf(tmp_path, {"Metadata/project_settings.config": evil}))


def test_scan_3mf_accepts_the_slicers_own_gcode_metadata(tmp_path):
    # every .gcode.3mf the slicer writes has an empty "; post_process =" line followed by more settings
    meta = b"; ooze_prevention = 0\n; post_process = \n; precise_outer_wall = 0\n"
    scan_3mf(_3mf(tmp_path, {"Metadata/plate_1.gcode.metadata": meta}))
    with pytest.raises(ValueError):
        scan_3mf(_3mf(tmp_path, {"Metadata/plate_1.gcode.metadata": b"; post_process = /bin/sh\n; x = 0\n"}))


def test_user_presets_are_written_so_the_window_lists_them(lib, fake_install):
    (fake_install.data_dir / "user" / "default").mkdir(parents=True)
    data = {"name": "My PETG", "inherits": "Vendor PETG @Test", "from": "User", "instantiation": "true",
            "nozzle_temperature": ["250"], "default_filament_colour": ["#EFF0F1"]}
    paths = lib.write_user_preset("filament", data)
    assert {p.parent.parent.name for p in paths} == {"123", "default"}  # signed in and signed out
    for p in paths:
        saved = json.loads(p.read_text())
        assert saved["version"] and saved["is_custom_defined"] == "0"
        assert saved["nozzle_temperature_HS"] == ["250"]  # the per-nozzle copy the slicer actually uses
        info = p.with_suffix(".info").read_text()
        assert "base_id = Vendor PETG @Test" in info
        assert ("user_id = 123" in info) == (p.parent.parent.name == "123")
    with pytest.raises(FileExistsError):
        lib.write_user_preset("filament", data)
    with pytest.raises(PresetError):
        lib.write_user_preset("filament", {**data, "name": "x", "inherits": "My PETG"})  # parent must be system


def test_older_user_presets_still_reach_the_per_nozzle_settings(lib, fake_install):
    folder = fake_install.data_dir / "user" / "123" / "filament"
    folder.mkdir(parents=True)
    (folder / "Old PETG.json").write_text(json.dumps({
        "name": "Old PETG", "inherits": "Vendor PETG @Test", "from": "User", "instantiation": "true",
        "nozzle_temperature": ["235"], "default_filament_colour": ["#112233"]}))
    lib.reload()
    flat = flatten_for_cli(lib.resolve("filament", "Old PETG"))
    assert flat["nozzle_temperature"] == flat["nozzle_temperature_HS"] == ["235"]
    assert flat["filament_colour"] == ["#112233"]
    assert lib.system_root("filament", "Old PETG").name == "Vendor PETG @Test"
    assert lib.resolve("filament", "Vendor PETG @Test")["nozzle_temperature_HS"] == ["240"]  # system untouched


def test_checked_file(tmp_path):
    f = tmp_path / "a.stl"; f.write_text("solid")
    assert checked_file(str(f), {".stl"}) == f.resolve()
    with pytest.raises(ValueError):
        checked_file(str(f), {".gcode"})
    with pytest.raises(FileNotFoundError):
        checked_file(str(tmp_path / "missing.stl"), {".stl"})
    with pytest.raises(ValueError, match="Network"):
        checked_file(r"\\server\share\a.stl", {".stl"})

# ------------------------------------------------------------------ geometry

def test_box_geometry_and_stl_roundtrip(tmp_path):
    b = calibration.box(0, 0, 0, 20, 10, 5)
    assert b.volume() == pytest.approx(1000)
    assert b.edge_report() == {"open_edges": 0, "overshared_edges": 0}
    p = b.save_stl(tmp_path / "b.stl")
    again = load_mesh(p)
    assert again.volume() == pytest.approx(1000, rel=1e-4) and again.shells() == 1


def test_ascii_stl_and_obj(tmp_path):
    (tmp_path / "t.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 3 2\nf 1 2 4\nf 1 4 3\nf 2 3 4\n")
    assert load_mesh(tmp_path / "t.obj").volume() == pytest.approx(1 / 6)


def test_overhang_and_rotation():
    # an upside-down "T": its top bar overhangs the stem
    t = calibration.combine([calibration.box(-2, -2, 0, 4, 4, 10), calibration.box(-10, -2, 10, 20, 4, 2)])
    assert t.overhangs(45)["overhang_area_mm2"] > 50
    flipped = t.transformed(rotation(x_deg=180)).on_bed()
    assert flipped.overhangs(45)["overhang_area_mm2"] < t.overhangs(45)["overhang_area_mm2"]
    assert euler_xyz(rotation(10, 20, 30)) == {"x": 10.0, "y": 20.0, "z": 30.0}
    assert np.allclose(rotation_to_down(np.array([0, 1.0, 0])) @ [0, 1, 0], [0, 0, -1])


def test_neck_ratio_and_orientation():
    assert analysis.neck_ratio([400, 400, 60, 400, 400]) == pytest.approx(0.15)
    assert analysis.neck_ratio([2000, 2000, 200, 200]) == 1.0  # a small top feature isn't a waist
    plate = calibration.box(0, 0, 0, 60, 60, 4).transformed(rotation(x_deg=90)).on_bed()  # stood on edge
    best = analysis.rank_orientations(plate, "balanced", (250, 250, 250))[0]
    assert best["height_mm"] == pytest.approx(4, abs=0.01)


def test_fit_report():
    big = calibration.box(0, 0, 0, 300, 20, 10)
    r = analysis.fit_report(big, (250, 250, 250))
    assert not r["fits"] and any("45°" in s or "%" in s for s in r["suggestions"])
    assert analysis.fit_report(calibration.box(0, 0, 0, 100, 100, 100), (250, 250, 250))["fits"]
    assert analysis.bed_size({"printable_area": ["0x0", "260x0", "260x260", "0x260"], "printable_height": "260"}) == (260, 260, 260)


def test_strength_plan():
    plan = analysis.strength_plan(calibration.box(0, 0, 0, 40, 10, 10), "compression", None, "max_strength", "PLA")
    assert plan["overrides"]["sparse_infill_pattern"] == "cubic" and plan["overrides"]["wall_loops"] >= 6
    with pytest.raises(ValueError):
        analysis.strength_plan(calibration.box(0, 0, 0, 1, 1, 1), "squish")

# ------------------------------------------------------------------ G-code

GCODE = """; HEADER
G90
M83
;LAYER_CHANGE
;Z:0.2
M73 P0 R10
;TYPE:Outer wall
G1 X0 Y0 F600
G1 X10 Y0 E1
G1 X10 Y10 E1
;TYPE:Sparse infill
G1 X0 Y10 E1
T1
;LAYER_CHANGE
;Z:0.4
M73 P10 R9
;TYPE:Inner wall
G1 X0 Y0 E1
G2 X10 Y0 I5 J0 E1
T0
; filament used [mm] = 10.0, 5.0
; filament used [g] = 1.5, 0.5
; total filament used [g] = 2.0
; total layers count = 2
; estimated printing time (normal mode) = 1m 2s
; layer_height = 0.2
; wall_loops = "2"
"""


def test_summary_and_toolpaths(tmp_path):
    p = tmp_path / "x.gcode"; p.write_text(GCODE)
    s = gc.summarize(p)
    assert s["time_normal_s"] == 62 and s["filament_g"] == [1.5, 0.5] and s["total_filament_g"] == 2.0
    assert s["layers"] == 2 and s["tool_changes"] == 1 and s["first_layer_time"] == "1m 0s"
    assert s["settings"]["wall_loops"] == "2"
    tp = gc.read_toolpaths(p)
    assert tp.count == 2 and tp.layer_z == [0.2, 0.4]
    lengths = tp.feature_lengths()
    assert lengths["Outer wall"] == pytest.approx(0.02) and "Inner wall" in lengths


def test_inject_and_3mf_repack(tmp_path):
    src = tmp_path / "in.gcode"; src.write_text(GCODE)
    dst = tmp_path / "plate_1.gcode"
    assert gc.inject_at_heights(src, dst, [(0.2, "M104 S220"), (0.4, "M104 S215")]) == 2
    text = dst.read_text()
    assert text.index("M104 S220") < text.index("M104 S215")
    three = tmp_path / "x.gcode.3mf"
    with zipfile.ZipFile(three, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "old"); z.writestr("Metadata/plate_1.gcode.md5", "x")
    calibration.replace_gcode_in_3mf(three, dst)
    with zipfile.ZipFile(three) as z:
        assert b"M104 S215" in z.read("Metadata/plate_1.gcode")


def test_temperature_tower():
    tower, cmds = calibration.temperature_tower([220, 215, 210])
    assert [c[1] for c in cmds] == ["M104 S220", "M104 S215", "M104 S210"]
    assert tower.size()[2] == pytest.approx(1.2 + 3 * 8)
    with pytest.raises(ValueError):
        calibration.temperature_tower([200])

# ------------------------------------------------------------------ doctor

def _cfg(lib, filament="Vendor PETG @Test"):
    return {"machine": lib.resolve("machine", PRINTER), "process": lib.resolve("process", "0.20mm Standard @Test"),
            "filament": lib.resolve("filament", filament)}


def test_doctor_directions_and_conflicts(lib):
    r = doctor.diagnose(["stringing", "warping", "layer_adhesion"], _cfg(lib))
    o = r["overrides"]
    assert o["filament_retraction_length"] > 0.8          # machine retraction used when filament says nil
    assert o["brim_width"] > 5 and o["close_fan_the_first_x_layers"] > 1
    assert "nozzle_temperature" not in o and r["conflicts"]  # stringing (down) vs adhesion (up)
    assert {i["issue"] for i in doctor.catalog()} == set(doctor.ISSUES)
    with pytest.raises(ValueError):
        doctor.diagnose(["gremlins"], _cfg(lib))


def test_doctor_overrides_are_real_settings(lib):
    keys = set().union(*(lib.known_keys(k) for k in ("machine", "process", "filament")))
    for issue in ("stringing", "layer_adhesion", "warping"):
        for s in doctor.suggestions(issue, _cfg(lib), "moderate"):
            assert s["setting"] in keys, s["setting"]

# ------------------------------------------------------------------ materials, inventory, cost

def test_material_names():
    assert materials.normalize_material("eSUN PLA+") == "PLA+"
    assert materials.normalize_material("Nylon") == "PA"
    with pytest.raises(ValueError):
        materials.normalize_material("cheese")


def test_label_to_profile(lib):
    info = materials.LabelInfo(material="PETG", brand="eSUN", colour_name="Blue", colour_hex="1e6fd9",
                               nozzle_min=230, nozzle_max=250, bed_min=70, bed_max=80, weight_g=1000, price=25)
    built = materials.build_profile(lib, PRINTER, info)
    p = built["profile"]
    assert built["base"] == "Vendor PETG @Test" and p["inherits"] == "Vendor PETG @Test"
    assert p["nozzle_temperature"] == ["240"] and p["nozzle_temperature_HS"] == ["240"]
    assert p["textured_plate_temp"] == ["75"] and p["cool_plate_temp"] == ["0"]  # cool plate unsuitable for PETG
    assert p["default_filament_colour"] == ["#1E6FD9"] and p["filament_cost"] == ["25.00"]
    path = materials.save_profile(lib, p)
    assert path.exists() and lib.get("filament", "eSUN PETG Blue", PRINTER).source == "user"
    with pytest.raises(FileExistsError):
        materials.save_profile(lib, p)
    with pytest.raises(ValueError):
        materials.build_profile(lib, PRINTER, materials.LabelInfo(material="PLA", nozzle_min=900))


def test_inventory(tmp_path):
    inv = Inventory(tmp_path / "s.json")
    s = inv.add("PLA", 1000)
    inv.update(s["id"], use_g=300)
    assert inv.list(need_g=600)["enough_for_print"] == [s["id"]]
    assert inv.list(need_g=700)["enough_for_print"] == []
    inv.update(s["id"], remove=True)
    assert inv.list()["count"] == 0
    with pytest.raises(KeyError):
        inv.update("nope", use_g=1)


def test_cost():
    c = optimize.estimate_cost(100, 3600, 20, power_w=200, kwh_price=0.25)
    assert c["material"] == 2.0 and c["energy"] == 0.05 and c["total"] == 2.05

# ------------------------------------------------------------------ slicer plumbing (no binary needed)

def test_choose_and_configs(lib, fake_install):
    s = Slicer(fake_install, lib)
    chosen = s.choose(None, None, None)
    assert chosen == {"printer": PRINTER, "process": "0.20mm Standard @Test", "filaments": ["Vendor PLA @Test"], "notes": []}
    cfg, placed = s.configs(chosen, {"wall_loops": 5}, bed_type="cool plate", colours=["#00ff00"])
    assert cfg["process"]["wall_loops"] == "5" and cfg["process"]["curr_bed_type"] == "Cool Plate"
    assert cfg["filament"][0]["default_filament_colour"] == ["#00FF00"]
    assert cfg["process"]["compatible_printers"] == [PRINTER]
    with pytest.raises(ValueError):
        s.configs(chosen, None, bed_type="lava plate")


def test_strength_reason_matches_wall_count():
    from anycubic_slicer_mcp import analysis, calibration
    plan = analysis.strength_plan(calibration.box(0, 0, 0, 40, 10, 4), load="bending", load_axis=None,
                                  priority="balanced", material="PLA", nozzle_mm=0.4, bed=(250, 250, 250))
    walls = plan["overrides"]["wall_loops"]
    assert plan["why"][0].startswith(f"{walls} walls")


def test_every_tool_is_documented():
    """Directories score tool definitions; keep every tool annotated and every parameter described."""
    import asyncio
    from anycubic_slicer_mcp import server
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 25
    for t in tools:
        assert t.annotations and t.annotations.title, t.name
        printer_tool = t.name in {"find_printers", "printer_status", "printer_camera_snapshot", "control_print"}
        assert t.annotations.openWorldHint is printer_tool, t.name
        assert t.description and len(t.description) > 80, t.name
        for key, prop in t.inputSchema.get("properties", {}).items():
            assert prop.get("description"), f"{t.name}.{key}"
