"""Explicit SOFTWARE fixtures; none of these numbers are printing evidence."""

import copy
import itertools
import os
import struct
import tempfile

import numpy as np

from ste_planner import geometry, planner, volume_mesh
from testutil import make_box, resources_dir

ELASTIC = {"E_xy_MPa": 2000.0, "E_z_MPa": 800.0, "nu_xy": 0.25,
           "nu_xz": 0.10, "G_xz_MPa": 400.0}
STRENGTH = {"tensile_xy_MPa": 40.0, "compressive_xy_MPa": 40.0,
            "tensile_z_MPa": 12.0, "compressive_z_MPa": 12.0,
            "shear_xy_MPa": 20.0, "shear_xz_MPa": 8.0}


def write_stl(mesh, path):
    with open(path, "wb") as f:
        f.write(b"software fixture".ljust(80, b"\0") + struct.pack("<I", len(mesh.faces)))
        for triangle in mesh.triangles:
            f.write(struct.pack("<12fH", *(list([0, 0, 0]) + triangle.reshape(-1).tolist() + [0])))


def structured_bar(length=10.0, width=2.0, height=2.0, nx=4, order=2):
    """Independent conforming Freudenthal tetrahedral mesh of a rectangular bar."""
    nodes = [[x, y, z] for x in np.linspace(0, length, nx + 1) for y in (0, width) for z in (0, height)]

    def index(i, j, k):
        return 4 * i + 2 * j + k

    elements = []
    for i in range(nx):
        for permutation in itertools.permutations(range(3)):
            corner = np.array([i, 0, 0])
            tet = [index(*corner)]
            for axis in permutation:
                corner = corner.copy()
                corner[axis] += 1
                tet.append(index(*corner))
            elements.append(tet)
    if order == 2:
        midpoint = {}
        for element in elements:
            for i, j in ((0, 1), (1, 2), (2, 0), (0, 3), (2, 3), (1, 3)):
                edge = tuple(sorted((element[i], element[j])))
                if edge not in midpoint:
                    midpoint[edge] = len(nodes)
                    nodes.append(((np.asarray(nodes[edge[0]]) + nodes[edge[1]]) / 2).tolist())
                element.append(midpoint[edge])
    return {"nodes_mm": nodes, "elements": elements}


def axial_inputs(mesh):
    count = len(mesh["elements"])
    elastic = {"E_xy_MPa": 2000.0, "E_z_MPa": 2000.0, "nu_xy": 0.0,
               "nu_xz": 0.0, "G_xz_MPa": 1000.0}
    strength = dict((key, 100.0) for key in STRENGTH)
    normal = np.tile([0, 0, 1], (count, 1))
    loads = [{"id": "axial", "on": {"axis": 0, "value_mm": 10}, "force_N": [40, 0, 0]}]
    fixed = [{"on": {"axis": 0, "value_mm": 0}}]
    return elastic, strength, normal, loads, fixed


_CACHE = {}


def planning_fixture(shape="beam"):
    if shape in _CACHE:
        return copy.deepcopy(_CACHE[shape])
    directory = tempfile.mkdtemp(prefix="ste-functional-")
    path = os.path.join(directory, shape + ".stl")
    mesh = make_box(60, 12, 8)
    if shape == "wedge":
        vertices = mesh.vertices.copy()
        vertices[4:, 2] += vertices[4:, 1] * 0.4
        mesh = geometry.Mesh(vertices, mesh.faces)
    write_stl(mesh, path)
    coarse = volume_mesh.tetrahedralize(path, 6.0)
    if not coarse.ok:
        raise AssertionError(coarse.trace)
    fine = volume_mesh.tetrahedralize(path, 4.0)
    if not fine.ok:
        raise AssertionError(fine.trace)
    initial = planner.plan_case("Рабочая консоль", path, resources_dir=resources_dir(), forced_mode="classic")
    context = dict((k, initial["calibration_context"][k]) for k in ("machine_id", "material_id", "printing_mode"))
    from ste_planner.functional import PROCESS_KEYS
    settings = dict((k, initial["effective_settings"][k]) for k in PROCESS_KEYS)
    baseline_elastic = copy.deepcopy(ELASTIC)
    for key in ("E_xy_MPa", "E_z_MPa", "G_xz_MPa"):
        baseline_elastic[key] /= 2
    baseline_strength = dict((key, value / 2) for key, value in STRENGTH.items())
    baseline = {"id": "baseline", "settings": settings, "source": "software_fixture",
                "protocol": "SOFTWARE fixture of a scoped coupon record; not a physical measurement",
                "context": context, "elastic": baseline_elastic, "strength": baseline_strength}
    conditions = {"temperature_C": 23.0, "environment": "dry_air", "load_regime": "static"}
    baseline["service_conditions"] = copy.deepcopy(conditions)
    improved = copy.deepcopy(baseline)
    improved.update({"id": "fine_strong", "elastic": copy.deepcopy(ELASTIC), "strength": copy.deepcopy(STRENGTH)})
    improved["settings"]["layer_height"] = 0.1
    improved["settings"]["wall_line_count"] = 6
    improved["settings"]["infill_sparse_density"] = 60.0
    normals, _ = geometry.face_normals_areas(mesh)
    surfaces = []
    for sid, description, axis, val, normal in [
            ("root", "левый торец X=0", 0, 0, [-1, 0, 0]),
            ("tip", "правый торец X=60", 0, 60, [1, 0, 0]),
            ("front", "передняя сторона Y=0", 1, 0, [0, -1, 0])]:
        ids = np.where(np.asarray(normals).dot(normal) > 0.99)[0].tolist()
        surfaces.append({"id": sid, "description": description,
                         "selector": {"axis": axis, "value_mm": val}, "face_ids": ids})
    spec = {"purpose": "Рабочая консоль", "geometry_sha256": coarse.value["geometry_sha256"],
            "service_conditions": conditions,
            "volume_mesh": coarse.value, "convergence_mesh": fine.value, "baseline_profile_id": "baseline",
            "profiles": [baseline, improved], "fixed": [{"on": {"axis": 0, "value_mm": 0}}],
            "load_cases": [{"id": "bending", "on": {"axis": 0, "value_mm": 60}, "force_N": [0, 0, -10]}],
            "limits": {"safety_factor": 1.5, "max_displacement_mm": 10}, "refine": False,
            "surface_regions": [{"id": "visible", "face_ids": list(range(len(mesh.faces))), "importance": 1}],
            "blocks": [{"id": "root_block", "bounds_mm": [[-0.1, -1, -1], [15, 20, 20]]},
                       {"id": "middle_block", "bounds_mm": [[15, -1, -1], [45, 20, 20]]},
                       {"id": "tip_block", "bounds_mm": [[45, -1, -1], [60.1, 20, 20]]}]}
    _CACHE[shape] = (path, spec, initial, surfaces)
    return copy.deepcopy(_CACHE[shape])
