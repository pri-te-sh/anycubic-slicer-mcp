"""Sending a print (against a fake printer) and the slicer-window helpers that don't need a screen."""
import json
import zipfile

import pytest

from anycubic_slicer_mcp import printer as lan
from anycubic_slicer_mcp import window as win
from test_printer import FakeLink, T


def _job_3mf(tmp_path, filaments=(("PETG", "#EFF0F1"),), model="Anycubic Kobra X", gcode=True, plates=1):
    p = tmp_path / "part.gcode.3mf"
    fil = "".join(f'<filament id="{i + 1}" type="{t}" color="{c}" used_m="70" used_g="210"/>'
                  for i, (t, c) in enumerate(filaments))
    plate = (f'<plate><metadata key="index" value="1"/><metadata key="printer_model_id" value="{model}"/>'
             f'<metadata key="prediction" value="32151"/><metadata key="weight" value="209.88"/>{fil}</plate>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Metadata/slice_info.config", f"<config>{plate * plates}</config>")
        if gcode:
            z.writestr("Metadata/plate_1.gcode", "G1 X0\n")
    return p


def test_job_file_is_read_and_checked(tmp_path):
    job = lan.read_job_file(_job_3mf(tmp_path))
    assert job["printer_model"] == "Anycubic Kobra X" and job["time"] == "8h 55m" and job["weight_g"] == 209.88
    assert job["filaments"] == [{"index": 0, "type": "PETG", "colour": "#EFF0F1", "used_g": 210.0}]
    with pytest.raises(lan.PrinterError, match="no G-code"):
        lan.read_job_file(_job_3mf(tmp_path, gcode=False))
    with pytest.raises(lan.PrinterError, match="2 plates"):
        lan.read_job_file(_job_3mf(tmp_path, plates=2))


BOX = [{"box": -1, "slots": [{"slot": 1, "material": "PLA", "colour": "#658946", "state": "ready"},
                             {"slot": 2, "material": "PLA", "colour": "#003594", "state": "ready"},
                             {"slot": 3, "material": "", "state": "empty"},
                             {"slot": 4, "material": "PETG", "colour": "#EFF0F1", "state": "ready"}]}]


def test_slots_are_matched_by_material_then_colour():
    m, notes = lan.plan_slots([{"index": 0, "type": "PETG", "colour": "#F2F2F2"}], BOX, None)
    assert m[0]["slot"] == 4 and not notes
    m, _ = lan.plan_slots([{"index": 0, "type": "PLA", "colour": "#003594"}], BOX, None)
    assert m[0]["slot"] == 2  # nearest colour among the PLA slots
    with pytest.raises(lan.PrinterError, match="No loaded slot has ABS"):
        lan.plan_slots([{"index": 0, "type": "ABS", "colour": "#000000"}], BOX, None)
    with pytest.raises(lan.PrinterError, match="has PLA"):
        lan.plan_slots([{"index": 0, "type": "PETG", "colour": "#EFF0F1"}], BOX, [1])  # wrong material by hand
    with pytest.raises(lan.PrinterError, match="empty"):
        lan.plan_slots([{"index": 0, "type": "PETG", "colour": "#EFF0F1"}], BOX, [3])
    with pytest.raises(lan.PrinterError, match="exactly one"):
        lan.plan_slots([{"index": 0, "type": "PLA", "colour": "#000000"}], BOX * 2, None)


def test_upload_url_is_pinned_and_start_payload_matches_the_protocol():
    assert lan._upload_url("192.168.1.84", {"fileUploadurl": "http://203.0.113.9:18910/gcode_upload?s=abc"}) == \
        "http://192.168.1.84:18910/gcode_upload?s=abc"
    assert lan._upload_url("192.168.1.84", {}) is None
    m, _ = lan.plan_slots([{"index": 0, "type": "PETG", "colour": "#EFF0F1"}], BOX, None)
    p = lan._start_payload("part.gcode.3mf", m, {1: "#EFF0F1"})
    assert p["taskid"] == "-1" and p["filepath"] == "/" and p["filename"] == "part.gcode.3mf"  # "/name" ends in 10111
    assert p["ams_settings"]["ams_box_mapping"] == [{"paint_index": 0, "ams_index": 3, "paint_color": [239, 240, 241],
                                                     "ams_color": [239, 240, 241], "material_type": "PETG"}]


class SendLink(FakeLink):
    """An idle (or busy) Kobra X that answers print/start with the given states."""

    def __init__(self, busy=False, start_states=("preheating", "printing"), code=200):
        msgs = [
            (T + "/info/report", {"type": "info", "data": {
                "state": "busy" if busy else "free", "project": {"taskid": "1"} if busy else None,
                "urls": {"fileUploadurl": "http://192.168.1.84:18910/gcode_upload?s=tok"}}}),
            (T + "/multiColorBox/report", {"type": "multiColorBox", "data": {"multi_color_box": [
                {"id": -1, "loaded_slot": -1, "slots": [{"index": 3, "type": "PETG", "color": [239, 240, 241], "status": 5}]}]}}),
        ]
        super().__init__(msgs)
        self.start_states, self.code = start_states, code

    def send(self, type_, action, data=None, role="web"):
        self.sent.append((type_, action, data, role))
        if (type_, action) == ("print", "start"):
            for st in self.start_states:
                self.messages.append((T + "/print/report", {"type": "print", "action": "start", "state": st, "code": self.code}))


def test_send_previews_then_uploads_and_starts(tmp_path, monkeypatch):
    link = SendLink()
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: link)
    uploads = []
    monkeypatch.setattr(lan, "upload_job", lambda url, path, timeout=300.0: uploads.append(url) or path.name)
    f = _job_3mf(tmp_path)
    pre = lan.send_job("192.168.1.84", f, None, confirm=False)
    assert pre["started"] is False and pre["confirm_required"] and pre["slots"][0]["slot"] == 4
    assert not uploads and not any(s[1] == "start" for s in link.sent)
    done = lan.send_job("192.168.1.84", f, None, confirm=True)
    assert done["started"] is True and uploads == ["http://192.168.1.84:18910/gcode_upload?s=tok"]
    assert [s[3] for s in link.sent if s[1] == "start"] == ["web"]


def test_send_refuses_a_busy_printer_and_the_wrong_model(tmp_path, monkeypatch):
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: SendLink(busy=True))
    with pytest.raises(lan.PrinterError, match="busy"):
        lan.send_job("192.168.1.84", _job_3mf(tmp_path), None, confirm=True)
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: SendLink())
    with pytest.raises(lan.PrinterError, match="sliced for Anycubic Kobra S1"):
        lan.send_job("192.168.1.84", _job_3mf(tmp_path, model="Anycubic Kobra S1"), None, confirm=True)


def test_send_retries_on_the_slicer_topic_only_when_the_message_was_invalid(tmp_path, monkeypatch):
    link = SendLink(start_states=("failed",), code=10110)
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: link)
    monkeypatch.setattr(lan, "upload_job", lambda url, path, timeout=300.0: path.name)
    out = lan.send_job("192.168.1.84", _job_3mf(tmp_path), None, confirm=True)
    assert out["started"] is False and [s[3] for s in link.sent if s[1] == "start"] == ["web", "slicer"]


def test_select_presets_rewrites_only_the_selection(fake_install, monkeypatch):
    monkeypatch.setattr(win, "running", lambda install: False)
    conf = fake_install.config_file
    before = json.loads(conf.read_text())
    backup = win.select_presets(fake_install, "Test Printer 0.4 nozzle", "0.20mm Standard @Test",
                                ["Vendor PETG @Test"], ["#eff0f1"])
    after = json.loads(conf.read_text())
    first = after["anycubic_presets"][0]
    assert first["machine"] == "Test Printer 0.4 nozzle" and first["process"] == "0.20mm Standard @Test"
    assert first["filament"] == "Vendor PETG @Test" and first["filament_colors"] == "#EFF0F1"
    assert after["app"] == before["app"]  # nothing else touched
    assert json.loads(backup.read_text()) == before and conf.read_text().endswith("}\n")


def test_select_presets_refuses_while_the_slicer_is_open(fake_install, monkeypatch):
    monkeypatch.setattr(win, "running", lambda install: True)
    with pytest.raises(win.WindowError, match="restart_slicer"):
        win.select_presets(fake_install, "Test Printer 0.4 nozzle", None, None, None)
