"""Generate examples/gear.stl: a 24-tooth spur gear (Ø60 mm) with a hub, keyed bore and lightening holes.

Used for the README / site previews. Regenerate with build123d (Python 3.10-3.14):
    uv run --python 3.13 --with build123d python examples/make_gear.py
"""
from pathlib import Path

from build123d import (Align, Box, BuildPart, BuildSketch, Circle, Cylinder, Hole, Locations, Mode,
                       PolarLocations, Rectangle, export_stl, extrude)

TEETH = 24
ROOT_R = 26.0        # mm, root circle radius
TOOTH_H = 4.0        # mm, radial tooth height
TOOTH_W = 3.6        # mm, tooth width at the root
THICKNESS = 6.0      # mm
HUB_R, HUB_H = 9.0, 3.0
BORE_D = 8.0
LIGHTENING = 6       # holes between hub and rim

with BuildPart() as gear:
    with BuildSketch():
        Circle(ROOT_R)
        with PolarLocations(ROOT_R + TOOTH_H / 2 - 0.4, TEETH):
            Rectangle(TOOTH_H + 0.8, TOOTH_W)
        with PolarLocations(16.0, LIGHTENING):
            Circle(4.6, mode=Mode.SUBTRACT)
    extrude(amount=THICKNESS)
    with Locations((0, 0, THICKNESS)):
        Cylinder(HUB_R, HUB_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
    with Locations((0, 0, THICKNESS + HUB_H)):
        Hole(radius=BORE_D / 2)
    with Locations((BORE_D / 2, 0, (THICKNESS + HUB_H) / 2)):
        Box(2.0, 2.4, THICKNESS + HUB_H, mode=Mode.SUBTRACT)  # keyway

part = gear.part
out = Path(__file__).with_name("gear.stl")
export_stl(part, str(out))
bb = part.bounding_box()
print(f"{out.name}: {bb.size.X:.1f} x {bb.size.Y:.1f} x {bb.size.Z:.1f} mm, {part.volume:.0f} mm3, valid={part.is_valid}")
