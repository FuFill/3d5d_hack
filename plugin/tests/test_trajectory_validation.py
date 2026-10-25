"""All-move coverage and independently specified machine-contract examples."""
import copy
import json
import math

from ste_planner.trajectory_validation import validate_trajectory


def machine():
    return {'source': 'unit-test machine calibration',
            'initial_state': {'axes': dict(X=0., Y=0., Z=10., A=0., C=0.),
                              'units': 'mm', 'absolute': True, 'extrusion_absolute': False,
                              'coordinate_system': 'G54', 'e': 0., 'feed_mm_min': 600.},
            'axis_limits': {'X': [-100., 100.], 'Y': [-100., 100.], 'Z': [0., 100.],
                            'A': [-100., 100.], 'C': [-720., 720.]},
            'axis_limits_frame': 'work', 'axis_limits_coordinate_system': 'G54',
            'max_feed_mm_min': 1200., 'feed_mode': 'linear_xyz_mm_per_min', 'rapid_uses_feed': True}


def pose():
    identity = [[1., 0, 0, 0], [0, 1., 0, 0], [0, 0, 1., 0], [0, 0, 0, 1.]]
    offset = copy.deepcopy(identity)
    offset[0][3] = 50.
    return {'type': 'table_AC', 'source': 'machine_calibration', 'angle_unit': 'degree',
            'interpolation': 'linear_unwrapped_joints', 'table_pivot_mm': [0., 0., 0.],
            'nozzle_axis_machine': [0., 0., 1.], 'work_offsets': {'G54': identity, 'G55': offset}}


def codes(result):
    return {issue['code'] for issue in result['issues']}


def test_travel_and_extrusion_are_both_checked_with_source_lines():
    result = validate_trajectory('G1 X10 E1\nG0 Z-2\n', machine())
    assert result['checks']['axis_limits']['status'] == 'fail'
    violation = next(i for i in result['issues'] if i['code'] == 'axis_limit_exceeded')
    assert violation['axis'] == 'Z' and violation['line'] == 2 and violation['value'] == -2.
    assert result['coverage']['moves'] == 2
    assert result['coverage']['travel_moves'] == result['coverage']['extrusion_moves'] == 1
    assert not result['physical_print_approval']


def test_no_contract_does_not_invent_initial_position_modes_or_machine():
    result = validate_trajectory('G1 X10\nG0 Z2')
    assert result['status'] == 'unknown'
    assert result['checks']['kinematics']['status'] == 'unknown'
    assert result['coverage']['known_start_moves'] == 0
    assert 'unknown_initial_or_modal_state' in codes(result)
    assert 'missing_collision_geometry' in codes(result)
    assert result['coverage']['moves'] == 2


def test_unknown_first_start_does_not_hide_known_out_of_range_destination():
    contract = machine()
    contract.pop('initial_state')
    result = validate_trajectory('G21\nG90\nG54\nG1 X300 Y0 Z0 A0 C0', contract)
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert result['evidence']['axis_extrema']['X'] == [300., 300.]
    assert result['checks']['kinematics']['status'] == 'unknown'


def test_relative_and_modal_compact_motion_blocks():
    result = validate_trajectory('G91\nG1X60\nX60\nG90\nG1X0', machine())
    assert result['coverage']['moves'] == 3
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert result['evidence']['axis_extrema']['X'] == [0., 120.]


def test_absolute_relative_extrusion_modes_are_independent_of_motion_modes():
    result = validate_trajectory('M82\nG1 X1 E2\nG91\nG1 X1 E3\nM83\nG1 X1 E-1\nG92 E0\nG1 X1 E1', machine())
    assert result['coverage']['extrusion_moves'] == 3
    assert result['coverage']['travel_moves'] == 1
    assert result['coverage']['spatial_moves'] == 4


def test_feed_and_joint_speed_use_explicit_declared_timing():
    contract = machine()
    contract['max_axis_speed_per_min'] = {'A': 30.}
    result = validate_trajectory('G1 X10 A90 F600\nG1 X20 F1500', contract)
    assert result['checks']['feed_limits']['status'] == 'fail'
    violations = {i['code']: i for i in result['issues']}
    assert math.isclose(violations['axis_speed_exceeded']['value'], 5400.)
    assert violations['feed_limit_exceeded']['line'] == 2


def test_feed_override_applies_and_rapid_semantics_are_not_assumed():
    contract = machine()
    contract.pop('rapid_uses_feed')
    result = validate_trajectory('M220 S300\nG1 X10 F600\nG0 X20', contract)
    assert result['checks']['feed_limits']['status'] == 'fail'
    assert 'feed_limit_exceeded' in codes(result) and 'unresolved_feed_semantics' in codes(result)
    assert result['coverage']['feed_moves_checked'] == 1


def test_rotary_only_feed_remains_unknown_without_timing_contract():
    result = validate_trajectory('G1 C90 F600', machine(), pose())
    assert result['checks']['feed_limits']['status'] == 'unknown'
    assert result['coverage']['pose_moves_checked'] == 1
    assert result['coverage']['pose_samples'] >= 19


def test_G92_rotary_relabels_coordinates_without_erasing_physical_turns():
    contract = machine()
    contract['rotary_g92'] = 'preserve_physical_angle_global'
    result = validate_trajectory('G1 C400\nG92 C0\nG1 C400', contract, pose())
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert result['evidence']['axis_extrema']['C'] == [0., 800.]
    assert result['coverage']['pose_moves_checked'] == 2
    assert result['coverage']['g92_resets'] == [{'line': 2, 'axis': 'C', 'value': 0.}]


def test_G92_rotary_without_explicit_controller_contract_stays_unknown():
    result = validate_trajectory('G1 C400\nG92 C0\nG1 C400', machine(), pose())
    assert result['checks']['axis_limits']['status'] == 'unknown'
    assert 'unresolved_spatial_reset' in codes(result)
    assert result['coverage']['pose_moves_checked'] == 1


def test_linear_G92_preserves_physical_position_for_bounds():
    result = validate_trajectory('G1 X90\nG92 X0\nG1 X20', machine())
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert result['evidence']['axis_extrema']['X'] == [0., 110.]


def test_work_coordinate_switch_maintains_physical_machine_position():
    contract = machine()
    contract['axis_limits_frame'] = 'machine'
    result = validate_trajectory('G1 X10\nG55\nG1 X60', contract, pose())
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert result['evidence']['axis_extrema']['X'] == [0., 110.]
    assert result['coverage']['known_start_moves'] == 2
    assert result['coverage']['pose_moves_checked'] == 2


def test_unknown_work_frame_does_not_reuse_previous_XYZ():
    result = validate_trajectory('G1 X10\nG56\nG1 X20', machine(), pose())
    assert result['checks']['axis_limits']['status'] == 'unknown'
    assert result['coverage']['known_start_moves'] == 1
    assert result['coverage']['pose_moves_checked'] == 1


def test_unwrapped_full_rotation_samples_the_interior():
    contract = machine()
    contract['initial_state']['axes']['A'] = 90.
    result = validate_trajectory('G1 C360', contract, pose())
    assert result['checks']['kinematics']['status'] == 'pass'
    assert result['coverage']['pose_samples'] >= 73
    assert result['evidence']['part_point_bounds_mm']['X'][0] < -9.99
    assert result['evidence']['part_point_bounds_mm']['X'][1] > 9.99
    assert not result['full_clearance_verified']


def test_analysis_assumption_is_not_calibrated_kinematic_pass():
    assumed = pose()
    assumed['source'] = 'analysis_assumption'
    result = validate_trajectory('G1 X10', machine(), assumed)
    assert result['coverage']['pose_moves_checked'] == 1
    assert result['checks']['kinematics']['status'] == 'unknown'
    assert 'unverified_pose_source' in codes(result)


def test_pose_budget_does_not_stop_axis_checking_later_moves():
    result = validate_trajectory('G1 C720\nG1 Z-5', machine(), pose(), max_samples=4)
    assert result['coverage']['sample_budget_exhausted']
    assert result['coverage']['moves'] == 2
    assert result['checks']['axis_limits']['status'] == 'fail'
    assert 'pose_budget_exhausted' in codes(result)


def test_START_END_and_unknown_motion_preserve_coverage_gaps():
    result = validate_trajectory('START\nG21 G90 G54\nG1 X1 Y1 Z1 A0 C0\nG28\nEND', machine(), pose())
    assert result['checks']['kinematics']['status'] == 'unknown'
    assert [i['line'] for i in result['coverage']['opaque_macros']] == [1, 5]
    assert result['coverage']['unsupported_commands'][0]['command'] == 'G28'
    assert 'unmodelled_controller_motion' in codes(result)
    assert not result['physical_print_approval']


def test_malformed_ambiguous_or_nonfinite_blocks_fail_instead_of_passing():
    for block in ['G1 Xnan', 'G1 Xinf', 'G1 X1 X2', 'G1 X1*bad', 'G92 G1 X0']:
        result = validate_trajectory(block, machine(), pose())
        assert result['checks']['syntax']['status'] == 'fail'
        assert result['status'] == 'fail'


def test_static_machine_tool_envelope_detects_travel_collision_between_endpoints():
    geometry = {'frame': 'machine', 'tool_spheres': [{'center_mm': [0, 0, 0], 'radius_mm': .5}],
                'obstacles': [{'type': 'sphere', 'center_mm': [5, 0, 10], 'radius_mm': 1.}]}
    result = validate_trajectory('G0 X10', machine(), pose(), geometry)
    assert result['checks']['collisions']['status'] == 'fail'
    assert result['checks']['collisions']['geometry_result']['issues'][0]['line'] == 1
    assert result['coverage']['collision_segments'] == 1


def test_static_geometry_pass_cannot_hide_macro_trajectory():
    geometry = {'frame': 'machine', 'tool_spheres': [{'center_mm': [0, 0, 0], 'radius_mm': .5}],
                'obstacles': [{'type': 'sphere', 'center_mm': [50, 0, 10], 'radius_mm': 1.}]}
    result = validate_trajectory('G0 X10\nEND', machine(), pose(), geometry)
    assert result['checks']['collisions']['geometry_result']['status'] == 'pass'
    assert result['checks']['collisions']['status'] == 'unknown'


def test_valid_scoped_checks_are_explicitly_separate_from_approval():
    result = validate_trajectory('G1 X10 E1', machine(), pose())
    assert result['checks']['syntax']['status'] == result['checks']['axis_limits']['status'] == 'pass'
    assert result['checks']['feed_limits']['status'] == result['checks']['kinematics']['status'] == 'pass'
    assert result['checks']['dynamics']['status'] == result['checks']['collisions']['status'] == 'unknown'
    assert result['status'] == 'unknown' and result['physical_print_approval'] is False
    json.dumps(result, allow_nan=False)


def test_malformed_contract_does_not_crash_or_silently_pass():
    for bad in ({'axis_limits': None}, {'initial_state': None}, {'initial_state': {'axes': []}},
                {'max_feed_mm_min': float('inf')}, {'pose_contract': [1]}, {'work_offsets': []}):
        result = validate_trajectory('G1 X1', bad)
        assert result['checks']['contract']['status'] == 'fail'
        json.dumps(result, allow_nan=False)


def test_unmodelled_rotary_axis_and_arcs_cannot_produce_kinematic_pass():
    for block in ('G1 X10 B90 F600', 'G2 X10 Y10 I5 J0'):
        result = validate_trajectory(block, machine(), pose())
        assert result['checks']['kinematics']['status'] == 'unknown'
        assert result['checks']['syntax']['status'] == 'unknown'


def test_linear_G92_across_rotated_workframe_remains_unresolved():
    contract_pose = pose()
    contract_pose['work_offsets']['G55'] = [[0, -1, 0, 0], [1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    result = validate_trajectory('G1 X10\nG92 X0\nG55\nG1 X20', machine(), contract_pose)
    assert 'reset_work_rotation_unresolved' in codes(result)
    assert result['checks']['kinematics']['status'] == 'unknown'


def test_default_native_macro_program_still_reports_literal_command_ranges():
    result = validate_trajectory('START\nG56\nG0 X0 Y1 Z8 A90 C-30\nG1 C-360 E1\nEND')
    assert result['evidence']['axis_word_extrema_by_work_frame']['G56']['C'] == [-360., -30.]
    assert result['evidence']['axis_word_extrema_by_work_frame']['G56']['A'] == [90., 90.]
    assert result['checks']['kinematics']['status'] == 'unknown'
    assert result['evidence']['axis_extrema'] == {}


def test_logical_command_ranges_do_not_hide_physical_G92_offset():
    result = validate_trajectory('G1 X90\nG92 X0\nG1 X20', machine())
    assert result['evidence']['commanded_axis_extrema_by_work_frame']['G54']['X'] == [20., 90.]
    assert result['evidence']['axis_extrema']['X'] == [0., 110.]


def test_collision_failures_include_top_level_error_and_source_line():
    geometry = {'frame': 'machine', 'tool_spheres': [{'center_mm': [0, 0, 0], 'radius_mm': .5}],
                'obstacles': [{'type': 'sphere', 'center_mm': [5, 0, 10], 'radius_mm': 1.}]}
    result = validate_trajectory('G0 X10', machine(), pose(), geometry)
    hits = [i for i in result['issues'] if i['check'] == 'collisions' and i['severity'] == 'error']
    assert hits and hits[0]['line'] == 1
