"""Draw the project artwork as SVG (standard library only).

    python3 docs/tools/make_art.py
    rsvg-convert -w 1280 -h 640 docs/assets/hero-static.svg > docs/assets/social-preview.png

Writes to docs/assets/:
  hero.svg         1280x640 README banner; the gear's layers stack up and a nozzle traces the outer wall
  hero-static.svg  the same banner without animation, for the social preview PNG
  gear-stack.svg   the stacked gear on its own, for the website hero (gear-stack-dark.svg for dark mode)

The gear matches examples/make_gear.py: 24 teeth, root R26, tip R30, six 9.2 mm holes, 8 mm keyed bore.
"""
from __future__ import annotations

import math
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "assets"

ACCENT = "#005aff"      # outer wall
MID = "#4d8dff"         # inner wall
LIGHT = "#a8c8ff"       # infill
INK = "#1d1d1f"
GREY = "#6e6e73"
FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Inter, Helvetica, Arial, sans-serif"
MONO = "'SF Mono', ui-monospace, Menlo, Consolas, monospace"

TEETH, ROOT_R, TIP_R, HALF_W = 24, 26.0, 30.0, 1.8
HOLES = [(16.0 * math.cos(math.radians(a)), 16.0 * math.sin(math.radians(a))) for a in range(0, 360, 60)]
HOLE_R, BORE_R = 4.6, 4.0


def f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def gear_outline(scale: float = 1.0) -> str:
    pts: list[tuple[float, float]] = []
    step = 2 * math.pi / TEETH
    hw_root, hw_tip = math.asin(HALF_W / ROOT_R), math.asin(HALF_W / TIP_R)
    for i in range(TEETH):
        a = i * step
        pts += [(ROOT_R * math.cos(a - hw_root), ROOT_R * math.sin(a - hw_root)),
                (TIP_R * math.cos(a - hw_tip), TIP_R * math.sin(a - hw_tip)),
                (TIP_R * math.cos(a + hw_tip), TIP_R * math.sin(a + hw_tip)),
                (ROOT_R * math.cos(a + hw_root), ROOT_R * math.sin(a + hw_root))]
        for k in range(1, 4):
            t = a + hw_root + (step - 2 * hw_root) * k / 4
            pts.append((ROOT_R * math.cos(t), ROOT_R * math.sin(t)))
    return "M" + " L".join(f"{f(x * scale)},{f(y * scale)}" for x, y in pts) + " Z"


def circle_path(cx: float, cy: float, r: float, scale: float) -> str:
    cx, cy, r = cx * scale, cy * scale, r * scale
    return (f"M{f(cx + r)},{f(cy)} A{f(r)},{f(r)} 0 1,0 {f(cx - r)},{f(cy)} "
            f"A{f(r)},{f(r)} 0 1,0 {f(cx + r)},{f(cy)} Z")


def bore_path(scale: float) -> str:
    r, kw = BORE_R * scale, 1.2 * scale
    ang = math.asin(kw / r)
    x0, y0 = r * math.cos(ang), r * math.sin(ang)
    kx = (BORE_R + 1.0) * scale
    return (f"M{f(x0)},{f(-y0)} L{f(kx)},{f(-kw)} L{f(kx)},{f(kw)} L{f(x0)},{f(y0)} "
            f"A{f(r)},{f(r)} 0 1,1 {f(x0)},{f(-y0)} Z")


def infill_paths(scale: float) -> str:
    lines, x = [], -TIP_R
    while x <= TIP_R:
        pts, y = [], -TIP_R
        while y <= TIP_R + 0.01:
            pts.append((x + 0.9 * math.sin(y * 0.55 + x), y))
            y += 0.75
        lines.append("M" + " L".join(f"{f(px * scale)},{f(py * scale)}" for px, py in pts))
        x += 3.4
    return " ".join(lines)


def top_layer(scale: float, prefix: str, animated: bool) -> str:
    outline, bore = gear_outline(scale), bore_path(scale)
    holes = " ".join(circle_path(cx, cy, HOLE_R, scale) for cx, cy in HOLES)
    sw = 0.75 * scale
    a_inf = ('<animate attributeName="stroke-dashoffset" from="2400" to="0" dur="3.2s" begin="0.9s" fill="freeze"/>'
             if animated else "")
    a_wall = ('<animate attributeName="stroke-dashoffset" from="1600" to="0" dur="2.4s" begin="0.5s" fill="freeze"/>'
              if animated else "")
    d_inf = ' stroke-dasharray="2400" stroke-dashoffset="2400"' if animated else ""
    d_wall = ' stroke-dasharray="1600" stroke-dashoffset="1600"' if animated else ""
    rings = "".join(
        f'<path d="{circle_path(cx, cy, HOLE_R, scale)}" stroke="{ACCENT}" stroke-width="{f(sw * 1.3)}"/>'
        f'<path d="{circle_path(cx, cy, HOLE_R + 0.75, scale)}" stroke="{MID}" stroke-width="{f(sw)}"/>'
        for cx, cy in HOLES)
    return f"""
    <clipPath id="{prefix}clip"><path d="{outline} {holes} {bore}" clip-rule="evenodd"/></clipPath>
    <path class="face" d="{outline} {holes} {bore}" fill="#ffffff" fill-rule="evenodd"/>
    <g clip-path="url(#{prefix}clip)">
      <path d="{infill_paths(scale)}" fill="none" stroke="{LIGHT}" stroke-width="{f(sw * 0.9)}" stroke-linecap="round"{d_inf}>{a_inf}</path>
    </g>
    <g fill="none" stroke-linejoin="round">
      <path d="{outline}" stroke="{ACCENT}" stroke-width="{f(sw * 1.4)}"{d_wall}>{a_wall}</path>
      <path d="{gear_outline(scale * 0.955)}" stroke="{MID}" stroke-width="{f(sw)}"/>
      {rings}
      <path d="{bore}" stroke="{ACCENT}" stroke-width="{f(sw * 1.3)}"/>
      <circle r="{f((BORE_R + 0.8) * scale)}" stroke="{MID}" stroke-width="{f(sw)}"/>
    </g>"""


def stack(scale: float, layers: int, animated: bool, prefix: str) -> str:
    out = []
    for i in range(layers):
        y = (layers - 1 - i) * 10
        op = 0.14 + 0.5 * i / max(layers - 1, 1)
        anim = (f'<animate attributeName="opacity" from="0" to="{op:.2f}" dur="0.45s" begin="{0.06 * i:.2f}s" fill="freeze"/>'
                if animated else "")
        out.append(f'<path class="under" d="{gear_outline(scale)}" transform="translate(0,{y})" fill="#f5f8ff" stroke="{ACCENT}" '
                   f'stroke-width="1.3" opacity="{0 if animated else round(op, 2)}">{anim}</path>')
    return "\n    ".join(out) + top_layer(scale, prefix, animated)


def nozzle(scale: float, animated: bool) -> str:
    if animated:
        mo = f'<animateMotion dur="9s" begin="3s" repeatCount="indefinite" path="{gear_outline(scale)}"/>'
        return (f'<circle r="15" fill="{ACCENT}" opacity="0.18">{mo}</circle>'
                f'<circle r="6.5" fill="{ACCENT}">{mo}</circle>')
    x, y = TIP_R * scale * math.cos(math.radians(-55)), TIP_R * scale * math.sin(math.radians(-55))
    return (f'<circle cx="{f(x)}" cy="{f(y)}" r="15" fill="{ACCENT}" opacity="0.18"/>'
            f'<circle cx="{f(x)}" cy="{f(y)}" r="6.5" fill="{ACCENT}"/>')


DARK = """<style>
    @media (prefers-color-scheme: dark) {
      .face { fill: #0d1730; } .under { fill: #0a1226; } .bg { fill: #000000; }
      .ink { fill: #f5f5f7; } .grey { fill: #a1a1a6; } .accent { fill: #3d82ff; }
    }
  </style>"""


def hero(animated: bool = True) -> str:
    W, H, scale = 1280, 640, 7.0
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d">
  <title id="t">Anycubic Slicer MCP</title>
  <desc id="d">An MCP server that lets AI agents slice, preview and plan 3D prints with Anycubic Slicer Next on Windows and macOS. The picture shows one layer of a sliced gear: blue walls and gyroid infill.</desc>
  {DARK if animated else ""}
  <defs>
    <radialGradient id="glow" cx="0.75" cy="0.52" r="0.42"><stop offset="0" stop-color="{ACCENT}" stop-opacity="0.13"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></radialGradient>
  </defs>
  <rect class="bg" width="{W}" height="{H}" fill="#fbfbfd"/>
  <rect width="{W}" height="{H}" fill="url(#glow)"/>
  <g transform="translate(92,236)">
    <text x="0" y="0" font-family="{FONT}" font-size="20" font-weight="600" class="accent" fill="{ACCENT}">MCP server for Anycubic Slicer Next</text>
    <text x="-3" y="74" font-family="{FONT}" font-size="66" font-weight="700" letter-spacing="-2" class="ink" fill="{INK}">Anycubic Slicer MCP</text>
    <text x="0" y="128" font-family="{FONT}" font-size="26" class="grey" fill="{GREY}">Your AI agent slices, checks and tunes</text>
    <text x="0" y="162" font-family="{FONT}" font-size="26" class="grey" fill="{GREY}">your 3D prints, and prints when you say yes.</text>
    <text x="0" y="230" font-family="{MONO}" font-size="16" class="grey" fill="{GREY}">slice_model  ›  30m 48s · 9.66 g PLA · 45 layers</text>
  </g>
  <g transform="translate(985,322) scale(1,0.62) rotate(-8)">
    {stack(scale, 9, animated, "h")}
    {nozzle(scale, animated)}
  </g>
  <text x="{W - 40}" y="{H - 28}" text-anchor="end" font-family="{FONT}" font-size="13" fill="#9a9aa0">Community project. Not affiliated with Anycubic.</text>
</svg>
"""


DARK_FIXED = """<style>
    .face { fill: #0d1730; } .under { fill: #0a1226; }
  </style>"""


def gear_stack(theme: str = "light") -> str:
    """The website hero picture. The page picks the light or dark file to match its own theme toggle,
    so neither file follows the system setting on its own."""
    scale = 8
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="-280 -200 560 420" role="img" aria-label="A gear building up layer by layer: blue walls and gyroid infill">
  {DARK_FIXED if theme == "dark" else ""}
  <defs><radialGradient id="sg" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{ACCENT}" stop-opacity="0.16"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></radialGradient></defs>
  <ellipse cx="0" cy="40" rx="275" ry="160" fill="url(#sg)"/>
  <g transform="scale(1,0.62) rotate(-8)">
    {stack(scale, 9, True, "s")}
    {nozzle(scale, True)}
  </g>
</svg>
"""


if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    files = {"hero.svg": hero(), "hero-static.svg": hero(animated=False), "gear-stack.svg": gear_stack(),
             "gear-stack-dark.svg": gear_stack("dark")}
    for name, svg in files.items():
        (ASSETS / name).write_text(svg, encoding="utf-8")
        print(name, len(svg.encode()), "bytes")
