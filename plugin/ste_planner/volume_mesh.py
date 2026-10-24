# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Optional Gmsh preprocessing. No silent voxel/Delaunay filling of STL holes.

The embedded legacy runtime can consume saved volume meshes; a modern worker
with Gmsh prepares them. A lock owns Gmsh's process-wide state.
"""

import hashlib
import itertools
import math
import threading
import uuid

import numpy as np

from . import geometry
from .result import computed, invalid, unknown, MODEL_GEOMETRIC, EVIDENCE_SIMULATION

_LOCK = threading.Lock()


def deposition_background(bounds_mm, size_mm, geometry_sha256):
    """Explicit integration grid, NOT a solid repair/fill of the STL.

    Material exists only where road-volume transfer deposits it. Empty cells
    remain empty; this coarse background does not resolve bead boundaries.
    """
    try:
        bounds = np.asarray(bounds_mm, float)
        size = float(size_mm)
        if (bounds.shape != (2, 3) or not np.isfinite(bounds).all() or
                (bounds[1] <= bounds[0]).any() or not math.isfinite(size) or size <= 0 or not geometry_sha256):
            raise ValueError("finite bounds, positive cell size and original geometry digest required")
        counts = np.maximum(1, np.ceil((bounds[1]-bounds[0])/size).astype(int))
        if 6 * np.prod(counts) > 20000:
            raise ValueError("background exceeds 20000 tetrahedra; choose explicit coarser integration scale")
        axes = [np.linspace(bounds[0, i], bounds[1, i], int(counts[i])+1) for i in range(3)]
        nodes = np.array(list(itertools.product(*axes)))
        def index(corner):
            i, j, k = corner
            return int((i*(counts[1]+1)+j)*(counts[2]+1)+k)
        elements = []
        for cell in itertools.product(*(range(int(n)) for n in counts)):
            for order in itertools.permutations(range(3)):
                corner = np.array(cell)
                tet = [index(corner)]
                for axis in order:
                    corner[axis] += 1
                    tet.append(index(corner))
                elements.append(tet)
        return computed({"nodes_mm": nodes.tolist(), "elements": elements,
                         "geometry_sha256": geometry_sha256, "size_mm": size, "order": 1,
                         "generator": "Freudenthal background integration grid",
                         "domain_role": "integration background; occupancy comes exclusively from toolpaths",
                         "is_STL_solid_mesh": False}, None, "deposition_background", MODEL_GEOMETRIC,
                        evidence=EVIDENCE_SIMULATION)
    except (ValueError, TypeError, OverflowError) as exc:
        return invalid("deposition_background", str(exc))


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tetrahedralize(path, size_mm, order=2):
    """Conforming single-component closed STL -> bounded straight T4/T10 mesh."""
    try:
        size_mm = float(size_mm)
        if not math.isfinite(size_mm) or size_mm <= 0 or order not in (1, 2):
            raise ValueError("positive finite element size and order 1/2 required")
        mesh = geometry.load_stl(path)
        analysis = geometry.analyze_mesh(mesh)
        if analysis["status"] == "invalid" or not analysis["watertight"] or not analysis["consistent_orientation"] or geometry.signed_volume(mesh) <= 0:
            raise ValueError("closed consistently oriented STL required")
        if geometry.connected_components(mesh)[0] != 1:
            raise ValueError("separate components require explicit assembly/contact modelling")
        if max(analysis["size"]) / size_mm > 150:
            raise ValueError("requested mesh size exceeds bounded preprocessing resolution")
    except (ValueError, TypeError, IOError) as exc:
        return invalid("volume_mesh", str(exc))
    try:
        import gmsh
    except (ImportError, OSError):
        return unknown("volume_mesh", "Gmsh unavailable; prepare a saved volume mesh in a modern worker",
                       status="needs_input")
    with _LOCK:
        owned = not gmsh.isInitialized()
        old_model, old_options = None, {}
        name = "ste_" + uuid.uuid4().hex
        try:
            if owned:
                gmsh.initialize()
            else:
                old_model = gmsh.model.getCurrent()
            options = {"General.Terminal": 0, "General.NumThreads": 1, "Mesh.MeshSizeMin": size_mm,
                       "Mesh.MeshSizeMax": size_mm, "Mesh.ElementOrder": order,
                       "Mesh.SecondOrderLinear": 1, "Mesh.RandomSeed": 1}
            for key, value in options.items():
                old_options[key] = gmsh.option.getNumber(key)
                gmsh.option.setNumber(key, value)
            gmsh.model.add(name)
            gmsh.merge(path)
            gmsh.model.mesh.classifySurfaces(math.radians(40), True, True, math.pi)
            gmsh.model.mesh.createGeometry()
            surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
            loop = gmsh.model.geo.addSurfaceLoop(surfaces)
            gmsh.model.geo.addVolume([loop])
            gmsh.model.geo.synchronize()
            gmsh.model.mesh.generate(3)
            tags, flat, _ = gmsh.model.mesh.getNodes()
            nodes = np.asarray(flat).reshape(-1, 3)
            kinds, _, connectivities = gmsh.model.mesh.getElements(3)
            expected_kind, count = (11, 10) if order == 2 else (4, 4)
            if list(kinds) != [expected_kind]:
                raise ValueError("expected straight tetrahedral elements only")
            raw = np.asarray(connectivities[0]).reshape(-1, count)
            tag_to_row = dict((int(tag), i) for i, tag in enumerate(tags))
            elements = np.array([[tag_to_row[int(tag)] for tag in row] for row in raw], dtype=int)
            used, inverse = np.unique(elements, return_inverse=True)
            nodes, elements = nodes[used], inverse.reshape(elements.shape)
            if len(elements) > 20000:
                raise ValueError("volume mesh exceeds 20000-element analysis budget")
            p = nodes[elements[:, :4]]
            volumes = np.abs(np.linalg.det(p[:, 1:] - p[:, :1])) / 6.0
            relative_volume_error = abs(volumes.sum() - abs(analysis["volume_mm3"])) / abs(analysis["volume_mm3"])
            if volumes.min() < 1e-12 or relative_volume_error > 0.01:
                raise ValueError("tetrahedra do not preserve STL volume within 1 percent")
            return computed({"nodes_mm": nodes.tolist(), "elements": elements.tolist(),
                             "geometry_sha256": file_digest(path), "size_mm": size_mm, "order": order,
                             "relative_volume_error": float(relative_volume_error),
                             "generator": "Gmsh " + gmsh.__version__}, None, "volume_mesh", MODEL_GEOMETRIC,
                            evidence=EVIDENCE_SIMULATION)
        except Exception as exc:
            return invalid("volume_mesh", "meshing failed: " + str(exc))
        finally:
            if gmsh.isInitialized():
                if gmsh.model.getCurrent() == name:
                    gmsh.model.remove()
                for key, value in old_options.items():
                    gmsh.option.setNumber(key, value)
                if owned:
                    gmsh.finalize()
                elif old_model:
                    gmsh.model.setCurrent(old_model)
