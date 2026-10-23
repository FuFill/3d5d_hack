# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Mesh analysis and orientation search (specification sections 5 and 7).

Pure numpy (works with numpy 1.15 bundled in STE Slicer 7.10). Characteristics
are computed from the mesh itself, never from a picture.
"""

import math
import struct

import numpy

from .result import computed, unknown, MODEL_GEOMETRIC, MODEL_POLICY, MODEL_ANALYTIC

_EPS = 1e-12


class Mesh(object):
    """Indexed triangle mesh in mm."""

    def __init__(self, vertices, faces, name=None):
        self.vertices = numpy.asarray(vertices, dtype=numpy.float64).reshape(-1, 3)
        self.faces = numpy.asarray(faces, dtype=numpy.int64).reshape(-1, 3)
        self.name = name

    @classmethod
    def from_triangles(cls, triangles, name=None, decimals=6):
        tri = numpy.asarray(triangles, dtype=numpy.float64).reshape(-1, 3)
        rounded = numpy.round(tri, decimals)
        verts, inverse = numpy.unique(rounded, axis=0, return_inverse=True)
        return cls(verts, inverse.reshape(-1, 3), name=name)

    @property
    def triangles(self):
        return self.vertices[self.faces]

    def copy(self):
        return Mesh(self.vertices.copy(), self.faces.copy(), self.name)

    def transformed(self, matrix):
        """Apply a 3x3 (rotation/scale) or 4x4 homogeneous matrix."""
        m = numpy.asarray(matrix, dtype=numpy.float64)
        if m.shape == (4, 4):
            v = self.vertices.dot(m[:3, :3].T) + m[:3, 3]
            det = numpy.linalg.det(m[:3, :3])
        else:
            v = self.vertices.dot(m.T)
            det = numpy.linalg.det(m)
        faces = self.faces if det > 0 else self.faces[:, ::-1]
        return Mesh(v, faces.copy(), self.name)


def load_stl(path):
    """Read binary or ASCII STL into a Mesh."""
    with open(path, "rb") as f:
        data = f.read()
    tri = None
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data):
            rec = numpy.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            arr = numpy.frombuffer(data, dtype=rec, count=n, offset=84)
            tri = arr["v"].astype(numpy.float64)
    if tri is None:
        text = data.decode("ascii", errors="replace")
        nums = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("vertex"):
                nums.extend(float(x) for x in line.split()[1:4])
        if not nums or len(nums) % 9:
            raise ValueError("Not a valid STL: %s" % path)
        tri = numpy.array(nums, dtype=numpy.float64).reshape(-1, 3, 3)
    return Mesh.from_triangles(tri, name=path)


# ---------------------------------------------------------------- basic properties

def face_normals_areas(mesh):
    t = mesh.triangles
    cr = numpy.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    norm = numpy.sqrt((cr ** 2).sum(axis=1))
    area = 0.5 * norm
    with numpy.errstate(invalid="ignore", divide="ignore"):
        normals = cr / norm[:, None]
    normals[norm < _EPS] = 0.0
    return normals, area


def signed_volume(mesh):
    """V = sum(dot(v0, cross(v1, v2))) / 6 (closed, consistently oriented)."""
    t = mesh.triangles
    return float(numpy.einsum("ij,ij->i", t[:, 0], numpy.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def _edges(mesh):
    f = mesh.faces
    directed = numpy.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    return directed


def edge_topology(mesh):
    """Counts of undirected edge usage and directed edge duplicates."""
    directed = _edges(mesh)
    undirected = numpy.sort(directed, axis=1)
    _, counts = numpy.unique(undirected, axis=0, return_counts=True)
    _, dcounts = numpy.unique(directed, axis=0, return_counts=True)
    return {
        "edges": int(len(counts)),
        "boundary_edges": int((counts == 1).sum()),
        "nonmanifold_edges": int((counts > 2).sum()),
        "duplicate_directed_edges": int((dcounts > 1).sum()),
    }


def connected_components(mesh):
    """Face components connected through shared vertices."""
    n = len(mesh.vertices)
    pairs = numpy.concatenate([mesh.faces[:, [0, 1]], mesh.faces[:, [1, 2]]])
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components as cc
        graph = coo_matrix((numpy.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        _, roots = cc(graph, directed=False)
    except ImportError:
        parent = numpy.arange(n)

        def find(i):
            root = i
            while parent[root] != root:
                root = parent[root]
            while parent[i] != root:
                parent[i], i = root, parent[i]
            return root

        for a, b in pairs:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
        roots = numpy.array([find(i) for i in range(n)])
    face_roots = roots[mesh.faces[:, 0]]
    labels, face_labels = numpy.unique(face_roots, return_inverse=True)
    return len(labels), face_labels


def mass_properties(mesh):
    """Volume, centroid and inertia tensor (unit density) of a closed mesh."""
    t = mesh.triangles
    v0, v1, v2 = t[:, 0], t[:, 1], t[:, 2]
    d = numpy.einsum("ij,ij->i", v0, numpy.cross(v1, v2))
    vol = d.sum() / 6.0
    if abs(vol) < _EPS:
        return 0.0, numpy.zeros(3), numpy.zeros((3, 3))
    centroid = ((v0 + v1 + v2) * d[:, None]).sum(axis=0) / (24.0 * vol)
    # second moments about the origin, then shift to the centroid
    s = numpy.zeros((3, 3))
    for i in range(3):
        for j in range(3):
            s[i, j] = (d * (2 * (v0[:, i] * v0[:, j] + v1[:, i] * v1[:, j] + v2[:, i] * v2[:, j]) +
                            v0[:, i] * v1[:, j] + v0[:, j] * v1[:, i] + v0[:, i] * v2[:, j] +
                            v0[:, j] * v2[:, i] + v1[:, i] * v2[:, j] + v1[:, j] * v2[:, i])).sum() / 120.0
    s -= vol * numpy.outer(centroid, centroid)
    inertia = numpy.trace(s) * numpy.eye(3) - s
    if vol < 0:
        inertia = -inertia
    return float(vol), centroid, inertia


def analyze_mesh(mesh, declared_unit="mm"):
    """GeometryFeatures: topology checks, bbox, volume, area, principal axes."""
    if len(mesh.faces) == 0:
        return {"status": "invalid", "errors": ["empty mesh"]}
    normals, areas = face_normals_areas(mesh)
    topo = edge_topology(mesh)
    ncomp, _ = connected_components(mesh)
    watertight = topo["boundary_edges"] == 0 and topo["nonmanifold_edges"] == 0
    consistent = watertight and topo["duplicate_directed_edges"] == 0
    vol, centroid, inertia = mass_properties(mesh)
    bmin = mesh.vertices[mesh.faces.reshape(-1)].min(axis=0)
    bmax = mesh.vertices[mesh.faces.reshape(-1)].max(axis=0)
    size = bmax - bmin
    euler = len(numpy.unique(mesh.faces)) - topo["edges"] + len(mesh.faces)
    genus = None
    if watertight:
        genus = int(round((2 * ncomp - euler) / 2.0))
    evals, evecs = numpy.linalg.eigh(inertia)
    errors, warnings = [], []
    if not watertight:
        errors.append("mesh is not closed: %d boundary, %d non-manifold edges" %
                      (topo["boundary_edges"], topo["nonmanifold_edges"]))
    elif not consistent:
        errors.append("inconsistent face orientation")
    if watertight and vol < 0:
        warnings.append("faces are oriented inwards (negative volume)")
    degenerate = int((areas < 1e-10).sum())
    if degenerate:
        warnings.append("%d degenerate faces" % degenerate)
    if declared_unit == "mm" and size.max() < 2.0:
        warnings.append("bounding box below 2 mm: model may be in metres or inches")
    if declared_unit == "mm" and size.max() > 2000.0:
        warnings.append("bounding box above 2 m: model may be in micrometres")
    return {
        "status": "ok" if not errors else "invalid",
        "errors": errors,
        "warnings": warnings,
        "faces": int(len(mesh.faces)),
        "vertices": int(len(numpy.unique(mesh.faces))),
        "components": int(ncomp),
        "watertight": bool(watertight),
        "consistent_orientation": bool(consistent),
        "euler_characteristic": int(euler),
        "genus": genus,
        "bbox_min": bmin.tolist(),
        "bbox_max": bmax.tolist(),
        "size": size.tolist(),
        "volume_mm3": abs(vol) if watertight else None,
        "area_mm2": float(areas.sum()),
        "centroid": centroid.tolist(),
        "principal_moments": evals.tolist(),
        "principal_axes": evecs.T.tolist(),
        "degenerate_faces": degenerate,
        "topology": topo,
        "unit": declared_unit,
    }


# ---------------------------------------------------------------- rotational features

def _orthonormal_basis(axis):
    a = numpy.asarray(axis, dtype=numpy.float64)
    a = a / numpy.linalg.norm(a)
    helper = numpy.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else numpy.array([0.0, 1.0, 0.0])
    u = numpy.cross(a, helper)
    u /= numpy.linalg.norm(u)
    v = numpy.cross(a, u)
    return a, u, v


def _sector_profile(pts, a, u, v, sectors=36):
    """Coefficient of variation of the max radius over angular sectors around axis a."""
    radial = numpy.sqrt(pts.dot(u) ** 2 + pts.dot(v) ** 2)
    ang = numpy.arctan2(pts.dot(v), pts.dot(u))
    idx = numpy.floor((ang + math.pi) / (2 * math.pi) * sectors).astype(numpy.int64) % sectors
    rmax = numpy.zeros(sectors)
    numpy.maximum.at(rmax, idx, radial)
    filled = rmax > 0
    if filled.sum() < sectors * 0.9:
        return float("inf")
    return float(rmax[filled].std() / max(rmax[filled].mean(), _EPS))


def _bore_radius(tri_r, tri_ang, inner, areas, bin_mm=0.25, sectors=36, coverage=0.85):
    """Smallest radius at which inward-facing side faces cover a full ring."""
    if not inner.any():
        return None
    r = tri_r[inner]
    sec = numpy.floor((tri_ang[inner] + math.pi) / (2 * math.pi) * sectors).astype(numpy.int64) % sectors
    bins = numpy.floor(r / bin_mm).astype(numpy.int64)
    for b in numpy.unique(bins):
        sel = (bins >= b - 1) & (bins <= b + 1)
        if len(numpy.unique(sec[sel])) >= coverage * sectors:
            return float(numpy.median(r[bins == b]))
    return None


def _canonical_sign(axis):
    for c in (2, 1, 0):
        if abs(axis[c]) > 1e-6:
            return -axis if axis[c] < 0 else axis
    return axis


def rotational_features(mesh, max_moment_ratio=0.03, max_sector_cv=0.025):
    """Detect an axis of rotational symmetry and the radial profile.

    For every principal axis two indicators are computed: mismatch of the two
    transverse principal moments of inertia (zero for n-fold symmetric parts,
    n >= 3) and the variation of the outer radius over angular sectors
    (robust to a set-screw hole or a keyway). The result is a geometric hint;
    the function of the axis must be confirmed by the requirements or a person.
    """
    vol, centroid, inertia = mass_properties(mesh)
    if abs(vol) < _EPS:
        return unknown("rotational_features", "zero volume")
    evals, evecs = numpy.linalg.eigh(inertia)
    pts = mesh.vertices[numpy.unique(mesh.faces)] - centroid
    per_axis = []
    for k in range(3):
        others = [evals[i] for i in range(3) if i != k]
        ratio = abs(others[0] - others[1]) / max(abs(max(others)), _EPS)
        a, u, v = _orthonormal_basis(_canonical_sign(evecs[:, k]))
        cv = _sector_profile(pts, a, u, v)
        per_axis.append({"axis": a, "u": u, "v": v, "moment_mismatch": float(ratio), "sector_cv": cv,
                         "length": float(pts.dot(a).max() - pts.dot(a).min())})
    best = min(per_axis, key=lambda d: (min(d["sector_cv"], 1e9), d["moment_mismatch"]))
    long_axis = max(per_axis, key=lambda d: d["length"])
    a, u, v = best["axis"], best["u"], best["v"]
    axial = pts.dot(a)
    radial = numpy.sqrt(pts.dot(u) ** 2 + pts.dot(v) ** 2)
    normals, areas = face_normals_areas(mesh)
    side = numpy.abs(normals.dot(a)) < 0.1
    tri_c = mesh.triangles.mean(axis=1) - centroid
    tri_r = numpy.sqrt(tri_c.dot(u) ** 2 + tri_c.dot(v) ** 2)
    tri_ang = numpy.arctan2(tri_c.dot(v), tri_c.dot(u))
    radial_dir = tri_c - numpy.outer(tri_c.dot(a), a)
    with numpy.errstate(invalid="ignore", divide="ignore"):
        radial_dir = radial_dir / numpy.linalg.norm(radial_dir, axis=1)[:, None]
    facing = numpy.einsum("ij,ij->i", numpy.nan_to_num(radial_dir), normals)
    inner = side & (facing < -0.9)
    outer = side & (facing > 0.9)
    total_area = float(areas.sum())
    is_rot = bool(best["moment_mismatch"] <= max_moment_ratio or best["sector_cv"] <= max_sector_cv)
    value = {
        "axis": a.tolist(),
        "center": centroid.tolist(),
        "moment_mismatch": best["moment_mismatch"],
        "sector_cv": best["sector_cv"],
        "is_rotational_candidate": is_rot,
        "axial_length": float(axial.max() - axial.min()),
        "r_max": float(radial.max()),
        "bore_radius": _bore_radius(tri_r, tri_ang, inner, areas),
        "outer_cylinder_area_fraction": float(areas[outer].sum() / total_area) if total_area > 0 else None,
        "inner_cylinder_area_fraction": float(areas[inner].sum() / total_area) if total_area > 0 else None,
        "long_axis": long_axis["axis"].tolist(),
        "long_axis_length": long_axis["length"],
    }
    return computed(value, None, "rotational_features", MODEL_GEOMETRIC,
                    inputs={"max_moment_ratio": max_moment_ratio, "max_sector_cv": max_sector_cv},
                    note="Symmetry hint from inertia and sector radii; semantics must be confirmed")


def radial_profile(mesh, axis, center, bins=40):
    """Per axial slice: innermost and outermost surface radius about the axis.

    r_inner of a slice without a bore is the distance of the closest surface
    point to the axis; it bounds a core cylinder that stays inside the part.
    """
    a, u, v = _orthonormal_basis(axis)
    pts = mesh.vertices[numpy.unique(mesh.faces)] - numpy.asarray(center, dtype=numpy.float64)
    t = pts.dot(a)
    r = numpy.sqrt(pts.dot(u) ** 2 + pts.dot(v) ** 2)
    edges = numpy.linspace(t.min(), t.max(), bins + 1)
    idx = numpy.clip(numpy.searchsorted(edges, t, side="right") - 1, 0, bins - 1)
    out = []
    for b in range(bins):
        sel = idx == b
        if not sel.any():
            continue
        out.append({"t0": float(edges[b]), "t1": float(edges[b + 1]), "r_inner": float(r[sel].min()),
                    "r_outer": float(r[sel].max())})
    return out


def coaxial_cylinder_axis(mesh, candidates=None, tol=None):
    """Axis shared by the cylindrical surfaces of a part (journals, discs, grooves).

    For a direction a, side faces (normal perpendicular to a) of cylinders
    coaxial with a have normal lines passing through one point of the plane
    normal to a. The point is found by weighted least squares with two inlier
    passes; the score is the area of side faces whose normal line passes within
    `tol` of it. Candidate directions: principal axes and coordinate axes.
    """
    normals, areas = face_normals_areas(mesh)
    cent = mesh.triangles.mean(axis=1)
    size = mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)
    tol = tol if tol is not None else max(0.2, 0.01 * float(size.max()))
    if candidates is None:
        _, _, inertia = mass_properties(mesh)
        _, evecs = numpy.linalg.eigh(inertia)
        candidates = [evecs[:, k] for k in range(3)] + list(numpy.eye(3))
    best = None
    total = float(areas.sum())
    for cand in candidates:
        a, u, v = _orthonormal_basis(_canonical_sign(numpy.asarray(cand, dtype=numpy.float64)))
        side = (numpy.abs(normals.dot(a)) < 0.02) & (areas > 0)
        if side.sum() < 6:
            continue
        c2 = numpy.stack([cent[side].dot(u), cent[side].dot(v)], axis=1)
        n2 = numpy.stack([normals[side].dot(u), normals[side].dot(v)], axis=1)
        n2 /= numpy.maximum(numpy.linalg.norm(n2, axis=1), _EPS)[:, None]
        w = areas[side]
        # RANSAC over pairs of normal lines (deterministic seed), then least-squares refinement
        rng = numpy.random.RandomState(0)
        prob = w / w.sum()
        n_side = len(w)
        p, keep = None, None
        best_vote = -1.0
        for _ in range(300):
            i, j = rng.choice(n_side, 2, replace=False, p=prob)
            m = numpy.array([[n2[i, 0], -n2[j, 0]], [n2[i, 1], -n2[j, 1]]])
            if abs(numpy.linalg.det(m)) < 0.05:
                continue
            ts = numpy.linalg.solve(m, c2[j] - c2[i])
            q = c2[i] + ts[0] * n2[i]
            d = c2 - q
            dist = numpy.abs(d[:, 0] * n2[:, 1] - d[:, 1] * n2[:, 0])
            k = dist < tol
            vote = float(w[k].sum())
            if vote > best_vote:
                best_vote, p, keep = vote, q, k
        for _ in range(2):
            if keep is None or keep.sum() < 3:
                break
            mats = numpy.eye(2)[None, :, :] - n2[keep][:, :, None] * n2[keep][:, None, :]
            lhs = (w[keep][:, None, None] * mats).sum(axis=0)
            rhs = (w[keep][:, None] * numpy.einsum("nij,nj->ni", mats, c2[keep])).sum(axis=0)
            if abs(numpy.linalg.det(lhs)) < 1e-9:
                break
            p = numpy.linalg.solve(lhs, rhs)
            d = c2 - p
            dist = numpy.abs(d[:, 0] * n2[:, 1] - d[:, 1] * n2[:, 0])
            keep = dist < tol
        if p is None:
            continue
        score = float(w[keep].sum())
        if best is None or score > best["coaxial_area"]:
            centre3 = u * p[0] + v * p[1] + a * float(cent.dot(a).mean())
            best = {"axis": a.tolist(), "center": centre3.tolist(), "coaxial_area": score,
                    "coaxial_area_fraction": score / total if total > 0 else 0.0}
    return best


def lateral_min_radius(mesh, axis, center, perpendicular_cos=0.95):
    """Smallest distance from the axis line to the lateral surface of the part.

    Faces nearly perpendicular to the axis (end faces, shoulders) are ignored:
    a core along the axis starts at an end face and passes shoulders. For the
    other faces the exact line-to-triangle distance is used (vertices, edges and
    piercing), so sparse meshes without vertices in mid sections are handled.
    """
    a, u, v = _orthonormal_basis(axis)
    normals, _ = face_normals_areas(mesh)
    lateral = numpy.abs(normals.dot(a)) < perpendicular_cos
    if not lateral.any():
        return None
    tri = mesh.triangles[lateral] - numpy.asarray(center, dtype=numpy.float64)
    # project to the plane normal to the axis: distance to the axis = norm in (u, v)
    p = numpy.stack([tri.dot(u), tri.dot(v)], axis=2)          # (n, 3, 2)
    d = numpy.linalg.norm(p, axis=2).min(axis=1)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        e = p[:, j] - p[:, i]
        ll = (e ** 2).sum(axis=1)
        with numpy.errstate(invalid="ignore", divide="ignore"):
            t = numpy.clip(-(p[:, i] * e).sum(axis=1) / ll, 0.0, 1.0)
        t[ll < _EPS] = 0.0
        q = p[:, i] + t[:, None] * e
        d = numpy.minimum(d, numpy.linalg.norm(q, axis=1))
    # the axis pierces a projected triangle -> distance 0
    def cross(o, b, c):
        return (b[:, 0] - o[:, 0]) * (c[:, 1] - o[:, 1]) - (b[:, 1] - o[:, 1]) * (c[:, 0] - o[:, 0])
    zero = numpy.zeros_like(p[:, 0])
    s1, s2, s3 = cross(p[:, 0], p[:, 1], zero), cross(p[:, 1], p[:, 2], zero), cross(p[:, 2], p[:, 0], zero)
    inside = ((s1 > 0) & (s2 > 0) & (s3 > 0)) | ((s1 < 0) & (s2 < 0) & (s3 < 0))
    d[inside] = 0.0
    return float(d.min())


def fit_axis_from_ends(mesh, axis, center, end_fraction=0.15):
    """Refine a shaft axis through the cross-section centres of both end portions.

    The inertia axis of an asymmetric shaft (cam lobes, flats) passes through the
    centroid, which may be off the journals; the journals at the ends define the
    rotation axis better.
    """
    a, u, v = _orthonormal_basis(axis)
    c0 = numpy.asarray(center, dtype=numpy.float64)
    pts = mesh.vertices[numpy.unique(mesh.faces)] - c0
    t = pts.dot(a)
    lo, hi = t.min(), t.max()
    span = hi - lo
    centres = []
    for sel in (t <= lo + end_fraction * span, t >= hi - end_fraction * span):
        if sel.sum() < 3:
            return list(a), list(c0)
        pu, pv = pts[sel].dot(u), pts[sel].dot(v)
        cu, cv = (pu.min() + pu.max()) / 2.0, (pv.min() + pv.max()) / 2.0
        centres.append(c0 + a * t[sel].mean() + u * cu + v * cv)
    d = centres[1] - centres[0]
    n = numpy.linalg.norm(d)
    if n < _EPS:
        return list(a), list(c0)
    d = d / n
    if d.dot(a) < 0:
        d = -d
    return d.tolist(), ((centres[0] + centres[1]) / 2.0).tolist()


def count_teeth(mesh, axis, center, sectors=1440, min_share=0.15, max_count=200):
    """Number of teeth/blades from the dominant harmonic of the outer radius around the axis.

    Points: vertices, face centres and edge midpoints (flanks have few vertices).
    Returns None unless one harmonic carries at least `min_share` of the
    spectrum between orders 3 and max_count (no clear periodicity).
    """
    a, u, v = _orthonormal_basis(axis)
    tri = mesh.triangles
    pts = numpy.concatenate([tri.reshape(-1, 3), tri.mean(axis=1), (tri[:, 0] + tri[:, 1]) / 2.0,
                             (tri[:, 1] + tri[:, 2]) / 2.0, (tri[:, 2] + tri[:, 0]) / 2.0])
    pts = pts - numpy.asarray(center, dtype=numpy.float64)
    r = numpy.sqrt(pts.dot(u) ** 2 + pts.dot(v) ** 2)
    ang = numpy.arctan2(pts.dot(v), pts.dot(u))
    idx = numpy.floor((ang + math.pi) / (2 * math.pi) * sectors).astype(numpy.int64) % sectors
    rmax = numpy.zeros(sectors)
    numpy.maximum.at(rmax, idx, r)
    for i in numpy.where(rmax == 0)[0]:
        rmax[i] = rmax[i - 1]
    spec = numpy.abs(numpy.fft.rfft(rmax - rmax.mean()))
    band = spec[3:max_count + 1]
    if band.sum() <= 0:
        return None
    k = int(numpy.argmax(band)) + 3
    share = float(band[k - 3] / band.sum())
    if share < min_share:
        return None
    return {"count": k, "share": share, "r_tip": float(rmax.max()), "r_root": float(numpy.percentile(rmax, 5))}


# ---------------------------------------------------------------- orientation

def rotation_to_z(up):
    """Rotation matrix that maps unit vector `up` onto +Z (Rodrigues)."""
    a = numpy.asarray(up, dtype=numpy.float64)
    a = a / numpy.linalg.norm(a)
    z = numpy.array([0.0, 0.0, 1.0])
    c = float(a.dot(z))
    if c > 1.0 - 1e-12:
        return numpy.eye(3)
    if c < -1.0 + 1e-12:
        return numpy.diag([1.0, -1.0, -1.0])
    v = numpy.cross(a, z)
    s = numpy.linalg.norm(v)
    vx = numpy.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return numpy.eye(3) + vx + vx.dot(vx) * ((1 - c) / (s * s))


def place_on_axis(mesh, rotation, axis_center):
    """5D placement: align the actual tooling axis, not the asymmetric bounding box."""
    rotation = numpy.asarray(rotation, dtype=float)
    center = numpy.asarray(axis_center, dtype=float)
    if center.shape != (3,) or not numpy.isfinite(center).all():
        raise ValueError("finite tooling-axis point required")
    placed = mesh.transformed(rotation)
    rotated_center = rotation.dot(center)
    shift = numpy.array([-rotated_center[0], -rotated_center[1], -placed.vertices[:, 2].min()])
    placed.vertices += shift
    return placed, shift


def overhang_mask(normals, centroids_z, z_min, support_angle_deg, bed_tolerance=0.05):
    """Faces needing support with a Cura-style overhang angle measured from vertical.

    A downward face needs support when its tilt from vertical exceeds the
    support angle, i.e. -n_z > sin(support_angle). Faces lying on the bed are
    excluded. The engine convention must be verified with test geometry.
    """
    threshold = math.sin(math.radians(support_angle_deg))
    down = -normals[:, 2] > threshold
    on_bed = centroids_z <= z_min + bed_tolerance
    return down & ~on_bed


def evaluate_orientation(mesh, up, support_angle_deg=50.0, layer_height=0.2, build_volume=None,
                         critical_face_mask=None, rotation=None):
    """Metrics of one orientation; feasibility first, score only afterwards."""
    rot = rotation_to_z(up) if rotation is None else numpy.asarray(rotation, dtype=float)
    if rot.shape != (3, 3) or not numpy.isfinite(rot).all() or not numpy.allclose(rot.T.dot(rot), numpy.eye(3), atol=1e-8) or abs(numpy.linalg.det(rot) - 1) > 1e-8:
        raise ValueError("orientation must be a proper finite rotation")
    m = mesh.transformed(rot)
    normals, areas = face_normals_areas(m)
    t = m.triangles
    cz = t[:, :, 2].mean(axis=1)
    zmin, zmax = float(t[:, :, 2].min()), float(t[:, :, 2].max())
    over = overhang_mask(normals, cz, zmin, support_angle_deg)
    projected = areas * numpy.abs(normals[:, 2])
    overhang_area = float(projected[over].sum())
    # Upper bound of support volume: projected overhang area times height above the bed.
    support_volume = float((projected[over] * (cz[over] - zmin)).sum())
    base = (normals[:, 2] < -0.995) & (cz <= zmin + 0.05)
    base_area = float(areas[base].sum())
    # Staircase proxy: area-weighted first-order cusp h*|n_s . n_l| (section 7).
    nz = numpy.abs(normals[:, 2])
    stair = nz * (nz < 0.999)
    staircase = float((areas * layer_height * stair).sum() / max(areas.sum(), _EPS))
    size = (t.reshape(-1, 3).max(axis=0) - t.reshape(-1, 3).min(axis=0))
    fits = None
    if build_volume is not None:
        if rotation is not None:
            fits = bool((size <= numpy.asarray(build_volume) + 1e-9).all())
        else:
            fits = bool(size[2] <= build_volume[2] + 1e-9 and
                        (min(size[0], size[1]) <= min(build_volume[0], build_volume[1]) + 1e-9) and
                        (max(size[0], size[1]) <= max(build_volume[0], build_volume[1]) + 1e-9))
    critical_support_contact = None
    if critical_face_mask is not None:
        critical_support_contact = float(areas[over & numpy.asarray(critical_face_mask)].sum())
    return {
        "up": [float(x) for x in numpy.asarray(up) / numpy.linalg.norm(up)],
        "rotation": rot.tolist(),
        "height": zmax - zmin,
        "size": size.tolist(),
        "overhang_area": overhang_area,
        "support_volume_upper": support_volume,
        "base_area": base_area,
        "staircase_mean_mm": staircase,
        "fits_build_volume": fits,
        "critical_support_contact": critical_support_contact,
        "support_angle_deg": support_angle_deg,
    }


def orientation_candidates(mesh, max_flat=6, min_angle_deg=5.0):
    """Candidate up-vectors: +/- principal axes and normals of large flat regions."""
    normals, areas = face_normals_areas(mesh)
    _, _, inertia = mass_properties(mesh)
    _, evecs = numpy.linalg.eigh(inertia)
    cands = []
    for k in range(3):
        cands.append(evecs[:, k])
        cands.append(-evecs[:, k])
    for c in (numpy.eye(3)):
        cands.append(c)
        cands.append(-c)
    # Large flat regions: group faces by quantized normal; place region on the bed -> up = -n.
    q = numpy.round(normals * 50.0).astype(numpy.int64)
    keys, inv = numpy.unique(q, axis=0, return_inverse=True)
    sums = numpy.bincount(inv, weights=areas)
    order = numpy.argsort(-sums)
    for idx in order[:max_flat]:
        if sums[idx] <= 0:
            continue
        sel = inv == idx
        n = (normals[sel] * areas[sel, None]).sum(axis=0)
        if numpy.linalg.norm(n) > _EPS:
            cands.append(-n / numpy.linalg.norm(n))
    out = []
    cos_lim = math.cos(math.radians(min_angle_deg))
    for c in cands:
        c = numpy.asarray(c, dtype=numpy.float64)
        c = c / numpy.linalg.norm(c)
        if all(float(c.dot(o)) < cos_lim for o in out):
            out.append(c)
    return [o.tolist() for o in out]


def score_orientations(evaluations, weights=None, refs=None):
    """J_R = sum w_i * metric_i / ref_i over feasible orientations only (policy P)."""
    weights = weights or {"support_volume_upper": 1.0, "critical_support_contact": 2.0,
                          "height": 0.3, "staircase_mean_mm": 0.5}
    feasible = [e for e in evaluations if e.get("fits_build_volume") is not False]
    if not feasible:
        return []
    refs = dict(refs or {})
    for key in weights:
        if key not in refs:
            vals = [e.get(key) or 0.0 for e in feasible]
            refs[key] = max(max(vals), 1e-9)
    scored = []
    for e in feasible:
        j = 0.0
        for key, w in sorted(weights.items()):
            j += w * (e.get(key) or 0.0) / refs[key]
        e2 = dict(e)
        e2["score"] = j
        scored.append(e2)
    scored.sort(key=lambda e: (e["score"], e["height"]))
    return scored


def chord_error(radius, segments):
    """eps = r (1 - cos(pi / N)) for N chords of a circle of radius r."""
    if radius <= 0 or segments < 3:
        return unknown("chord_error", "radius must be > 0 and N >= 3")
    return computed(radius * (1.0 - math.cos(math.pi / segments)), "mm", "chord_error", MODEL_GEOMETRIC,
                    inputs={"radius": radius, "segments": segments})


def segments_for_chord_error(radius, eps):
    """N >= ceil(pi / arccos(1 - eps / r)) for 0 < eps < r."""
    if not (0 < eps < radius):
        return unknown("segments_for_chord_error", "requires 0 < eps < r")
    return computed(int(math.ceil(math.pi / math.acos(1.0 - eps / radius))), "count",
                    "segments_for_chord_error", MODEL_GEOMETRIC, inputs={"radius": radius, "eps": eps})


def staircase_height(layer_height, surface_normal, layer_normal=(0.0, 0.0, 1.0)):
    """Conservative first-order step: c <= h |dot(n_s, n_l)| (not a model of Ra)."""
    ns = numpy.asarray(surface_normal, dtype=numpy.float64)
    nl = numpy.asarray(layer_normal, dtype=numpy.float64)
    ns, nl = ns / numpy.linalg.norm(ns), nl / numpy.linalg.norm(nl)
    return computed(layer_height * abs(float(ns.dot(nl))), "mm", "staircase_height", MODEL_ANALYTIC,
                    inputs={"h": layer_height})


def overhang_offset(layer_height, slope_deg):
    """Layer shift on a slope beta to horizontal: delta = h / tan(beta); beta=0 is a bridge."""
    if slope_deg <= 0:
        return unknown("overhang_offset", "beta = 0: bridge/ceiling must be checked separately")
    return computed(layer_height / math.tan(math.radians(slope_deg)), "mm", "overhang_offset",
                    MODEL_ANALYTIC, inputs={"h": layer_height, "beta_deg": slope_deg})


def place_on_bed(mesh, rotation):
    """Rotate and translate so that the part sits on z = 0 centred at x=y=0."""
    m = mesh.transformed(rotation)
    v = m.vertices[numpy.unique(m.faces)]
    bmin, bmax = v.min(axis=0), v.max(axis=0)
    shift = numpy.array([-(bmin[0] + bmax[0]) / 2.0, -(bmin[1] + bmax[1]) / 2.0, -bmin[2]])
    m.vertices = m.vertices + shift
    return m, shift
