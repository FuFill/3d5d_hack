"""Independent continuum-mechanics oracles; these are NOT physical print tests."""
import math

import numpy as np
import pytest

from ste_planner import elasticity as E, toolpath_physics as P
from functional_fixtures import structured_bar, axial_inputs


def test_hydrostatic_uniaxial_and_shear_invariants_are_distinct():
    pressure, vm, principal = E.stress_invariants(-7. * np.eye(3))
    assert pressure == 7. and vm == 0.
    assert np.allclose(principal, [-7., -7., -7.])
    pressure, vm, _ = E.stress_invariants(E.stress_tensor([-12., 0, 0, 0, 0, 0]))
    assert pressure == 4. and vm == 12.
    pressure, vm, principal = E.stress_invariants(E.stress_tensor([0, 0, 0, 5., 0, 0]))
    assert pressure == 0. and np.isclose(vm, 5 * math.sqrt(3))
    assert np.allclose(principal, [-5., 0, 5.])
    q = P.frame([1., 2., 3.], [0, 0, 1.])
    stress = E.stress_tensor([4., -12., 2., 3., 1., 7.])
    first, rotated = E.stress_invariants(stress), E.stress_invariants(q.dot(stress).dot(q.T))
    for a, b in zip(first, rotated):
        assert np.allclose(a, b, atol=1e-12)


@pytest.mark.parametrize("order", [1, 2])
@pytest.mark.parametrize("pressure", [10., -10.])
def test_normal_pressure_recovers_exact_compressed_or_tensioned_bar(order, pressure):
    mesh = structured_bar(order=order)
    elastic, strength, normals, _, fixed = axial_inputs(mesh)
    load = [{"id": "pressure", "on": {"axis": 0, "value_mm": 10.}, "pressure_MPa": pressure}]
    solved = E.solve(mesh, elastic, strength, normals, load, fixed)
    assert solved.ok, solved.trace
    value = solved.value
    case = value["cases"][0]
    maps = case["maps"]
    # A=4 mm², E=2000 MPa, L=10 mm; stress_xx=-p and u_x=-p*L/E.
    assert np.allclose(case["applied_force_N"], [-pressure * 4, 0, 0], atol=1e-9)
    assert np.allclose(case["reaction_N"], [pressure * 4, 0, 0], atol=1e-9)
    assert np.isclose(case["loaded_area_mm2"], 4.)
    assert np.isclose(case["max_displacement_mm"], abs(pressure) * 10 / 2000)
    expected = np.diag([-pressure, 0, 0])
    assert np.allclose(maps["element_stress_tensor_MPa"], expected, atol=1e-9)
    assert np.allclose(maps["element_pressure_MPa"], pressure / 3., atol=1e-9)
    assert np.allclose(maps["element_pressure_range_MPa"], pressure / 3., atol=1e-9)
    assert np.allclose(maps["element_von_mises_MPa"], abs(pressure), atol=1e-9)
    assert np.allclose(maps["element_von_mises_max_MPa"], abs(pressure), atol=1e-9)
    face_normals = np.asarray(value["boundary_normals"])
    assert np.allclose(maps["boundary_pressure_MPa"], pressure * face_normals[:, 0] ** 2, atol=1e-9)
    assert np.allclose(maps["boundary_shear_traction_MPa"], 0, atol=1e-9)
    # Integrating constant stress over the closed boundary must give zero resultant.
    areas = np.asarray(value["boundary_areas_mm2"])
    assert np.allclose((areas[:, None] * maps["boundary_traction_MPa"]).sum(axis=0), 0., atol=1e-9)
    assert case["equilibrium_relative_residual"] < 1e-10
    assert case["energy_relative_error"] < 1e-10
    assert case["effective_stiffness_N_per_mm"] is None
    assert value["field_semantics"]["contact_pressure_solved"] is False


def test_pressure_normal_does_not_depend_on_tetrahedral_handedness():
    mesh = structured_bar(order=1)
    for tet in mesh["elements"]:
        tet[0], tet[1] = tet[1], tet[0]
    elastic, strength, normals, _, fixed = axial_inputs(mesh)
    load = [{"id": "reverse_connectivity", "on": {"axis": 0, "value_mm": 10}, "pressure_MPa": 3.}]
    solved = E.solve(mesh, elastic, strength, normals, load, fixed)
    assert solved.ok, solved.trace
    assert np.allclose(solved.value["cases"][0]["maps"]["element_stress_tensor_MPa"], np.diag([-3., 0, 0]), atol=1e-9)


@pytest.mark.parametrize("order", [1, 2])
def test_closed_surface_hydrostatic_pressure_matches_exact_bulk_strain(order):
    mesh = structured_bar(order=order)
    _, _, normals, _, _ = axial_inputs(mesh)
    modulus, nu, pressure = 2000., .25, 7.
    elastic = {"E_xy_MPa": modulus, "E_z_MPa": modulus, "nu_xy": nu,
               "nu_xz": nu, "G_xz_MPa": modulus / (2 * (1 + nu))}
    fixed = [{"on": {"axis": axis, "value_mm": 0.}, "dofs": [axis]} for axis in (0, 1, 2)]
    loads = [{"id": "hydrostatic", "on": {"bounds_mm": [[-1., -1., -1.], [11., 3., 3.]]},
              "pressure_MPa": pressure}]
    result = E.solve(mesh, elastic, None, normals, loads, fixed)
    assert result.ok, result.trace
    case = result.value["cases"][0]
    maps = case["maps"]
    assert np.allclose(maps["nodal_displacement_mm"],
                       -pressure * (1 - 2 * nu) / modulus * np.asarray(mesh["nodes_mm"]), atol=1e-10)
    assert np.allclose(maps["element_stress_tensor_MPa"], -pressure * np.eye(3), atol=1e-9)
    assert np.allclose(maps["element_pressure_MPa"], pressure, atol=1e-9)
    assert np.allclose(maps["element_von_mises_MPa"], 0., atol=1e-9)
    assert np.allclose(maps["boundary_pressure_MPa"], pressure, atol=1e-9)
    assert np.allclose(case["applied_force_N"], 0., atol=1e-9)
    assert np.allclose(case["reaction_N"], 0., atol=1e-9)
    assert case["equilibrium_relative_residual"] < 1e-10
    assert case["energy_relative_error"] < 1e-10


def test_global_stress_and_surface_pressure_follow_rotated_orthotropic_part():
    mesh = structured_bar(order=2)
    q = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
    mesh["nodes_mm"] = np.asarray(mesh["nodes_mm"]).dot(q.T).tolist()
    elastic = {"E1_MPa": 3000., "E2_MPa": 1000., "E3_MPa": 500., "nu12": 0., "nu13": 0.,
               "nu23": 0., "G12_MPa": 400., "G23_MPa": 200., "G13_MPa": 250.}
    count = len(mesh["elements"])
    loads = [{"id": "rotated", "on": {"axis": 1, "value_mm": 10.}, "pressure_MPa": 6.}]
    solved = E.solve(mesh, elastic, None, np.tile([0, 0, 1.], (count, 1)), loads,
                     [{"on": {"axis": 1, "value_mm": 0.}}], material_frames=np.tile(q, (count, 1, 1)))
    assert solved.ok, solved.trace
    case = solved.value["cases"][0]
    assert np.allclose(case["maps"]["element_stress_tensor_MPa"], np.diag([0., -6., 0.]), atol=1e-9)
    assert np.isclose(case["max_displacement_mm"], 6. * 10. / 3000.)
    normals = np.asarray(solved.value["boundary_normals"])
    assert np.allclose(case["maps"]["boundary_pressure_MPa"], 6. * normals[:, 1] ** 2, atol=1e-9)


def test_t10_surface_tractions_use_face_centroids_and_keep_vertex_pressure_extrema():
    mesh = structured_bar(order=2)
    elastic, _, normals, loads, fixed = axial_inputs(mesh)
    loads[0]["force_N"] = [0., 0., -2.]
    solved = E.solve(mesh, elastic, None, normals, loads, fixed)
    assert solved.ok, solved.trace
    value = solved.value
    maps = value["cases"][0]["maps"]
    nodes, elements, u = [np.asarray(x) for x in (value["nodes_mm"], value["elements"], maps["nodal_displacement_mm"])]
    # Independent quadratic polynomial interpolation of u, without shape-function
    # gradients from the solver. nu=0 and E=2000 give sigma=2000*sym(grad u).
    coefficients = []
    for element in elements:
        x, y, z = nodes[element].T
        matrix = np.column_stack((np.ones(10), x, y, z, x*x, y*y, z*z, x*y, y*z, x*z))
        coefficients.append(np.linalg.solve(matrix, u[element]))
    def stress(owner, point):
        x, y, z = point
        deriv = np.array([[0, 1, 0, 0, 2*x, 0, 0, y, 0, z],
                          [0, 0, 1, 0, 0, 2*y, 0, x, z, 0],
                          [0, 0, 0, 1, 0, 0, 2*z, 0, y, x]], float)
        grad = deriv.dot(coefficients[owner])
        return 1000. * (grad + grad.T)
    different_from_element_average = False
    for i, owner in enumerate(value["boundary_element_ids"]):
        tensor = stress(owner, value["boundary_centers_mm"][i])
        normal = np.asarray(value["boundary_normals"])[i]
        assert np.allclose(maps["boundary_traction_MPa"][i], tensor.dot(normal), atol=1e-9)
        assert np.isclose(maps["boundary_pressure_MPa"][i], -normal.dot(tensor).dot(normal), atol=1e-9)
        different_from_element_average |= not np.allclose(tensor, maps["element_stress_tensor_MPa"][owner], atol=1e-5)
    assert different_from_element_average
    for owner, element in enumerate(elements):
        exact = [-np.trace(stress(owner, point))/3. for point in nodes[element[:4]]]
        assert np.allclose(maps["element_pressure_range_MPa"][owner], [min(exact), max(exact)], atol=1e-9)


@pytest.mark.parametrize("bad", [True, 0., float("nan"), float("inf"), "10"])
def test_invalid_pressure_and_ambiguous_loads_are_rejected(bad):
    mesh = structured_bar(order=1)
    elastic, strength, normals, loads, fixed = axial_inputs(mesh)
    loads[0] = {"id": "bad", "on": loads[0]["on"], "pressure_MPa": bad}
    assert E.solve(mesh, elastic, strength, normals, loads, fixed).status == "invalid_input"
    loads[0]["pressure_MPa"] = 10.
    loads[0]["force_N"] = [40., 0, 0]
    assert E.solve(mesh, elastic, strength, normals, loads, fixed).status == "invalid_input"


def test_5d_roads_change_axial_stiffness_without_changing_applied_pressure():
    """SOFTWARE integration fixture: E-volume lines, not a manufacturing recipe.

    In each tetrahedron a short road has exactly 10% of its cell volume. Rotating
    C changes the road direction from X to -Y, so axial E is E1 or E2 respectively.
    The external 1 MPa pressure is prescribed in the original service frame.
    """
    mesh = structured_bar(order=1, nx=2)
    points = np.asarray(mesh["nodes_mm"])
    tets = points[np.asarray(mesh["elements"])]
    props = {"E1_MPa": 3000., "E2_MPa": 1000., "E3_MPa": 500., "nu12": 0., "nu13": 0.,
             "nu23": 0., "G12_MPa": 400., "G23_MPa": 200., "G13_MPa": 250.}
    d = {"route": {"mode": "cylindrical"}, "effective_settings": {},
         "scene_operations": [{"op": "set_transform", "rotation": np.eye(3).tolist(), "translation": [0, 0, 0]}]}
    pose = {"type": "table_AC", "source": "analysis_assumption", "angle_unit": "degree",
            "interpolation": "linear_unwrapped_joints", "table_pivot_mm": [0, 0, 0],
            "nozzle_axis_machine": [0, 0, 1], "work_offsets": {"G56": np.eye(4).tolist()}}
    phi = .1
    compliance = []
    for angle, modulus in ((0, 3000.), (90, 1000.)):
        radians = math.radians(angle)
        q = np.array([[math.cos(radians), -math.sin(radians), 0],
                      [math.sin(radians), math.cos(radians), 0], [0, 0, 1.]])
        lines = ["G21", "M83", "G56"]
        for tet in tets:
            center = q.dot(tet.mean(axis=0))
            a, b = center - [.001, 0, 0], center + [.001, 0, 0]
            volume = abs(np.linalg.det(tet[1:] - tet[:1])) / 6.
            lines += ["G0 X%.12f Y%.12f Z%.12f A0 C%d" % (*a, angle), ";TYPE:FILL",
                      "G1 X%.12f Y%.12f Z%.12f E%.12f" % (*b, phi * volume / math.pi)]
        deposited = P.deposition("\n".join(lines), d, 2., pose).require()
        field = P.material_field(mesh, deposited["roads"], props).require()
        assert np.allclose(field["element_deposition_fraction"], phi, atol=1e-9)
        assert field["summary"]["void_elements"] == 0
        _, _, normals, _, fixed = axial_inputs(mesh)
        loads = [{"id": "pressure", "on": {"axis": 0, "value_mm": 10}, "pressure_MPa": 1.}]
        solved = E.solve(mesh, {}, None, normals, loads, fixed,
                         element_stiffness=field["element_stiffness_MPa"])
        assert solved.ok, solved.trace
        case = solved.value["cases"][0]
        assert np.isclose(case["max_displacement_mm"], 10. / (phi * modulus), rtol=1e-8)
        assert np.allclose(case["maps"]["element_pressure_MPa"], 1./3., atol=1e-8)
        assert case["failure_index_max"] is None  # homogenized stress cannot certify bead failure
        compliance.append(case["compliance_Nmm"])
    assert np.isclose(compliance[1] / compliance[0], 3., rtol=1e-8)
