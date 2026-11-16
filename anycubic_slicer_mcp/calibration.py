"""Generate calibration models: temperature tower, flow-rate cubes, retraction towers, first-layer test."""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import numpy as np

from .mesh import Mesh

_BOX_FACES = np.array([
    [0, 2, 1], [0, 3, 2],  # bottom (z0), facing down
    [4, 5, 6], [4, 6, 7],  # top
    [0, 1, 5], [0, 5, 4],  # front (y0)
    [1, 2, 6], [1, 6, 5],  # right
    [2, 3, 7], [2, 7, 6],  # back
    [3, 0, 4], [3, 4, 7],  # left
], dtype=np.int64)


def box(x: float, y: float, z: float, dx: float, dy: float, dz: float) -> Mesh:
    v = np.array([[x, y, z], [x + dx, y, z], [x + dx, y + dy, z], [x, y + dy, z],
                  [x, y, z + dz], [x + dx, y, z + dz], [x + dx, y + dy, z + dz], [x, y + dy, z + dz]], float)
    return Mesh(v, _BOX_FACES.copy())


def combine(parts: list[Mesh]) -> Mesh:
    verts, faces, off = [], [], 0
    for p in parts:
        verts.append(p.vertices); faces.append(p.faces + off); off += len(p.vertices)
    return Mesh(np.vstack(verts), np.vstack(faces))


def temperature_tower(temps: list[int], block_h: float = 8.0, base_h: float = 1.2) -> tuple[Mesh, list[tuple[float, str]]]:
    """A stack of blocks, one per temperature (hottest at the bottom). Each block has two pillars and a
    bridge, so stringing, bridging and surface finish can be compared between temperatures."""
    if not 2 <= len(temps) <= 12:
        raise ValueError("Give 2 to 12 temperatures.")
    parts = [box(0, 0, 0, 40, 12, base_h)]
    commands = []
    for i, t in enumerate(temps):
        z0 = base_h + i * block_h
        parts += [box(2, 1, z0, 9, 10, block_h), box(29, 1, z0, 9, 10, block_h),  # pillars
                  box(11, 1, z0 + block_h - 1.6, 18, 10, 1.6),                     # bridge
                  box(18, 4, z0, 4, 4, block_h - 1.6)]                              # spire (stringing)
        commands.append((round(z0 + 0.01, 3), f"M104 S{int(t)}"))
    return combine(parts), commands


def flow_cube() -> Mesh:
    return box(0, 0, 0, 24, 24, 4)


def retraction_towers() -> Mesh:
    return combine([box(0, 0, 0, 50, 8, 0.6), box(2, 1, 0.6, 6, 6, 30), box(42, 1, 0.6, 6, 6, 30)])


def first_layer_test(bed: tuple[float, float, float], size: float = 30.0, margin: float = 20.0) -> Mesh:
    """Five single-layer squares (centre and corners) in bed coordinates: slice with arrange off."""
    bx, by = bed[0], bed[1]
    spots = [(margin, margin), (bx - margin - size, margin), (margin, by - margin - size),
             (bx - margin - size, by - margin - size), ((bx - size) / 2, (by - size) / 2)]
    return combine([box(x, y, 0, size, size, 0.2) for x, y in spots])


def replace_gcode_in_3mf(three_mf: Path, gcode: Path, plate: int = 1) -> None:
    """After editing a plate's G-code, put it back into the printer-ready .gcode.3mf (with its md5)."""
    entry = f"Metadata/plate_{plate}.gcode"
    data = gcode.read_bytes()
    tmp = three_mf.with_suffix(".tmp")
    with zipfile.ZipFile(three_mf) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == entry:
                zout.writestr(item, data)
            elif item.filename == entry + ".md5":
                zout.writestr(item, hashlib.md5(data).hexdigest().upper())
            else:
                zout.writestr(item, zin.read(item))
    shutil.move(str(tmp), three_mf)
