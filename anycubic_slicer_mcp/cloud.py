"""Printing and monitoring through Anycubic Cloud, by driving the slicer window.

When a printer's LAN mode is off it talks only to Anycubic Cloud, and the slicer window (signed in to the user's
account) is what reaches it. This module never touches the account or its tokens: it reads the window with OCR and
clicks the same buttons the user would. macOS only, like the window screenshots and clicks it builds on.

Two screens matter:
- Workbench (Cloud Printer tab): the printer's state, the job, temperatures, fan, speed and the loaded slots.
- The Start Print dialog (Remote Print button in Prepare or Preview): the task name, time, weight, filaments and the
  printer it will go to.
"""
from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any

from . import window as win
from .install import Install

MATERIALS = {"pla", "pla+", "petg", "pet", "abs", "asa", "tpu", "pa", "pc", "pva", "hips", "pla-cf", "petg-cf",
             "pa-cf", "silk", "matte"}
PRINTER_STATES = {"busy", "idle", "free", "online", "offline", "printing", "paused", "ready", "error", "standby"}
JOB_STATUS = re.compile(r"^(printing|paused|pausing|preparing|heating|preheating|leveling|levelling|downloading|"
                        r"uploading|completed|complete|finished|failed|cancell?ed|stopped|resuming)\b", re.IGNORECASE)
TEMPS = re.compile(r"(-?\d+)\s*°?\s*C?\s*/\s*(-?\d+)")
TIME = re.compile(r"^(?:\d+d\s*)?(?:\d+h\s*)?\d+m(?:\s*\d+s)?$|^\d+s$")
WEIGHT = re.compile(r"([\d.]+)\s*g\s*/\s*([\d.]+)\s*m")
SHOT = Path(tempfile.gettempdir()) / "acmcp-slicer-window.png"


# ------------------------------------------------------------------ reading
def capture(install: Install, out: Path = SHOT) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = win.screenshot(install, out, ocr=True)
    return meta, meta.pop("items")


def _label(items, pred) -> dict | None:
    return next((i for i in items if any(pred(v) for v in win.variants(i["text"]))), None)


def _below(items, label: dict | None, dx: int = 60, dy: int = 45) -> str | None:
    if not label:
        return None
    lx, ly = label["box"][0], label["box"][1] + label["box"][3]
    cands = [i for i in items if i is not label and abs(i["box"][0] - lx) <= dx and -2 <= i["box"][1] - ly <= dy]
    return min(cands, key=lambda i: (i["box"][1], abs(i["box"][0] - lx)))["text"] if cands else None


def _temps(text: str | None) -> dict | None:
    m = TEMPS.search(text or "")
    return {"now_c": int(m.group(1)), "target_c": int(m.group(2))} if m else None


def parse_workbench(items: list[dict[str, Any]]) -> dict[str, Any]:
    """What the Workbench screen says, best effort. The screenshot is the ground truth."""
    out: dict[str, Any] = {}
    state = next((i for i in items if win.norm(i["text"]) in PRINTER_STATES), None)
    if state:
        out["printer_state"] = win.norm(state["text"])
        sx, sy = state["box"][0], state["box"][1]
        near = [i for i in items if i is not state and (
            (abs(i["box"][0] - sx) <= 25 and 0 < sy - i["box"][1] <= 40) or  # name above the state
            (abs(i["box"][1] - sy) <= 8 and i["box"][0] < sx))]               # or left of it on the same row
        if near:
            out["printer"] = min(near, key=lambda i: abs(i["box"][0] - sx) + abs(i["box"][1] - sy))["text"]
    task = _label(items, lambda v: v == "print task")
    if task:
        tx, ty = task["box"][0], task["box"][1]
        rows = [i for i in items if tx + 60 < i["box"][0] < tx + 400 and 20 <= i["box"][1] - ty <= 100
                and len(i["text"]) > 12]
        if rows:
            out["job"] = max(rows, key=lambda i: len(i["text"]))["text"]
    status = next((i for i in items if JOB_STATUS.match(win.norm(i["text"]))), None)
    if status:
        out["job_status"] = win.norm(status["text"]).rstrip(".")
        sx, sy = status["box"][0], status["box"][1]
        pct = [i for i in items if re.fullmatch(r"\d{1,3}%", win.norm(i["text"])) and abs(i["box"][0] - sx) <= 40
               and 0 < i["box"][1] - sy <= 60]
        if pct:
            out["progress_percent"] = int(win.norm(pct[0]["text"])[:-1])
    layers = _below(items, _label(items, lambda v: v == "layers"))
    if layers and (m := re.match(r"(\d+)\s*/\s*(\d+)", layers)):
        out["layer"], out["layers"] = int(m.group(1)), int(m.group(2))
    rem = _below(items, _label(items, lambda v: v == "remaining"))
    if rem:
        left, _, total = rem.partition("/")
        out["remaining"], out["total_time"] = left.strip(), (total.strip() or None)
    eta = _below(items, _label(items, lambda v: v.startswith("est. completion")))
    if eta:
        out["estimated_completion"] = eta
    noz = _temps(_below(items, _label(items, lambda v: v == "nozzle")))
    bed = _temps(_below(items, _label(items, lambda v: v in ("hotbed", "bed", "heatbed"))))
    if noz:
        out["nozzle"] = noz
    if bed:
        out["bed"] = bed
    fan = _below(items, _label(items, lambda v: v.startswith("model") and "fan" in v or v == "model ..."))
    if fan and re.fullmatch(r"\d{1,3}%", win.norm(fan)):
        out["part_fan_percent"] = int(win.norm(fan)[:-1])
    speed = _below(items, _label(items, lambda v: v.startswith("speed")))
    if speed:
        out["speed_mode"] = speed
    fw = next((i["text"] for i in items if win.norm(i["text"]).startswith("firmware")), None)
    if fw:
        out["firmware"] = re.sub(r"(?i)^firmware\s*", "", fw).strip()
    mats = sorted((i for i in items if win.norm(i["text"]) in MATERIALS and _in_rack(items, i)), key=lambda i: i["box"][0])
    if mats:
        slots = []
        for n, m in enumerate(mats, 1):
            mx, my = m["box"][0] + m["box"][2] / 2, m["box"][1]
            num = next((int(i["text"]) for i in items if i["text"].strip().isdigit() and len(i["text"].strip()) == 1
                        and abs(i["box"][0] + i["box"][2] / 2 - mx) <= 20 and 0 < i["box"][1] - my <= 40), None)
            slots.append({"slot": num or n, "material": m["text"].strip()})
        out["slots"] = slots
        if len(slots) < 4:
            out["slots_note"] = "Fewer than 4 slots read; numbering may be off. Check the screenshot."
    return out


def _in_rack(items, mat) -> bool:
    """Material labels under the Material Management panel, not the ones in a dialog or the filament list."""
    head = _label(items, lambda v: v.startswith("material management") or v == "rack")
    return bool(head) and 0 < mat["box"][1] - head["box"][1] <= 260 and mat["box"][0] >= head["box"][0] - 40


def dialog_items(items: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The Start Print dialog's lines, or None when it isn't open."""
    task = _label(items, lambda v: v.startswith("printing task"))
    starts = [i for i in items if "start print" in win.variants(i["text"])]
    cancel = [i for i in items if "cancel" in win.variants(i["text"])]
    if not task or not cancel:
        return None
    button = max(starts, key=lambda i: i["box"][1]) if starts else None
    bottom = max(c["box"][1] + c["box"][3] for c in cancel)
    left = task["box"][0] - 80
    right = (button["box"][0] + button["box"][2] + 80) if button else task["box"][0] + 700
    top = task["box"][1] - 10
    return [i for i in items if left <= i["box"][0] <= right and top <= i["box"][1] <= bottom + 5]


def parse_dialog(items: list[dict[str, Any]]) -> dict[str, Any]:
    d = dialog_items(items) or []
    out: dict[str, Any] = {"open": bool(d)}
    if not d:
        return out
    task = _label(d, lambda v: v.startswith("printing task"))
    name = task["text"].split(":", 1)[1].strip() if task and ":" in task["text"] else ""
    if not name and task:
        right = [i for i in d if abs(i["box"][1] - task["box"][1]) <= 8 and i["box"][0] > task["box"][0]]
        name = min(right, key=lambda i: i["box"][0])["text"] if right else ""
    out["task"] = name or None
    sliced_for = next((i["text"] for i in d if win.norm(i["text"]).startswith("for ")), None)
    out["sliced_for"] = sliced_for[4:].strip() if sliced_for else None
    out["time"] = next((i["text"].strip() for i in d if TIME.match(i["text"].strip())), None)
    w = next((WEIGHT.search(i["text"]) for i in d if WEIGHT.search(i["text"])), None)
    out["weight_g"], out["length_m"] = (float(w.group(1)), float(w.group(2))) if w else (None, None)
    out["filaments"] = [i["text"].strip() for i in d if win.norm(i["text"]) in MATERIALS]
    out["no_printers"] = win.has(d, "no available printers")
    status_words = PRINTER_STATES | {"disconnected", "connected", "connecting"}
    out["printer_status"] = next((win.norm(i["text"]) for i in d if win.norm(i["text"]) in status_words), None)
    pick = _label(d, lambda v: v.startswith("please select a printer") or v == "printer")
    if pick:
        py = pick["box"][1]
        btn_y = max((i["box"][1] for i in d if "cancel" in win.variants(i["text"])), default=py + 400)
        out["printer_area"] = [i["text"] for i in d if py < i["box"][1] < btn_y - 10]
    starts = [i for i in d if "start print" in win.variants(i["text"])]
    out["start_button"] = max(starts, key=lambda i: i["box"][1]) if starts else None
    return out


def button_enabled(image: Path, box: list[int]) -> bool | None:
    """A disabled Start Print button is pale; an enabled one is saturated blue. None if it can't tell."""
    try:
        from PIL import Image
        with Image.open(image) as im:
            x, y, w, h = box
            px = list(im.convert("RGB").crop((x - 20, y - 4, x + w + 20, y + h + 4)).getdata())
    except Exception:  # noqa: BLE001
        return None
    if not px:
        return None
    r = sum(p[0] for p in px) / len(px)
    b = sum(p[2] for p in px) / len(px)
    if b - r > 90:
        return True
    if b - r < 60:
        return False
    return None


# ------------------------------------------------------------------ actions
def _click_text(install: Install, meta, items, text: str) -> None:
    hit = win.find_text(items, text)
    win.click(install, hit["point"][0], hit["point"][1], tuple(meta["image_size"]))


def workbench(install: Install) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Switch the window to Workbench > Cloud Printer and read it."""
    meta, items = capture(install)
    if parse_dialog(items)["open"]:
        raise win.WindowError("The Start Print dialog is open. Cancel it (slicer_click text='Cancel') or finish "
                              "print_from_slicer first.")
    if not win.has(items, "workbench"):
        raise win.WindowError("No Workbench tab in the slicer window. A dialog may be open: look at slicer_screenshot.")
    _click_text(install, meta, items, "Workbench")
    time.sleep(1.5)
    meta, items = capture(install)
    if any("cloud printer" in win.variants(i["text"]) for i in items):
        _click_text(install, meta, items, "Cloud Printer")
        time.sleep(2.0)
        meta, items = capture(install)
    data = parse_workbench(items)
    if not data.get("printer_state") and win.has(items, "lan printer"):
        data["note"] = ("No cloud printer is listed. If the printer's LAN mode is on, it isn't on Anycubic Cloud: use "
                        "printer_status instead. Otherwise check the slicer is signed in to the account the printer is bound to.")
    return meta, items, {**data, "text": win.compact(items)}


def _open_dialog(install: Install, wait_s: float = 90.0) -> tuple[dict, list]:
    meta, items = capture(install)
    if parse_dialog(items)["open"]:
        return meta, items
    if not win.has(items, "remote print"):
        tab = next((t for t in ("Preview", "Prepare") if any(t.lower() in win.variants(i["text"]) for i in items)), None)
        if tab:
            _click_text(install, meta, items, tab)
            time.sleep(1.5)
            meta, items = capture(install)
    if not win.has(items, "remote print"):
        raise win.WindowError("There's no Remote Print button in the slicer window. Open the model or project there "
                              "(open_in_slicer or restart_slicer with open_path) and slice it first.")
    _click_text(install, meta, items, "Remote Print")
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        time.sleep(2.0)
        meta, items = capture(install)
        if parse_dialog(items)["open"]:
            return meta, items
    raise win.WindowError("The Start Print dialog didn't open. Look at slicer_screenshot: the plate may need slicing, "
                          "or another dialog is asking something.")


def _task_key(name: str | None) -> str:
    """The task name without the MMDD-HHMM-NN_ prefix the window puts on it each time the dialog opens."""
    return re.sub(r"^\d{4}-\d{4}-\d{2}_", "", name or "")


def _public(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in ("start_button", "open")}


def print_from_slicer(install: Install, confirm: bool, expected: dict[str, Any] | None) -> tuple[dict, list, dict]:
    """Open the window's Start Print dialog and read it (preview), or, with confirm, press its Start Print button.
    `expected` is the preview the user agreed to; the dialog must still show the same task and time."""
    meta, items = _open_dialog(install)
    d = parse_dialog(items)
    problems = []
    if d["no_printers"]:
        problems.append("No printer can take the job. It may be busy with another print (check cloud_printer_status), "
                        "in LAN mode (then use send_print), offline, or bound to another account.")
    if d.get("printer_status") in ("busy", "printing", "paused", "offline", "disconnected", "error"):
        problems.append(f"The printer shows as {d['printer_status']}.")
    btn = d.get("start_button")
    enabled = button_enabled(SHOT, btn["box"]) if btn else None
    if btn is None:
        problems.append("No Start Print button found in the dialog.")
    elif enabled is False:
        problems.append("The Start Print button is greyed out (no printer selected, or the printer can't take a job).")
    summary = {**_public(d), "start_enabled": enabled}
    if not confirm:
        return meta, items, {"started": False, "confirm_required": True, "dialog": summary, "problems": problems,
                      "will": "Press Start Print in the slicer's dialog: the slicer uploads this plate through Anycubic "
                              "Cloud and the printer starts heating and printing it.",
                      "check_first": "Show the user the dialog and these numbers, ask them to make sure the bed is empty, "
                                     "and call again with confirm=True only after an explicit yes."}
    if expected is None:
        raise win.WindowError("Call print_from_slicer without confirm first and show the user the preview.")
    if (_task_key(d.get("task")), d.get("time")) != (_task_key(expected.get("task")), expected.get("time")):
        raise win.WindowError(f"The dialog now shows '{d.get('task')}' ({d.get('time')}), not the previewed "
                              f"'{expected.get('task')}' ({expected.get('time')}). Preview again and ask the user.")
    if problems:
        raise win.WindowError("Not starting: " + " ".join(problems))
    win.click(install, btn["box"][0] + btn["box"][2] // 2, btn["box"][1] + btn["box"][3] // 2, tuple(meta["image_size"]))
    end = time.monotonic() + 120
    closed = False
    while time.monotonic() < end:
        time.sleep(2.0)
        meta, items = capture(install)
        if not parse_dialog(items)["open"]:
            closed = True
            break
    return meta, items, {"started": closed, "task": d.get("task"), "time": d.get("time"),
                  "note": ("The slicer took the job. Check with cloud_printer_status in a minute that the printer is "
                           "heating or printing.") if closed else
                          ("The dialog is still open after 2 minutes; look at slicer_screenshot for an upload "
                           "error or a question."),
                  "text": win.compact(items)}
