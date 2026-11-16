"""Draw the website's faded isometric blueprint background and write it into docs/index.html.

    python3 docs/tools/make_blueprint.py

Everything between <!-- blueprint:start --> and <!-- blueprint:end --> in index.html is replaced.
The drawing is plain SVG: an isometric grid that drifts slowly, and wireframe parts (a gear, a filament
spool, a box, an L-bracket) with dimension lines that draw themselves in, hold, and fade, one after another.
Hidden edges are dashed, like a real drawing.
"""
from __future__ import annotations

import math
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "index.html"
C30, S30 = math.cos(math.radians(30)), math.sin(math.radians(30))
CELL = 24.0                                      # grid spacing along the iso axes


def iso(x: float, y: float, z: float, ox: float, oy: float) -> tuple[float, float]:
    return ox + (x - y) * C30, oy + (x + y) * S30 - z


def f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def path(points: list[tuple[float, float]], closed: bool = False) -> str:
    d = "M" + " L".join(f"{f(x)},{f(y)}" for x, y in points)
    return d + (" Z" if closed else "")


def draw(d: str) -> str:
    return f'<path class="draw" pathLength="1" d="{d}"/>'


def ghost(d: str) -> str:
    return f'<path class="ghost" d="{d}"/>'


def label(x: float, y: float, text: str, anchor: str = "middle") -> str:
    return f'<text class="lbl" x="{f(x)}" y="{f(y)}" text-anchor="{anchor}">{text}</text>'


def dim(a: tuple[float, float], b: tuple[float, float], text: str, off: tuple[float, float]) -> str:
    """A dimension line from a to b with end ticks and a label, pushed out by off."""
    (ax, ay), (bx, by) = (a[0] + off[0], a[1] + off[1]), (b[0] + off[0], b[1] + off[1])
    nx, ny = by - ay, ax - bx
    n = math.hypot(nx, ny) or 1
    tx, ty = nx / n * 5, ny / n * 5
    parts = [
        draw(path([(ax, ay), (bx, by)])),
        draw(path([(ax - tx, ay - ty), (ax + tx, ay + ty)])),
        draw(path([(bx - tx, by - ty), (bx + tx, by + ty)])),
        ghost(path([a, (ax, ay)])), ghost(path([b, (bx, by)])),
    ]
    mx, my = (ax + bx) / 2, (ay + by) / 2
    return "".join(parts) + label(mx + off[0] * 0.5, my + off[1] * 0.5 + 4, text)


def box(ox: float, oy: float, w: float, d: float, h: float) -> list[str]:
    P = lambda x, y, z: iso(x, y, z, ox, oy)
    top = [P(0, 0, h), P(w, 0, h), P(w, d, h), P(0, d, h)]
    out = [draw(path(top, True)),
           draw(path([P(w, 0, h), P(w, 0, 0), P(w, d, 0), P(0, d, 0), P(0, d, h)])),
           draw(path([P(w, d, h), P(w, d, 0)])),
           ghost(path([P(0, 0, h), P(0, 0, 0), P(w, 0, 0)])), ghost(path([P(0, 0, 0), P(0, d, 0)]))]
    return out


def ellipse_pts(cx: float, cy: float, r: float, z: float, ox: float, oy: float,
                a0: float = 0, a1: float = 360, n: int = 48) -> list[tuple[float, float]]:
    return [iso(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a)), z, ox, oy)
            for a in [a0 + (a1 - a0) * i / n for i in range(n + 1)]]


def cylinder(ox: float, oy: float, r: float, z0: float, h: float) -> list[str]:
    top = ellipse_pts(0, 0, r, z0 + h, ox, oy)
    front = ellipse_pts(0, 0, r, z0, ox, oy, -45, 135)
    back = ellipse_pts(0, 0, r, z0, ox, oy, 135, 315)
    left, right = iso(r * math.cos(math.radians(135)), r * math.sin(math.radians(135)), 0, ox, oy), \
        iso(r * math.cos(math.radians(-45)), r * math.sin(math.radians(-45)), 0, ox, oy)
    return [draw(path(top, True)), draw(path(front)), ghost(path(back)),
            draw(path([(left[0], left[1] - z0 - h), (left[0], left[1] - z0)])),
            draw(path([(right[0], right[1] - z0 - h), (right[0], right[1] - z0)]))]


def gear(ox: float, oy: float, s: float, h: float) -> list[str]:
    """The site's 24-tooth gear, top outline drawn, bottom outline as a hidden edge."""
    teeth, root, tip, half = 24, 26.0 * s, 30.0 * s, 1.8 * s
    def outline(z: float) -> list[tuple[float, float]]:
        pts = []
        step = 2 * math.pi / teeth
        hr, ht = math.asin(half / root), math.asin(half / tip)
        for i in range(teeth):
            a = i * step
            for r, t in ((root, a - hr), (tip, a - ht), (tip, a + ht), (root, a + hr)):
                pts.append(iso(r * math.cos(t), r * math.sin(t), z, ox, oy))
            for k in range(1, 3):
                t = a + hr + (step - 2 * hr) * k / 3
                pts.append(iso(root * math.cos(t), root * math.sin(t), z, ox, oy))
        return pts
    out = [draw(path(outline(h), True)), ghost(path(outline(0), True)),
           draw(path(ellipse_pts(0, 0, 4.6 * s, h, ox, oy), True))]
    for k in range(6):
        a = math.radians(60 * k)
        out.append(draw(path(ellipse_pts(16 * s * math.cos(a), 16 * s * math.sin(a), 4.6 * s, h, ox, oy, n=24), True)))
    l, r = iso(tip * math.cos(math.radians(135)), tip * math.sin(math.radians(135)), 0, ox, oy), \
        iso(tip * math.cos(math.radians(-45)), tip * math.sin(math.radians(-45)), 0, ox, oy)
    out += [draw(path([(l[0], l[1] - h), l])), draw(path([(r[0], r[1] - h), r]))]
    return out


def spool(ox: float, oy: float) -> list[str]:
    out = cylinder(ox, oy, 74, 0, 7) + cylinder(ox, oy, 30, 7, 46) + cylinder(ox, oy, 74, 53, 7)
    out.append(draw(path(ellipse_pts(0, 0, 14, 60, ox, oy), True)))
    for k in range(5):  # a few wound filament lines on the core
        z = 12 + k * 8
        out.append(ghost(path(ellipse_pts(0, 0, 44, z, ox, oy, -45, 135))))
    return out


def bracket(ox: float, oy: float) -> list[str]:
    return box(ox, oy, 110, 40, 10) + box(ox + 0 * C30, oy - 10, 14, 40, 70)


def crosshair(x: float, y: float) -> str:
    return draw(path([(x - 7, y), (x + 7, y)])) + draw(path([(x, y - 7), (x, y + 7)]))


def group(delay: float, parts: list[str]) -> str:
    return f'<g class="obj" style="--d:{delay}s">' + "".join(parts) + "</g>"


def svg() -> str:
    W, H = 1600, 1000
    pw, ph = 2 * CELL * C30, CELL
    groups = []
    # gear, upper left
    gx, gy = 210, 250
    g = gear(gx, gy, 3.0, 26)
    g.append(dim(iso(-90, 0, 0, gx, gy), iso(90, 0, 0, gx, gy), "Ø 60.0", (0, 70)))
    g.append(label(gx + 120, gy - 92, "GEAR · 24T · 0.2 MM LAYERS", "start"))
    groups.append(group(0, g))
    # box, upper right
    bx, by = 1330, 150
    b = box(bx, by, 150, 110, 60)
    b.append(dim(iso(0, 110, 0, bx, by), iso(150, 110, 0, bx, by), "150.0", (-14, 26)))
    b.append(dim(iso(150, 0, 0, bx, by), iso(150, 110, 0, bx, by), "110.0", (24, 12)))
    groups.append(group(4, b))
    # spool, lower left
    sx, sy = 230, 720
    s = spool(sx, sy)
    s.append(label(sx + 110, sy + 70, "SPOOL · 1 KG · Ø 1.75", "start"))
    groups.append(group(8, s))
    # L-bracket, lower right
    lx, ly = 1300, 700
    l = bracket(lx, ly)
    l.append(dim(iso(0, 40, 0, lx, ly), iso(110, 40, 0, lx, ly), "110.0", (-14, 26)))
    l.append(label(lx + 40, ly - 140, "BRACKET · 5 WALLS · 25% GYROID", "start"))
    groups.append(group(12, l))
    # build plate corner marks and crosshairs, always faint
    marks = "".join(crosshair(x, y) for x, y in ((560, 120), (1060, 120), (560, 900), (1060, 900), (800, 520)))
    groups.append(group(2, [marks]))
    title = ('<g class="title-block">'
             f'<rect x="{W - 300}" y="{H - 96}" width="270" height="70"/>'
             f'<path d="M{W - 300},{H - 61} H{W - 30} M{W - 160},{H - 96} V{H - 26}"/>'
             f'<text x="{W - 290}" y="{H - 73}">ANYCUBIC SLICER MCP</text>'
             f'<text x="{W - 290}" y="{H - 38}">SCALE 1:1</text>'
             f'<text x="{W - 150}" y="{H - 73}">KOBRA X</text>'
             f'<text x="{W - 150}" y="{H - 38}">SHEET 1 / 1</text></g>')
    return (f'<svg class="bp" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">'
            f'<defs><pattern id="bpGrid" width="{f(pw)}" height="{f(ph)}" patternUnits="userSpaceOnUse">'
            f'<path d="M0,0 L{f(pw)},{f(ph)} M0,{f(ph)} L{f(pw)},0 M0,0 V{f(ph)}"/>'
            f'<animateTransform attributeName="patternTransform" type="translate" from="0 0" to="{f(pw)} {f(ph)}" dur="40s" repeatCount="indefinite"/>'
            f'</pattern><pattern id="bpGridMajor" width="{f(pw * 5)}" height="{f(ph * 5)}" patternUnits="userSpaceOnUse">'
            f'<path d="M0,0 L{f(pw * 5)},{f(ph * 5)} M0,{f(ph * 5)} L{f(pw * 5)},0"/>'
            f'<animateTransform attributeName="patternTransform" type="translate" from="0 0" to="{f(pw)} {f(ph)}" dur="40s" repeatCount="indefinite"/>'
            f'</pattern></defs>'
            f'<rect class="grid" width="{W}" height="{H}" fill="url(#bpGrid)"/>'
            f'<rect class="grid major" width="{W}" height="{H}" fill="url(#bpGridMajor)"/>'
            + "".join(groups) + title + "</svg>")


def main() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start, end = "<!-- blueprint:start -->", "<!-- blueprint:end -->"
    a, b = html.index(start) + len(start), html.index(end)
    markup = f'\n<div class="blueprint" aria-hidden="true">{svg()}</div>\n'
    INDEX.write_text(html[:a] + markup + html[b:], encoding="utf-8")
    print(f"blueprint: {len(markup) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
