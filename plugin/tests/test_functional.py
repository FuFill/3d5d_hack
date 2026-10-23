"""Constrained orientation + settings + existing planner + DML safety integration."""

import copy

import numpy as np

from ste_planner import functional, geometry, planner
from ste_planner.result import to_json
from functional_fixtures import planning_fixture
from testutil import resources_dir, assert_close, make_box


def plan(spec, path):
    return planner.plan_case("Рабочая консоль", path, resources_dir=resources_dir(), forced_mode="classic", functional_spec=spec)


def test_profiles_search_with_maps_and_no_regression():
    path, spec, initial, _ = planning_fixture()
    dec = plan(spec, path)
    result = dec["functional_analysis"]
    assert result["status"] == "needs_calibration", result
    value = result["value"]
    assert value["selected"]["profile_id"] == "fine_strong"
    assert value["candidate_count"] >= 12 and value["feasible_count"] > 0
    assert value["mesh_convergence"]["status"] == "pass", value["mesh_convergence"]
    assert value["selected"]["rejected_reasons"] == []
    old = value["baseline"]["mechanics"]["cases"][0]
    new = value["selected"]["mechanics"]["cases"][0]
    assert new["effective_stiffness_N_per_mm"] > old["effective_stiffness_N_per_mm"] * 1.9
    assert new["max_displacement_mm"] < old["max_displacement_mm"] * 0.51
    assert new["safety_factor_min"] > old["safety_factor_min"]
    assert value["maps"]["cases"][0]["element_compliance_density_MPa"]
    assert len(new["blocks"]) == 3
    assert_close(sum(b["compliance_contribution_Nmm"] for b in new["blocks"]), new["compliance_Nmm"])
    assert dec["effective_settings"] == initial["effective_settings"]  # software fixtures never apply
    assert dec["causal_analysis"]["status"] == "needs_calibration"
    assert not value["applied"]
    assert to_json(dec)


def test_geometry_smoothness_improves_but_is_not_reported_as_Ra():
    path, spec, _, _ = planning_fixture("wedge")
    value = plan(spec, path)["functional_analysis"]["value"]
    assert value["selected"], value
    old = value["baseline"]["surface"]["visible_staircase_mm"]
    new = value["selected"]["surface"]["visible_staircase_mm"]
    assert old > 0 and new < old
    assert value["metrics"]["Ra_um"] is None and value["metrics"]["flatness_mm"] is None


def test_scoped_physical_record_contract_updates_plan_and_calibration_context():
    # Simulated records exercise the physical-record CODE path, not printing evidence.
    path, spec, initial, _ = planning_fixture()
    for profile in spec["profiles"]:
        profile["source"] = "physical_test"
    dec = plan(spec, path)
    value = dec["functional_analysis"]["value"]
    assert value["applied"], value["mesh_convergence"]
    assert dec["effective_settings"]["layer_height"] == 0.1
    assert dec["effective_settings"]["wall_line_count"] == 6
    assert dec["effective_settings"]["infill_sparse_density"] == 60.0
    assert dec["calibration_context"]["settings"] == dec["effective_settings"]
    assert dec["scene_operations"][1]["rotation"] == value["selected"]["rotation"]
    assert dec["setting_patch"]["errors"] == []
    assert dec["status"] != "qualified"
    assert any(q["property"] == "functional_strength_stiffness" for q in dec["qualification_plan"])


def test_hard_mechanical_limits_cannot_be_bought_with_smoothness():
    path, spec, _, _ = planning_fixture()
    profile = spec["profiles"][1]
    for key in ("E_xy_MPa", "E_z_MPa", "G_xz_MPa"):
        profile["elastic"][key] /= 10
    result = plan(spec, path)["functional_analysis"]["value"]
    assert result["selected"]["profile_id"] == "baseline"
    assert any("mechanical_regression:bending" in c["rejected_reasons"] for c in result["candidates"])


def test_profiles_refuse_missing_properties_wrong_context_and_wrong_mesh():
    path, spec, _, _ = planning_fixture()
    bad = copy.deepcopy(spec)
    bad["geometry_sha256"] = "wrong"
    assert plan(bad, path)["functional_analysis"]["status"] == "out_of_domain"
    bad = copy.deepcopy(spec)
    bad["profiles"][0]["context"]["material_id"] = "another"
    assert plan(bad, path)["functional_analysis"]["status"] == "out_of_domain"
    bad = copy.deepcopy(spec)
    del bad["profiles"][1]["strength"]["tensile_z_MPa"]
    assert plan(bad, path)["functional_analysis"]["status"] == "invalid_input"
    bad = copy.deepcopy(spec)
    bad["surface_regions"][0]["face_ids"] = [-1]
    assert plan(bad, path)["functional_analysis"]["status"] == "invalid_input"


def test_no_absolute_roughness_or_flatness_pass_without_calibration():
    path, spec, initial, _ = planning_fixture()
    for key in ("max_Ra_um", "max_flatness_mm"):
        bad = copy.deepcopy(spec)
        bad["limits"][key] = 0.01
        actual = plan(bad, path)
        assert actual["functional_analysis"]["status"] == "needs_calibration"
        assert actual["effective_settings"] == initial["effective_settings"]


def test_no_application_without_mesh_convergence_even_for_physical_record():
    path, spec, initial, _ = planning_fixture()
    spec.pop("convergence_mesh")
    for profile in spec["profiles"]:
        profile["source"] = "physical_test"
    actual = plan(spec, path)
    assert actual["functional_analysis"]["status"] == "needs_input"
    assert actual["functional_analysis"]["value"]["applied"] is False
    assert actual["effective_settings"] == initial["effective_settings"]


def test_cylindrical_normals_are_radial_and_full_core_is_axial():
    centers = np.array([[10., 0, 0], [0, 10, 0], [1, 0, 0]])
    route = {"parameters": {"axis": [0, 0, 1], "center": [0, 0, 0], "base_diameter": 4}}
    normals = functional.layer_normals(centers, np.eye(3), "cylindrical_full", route)
    assert np.allclose(normals, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    classic = functional.layer_normals(centers, geometry.rotation_to_z([1, 0, 0]), "classic", None)
    assert np.allclose(classic, [[1, 0, 0]] * 3)


def test_full_rotation_does_not_claim_implicit_yaw_fits_bed():
    mesh = make_box(30, 10, 5)
    ev = geometry.evaluate_orientation(mesh, [0, 0, 1], build_volume=[15, 35, 10], rotation=np.eye(3))
    assert ev["fits_build_volume"] is False
    r = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    ev = geometry.evaluate_orientation(mesh, [0, 0, 1], build_volume=[15, 35, 10], rotation=r)
    assert ev["fits_build_volume"] is True


def test_forbidden_support_contact_is_a_hard_candidate_rejection():
    from ste_planner import routes
    mesh = make_box(20, 10, 5)
    # Place a forbidden underside above the bed using a nonconvex assembly for the geometric screen.
    stem = make_box(4, 4, 10, origin=(8, 3, 0))
    cap = make_box(20, 10, 3, origin=(0, 0, 10))
    combined = geometry.Mesh(np.vstack([stem.vertices, cap.vertices]), np.vstack([stem.faces, cap.faces + len(stem.vertices)]))
    region = [{"id": "underside", "face_ids": [12, 13], "importance": 1, "forbid_support_contact": True}]
    result, _ = functional._surface(combined, np.eye(3), "classic", None, 0.2, region, routes.machine_capabilities())
    assert result["forbidden_contact"] is True
    assert result["support_plan"]["blocked_face_ids"] == [12, 13]


def test_planning_is_deterministic_on_identical_saved_mesh_and_inputs():
    path, spec, _, _ = planning_fixture()
    first, second = plan(spec, path), plan(spec, path)
    assert to_json(first) == to_json(second)


def test_support_modifier_prisms_are_closed_and_carry_real_STE_keys():
    mesh = make_box(20, 10, 5, origin=(0, 0, 10))
    selected = {"mode": "classic", "rotation": np.eye(3),
                "surface": {"support_plan": {"required_face_ids": [0], "blocked_face_ids": [2]}}}
    operations = functional.support_modifiers(mesh, selected)
    assert len(operations) == 2
    for operation in operations:
        modifier = geometry.Mesh(operation["vertices_mm"], operation["faces"])
        topology = geometry.edge_topology(modifier)
        assert topology["boundary_edges"] == 0 and topology["nonmanifold_edges"] == 0
        assert abs(geometry.signed_volume(modifier)) > 0
    assert operations[0]["settings"]["support_mesh"] is True
    assert operations[1]["settings"]["anti_overhang_mesh"] is True


def test_5D_volume_route_uses_radial_physics_and_never_assumes_collision_clearance():
    import os
    import tempfile
    from ste_planner import volume_mesh
    from functional_fixtures import write_stl, ELASTIC, STRENGTH
    from testutil import make_tube
    path = os.path.join(tempfile.mkdtemp(), "tube.stl")
    write_stl(make_tube(8, 4, 20, 24), path)
    volume = volume_mesh.tetrahedralize(path, 4)
    assert volume.ok, volume.trace
    initial = planner.plan_case("Втулка", path, resources_dir=resources_dir(), forced_mode="cylindrical")
    context = dict((k, initial["calibration_context"][k]) for k in ("machine_id", "material_id", "printing_mode"))
    profile = {"id": "radial", "source": "physical_test", "protocol": "SOFTWARE fixture of a physical-record contract",
               "context": context, "elastic": copy.deepcopy(ELASTIC), "strength": copy.deepcopy(STRENGTH),
               "settings": dict((k, initial["effective_settings"][k]) for k in functional.PROCESS_KEYS)}
    conditions = {"temperature_C": 23.0, "environment": "dry_air", "load_regime": "static"}
    profile["service_conditions"] = copy.deepcopy(conditions)
    spec = {"purpose": "Втулка", "geometry_sha256": volume.value["geometry_sha256"], "volume_mesh": volume.value,
            "service_conditions": conditions,
            "profiles": [profile], "baseline_profile_id": "radial", "fixed": [{"on": {"axis": 2, "value_mm": 0}}],
            "load_cases": [{"id": "axial", "on": {"axis": 2, "value_mm": 20}, "force_N": [0, 0, 1]}],
            "limits": {"safety_factor": 1, "max_displacement_mm": 10}, "refine": False}
    actual = planner.plan_case("Втулка", path, resources_dir=resources_dir(), forced_mode="cylindrical", functional_spec=spec)
    value = actual["functional_analysis"]["value"]
    assert value and value["selected"], actual["functional_analysis"]
    assert value["selected"]["surface"]["support_plan"]["status"] == "needs_kinematics"
    assert not value["applied"]
    assert actual["effective_settings"] == initial["effective_settings"]


def test_5D_places_asymmetric_part_on_tooling_axis_not_bbox_center():
    from testutil import make_tube
    cylinder = make_tube(8, 4, 20, 24)
    lug = make_box(20, 3, 5, origin=(5, -1.5, 10))
    mesh = geometry.Mesh(np.vstack([cylinder.vertices, lug.vertices]),
                         np.vstack([cylinder.faces, lug.faces + len(cylinder.vertices)]))
    placed, shift = geometry.place_on_axis(mesh, np.eye(3), [0, 0, 0])
    assert np.allclose(shift, [0, 0, 0])
    assert_close(float(placed.vertices[:, 2].min()), 0)
    assert placed.vertices[:, 0].max() == 25  # eccentric lug is preserved, not centred as a bounding box


def test_service_environment_and_temperature_do_not_transfer_silently():
    path, spec, _, _ = planning_fixture()
    spec["profiles"][0]["service_conditions"]["environment"] = "wet"
    assert plan(spec, path)["functional_analysis"]["status"] == "out_of_domain"
    _, spec, _, _ = planning_fixture()
    decision = planner.plan_case("Рабочая консоль. Рабочая температура +85 °C.", path,
                                 resources_dir=resources_dir(), forced_mode="classic", functional_spec=spec)
    assert decision["functional_analysis"]["status"] == "out_of_domain"


def test_no_inward_or_open_mesh_is_used_for_support_and_strength_search():
    from ste_planner import routes
    from ste_planner.settings_schema import SettingResolver, load_machine_schema
    mesh = make_box(10, 10, 10)
    bad = geometry.Mesh(mesh.vertices, mesh.faces[:, ::-1])
    resolver = SettingResolver(load_machine_schema(resources_dir(), planner.DEFAULT_MACHINE))
    result, patch = functional.optimize(bad, {"purpose": "part", "geometry_sha256": "same"},
                                         {"geometry_sha256": "same"}, np.eye(3), "classic", None,
                                         routes.machine_capabilities(), {"items": [], "resolver": resolver}, resolver)
    assert result.status == "invalid_input" and patch is None
