"""Triangle meshes: load STL / OBJ / 3MF, measure, transform, analyse for printing, write STL."""
from __future__ import annotations

import math
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

MESH_SUFFIXES = {".stl", ".obj", ".3mf"}


@dataclass
class Mesh:
    vertices: np.ndarray  # (n, 3) float64, millimetres
    faces: np.ndarray     # (m, 3) int64

    # ------------------------------------------------------------ basic geometry
    @property
    def triangles(self) -> np.ndarray:
        return self.vertices[self.faces]

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if len(self.vertices) == 0:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def size(self) -> np.ndarray:
        lo, hi = self.bounds()
        return hi - lo

    def face_normals_and_areas(self) -> tuple[np.ndarray, np.ndarray]:
        t = self.triangles
        cross = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        norm = np.linalg.norm(cross, axis=1)
        areas = norm / 2.0
        with np.errstate(invalid="ignore", divide="ignore"):
            normals = np.where(norm[:, None] > 0, cross / norm[:, None], 0.0)
        return normals, areas

    def surface_area(self) -> float:
        return float(self.face_normals_and_areas()[1].sum())

    def volume(self) -> float:
        t = self.triangles
        return float(abs(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum()) / 6.0)

    def edge_report(self) -> dict[str, int]:
        """Watertightness: every edge of a closed mesh is shared by exactly two triangles."""
        f = self.faces
        edges = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        return {"open_edges": int((counts == 1).sum()), "overshared_edges": int((counts > 2).sum())}

    def shells(self) -> int:
        """Connected pieces (union-find over shared vertices)."""
        n = len(self.vertices)
        if n == 0:
            return 0
        parent = np.arange(n)

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for a, b, c in self.faces:
            ra, rb, rc = find(a), find(b), find(c)
            parent[rb] = ra
            parent[find(rc)] = ra
        used = np.unique(self.faces)
        return len({find(int(i)) for i in used})

    # ------------------------------------------------------------ transforms
    def transformed(self, matrix: np.ndarray) -> "Mesh":
        m = np.asarray(matrix, dtype=float)
        if m.shape == (3, 3):
            v = self.vertices @ m.T
        else:
            v = (np.c_[self.vertices, np.ones(len(self.vertices))] @ m.T)[:, :3]
        faces = self.faces
        if np.linalg.det(m[:3, :3]) < 0:  # mirroring flips winding
            faces = faces[:, ::-1]
        return Mesh(v, faces.copy())

    def on_bed(self) -> "Mesh":
        """Translate so the lowest point is at Z=0 and the footprint is centred on X/Y=0."""
        lo, hi = self.bounds()
        shift = np.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
        return Mesh(self.vertices + shift, self.faces.copy())

    # ------------------------------------------------------------ print analysis
    def overhangs(self, threshold_deg: float = 45.0, bed_tolerance: float = 0.2) -> dict[str, float]:
        """Downward-facing area steeper than `threshold_deg` from vertical (needs support), excluding
        faces resting on the bed; plus the bed contact area."""
        normals, areas = self.face_normals_and_areas()
        zmin = self.bounds()[0][2]
        centroid_z = self.triangles[:, :, 2].mean(axis=1)
        on_bed = (centroid_z <= zmin + bed_tolerance) & (normals[:, 2] < -0.99)
        limit = -math.cos(math.radians(90.0 - threshold_deg))  # slope below threshold from horizontal
        needs = (normals[:, 2] < limit) & ~on_bed
        return {"overhang_area_mm2": float(areas[needs].sum()), "bed_contact_mm2": float(areas[on_bed].sum())}

    def section_profile(self, z_samples: int = 36, y_samples: int = 48) -> list[tuple[float, float]]:
        """Approximate cross-section area (mm²) at evenly spaced heights, by casting rays along X.
        Narrow layers are where FDM parts usually break (layer bonds are the weakest direction)."""
        lo, hi = self.bounds()
        if hi[2] - lo[2] <= 0 or len(self.faces) == 0:
            return []
        t = self.triangles
        ys = np.linspace(lo[1], hi[1], y_samples + 2)[1:-1]
        dy = (hi[1] - lo[1]) / (y_samples + 1)
        zs = np.linspace(lo[2], hi[2], z_samples + 2)[1:-1]
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        out = []
        for z in zs:
            # triangles spanning this height
            zmin_t = np.minimum(np.minimum(a[:, 2], b[:, 2]), c[:, 2])
            zmax_t = np.maximum(np.maximum(a[:, 2], b[:, 2]), c[:, 2])
            sel = (zmin_t <= z) & (zmax_t >= z)
            if not sel.any():
                out.append((float(z), 0.0))
                continue
            A, B, C = a[sel], b[sel], c[sel]
            area = 0.0
            for y in ys:
                xs = _ray_x_hits(A, B, C, y, z)
                if len(xs) >= 2:
                    xs.sort()
                    pairs = xs[: len(xs) // 2 * 2].reshape(-1, 2)
                    area += float((pairs[:, 1] - pairs[:, 0]).sum()) * dy
            out.append((float(z), area))
        return out

    # ------------------------------------------------------------ output
    def save_stl(self, path: str | Path) -> Path:
        path = Path(path)
        normals, _ = self.face_normals_and_areas()
        t = self.triangles.astype(np.float32)
        with open(path, "wb") as fh:
            fh.write(b"anycubic-slicer-mcp".ljust(80, b" "))
            fh.write(struct.pack("<I", len(self.faces)))
            rec = np.zeros(len(self.faces), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
            rec["n"] = normals.astype(np.float32)
            rec["v"] = t
            fh.write(rec.tobytes())
        return path


def _ray_x_hits(A: np.ndarray, B: np.ndarray, C: np.ndarray, y: float, z: float) -> np.ndarray:
    """X coordinates where the line (y, z) parallel to X crosses the given triangles."""
    # barycentric test in the YZ plane
    v0 = C[:, 1:] - A[:, 1:]
    v1 = B[:, 1:] - A[:, 1:]
    v2 = np.array([y, z]) - A[:, 1:]
    d00 = (v0 * v0).sum(1); d01 = (v0 * v1).sum(1); d11 = (v1 * v1).sum(1)
    d20 = (v2 * v0).sum(1); d21 = (v2 * v1).sum(1)
    den = d00 * d11 - d01 * d01
    ok = np.abs(den) > 1e-12
    with np.errstate(invalid="ignore", divide="ignore"):
        u = (d11 * d20 - d01 * d21) / den
        v = (d00 * d21 - d01 * d20) / den
    with np.errstate(invalid="ignore"):
        inside = ok & (u >= 0) & (v >= 0) & (u + v <= 1)
    xs = A[inside, 0] + u[inside] * (C[inside, 0] - A[inside, 0]) + v[inside] * (B[inside, 0] - A[inside, 0])
    return np.unique(np.round(xs, 5))


# ---------------------------------------------------------------- rotations
def rotation(x_deg: float = 0.0, y_deg: float = 0.0, z_deg: float = 0.0) -> np.ndarray:
    """Rotation matrix applying X, then Y, then Z (degrees)."""
    rx, ry, rz = (math.radians(v) for v in (x_deg, y_deg, z_deg))
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def rotation_to_down(normal: np.ndarray) -> np.ndarray:
    """Rotation that turns `normal` to point straight down (-Z), i.e. puts that face on the bed."""
    n = np.asarray(normal, float) / np.linalg.norm(normal)
    target = np.array([0.0, 0.0, -1.0])
    v = np.cross(n, target)
    c = float(np.dot(n, target))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else rotation(x_deg=180)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))


def euler_xyz(matrix: np.ndarray) -> dict[str, float]:
    """Decompose a rotation (X then Y then Z) into degrees, for reporting and re-use."""
    m = np.asarray(matrix)
    sy = -m[2, 0]
    sy = max(-1.0, min(1.0, sy))
    y = math.asin(sy)
    if abs(math.cos(y)) > 1e-6:
        x = math.atan2(m[2, 1], m[2, 2]); z = math.atan2(m[1, 0], m[0, 0])
    else:
        x = math.atan2(-m[1, 2], m[1, 1]); z = 0.0
    return {k: round(math.degrees(v), 2) + 0.0 for k, v in (("x", x), ("y", y), ("z", z))}


# ---------------------------------------------------------------- loading
def load_mesh(path: str | Path) -> Mesh:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".stl":
        return _load_stl(path)
    if suffix == ".obj":
        return _load_obj(path)
    if suffix == ".3mf":
        return _load_3mf(path)
    raise ValueError(f"Can't read {suffix} files as meshes (supported: STL, OBJ, 3MF).")


def _merge(vertices: np.ndarray, faces: np.ndarray) -> Mesh:
    """Weld identical vertices (STL stores each triangle's corners separately)."""
    if len(vertices) == 0:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    key = np.round(vertices, 5)
    uniq, inverse = np.unique(key, axis=0, return_inverse=True)
    f = inverse.reshape(-1)[faces]
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    return Mesh(uniq.astype(float), f[keep].astype(np.int64))


def _load_stl(path: Path) -> Mesh:
    data = path.read_bytes()
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + count * 50 == len(data):
            rec = np.frombuffer(data, dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")],
                                count=count, offset=84)
            verts = rec["v"].reshape(-1, 3).astype(float)
            return _merge(verts, np.arange(len(verts)).reshape(-1, 3))
    text = data.decode("utf-8", errors="replace")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not nums or len(nums) % 3:
        raise ValueError(f"{path.name} isn't a valid STL file.")
    verts = np.array(nums, dtype=float)
    return _merge(verts, np.arange(len(verts)).reshape(-1, 3))


def _load_obj(path: Path) -> Mesh:
    verts, faces = [], []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            verts.append([float(p) for p in parts[1:4]])
        elif parts[0] == "f" and len(parts) >= 4:
            idx = []
            for p in parts[1:]:
                i = int(p.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):  # fan-triangulate polygons
                faces.append([idx[0], idx[k], idx[k + 1]])
    if not faces:
        raise ValueError(f"{path.name} has no faces.")
    return Mesh(np.array(verts, float), np.array(faces, np.int64))


_NS = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
_PNS = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"


def _matrix_3mf(text: str | None) -> np.ndarray:
    m = np.eye(4)
    if text:
        v = [float(x) for x in text.split()]
        if len(v) == 12:
            m[:3, :3] = np.array(v[:9]).reshape(3, 3).T
            m[:3, 3] = v[9:12]
    return m


def _load_3mf(path: Path) -> Mesh:
    with zipfile.ZipFile(path) as z:
        models = {}
        for name in z.namelist():
            if name.lower().endswith(".model"):
                try:
                    models["/" + name.lstrip("/")] = ET.fromstring(z.read(name))
                except ET.ParseError:
                    continue
    main = next((k for k in models if k.lower().endswith("3d/3dmodel.model")), None)
    if main is None:
        raise ValueError(f"{path.name} has no 3D model inside.")

    def obj(file: str, oid: str):
        root = models.get(file)
        return None if root is None else root.find(f".//m:resources/m:object[@id='{oid}']", _NS)

    verts_all, faces_all = [], []

    def emit(file: str, oid: str, transform: np.ndarray, depth: int = 0) -> None:
        o = obj(file, oid)
        if o is None or depth > 8:
            return
        mesh = o.find("m:mesh", _NS)
        if mesh is not None:
            vs = np.array([[float(v.get("x")), float(v.get("y")), float(v.get("z"))]
                           for v in mesh.findall("m:vertices/m:vertex", _NS)], float)
            fs = np.array([[int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))]
                           for t in mesh.findall("m:triangles/m:triangle", _NS)], np.int64)
            if len(vs) and len(fs):
                vs = (np.c_[vs, np.ones(len(vs))] @ transform.T)[:, :3]
                offset = sum(len(v) for v in verts_all)
                verts_all.append(vs); faces_all.append(fs + offset)
        for comp in o.findall("m:components/m:component", _NS):
            target = comp.get(_PNS) or file
            emit(target if target.startswith("/") else "/" + target, comp.get("objectid"),
                 transform @ _matrix_3mf(comp.get("transform")), depth + 1)

    items = models[main].findall("m:build/m:item", _NS)
    for item in items:
        emit(item.get(_PNS) or main, item.get("objectid"), _matrix_3mf(item.get("transform")))
    if not faces_all:
        raise ValueError(f"{path.name} contains no printable mesh.")
    return _merge(np.vstack(verts_all), np.vstack(faces_all))


def describe(mesh: Mesh) -> dict:
    lo, hi = mesh.bounds()
    edges = mesh.edge_report()
    return {
        "size_mm": [round(float(v), 2) for v in (hi - lo)],
        "bounds_min_mm": [round(float(v), 2) for v in lo],
        "bounds_max_mm": [round(float(v), 2) for v in hi],
        "volume_mm3": round(mesh.volume(), 1),
        "surface_area_mm2": round(mesh.surface_area(), 1),
        "triangles": int(len(mesh.faces)),
        "shells": mesh.shells(),
        "watertight": edges["open_edges"] == 0 and edges["overshared_edges"] == 0,
        **edges,
    }
