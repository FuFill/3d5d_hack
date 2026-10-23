# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Mesh analysis against analytic meshes and an independent oracle (trimesh) on the example STLs."""

import math
import os
import struct
import tempfile

import numpy

from testutil import (assert_close, make_box, make_tube, polygon_area_volume, example_cases, skip)

from ste_planner import geometry as G


def test_box_properties():
    m = make_box(10, 20, 30)
    a = G.analyze_mesh(m)
    assert a["watertight"] and a["consistent_orientation"] and a["genus"] == 0
    assert_close(a["volume_mm3"], 6000.0)
    assert_close(a["area_mm2"], 2 * (200 + 600 + 300))
    assert a["size"] == [10.0, 20.0, 30.0]
    vol, c, inertia = G.mass_properties(m)
    assert_close(c[0], 5.0)
    # Ixx of a box with unit density: m (b^2 + c^2) / 12
    assert_close(inertia[0, 0], 6000.0 * (20 ** 2 + 30 ** 2) / 12.0, rel=1e-9)


def test_cylinder_volume_exact_for_polygon():
    n = 48
    m = make_tube(10.0, 0.0, 5.0, n)
    a = G.analyze_mesh(m)
    assert a["watertight"]
    assert_close(a["volume_mm3"], polygon_area_volume(10.0, n, 5.0), rel=1e-9)


def test_tube_genus_and_bore():
    m = make_tube(20.0, 8.0, 30.0, 96)
    a = G.analyze_mesh(m)
    assert a["genus"] == 1
    r = G.rotational_features(m).value
    assert r["is_rotational_candidate"]
    assert abs(abs(r["axis"][2]) - 1.0) < 1e-6
    assert abs(r["bore_radius"] - 8.0) < 0.05
    assert abs(r["r_max"] - 20.0) < 1e-6


def test_open_and_flipped_meshes_are_reported():
    m = make_box(10, 10, 10)
    opened = G.Mesh(m.vertices, m.faces[:-1])
    a = G.analyze_mesh(opened)
    assert not a["watertight"] and a["status"] == "invalid" and a["volume_mm3"] is None
    flipped = G.Mesh(m.vertices, m.faces[:, ::-1])
    a = G.analyze_mesh(flipped)
    assert a["watertight"] and any("inwards" in w for w in a["warnings"])
    half = m.faces.copy()
    half[0] = half[0][::-1]
    a = G.analyze_mesh(G.Mesh(m.vertices, half))
    assert not a["consistent_orientation"]


def test_unit_warning():
    a = G.analyze_mesh(make_box(0.05, 0.05, 0.05))
    assert any("metres" in w for w in a["warnings"])


def test_stl_roundtrip_binary_and_ascii():
    m = make_box(3, 4, 5)
    tri = m.triangles
    d = tempfile.mkdtemp()
    pb = os.path.join(d, "b.stl")
    with open(pb, "wb") as f:
        f.write(b"\0" * 80 + struct.pack("<I", len(tri)))
        for t in tri:
            f.write(struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *t.reshape(-1)) + b"\0\0")
    pa = os.path.join(d, "a.stl")
    with open(pa, "w") as f:
        f.write("solid t\n")
        for t in tri:
            f.write(" facet normal 0 0 0\n  outer loop\n")
            for v in t:
                f.write("   vertex %f %f %f\n" % tuple(v))
            f.write("  endloop\n endfacet\n")
        f.write("endsolid t\n")
    for p in (pb, pa):
        a = G.analyze_mesh(G.load_stl(p))
        assert_close(a["volume_mm3"], 60.0, rel=1e-6)


def test_rotation_to_z():
    for up in ([1, 0, 0], [0, -1, 0], [0.3, 0.4, -0.866], [0, 0, -1], [0, 0, 1]):
        r = G.rotation_to_z(up)
        u = numpy.asarray(up, dtype=float)
        u /= numpy.linalg.norm(u)
        assert numpy.allclose(r.dot(u), [0, 0, 1], atol=1e-9)
        assert abs(numpy.linalg.det(r) - 1.0) < 1e-9


def test_overhang_and_orientation():
    # Two boxes forming an overhanging "T": the cap overhangs on both sides.
    stem = make_box(10, 10, 20, origin=(10, 0, 0))
    cap = make_box(30, 10, 5, origin=(0, 0, 20))
    v = numpy.vstack([stem.vertices, cap.vertices])
    f = numpy.vstack([stem.faces, cap.faces + len(stem.vertices)])
    m = G.Mesh(v, f)
    up_eval = G.evaluate_orientation(m, [0, 0, 1])
    down_eval = G.evaluate_orientation(m, [0, 0, -1])
    assert up_eval["overhang_area"] > 0           # cap undersides need support
    assert_close(up_eval["overhang_area"], 2 * 10 * 10 + 10 * 10, rel=1e-6)  # includes the stem/cap seam face
    assert down_eval["overhang_area"] < up_eval["overhang_area"]
    scored = G.score_orientations([up_eval, down_eval])
    assert scored[0]["up"][2] < 0                 # flipping removes the overhang


def test_chord_and_staircase():
    assert_close(G.chord_error(10.0, 128).value, 10.0 * (1 - math.cos(math.pi / 128)))
    n = G.segments_for_chord_error(10.0, 0.01).value
    assert G.chord_error(10.0, n).value <= 0.01 < G.chord_error(10.0, n - 1).value
    assert G.segments_for_chord_error(1.0, 2.0).status == "unknown"
    assert_close(G.staircase_height(0.2, (0, 0, 1)).value, 0.2)
    assert_close(G.staircase_height(0.2, (1, 0, 0)).value, 0.0)
    assert_close(G.overhang_offset(0.2, 45).value, 0.2)
    assert G.overhang_offset(0.2, 0).status == "unknown"


def test_coaxial_axis_of_offset_journals():
    # shaft: two coaxial cylinders along Z plus an off-axis lug that shifts the centroid
    a = make_tube(6.0, 0.0, 20.0, 64)
    b = make_tube(10.0, 0.0, 8.0, 64, z0=20.0)
    lug = make_box(30, 6, 8, origin=(5, -3, 20))
    parts = [a, b, lug]
    v, f, off = [], [], 0
    for p in parts:
        v.append(p.vertices)
        f.append(p.faces + off)
        off += len(p.vertices)
    m = G.Mesh(numpy.vstack(v), numpy.vstack(f))
    res = G.coaxial_cylinder_axis(m)
    assert abs(abs(res["axis"][2]) - 1.0) < 1e-6
    assert abs(res["center"][0]) < 0.05 and abs(res["center"][1]) < 0.05


def test_teeth_count_on_synthetic_gear():
    # 24 rectangular-ish teeth: star-shaped prism
    n_teeth, pts = 24, []
    for i in range(n_teeth * 4):
        ang = 2 * math.pi * i / (n_teeth * 4)
        r = 20.0 if (i % 4) in (1, 2) else 18.0
        pts.append((r * math.cos(ang), r * math.sin(ang)))
    k = len(pts)
    verts = [[x, y, 0.0] for x, y in pts] + [[x, y, 5.0] for x, y in pts] + [[0, 0, 0], [0, 0, 5]]
    faces = []
    for i in range(k):
        j = (i + 1) % k
        faces += [[i, j, k + j], [i, k + j, k + i], [2 * k, j, i], [2 * k + 1, k + i, k + j]]
    m = G.Mesh(numpy.array(verts), numpy.array(faces))
    assert G.analyze_mesh(m)["watertight"]
    t = G.count_teeth(m, [0, 0, 1], [0, 0, 2.5])
    assert t is not None and t["count"] == n_teeth


def test_example_volumes_match_trimesh():
    try:
        import trimesh
    except ImportError:
        skip("trimesh not available")
    cases = example_cases()
    if not cases:
        skip("examples not found")
    for name, _, stl in cases:
        mine = G.analyze_mesh(G.load_stl(stl))
        ref = trimesh.load(stl, force="mesh")
        assert mine["watertight"], name
        assert_close(mine["volume_mm3"], abs(float(ref.volume)), rel=1e-6, msg=name)
        assert_close(mine["area_mm2"], float(ref.area), rel=1e-6, msg=name)
