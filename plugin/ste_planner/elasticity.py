# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Small-strain orthotropic tetrahedral elasticity; mm, N, MPa.

T4 and straight-sided Gmsh T10 meshes. SciPy owns sparse assembly/solution;
no mesher, language model, settings heuristic or material constant lives here.
Maps retain element/integration-point maxima; nodal smoothing is visual only.
"""

import warnings

import numpy as np

from .result import computed, invalid, unknown, MODEL_ANALYTIC, EVIDENCE_SIMULATION

_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (2, 3), (1, 3))
_FACES = ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3))


def constitutive(properties):
    """Transverse isotropy: plane 1/2, layer normal 3; five measured constants."""
    if any(isinstance(v, bool) for v in properties.values()):
        raise ValueError("boolean values are not elastic constants")
    ex, ez, nxy, nxz, gxz = [float(properties[k]) for k in
                             ("E_xy_MPa", "E_z_MPa", "nu_xy", "nu_xz", "G_xz_MPa")]
    if not np.isfinite([ex, ez, nxy, nxz, gxz]).all() or min(ex, ez, gxz) <= 0 or nxy <= -1:
        raise ValueError("invalid elastic constants")
    compliance = np.zeros((6, 6))
    compliance[0, 0] = compliance[1, 1] = 1.0 / ex
    compliance[2, 2] = 1.0 / ez
    compliance[0, 1] = compliance[1, 0] = -nxy / ex
    compliance[0, 2] = compliance[2, 0] = -nxz / ex
    compliance[1, 2] = compliance[2, 1] = -nxz / ex
    compliance[3, 3] = 2.0 * (1.0 + nxy) / ex
    compliance[4, 4] = compliance[5, 5] = 1.0 / gxz
    if np.linalg.eigvalsh(compliance).min() <= 0:
        raise ValueError("elastic compliance must be positive definite with reciprocal Poisson ratios")
    return np.linalg.inv(compliance)


def basis(normal):
    """Arbitrary in-plane axes are valid only for transverse isotropy."""
    b = np.asarray(normal, dtype=float)
    if b.shape != (3,) or not np.isfinite(b).all() or np.linalg.norm(b) < 1e-12:
        raise ValueError("a finite nonzero layer normal is required")
    b = b / np.linalg.norm(b)
    ref = np.eye(3)[int(np.argmin(np.abs(b)))]
    first = np.cross(ref, b)
    first /= np.linalg.norm(first)
    return np.column_stack((first, np.cross(b, first), b))


def strain_transform(q):
    """Engineering Voigt order xx,yy,zz,xy,yz,xz; preserves sigma:epsilon."""
    transform = np.empty((6, 6))
    for k in range(6):
        tensor = np.zeros((3, 3))
        if k < 3:
            tensor[k, k] = 1.0
        else:
            i, j = ((0, 1), (1, 2), (0, 2))[k - 3]
            tensor[i, j] = tensor[j, i] = 0.5
        local = q.T.dot(tensor).dot(q)
        transform[:, k] = [local[0, 0], local[1, 1], local[2, 2],
                            2 * local[0, 1], 2 * local[1, 2], 2 * local[0, 2]]
    return transform


def failure_index(stress, strengths):
    if any(isinstance(v, bool) for v in strengths.values()):
        raise ValueError("boolean values are not strengths")
    tensile = np.array([strengths["tensile_xy_MPa"], strengths["tensile_xy_MPa"],
                        strengths["tensile_z_MPa"]], dtype=float)
    compressive = np.array([strengths["compressive_xy_MPa"], strengths["compressive_xy_MPa"],
                            strengths["compressive_z_MPa"]], dtype=float)
    shear = np.array([strengths["shear_xy_MPa"], strengths["shear_xz_MPa"],
                      strengths["shear_xz_MPa"]], dtype=float)
    if not np.isfinite(np.r_[tensile, compressive, shear]).all() or min(tensile.min(), compressive.min(), shear.min()) <= 0:
        raise ValueError("positive finite directional strengths required")
    return float(max((np.maximum(stress[:3], 0) / tensile).max(),
                     (np.maximum(-stress[:3], 0) / compressive).max(),
                     (np.abs(stress[3:]) / shear).max()))


def _gradients(points, count, at_vertices=False):
    mat = np.column_stack((np.ones(4), points[:4]))
    determinant = np.linalg.det(mat)
    if abs(determinant) < 1e-12:
        raise ValueError("degenerate tetrahedron")
    gradients = np.linalg.inv(mat)[1:, :].T
    volume = abs(determinant) / 6.0
    if count == 4:
        return volume, [gradients]
    expected = np.array([(points[i] + points[j]) / 2 for i, j in _EDGES])
    if not np.allclose(points[4:], expected, atol=1e-7, rtol=1e-8):
        raise ValueError("T10 must have straight midside nodes in Gmsh ordering")
    a, b = 0.5854101966249685, 0.1381966011250105
    out = []
    for index in range(4):
        bary = np.eye(4)[index] if at_vertices else np.full(4, b)
        if not at_vertices:
            bary[index] = a
        grad = [(4 * bary[i] - 1) * gradients[i] for i in range(4)]
        grad.extend(4 * (bary[i] * gradients[j] + bary[j] * gradients[i]) for i, j in _EDGES)
        out.append(np.asarray(grad))
    return volume, out


def _b_matrix(grad):
    b = np.zeros((6, 3 * len(grad)))
    for i, (gx, gy, gz) in enumerate(grad):
        b[:, 3 * i:3 * i + 3] = [[gx, 0, 0], [0, gy, 0], [0, 0, gz],
                                  [gy, gx, 0], [0, gz, gy], [gz, 0, gx]]
    return b


def boundary_faces(elements):
    faces = {}
    for element_index, element in enumerate(elements):
        for face in _FACES:
            nodes = [int(element[i]) for i in face]
            if len(element) == 10:
                for i, j in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
                    edge = next(k for k, pair in enumerate(_EDGES) if set(pair) == set((i, j)))
                    nodes.append(int(element[4 + edge]))
            key = tuple(sorted(nodes[:3]))
            faces.setdefault(key, []).append((element_index, nodes))
    if any(len(entries) > 2 for entries in faces.values()):
        raise ValueError("non-manifold volume mesh")
    return [entries[0] for _, entries in sorted(faces.items()) if len(entries) == 1]


def select_points(points, selector):
    if not isinstance(selector, dict):
        raise ValueError("a spatial selector is required")
    if "bounds_mm" in selector:
        bounds = np.asarray(selector["bounds_mm"], dtype=float)
        if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or (bounds[0] > bounds[1]).any():
            raise ValueError("bounds_mm needs finite ordered min/max vectors")
        return ((points >= bounds[0]) & (points <= bounds[1])).all(axis=1)
    axis = selector.get("axis")
    if isinstance(axis, bool) or axis not in (0, 1, 2):
        raise ValueError("selector axis must be 0, 1 or 2")
    value, tolerance = float(selector["value_mm"]), float(selector.get("tolerance_mm", 1e-6))
    if not np.isfinite([value, tolerance]).all() or tolerance <= 0:
        raise ValueError("finite plane and positive tolerance required")
    return np.abs(points[:, axis] - value) <= tolerance


def _mesh(data):
    nodes = np.asarray(data["nodes_mm"], dtype=float)
    raw = np.asarray(data["elements"])
    if nodes.ndim != 2 or nodes.shape[1] != 3 or len(nodes) < 4 or not np.isfinite(nodes).all():
        raise ValueError("finite N x 3 nodes required")
    if raw.ndim != 2 or raw.shape[1] not in (4, 10) or not len(raw):
        raise ValueError("T4 or T10 elements required")
    elements = raw.astype(int)
    if not np.array_equal(raw, elements) or elements.min() < 0 or elements.max() >= len(nodes):
        raise ValueError("invalid node indices")
    if len(np.unique(elements)) != len(nodes):
        raise ValueError("unused nodes in volume mesh")
    if len(elements) > 20000 or len(nodes) > 100000:
        raise ValueError("volume mesh exceeds bounded analysis size")
    return nodes, elements


def solve(volume_mesh, elastic, strengths, normals, load_cases, fixed, blocks=None):
    """Return fields in the original part coordinates for the exact supplied properties.

    Loads are total forces distributed as uniform tractions over selected boundary
    faces. Supports here are SERVICE fixtures; removable print supports are absent.
    """
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.linalg import spsolve, MatrixRankWarning
    except ImportError:
        return unknown("elasticity", "SciPy sparse solver unavailable", status="needs_input")
    try:
        nodes, elements = _mesh(volume_mesh)
        local_c = constitutive(elastic)
        failure_index(np.zeros(6), strengths)  # validate strengths even under zero local stress
        ns = np.asarray(normals, dtype=float)
        if ns.shape != (len(elements), 3) or not np.isfinite(ns).all():
            raise ValueError("one layer normal per element required")
        if not isinstance(load_cases, list) or not load_cases or len(load_cases) > 20:
            raise ValueError("one to twenty explicit load cases required")
        if not isinstance(fixed, list) or not fixed:
            raise ValueError("service fixtures required")
        faces = boundary_faces(elements)
        constrained = set()
        for fixture in fixed:
            ids = np.where(select_points(nodes, fixture["on"]))[0]
            dofs = fixture.get("dofs", [0, 1, 2])
            if not len(ids) or not dofs or any(isinstance(d, bool) or d not in (0, 1, 2) for d in dofs):
                raise ValueError("empty fixture or invalid degrees of freedom")
            constrained.update(int(3 * i + d) for i in ids for d in dofs)
        fixed_ids = sorted(constrained)
        rigid = []
        center = nodes.mean(axis=0)
        for dof in fixed_ids:
            x, y, z = nodes[dof // 3] - center
            rigid.append(np.column_stack((np.eye(3), [[0, z, -y], [-z, 0, x], [y, -x, 0]]))[dof % 3])
        if np.linalg.matrix_rank(np.asarray(rigid)) < 6:
            raise ValueError("fixtures leave rigid body motion unconstrained")
        transforms, b_matrices, peak_b_matrices, volumes, dof_ids, rows, cols, vals = [], [], [], [], [], [], [], []
        for index, element in enumerate(elements):
            volume, gradients = _gradients(nodes[element], len(element))
            transform = strain_transform(basis(ns[index]))
            c = transform.T.dot(local_c).dot(transform)
            bs = [_b_matrix(g) for g in gradients]
            ke = sum(b.T.dot(c).dot(b) for b in bs) * volume / len(bs)
            ids = (3 * element[:, None] + np.arange(3)).reshape(-1)
            rows.append(np.repeat(ids, len(ids)))
            cols.append(np.tile(ids, len(ids)))
            vals.append(ke.reshape(-1))
            transforms.append(transform)
            b_matrices.append(bs)
            peak_b_matrices.append([_b_matrix(g) for g in _gradients(nodes[element], len(element), at_vertices=True)[1]])
            volumes.append(volume)
            dof_ids.append(ids)
        k = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                       shape=(3 * len(nodes), 3 * len(nodes))).tocsr()
        free = np.setdiff1d(np.arange(3 * len(nodes)), fixed_ids)
        if not len(free):
            raise ValueError("all nodes are constrained")
        centers = nodes[elements[:, :4]].mean(axis=1)
        labels = np.full(len(elements), "unassigned", dtype=object)
        block_ids = set()
        for block in blocks or []:
            name = block["id"]
            if not isinstance(name, str) or not name.strip() or name in block_ids or name == "unassigned":
                raise ValueError("distinct nonempty block ids required")
            block_ids.add(name)
            selected = select_points(centers, {"bounds_mm": block["bounds_mm"]})
            if not selected.any() or (labels[selected] != "unassigned").any():
                raise ValueError("empty or overlapping element blocks")
            labels[selected] = name
        outcomes, case_ids = [], set()
        for case in load_cases:
            case_id = case["id"]
            if not isinstance(case_id, str) or not case_id or case_id in case_ids:
                raise ValueError("distinct load-case ids required")
            case_ids.add(case_id)
            force = np.asarray(case["force_N"], dtype=float)
            if force.shape != (3,) or not np.isfinite(force).all() or np.linalg.norm(force) == 0:
                raise ValueError("nonzero finite total force required")
            selected = select_points(nodes, case["on"])
            weights = np.zeros(len(nodes))
            for _, face in faces:
                if not selected[face[:3]].all():
                    continue
                a, b, c = nodes[face[:3]]
                area = np.linalg.norm(np.cross(b - a, c - a)) / 2.0
                # Consistent constant traction: T6 corner integrals zero, mids A/3.
                active = face if len(face) == 3 else face[3:]
                weights[active] += area / 3.0
            if weights.sum() <= 0:
                raise ValueError("load selector must cover boundary faces, not isolated nodes")
            f = (weights[:, None] * force[None, :] / weights.sum()).reshape(-1)
            if np.linalg.norm(f[fixed_ids]) > 1e-12:
                raise ValueError("load overlaps constrained degrees of freedom")
            displacement = np.zeros(3 * len(nodes))
            with warnings.catch_warnings():
                warnings.simplefilter("error", MatrixRankWarning)
                displacement[free] = spsolve(k[free][:, free], f[free])
            if not np.isfinite(displacement).all():
                raise ValueError("singular or non-finite elastic solution")
            residual = k.dot(displacement) - f
            relative_residual = np.linalg.norm(residual[free]) / np.linalg.norm(f[free])
            if relative_residual > 1e-6:
                raise ValueError("elastic equilibrium residual exceeds tolerance")
            density, fis, stress_max, tension_directions, tension_values = [], [], [], [], []
            for element_index, (transform, bs, peak_bs, ids) in enumerate(zip(transforms, b_matrices, peak_b_matrices, dof_ids)):
                strains = [transform.dot(b.dot(displacement[ids])) for b in bs]
                stresses = [local_c.dot(e) for e in strains]
                density.append(float(sum(s.dot(e) for s, e in zip(stresses, strains)) / len(bs)))
                # On a straight T10, stress is affine. Maximum-stress FI is convex,
                # so its element maximum occurs at a vertex, not a Gauss point.
                peak_stresses = [local_c.dot(transform.dot(b.dot(displacement[ids]))) for b in peak_bs]
                fis.append(max(failure_index(s, strengths) for s in peak_stresses))
                stress_max.append(float(max(np.abs(s).max() for s in peak_stresses)))
                s = np.mean(stresses, axis=0)
                local_tensor = np.array([[s[0], s[3], s[5]], [s[3], s[1], s[4]], [s[5], s[4], s[2]]])
                q = basis(ns[element_index])
                principal, directions = np.linalg.eigh(q.dot(local_tensor).dot(q.T))
                tension_directions.append(directions[:, -1].tolist())
                tension_values.append(float(max(principal[-1], 0)))
            contributions = np.asarray(density) * volumes
            compliance = float(f.dot(displacement))
            if compliance <= 0 or not np.isclose(contributions.sum(), compliance, rtol=1e-7):
                raise ValueError("strain energy/work consistency failed")
            nodal_u = displacement.reshape(-1, 3)
            weighted_delta = compliance / np.linalg.norm(force)
            summaries = []
            for label in sorted(set(labels)):
                mask = labels == label
                max_fi = float(np.asarray(fis)[mask].max())
                summaries.append({"id": label, "volume_mm3": float(np.asarray(volumes)[mask].sum()),
                                  "compliance_contribution_Nmm": float(contributions[mask].sum()),
                                  "energy_share": float(contributions[mask].sum() / compliance),
                                  "max_displacement_mm": float(np.linalg.norm(nodal_u[np.unique(elements[mask])], axis=1).max()),
                                  "failure_index_max": max_fi, "safety_factor_min": 1.0 / max_fi if max_fi else None})
            max_fi = max(fis)
            outcomes.append({"id": case_id, "compliance_Nmm": compliance,
                             "effective_stiffness_N_per_mm": float(np.linalg.norm(force) / weighted_delta),
                             "load_weighted_deflection_mm": weighted_delta,
                             "max_displacement_mm": float(np.linalg.norm(nodal_u, axis=1).max()),
                             "failure_index_max": max_fi, "safety_factor_min": 1.0 / max_fi if max_fi else None,
                             "equilibrium_relative_residual": float(relative_residual),
                             "energy_relative_error": float(abs(contributions.sum() - compliance) / compliance),
                             "reaction_N": residual.reshape(-1, 3).sum(axis=0).tolist(), "blocks": summaries,
                             "maps": {"nodal_displacement_mm": nodal_u.tolist(), "element_compliance_density_MPa": density,
                                      "element_failure_index": fis, "element_stress_max_MPa": stress_max,
                                      "principal_tension_direction": tension_directions,
                                      "principal_tension_MPa": tension_values}})
        return computed({"cases": outcomes, "nodes_mm": nodes.tolist(), "elements": elements.tolist(),
                         "element_centers_mm": centers.tolist(), "element_volumes_mm3": volumes,
                         "element_blocks": labels.tolist(), "boundary_faces": [face for _, face in faces],
                         "boundary_element_ids": [element_index for element_index, _ in faces],
                         "volume_mm3": float(sum(volumes)), "element_order": 2 if elements.shape[1] == 10 else 1},
                        None, "elasticity", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION,
                        note="linear elasticity, fixed process constitutive model; no fatigue/thermal qualification")
    except (ValueError, TypeError, KeyError, IndexError, np.linalg.LinAlgError, Warning) as exc:
        return invalid("elasticity", str(exc))
