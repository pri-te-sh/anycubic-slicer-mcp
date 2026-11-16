import json
from pathlib import Path

import pytest

from anycubic_slicer_mcp.install import Install
from anycubic_slicer_mcp.presets import PresetLibrary

PRINTER = "Test Printer 0.4 nozzle"


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def fake_install(tmp_path) -> Install:
    """A miniature Anycubic Slicer Next data folder: one printer, an inheriting process and two filaments."""
    data = tmp_path / "data"
    sysdir = data / "system" / "Vendor"
    _write(sysdir / "machine" / f"{PRINTER}.json", {
        "name": PRINTER, "inherits": "", "instantiation": "true", "from": "system",
        "printable_area": ["0x0", "250x0", "250x250", "0x250"], "printable_height": "260",
        "nozzle_diameter": ["0.4"], "retraction_length": ["0.8"], "z_hop": ["0.4"], "wipe": ["0"],
        "default_print_profile": "0.20mm Standard @Test", "default_filament_profile": ["Vendor PLA @Test"],
    })
    _write(sysdir / "process" / "fdm_process_common.json", {
        "name": "fdm_process_common", "instantiation": "false", "wall_loops": "2", "layer_height": "0.2",
        "sparse_infill_density": "15%", "sparse_infill_pattern": "grid", "top_shell_layers": "4",
        "bottom_shell_layers": "3", "travel_speed": "250", "outer_wall_speed": "150", "brim_width": "5",
        "brim_type": "auto_brim", "enable_support": "0", "initial_layer_speed": "50", "post_process": [],
        "flush_into_infill": "0",
    })
    _write(sysdir / "process" / "0.20mm Standard @Test.json", {
        "name": "0.20mm Standard @Test", "inherits": "fdm_process_common", "instantiation": "true",
        "compatible_printers": [PRINTER], "outer_wall_speed": "120",
    })
    for name, ftype, temps in (("Vendor PLA @Test", "PLA", ("210", "190", "230")),
                               ("Vendor PETG @Test", "PETG", ("240", "220", "260"))):
        _write(sysdir / "filament" / f"{name}.json", {
            "name": name, "inherits": "", "instantiation": "true", "compatible_printers": [PRINTER],
            "filament_type": [ftype], "nozzle_temperature": [temps[0]], "nozzle_temperature_HS": [temps[0]],
            "nozzle_temperature_initial_layer": [temps[0]], "nozzle_temperature_range_low": [temps[1]],
            "nozzle_temperature_range_high": [temps[2]], "textured_plate_temp": ["60"],
            "textured_plate_temp_initial_layer": ["60"], "cool_plate_temp": ["35"], "cool_plate_temp_initial_layer": ["35"],
            "filament_retraction_length": ["nil"], "filament_flow_ratio": ["0.98"], "fan_max_speed": ["100"],
            "close_fan_the_first_x_layers": ["1"], "filament_density": ["1.24"], "filament_cost": ["20"],
            "filament_max_volumetric_speed": ["15"],
        })
    _write(data / "AnycubicSlicerNext.conf", {
        "app": {"preset_folder": "123", "version": "2.0.0.3"},
        "anycubic_presets": [{"machine": PRINTER, "process": "0.20mm Standard @Test", "filament": "Vendor PLA @Test",
                              "filament_colors": "#FF0000", "curr_bed_type": "3"}],
    })
    exe = tmp_path / "no-slicer" / "AnycubicSlicerNext"
    return Install(executable=exe, data_dir=data, os_name="macos")


@pytest.fixture
def lib(fake_install) -> PresetLibrary:
    return PresetLibrary(fake_install)
