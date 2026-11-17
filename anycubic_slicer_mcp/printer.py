"""Read an Anycubic printer's live state over its LAN mode (Kobra X, Kobra S1, Kobra 3 family).

How LAN mode works, as documented by the community (no official Anycubic documentation exists):
1. GET http://<printer>:18910/info returns the model, a 32-character token and ctrlType ("lan" when LAN mode is on).
2. POST /ctrl?ts&nonce&sign&did, signed with md5(md5(token[:16]) + ts + nonce), returns broker credentials
   encrypted with AES-128-CBC (key token[16:32], IV from the reply).
3. MQTT 3.1.1 over TLS on port 9883: reports arrive on anycubic/anycubicCloud/v1/printer/+/<model>/<device>/#,
   queries and commands go to anycubic/anycubicCloud/v1/web/printer/<model>/<device>/<type>.

Nothing here is kept on disk except the printer's address. Credentials stay in memory for this process only.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import shutil
import socket
import ssl
import string
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# httpx logs each request URL at INFO, and the MCP server logs at INFO. The handshake URL carries a one-time
# signature, so keep those lines out of the client's logs.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
from urllib.parse import urlparse

from .install import state_dir

INFO_PORT = 18910
TOPIC_ROOT = "anycubic/anycubicCloud/v1"
MODELS = {"20024": "Kobra 3", "20025": "Kobra S1", "20026": "Kobra 3 Max", "20027": "Kobra 3 V2",
          "20028": "Kobra 4", "20029": "Kobra S1 Max", "20030": "Kobra X"}
WIKI_SUFFIX = {"20030": "kx"}
STATUS_TYPES = ("info", "tempature", "fan", "light", "peripherie", "status", "print")
CRED_TTL = 600  # seconds; credentials rotate when the printer restarts, so re-handshake now and then

# Meanings from Anycubic's public error-code list (wiki.anycubic.com/en/error-codes). Unknown codes are reported raw.
ERRORS: dict[int, str] = {
    10101: "A print task is already running", 10102: "There is no print task", 10103: "The print task is paused",
    10104: "The print task has stopped", 10105: "File download to the printer failed",
    10106: "Not enough printer storage", 10107: "Filament runout detected", 10110: "Slicing file error; re-slice",
    10111: "Print task ended abnormally", 10113: "Downloaded file failed its checksum",
    10114: "The printer can't do that in its current state", 10115: "The printer can't parse the file",
    10116: "Abnormal slice file (check slicer settings)", 10118: "X-axis homing failed", 10119: "Y-axis homing failed",
    10120: "Z-axis homing failed", 10121: "Heated bed heating fault", 10122: "Hotend heating fault",
    10123: "Heated bed thermistor fault", 10124: "Hotend thermistor fault", 10125: "Print task was restored",
    10126: "X-axis vibration compensation check failed (belt tension)",
    10127: "Y-axis vibration compensation check failed (belt tension)",
    10128: "X-axis vibration compensation fault (accelerometer or wiring)",
    10130: "Z-axis homing failed (print head pressure sensor)", 10131: "Print head board fault (check cable)",
    10132: "Print head scraped (check the hotend is seated)", 10133: "File is missing required G-code commands",
    10134: "This feature needs the camera", 10135: "This feature needs a USB drive",
    10136: "This feature needs the camera and a USB drive", 10137: "USB drive has under 800 MB free for time-lapse",
    10237: "Auto-levelling failed",
    10401: "Paused by the user", 10402: "Filament runout; load new filament",
    10403: "Filament runout; load new filament into the ACE", 10408: "Print head or mainboard firmware too old",
    10409: "Board connection fault (material box, driver board or print head)",
    10411: "Driver or extruder overheating; check cooling", 10412: "Colour engine connection fault; restart",
    10413: "Colour engine connection fault; restart", 10414: "Colour engine connection fault; restart",
    10415: "Heated bed too hot (control fault); power off and inspect", 10536: "Colour engine error; restart",
    10537: "Nozzle size in the file doesn't match the printer", 10601: "Stopped by the user",
    10603: "Print interrupted (power loss)", 10607: "Task force-ended", 10608: "Task ended abnormally",
    10609: "Cancelled after power-loss recovery", 10801: "Firmware download failed", 10802: "Firmware update failed",
    10803: "Not enough storage for the update", 10804: "USB drive couldn't be read",
    10805: "Firmware verification failed", 11001: "Couldn't delete a local file", 11101: "Wi-Fi connection failed",
    11401: "Camera not connected", 11402: "Network connection timed out", 11403: "Camera fault",
    11404: "Video stream failed to start", 11407: "Device failed to start (print head cable)",
    11410: "Video streaming error", 11412: "Mainboard and driver board lost contact",
    11503: "Colour engine connection fault", 11504: "Unknown feed position", 11509: "ACE stopped responding",
    11510: "Filament colour can't be edited while printing", 11511: "Extrusion fault (filament not reaching the head)",
    11512: "Retraction fault", 11513: "Colour engine stopped responding", 11517: "No colour engine connected",
    11518: "Filament clog detected", 11519: "Filament tangle detected", 11520: "Colour engine is out of filament",
    11521: "Colour engine motor fault; power-cycle it", 11524: "Colour engine connection fault",
    11525: "Not enough filaments loaded for this model's colours", 11527: "Colour engine busy; can't update",
    11529: "Colour engine thermistor fault", 11530: "Colour engine heater fault (drying stopped)",
    11531: "Model has more than 8 colours", 11532: "Printer hasn't been auto-levelled", 11534: "Auto-levelling failed",
    11535: "Unknown filament in the cutter module", 11538: "Cutter signal fault",
    11801: "Spaghetti detected; print paused", 11802: "Foreign object on the bed", 11810: "Too dark for AI detection",
    11811: "Camera may be blocked", 11812: "Spaghetti detected", 11813: "Power supply fault; turn the printer off",
    11817: "Extruder motor may be short-circuited", 11830: "Printer storage nearly full", 11831: "File couldn't be unpacked",
    11858: "An ACE slot ran out of filament", 11871: "Print head shell detached",
}
PAUSE_STATES = {0: "running", 1: "paused", 2: "pausing", 3: "resuming", 4: "stopping"}


class PrinterError(RuntimeError):
    pass


# ------------------------------------------------------------------ addresses
def _allow_public() -> bool:
    return os.environ.get("ANYCUBIC_MCP_ALLOW_PUBLIC_PRINTER") == "1"


def check_host(host: str) -> str:
    """Accept only addresses on the local network, so the tools can't be pointed at the internet."""
    host = (host or "").strip()
    if not host or any(c in host for c in "/:@?# ") or len(host) > 253:
        raise PrinterError("Give the printer's IP address on your network, e.g. 192.168.1.50.")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, INFO_PORT, socket.AF_INET, socket.SOCK_STREAM)}
    except OSError as e:
        raise PrinterError(f"Can't resolve {host}: {e}") from None
    for a in addrs:
        ip = ipaddress.ip_address(a)
        local = (ip.is_private or ip.is_link_local) and not ip.is_loopback
        if not local and not _allow_public():
            raise PrinterError(f"{host} ({a}) isn't a local-network address. Printer tools only talk to printers on "
                               "your own network; set ANYCUBIC_MCP_ALLOW_PUBLIC_PRINTER=1 to override.")
    return host


def _settings_file() -> Path:
    return state_dir() / "printer.json"


def remembered_host() -> str | None:
    env = os.environ.get("ANYCUBIC_PRINTER_HOST")
    if env:
        return env.strip()
    try:
        return json.loads(_settings_file().read_text()).get("host")
    except (OSError, ValueError):
        return None


def remember_host(host: str) -> None:
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"host": host}))


def resolve_host(host: str | None) -> str:
    h = host or remembered_host()
    if not h:
        raise PrinterError("No printer configured. Call find_printers to look for it on your network, or pass "
                           "host='<printer IP>' (shown on the printer under Settings > Network).")
    return check_host(h)


# ------------------------------------------------------------------ discovery & handshake
def _http():
    import httpx
    return httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0), follow_redirects=False, trust_env=False)


def _json(resp) -> dict:
    try:
        return json.loads(resp.content.decode("utf-8", "replace"))
    except ValueError:
        raise PrinterError(f"Unexpected reply from the printer (HTTP {resp.status_code}).") from None


def fetch_info(host: str, client=None) -> dict:
    own = client is None
    client = client or _http()
    try:
        r = client.get(f"http://{host}:{INFO_PORT}/info")
    except Exception as e:  # noqa: BLE001 - connection errors vary by platform
        raise PrinterError(f"No Anycubic printer answered at {host}:{INFO_PORT} ({type(e).__name__}). Check the IP, "
                           "that the printer is on and in LAN mode (Settings > Network > LAN Mode; not switchable "
                           "during a print), and that this computer is on the same network. With LAN mode off the "
                           "printer is on Anycubic Cloud and closes this port: use cloud_printer_status and "
                           "print_from_slicer, which go through the slicer window.") from None
    finally:
        if own:
            client.close()
    return _json(r)


def describe_info(host: str, info: dict) -> dict:
    model_id = str(info.get("modelId") or info.get("modeId") or "")
    return {"host": host, "model": info.get("modelName") or MODELS.get(model_id) or f"model {model_id or '?'}",
            "model_id": model_id or None, "name": info.get("deviceName"),
            "lan_mode": info.get("ctrlType") == "lan", "ctrl_type": info.get("ctrlType"),
            "serial_tail": (str(info.get("cn"))[-4:] if info.get("cn") else None)}


def sign(token: str, ts: int, nonce: str) -> str:
    inner = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5(f"{inner}{ts}{nonce}".encode()).hexdigest()


def decrypt_credentials(token: str, info_b64: str, iv: str) -> dict:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = token[16:32].encode()
    ivb = iv.encode()[:16].ljust(16, b"\0")
    dec = Cipher(algorithms.AES(key), modes.CBC(ivb)).decryptor()
    raw = dec.update(base64.b64decode(info_b64)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return json.loads((unpad.update(raw) + unpad.finalize()).decode("utf-8"))


@dataclass
class Credentials:
    host: str
    port: int
    username: str
    password: str
    device_id: str
    model_id: str
    model: str
    cert: str | None
    key: str | None
    info: dict
    made: float = field(default_factory=time.monotonic)


def handshake(host: str, client=None) -> Credentials:
    own = client is None
    client = client or _http()
    try:
        info = fetch_info(host, client)
        if info.get("ctrlType") != "lan":
            raise PrinterError("LAN mode is off on this printer. Either use cloud_printer_status and print_from_slicer, "
                               "which go through the slicer window and Anycubic Cloud, or switch LAN mode on at the "
                               "printer's Settings > Network > LAN Mode (this disconnects it from Anycubic Cloud).")
        token = str(info.get("token") or "")
        if len(token) < 32:
            raise PrinterError("This printer's LAN handshake isn't supported (older firmware or model).")
        ts = int(time.time() * 1000)
        nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
        did = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(32))
        # Always POST to the host we dialled, never to an address the printer's reply points at.
        path = urlparse(str(info.get("ctrlInfoUrl") or "/ctrl")).path or "/ctrl"
        r = client.post(f"http://{host}:{INFO_PORT}{path}",
                        params={"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
        body = _json(r)
    except PrinterError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PrinterError(f"LAN handshake with {host} failed ({type(e).__name__}).") from None
    finally:
        if own:
            client.close()
    if body.get("code") != 200 or not isinstance(body.get("data"), dict):
        if body.get("code") == 19002:
            raise PrinterError("The printer refused: too many LAN clients are paired. Restart the printer and retry.")
        raise PrinterError(f"The printer refused the LAN handshake (code {body.get('code')}: {body.get('message')}).")
    try:
        creds = decrypt_credentials(token, body["data"]["info"], str(body["data"].get("token") or ""))
    except Exception:  # noqa: BLE001
        raise PrinterError("Couldn't read the printer's LAN credentials (unexpected format; firmware may have "
                           "changed).") from None
    broker = urlparse(str(creds.get("broker") or ""))
    model_id = str(creds.get("modelId") or creds.get("modeId") or info.get("modelId") or "")
    return Credentials(host=host, port=broker.port or 9883, username=str(creds.get("username") or ""),
                       password=str(creds.get("password") or ""), device_id=str(creds.get("deviceId") or ""),
                       model_id=model_id, model=MODELS.get(model_id) or str(info.get("modelName") or "printer"),
                       cert=creds.get("devicecrt"), key=creds.get("devicepk"), info=info)


_CREDS: dict[str, Credentials] = {}
_CREDS_LOCK = threading.Lock()


def credentials(host: str, fresh: bool = False) -> Credentials:
    with _CREDS_LOCK:
        c = _CREDS.get(host)
        if fresh or not c or time.monotonic() - c.made > CRED_TTL:
            c = _CREDS[host] = handshake(host)
        return c


def local_subnets() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    candidates: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            candidates.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.update(ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    for a in candidates:
        ip = ipaddress.ip_address(a)
        if ip.is_private and not ip.is_loopback:
            n = ipaddress.ip_network(f"{a}/24", strict=False)
            if n not in nets:
                nets.append(n)
    return nets


def _port_open(ip: str, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((ip, INFO_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def find(subnet: str | None = None) -> dict:
    if subnet:
        net = ipaddress.ip_network(subnet, strict=False)
        if not isinstance(net, ipaddress.IPv4Network) or not net.is_private or net.num_addresses > 1024:
            raise PrinterError("subnet must be a private IPv4 range of at most 1024 addresses, e.g. 192.168.1.0/24.")
        nets = [net]
    else:
        nets = local_subnets()
        if not nets:
            raise PrinterError("Couldn't work out this computer's local network. Pass subnet='192.168.1.0/24'.")
    hosts = [str(h) for n in nets for h in n.hosts()]
    with ThreadPoolExecutor(max_workers=64) as pool:
        open_hosts = [h for h, ok in zip(hosts, pool.map(_port_open, hosts)) if ok]
    found = []
    with _http() as client:
        for h in open_hosts:
            try:
                found.append(describe_info(h, fetch_info(h, client)))
            except PrinterError:
                continue
    out: dict = {"searched": [str(n) for n in nets], "printers": found}
    if not found:
        out["hint"] = ("No printer answered. A printer only shows up here with LAN mode on: on its screen open Settings > "
                       "Network > LAN Mode (it can't be switched while a print is running or paused). That screen also "
                       "shows its IP. Also check this computer is on the same Wi-Fi and not a guest network. If the "
                       "printer is meant to stay on Anycubic Cloud (LAN mode off), use cloud_printer_status and "
                       "print_from_slicer instead; they go through the slicer window.")
    return out


# ------------------------------------------------------------------ MQTT session
def _envelope(type_: str, action: str, data: Any = None) -> dict:
    return {"type": type_, "action": action, "timestamp": int(time.time() * 1000), "msgid": str(uuid.uuid4()),
            "data": data}


class Link:
    """One short MQTT session with the printer: subscribe, send queries or commands, collect reports."""

    def __init__(self, creds: Credentials, timeout: float = 8.0):
        self.c = creds
        self.timeout = timeout
        self.messages: list[tuple[str, dict]] = []
        self._cv = threading.Condition()
        self._connected = threading.Event()
        self._rc: Any = None
        self._client = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._seen_ids: set[tuple[str, str]] = set()

    # topics
    def _sub_topic(self) -> str:
        return f"{TOPIC_ROOT}/printer/+/{self.c.model_id}/{self.c.device_id}/#"

    def _pub_topic(self, type_: str, role: str = "web") -> str:
        return f"{TOPIC_ROOT}/{role}/printer/{self.c.model_id}/{self.c.device_id}/{type_}"

    def _ssl(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the printer's broker uses a self-signed certificate
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        if self.c.cert and self.c.key:
            self._tmp = tempfile.TemporaryDirectory(prefix="acmcp-")
            d = Path(self._tmp.name)
            for name, text in (("c.pem", self.c.cert), ("k.pem", self.c.key)):
                p = d / name
                p.touch(mode=0o600)
                p.write_text(text)
            try:
                ctx.load_cert_chain(str(d / "c.pem"), str(d / "k.pem"))
            except (ssl.SSLError, OSError):
                pass  # some firmware doesn't need the client certificate
            finally:
                self._tmp.cleanup()
                self._tmp = None
        return ctx

    def __enter__(self) -> "Link":
        import paho.mqtt.client as mqtt
        cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"acmcp-{secrets.token_hex(6)}",
                         protocol=mqtt.MQTTv311)
        cl.username_pw_set(self.c.username, self.c.password)
        cl.tls_set_context(self._ssl())
        cl.tls_insecure_set(True)

        def on_connect(client, userdata, flags, rc, props=None):
            self._rc = rc
            if not rc.is_failure:
                client.subscribe(self._sub_topic(), qos=0)
            self._connected.set()

        def on_message(client, userdata, msg):
            try:
                data = json.loads(msg.payload.decode("utf-8", "replace"))
            except ValueError:
                return
            if isinstance(data, dict):
                with self._cv:
                    mid = data.get("msgid")
                    if mid and (msg.topic, mid) in self._seen_ids:
                        return  # the printer sends each report twice
                    if mid:
                        self._seen_ids.add((msg.topic, mid))
                    self.messages.append((msg.topic, data))
                    self._cv.notify_all()

        cl.on_connect = on_connect
        cl.on_message = on_message
        try:
            cl.connect(self.c.host, self.c.port, keepalive=60)
        except (OSError, ssl.SSLError) as e:
            raise PrinterError(f"Couldn't open the printer's MQTT connection ({type(e).__name__}).") from None
        cl.loop_start()
        self._client = cl
        if not self._connected.wait(self.timeout) or self._rc is None or self._rc.is_failure:
            self.__exit__(None, None, None)
            raise PrinterError(f"The printer refused the MQTT login ({self._rc}).")
        time.sleep(0.3)  # let the subscription settle before the first query
        return self

    def __exit__(self, *exc) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
                self._client.loop_stop()
            finally:
                self._client = None

    def send(self, type_: str, action: str, data: Any = None, role: str = "web") -> None:
        self._client.publish(self._pub_topic(type_, role), json.dumps(_envelope(type_, action, data)), qos=0)

    def wait_for(self, pred: Callable[[list[tuple[str, dict]]], bool], seconds: float) -> bool:
        end = time.monotonic() + seconds
        with self._cv:
            while not pred(self.messages):
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True


def open_link(host: str, timeout: float = 8.0) -> Link:
    """Connect, re-doing the handshake once if cached credentials were rejected (they rotate on reboot)."""
    try:
        return Link(credentials(host), timeout).__enter__()
    except PrinterError:
        return Link(credentials(host, fresh=True), timeout).__enter__()


def msg_type(topic: str, msg: dict) -> str:
    t = msg.get("type")
    if t:
        return str(t)
    parts = topic.split("/")
    return parts[-2] if len(parts) >= 2 and parts[-1] == "report" else ""


# ------------------------------------------------------------------ state
def _hex(c: Any) -> str | None:
    if isinstance(c, (list, tuple)) and len(c) >= 3 and all(isinstance(v, (int, float)) for v in c[:3]):
        return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(v))) for v in c[:3]))
    if isinstance(c, str) and c:
        return c if c.startswith("#") else f"#{c}"
    return None


def error_entry(code: Any, model_id: str | None = None, msg: str | None = None) -> dict | None:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return None
    if n in (0, 200) or n < 10000:
        return None
    suffix = WIKI_SUFFIX.get(str(model_id or ""))
    wiki = f"https://wiki.anycubic.com/en/error-codes/{n}-code/{suffix}" if suffix else "https://wiki.anycubic.com/en/error-codes"
    return {"code": n, "meaning": ERRORS.get(n) or (msg or "Not in Anycubic's published list"), "wiki": wiki}


class State:
    """Merges the printer's partial reports into one picture. Missing fields stay missing, never zero."""

    def __init__(self, model_id: str | None = None):
        self.model_id = model_id
        self.info: dict = {}
        self.temps: dict = {}
        self.fans: dict = {}
        self.lights: dict = {}
        self.peripherals: dict = {}
        self.work: dict = {}
        self.job: dict | None = None
        self.last_job: dict | None = None
        self.print_state: str | None = None
        self.boxes: dict[Any, dict] = {}
        self.external: dict | None = None
        self.errors: dict[int, dict] = {}
        self.camera: dict = {}
        self.urls: dict = {}
        self.seen: set[str] = set()

    def feed(self, topic: str, msg: dict) -> None:
        t = msg_type(topic, msg)
        if not t:
            return
        self.seen.add(t)
        data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
        err = error_entry(msg.get("code"), self.model_id, msg.get("msg"))
        if err:
            self.errors[err["code"]] = err
        if t == "info":
            self.info.update({k: v for k, v in data.items() if k not in ("project", "temp", "urls")})
            if "project" in data:
                self.job = data["project"] if isinstance(data["project"], dict) else None
            if isinstance(data.get("last_project"), dict):
                self.last_job = data["last_project"]
            if isinstance(data.get("temp"), dict):
                self.temps.update(data["temp"])
            if isinstance(data.get("urls"), dict):
                self.camera.update(data["urls"])
                self.urls.update(data["urls"])
        elif t == "tempature":
            self.temps.update(data)
        elif t == "fan":
            self.fans.update(data)
        elif t == "light":
            for lt in data.get("lights") or ([data] if "type" in data else []):
                if isinstance(lt, dict) and "type" in lt:
                    self.lights[lt["type"]] = lt
        elif t == "peripherie":
            self.peripherals.update(data)
        elif t == "status":
            self.work.update(data)
            if msg.get("state"):
                self.work["state"] = msg["state"]
        elif t == "print":
            if msg.get("state"):
                self.print_state = str(msg["state"])
            if data:
                self.job = {**(self.job or {}), **data}
        elif t == "multiColorBox":
            for box in data.get("multi_color_box") or []:
                if not isinstance(box, dict):
                    continue
                cur = self.boxes.setdefault(box.get("id", len(self.boxes)), {"slots": {}})
                for k, v in box.items():
                    if k == "slots" and isinstance(v, list):
                        for s in v:
                            if isinstance(s, dict):
                                cur["slots"].setdefault(s.get("index", len(cur["slots"])), {}).update(s)
                    else:
                        cur[k] = v
        elif t == "extfilbox":
            self.external = {**(self.external or {}), **data}
        elif t == "video":
            urls = data.get("urls") if isinstance(data.get("urls"), dict) else {}
            self.camera.update(urls)
            if msg.get("state"):
                self.camera["state"] = msg["state"]

    # ---- summaries
    def _temp(self, name: str) -> dict | None:
        cur, tgt = self.temps.get(f"curr_{name}_temp"), self.temps.get(f"target_{name}_temp")
        return None if cur is None and tgt is None else {"current_c": cur, "target_c": tgt}

    def state_name(self) -> str:
        job = self.job or {}
        pause = job.get("pause")
        if isinstance(pause, int) and pause in (1, 2, 3, 4):
            return PAUSE_STATES[pause]
        if self.print_state in ("pausing", "paused", "resuming", "stopping", "preheating", "auto_leveling", "printing"):
            return self.print_state
        if self.job and job.get("state"):
            return str(job["state"])
        if self.job:
            return "printing"
        ended = self.print_state if self.print_state in ("stoped", "finished", "failed") else None
        if not ended and isinstance(self.last_job, dict) and self.last_job.get("state") in ("stoped", "finished", "failed"):
            ended = self.last_job["state"]
        if ended:
            return "idle (last print " + ("stopped" if ended == "stoped" else ended) + ")"
        ws = self.work.get("state")
        return "busy" if ws == "busy" else "idle"

    def task_id(self) -> str | None:
        job = self.job or {}
        tid = job.get("taskid") or job.get("task_id")
        return str(tid) if tid not in (None, "") else None

    def job_summary(self) -> dict | None:
        j = self.job
        if not j:
            return None
        out = {"file": j.get("filename") or j.get("file_name") or j.get("gcode_name"),
               "progress_pct": j.get("progress"), "layer": j.get("curr_layer"), "total_layers": j.get("total_layers"),
               "remaining_min": j.get("remain_time"), "elapsed_as_reported": j.get("print_time"),
               "filament_used_mm": j.get("supplies_usage"), "task_id": self.task_id()}
        return {k: v for k, v in out.items() if v is not None}

    def last_job_summary(self) -> dict | None:
        j = self.last_job
        if not j or self.job:
            return None
        st = j.get("state")
        out = {"file": j.get("filename"), "ended": "stopped" if st == "stoped" else st,
               "layer": j.get("curr_layer"), "total_layers": j.get("total_layers"),
               "progress_pct": j.get("progress") or None,  # a stopped job reports 0 here, which says nothing
               "filament_used_mm": round(j["supplies_usage"]) if isinstance(j.get("supplies_usage"), (int, float)) else None,
               "task_id": str(j["task_id"]) if j.get("task_id") not in (None, "") else None}
        return {k: v for k, v in out.items() if v is not None}

    def filament(self) -> dict:
        boxes = []
        for bid, b in sorted(self.boxes.items(), key=lambda kv: str(kv[0])):
            slots = []
            for idx, s in sorted(b.get("slots", {}).items(), key=lambda kv: str(kv[0])):
                empty = not s.get("type") or s.get("edit_status") == 2
                slots.append({k: v for k, v in {
                    "slot": idx + 1 if isinstance(idx, int) else idx, "material": s.get("type") or None,
                    "colour": None if empty else _hex(s.get("color")),
                    "remaining_pct": s.get("consumables_percent"), "sku": s.get("sku") or None,
                    "state": "empty" if empty else ("ready" if s.get("status") == 5 else "present"),
                }.items() if v is not None})
            ls = b.get("loaded_slot")
            entry = {"box": bid,
                     "slot_in_nozzle": ls + 1 if isinstance(ls, int) and ls >= 0 else "none",
                     "temperature_c": b.get("temp") or None, "humidity_pct": b.get("humidity") or None,
                     "drying": b.get("drying_status") if isinstance(b.get("drying_status"), dict) and
                     b["drying_status"].get("status") else None, "slots": slots}
            boxes.append({k: v for k, v in entry.items() if v is not None})
        out: dict = {"boxes": boxes}
        if any("remaining_pct" in s for b in boxes for s in b.get("slots", [])):
            out["note"] = ("remaining_pct is the figure stored on the printer, not a measurement; "
                           "it can read 100 after a long print.")
        if self.external:
            e = self.external
            out["external_spool"] = {k: v for k, v in {"material": e.get("type") or None, "colour": _hex(e.get("color")),
                                                        "loaded": bool(e.get("loaded")) if "loaded" in e else None}.items()
                                     if v is not None}
        return out

    def summary(self, host: str, model: str) -> dict:
        lights = {("camera" if t == 3 else "chamber" if t == 2 else "head" if t == 1 else f"type {t}"):
                  ("on" if v.get("status") == 1 else "off") for t, v in self.lights.items()}
        temps = {k: v for k, v in (("nozzle", self._temp("nozzle")), ("bed", self._temp("hotbed")),
                                    ("chamber", self._temp("chamber"))) if v}
        out = {"printer": {"host": host, "model": model, "firmware": self.info.get("version"),
                           "name": self.info.get("printerName") or self.info.get("deviceName")},
               "state": self.state_name(), "job": self.job_summary(),
               "last_job": self.last_job_summary(),
               "temperatures": temps or None,
               "fans_pct": {k: v for k, v in self.fans.items() if k.endswith(("pct", "level"))} or None,
               "lights": lights or None, "filament": self.filament(),
               "camera_present": bool(self.peripherals.get("camera")) if "camera" in self.peripherals else None,
               "usb_drive": bool(self.peripherals.get("udisk")) if "udisk" in self.peripherals else None,
               "errors": sorted(self.errors.values(), key=lambda e: e["code"]),
               "reports_received": sorted(self.seen)}
        return {k: v for k, v in out.items() if v not in (None, {}, [])} | {"errors": out["errors"]}


def _have(*types: str):
    def pred(msgs):
        got = {msg_type(t, m) for t, m in msgs}
        return all(x in got for x in types)
    return pred


def status(host: str, wait_seconds: float = 8.0) -> dict:
    with open_link(host) as link:
        for t in STATUS_TYPES:
            link.send(t, "query")
        link.send("multiColorBox", "getInfo")
        link.wait_for(_have("info", "tempature", "multiColorBox"), wait_seconds)
        link.wait_for(lambda m: False, 1.0)  # a moment for stragglers
        st = State(link.c.model_id)
        for topic, msg in list(link.messages):
            st.feed(topic, msg)
        return st.summary(host, link.c.model)


# ------------------------------------------------------------------ pause / resume / stop
EXPECT = {"pause": {"pausing", "paused"}, "resume": {"resuming", "printing"}, "stop": {"stopping", "stoped", "stopped"}}
ALLOWED_FROM = {"pause": {"printing", "running", "preheating", "auto_leveling", "resuming"},
                "resume": {"paused", "pausing"},
                "stop": {"printing", "running", "paused", "pausing", "resuming", "preheating", "auto_leveling"}}


def control(host: str, action: str, confirm: bool) -> dict:
    if action not in EXPECT:
        raise PrinterError("action must be 'pause', 'resume' or 'stop'.")
    with open_link(host) as link:
        for t in ("info", "print", "status"):
            link.send(t, "query")
        link.wait_for(_have("info"), 6.0)
        st = State(link.c.model_id)
        for topic, msg in list(link.messages):
            st.feed(topic, msg)
        before = st.state_name()
        job = st.job_summary()
        if before not in ALLOWED_FROM[action]:
            return {"done": False, "reason": f"The printer is '{before}', so there's nothing to {action}.", "job": job}
        what = {"pause": "pause the print (heaters stay on; resume continues from here)",
                "resume": "resume the paused print",
                "stop": "STOP the print. This can't be undone; the part on the bed will be unfinished"}[action]
        if not confirm:
            return {"done": False, "confirm_required": True, "state": before, "job": job,
                    "will": what, "next": f"Ask the user to confirm, then call control_print(action='{action}', confirm=True)."}
        n = len(link.messages)
        link.send("print", action, {"taskid": st.task_id() or "-1"})
        ok = link.wait_for(lambda msgs: any(msg_type(t, m) == "print" and str(m.get("state")) in EXPECT[action]
                                           for t, m in msgs[n:]), 20.0)
        for topic, msg in list(link.messages[n:]):
            st.feed(topic, msg)
        result = {"done": ok, "action": action, "state_before": before, "state_now": st.state_name(), "job": st.job_summary(),
                  "errors": sorted(st.errors.values(), key=lambda e: e["code"])}
        if not ok:
            result["note"] = "No confirmation from the printer within 20 s. Check printer_status before trying again."
        return result


# ------------------------------------------------------------------ camera
def ffmpeg_path() -> str | None:
    return os.environ.get("ANYCUBIC_MCP_FFMPEG") or shutil.which("ffmpeg")


def _stream_url(host: str, urls: dict) -> str | None:
    for key in ("rtspUrl", "flvUrl", "url"):
        u = urls.get(key)
        if isinstance(u, str) and u.startswith("http"):
            p = urlparse(u)
            if p.hostname != host:  # only ever fetch from the printer itself
                u = p._replace(netloc=f"{host}:{p.port or 18088}").geturl()
            return u
    return None


def grab_frame(url: str, out: Path, ffmpeg: str, seconds: float = 20.0) -> None:
    """Save one JPEG from the HTTP-FLV stream. A small probe keeps ffmpeg from buffering several seconds
    of the live stream before decoding (on a Kobra X: about 7 s instead of 21 s)."""
    import httpx
    proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "32768",
                             "-analyzeduration", "500000", "-f", "flv", "-i", "pipe:0",
                             "-frames:v", "1", "-q:v", "2", "-y", str(out)],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    end = time.monotonic() + seconds
    sent = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False) as client:
            with client.stream("GET", url) as r:
                if r.status_code not in (200, 206):
                    raise PrinterError(f"Camera stream answered HTTP {r.status_code}.")
                for chunk in r.iter_bytes(65536):
                    if proc.poll() is not None or time.monotonic() > end or sent > 40_000_000:
                        break
                    try:
                        proc.stdin.write(chunk)
                        sent += len(chunk)
                    except (BrokenPipeError, OSError):
                        break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(1.0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
    if not out.is_file() or out.stat().st_size == 0:
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()[-300:]
        raise PrinterError(f"No frame decoded from the camera stream{': ' + err if err else ''}.")


def snapshot(host: str, out: Path) -> dict:
    ff = ffmpeg_path()
    if not ff:
        raise PrinterError("Camera snapshots need ffmpeg. Install it (macOS: brew install ffmpeg; Windows: winget "
                           "install ffmpeg) or set ANYCUBIC_MCP_FFMPEG to its path.")
    with open_link(host) as link:
        link.send("light", "query")
        link.send("peripherie", "query")
        link.send("info", "query")
        link.wait_for(_have("light", "info"), 5.0)
        st = State(link.c.model_id)
        for t, m in list(link.messages):
            st.feed(t, m)
        if st.peripherals.get("camera") == 0:
            raise PrinterError("The printer reports no camera connected.")
        cam_light_was = st.lights.get(3, {}).get("status")
        n = len(link.messages)
        link.send("video", "startCapture")
        link.wait_for(lambda msgs: any(msg_type(t, m) == "video" for t, m in msgs[n:]), 8.0)
        for t, m in list(link.messages[n:]):
            st.feed(t, m)
        url = _stream_url(host, st.camera)
        if not url:
            link.send("info", "query")
            link.wait_for(lambda msgs: False, 2.0)
            for t, m in list(link.messages):
                st.feed(t, m)
            url = _stream_url(host, st.camera) or f"http://{host}:18088/flv"
        try:
            grab_frame(url, out, ff)
        finally:
            link.send("video", "stopCapture")
            if cam_light_was == 0:  # opening the stream switches the camera light on; put it back
                link.send("light", "control", {"type": 3, "status": 0, "brightness": 0})
            time.sleep(0.5)
    return {"image": str(out), "light_restored": cam_light_was == 0, "state": st.state_name(), "job": st.job_summary()}


# ------------------------------------------------------------------ send a print
# Kobra X LAN printing (community-documented, confirmed on firmware 2.0.2.8): upload the .gcode.3mf with an
# HTTP multipart POST to the URL the printer's info report gives (fileUploadurl, port 18910, carries a one-time
# token), then publish print/start naming the file and mapping each filament to a colour-unit slot.
START_STATES = {"init", "checking", "downloading", "preheating", "auto_leveling", "printing", "hotbed_heating",
                "nozzle_heating", "ace_filament_feeding", "nozzle_purging", "nozzle_wiping",
                "z_offset_calibrating", "frequency_sweeping"}
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def read_job_file(path: Path) -> dict:
    """What a sliced .gcode.3mf says about itself: printer model, time, weight and filaments per slot."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError, OSError) as exc:
        raise PrinterError(f"{path.name} isn't a sliced .gcode.3mf (no slice info).") from exc
    plates = root.findall("plate")
    if len(plates) != 1:
        raise PrinterError(f"{path.name} has {len(plates)} plates; send a file with exactly one sliced plate.")
    meta = {m.get("key"): m.get("value") for m in plates[0].findall("metadata")}
    if f"Metadata/plate_{meta.get('index', '1')}.gcode" not in names:
        raise PrinterError(f"{path.name} has no G-code inside; slice it first.")
    fils = [{"index": int(f.get("id", "1")) - 1, "type": f.get("type") or "", "colour": (f.get("color") or "").upper(),
             "used_g": float(f.get("used_g") or 0)} for f in plates[0].findall("filament")]
    pred = int(float(meta.get("prediction") or 0))
    return {"printer_model": meta.get("printer_model_id") or "", "time_s": pred,
            "time": f"{pred // 3600}h {pred % 3600 // 60:02d}m", "weight_g": float(meta.get("weight") or 0),
            "filaments": fils}


def _rgb(hex_colour: str | None) -> list[int]:
    h = (hex_colour or "").lstrip("#")
    try:
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return [255, 255, 255]


def _colour_distance(a: str | None, b: str | None) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5


def plan_slots(filaments: list[dict], boxes: list[dict], slots: list[int] | None) -> tuple[list[dict], list[str]]:
    """Map each filament in the file to a slot of the printer's colour unit (1-based slots for people,
    0-based ams_index for the printer). Picks by material, then nearest colour, unless slots are given."""
    if len(boxes) != 1:
        raise PrinterError("This printer reports " + (f"{len(boxes)} colour units" if boxes else "no colour unit") +
                           "; sending is only supported with exactly one (the Kobra X's own unit or one ACE).")
    box = boxes[0]
    loaded = [s for s in box.get("slots", []) if s.get("state") != "empty"]
    if slots is not None and len(slots) != len(filaments):
        raise PrinterError(f"The file uses {len(filaments)} filament(s); give one slot for each.")
    mapping, notes, used = [], [], set()
    for i, f in enumerate(filaments):
        if slots is not None:
            want = int(slots[i])
            slot = next((s for s in box.get("slots", []) if s.get("slot") == want), None)
            if slot is None or slot.get("state") == "empty":
                raise PrinterError(f"Slot {want} is empty or doesn't exist.")
        else:
            same = [s for s in loaded if str(s.get("material", "")).upper() == f["type"].upper() and s["slot"] not in used]
            if not same:
                raise PrinterError(f"No loaded slot has {f['type']} for filament {i + 1}. Load it, or pass slots=[...].")
            slot = min(same, key=lambda s: _colour_distance(s.get("colour"), f["colour"]))
        if str(slot.get("material", "")).upper() != f["type"].upper():
            raise PrinterError(f"Slot {slot['slot']} has {slot.get('material')}, but filament {i + 1} was sliced as "
                               f"{f['type']}. Re-slice with the right filament or load {f['type']}.")
        if _colour_distance(slot.get("colour"), f["colour"]) > 60:
            notes.append(f"Filament {i + 1} was sliced as {f['colour']} but slot {slot['slot']} holds {slot.get('colour')}.")
        used.add(slot["slot"])
        mapping.append({"filament": i + 1, "material": f["type"], "file_colour": f["colour"], "slot": slot["slot"],
                        "slot_colour": slot.get("colour"), "slot_material": slot.get("material")})
    return mapping, notes


def _upload_url(host: str, urls: dict) -> str | None:
    u = urls.get("fileUploadurl") or urls.get("fileUploadUrl")
    if not isinstance(u, str) or not u.startswith("http"):
        return None
    p = urlparse(u)
    return p._replace(scheme="http", netloc=f"{host}:{p.port or INFO_PORT}").geturl()  # only ever the printer itself


def upload_job(url: str, path: Path, timeout: float = 300.0) -> str:
    import httpx
    size = path.stat().st_size
    with open(path, "rb") as fh, httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), trust_env=False,
                                               follow_redirects=False) as client:
        r = client.post(url, data={"filename": path.name}, files={"gcode": (path.name, fh, "application/octet-stream")},
                        headers={"X-File-Length": str(size)})
    if r.status_code == 401:
        raise PrinterError("The printer refused the upload token (401).")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or body.get("code") not in (200, "200"):
        raise PrinterError(f"Upload failed (HTTP {r.status_code}, code {body.get('code')}: {body.get('message', '')}).")
    return str((body.get("data") or {}).get("gcode") or path.name)


def _start_payload(filename: str, mapping: list[dict], colours: dict[int, str]) -> dict:
    return {"taskid": "-1", "filename": filename, "filepath": "/", "filetype": 1, "project_type": 1,
            "file_key": "", "file_name": "", "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "ams_settings": {"use_ams": True, "ams_box_mapping": [
                {"paint_index": m["filament"] - 1, "ams_index": m["slot"] - 1,
                 "paint_color": _rgb(colours.get(m["filament"])), "ams_color": _rgb(m.get("slot_colour")),
                 "material_type": m["material"]} for m in mapping]}}


def send_job(host: str, path: Path, slots: list[int] | None, confirm: bool) -> dict:
    """Upload a sliced .gcode.3mf and start it, only with confirm=True. Without it, return what would happen."""
    if path.suffix.lower() != ".3mf" or not path.name.lower().endswith(".gcode.3mf"):
        raise PrinterError("Send a sliced .gcode.3mf (slice_model writes one next to the .gcode).")
    if not path.is_file():
        raise PrinterError(f"{path} doesn't exist.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise PrinterError(f"{path.name} is larger than {MAX_UPLOAD_BYTES // 2**20} MB.")
    job = read_job_file(path)
    with open_link(host) as link:
        for t in ("info", "print", "status"):
            link.send(t, "query")
        link.send("multiColorBox", "getInfo")
        link.wait_for(_have("info", "multiColorBox"), 8.0)
        st = State(link.c.model_id)
        for topic, msg in list(link.messages):
            st.feed(topic, msg)
        model = link.c.model or ""
        if job["printer_model"] and model and model.lower() not in job["printer_model"].lower():
            raise PrinterError(f"{path.name} was sliced for {job['printer_model']}, but this printer is a {model}.")
        if st.info.get("state") not in ("free", None) or st.job:
            raise PrinterError(f"The printer is busy ('{st.state_name()}'). Wait until it's idle.")
        mapping, notes = plan_slots(job["filaments"], st.filament().get("boxes", []), slots)
        preview = {"file": path.name, "size_mb": round(path.stat().st_size / 2**20, 1), "printer": model,
                   "time_normal_mode": job["time"], "weight_g": job["weight_g"], "slots": mapping, "notes": notes}
        if not confirm:
            return {"started": False, "confirm_required": True, **preview,
                    "will": "Upload this file to the printer and START printing it. The nozzle and bed heat up and the "
                            "head moves as soon as it starts.",
                    "check_first": ["the bed is empty and the build plate is seated flat",
                                    "the right filament is in the slots shown"],
                    "next": "Show this to the user and ask; on a clear yes call send_print again with confirm=True."}
        url = _upload_url(host, st.urls)
        if not url:
            raise PrinterError("The printer didn't report an upload address. Check that LAN mode is on and try again.")
        stored = upload_job(url, path)
        colours = {f["index"] + 1: f["colour"] for f in job["filaments"]}
        payload = _start_payload(stored, mapping, colours)
        result: dict = {}
        for role in ("web", "slicer"):
            n = len(link.messages)
            link.send("print", "start", payload, role=role)
            link.wait_for(lambda msgs: any(msg_type(t, m) == "print" and (str(m.get("state")) in START_STATES or
                                           m.get("state") in ("failed", "stoped")) for t, m in msgs[n:]), 25.0)
            link.wait_for(lambda msgs: False, 6.0)  # an accepted start can still end a moment later (10111)
            fresh = State(link.c.model_id)
            for topic, msg in list(link.messages[n:]):
                fresh.feed(topic, msg)
            states = [str(m.get("state")) for t, m in link.messages[n:] if msg_type(t, m) == "print"]
            errors = sorted(fresh.errors.values(), key=lambda e: e["code"])
            started = bool(states) and states[-1] in START_STATES and not errors
            result = {"started": started, **preview, "uploaded_as": stored, "topic": role,
                      "printer_states": states[-6:], "errors": errors}
            if started or not any(e["code"] == 10110 for e in errors):
                break  # only retry on the other topic when the printer said the message itself was invalid
        if not result.get("started"):
            result["note"] = ("The printer didn't confirm the start. Check printer_status and the printer's screen "
                              "before trying again; the file is already on the printer.")
        return result
