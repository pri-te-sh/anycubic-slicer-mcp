"""Reading the slicer window's Workbench and Start Print dialog, and the click guards. The OCR lines are real ones
read from Anycubic Slicer Next 2.0.0.3 during a cloud print (serial number left out)."""
import pytest

from anycubic_slicer_mcp import cloud
from anycubic_slicer_mcp import window as win


def L(text, x, y, w=60, h=14):
    return {"text": text, "conf": 1.0, "box": [x, y, w, h]}


WORKBENCH = [
    L("A Home", 428, 46, 72, 17), L("Prepare", 577, 46), L("Preview", 721, 46), L("Workbench", 846, 43, 91, 20),
    L("Cloud Printer", 44, 121, 76), L("LAN Printer", 156, 121, 64),
    L("Anycubic Kobra X", 52, 170, 102), L("Busy", 178, 170, 26),
    L("Print task", 263, 137, 81, 16), L("1116-1907-01_static_base_and_stator_plate(01)_P...", 426, 199, 333, 16),
    L("Layers", 426, 234, 42), L("23/277", 426, 255, 47), L("Remaining", 500, 234, 66, 16),
    L("5h36m/6h2m", 500, 255, 88), L("Est. Completion", 614, 234, 95), L("11-17 05:24", 614, 255, 79),
    L("Printing...", 263, 309, 61, 15), L("37%", 263, 348, 33, 16), L("a Skip | 1l Pause | • End", 581, 348, 179, 16),
    L("Control", 807, 129, 65, 20), L("• Nozzle", 826, 179, 65), L("241°C /240°C", 833, 202, 81),
    L("Hotbed", 940, 179, 70), L("80°C /80°C", 949, 201, 68, 15), L("$ Model ...", 823, 250, 79, 16),
    L("20%", 833, 274, 33), L("Speed ...", 939, 249, 80, 18), L("Standard", 949, 273, 68),
    L("Material Management", 807, 436, 184, 19), L("Rack", 856, 475, 33),
    L("PLA", 840, 589, 26, 12), L("PLA", 926, 589, 26, 12), L("PLA", 1012, 589, 23, 12), L("PETG", 1093, 589, 30, 12),
    L("Device Info", 1202, 132, 93), L("Anycubic Kobra X", 1337, 181, 117, 19), L("Busy", 1342, 206, 30),
    L("Model", 1337, 230, 42), L("Anycubic Kobra X", 1402, 230, 105), L("Firmware V 2.0.2.8", 1337, 276, 121, 12),
    L("• Nozzle", 1402, 329, 63), L("Hardened steel ›", 1418, 350, 126, 18),
]

DIALOG = [
    L("Start Print", 764, 54, 71, 15), L("Printing task: 1116-2355-01_static_base_and_stator_plate(...", 579, 99, 402, 16),
    L("for Anycubic Kobra X", 545, 141, 128, 13), L("8h 55m 51s", 565, 248, 67, 13), L("209.9g/70.94m", 565, 280, 87, 13),
    L("PETG", 575, 325, 35, 14), L("Please select a printer", 529, 389, 156, 16), L("Select Printer", 546, 425, 92, 15),
    L("Disconnected", 827, 425, 86, 15), L("No available printers found.", 529, 457, 186, 16),
    L("Cancel", 887, 814, 43, 15), L("Start Print", 985, 814, 65, 15),
    L("Inner wall", 1162, 151, 61), L("PLA", 1500, 589, 26),  # outside the dialog
]


def test_workbench_is_read():
    d = cloud.parse_workbench(WORKBENCH)
    assert d["printer_state"] == "busy" and d["printer"] == "Anycubic Kobra X"
    assert d["job"].startswith("1116-1907-01_static_base") and d["job_status"] == "printing"
    assert d["progress_percent"] == 37 and (d["layer"], d["layers"]) == (23, 277)
    assert (d["remaining"], d["total_time"], d["estimated_completion"]) == ("5h36m", "6h2m", "11-17 05:24")
    assert d["nozzle"] == {"now_c": 241, "target_c": 240} and d["bed"] == {"now_c": 80, "target_c": 80}
    assert d["part_fan_percent"] == 20 and d["speed_mode"] == "Standard" and d["firmware"] == "V 2.0.2.8"
    assert [s["material"] for s in d["slots"]] == ["PLA", "PLA", "PLA", "PETG"] and d["slots"][3]["slot"] == 4


def test_start_print_dialog_is_read():
    d = cloud.parse_dialog(DIALOG)
    assert d["open"] and d["task"].startswith("1116-2355-01") and d["sliced_for"] == "Anycubic Kobra X"
    assert d["time"] == "8h 55m 51s" and (d["weight_g"], d["length_m"]) == (209.9, 70.94)
    assert d["filaments"] == ["PETG"] and d["no_printers"] and d["printer_status"] == "disconnected"
    assert d["start_button"]["box"][1] == 814  # the button, not the dialog's title
    assert not cloud.parse_dialog(WORKBENCH)["open"]


def test_guarded_buttons_are_found_in_a_row_and_alone():
    for word in ("Skip", "Pause", "End"):
        hit = win.find_text(WORKBENCH, word)
        assert win.guarded_at(WORKBENCH, *hit["point"]) == word.lower()
    assert win.guarded_at(WORKBENCH, *win.find_text(WORKBENCH, "Workbench")["point"]) is None
    assert win.guarded_at(DIALOG, *win.find_text(DIALOG, "Start Print", occurrence=2)["point"]) == "start print"
    assert win.guarded_at(DIALOG, *win.find_text(DIALOG, "Cancel")["point"]) is None
    assert win.guarded_label("No") == "no" and win.guarded_label("Remote Print") is None


def test_find_text_needs_an_occurrence_when_ambiguous():
    with pytest.raises(win.WindowError, match="appears 2 times"):
        win.find_text(DIALOG, "Start Print")
    with pytest.raises(win.WindowError, match="No text matching"):
        win.find_text(DIALOG, "Send")


def _fake_window(monkeypatch, items, clicks):
    monkeypatch.setattr(cloud, "capture", lambda install, out=cloud.SHOT: ({"image_size": [1600, 909]}, items))
    monkeypatch.setattr(cloud, "button_enabled", lambda image, box: True)
    monkeypatch.setattr(win, "click", lambda install, x, y, size, double=False: clicks.append((x, y)))
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)


def test_print_from_slicer_previews_and_refuses_without_a_printer(monkeypatch):
    clicks = []
    _fake_window(monkeypatch, DIALOG, clicks)
    _, _, out = cloud.print_from_slicer(None, False, None)
    assert out["started"] is False and out["confirm_required"] and out["dialog"]["time"] == "8h 55m 51s"
    assert any("No printer can take the job" in p for p in out["problems"]) and not clicks
    with pytest.raises(win.WindowError, match="Not starting"):
        cloud.print_from_slicer(None, True, {"task": out["dialog"]["task"], "time": out["dialog"]["time"]})
    assert not clicks


def test_print_from_slicer_confirms_only_the_previewed_dialog(monkeypatch):
    ready = [i for i in DIALOG if i["text"] not in ("Disconnected", "No available printers found.")] + \
            [L("Anycubic Kobra X", 546, 425, 120), L("Idle", 827, 425, 30)]
    clicks = []
    _fake_window(monkeypatch, ready, clicks)
    with pytest.raises(win.WindowError, match="without confirm first"):
        cloud.print_from_slicer(None, True, None)
    with pytest.raises(win.WindowError, match="not the previewed"):
        cloud.print_from_slicer(None, True, {"task": "something else", "time": "1h 0m 0s"})
    assert not clicks
    states = iter([ready, ready, WORKBENCH])  # dialog read, then it closes after the click
    monkeypatch.setattr(cloud, "capture", lambda install, out=cloud.SHOT: ({"image_size": [1600, 909]}, next(states)))
    d = cloud.parse_dialog(ready)
    reopened = "1116-2359-02_" + d["task"].split("_", 1)[1]  # same plate, dialog reopened a minute later
    _, _, out = cloud.print_from_slicer(None, True, {"task": reopened, "time": d["time"]})
    assert out["started"] is True and clicks == [(985 + 65 // 2, 814 + 15 // 2)]


def test_slicer_click_refuses_guarded_buttons_without_confirm(monkeypatch):
    from anycubic_slicer_mcp import server
    clicks = []
    monkeypatch.setattr(win, "click", lambda install, x, y, size, double=False: clicks.append((x, y)) or {"clicked": [x, y]})
    monkeypatch.setattr(server, "_window_shot", lambda extra=None: ["shot", extra])
    server._LAST_SHOT.update(size=(1600, 909), items=WORKBENCH)
    click = server.slicer_click.__wrapped__  # the plain function under the thread wrapper
    for kwargs in ({"text": "End"}, {"text": "Pause"}, {"x": 690, "y": 356}, {"key": "return"}):
        with pytest.raises(ValueError, match="confirm"):
            click(**kwargs)
    assert not clicks
    out = click(text="Workbench")
    assert clicks and out[1]["done"]["clicked"]
