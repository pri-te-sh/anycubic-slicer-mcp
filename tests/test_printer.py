"""Printer LAN-mode client, tested against a fake printer (no network, no MQTT broker)."""
import base64
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from anycubic_slicer_mcp import printer as lan

TOKEN = "0123456789abcdefFEDCBA9876543210"
CREDS = {"broker": "mqtts://192.168.1.84:9883", "username": "u", "password": "p", "deviceId": "d" * 32,
         "modelId": "20030", "devicecrt": "CERT", "devicepk": "KEY"}


def _encrypt(obj: dict, token: str, iv: str) -> str:
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(obj).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(iv.encode()[:16].ljust(16, b"\0"))).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def _fake_printer(ctrl_type="lan", ctrl_code=200, ctrl_url="http://192.168.1.84:18910/ctrl", seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/info":
            return httpx.Response(200, text=json.dumps({"ctrlType": ctrl_type, "token": TOKEN, "modelId": 20030,
                                                         "ctrlInfoUrl": ctrl_url, "modelName": "Kobra X",
                                                         "cn": "SERIAL1234"}))
        if request.url.path == "/ctrl":
            iv = "abcdefghijklmnop"
            return httpx.Response(200, json={"code": ctrl_code, "message": "x",
                                             "data": {"info": _encrypt(CREDS, TOKEN, iv), "token": iv}})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sign_matches_documented_formula():
    inner = hashlib.md5(TOKEN[:16].encode()).hexdigest()
    assert lan.sign(TOKEN, 1700000000000, "aB3dE9") == hashlib.md5(f"{inner}1700000000000aB3dE9".encode()).hexdigest()


def test_handshake_decrypts_credentials_and_posts_to_the_dialled_host():
    seen = []
    c = lan.handshake("192.168.1.84", _fake_printer(ctrl_url="http://203.0.113.9:18910/ctrl", seen=seen))
    assert (c.username, c.password, c.port, c.model_id, c.model, c.device_id) == ("u", "p", 9883, "20030", "Kobra X", "d" * 32)
    post = [r for r in seen if r.method == "POST"][0]
    assert post.url.host == "192.168.1.84"  # never follows an address the printer's reply points at
    params = dict(post.url.params)
    assert params["sign"] == lan.sign(TOKEN, int(params["ts"]), params["nonce"])
    assert len(params["did"]) == 32 and len(params["nonce"]) == 6


def test_handshake_explains_lan_mode_off_and_client_limit():
    with pytest.raises(lan.PrinterError, match="LAN Mode"):
        lan.handshake("192.168.1.84", _fake_printer(ctrl_type="cloud"))
    with pytest.raises(lan.PrinterError, match="too many LAN clients"):
        lan.handshake("192.168.1.84", _fake_printer(ctrl_code=19002))


def test_only_local_addresses_are_accepted(monkeypatch):
    monkeypatch.delenv("ANYCUBIC_MCP_ALLOW_PUBLIC_PRINTER", raising=False)
    assert lan.check_host("192.168.1.84") == "192.168.1.84"
    assert lan.check_host("10.0.0.5") == "10.0.0.5"
    for bad in ("8.8.8.8", "127.0.0.1", "http://192.168.1.84", "192.168.1.84:80", ""):
        with pytest.raises(lan.PrinterError):
            lan.check_host(bad)
    monkeypatch.setenv("ANYCUBIC_MCP_ALLOW_PUBLIC_PRINTER", "1")
    assert lan.check_host("127.0.0.1") == "127.0.0.1"


def test_subnet_search_is_limited():
    with pytest.raises(lan.PrinterError):
        lan.find("8.8.8.0/24")
    with pytest.raises(lan.PrinterError):
        lan.find("10.0.0.0/16")


T = "anycubic/anycubicCloud/v1/printer/public/20030/" + "d" * 32


def _paused_printer_messages():
    return [
        (T + "/info/report", {"type": "info", "action": "query", "data": {
            "version": "2.0.2.2", "printerName": "Workshop",
            "project": {"filename": "base_plate.gcode", "progress": 72, "curr_layer": 48, "total_layers": 277,
                        "remain_time": 122, "taskid": "T42", "pause": 1},
            "temp": {"curr_nozzle_temp": 142, "target_nozzle_temp": 140}}}),
        (T + "/tempature/report", {"type": "tempature", "action": "auto",
                                    "data": {"curr_hotbed_temp": 75, "target_hotbed_temp": 75}}),
        (T + "/print/report", {"type": "print", "action": "pause", "state": "paused", "code": 11518,
                                "msg": "clog", "data": {"taskid": "T42"}}),
        (T + "/multiColorBox/report", {"type": "multiColorBox", "action": "getInfo", "data": {"multi_color_box": [
            {"id": 0, "loaded_slot": 1, "temp": 24, "humidity": 0,
             "slots": [{"index": 0, "type": "PLA", "color": [255, 255, 255], "status": 5, "consumables_percent": 80},
                       {"index": 1, "type": "PLA", "color": [0, 0, 0], "status": 5}]}]}}),
        # partial update for the same box merges instead of replacing
        (T + "/multiColorBox/report", {"type": "multiColorBox", "action": "autoUpdateInfo", "data": {"multi_color_box": [
            {"id": 0, "slots": [{"index": 2, "type": "", "edit_status": 2}]}]}}),
        (T + "/light/report", {"type": "light", "action": "query", "data": {"lights": [{"type": 3, "status": 1}]}}),
        (T + "/peripherie/report", {"type": "peripherie", "data": {"camera": 1, "udisk": 0}}),
        ("anycubic/x/" + "d" * 32 + "/response", {"type": "", "data": None}),  # noise is dropped
    ]


def test_state_merges_partial_reports():
    st = lan.State("20030")
    for t, m in _paused_printer_messages():
        st.feed(t, m)
    s = st.summary("192.168.1.84", "Kobra X")
    assert s["state"] == "paused"
    assert s["job"]["layer"] == 48 and s["job"]["remaining_min"] == 122 and s["job"]["task_id"] == "T42"
    assert s["temperatures"] == {"nozzle": {"current_c": 142, "target_c": 140}, "bed": {"current_c": 75, "target_c": 75}}
    assert s["errors"][0]["code"] == 11518 and "clog" in s["errors"][0]["meaning"].lower()
    assert s["errors"][0]["wiki"].endswith("/11518-code/kx")
    box = s["filament"]["boxes"][0]
    assert box["kind"] == "built-in unit" and box["loaded_slot"] == 2 and "humidity_pct" not in box
    assert [x["state"] for x in box["slots"]] == ["loaded", "loaded", "empty"]
    assert box["slots"][1]["colour"] == "#000000" and box["slots"][0]["remaining_pct"] == 80
    assert s["lights"] == {"camera": "on"} and s["camera_present"] is True and s["usb_drive"] is False
    assert s["printer"]["firmware"] == "2.0.2.2"


def test_idle_printer_and_unknown_codes():
    st = lan.State("20030")
    st.feed(T + "/info/report", {"type": "info", "data": {"project": None}})
    st.feed(T + "/print/report", {"type": "print", "state": "finished", "code": 19999, "msg": "new thing"})
    s = st.summary("h", "Kobra X")
    assert s["state"].startswith("idle") and "job" not in s
    assert s["errors"] == [{"code": 19999, "meaning": "new thing", "wiki": "https://wiki.anycubic.com/en/error-codes/19999-code/kx"}]


class FakeLink:
    """Stands in for the MQTT session: replays a paused printer and answers commands."""

    def __init__(self, msgs):
        self.messages = list(msgs)
        self.sent = []
        self.c = lan.Credentials("192.168.1.84", 9883, "u", "p", "d" * 32, "20030", "Kobra X", None, None, {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def send(self, type_, action, data=None):
        self.sent.append((type_, action, data))
        if type_ == "print" and action in ("pause", "resume", "stop"):
            state = {"pause": "paused", "resume": "printing", "stop": "stoped"}[action]
            pause = {"pause": 1, "resume": 0, "stop": 4}[action]
            self.messages.append((T + "/print/report", {"type": "print", "action": action, "state": state,
                                                         "code": 10601 if action == "stop" else 200,
                                                         "data": {"taskid": "T42", "pause": pause}}))

    def wait_for(self, pred, seconds):
        return pred(self.messages)


def test_control_previews_until_confirmed_then_resumes(monkeypatch):
    link = FakeLink(_paused_printer_messages())
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: link)
    preview = lan.control("192.168.1.84", "resume", confirm=False)
    assert preview["done"] is False and preview["confirm_required"] and preview["state"] == "paused"
    assert not any(s[0] == "print" and s[1] == "resume" for s in link.sent)
    done = lan.control("192.168.1.84", "resume", confirm=True)
    assert done["done"] is True and ("print", "resume", {"taskid": "T42"}) in link.sent


def test_control_refuses_what_makes_no_sense(monkeypatch):
    link = FakeLink(_paused_printer_messages())
    monkeypatch.setattr(lan, "open_link", lambda host, timeout=8.0: link)
    out = lan.control("192.168.1.84", "pause", confirm=True)  # already paused
    assert out["done"] is False and "nothing to pause" in out["reason"]
    assert not any(s[0] == "print" and s[1] != "query" for s in link.sent)
    with pytest.raises(lan.PrinterError):
        lan.control("192.168.1.84", "explode", confirm=True)


def test_camera_url_is_pinned_to_the_printer():
    assert lan._stream_url("192.168.1.84", {"rtspUrl": "http://192.168.1.84:18088/live/abc"}) == "http://192.168.1.84:18088/live/abc"
    assert lan._stream_url("192.168.1.84", {"rtspUrl": "http://203.0.113.9:18088/live/abc"}) == "http://192.168.1.84:18088/live/abc"
    assert lan._stream_url("192.168.1.84", {}) is None


def test_printer_host_is_remembered_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANYCUBIC_MCP_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ANYCUBIC_PRINTER_HOST", raising=False)
    with pytest.raises(lan.PrinterError, match="find_printers"):
        lan.resolve_host(None)
    lan.remember_host("192.168.1.84")
    assert json.loads((tmp_path / "printer.json").read_text()) == {"host": "192.168.1.84"}
    assert lan.resolve_host(None) == "192.168.1.84"
