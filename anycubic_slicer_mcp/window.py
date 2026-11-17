"""Drive the Anycubic Slicer Next window: open it with chosen presets, restart it, take a screenshot
of it, and click inside it.

Everything else in this server works without the window. These tools exist for what only the window
does: showing the user a slice, sending a print from its device tab, or settings the command line
can't reach. Screenshots and clicks are limited to the slicer's own window, and clicks only land when
the slicer is the frontmost app.

macOS: screenshots need Screen Recording permission and clicks need Accessibility permission for the
app that runs this server (System Settings > Privacy & Security). Windows: open and restart work;
screenshots and clicks are not supported yet.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .install import Install

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
OWNER_HINTS = ("AnycubicSlicerNext", "Anycubic Slicer Next")
TITLE_BAR_PT = 30  # macOS title bar with the close/minimise/zoom buttons; never clicked
KEYS = {"return": 36, "enter": 36, "escape": 53, "tab": 48, "space": 49, "delete": 51}


class WindowError(ValueError):
    pass


# ------------------------------------------------------------------ process
def running(install: Install) -> bool:
    name = install.executable.name
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return name.lower() in out.lower()
    return subprocess.run(["pgrep", "-f", str(install.executable)], capture_output=True).returncode == 0


def _bundle(install: Install) -> Path:
    exe = install.executable
    return exe.parents[2] if exe.parent.name == "MacOS" else exe


def launch(install: Install, path: Path | None = None) -> None:
    if not install.executable.is_file():
        raise WindowError(f"Anycubic Slicer Next not found at {install.executable}.")
    if IS_MAC:
        subprocess.run(["open", "-a", str(_bundle(install)), *([str(path)] if path else [])], check=True)
    else:
        subprocess.Popen([str(install.executable), *([str(path)] if path else [])],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


def bring_to_front(install: Install) -> None:
    if IS_MAC:
        subprocess.run(["open", "-a", str(_bundle(install))], check=False)
        time.sleep(1.2)


def quit_slicer(install: Install, wait_s: float = 20.0) -> dict[str, Any]:
    """Ask the slicer to quit the way the user would. Never forces it: if it's asking whether to save the
    project, it stays open and the user answers."""
    if not running(install):
        return {"quit": True, "was_running": False}
    if IS_MAC:
        try:  # "quit" is the same Apple Event as Cmd-Q; it returns only after the app answers
            subprocess.run(["osascript", "-e", f'tell application "{_bundle(install).stem}" to quit'],
                           capture_output=True, timeout=wait_s)
        except subprocess.TimeoutExpired:
            pass
    elif IS_WIN:
        subprocess.run(["taskkill", "/IM", install.executable.name], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    end = time.monotonic() + wait_s
    while time.monotonic() < end and running(install):
        time.sleep(0.5)
    if running(install):
        return {"quit": False, "was_running": True,
                "reason": "The slicer is still open, most likely asking whether to save the current project. "
                          "Ask the user to answer that dialog (or use slicer_screenshot to look), then try again."}
    return {"quit": True, "was_running": True}


# ------------------------------------------------------------------ preset selection
def select_presets(install: Install, printer: str, process: str | None, filaments: list[str] | None,
                   colours: list[str] | None) -> Path:
    """Write the printer/process/filament selection into the slicer's settings file, which it reads when it
    starts. Only while it's closed, since it rewrites the file on exit. Keeps a backup next to it."""
    if running(install):
        raise WindowError("The slicer is open; it would overwrite the selection when it quits. Use restart_slicer.")
    conf_path = install.config_file
    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    blocks = conf.get("anycubic_presets")
    if not isinstance(blocks, list):
        blocks = []
    block = next((b for b in blocks if isinstance(b, dict) and b.get("machine") == printer), None)
    if block is None:
        template = next((b for b in blocks if isinstance(b, dict)), {})
        block = {k: v for k, v in template.items() if k.startswith("flush_") or k == "curr_bed_type"}
        block["machine"] = printer
    else:
        blocks.remove(block)
    if process:
        block["process"] = process
    if filaments:
        block["filament"] = ";".join(filaments)
        old = [c for c in str(block.get("filament_colors") or "").split(";") if c]
        cols = list(colours or old)[: len(filaments)]
        cols += ["#FFFFFF"] * (len(filaments) - len(cols))
        block["filament_colors"] = ";".join(c.upper() for c in cols)
    blocks.insert(0, block)  # the slicer reads the first entry as the current selection
    conf["anycubic_presets"] = blocks
    if isinstance(conf.get("presets"), dict):
        conf["presets"]["machine"] = printer
    backup = conf_path.with_name(conf_path.name + ".bak-acmcp")
    shutil.copy2(conf_path, backup)
    tmp = conf_path.with_name(conf_path.name + ".tmp-acmcp")
    tmp.write_text(json.dumps(conf, indent=4) + "\n", encoding="utf-8")
    tmp.replace(conf_path)
    return backup


# ------------------------------------------------------------------ macOS window, screenshot, click
def _quartz():
    try:
        import Quartz  # pyobjc-framework-Quartz, installed on macOS with this package
    except ImportError as exc:
        raise WindowError("Window control needs pyobjc-framework-Quartz (pip install pyobjc-framework-Quartz).") from exc
    return Quartz


def _windows_front_to_back() -> list[dict[str, Any]]:
    Q = _quartz()
    opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
    out = []
    for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
        if int(w.get("kCGWindowLayer", 1)) != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        out.append({"id": int(w["kCGWindowNumber"]), "owner": str(w.get("kCGWindowOwnerName", "")),
                    "title": str(w.get("kCGWindowName") or ""),
                    "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
                    "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0))})
    return out


def _is_slicer(w: dict[str, Any]) -> bool:
    return any(h in w["owner"] for h in OWNER_HINTS)


def slicer_window(install: Install, front: bool = True) -> dict[str, Any]:
    if not IS_MAC:
        raise WindowError("Screenshots and clicks are supported on macOS only for now.")
    if not running(install):
        raise WindowError("Anycubic Slicer Next isn't running. Open it with open_in_slicer or restart_slicer.")
    if front:
        bring_to_front(install)
    wins = [w for w in _windows_front_to_back() if _is_slicer(w) and w["w"] > 200 and w["h"] > 200]
    if not wins:
        raise WindowError("The slicer has no window on this screen. It may be minimised or on another Space.")
    return max(wins, key=lambda w: w["w"] * w["h"])


def screenshot(install: Install, out: Path, max_px: int = 1600, ocr: bool = False) -> dict[str, Any]:
    """Capture only the slicer's window. Returns the image path and how image pixels map to the screen. With ocr,
    also the window's text ("items"), read at full resolution and given in the saved image's pixels."""
    win = slicer_window(install)
    items: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="acmcp-shot-") as tmp:
        raw = Path(tmp) / "w.png"
        r = subprocess.run(["screencapture", "-x", "-o", "-l", str(win["id"]), str(raw)], capture_output=True, text=True)
        if r.returncode != 0 or not raw.is_file():
            raise WindowError("macOS refused the screenshot. Give the app running this server Screen Recording "
                              "permission (System Settings > Privacy & Security > Screen Recording), then restart it.")
        from PIL import Image
        img = Image.open(raw)
        img.load()
        if ocr:
            items = read_text(raw)
    scale = min(1.0, max_px / max(img.width, 1))
    for i in items:
        i["box"] = [round(v * scale) for v in i["box"]]
    if scale < 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "PNG")
    meta = {"image": str(out), "image_size": [img.width, img.height], "window": {k: win[k] for k in ("x", "y", "w", "h")},
            "title": win["title"], "note": "Click coordinates are pixels in this image."}
    if ocr:
        meta["items"] = items
    return meta


def _accessibility_ok() -> bool:
    try:
        from ApplicationServices import (
            AXIsProcessTrusted,  # pyobjc-framework-ApplicationServices
        )
    except ImportError:
        return False
    return bool(AXIsProcessTrusted())


def click(install: Install, x: float, y: float, image_size: tuple[int, int], double: bool = False) -> dict[str, Any]:
    """Click at (x, y) in the last screenshot's pixels. Refuses points outside the slicer window, and refuses
    unless the slicer's window is the frontmost window, so a click can never land in another app."""
    if not _accessibility_ok():
        raise WindowError("Clicking needs Accessibility permission for the app running this server (System Settings "
                          "> Privacy & Security > Accessibility). Ask the user; it's their call.")
    win = slicer_window(install)
    iw, ih = image_size
    if not (0 <= x < iw and 0 <= y < ih):
        raise WindowError(f"({x}, {y}) is outside the {iw}x{ih} screenshot.")
    sx, sy = win["x"] + x * win["w"] / iw, win["y"] + y * win["h"] / ih
    if sy - win["y"] < TITLE_BAR_PT:
        raise WindowError("That point is in the window's title bar (close, minimise and full-screen buttons); "
                          "clicks there are refused.")
    top = next(iter(_windows_front_to_back()), None)
    if top is None or not _is_slicer(top):
        raise WindowError("Another window is in front of the slicer, so the click was not sent.")
    Q = _quartz()
    for i in range(2 if double else 1):
        for kind in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            ev = Q.CGEventCreateMouseEvent(None, kind, (sx, sy), Q.kCGMouseButtonLeft)
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, i + 1)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.05)
    time.sleep(0.6)
    return {"clicked": [round(sx), round(sy)], "double": double}


# ------------------------------------------------------------------ reading the window (macOS Vision OCR)
# Labels that start, stop, send, delete or discard something. A click on one needs confirm=True, which the agent
# may only pass after the user said yes to that exact action.
GUARDED = ("start print", "print", "print now", "send", "end", "stop", "pause", "resume", "skip", "delete",
           "remove", "discard", "don't save", "dont save", "no", "overwrite", "replace", "upload", "format")
_GLYPHS = re.compile(r"^[^0-9A-Za-z]+|[^0-9A-Za-z.%)]+$")


def norm(text: str) -> str:
    """Lower-case, without the icon glyphs OCR reads around labels ('• End', '„ Print Plate', 'Print A... ›')."""
    return re.sub(r"\s+", " ", _GLYPHS.sub("", text.strip()).replace("\u2019", "'").lower())


def variants(text: str) -> list[str]:
    """The label, and the label without a short leading token where OCR read an icon as letters ('a Skip', '1l Pause')."""
    n = norm(text)
    m = re.match(r"^[a-z0-9]{1,2} (.+)$", n)
    return [n, m.group(1)] if m else [n]


def read_text(image: Path) -> list[dict[str, Any]]:
    """Every line of text in an image, with its box in the image's pixels. Uses Apple's Vision framework."""
    if not IS_MAC:
        return []
    try:
        import Vision  # pyobjc-framework-Vision, installed on macOS with this package
        from Foundation import NSURL
    except ImportError as exc:
        raise WindowError("Reading the window needs pyobjc-framework-Vision (pip install pyobjc-framework-Vision).") from exc
    from PIL import Image
    with Image.open(image) as im:
        w, h = im.size
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(image)), None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return []
    items = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        bb = obs.boundingBox()  # normalised, origin bottom-left
        x, y = bb.origin.x * w, (1 - bb.origin.y - bb.size.height) * h
        items.append({"text": str(cand[0].string()), "conf": round(float(cand[0].confidence()), 2),
                      "box": [round(x), round(y), round(bb.size.width * w), round(bb.size.height * h)]})
    items.sort(key=lambda i: (i["box"][1] // 8, i["box"][0]))
    return items


def compact(items: list[dict[str, Any]]) -> list[list]:
    """[text, centre x, centre y] per line: what the agent needs to read the window and click by position."""
    return [[i["text"], i["box"][0] + i["box"][2] // 2, i["box"][1] + i["box"][3] // 2] for i in items]


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s*[|·•]\s*|\s{2,}", text) if w.strip()]


def guarded_label(text: str) -> str | None:
    """The guarded action a label stands for, if any: the whole label, or one part of a 'Skip | Pause | End' row."""
    for part in [text, *_words(text)]:
        for n in variants(part):
            if n in GUARDED or n.startswith(("start print", "send ", "delete ", "remove ", "discard ")):
                return n
    return None


def guarded_at(items: list[dict[str, Any]], x: float, y: float, pad: int = 6, icon: int = 30) -> str | None:
    """The guarded label under (x, y), counting a button's icon just left of its text and a little margin."""
    for i in items:
        bx, by, bw, bh = i["box"]
        if not (bx - icon - pad <= x <= bx + bw + pad and by - pad <= y <= by + bh + pad):
            continue
        t = i["text"]
        parts = _words(t)
        if len(parts) > 1:  # a 'Skip | Pause | End' row: find the part under the point by its share of the line
            for part in parts:
                k = t.find(part)
                x0, x1 = bx + bw * k / len(t), bx + bw * (k + len(part)) / len(t)
                if x0 - icon <= x <= x1 + pad and guarded_label(part):
                    return guarded_label(part)
        g = guarded_label(t)
        if g:
            return g
    return None


def find_text(items: list[dict[str, Any]], query: str, occurrence: int | None = None) -> dict[str, Any]:
    """Where to click for a label. Exact matches (ignoring icons and case) win over lines that contain the text.
    Returns the point and the matched line; raises if nothing or more than one thing matches."""
    q = norm(query)
    if not q:
        raise WindowError("Give the text of the button or label to click.")
    exact = [i for i in items if q in variants(i["text"])]
    parts = []
    if not exact:
        for i in items:
            t = i["text"]
            k = t.lower().find(query.strip().lower())
            if k < 0:
                continue
            bx, by, bw, bh = i["box"]  # aim at the matched part of a longer line
            mid = (k + len(query.strip()) / 2) / max(len(t), 1)
            parts.append({**i, "point": [round(bx + bw * mid), by + bh // 2]})
    hits = [{**i, "point": [i["box"][0] + i["box"][2] // 2, i["box"][1] + i["box"][3] // 2]} for i in exact] or parts
    if not hits:
        raise WindowError(f"No text matching '{query}' in the slicer window. Look at slicer_screenshot's text list.")
    if len(hits) > 1 and occurrence is None:
        where = "; ".join(f"{n + 1}: '{h['text']}' at {h['point']}" for n, h in enumerate(hits[:8]))
        raise WindowError(f"'{query}' appears {len(hits)} times ({where}). Pass occurrence, or click by x/y.")
    n = occurrence or 1
    if not 1 <= n <= len(hits):
        raise WindowError(f"occurrence must be 1 to {len(hits)}.")
    return hits[n - 1]


def has(items: list[dict[str, Any]], *texts: str) -> bool:
    lines = [norm(i["text"]) for i in items]
    return any(any(norm(t) in line for line in lines) for t in texts)


def press(install: Install, key: str) -> dict[str, Any]:
    if key not in KEYS:
        raise WindowError(f"key must be one of: {', '.join(KEYS)}")
    if not _accessibility_ok():
        raise WindowError("Key presses need Accessibility permission for the app running this server.")
    slicer_window(install)
    top = next(iter(_windows_front_to_back()), None)
    if top is None or not _is_slicer(top):
        raise WindowError("Another window is in front of the slicer, so the key was not sent.")
    Q = _quartz()
    for down in (True, False):
        Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateKeyboardEvent(None, KEYS[key], down))
        time.sleep(0.05)
    time.sleep(0.6)
    return {"pressed": key}
