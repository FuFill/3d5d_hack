# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Software fixtures simulate measured-record schemas; they are not printing evidence."""

import copy

import numpy

from ste_planner import calibration, planner
from ste_planner.result import to_json
from ste_planner.settings_schema import load_machine_schema, SettingResolver
from testutil import resources_dir, example_cases, assert_close


def _resolver():
    schema = load_machine_schema(resources_dir(), planner.DEFAULT_MACHINE)
    return SettingResolver(schema, {"printing_mode": "classic", "layer_height": 0.2},
                           {"0": {"infill_sparse_density": 80.0, "wall_line_count": 4}})


def _context(resolver):
    return {"machine_id": planner.DEFAULT_MACHINE, "material_id": "test_material",
            "printing_mode": "classic", "geometry_sha256": "a" * 64,
            "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "fixed_settings": {"layer_height": resolver.value("layer_height"),
                               "machine_nozzle_size": resolver.value("machine_nozzle_size")}}


def _experiment(context):
    rng = numpy.random.RandomState(23)
    rows = []
    for job in range(90):
        density = [60.0, 80.0, 100.0][job % 3]
        walls = 2 + (job // 3) % 4
        noise = float(rng.normal(scale=0.1))
        for sample in range(2):
            rows.append({"sample_id": "%s:%s" % (job, sample), "print_job_id": str(job),
                         "settings": {"infill_sparse_density": density, "wall_line_count": walls},
                         "outcome": 2 * density + 20 * walls + noise})
    return {"dataset_id": "software_fixture", "source": "physical_test",
            "design": "software fixture of a randomized, controlled physical-test record",
            "context": copy.deepcopy(context), "treatment": "infill_sparse_density",
            "covariates": ["wall_line_count"],
            "units": {"infill_sparse_density": "%", "wall_line_count": "count"},
            "outcome": {"name": "breaking_load", "unit": "N"}, "observations": rows}


def test_calibration_fits_data_and_compiles_percent_change_once():
    resolver = _resolver()
    context = _context(resolver)
    experiment = _experiment(context)
    result = calibration.assess_double_ml([experiment], context, resolver, {"infill_sparse_density": 60})
    assert result.ok and result.evidence is None
    value = result.value[0]["value"]
    assert value["fit"]["value"]["independent_groups"] == 90
    assert abs(value["change"]["value"]["expected_change"] + 40) < 0.1
    payload = value["proposed_patch"]["payload"]
    assert payload["extruders"]["0"]["infill_sparse_density"] == 60.0
    assert resolver.value("infill_sparse_density", extruder="0") == 80.0
    assert len(value["dataset_sha256"]) == 64
    assert to_json(result)                       # no estimator objects leak into the Decision


def test_calibration_rejects_synthetic_data_and_context_transfer():
    resolver = _resolver()
    context = _context(resolver)
    data = _experiment(context)
    data["source"] = "synthetic"
    result = calibration.assess_double_ml([data], context, resolver)
    assert result.value[0]["status"] == "needs_calibration"
    for key in ("material_id", "machine_id", "printing_mode", "geometry_sha256", "rotation"):
        data = _experiment(context)
        data["context"][key] = "different"
        result = calibration.assess_double_ml([data], context, resolver)
        assert result.value[0]["status"] == "out_of_domain", key


def test_calibration_refuses_unit_errors_missing_outcomes_and_duplicate_samples():
    resolver = _resolver()
    context = _context(resolver)
    variants = []
    data = _experiment(context)
    data["units"]["infill_sparse_density"] = "fraction"
    variants.append(data)
    data = _experiment(context)
    data["observations"][0]["outcome"] = None
    variants.append(data)
    data = _experiment(context)
    data["observations"][1]["sample_id"] = data["observations"][0]["sample_id"]
    variants.append(data)
    for data in variants:
        result = calibration.assess_double_ml([data], context, resolver)
        assert result.value[0]["status"] == "invalid_input"
    assert calibration.assess_double_ml({}, context, resolver).status == "invalid_input"
    assert calibration.assess_double_ml([_experiment(context)], context, resolver, {1: 80}).status == "invalid_input"


def test_calibration_database_uses_matching_cohort_only():
    resolver = _resolver()
    context = _context(resolver)
    applicable = _experiment(context)
    other = copy.deepcopy(applicable)
    other["dataset_id"] = "other_material"
    other["context"]["material_id"] = "different"
    result = calibration.assess_double_ml([other, applicable], context, resolver, {"infill_sparse_density": 60})
    assert result.ok
    assert result.value[0]["status"] == "out_of_domain"
    assert result.value[1]["status"] == "computed"


def test_calibration_refuses_extrapolation_and_disabled_targets():
    resolver = _resolver()
    context = _context(resolver)
    data = _experiment(context)
    result = calibration.assess_double_ml([data], context, resolver, {"infill_sparse_density": 20})
    assert result.value[0]["status"] == "out_of_domain"
    changed = resolver.copy_with({"layer_height": 0.3})
    result = calibration.assess_double_ml([data], context, changed)
    assert result.value[0]["status"] == "out_of_domain"
    duplicate = calibration.assess_double_ml([data, data], context, resolver)
    assert duplicate.value[1]["status"] == "invalid_input"
    unknown = calibration.assess_double_ml([data], context, resolver, {"speed_print": 50})
    assert unknown.value[-1]["status"] == "needs_calibration"
    disabled = resolver.copy_with({"support_enable": False})
    data = _experiment(context)
    data["treatment"] = "support_angle"
    data["units"]["support_angle"] = calibration._unit(disabled, "support_angle")
    result = calibration.assess_double_ml([data], context, disabled, {"support_angle": 45})
    assert result.value[0]["status"] == "out_of_domain"


def test_causal_support_does_not_bypass_slicer_bounds_or_fixed_controls():
    resolver = _resolver()
    context = _context(resolver)
    data = _experiment(context)
    for row in data["observations"]:
        if row["settings"]["infill_sparse_density"] == 60:
            row["settings"]["infill_sparse_density"] = -20.0
    result = calibration.assess_double_ml([data], context, resolver, {"infill_sparse_density": -10})
    assert result.value[0]["status"] == "invalid_input"

    data = _experiment(context)
    data["treatment"], data["covariates"] = "layer_height", ["top_layers"]
    data["units"] = {"layer_height": "mm", "top_layers": "count"}
    del data["context"]["fixed_settings"]["layer_height"]
    for i, row in enumerate(data["observations"]):
        height, layers = [0.1, 0.2, 0.3][i % 3], 3 + (i // 3) % 3
        row["settings"] = {"layer_height": height, "top_layers": layers}
        row["outcome"] = 2 * height + 3 * layers
    result = calibration.assess_double_ml([data], context, resolver, {"layer_height": 0.3})
    assert result.value[0]["status"] == "out_of_domain"  # top_layers would change too


def test_planner_integrates_effects_without_changing_patch_or_qualification():
    resources_dir()
    cases = example_cases()
    if not cases:
        from testutil import skip
        skip("organiser examples unavailable")
    _, docx, stl = cases[-1]
    original = planner.plan_case_files(docx, stl)
    assert original["causal_analysis"]["status"] == "needs_calibration"
    context = copy.deepcopy(original["calibration_context"])
    context["fixed_settings"] = {k: context["settings"][k] for k in ("layer_height", "machine_nozzle_size")}
    data = _experiment(context)
    actual = planner.plan_case_files(docx, stl, calibration_data=[data],
                                     setting_changes={"infill_sparse_density": 80})
    assert actual["causal_analysis"]["status"] == "computed"
    change = actual["causal_analysis"]["value"][0]["value"]["change"]["value"]
    assert_close(change["baseline"], original["effective_settings"]["infill_sparse_density"])
    assert abs(change["expected_change"] + 40) < 0.1
    for field in ("setting_patch", "effective_settings", "verification", "qualification_plan", "status"):
        assert to_json(actual[field]) == to_json(original[field]), field
    assert any("Double ML (software_fixture)" in line for line in actual["explanation"])
