"""Draw toolpath previews: one layer from above, every layer shaded by height, a side view,
an animated layer GIF, and a shareable print card."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .gcode import FEATURES, Toolpaths

BG = (17, 18, 22)
GRID = (32, 34, 40)
TEXT = (210, 212, 220)
MUTED = (130, 134, 145)
ACCENT = (0, 90, 255)

PALETTE = {
    "Outer wall": (255, 122, 26), "Inner wall": (255, 201, 60), "Overhang wall": (72, 110, 255),
    "Sparse infill": (196, 69, 58), "Internal solid infill": (155, 93, 229), "Solid infill": (155, 93, 229),
    "Top surface": (241, 91, 181), "Bottom surface": (58, 134, 255), "Bridge": (0, 187, 249),
    "Internal Bridge": (0, 160, 220), "Gap infill": (236, 236, 236), "Ironing": (255, 230, 109),
    "Support": (46, 196, 182), "Support interface": (6, 214, 160), "Support transition": (6, 180, 140),
    "Brim": (138, 201, 38), "Skirt": (138, 201, 38), "Prime tower": (160, 160, 168),
    "Wipe tower": (160, 160, 168), "Custom": (90, 90, 96), "Other": (110, 110, 118),
}
COLORS = np.array([PALETTE[f] for f in FEATURES], dtype=np.uint8)


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class _Canvas:
    def __init__(self, tp: Toolpaths, size: int, pad: int = 34):
        x0, y0, x1, y1 = tp.bounds()
        span = max(x1 - x0, y1 - y0, 1.0)
        self.scale = (size - 2 * pad) / span
        self.ox = pad + ((size - 2 * pad) - (x1 - x0) * self.scale) / 2 - x0 * self.scale
        self.oy = pad + ((size - 2 * pad) - (y1 - y0) * self.scale) / 2 + y1 * self.scale
        self.size = size

    def xy(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.ox + x * self.scale, self.oy - y * self.scale  # flip Y: bed front at the bottom


def _draw_segments(draw: ImageDraw.ImageDraw, segs: np.ndarray, canvas: _Canvas, colors, width: int) -> None:
    if not len(segs):
        return
    ax, ay = canvas.xy(segs[:, 0], segs[:, 1])
    bx, by = canvas.xy(segs[:, 2], segs[:, 3])
    for i in range(len(segs)):
        draw.line((float(ax[i]), float(ay[i]), float(bx[i]), float(by[i])), fill=colors[i], width=width)


def _caption(draw: ImageDraw.ImageDraw, text: str, size: int) -> None:
    draw.text((14, size - 26), text, fill=MUTED, font=font(14))


def top_view(tp: Toolpaths, layer: int | None = None, size: int = 900, title: str = "") -> tuple[bytes, dict]:
    """layer=None: every layer, brighter = higher. layer=N (1-based): that layer coloured by feature."""
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)
    canvas = _Canvas(tp, size)
    width = max(1, int(round(0.42 * canvas.scale)))
    meta: dict = {"layers": tp.count}
    if layer is None:
        n = max(tp.count, 1)
        for i, segs in enumerate(tp.layers):
            t = (i + 1) / n  # deep blue at the bed, light blue at the top
            col = tuple(int(lo + (hi - lo) * t) for lo, hi in zip((0, 38, 120), (120, 178, 255)))
            _draw_segments(draw, segs, canvas, [col] * len(segs), width)
        _caption(draw, f"{title} · all {tp.count} layers · brighter = higher", size)
    else:
        if not 1 <= layer <= tp.count:
            raise ValueError(f"layer must be between 1 and {tp.count}")
        segs = tp.layers[layer - 1]
        if layer > 1:  # faint outline of the layer below, for context
            below = tp.layers[layer - 2]
            _draw_segments(draw, below, canvas, [GRID] * len(below), width)
        _draw_segments(draw, segs, canvas, [tuple(COLORS[int(f)]) for f in segs[:, 4]], width)
        feats = {FEATURES[int(f)]: int((segs[:, 4] == f).sum()) for f in np.unique(segs[:, 4])} if len(segs) else {}
        meta.update(layer=layer, z_mm=round(tp.layer_z[layer - 1], 3), features=feats)
        _legend(draw, list(feats), size)
        _caption(draw, f"{title} · layer {layer}/{tp.count} · z {tp.layer_z[layer - 1]:.2f} mm", size)
    return _png(img), meta


def _legend(draw: ImageDraw.ImageDraw, names: list[str], size: int) -> None:
    y = 14
    f = font(13)
    for name in names[:12]:
        draw.rounded_rectangle((size - 190, y + 3, size - 180, y + 13), radius=2, fill=PALETTE.get(name, MUTED))
        draw.text((size - 172, y), name, fill=TEXT, font=f)
        y += 19


def side_view(tp: Toolpaths, size: int = 900, title: str = "") -> tuple[bytes, dict]:
    """Front elevation (X horizontal, Z vertical): every extrusion projected onto the X-Z plane,
    coloured by feature. Shows overhangs, supports and layer stacking."""
    pad = 34
    xs, zs = [], []
    for z, segs in zip(tp.layer_z, tp.layers):
        if len(segs):
            xs.append(segs[:, [0, 2]].ravel()); zs.append(z)
    if not xs:
        raise ValueError("No extrusion moves found.")
    allx = np.concatenate(xs)
    x0, x1 = float(allx.min()), float(allx.max())
    z1 = max(zs)
    span = max(x1 - x0, z1, 1.0)
    scale = (size - 2 * pad) / span
    height = int(min(size, max(260, z1 * scale + 2 * pad + 30)))  # fit flat parts
    img = Image.new("RGB", (size, height), BG)
    draw = ImageDraw.Draw(img)
    ox = pad + ((size - 2 * pad) - (x1 - x0) * scale) / 2 - x0 * scale
    base = height - pad - 10
    h = max(1, int(round(0.2 * scale)))
    for z, segs in zip(tp.layer_z, tp.layers):
        if not len(segs):
            continue
        y = base - z * scale
        order = np.argsort(-segs[:, 1])  # draw back-to-front so the front face wins
        for s in segs[order]:
            a, b = sorted((s[0], s[2]))
            draw.line((ox + a * scale, y, ox + b * scale, y), fill=tuple(COLORS[int(s[4])]), width=h)
    draw.line((pad, base + 2, size - pad, base + 2), fill=GRID, width=2)
    _caption(draw, f"{title} · side view (front) · {tp.count} layers · {z1:.1f} mm tall", height)
    return _png(img), {"layers": tp.count, "height_mm": round(z1, 2)}


def layer_animation(tp: Toolpaths, out: Path, size: int = 520, max_frames: int = 60, ms: int = 90) -> Path:
    """Animated GIF: the print building up layer by layer (current layer coloured, earlier ones dimmed)."""
    canvas = _Canvas(tp, size, pad=24)
    width = max(1, int(round(0.42 * canvas.scale)))
    step = max(1, int(np.ceil(tp.count / max_frames)))
    base = Image.new("RGB", (size, size), BG)
    bdraw = ImageDraw.Draw(base)
    frames = []
    for i in range(0, tp.count, step):
        for j in range(max(0, i - step), i):  # bake earlier layers into the background, dimmed
            segs = tp.layers[j]
            _draw_segments(bdraw, segs, canvas, [tuple(int(c * 0.32) for c in COLORS[int(f)]) for f in segs[:, 4]], width)
        frame = base.copy()
        d = ImageDraw.Draw(frame)
        segs = tp.layers[i]
        _draw_segments(d, segs, canvas, [tuple(COLORS[int(f)]) for f in segs[:, 4]], width)
        d.text((12, size - 24), f"layer {i + 1}/{tp.count}  z {tp.layer_z[i]:.2f} mm", fill=MUTED, font=font(13))
        frames.append(frame.convert("P", palette=Image.ADAPTIVE, colors=128))
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=ms, loop=0, optimize=True)
    return out


def print_card(preview_png: bytes, title: str, rows: list[tuple[str, str]], out: Path) -> Path:
    """1200x630 summary image (works as a social preview): toolpath preview + key numbers."""
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), (250, 250, 252))
    d = ImageDraw.Draw(img)
    prev = Image.open(io.BytesIO(preview_png)).convert("RGB").resize((570, 570))
    mask = Image.new("L", (570, 570), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 569, 569), radius=28, fill=255)
    img.paste(prev, (30, 30), mask)
    x = 640
    d.text((x, 54), title[:34], fill=(29, 29, 31), font=font(40, bold=True))
    d.rectangle((x, 112, x + 64, 116), fill=ACCENT)
    y = 146
    for label, value in rows[:9]:
        d.text((x, y), label, fill=(110, 110, 115), font=font(20))
        d.text((x + 230, y), value, fill=(29, 29, 31), font=font(24, bold=True))
        y += 48
    d.text((x, H - 50), "Sliced with Anycubic Slicer MCP", fill=(150, 150, 155), font=font(17))
    img.save(out, format="PNG", optimize=True)
    return out
