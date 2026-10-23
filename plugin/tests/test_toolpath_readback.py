"""Regression checks for post-slice inspection; no printer connection required."""

from ste_planner import gcode


PROGRAM = """G21
G90
M82
M104 S235
M140 S60
G92 E0
;LAYER:0
G0 X0 Y0 Z0.3
;TYPE:SUPPORT
G1 F600 X10 E1
G1 E0.5
G1 E1
;TYPE:WALL-OUTER
G1 Y10 E2
;LAYER:1
G0 Z0.5
;TYPE:SUPPORT-INTERFACE
G1 X0 E3
"""

EXPECTED = {"printing_mode": "classic", "nozzle_temperature": 235, "bed_temperature": 60,
            "layer_height": 0.2, "build_volume": [30, 30, 30], "xy_centered": True}


def statuses(text=PROGRAM, expected=None):
    result = gcode.inspect_toolpath(text, EXPECTED if expected is None else expected)
    return dict((c["check"], c["status"]) for c in result["checks"])


def test_support_statistics_exclude_stationary_unretraction():
    result = gcode.inspect_toolpath(PROGRAM, EXPECTED)
    assert result["summary"]["support_extruded_mm"] == 2.0
    assert result["summary"]["feature_extruded_mm"]["WALL-OUTER"] == 1.0
    assert all(value == "pass" for key, value in statuses().items() if key != "volumetric_flow")
    assert statuses()["volumetric_flow"] == "unknown"


def test_bed_temperature_mismatch_is_failure():
    assert statuses(PROGRAM.replace("M140 S60", "M140 S80"))["bed_temperature"] == "fail"


def test_actual_layer_increments_must_match_profile():
    assert statuses(PROGRAM.replace("G0 Z0.5", "G0 Z0.6"))["layer_schedule"] == "fail"
    assert statuses(PROGRAM.split(";LAYER:1")[0])["layer_schedule"] == "unknown"


def test_nonplanar_classic_layer_is_failure():
    text = PROGRAM.replace("G1 Y10 E2", "G1 Y10 Z0.35 E2")
    assert statuses(text)["layer_schedule"] == "fail"


def test_xyz_bounds_include_negative_and_positive_limits():
    for move in ("G1 X40 E3", "G1 X-40 E3", "G1 X0 Z-0.2 E3", "G1 X0 Y40 E3"):
        text = PROGRAM.replace("G1 X0 E3", move)
        assert statuses(text)["extrusion_within_build_volume"] == "fail"
    positive = dict(EXPECTED, xy_centered=False)
    assert statuses(PROGRAM, positive)["extrusion_within_build_volume"] == "pass"


def test_spatial_reset_requires_coordinate_transform():
    checks = statuses(PROGRAM.replace(";LAYER:1", "G92 Z0\n;LAYER:1"))
    for key in ("extrusion_within_build_volume", "z_within_build_volume", "layer_schedule"):
        assert checks[key] == "unknown"
    # Resetting only the filament counter does not affect XYZ checks.
    assert statuses()["extrusion_within_build_volume"] == "pass"


def test_5d_coordinate_system_rejects_classic_without_rotary_moves():
    assert statuses("G55\n" + PROGRAM)["mode_consistency"] == "fail"


def test_rotary_flow_remains_unknown_even_below_linear_limit():
    checks = statuses(PROGRAM + "G1 A10 E4\n", {"printing_mode": "cylindrical", "q_limit": 100})
    assert checks["volumetric_flow"] == "unknown"
    checks = statuses(PROGRAM + "G1 A10 E4\n", {"printing_mode": "cylindrical", "q_limit": 0.001})
    assert checks["volumetric_flow"] == "fail"


def test_nonfinite_words_are_not_silently_ignored():
    for word in ("X1e999", "Xnan", "Y-inf", "ZInfinity"):
        assert statuses(PROGRAM + "G1 " + word + " E4\n")["finite_coordinates"] == "fail"


def test_invalid_filament_diameter_is_rejected():
    for value in (0, -1, True, None, "1.75", float("nan"), float("inf")):
        try:
            gcode.parse_gcode(PROGRAM, value)
        except ValueError:
            continue
        raise AssertionError("invalid filament diameter accepted")
