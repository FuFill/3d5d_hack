"""Actual sliced roads -> material frames/volume -> existing FE solver.

No STL-only orientation field, invented moduli, void ersatz stiffness, or
automatic print approval. Rotary paths require an explicit pose contract.
Line-volume integration is a documented coarse swept-road approximation,
not exact bead Boolean geometry or a proven homogenization bound.
"""
import hashlib
import gzip
import json
import math

import numpy as np

from . import elasticity, gcode
from .result import computed, invalid, unknown, MODEL_ANALYTIC, EVIDENCE_SIMULATION
from .functional import PROCESS_KEYS

PART_FEATURES = {"WALL-OUTER", "WALL-INNER", "SKIN", "FILL", "INFILL", "BRIDGE"}


class _NeedsInput(ValueError):
    def __init__(self, reason, status="needs_input"):
        self.status = status
        ValueError.__init__(self, reason)


def frame(tangent, normal):
    t, n = np.asarray(tangent, float), np.asarray(normal, float)
    if t.shape != (3,) or n.shape != (3,) or not np.isfinite(np.r_[t, n]).all() or min(np.linalg.norm(t), np.linalg.norm(n)) < 1e-12:
        raise ValueError("finite nonzero road tangent and build direction required")
    t, n = t / np.linalg.norm(t), n / np.linalg.norm(n)
    n = n - t.dot(n) * t
    if np.linalg.norm(n) < 1e-8:
        raise ValueError("build direction is parallel to road tangent")
    n /= np.linalg.norm(n)
    return np.column_stack((t, np.cross(n, t), n))


def _proper(rotation):
    r = np.asarray(rotation, float)
    if r.shape != (3, 3) or not np.isfinite(r).all() or not np.allclose(r.T.dot(r), np.eye(3), atol=1e-7) or not np.isclose(np.linalg.det(r), 1., atol=1e-7):
        raise ValueError("proper material/part rotation required")
    return r


def _pose_point(axes, cs, contract):
    """Explicit ideal table A-about-X then carried C-about-Z; not inferred from axis names."""
    if contract.get("type") != "table_AC" or contract.get("angle_unit") != "degree":
        raise ValueError("explicit table_AC contract in degrees required")
    a, c = np.deg2rad([axes["A"], axes["C"]])
    rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    rz = np.array([[np.cos(c), -np.sin(c), 0], [np.sin(c), np.cos(c), 0], [0, 0, 1]])
    rotation = rx.dot(rz)
    work = np.asarray(contract["work_offsets"][cs], float)
    if work.shape != (4, 4) or not np.isfinite(work).all() or not np.allclose(work[3], [0, 0, 0, 1]):
        raise ValueError("rigid work-coordinate transform required")
    _proper(work[:3, :3])
    tcp = work[:3, :3].dot([axes[k] for k in ("X", "Y", "Z")]) + work[:3, 3]
    pivot = np.asarray(contract["table_pivot_mm"], float)
    nozzle = np.asarray(contract["nozzle_axis_machine"], float)
    if pivot.shape != (3,) or nozzle.shape != (3,) or not np.isfinite(np.r_[pivot, nozzle]).all():
        raise ValueError("finite machine pivot/nozzle direction required")
    return rotation.T.dot(tcp - pivot), rotation.T.dot(nozzle)


def _prepared_pose(contract):
    """Validate rigid machine data once; analytically apply Rz(-C)Rx(-A)."""
    if contract.get("type") != "table_AC" or contract.get("angle_unit") != "degree":
        raise ValueError("explicit table_AC contract in degrees required")
    if contract.get("interpolation") != "linear_unwrapped_joints":
        raise ValueError("controller interpolation contract required")
    works = {}
    for cs, work in contract["work_offsets"].items():
        work = np.asarray(work, float)
        if work.shape != (4, 4) or not np.isfinite(work).all() or not np.allclose(work[3], [0, 0, 0, 1]):
            raise ValueError("rigid work-coordinate transform required")
        _proper(work[:3, :3])
        works[cs] = work
    pivot, nozzle = np.asarray(contract["table_pivot_mm"], float), np.asarray(contract["nozzle_axis_machine"], float)
    if pivot.shape != (3,) or nozzle.shape != (3,) or not np.isfinite(np.r_[pivot, nozzle]).all():
        raise ValueError("finite machine pivot/nozzle direction required")
    def point(axes, cs):
        work = works[cs]
        v = work[:3, :3].dot([axes[k] for k in ("X", "Y", "Z")]) + work[:3, 3] - pivot
        a, c = math.radians(axes["A"]), math.radians(axes["C"])
        ca, sa, cc, sc = math.cos(a), math.sin(a), math.cos(c), math.sin(c)
        def inverse(vector):
            x, y, z = vector
            yy, zz = ca*y + sa*z, -sa*y + ca*z
            return np.array([cc*x + sc*yy, -sc*x + cc*yy, zz])
        return inverse(v), inverse(nozzle)
    return point


def deposition(text, decision, filament_diameter_mm, pose_contract=None,
               chord_error_mm=.01, max_segments=600000, retain_roads=True, road_callback=None):
    """Canonical roads in ORIGINAL part coordinates, with mass-conserving extrusion.

    Table interpolation uses unwrapped A/C, including full turns. At most 5 deg
    and the requested radial chord tolerance per subdivision. General controller
    interpolation, offsets and G92 need their own verified contract.
    """
    try:
        if isinstance(filament_diameter_mm, bool) or isinstance(chord_error_mm, bool):
            raise ValueError("boolean is not a filament diameter or spatial tolerance")
        diameter, tolerance = float(filament_diameter_mm), float(chord_error_mm)
        if not np.isfinite([diameter, tolerance]).all() or min(diameter, tolerance) <= 0:
            raise ValueError("positive filament diameter and chord tolerance required")
        mode = decision["route"]["mode"]
        if mode != "classic" and pose_contract is None:
            return unknown("toolpath_deposition", "rotary coordinate letters do not determine physical deposition directions",
                           ["forward_kinematics", "work_offsets", "TCP_and_table_pivot", "controller_interpolation"], status="needs_input")
        if pose_contract and pose_contract.get("source") not in ("analysis_assumption", "machine_calibration"):
            raise ValueError("pose source must explicitly state analysis_assumption or machine_calibration")
        pose_point = _prepared_pose(pose_contract) if mode != "classic" else None
        transform = next(op for op in decision["scene_operations"] if op["op"] == "set_transform")
        rotation = _proper(transform["rotation"])
        shift = np.asarray(transform["translation"], float)
        settings = decision["effective_settings"]
        if shift.shape != (3,) or not np.isfinite(shift).all():
            raise ValueError("finite part registration required")
        origin = np.zeros(3)
        if mode == "classic" and not settings["machine_center_is_zero"]:
            origin[:2] = [settings["machine_width"] / 2, settings["machine_depth"] / 2]
        roads, excluded, subdivisions = [], {}, 0
        stats = {"count": 0, "volume": 0., "length": 0., "a2": np.zeros((3, 3)), "n2": np.zeros((3, 3)), "tools": set()}
        af = math.pi * diameter ** 2 / 4
        def consume(segment):
            nonlocal subdivisions
            feature = segment["feature"]
            if feature not in PART_FEATURES:
                excluded[feature] = excluded.get(feature, 0.) + segment["deposited_filament_mm"] * af
                return
            if mode == "classic":
                if segment["cs"] != "G54" or segment["rotary"] or abs(segment["start"][2] - segment["end"][2]) > 1e-6:
                    raise _NeedsInput("nonplanar/classic work frame needs explicit surface normals/poses")
                points = [rotation.T.dot(np.asarray(segment[k]) - origin - shift) for k in ("start", "end")]
                normal = rotation.T.dot([0., 0., 1.])
                records = [(points[0], points[1], normal, 1.)]
            else:
                if pose_contract.get("interpolation") != "linear_unwrapped_joints":
                    raise ValueError("controller interpolation contract required")
                a, b = segment["start_axes"], segment["end_axes"]
                omega = sum(abs(np.deg2rad(b[k] - a[k])) for k in ("A", "C"))
                radius = max(np.linalg.norm(pose_point(q, segment["cs"])[0]) for q in (a, b))
                velocity = np.linalg.norm([b[k] - a[k] for k in ("X", "Y", "Z")])
                # p(u)=Rz(-C(u)) Rx(-A(u)) v(u), linear v/A/C.
                # ||p''|| <= omega^2*r_max+2*omega*||v'|| and the
                # linear-chord error on a subinterval is <= M/(8*n^2).
                curvature_bound = omega ** 2 * radius + 2 * omega * velocity
                count = max(1, int(math.ceil(math.sqrt(curvature_bound / (8*tolerance)))),
                            int(math.ceil(omega / np.deg2rad(5.))))
                if count > max_segments or (retain_roads and len(roads) + count > max_segments):
                    raise _NeedsInput("rotary subdivision exceeds explicit analysis budget", "needs_resolution")
                records = []
                for j in range(count):
                    values = []
                    for u in (j / float(count), (j + 1.) / count, (j + .5) / count):
                        q = dict((k, a[k] + u * (b[k] - a[k])) for k in a)
                        point, normal = pose_point(q, segment["cs"])
                        values.append((rotation.T.dot(point - shift), rotation.T.dot(normal)))
                    records.append((values[0][0], values[1][0], values[2][1], 1. / count))
                subdivisions += count - 1
            for start, end, normal, fraction in records:
                length = float(np.linalg.norm(end - start))
                if length <= 1e-12:
                    raise ValueError("positive deposition with zero physical path length")
                q = frame(end - start, normal)
                volume = af * segment["deposited_filament_mm"] * fraction
                if retain_roads or road_callback is not None:
                    road = {"start_mm": start.tolist(), "end_mm": end.tolist(), "frame": q.tolist(),
                              "volume_mm3": volume, "area_mm2": volume / length, "length_mm": length,
                            "feature": feature, "layer": segment["layer"], "source_line": segment["line"], "tool": segment["tool"]}
                    if retain_roads:
                        roads.append(road)
                    if road_callback is not None:
                        road_callback(road)
                stats["count"] += 1
                stats["volume"] += volume
                stats["length"] += length
                stats["a2"] += volume * np.outer(q[:, 0], q[:, 0])
                stats["n2"] += volume * np.outer(q[:, 2], q[:, 2])
                stats["tools"].add(segment["tool"])
                if retain_roads and len(roads) > max_segments:
                    raise _NeedsInput("analysis segment budget exceeded", "needs_resolution")
        # Large planar exports need only second moments for summary mode.
        # Batch their vector algebra; retaining millions of Python road/frame
        # objects is unnecessary and can exhaust the embedded machine's RAM.
        if mode == "classic" and not retain_roads and road_callback is None:
            pending = []
            normal = rotation.T.dot([0., 0., 1.])
            def flush():
                if not pending:
                    return
                data = np.asarray(pending, float)
                delta = data[:, :3].dot(rotation)
                lengths = np.linalg.norm(delta, axis=1)
                if (lengths <= 1e-12).any():
                    raise ValueError("positive deposition with zero physical path length")
                tangent = delta / lengths[:, None]
                volumes = af * data[:, 3]
                stats["count"] += len(pending)
                stats["volume"] += float(volumes.sum())
                stats["length"] += float(lengths.sum())
                stats["a2"] += np.einsum("n,ni,nj->ij", volumes, tangent, tangent)
                stats["n2"] += volumes.sum() * np.outer(normal, normal)
                pending[:] = []
            def consume_planar(segment):
                feature = segment["feature"]
                if feature not in PART_FEATURES:
                    excluded[feature] = excluded.get(feature, 0.) + segment["deposited_filament_mm"] * af
                    return
                if segment["cs"] != "G54" or segment["rotary"] or abs(segment["start"][2]-segment["end"][2]) > 1e-6:
                    raise _NeedsInput("nonplanar/classic work frame needs explicit surface normals/poses")
                pending.append([segment["end"][k]-segment["start"][k] for k in range(3)] + [segment["deposited_filament_mm"]])
                stats["tools"].add(segment["tool"])
                if len(pending) >= 4096:
                    flush()
            parsed = gcode.parse_gcode(text, diameter, segment_callback=consume_planar)
            flush()
        else:
            parsed = gcode.parse_gcode(text, diameter, segment_callback=consume,
                physical_rotary_resets=bool(pose_contract and pose_contract.get("rotary_g92") == "preserve_physical_angle_global"))
        if parsed["physical_controls"]:
            return unknown("toolpath_deposition", "flow scaling/firmware retraction needs an explicit controller model",
                           ["controller_extrusion_state"], status="needs_input")
        if parsed["parse_errors"] or parsed["unsupported"]:
            return unknown("toolpath_deposition", "unsupported/nonfinite G-code needs a controller-aware parser",
                           sorted(parsed["unsupported"]), status="needs_input")
        if any(not event["tracked"] for event in parsed["spatial_reset_events"]):
            return unknown("toolpath_deposition", "spatial G92 requires explicit logical-to-physical reset tracking",
                           ["spatial_reset_transform"], status="needs_input")
        if not stats["count"]:
            raise ValueError("no recognized part deposition; support/adhesion/prime cannot replace a part")
        if len(stats["tools"]) > 1:
            return unknown("toolpath_deposition", "multiple part tools require per-tool diameter, offsets and constitutive laws",
                           ["per_tool_material_and_pose"], status="needs_input")
        volume = stats["volume"]
        return computed({"roads": roads if retain_roads else None, "summary": {"road_count": stats["count"], "part_volume_mm3": volume,
            "part_path_length_mm": stats["length"], "tangent_structure_tensor": (stats["a2"]/volume).tolist(),
            "normal_structure_tensor": (stats["n2"]/volume).tolist(), "excluded_volume_mm3": excluded,
            "rotary_subdivisions": subdivisions, "chord_tolerance_mm": tolerance,
            "tracked_rotary_resets": [e for e in parsed["spatial_reset_events"] if e["tracked"]],
            "normalized_gcode_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "pose_source": pose_contract.get("source") if pose_contract else "native_planar_layers_and_registration",
            "opaque_macros": parsed["opaque_macros"], "physical_print_approval": False}}, None,
            "toolpath_deposition", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION,
            note="actual toolpath; nominal bead mass/pose model, not observed as-built geometry")
    except _NeedsInput as exc:
        return unknown("toolpath_deposition", str(exc), status=exc.status)
    except (ValueError, TypeError, KeyError, StopIteration, np.linalg.LinAlgError) as exc:
        return invalid("toolpath_deposition", str(exc))


def _segment_tetra_intervals(start, end, tet_vertices, inverse=None):
    tets = np.asarray(tet_vertices, float)
    origin = tets[:, 0]
    if inverse is None:
        inverse = np.linalg.inv((tets[:, 1:] - origin[:, None]).transpose(0, 2, 1))
    u = np.einsum("nij,nj->ni", inverse, np.asarray(start) - origin)
    v = np.einsum("nij,nj->ni", inverse, np.asarray(end) - origin)
    a, b = np.column_stack((1-u.sum(axis=1), u)), np.column_stack((1-v.sum(axis=1), v))
    change = b - a
    lower, upper = np.zeros(len(tets)), np.ones(len(tets))
    valid = np.ones(len(tets), dtype=bool)
    for axis in range(4):
        static = np.abs(change[:, axis]) < 1e-14
        valid &= ~(static & (a[:, axis] < -1e-10))
        positive, negative = change[:, axis] > 1e-14, change[:, axis] < -1e-14
        lower[positive] = np.maximum(lower[positive], -a[positive, axis] / change[positive, axis])
        upper[negative] = np.minimum(upper[negative], -a[negative, axis] / change[negative, axis])
    return lower, upper, valid & (upper > lower)


def segment_tetra_fractions(start, end, tet_vertices):
    """Exact centreline clipping in barycentric coordinates; returns length fractions."""
    lower, upper, valid = _segment_tetra_intervals(start, end, tet_vertices)
    return np.where(valid, upper-lower, 0.)


def _partition_intervals(lower, upper, valid):
    """Assign each covered interval once, splitting shared-face ties symmetrically.

    Unlike normalization by sum, this preserves genuinely unassigned portions
    of a segment and does not alter the non-overlapping portion's density.
    """
    events = []
    result = np.zeros(len(lower))
    for i in np.flatnonzero(valid):
        events.extend([(float(lower[i]), 1, int(i)), (float(upper[i]), -1, int(i))])
    events.sort()
    active, previous, tied = set(), 0., False
    for position, direction, index in events:
        if position > previous and active:
            share = (position-previous)/len(active)
            for owner in active:
                result[owner] += share
            tied = tied or len(active) > 1
        if direction > 0:
            active.add(index)
        else:
            active.discard(index)
        previous = position
    return result, tied


def stiffness_basis():
    """D11,D22,D33,D12,D13,D23,G12,G23,G13; no invented modulus."""
    result = []
    for i, j in ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2), (3, 3), (4, 4), (5, 5)):
        h = np.zeros((6, 6))
        h[i, j] = h[j, i] = 1.
        result.append(h)
    return np.asarray(result)


def material_field(volume_mesh, roads, road_elastic=None, max_pairs=30000000):
    """Mass-conserving A*length/tet-volume field. Shared-face ties counted once.

    Constant-strain mixture; reports unresolved porosity, overlap and material
    outside the design mesh. Does not fill void cells or invent solid modulus.
    """
    try:
        from scipy.spatial import cKDTree
        nodes, elements = elasticity._mesh(volume_mesh)
        tets = nodes[elements[:, :4]]
        centers = tets.mean(axis=1)
        inverse_tets = np.linalg.inv((tets[:, 1:] - tets[:, :1]).transpose(0, 2, 1))
        volumes = np.abs(np.linalg.det(tets[:, 1:] - tets[:, :1])) / 6.
        radii = np.linalg.norm(tets - centers[:, None], axis=2).max(axis=1)
        tree = cKDTree(centers)
        c0 = elasticity.constitutive(road_elastic) if road_elastic is not None else None
        basis_matrices = stiffness_basis()
        operators = np.zeros((len(elements), 9, 6, 6))
        deposited = np.zeros(len(elements))
        assigned, pairs, boundary_ties, total = 0., 0, 0, 0.
        def integrate(road):
            nonlocal assigned, pairs, boundary_ties, total
            total += road["volume_mm3"]
            a, b = np.asarray(road["start_mm"]), np.asarray(road["end_mm"])
            ids = np.asarray(tree.query_ball_point((a+b)/2, np.linalg.norm(b-a)/2 + radii.max()), int)
            if not len(ids):
                return
            mask = ((np.maximum(a, b) >= tets[ids].min(axis=1)-1e-9) &
                    (np.minimum(a, b) <= tets[ids].max(axis=1)+1e-9)).all(axis=1)
            ids = ids[mask]
            pairs += len(ids)
            if pairs > max_pairs:
                raise _NeedsInput("line/tet integration budget exceeded", "needs_resolution")
            if not len(ids):
                return
            lower, upper, valid = _segment_tetra_intervals(a, b, tets[ids], inverse_tets[ids])
            # An entire centreline on shared tetrahedral faces can have positive
            # measure in multiple closed tets. Conservative per-road partition;
            # never deposit the same E-volume twice.
            fraction, tied = _partition_intervals(lower, upper, valid)
            boundary_ties += int(tied)
            dv = road["volume_mm3"] * fraction
            transform = elasticity.strain_transform(_proper(road["frame"]))
            transformed = np.matmul(np.matmul(transform.T, basis_matrices), transform)
            operators[ids] += dv[:, None, None, None] * transformed
            deposited[ids] += dv
            assigned += float(dv.sum())
        if callable(roads):
            roads(integrate)
        else:
            for road in roads:
                integrate(road)
        phi = deposited / volumes
        active = np.flatnonzero(deposited > 0)
        operators /= volumes[:, None, None, None]
        stiffness = None
        if c0 is not None:
            coefficients = np.array([c0[i, j] for i, j in ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2), (3, 3), (4, 4), (5, 5))])
            stiffness = np.einsum("naij,a->nij", operators, coefficients).tolist()
        return computed({"element_stiffness_MPa": stiffness,
            "constitutive_operator": operators.tolist(),
            "operator_coefficients": ["D11", "D22", "D33", "D12", "D13", "D23", "G12", "G23", "G13"],
            "element_deposition_fraction": phi.tolist(), "active_elements": active.tolist(),
            "summary": {"assigned_volume_mm3": assigned, "unassigned_volume_mm3": max(0., total-assigned),
                "mass_assignment_relative_error": abs(total-assigned)/total if total else None,
                "void_elements": int((deposited == 0).sum()), "fraction_above_one_elements": int((phi > 1+1e-7).sum()),
                "fraction_range": [float(phi.min()), float(phi.max())], "shared_face_road_ties": boundary_ties,
                "line_tet_pairs": pairs, "model": "coarse line-volume constant-strain approximation",
                "physical_prediction_verified": False, "requires_mesh_and_bead_convergence": True}},
            None, "toolpath_material_field", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION)
    except _NeedsInput as exc:
        return unknown("toolpath_material_field", str(exc), status=exc.status)
    except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as exc:
        return invalid("toolpath_material_field", str(exc))


def compliance_sensitivity(volume_mesh, operators, displacement_mm, covariance_MPa2=None):
    """Exact linear-FE dC/dc for fixed loads/geometry and homogeneous fixtures.

    c=(D11,D22,D33,D12,D13,D23,G12,G23,G13). Covariance propagation is only
    first-order; it cannot represent contact changes or a re-sliced topology.
    No noise distribution or coefficient uncertainty is supplied by default.
    """
    try:
        nodes, elements = elasticity._mesh(volume_mesh)
        field, u = np.asarray(operators, float), np.asarray(displacement_mm, float)
        if field.shape != (len(elements), 9, 6, 6) or u.shape != nodes.shape or not np.isfinite(field).all() or not np.isfinite(u).all():
            raise ValueError("matching finite nine-operator field and solved nodal displacement required")
        gradient = np.zeros(9)
        for index, element in enumerate(elements):
            volume, gradients = elasticity._gradients(nodes[element], len(element))
            for derivative in gradients:
                strain = elasticity._b_matrix(derivative).dot(u[element].reshape(-1))
                gradient -= volume/len(gradients) * np.einsum("i,aij,j->a", strain, field[index], strain)
        value = {"d_compliance_d_coefficients_Nmm_per_MPa": gradient.tolist(),
                 "coefficient_order": ["D11", "D22", "D33", "D12", "D13", "D23", "G12", "G23", "G13"],
                 "linearized_variance_Nmm2": None, "linearized_standard_deviation_Nmm": None,
                 "domain": "fixed force pattern, fixed deposition geometry, linear SPD stiffness, zero prescribed fixture displacement",
                 "noise_distribution_assumed": False, "physical_print_approval": False}
        if covariance_MPa2 is not None:
            covariance = np.asarray(covariance_MPa2, float)
            if (covariance.shape != (9, 9) or not np.isfinite(covariance).all() or
                    not np.allclose(covariance, covariance.T) or np.linalg.eigvalsh(covariance).min() < -1e-10):
                raise ValueError("finite symmetric positive-semidefinite measured coefficient covariance required")
            variance = max(0., float(gradient.dot(covariance).dot(gradient)))
            value["linearized_variance_Nmm2"] = variance
            value["linearized_standard_deviation_Nmm"] = math.sqrt(variance)
        return computed(value, None, "toolpath_compliance_sensitivity", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION,
                        note="first-order uncertainty propagation; no nonlinear/contact/path-switch guarantee")
    except (ValueError, TypeError, KeyError, np.linalg.LinAlgError) as exc:
        return invalid("toolpath_compliance_sensitivity", str(exc))


def analyze_slice(gcode_path, decision, applied, sliced, filament_diameter_mm,
                  pose_contract=None, volume_mesh=None, measured_material=None, load_cases=None, fixed=None,
                  service_conditions=None):
    """Native provenance guard + optional reuse of existing FEA, always fail closed.

    A symbolic geometry/material operator is available without manufacturing
    constants. Actual stiffness needs measured properties, service fixtures and
    loads. Coarse line-volume fields require separate bead/mesh convergence.
    """
    try:
        opener = gzip.open if str(gcode_path).endswith(".gz") else open
        with opener(gcode_path, "rb") as handle:
            raw = handle.read()
        digest = hashlib.sha256(raw).hexdigest()
        expected = hashlib.sha256(json.dumps(decision, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
        if applied.get("decision_sha256") != expected or sliced.get("gcode", {}).get("sha256") != digest:
            raise ValueError("slice/decision provenance mismatch; candidate must be re-sliced")
        if sliced.get("status") != "slice_completed" or sliced.get("readback", {}).get("status") != "pass":
            raise ValueError("completed native slice and matching effective readback required")
        bad = [c["check"] for c in sliced["inspection"]["checks"] if c["status"] == "fail"]
        result = deposition(raw.decode("utf-8").replace("\r\n", "\n"), decision, filament_diameter_mm, pose_contract,
                            retain_roads=False)
        if not result.ok:
            return result
        roads = result.value["roads"]
        value = {"deposition": result.value["summary"], "gcode_bytes_sha256": digest,
                 "source_geometry_sha256": decision["calibration_context"]["geometry_sha256"],
                 "native_failed_checks": bad, "stiffness": None, "failure_capacity": None,
                 "physical_print_approval": False, "missing": []}
        if pose_contract and pose_contract.get("source") != "machine_calibration":
            value["missing"].append("measured/authoritative kinematic contract; nominal pose is an analysis assumption")
        mode_settings = sliced.get("physics_context", {}).get("mode_specific_settings")
        if decision["route"]["mode"] != "classic" and mode_settings is None:
            value["missing"].append("native mode-specific process settings for constitutive calibration scope")
        properties = None
        if measured_material is None:
            value["missing"].append("measured road/interface constitutive properties and protocol")
        else:
            if measured_material.get("source") != "physical_test" or not measured_material.get("protocol"):
                raise ValueError("material calibration needs physical-test provenance and protocol")
            if measured_material.get("constitutive_scale") != "printed_road":
                raise ValueError("road-volume model requires printed_road properties, not twice-scaled infill coupon moduli")
            if (not service_conditions or measured_material.get("service_conditions") != service_conditions or
                    any(k not in service_conditions for k in ("temperature_C", "environment", "load_regime"))):
                raise ValueError("measured constitutive properties must match explicit service conditions")
            scope = measured_material.get("context", {})
            for key in ("machine_id", "material_id", "printing_mode"):
                if scope.get(key) != decision["calibration_context"][key]:
                    raise ValueError("material calibration scope mismatch: " + key)
            if measured_material.get("settings") != dict((k, decision["effective_settings"][k]) for k in PROCESS_KEYS):
                raise ValueError("constitutive calibration settings differ from actual sliced profile")
            if decision["route"]["mode"] != "classic" and (mode_settings is None or
                    measured_material.get("mode_specific_settings") != mode_settings):
                raise ValueError("5D calibration requires matching native cylindrical surface/core settings")
            properties = measured_material["elastic"]
        if volume_mesh is not None:
            if volume_mesh.get("geometry_sha256") != decision["calibration_context"]["geometry_sha256"]:
                raise ValueError("volume mesh is not the sliced source geometry")
            def road_provider(consume):
                streamed = deposition(raw.decode("utf-8").replace("\r\n", "\n"), decision, filament_diameter_mm,
                                      pose_contract, retain_roads=False, road_callback=consume)
                if not streamed.ok:
                    raise _NeedsInput(str(streamed.trace), streamed.status)
            field = material_field(volume_mesh, road_provider, properties)
            if not field.ok:
                return field
            value["material_field"] = field.value
            value["missing"].append("bead/mesh convergence and resolved contact/porosity before physical prediction")
            admissible = (field.value["summary"]["fraction_above_one_elements"] == 0 and
                          field.value["summary"]["mass_assignment_relative_error"] <= .01)
            if not admissible:
                value["missing"].append("resolve overfilled cells/unassigned deposition before numerical FE evaluation")
            if properties is not None and load_cases and fixed and admissible:
                active = np.asarray(field.value["active_elements"], int)
                if len(active):
                    nodes = np.asarray(volume_mesh["nodes_mm"])
                    elements = np.asarray(volume_mesh["elements"], int)[active]
                    used, inverse = np.unique(elements, return_inverse=True)
                    domain = {"nodes_mm": nodes[used].tolist(), "elements": inverse.reshape(elements.shape).tolist()}
                    c = np.asarray(field.value["element_stiffness_MPa"])[active]
                    solved = elasticity.solve(domain, {}, None, np.tile([0, 0, 1.], (len(active), 1)),
                                              load_cases, fixed, element_stiffness=c)
                    value["conditional_coarse_FE"] = solved.to_dict()
                    if solved.ok:
                        operators = np.asarray(field.value["constitutive_operator"])[active]
                        value["constitutive_noise_sensitivity"] = [compliance_sensitivity(domain, operators,
                            case["maps"]["nodal_displacement_mm"], measured_material.get("coefficient_covariance_MPa2")).to_dict()
                            for case in solved.value["cases"]]
                        if measured_material.get("coefficient_covariance_MPa2") is None:
                            value["missing"].append("measured coefficient uncertainty for numerical stability assessment")
            else:
                value["missing"].append("explicit service load patches and fixtures, not manufacturing supports")
        else:
            value["missing"].append("volume mesh for toolpath-to-FE transfer")
        out = computed(value, None, "sliced_physics", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION,
                       note="actual slices; no measured constants or service boundary conditions invented")
        out.status = "needs_calibration" if not bad else "needs_slice_verification"
        return out
    except (ValueError, TypeError, KeyError, IOError, np.linalg.LinAlgError) as exc:
        return invalid("sliced_physics", str(exc))
