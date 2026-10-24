"""Independent geometry/energy/resultant checks, never printing calibration."""
import copy
import math
import gzip
import hashlib
import json
import os
import tempfile
import unittest

import numpy as np

from ste_planner import elasticity as E, gcode, toolpath_physics as P
from functional_fixtures import structured_bar, axial_inputs
from testutil import assert_close


def require_sparse_worker():
    try:
        from scipy.sparse import coo_matrix
        from scipy.sparse.linalg import spsolve, MatrixRankWarning
    except ImportError:
        raise unittest.SkipTest("FE solve requires modern worker; embedded STE still evaluates road geometry/operators")


def decision(mode="classic"):
    return {"route": {"mode": mode}, "effective_settings": {"machine_center_is_zero": True},
            "scene_operations": [{"op": "set_transform", "rotation": np.eye(3).tolist(), "translation": [0, 0, 0]}]}


def pose():
    return {"type": "table_AC", "source": "analysis_assumption", "angle_unit": "degree",
            "interpolation": "linear_unwrapped_joints", "table_pivot_mm": [0, 0, 0],
            "nozzle_axis_machine": [0, 0, 1], "work_offsets": {"G56": np.eye(4).tolist()}}


def test_retraction_repayment_is_not_new_material_and_moving_repayment_clips_path():
    text = "G21\nM83\nG0 X0 Y0 Z1\n;TYPE:WALL-OUTER\nG1 E-2\nG1 X10 E3\n"
    road = P.deposition(text, decision(), 2.).require()["roads"][0]
    assert_close(road["volume_mm3"], math.pi)
    assert_close(road["start_mm"][0], 20/3.)
    assert_close(road["length_mm"], 10/3.)
    records = []
    gcode.parse_gcode(text, segment_callback=records.append)
    assert len(records) == 1


def test_C_only_two_full_turns_have_arc_length_and_conserve_E_volume():
    text = "G21\nM83\nG56\nG0 X0 Y0 Z10 A90 C0\n;TYPE:WALL-OUTER\nG1 C720 E4\n"
    assert P.deposition(text, decision("cylindrical"), 2.).status == "needs_input"
    result = P.deposition(text, decision("cylindrical"), 2., pose()).require()
    assert result["summary"]["rotary_subdivisions"] > 140
    assert_close(result["summary"]["part_volume_mm3"], 4*math.pi)
    assert_close(result["summary"]["part_path_length_mm"], 40*math.pi, rel=.001)
    for road in result["roads"]:
        q = np.asarray(road["frame"])
        assert np.allclose(q.T.dot(q), np.eye(3))
        assert_close(np.linalg.det(q), 1.)
    streamed = P.deposition(text, decision("cylindrical"), 2., pose(), retain_roads=False).require()
    assert streamed["roads"] is None and streamed["summary"] == result["summary"]


def test_prepared_kinematics_matches_independent_matrix_composition():
    contract = pose()
    contract["work_offsets"]["G56"] = [[0, -1, 0, 12], [1, 0, 0, 3], [0, 0, 1, -2], [0, 0, 0, 1]]
    contract["table_pivot_mm"] = [2, 4, -3]
    prepared = P._prepared_pose(contract)
    for a, c in ((0, 0), (90, 720), (30, -755), (-45, 17)):
        axes = {"X": 3., "Y": -5., "Z": 7., "A": a, "C": c}
        direct, optimized = P._pose_point(axes, "G56", contract), prepared(axes, "G56")
        assert np.allclose(direct[0], optimized[0]) and np.allclose(direct[1], optimized[1])


def test_rotary_G92_requires_explicit_contract_and_preserves_physical_turns():
    text = "M83\nG56\nG0 X0 Y0 Z10 A90 C0\n;TYPE:WALL-OUTER\nG1 C360 E1\nG92 C0\nG1 C360 E1\n"
    contract = pose()
    assert P.deposition(text, decision("cylindrical_full"), 2., contract).status == "needs_input"
    contract["rotary_g92"] = "preserve_physical_angle_global"
    result = P.deposition(text, decision("cylindrical_full"), 2., contract).require()
    assert_close(result["summary"]["part_path_length_mm"], 40*math.pi, rel=.001)
    assert len(result["summary"]["tracked_rotary_resets"]) == 1
    assert_close(result["summary"]["part_volume_mm3"], 2*math.pi)


def test_unknown_flow_firmware_retraction_and_spatial_reset_do_not_silently_pass():
    for command in ("M221 S80", "G10", "G92 Z0"):
        text = command + "\nM83\n;TYPE:FILL\nG1 X1 E1\n"
        assert P.deposition(text, decision(), 1.75).status == "needs_input"
    assert P.deposition("M83\n;TYPE:SUPPORT\nG1 X1 E1", decision(), 1.75).status == "invalid_input"


def test_registration_changes_material_directions_in_original_part_frame():
    d = decision()
    d["scene_operations"][0]["rotation"] = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    result = P.deposition("M83\n;TYPE:FILL\nG1 X2 E1", d, 1.75).require()
    assert np.allclose(result["summary"]["normal_structure_tensor"], np.diag([0, 1, 0]))
    text = "M83\n;TYPE:FILL\nG1 X2 E1\nG1 Y3 E.5\n;TYPE:SUPPORT\nG1 X4 E.1"
    kept = P.deposition(text, d, 1.75).require()["summary"]
    streamed = P.deposition(text, d, 1.75, retain_roads=False).require()["summary"]
    for key in ("part_volume_mm3", "part_path_length_mm", "tangent_structure_tensor", "normal_structure_tensor"):
        assert np.allclose(kept[key], streamed[key])


def test_integration_background_is_not_a_fictitious_STL_solid():
    from ste_planner.volume_mesh import deposition_background
    mesh = deposition_background([[0, 0, 0], [2, 3, 4]], 2., "original-source").require()
    assert mesh["is_STL_solid_mesh"] is False and len(mesh["elements"]) == 24
    nodes = np.asarray(mesh["nodes_mm"])
    tets = nodes[np.asarray(mesh["elements"])]
    volume = abs(np.linalg.det(tets[:, 1:] - tets[:, :1])).sum()/6.
    assert_close(volume, 24.)
    assert P.material_field(mesh, []).value["summary"]["void_elements"] == len(tets)


def test_segment_clipping_and_shared_face_partition_preserve_unassigned_volume():
    tet = np.array([[0., 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
    fractions = P.segment_tetra_fractions([-.5, .1, .1], [1.5, .1, .1], [tet])
    assert_close(fractions[0], .4)
    shares, tied = P._partition_intervals(np.array([.2, .2, .6]), np.array([.6, .6, .8]), np.ones(3, bool))
    assert tied and np.allclose(shares, [.2, .2, .2])
    assert_close(shares.sum(), .6)  # outside 40% is never redistributed


def test_full_orthotropy_energy_rotation_and_symbolic_operator_reconstruction():
    props = {"E1_MPa": 3000., "E2_MPa": 1200., "E3_MPa": 700., "nu12": .2,
             "nu13": .15, "nu23": .1, "G12_MPa": 450., "G23_MPa": 200., "G13_MPa": 300.}
    c = E.constitutive(props)
    q = P.frame([1, 2, 0], [0, 0, 1])
    t = E.strain_transform(q)
    strain = np.array([.01, -.02, .03, .04, -.01, .02])
    assert_close(strain.dot(t.T.dot(c).dot(t)).dot(strain), t.dot(strain).dot(c).dot(t.dot(strain)))
    assert np.linalg.eigvalsh(t.T.dot(c).dot(t)).min() > 0
    mesh = {"nodes_mm": [[0., 0, 0], [2, 0, 0], [0, 2, 0], [0, 0, 2]], "elements": [[0, 1, 2, 3]]}
    road = {"start_mm": [.1, .1, .1], "end_mm": [.5, .5, .1], "volume_mm3": .1, "frame": q.tolist()}
    result = P.material_field(mesh, [road], props).require()
    streamed = P.material_field(mesh, lambda consume: consume(road), props).require()
    assert np.allclose(streamed["constitutive_operator"], result["constitutive_operator"])
    assert_close(result["summary"]["assigned_volume_mm3"], .1)
    assert np.allclose(result["element_stiffness_MPa"][0], .1/(8/6.) * t.T.dot(c).dot(t))
    assert result["summary"]["physical_prediction_verified"] is False
    broken = copy.deepcopy(props)
    broken["nu12"] = 10
    try:
        E.constitutive(broken)
        assert False, "non-SPD compliance accepted"
    except ValueError:
        pass


def test_torque_load_has_correct_resultant_reaction_units_and_linear_scaling():
    require_sparse_worker()
    mesh = structured_bar(nx=3, order=2)
    elastic, _, normals, loads, fixed = axial_inputs(mesh)
    loads[0].pop("force_N")
    loads[0]["moment_Nmm"] = [10., 0, 0]
    result = E.solve(mesh, elastic, None, normals, loads, fixed)
    assert result.ok, result.trace
    first = result.require()["cases"][0]
    assert np.allclose(first["reaction_N"], [0, 0, 0], atol=1e-8)
    assert np.allclose(first["reaction_moment_Nmm"], [-10, 0, 0], atol=1e-8)
    assert first["effective_stiffness_N_per_mm"] is None
    assert first["failure_index_max"] is None
    assert_close(first["rotational_stiffness_Nmm_per_rad"] * first["work_conjugate_rotation_rad"], 10.)
    loads[0]["moment_Nmm"] = [20., 0, 0]
    second = E.solve(mesh, elastic, None, normals, loads, fixed).require()["cases"][0]
    assert_close(second["compliance_Nmm"], first["compliance_Nmm"] * 4.)


def test_element_field_has_no_ersatz_void_or_spurious_failure_strength():
    require_sparse_worker()
    mesh = structured_bar(order=1)
    elastic, strength, normals, loads, fixed = axial_inputs(mesh)
    field = np.tile(E.constitutive(elastic), (len(mesh["elements"]), 1, 1))
    result = E.solve(mesh, {}, None, normals, loads, fixed, element_stiffness=field)
    assert result.ok, result.trace
    assert_close(result.value["cases"][0]["compliance_Nmm"], 2.)
    assert E.solve(mesh, {}, strength, normals, loads, fixed, element_stiffness=field).status == "invalid_input"
    field[0] = 0
    assert E.solve(mesh, {}, None, normals, loads, fixed, element_stiffness=field).status == "invalid_input"


def test_material_noise_sensitivity_matches_independent_axial_bar_derivative():
    require_sparse_worker()
    mesh = structured_bar(order=1)
    args = axial_inputs(mesh)
    solved = E.solve(mesh, *args).require()["cases"][0]
    operators = np.tile(P.stiffness_basis(), (len(mesh["elements"]), 1, 1, 1))
    covariance = np.zeros((9, 9))
    covariance[0, 0] = 20.**2  # SOFTWARE fixture, not measured process noise
    result = P.compliance_sensitivity(mesh, operators, solved["maps"]["nodal_displacement_mm"], covariance).require()
    assert_close(result["d_compliance_d_coefficients_Nmm_per_MPa"][0], -40.**2*10/(2000.**2*4))
    assert_close(result["linearized_standard_deviation_Nmm"], .02)


def test_slice_provenance_and_search_reslice_each_orientation_without_fake_winner():
    from ste_planner import sliced_search
    d = decision()
    d["calibration_context"] = {"geometry_sha256": "source-fixture"}
    raw = b"M83\r\n;TYPE:FILL\r\nG1 X2 E1\r\n"
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "part.gcode.gz")
        with gzip.open(path, "wb") as handle:
            handle.write(raw)
        sliced = {"status": "slice_completed", "readback": {"status": "pass"},
                  "gcode": {"sha256": hashlib.sha256(raw).hexdigest()}, "inspection": {"checks": []}}
        applied = {"decision_sha256": sliced_search.decision_digest(d)}
        first = P.analyze_slice(path, d, applied, sliced, 1.75)
        assert first.status == "needs_calibration" and first.value["stiffness"] is None
        changed = copy.deepcopy(d)
        changed["scene_operations"][0]["translation"][0] = 2
        assert P.analyze_slice(path, changed, applied, sliced, 1.75).status == "invalid_input"
        calls = []
        def native(name, candidate):
            calls.append(name)
            return {"gcode_path": path, "applied": {"decision_sha256": sliced_search.decision_digest(candidate)},
                    "sliced": sliced, "filament_diameter_mm": 1.75}
        result = sliced_search.evaluate_candidates([{"id": "one", "decision": d}, {"id": "two", "decision": changed}], native)
        assert calls == ["one", "two"] and result.value["native_slice_invocations"] == 2
        assert result.value["selected"] is None and result.status == "needs_calibration"
