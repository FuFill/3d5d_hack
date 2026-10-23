"""Independent exact bar solution, energy balance, rotations and invalid FE inputs."""

import copy

import numpy as np

from ste_planner import elasticity as E
from functional_fixtures import structured_bar, axial_inputs
from testutil import assert_close


def test_axial_bar_matches_exact_solution_for_T4_and_T10():
    for order in (1, 2):
        mesh = structured_bar(order=order)
        result = E.solve(mesh, *axial_inputs(mesh))
        assert result.ok, result.trace
        case = result.value["cases"][0]
        assert_close(case["load_weighted_deflection_mm"], 40 * 10 / (2000 * 4), rel=1e-9)
        assert_close(case["effective_stiffness_N_per_mm"], 2000 * 4 / 10, rel=1e-9)
        assert_close(case["compliance_Nmm"], 2.0, rel=1e-9)
        assert_close(case["failure_index_max"], 0.1, rel=1e-9)
        assert_close(case["reaction_N"][0], -40.0, rel=1e-9)
        assert case["equilibrium_relative_residual"] < 1e-9
        assert case["energy_relative_error"] < 1e-9


def test_block_energy_is_additive_and_not_inverse_local_stiffness():
    mesh = structured_bar(nx=4)
    blocks = [{"id": "first", "bounds_mm": [[-1, -1, -1], [5, 3, 3]]},
              {"id": "second", "bounds_mm": [[5, -1, -1], [11, 3, 3]]}]
    result = E.solve(mesh, *axial_inputs(mesh), blocks=blocks)
    assert result.ok, result.trace
    case = result.value["cases"][0]
    assert_close(sum(b["compliance_contribution_Nmm"] for b in case["blocks"]), case["compliance_Nmm"])
    for block in case["blocks"]:
        assert_close(block["energy_share"], 0.5, rel=1e-8)


def test_adjacent_block_boundary_centroids_are_assigned_once():
    mesh = structured_bar(nx=4)
    centers = np.asarray(mesh["nodes_mm"])[np.asarray(mesh["elements"])[:, :4]].mean(axis=1)
    cut = float(centers[len(centers) // 2, 0])
    assert (centers[:, 0] == cut).any()
    blocks = [{"id": "left", "bounds_mm": [[-1, -1, -1], [cut, 3, 3]]},
              {"id": "right", "bounds_mm": [[cut, -1, -1], [11, 3, 3]]}]
    result = E.solve(mesh, *axial_inputs(mesh), blocks=blocks)
    assert result.ok, result.trace
    case = result.value["cases"][0]
    assert_close(sum(b["compliance_contribution_Nmm"] for b in case["blocks"]), case["compliance_Nmm"])
    blocks[1]["bounds_mm"][0][0] = cut - 0.1
    assert E.solve(mesh, *axial_inputs(mesh), blocks=blocks).status == "invalid_input"


def test_orthotropic_rotation_energy_and_positive_definiteness():
    c = E.constitutive({"E_xy_MPa": 2000, "E_z_MPa": 800, "nu_xy": 0.25,
                        "nu_xz": 0.1, "G_xz_MPa": 400})
    transform = E.strain_transform(E.basis([0.2, 0.3, 0.4]))
    strain = np.array([0.01, -0.02, 0.03, 0.02, -0.01, 0.04])
    assert_close(strain.dot(transform.T.dot(c).dot(transform)).dot(strain),
                 transform.dot(strain).dot(c).dot(transform.dot(strain)))
    assert np.linalg.eigvalsh(transform.T.dot(c).dot(transform)).min() > 0


def test_linear_load_scaling_and_directional_strengths():
    mesh = structured_bar()
    args = list(axial_inputs(mesh))
    first = E.solve(mesh, *args).require()["cases"][0]
    args[3][0]["force_N"] = [80, 0, 0]
    second = E.solve(mesh, *args).require()["cases"][0]
    assert_close(second["compliance_Nmm"], 4 * first["compliance_Nmm"])
    assert_close(second["max_displacement_mm"], 2 * first["max_displacement_mm"])
    assert_close(second["safety_factor_min"], first["safety_factor_min"] / 2)
    strengths = args[1]
    strengths["tensile_z_MPa"] = 5
    assert_close(E.failure_index(np.array([0, 0, 10, 0, 0, 0]), strengths), 2)


def test_refuse_unconstrained_invalid_properties_bad_force_and_bad_elements():
    mesh = structured_bar()
    args = list(axial_inputs(mesh))
    bad = copy.deepcopy(args)
    bad[0]["E_z_MPa"] = -1
    assert E.solve(mesh, *bad).status == "invalid_input"
    bad = copy.deepcopy(args)
    bad[4] = [{"on": {"axis": 0, "value_mm": 0}, "dofs": [0]}]
    assert E.solve(mesh, *bad).status == "invalid_input"
    bad = copy.deepcopy(args)
    bad[3][0]["on"] = {"axis": 0, "value_mm": -1}
    assert E.solve(mesh, *bad).status == "invalid_input"
    bad = copy.deepcopy(mesh)
    bad["elements"][0][0] = -1
    assert E.solve(bad, *args).status == "invalid_input"
    bad = copy.deepcopy(args)
    bad[3][0]["force_N"] = [float("nan"), 0, 0]
    assert E.solve(mesh, *bad).status == "invalid_input"


def test_t10_rejects_wrong_midpoint_order_instead_of_wrong_mechanics():
    mesh = structured_bar()
    mesh["elements"][0][8], mesh["elements"][0][9] = mesh["elements"][0][9], mesh["elements"][0][8]
    assert E.solve(mesh, *axial_inputs(mesh)).status == "invalid_input"


def test_T10_reproduces_quadratic_displacement_gradients_including_vertex_peaks():
    mesh = structured_bar()
    points = np.asarray(mesh["nodes_mm"])[mesh["elements"][0]]
    x, y, z = points.T
    u = np.column_stack((x * x / 2, 2 * y * z, x * z)).reshape(-1)
    for grad, position in zip(E._gradients(points, 10, at_vertices=True)[1], points[:4]):
        x, y, z = position
        assert np.allclose(E._b_matrix(grad).dot(u), [x, 2 * z, x, 0, 2 * y, z], atol=1e-10)
