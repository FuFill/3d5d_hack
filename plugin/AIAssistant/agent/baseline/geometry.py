# -*- coding: utf-8 -*-
"""Геометрический анализ сетки (класс зависимостей G по спецификации, раздел 5).

Код совместим с Python 3.5 (интерпретатор STE Slicer): без f-строк и аннотаций
переменных. Используется numpy, который входит в поставку Cura 3.6 / STE Slicer.
Внутри слайсера вместо чтения STL можно передать вершины из MeshData узла сцены.
"""

import math
import os
import struct

import numpy

# Порог нависания по умолчанию соответствует support_angle=50 из fdmprinter:
# поддержка нужна, если угол нависания (от вертикали) больше support_angle.
DEFAULT_SUPPORT_ANGLE = 50.0


def load_stl(path):
    """Читает ASCII или бинарный STL и возвращает массив треугольников (N, 3, 3)."""
    if os.name == "nt" and len(os.path.abspath(path)) >= 240 and not path.startswith("\\\\?\\"):
        path = "\\\\?\\" + os.path.abspath(path)
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        count = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * count == len(data):
            record = numpy.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
            arr = numpy.frombuffer(data, dtype=record, count=count, offset=84)
            return arr["v"].astype(numpy.float64)
    text = data.decode("latin-1")
    values = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            values.append((float(parts[1]), float(parts[2]), float(parts[3])))
    if not values or len(values) % 3:
        raise ValueError("Не удалось прочитать STL: %s" % path)
    return numpy.array(values, dtype=numpy.float64).reshape(-1, 3, 3)


def _face_data(tris):
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    cross = numpy.cross(e1, e2)
    double_area = numpy.linalg.norm(cross, axis=1)
    safe = numpy.where(double_area > 0, double_area, 1.0)
    normals = cross / safe[:, None]
    return normals, double_area / 2.0


def _solid_moments(tris):
    """Объём, центр масс и тензор инерции однородного тела по замкнутой сетке."""
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    det = numpy.einsum("ij,ij->i", a, numpy.cross(b, c))
    vol = det.sum() / 6.0
    sign = 1.0 if vol >= 0 else -1.0
    vol = abs(vol)
    if vol <= 0:
        return 0.0, tris.reshape(-1, 3).mean(axis=0), numpy.zeros((3, 3))
    centroid = sign * ((a + b + c) * det[:, None]).sum(axis=0) / (24.0 * vol)
    # Второй момент тетраэдра (0, a, b, c): det/120 * (sum v_i v_j^T + sum v_i v_i^T)
    s = a + b + c
    cov = (numpy.einsum("i,ij,ik->jk", det, s, s)
           + numpy.einsum("i,ij,ik->jk", det, a, a)
           + numpy.einsum("i,ij,ik->jk", det, b, b)
           + numpy.einsum("i,ij,ik->jk", det, c, c)) * sign / 120.0
    cov -= vol * numpy.outer(centroid, centroid)
    inertia = numpy.trace(cov) * numpy.eye(3) - cov
    return vol, centroid, inertia


def _edge_check(tris):
    """Число рёбер, не принадлежащих ровно двум граням (0 у замкнутой 2-многообразной сетки)."""
    verts = numpy.round(tris.reshape(-1, 3), 5)
    _, inverse = numpy.unique(verts, axis=0, return_inverse=True)
    idx = inverse.reshape(-1, 3)
    edges = numpy.concatenate([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
    edges.sort(axis=1)
    _, counts = numpy.unique(edges, axis=0, return_counts=True)
    return int((counts != 2).sum())


def _axis_line_hits(tris, origin, axis):
    """Сколько треугольников пересекает прямая origin + t*axis (Мёллер–Трумбор, без ограничения t)."""
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    p = numpy.cross(axis, e2)
    det = numpy.einsum("ij,ij->i", e1, p)
    ok = numpy.abs(det) > 1e-12
    inv = numpy.where(ok, 1.0 / numpy.where(ok, det, 1.0), 0.0)
    s = origin - tris[:, 0]
    u = numpy.einsum("ij,ij->i", s, p) * inv
    q = numpy.cross(s, e1)
    v = numpy.einsum("j,ij->i", axis, q) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1)
    return int(hit.sum())


def _fit_revolution_axis(centers, normals, areas, direction, tol):
    """Ищет прямую с направлением direction, через которую проходят нормали боковых граней.

    У поверхности вращения нормаль каждой боковой грани пересекает ось, поэтому
    точка оси — решение взвешенной задачи наименьших квадратов для прямых
    (центр грани, нормаль). Грани кулачков, лопаток и т.п. отсеиваются как выбросы.
    Возвращает (точку оси, маску граней-«поверхностей вращения»).
    """
    d = direction / numpy.linalg.norm(direction)
    side = numpy.abs(normals.dot(d)) < 0.1
    if side.sum() < 8:
        return None, numpy.zeros(len(areas), dtype=bool)
    c = centers[side] - numpy.outer(centers[side].dot(d), d)
    n = normals[side] - numpy.outer(normals[side].dot(d), d)
    n /= numpy.maximum(numpy.linalg.norm(n, axis=1), 1e-12)[:, None]
    w = areas[side].copy()
    p = (c * w[:, None]).sum(axis=0) / max(w.sum(), 1e-12)
    keep = numpy.ones(len(w), dtype=bool)
    for _ in range(6):
        proj = numpy.eye(3)[None, :, :] - n[:, :, None] * n[:, None, :]
        ww = w * keep
        a = (proj * ww[:, None, None]).sum(axis=0) + numpy.outer(d, d) * ww.sum()
        b = numpy.einsum("ijk,ik->j", proj * ww[:, None, None], c)
        if not keep.any():
            break
        # lstsq: у призмы нормали боковых граней параллельны, и система вырождена по одному направлению.
        p = numpy.linalg.lstsq(a, b, rcond=-1)[0]
        diff = p - c
        resid = numpy.linalg.norm(diff - n * numpy.einsum("ij,ij->i", diff, n)[:, None], axis=1)
        keep = resid < max(tol, 3.0 * float(numpy.median(resid[keep])) if keep.any() else tol)
        keep &= resid < 4.0 * tol
    mask = numpy.zeros(len(areas), dtype=bool)
    mask[numpy.where(side)[0][keep]] = True
    return p, mask


def _revolved_mask(centers, normals, point, axis, tol):
    """Грани, нормаль которых пересекает ось (конусы, торы, гофры, цилиндры)."""
    cr = numpy.cross(normals, axis)
    norm = numpy.linalg.norm(cr, axis=1)
    rel = centers - point
    dist = numpy.abs(numpy.einsum("ij,ij->i", rel, cr)) / numpy.maximum(norm, 1e-12)
    # Плоская фасетка аппроксимирует дугу: центр грани смещён от радиальной плоскости
    # нормали на долю радиуса (≈ r·sin(Δθ/6)), поэтому допуск растёт с радиусом.
    radius = numpy.linalg.norm(rel - numpy.outer(rel.dot(axis), axis), axis=1)
    return (norm > 0.17) & (dist < tol + 0.015 * radius)


def _axial_profile(centers, normals, areas, origin, axis, mask, bins=24):
    """Профиль поверхности вращения: внешний и внутренний радиус по длине оси."""
    rel = centers - origin
    along = rel.dot(axis)
    radial = rel - numpy.outer(along, axis)
    r = numpy.linalg.norm(radial, axis=1)
    outward = numpy.einsum("ij,ij->i", normals, radial / numpy.maximum(r, 1e-9)[:, None]) > 0.7
    inward = numpy.einsum("ij,ij->i", normals, radial / numpy.maximum(r, 1e-9)[:, None]) < -0.7
    edges = numpy.linspace(along.min(), along.max() + 1e-9, bins + 1)
    idx = numpy.clip(numpy.digitize(along, edges) - 1, 0, bins - 1)
    outer = []
    for i in range(bins):
        sel = mask & outward & (idx == i)
        if sel.any():
            outer.append(float(r[sel].max()))
    # Центральное отверстие: минимальный радиус внутренних цилиндрических граней с заметной площадью.
    bore = None
    sel = mask & inward
    if sel.any():
        order = numpy.argsort(r[sel])
        rs, aa = r[sel][order], areas[sel][order]
        length = float(along.max() - along.min())
        for i in range(len(rs)):
            band = (rs >= rs[i]) & (rs < rs[i] + 0.3)
            if aa[band].sum() >= 0.3 * 2.0 * math.pi * rs[i] * min(length, 5.0):
                bore = float(rs[i])
                break
    return {
        "min_outer_radius": min(outer) if outer else None,
        "max_outer_radius": max(outer) if outer else None,
        "bore_radius": bore,
        "r_max_any": float(r.max()),
        "length": float(along.max() - along.min()),
    }


def orientation_metrics(tris, normals, areas, up, support_angle=DEFAULT_SUPPORT_ANGLE):
    """Печатаемость для направления «вверх» up (единичный вектор)."""
    heights = tris.dot(up)
    z_face_min = heights.min(axis=1)
    z0 = heights.min()
    cos_up = normals.dot(up)
    on_bed = (cos_up < -0.985) & (z_face_min - z0 < 0.05)
    # Угол между нормалью и -up < 90 - support_angle → требуется поддержка.
    limit = math.cos(math.radians(90.0 - support_angle))
    overhang = (cos_up < -limit) & ~on_bed
    stair = (numpy.abs(cos_up) > 0.05) & (numpy.abs(cos_up) < 0.985)
    return {
        "height": float(heights.max() - z0),
        "bed_contact_area": float(areas[on_bed].sum()),
        "overhang_area": float(areas[overhang].sum()),
        "stair_area": float(areas[stair].sum()),
    }


def analyze_mesh(tris, support_angle=DEFAULT_SUPPORT_ANGLE):
    """Возвращает словарь GeometryFeatures (единицы — мм, мм², мм³)."""
    tris = numpy.asarray(tris, dtype=numpy.float64)
    normals, areas = _face_data(tris)
    vol, centroid, inertia = _solid_moments(tris)
    lo = tris.reshape(-1, 3).min(axis=0)
    hi = tris.reshape(-1, 3).max(axis=0)
    moments, axes = numpy.linalg.eigh(inertia)
    axes = axes.T
    size = hi - lo
    centers = tris.mean(axis=1)

    # Ось вращения: направление, для которого наибольшая доля площади — поверхности вращения.
    tol = max(0.05, 0.004 * float(size.max()))
    best = None
    for vec in list(numpy.eye(3)) + list(axes):
        point, mask = _fit_revolution_axis(centers, normals, areas, vec, tol)
        if point is None:
            continue
        frac = float(areas[mask].sum() / areas.sum())
        if best is None or frac > best[0] + 1e-6:
            best = (frac, vec / numpy.linalg.norm(vec), point, mask)
    if best is None:
        best = (0.0, axes[0], centroid, numpy.zeros(len(areas), dtype=bool))
    revolved_fraction, axis, axis_point, mask = best
    k = int(numpy.argmax(numpy.abs(axis)))
    if axis[k] < 0:
        axis = -axis
    # Шум подгонки ~0.1° наклоняет опорный торец; почти мировую ось (< 1°) округляем до точной.
    if abs(axis[k]) > math.cos(math.radians(1.0)):
        axis = numpy.eye(3)[k]
    # Точка оси на уровне центра масс (удобно для переноса на ось 5D).
    axis_point = axis_point + axis * float((centroid - axis_point).dot(axis))
    profile = _axial_profile(centers, normals, areas, axis_point, axis, mask)
    line_hits = _axis_line_hits(tris, axis_point, axis)
    offset = float(numpy.linalg.norm((centroid - axis_point) - axis * (centroid - axis_point).dot(axis)))
    # Торцы (нормаль вдоль оси) совместимы с телом вращения, поэтому учитываются в оценке.
    caps = numpy.abs(normals.dot(axis)) > 0.98
    revolved = _revolved_mask(centers, normals, axis_point, axis, tol)
    revolution_score = float((areas[revolved].sum() + areas[caps & ~revolved].sum()) / areas.sum())
    # Тело вращения в строгом смысле: почти вся площадь совместима с вращением и центр масс на оси.
    axisymmetric = revolution_score > 0.9 and offset < 0.02 * float(size.max())

    candidates = []
    seen = []
    for vec in list(numpy.eye(3)) + list(-numpy.eye(3)) + list(axes) + list(-axes):
        vec = vec / numpy.linalg.norm(vec)
        if any(float(vec.dot(s)) > 0.999 for s in seen):
            continue
        seen.append(vec)
        m = orientation_metrics(tris, normals, areas, vec, support_angle)
        m["up"] = [round(float(x), 4) for x in vec]
        candidates.append(m)

    return {
        "triangles": int(len(tris)),
        "open_edges": _edge_check(tris) if len(tris) <= 2000000 else None,
        "bbox_min": [float(x) for x in lo],
        "bbox_size": [float(x) for x in (hi - lo)],
        "volume_mm3": float(vol),
        "area_mm2": float(areas.sum()),
        "centroid": [float(x) for x in centroid],
        "principal_moments": [float(x) for x in moments],
        "principal_axes": [[float(x) for x in a] for a in axes],
        "rotation_axis": {
            "vector": [round(float(x), 4) for x in axis],
            "point": [float(x) for x in axis_point],
            "revolved_area_fraction": revolved_fraction,
            "revolution_score": revolution_score,
            "centroid_offset": offset,
            "axisymmetric": bool(axisymmetric),
            "r_max": profile["r_max_any"],
            "min_outer_radius": profile["min_outer_radius"],
            "max_outer_radius": profile["max_outer_radius"],
            "length": profile["length"],
            "axis_line_hits": line_hits,
            "through_bore": line_hits == 0,
            "bore_radius": profile["bore_radius"] if line_hits == 0 else None,
        },
        "orientations": candidates,
    }
