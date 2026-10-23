"""Noise scenarios are explicit assumptions, not manufactured physical results."""
from ste_planner import robustness as R

SETTINGS = {"layer_height": .2, "layer_height_0": .3, "line_width": .4, "speed_print": 45.}
MECHANICS = {"effective_stiffness_N_per_mm": 10., "compliance_Nmm": 2.,
             "max_displacement_mm": .2, "safety_factor_min": 4.}


def test_constant_z_shift_is_not_an_interlayer_thickness_change():
    result = R.assess_process_stability(SETTINGS, MECHANICS,
        {"source": "user_scenario", "bounds": {"z_offset_mm": [-.05, .05]}})
    assert result.status == "needs_calibration"
    assert result.value["envelope"]["first_layer_gap_mm"] == [.25, .35]
    assert result.value["envelope"]["layer_increment_mm"] == [.2, .2]
    assert result.value["envelope"]["effective_stiffness_N_per_mm"] == [10., 10.]
    assert result.value["physical_stability_verified"] is False
    assert result.value["success_probability"] is None


def test_position_jitter_differences_and_constitutive_sensitivity_are_bounded():
    noise = {"source": "software_fixture", "bounds": {"layer_position_error_mm": [-.02, .02],
             "elastic_scale": [.8, 1.2], "strength_scale": [.5, 1.], "load_scale": [1., 2.]}}
    value = R.assess_process_stability(SETTINGS, MECHANICS, noise).value
    assert abs(value["envelope"]["layer_increment_mm"][0] - .16) < 1e-12
    assert abs(value["envelope"]["layer_increment_mm"][1] - .24) < 1e-12
    assert value["envelope"]["safety_factor_min"] == [1., 4.]
    assert value["envelope"]["max_displacement_mm"][1] == .5
    assert value["envelope"]["compliance_Nmm"][1] == 10.


def test_unknown_tolerances_cannot_win_stable_parameter_selection():
    candidates = [{"id": "nominal", "settings": SETTINGS, "mechanics": MECHANICS}]
    noise = {"source": "user_scenario", "bounds": {"z_offset_mm": [-.05, .05]}}
    assert R.choose_stable_candidate(candidates, noise, {})["conditional_selected_id"] is None
    limits = {"first_layer_gap_mm": [.1, .4], "layer_increment_mm": [.1, .3],
              "flow_mm3_s": [0., 10.], "displacement_mm": [0., .3], "safety_factor": [1.5, 100.]}
    value = R.choose_stable_candidate(candidates, noise, limits)
    assert value["conditional_selected_id"] == "nominal"
    assert value["status"] == "needs_calibration" and value["setting_approval"] is False
    limits["first_layer_gap_mm"] = [.28, .32]
    assert R.choose_stable_candidate(candidates, noise, limits)["conditional_selected_id"] is None


def test_noise_is_never_invented_or_accepted_without_provenance():
    assert R.assess_process_stability(SETTINGS, MECHANICS).status == "needs_calibration"
    for noise in ({"bounds": {"z_offset_mm": [-.05, .05]}},
                  {"source": "physical_test", "bounds": {"z_offset_mm": [-.05, .05]}},
                  {"source": "user_scenario", "bounds": {"elastic_scale": [-1, 1]}}):
        assert R.assess_process_stability(SETTINGS, MECHANICS, noise).status == "invalid_input"
