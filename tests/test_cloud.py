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

# A ready printer and a short task name, read with the slicer in full screen and Preview's line list beside the dialog.
DIALOG_SHORT = [
    L("Start Print", 763, 105, 77, 12), L("all", 1093, 121, 19), L("Printing task: first_layer_test /", 684, 147, 233, 19),
    L("surface 2m50s", 1093, 144, 100), L("for Anycubic Kobra X", 544, 190, 133), L("3m 12s", 563, 297, 47, 12),
    L("0))) 1.0g/0.35m", 540, 328, 93, 16), L("PETG", 572, 372, 40, 16), L("ation", 1095, 377, 40),
    L("Color mapping mismatch may cause bleeding. Edit mapping or sync from ACE Pro.", 528, 428, 516, 16),
    L("Please select a printer •", 528, 451, 182, 20), L("ing time: 3m11s", 1095, 479, 105, 18),
    L("- Anycubic Kobra X", 537, 486, 133, 22), L("Bed leveling", 558, 557, 79, 16), L("Time-lapse", 558, 592, 75, 15),
    L("Cancel", 888, 862, 44, 14), L("Start Print", 986, 861, 68, 16),
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


def test_title_bar_is_refused_unless_the_window_is_full_screen(monkeypatch):
    """A windowed slicer has the macOS title bar in its top 30 pt; in full screen that bar is hidden and the
    slicer's own tabs and Remote Print button sit there instead. Nothing real is clicked: Quartz is a fake."""
    window = {"id": 1, "owner": "AnycubicSlicerNext", "pid": 1, "title": "", "x": 0.0, "y": 33.0, "w": 1512.0, "h": 949.0}
    posted = []

    class FakeQuartz:
        kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGMouseButtonLeft = 1, 2, 0
        kCGMouseEventClickState, kCGHIDEventTap = 0, 0

        @staticmethod
        def CGEventCreateMouseEvent(_src, kind, point, _button):
            return (kind, point)

        @staticmethod
        def CGEventSetIntegerValueField(*_args):
            pass

        @staticmethod
        def CGEventPost(_tap, event):
            posted.append(event)

    monkeypatch.setattr(win, "_accessibility_ok", lambda: True)
    monkeypatch.setattr(win, "slicer_window", lambda install, front=True: window)
    monkeypatch.setattr(win, "_windows_front_to_back", lambda: [window])
    monkeypatch.setattr(win, "_quartz", lambda: FakeQuartz)
    monkeypatch.setattr(win.time, "sleep", lambda _s: None)
    workbench_tab = (894, 23)  # where full-screen Anycubic Slicer Next 2.0.0.3 draws its Workbench tab

    monkeypatch.setattr(win, "_full_screen", lambda w: False)
    with pytest.raises(win.WindowError, match="title bar"):
        win.click(None, *workbench_tab, (1600, 1004))
    assert not posted

    monkeypatch.setattr(win, "_full_screen", lambda w: True)
    out = win.click(None, *workbench_tab, (1600, 1004))
    assert posted and out["clicked"] == [845, 55]


def test_dialog_with_a_short_task_name_is_read_whole():
    """The task label is centred with the name, so a short name moves it right; the dialog's edges come from its
    title and Start Print button instead, and the line list beside the dialog stays out."""
    d = cloud.parse_dialog(DIALOG_SHORT)
    assert d["task"] == "first_layer_test" and d["time"] == "3m 12s" and d["sliced_for"] == "Anycubic Kobra X"
    assert (d["weight_g"], d["length_m"]) == (1.0, 0.35) and d["filaments"] == ["PETG"]
    assert d["warnings"] and "mismatch" in d["warnings"][0] and not d["no_printers"]
    assert d["start_button"]["box"][1] == 861


def test_camera_panel_button_and_picture_are_found(tmp_path):
    """Panel bounds from the Workbench's real label positions; the play/stop button and a dark frame from a drawn
    image. No window is involved."""
    from PIL import Image, ImageDraw
    items = [L("Camera", 265, 406, 63, 17), L("Material Management", 807, 405, 184, 22)]
    panel = cloud.camera_panel(items)
    assert panel == [247, 387, 530, 398]
    assert cloud.camera_panel([L("Camera", 265, 406)]) is None
    assert cloud.camera_panel([L("Camera", 267, 409, 60, 14), L("Control", 810, 101, 60, 18)])[2] == 531
    x, y, w, h = panel
    shot = tmp_path / "w.png"
    im = Image.new("RGB", (1600, 1004), (30, 30, 30))
    d = ImageDraw.Draw(im)
    d.rectangle((x, y, x + w, y + h), fill=(0, 0, 0))
    d.ellipse((x + 18, y + h - 48, x + 48, y + h - 18), fill=(40, 110, 240))  # blue play button
    im.save(shot)
    kind, point = cloud._stream_button(shot, panel)
    assert kind == "play" and abs(point[0] - (x + 33)) <= 2 and abs(point[1] - (y + h - 33)) <= 2
    assert cloud._frame_is_dark(shot, panel)
    d.rectangle((x + 60, y + 40, x + w - 20, y + h - 60), fill=(150, 140, 120))  # a picture of the bed
    d.ellipse((x + 18, y + h - 48, x + 48, y + h - 18), fill=(235, 50, 50))  # red stop button
    im.save(shot)
    assert cloud._stream_button(shot, panel)[0] == "stop" and not cloud._frame_is_dark(shot, panel)


@pytest.mark.parametrize("line, status", [
    ("Print Prepare: File Downloading... (0%)", "print prepare: file downloading... (0%)"),
    ("Auto-leveling", "auto-leveling"), ("Nozzle purging", "nozzle purging"), ("Printing...", "printing"),
])
def test_job_stages_of_a_cloud_start_are_named(line, status):
    """The stages a Kobra X went through after print_from_slicer, as the Workbench showed them."""
    d = cloud.parse_workbench([L("Print task", 265, 105, 80), L(line, 265, 281, 200), L("0%", 263, 318, 20)])
    assert d["job_status"] == status and d["progress_percent"] == 0


def test_print_finished_card_is_reported():
    """After a cloud print the slicer shows a Print Finished card over the Workbench, with a rating to submit."""
    items = [L("Anycubic Kobra X", 52, 140, 102), L("Busy", 178, 140, 26), L("Print Finished", 665, 416, 110),
             L("first_layer_test", 715, 476, 90), L("The print task is completed", 738, 527, 150),
             L("Print Quality", 624, 588, 72), L("Submit", 992, 588, 44)]
    n = cloud.parse_workbench(items)["notice"]
    assert n["title"] == "Print Finished" and "The print task is completed" in n["lines"]
