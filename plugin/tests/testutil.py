# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Helpers shared by the tests; no pytest dependency (tests also run on Python 3.5)."""

import glob
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(PLUGIN)
EXAMPLES = os.path.join(REPO, "data", "examples")
if PLUGIN not in sys.path:
    sys.path.insert(0, PLUGIN)

import numpy  # noqa: E402


class SkipTest(unittest.SkipTest):
    """pytest reports unittest.SkipTest as a skip; the Python 3.5 runner does the same."""


def skip(reason):
    raise SkipTest(reason)


def approx(a, b, rel=1e-9, abs_tol=1e-9):
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), abs_tol)


def assert_close(a, b, rel=1e-9, abs_tol=1e-9, msg=""):
    assert approx(a, b, rel, abs_tol), "%r != %r %s" % (a, b, msg)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    raise AssertionError("%s not raised" % exc.__name__)


def example_cases():
    """[(folder name, docx path, stl path)] sorted by folder."""
    out = []
    for folder in sorted(glob.glob(os.path.join(EXAMPLES, "*"))):
        docx = glob.glob(os.path.join(folder, "*.docx"))
        stl = [p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(".stl")]
        if docx and stl:
            out.append((os.path.basename(folder), docx[0], stl[0]))
    return out


def resources_dir():
    from ste_planner import planner
    try:
        return planner.default_resources_dir()
    except IOError:
        skip("STE Slicer resources not found")


# ---------------------------------------------------------------- synthetic meshes

def make_box(dx, dy, dz, origin=(0.0, 0.0, 0.0)):
    from ste_planner.geometry import Mesh
    x0, y0, z0 = origin
    v = numpy.array([[x0, y0, z0], [x0 + dx, y0, z0], [x0 + dx, y0 + dy, z0], [x0, y0 + dy, z0],
                     [x0, y0, z0 + dz], [x0 + dx, y0, z0 + dz], [x0 + dx, y0 + dy, z0 + dz], [x0, y0 + dy, z0 + dz]])
    f = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
         [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return Mesh(v, f)


def make_tube(r_outer, r_inner, height, n=64, z0=0.0):
    """Closed tube (r_inner > 0) or cylinder (r_inner == 0) along Z, outward oriented."""
    from ste_planner.geometry import Mesh
    ang = numpy.linspace(0, 2 * math.pi, n, endpoint=False)
    verts, faces = [], []

    def ring(r, z):
        start = len(verts)
        for a in ang:
            verts.append([r * math.cos(a), r * math.sin(a), z])
        return start

    ob, ot = ring(r_outer, z0), ring(r_outer, z0 + height)
    for i in range(n):
        j = (i + 1) % n
        faces.append([ob + i, ob + j, ot + j])
        faces.append([ob + i, ot + j, ot + i])
    if r_inner > 0:
        ib, it = ring(r_inner, z0), ring(r_inner, z0 + height)
        for i in range(n):
            j = (i + 1) % n
            faces.append([ib + i, it + j, ib + j])
            faces.append([ib + i, it + i, it + j])
            faces.append([ob + i, ib + j, ob + j])   # bottom annulus (normal -z)
            faces.append([ob + i, ib + i, ib + j])
            faces.append([ot + i, ot + j, it + j])   # top annulus (normal +z)
            faces.append([ot + i, it + j, it + i])
    else:
        cb = len(verts)
        verts.append([0.0, 0.0, z0])
        ct = len(verts)
        verts.append([0.0, 0.0, z0 + height])
        for i in range(n):
            j = (i + 1) % n
            faces.append([cb, ob + j, ob + i])
            faces.append([ct, ot + i, ot + j])
    return Mesh(numpy.array(verts), numpy.array(faces))


def polygon_area_volume(r, n, h):
    """Exact volume of an n-gon prism inscribed in radius r."""
    return 0.5 * n * r * r * math.sin(2 * math.pi / n) * h
