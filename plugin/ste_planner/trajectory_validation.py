"""Conservative validation of *all* commanded G0/G1 moves, including travel.

This is a program/contract check, not physical print qualification. Joint limits
are checked in their explicitly declared coordinate frame. Forward table_AC
poses reuse toolpath_physics; acceleration and machine-wide clearance are never
inferred from axis letters, slicer dimensions, or a successful parse.

machine_contract accepts initial_state (axes, coordinate_system, absolute,
extrusion_absolute, units='mm', e, feed_mm_min), axis_limits ({axis:[lo,hi]}),
axis_limits_frame ('work' or 'machine'), axis_limits_coordinate_system ('G54'),
max_feed_mm_min, max_axis_speed_per_min, feed_mode
('linear_xyz_mm_per_min'), rapid_uses_feed and optionally pose_contract.
A/C are unwrapped global joints, degrees. G92 requires an explicit
rotary_g92='preserve_physical_angle_global' contract for physical A/C tracking.
The work frame limit range refers to physical coordinates before G92 relabeling.

Example (illustrative numbers, NOT the STE machine's calibrated limits)::

    machine = {
        'source': 'analysis_assumption',
        'initial_state': {'axes': {'X': 0, 'Y': 0, 'Z': 10, 'A': 0, 'C': 0},
                          'coordinate_system': 'G54', 'units': 'mm',
                          'absolute': True, 'extrusion_absolute': False,
                          'e': 0, 'feed_mm_min': 600},
        'axis_limits': {'X': [-100, 100], 'Y': [-100, 100], 'Z': [0, 100],
                        'A': [-90, 90], 'C': [-720, 720]},
        'axis_limits_frame': 'work', 'axis_limits_coordinate_system': 'G54',
        'feed_mode': 'linear_xyz_mm_per_min', 'max_feed_mm_min': 1200,
        'rapid_uses_feed': True,
        'rotary_g92': 'preserve_physical_angle_global'}
    result = validate_trajectory('G0 Z-1', machine)
    # result['checks']['axis_limits']['status'] == 'fail', line 1.

A source tag records provenance; it does not establish calibration by itself.
See toolpath_physics._prepared_pose for the table_AC pose contract and
trajectory_collision for optional static envelope geometry. Opaque START/END
remain unknown even when numeric state before those macros is supplied.
"""
import hashlib
import math
import re

AXES = ('X', 'Y', 'Z', 'A', 'C')
LINEAR = ('X', 'Y', 'Z')
_WORD = re.compile(r'([A-Z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))')
_SAFE_M = {18, 84, 104, 105, 106, 107, 109, 110, 117, 140, 141, 190, 191, 201, 204, 205, 400}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _state(initial):
    axes = initial.get('axes', {})
    if not isinstance(axes, dict):
        axes = {}
    return {'pos': {a: axes.get(a) if _finite(axes.get(a)) else None for a in AXES},
            'shift': {a: 0. for a in AXES}, 'cs': initial.get('coordinate_system'),
            'absolute': initial.get('absolute'), 'absolute_e': initial.get('extrusion_absolute'),
            'unit': 1. if initial.get('units') == 'mm' else (25.4 if initial.get('units') == 'inch' else None),
            'e': initial.get('e'), 'feed': initial.get('feed_mm_min'), 'override': 1., 'feed_g': 94,
            'motion': initial.get('motion') if initial.get('motion') in (0, 1) else None}


def validate_trajectory(text, machine_contract=None, pose_contract=None, collision_geometry=None,
                        max_samples=200000, max_issues=200, chord_error_mm=.05):
    """Return JSON-native checks with source lines and explicit coverage gaps.

    Every input line is consumed even if the pose/collision sample budget is
    exhausted. No-contract results still expose unsupported commands, unknown
    initial state, macro locations and move counts. No default machine pose is
    invented. Collision geometry is optional and uses trajectory_collision's
    explicitly scoped static machine-frame envelope contract.
    """
    if not isinstance(text, str):
        raise ValueError('G-code text required')
    if not isinstance(max_samples, int) or isinstance(max_samples, bool) or max_samples < 1:
        raise ValueError('positive integer sample budget required')
    if not isinstance(max_issues, int) or isinstance(max_issues, bool) or max_issues < 1:
        raise ValueError('positive integer issue budget required')
    if not _finite(chord_error_mm) or chord_error_mm <= 0:
        raise ValueError('positive finite chord tolerance required')
    contract = {} if machine_contract is None else machine_contract
    if not isinstance(contract, dict):
        raise ValueError('machine contract must be an object')
    pose = pose_contract if pose_contract is not None else contract.get('pose_contract')
    geometry = collision_geometry if collision_geometry is not None else contract.get('collision_geometry')
    checks = {name: {'status': 'pass'} for name in ('contract', 'syntax', 'axis_limits', 'feed_limits', 'kinematics', 'collisions', 'dynamics')}
    issues, issue_keys, issue_counts = [], set(), {}

    def issue(check, code, message, line=None, severity='unknown', **details):
        issue_counts[code] = issue_counts.get(code, 0) + 1
        rank = {'pass': 0, 'unknown': 1, 'fail': 2}
        status = 'fail' if severity == 'error' else 'unknown'
        if rank[status] > rank[checks[check]['status']]:
            checks[check]['status'] = status
        key = (check, code, details.get('axis'), details.get('command'))
        if key not in issue_keys and len(issues) < max_issues:
            issue_keys.add(key)
            entry = {'check': check, 'code': code, 'severity': severity, 'message': message}
            if line is not None:
                entry['line'] = line
            entry.update(details)
            issues.append(entry)

    initial = contract.get('initial_state', {})
    if not isinstance(initial, dict):
        initial = {}
        issue('contract', 'invalid_initial_state', 'initial_state must be an object', severity='error')
    if not isinstance(initial.get('axes', {}), dict):
        issue('contract', 'invalid_initial_axes', 'initial_state.axes must be an object', severity='error')
    for field in ('absolute', 'extrusion_absolute'):
        if field in initial and not isinstance(initial[field], bool):
            initial = dict(initial)
            initial.pop(field)
            issue('contract', 'invalid_initial_mode', 'Initial modal flags must be booleans', severity='error')
    if initial.get('coordinate_system') not in (None, 'G54', 'G55', 'G56'):
        initial = dict(initial)
        initial['coordinate_system'] = None
        issue('contract', 'invalid_initial_frame', 'Initial work coordinate system must be G54/G55/G56', severity='error')
    state = _state(initial)
    bounds = {}
    raw_bounds = contract.get('axis_limits', {})
    if not isinstance(raw_bounds, dict):
        raw_bounds = {}
        issue('contract', 'invalid_axis_limits', 'axis_limits must be an object', severity='error')
    for axis, value in raw_bounds.items():
        if isinstance(value, dict):
            value = [value.get('min'), value.get('max')]
        if axis not in AXES or not isinstance(value, (tuple, list)) or len(value) != 2 or not all(_finite(v) for v in value) or value[0] > value[1]:
            issue('contract', 'invalid_axis_limit', 'axis limits require finite ordered [min,max]', severity='error', axis=str(axis))
        else:
            bounds[axis] = list(value)
    limit_frame = contract.get('axis_limits_frame')
    limit_cs = contract.get('axis_limits_coordinate_system')
    if not bounds:
        issue('axis_limits', 'missing_axis_limits', 'Authoritative joint position limits are not supplied')
    if limit_frame not in ('work', 'machine') or (limit_frame == 'work' and limit_cs not in ('G54', 'G55', 'G56')):
        issue('axis_limits', 'missing_axis_limit_frame', 'Declare machine limits or work limits with their coordinate system')
    max_feed = contract.get('max_feed_mm_min')
    if max_feed is not None and (not _finite(max_feed) or max_feed <= 0):
        issue('contract', 'invalid_feed_limit', 'max_feed_mm_min must be finite and positive', severity='error')
        max_feed = None
    speed_limits = contract.get('max_axis_speed_per_min', {})
    if not isinstance(speed_limits, dict) or any(a not in AXES or not _finite(v) or v <= 0 for a, v in speed_limits.items()):
        issue('contract', 'invalid_axis_speed_limit', 'Axis speed limits require positive finite values per minute', severity='error')
        speed_limits = {}
    if max_feed is None and not speed_limits:
        issue('feed_limits', 'missing_feed_limits', 'No authoritative commanded-feed or axis-speed limits supplied')
    issue('dynamics', 'dynamics_not_modelled', 'Acceleration, jerk, controller blending and torque limits require a controller dynamics model')
    if geometry is None:
        issue('collisions', 'missing_collision_geometry', 'Head, fixture and obstacle envelopes are not supplied')
    pose_point = None
    works = {}
    if pose is not None and not isinstance(pose, dict):
        issue('contract', 'invalid_pose_contract', 'pose_contract must be an object', severity='error')
        pose = None
    if pose:
        try:
            from .toolpath_physics import _prepared_pose
            pose_point = _prepared_pose(pose)
            works = pose['work_offsets']
            if pose.get('source') != 'machine_calibration':
                issue('kinematics', 'unverified_pose_source', 'Forward poses use an analysis assumption, not a calibrated machine contract')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            issue('contract', 'invalid_pose_contract', str(exc), severity='error')
            issue('kinematics', 'invalid_pose_contract', 'Forward pose contract is incomplete or invalid')
    else:
        issue('kinematics', 'missing_pose_contract', 'Explicit table_AC pivots, work transforms and controller interpolation are required')
        works = contract.get('work_offsets', {})
    prepared_works = {}
    if isinstance(works, dict):
        for cs, work in works.items():
            try:
                from .toolpath_physics import _proper
                import numpy as np
                work = np.asarray(work, float)
                if cs not in ('G54', 'G55', 'G56') or work.shape != (4, 4) or not np.isfinite(work).all() or not np.allclose(work[3], [0, 0, 0, 1]):
                    raise ValueError('finite rigid G54/G55/G56 transforms required')
                _proper(work[:3, :3])
                prepared_works[cs] = work.tolist()
            except (ValueError, TypeError):
                issue('contract', 'invalid_work_transform', 'Finite rigid G54/G55/G56 work transform required', severity='error')
    else:
        issue('contract', 'invalid_work_transforms', 'work_offsets must be an object', severity='error')
    works = prepared_works
    coverage = {'lines': 0, 'moves': 0, 'spatial_moves': 0, 'travel_moves': 0, 'extrusion_moves': 0,
                'extrusion_unknown_moves': 0, 'rotary_moves': 0, 'known_start_moves': 0,
                'axis_endpoints_checked': 0, 'feed_moves_checked': 0, 'pose_moves_checked': 0,
                'pose_samples': 0, 'collision_segments': 0, 'opaque_macros': [], 'coordinate_systems': [],
                'g92_resets': [], 'unsupported_commands': [], 'sample_budget_exhausted': False}
    evidence = {'gcode_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
                'contract_source': contract.get('source'), 'pose_source': pose.get('source') if isinstance(pose, dict) else None,
                'axis_extrema': {}, 'commanded_axis_extrema_by_work_frame': {},
                'axis_word_extrema_by_work_frame': {},
                'commanded_range_scope': 'Commanded logical state when modes/units are known; excludes G92 relabel blocks. Not physical travel.',
                'axis_word_range_scope': 'Literal numeric words in G0/G1 blocks by declared work frame, in native program units; not accumulated relative motion or physical joint ranges.',
                'part_point_bounds_mm': {}, 'pose_examples': [], 'issue_counts': issue_counts,
                'scope': 'G0/G1 commanded ranges and interpretable feeds; ideal table_AC forward poses; scoped static envelope collisions',
                'excluded': ['inverse kinematic solution search', 'acceleration/torque and controller blending',
                             'full rotating fixture and deposited-volume collision certification', 'physical print qualification']}
    collision_segments = []
    state_uncertain = False

    def uncertain_state(line, command):
        nonlocal state_uncertain
        state_uncertain = True
        state['pos'] = {a: None for a in AXES}
        state['shift'] = {a: None for a in AXES}
        state['absolute'] = state['absolute_e'] = state['unit'] = state['cs'] = state['e'] = state['feed'] = None
        state['motion'] = None
        state['feed_g'] = None
        state['override'] = None
        for check in ('axis_limits', 'feed_limits', 'kinematics', 'collisions'):
            issue(check, 'unmodelled_controller_motion', 'Controller command can change motion/state; its trajectory is not expanded', line, command=command)

    def machine_point(values, cs):
        if not all(_finite(values.get(a)) for a in LINEAR) or cs not in works:
            return None
        work = works[cs]
        result = [sum(work[i][j] * values[a] for j, a in enumerate(LINEAR)) + work[i][3] for i in range(3)]
        return result if all(math.isfinite(v) for v in result) else None

    def physical():
        return {a: state['pos'][a] + state['shift'][a] if _finite(state['pos'][a]) and _finite(state['shift'][a]) else None for a in AXES}

    def limit_values(values, cs):
        result = {a: values[a] for a in ('A', 'C')}
        if limit_frame == 'work' and cs == limit_cs:
            result.update({a: values[a] for a in LINEAR})
        elif limit_frame == 'machine':
            point = machine_point(values, cs)
            result.update({a: point[i] if point else None for i, a in enumerate(LINEAR)})
        return result

    def check_limits(values, cs, line, location):
        if not bounds:
            return
        converted = limit_values(values, cs)
        for axis in AXES:
            if axis not in bounds:
                issue('axis_limits', 'missing_axis_limit', 'No limit for this joint', line, axis=axis)
                continue
            value = converted.get(axis)
            if not _finite(value):
                issue('axis_limits', 'unknown_joint_position', 'Joint position or declared limit frame is unresolved', line, axis=axis)
                continue
            coverage['axis_endpoints_checked'] += 1
            limits = evidence['axis_extrema'].setdefault(axis, [value, value])
            limits[0], limits[1] = min(limits[0], value), max(limits[1], value)
            if value < bounds[axis][0] - 1e-9 or value > bounds[axis][1] + 1e-9:
                issue('axis_limits', 'axis_limit_exceeded', 'Commanded physical joint position exceeds its declared range', line,
                      'error', axis=axis, value=value, limits=bounds[axis], location=location)

    for number, raw in enumerate(text.splitlines(), 1):
        coverage['lines'] = number
        code = re.sub(r'\([^)]*\)', '', raw.split(';', 1)[0]).strip().upper()
        if not code or code == '%':
            continue
        if re.match(r'^(?:N\d+\s*)?M117(?:\s|$)', code):
            continue  # Display text is not a motion block or numeric parameter list.
        if re.match(r'^(START|END)(?:\s|$)', code):
            command = code.split()[0]
            coverage['opaque_macros'].append({'line': number, 'command': command})
            uncertain_state(number, command)
            continue
        if '*' in code:
            block, checksum = code.rsplit('*', 1)
            if not checksum.strip().isdigit():
                issue('syntax', 'invalid_checksum', 'Malformed checksum', number, 'error')
                uncertain_state(number, 'malformed block')
                continue
            code = block
            issue('syntax', 'checksum_not_verified', 'Transport checksum is present but controller acceptance is not verified', number)
        tokens = list(_WORD.finditer(code))
        residue = _WORD.sub('', code).strip()
        if residue or not tokens:
            issue('syntax', 'malformed_block', 'Unparsed or nonfinite G-code words', number, 'error', command=code[:100])
            uncertain_state(number, 'malformed block')
            continue
        commands, words = [], {}
        for token in tokens:
            letter, value = token.group(1), float(token.group(2))
            if not math.isfinite(value):
                issue('syntax', 'nonfinite_word', 'Nonfinite numeric word', number, 'error')
                continue
            if letter in ('G', 'M', 'T'):
                if value != int(value):
                    commands.append((letter, value))
                else:
                    commands.append((letter, int(value)))
            elif letter != 'N':
                if letter in words:
                    issue('syntax', 'duplicate_word', 'Duplicate parameter in one block', number, 'error', axis=letter)
                words[letter] = value
        motion = None
        is_reset = False
        dwell = False
        unknown_command = False
        for letter, value in commands:
            if letter == 'M':
                if value == 82:
                    state['absolute_e'] = True
                elif value == 83:
                    state['absolute_e'] = False
                elif value == 220:
                    if words.get('S', 0) > 0:
                        state['override'] = words['S'] / 100.
                    else:
                        issue('feed_limits', 'invalid_feed_override', 'M220 override must be positive', number, 'error')
                elif value not in _SAFE_M and value != 221:
                    unknown_command = True
            elif letter == 'T':
                unknown_command = True
            elif value in (0, 1):
                if motion is not None:
                    issue('syntax', 'multiple_motion_commands', 'Multiple motion words in one block', number, 'error')
                motion = value
                state['motion'] = value
            elif value in (90, 91):
                state['absolute'] = value == 90
            elif value in (20, 21):
                state['unit'] = 25.4 if value == 20 else 1.
            elif value in (93, 94):
                state['feed_g'] = value
            elif value in (54, 55, 56):
                cs = 'G' + str(value)
                if cs != state['cs']:
                    old = physical()
                    old_cs = state['cs']
                    point = machine_point(old, state['cs'])
                    state['cs'] = cs
                    if point is not None and cs in works:
                        try:
                            work = works[cs]
                            if old_cs in works and any(abs(state['shift'][a] or 0.) > 1e-12 for a in LINEAR):
                                rotated = any(abs(work[i][j] - works[old_cs][i][j]) > 1e-9 for i in range(3) for j in range(3))
                                if rotated:
                                    for axis in LINEAR:
                                        state['shift'][axis] = None
                                    for check in ('axis_limits', 'kinematics', 'collisions'):
                                        issue(check, 'reset_work_rotation_unresolved', 'G92 XYZ offsets across rotated work frames require explicit controller semantics', number)
                            q = [sum(work[j][i] * (point[j] - work[j][3]) for j in range(3)) for i in range(3)]
                            for i, axis in enumerate(LINEAR):
                                state['pos'][axis] = float(q[i]) - state['shift'][axis] if _finite(state['shift'][axis]) else None
                        except (ValueError, TypeError, IndexError):
                            state['pos'].update({a: None for a in LINEAR})
                    else:
                        state['pos'].update({a: None for a in LINEAR})
                if cs not in coverage['coordinate_systems']:
                    coverage['coordinate_systems'].append(cs)
            elif value == 92:
                is_reset = True
            elif value == 4:
                dwell = True
            else:
                unknown_command = True
        if unknown_command:
            label = ' '.join(str(a) + str(b) for a, b in commands)
            coverage['unsupported_commands'].append({'line': number, 'command': label})
            issue('syntax', 'unsupported_command', 'Controller command is not modelled', number, command=label)
            uncertain_state(number, label)
            continue
        if is_reset:
            if motion is not None:
                issue('syntax', 'reset_with_motion', 'G92 and motion in the same block are ambiguous', number, 'error')
                uncertain_state(number, 'G92 with motion')
                continue
            old = physical()
            for axis in AXES:
                if axis in words:
                    coverage['g92_resets'].append({'line': number, 'axis': axis, 'value': words[axis]})
                    scale = state['unit'] if axis in LINEAR else 1.
                    value = words[axis] * scale if scale is not None else None
                    allowed = axis in LINEAR or contract.get('rotary_g92', (pose or {}).get('rotary_g92')) == 'preserve_physical_angle_global'
                    state['shift'][axis] = old[axis] - value if allowed and _finite(old[axis]) and _finite(value) else None
                    state['pos'][axis] = value
                    if state['shift'][axis] is None:
                        for check in ('axis_limits', 'kinematics', 'collisions'):
                            issue(check, 'unresolved_spatial_reset', 'G92 physical position cannot be reconstructed without prior pose and rotary reset semantics', number, axis=axis)
            if 'E' in words:
                state['e'] = words['E'] * state['unit'] if state['unit'] else None
            continue
        if 'F' in words:
            state['feed'] = words['F'] * state['unit'] if state['unit'] else None
            if words['F'] <= 0:
                issue('feed_limits', 'nonpositive_feed', 'Commanded feed must be positive', number, 'error')
        if dwell:
            continue
        if motion is None and (any(a in words for a in AXES) or 'E' in words):
            motion = state['motion']
        if motion is None:
            if any(a in words for a in AXES) or 'E' in words:
                issue('syntax', 'unsupported_modal_motion', 'Coordinate-only modal motion blocks require explicit G0/G1 in this validator', number)
                uncertain_state(number, 'implicit modal motion')
            continue
        unexpected = sorted(set(words) - set(AXES) - {'E', 'F'})
        if unexpected:
            coverage['moves'] += 1
            coverage['spatial_moves'] += 1
            issue('syntax', 'unsupported_motion_word', 'Unmodelled motion words: ' + ', '.join(unexpected), number)
            uncertain_state(number, 'G' + str(motion) + ' ' + ' '.join(unexpected))
            continue
        coverage['moves'] += 1
        start = physical()
        spatial = any(a in words for a in AXES)
        rotary = any(a in words for a in ('A', 'C'))
        if spatial:
            coverage['spatial_moves'] += 1
        if rotary:
            coverage['rotary_moves'] += 1
        for axis in AXES:
            if axis in words:
                scale = state['unit'] if axis in LINEAR else 1.
                value = words[axis] * scale if scale else None
                if state['absolute'] is True:
                    state['pos'][axis] = value
                elif state['absolute'] is False and _finite(state['pos'][axis]) and _finite(value):
                    state['pos'][axis] += value
                else:
                    state['pos'][axis] = None
        end = physical()
        work_frame = state['cs'] or 'unknown'
        for axis in AXES:
            if _finite(state['pos'][axis]):
                extrema = evidence['commanded_axis_extrema_by_work_frame'].setdefault(work_frame, {}).setdefault(axis, [state['pos'][axis], state['pos'][axis]])
                extrema[0], extrema[1] = min(extrema[0], state['pos'][axis]), max(extrema[1], state['pos'][axis])
            if axis in words:
                extrema = evidence['axis_word_extrema_by_work_frame'].setdefault(work_frame, {}).setdefault(axis, [words[axis], words[axis]])
                extrema[0], extrema[1] = min(extrema[0], words[axis]), max(extrema[1], words[axis])
        de = 0.
        if 'E' in words:
            value = words['E'] * state['unit'] if state['unit'] else None
            if state['absolute_e'] is True:
                de = value - state['e'] if _finite(value) and _finite(state['e']) else None
                state['e'] = value
            elif state['absolute_e'] is False:
                de = value
                state['e'] = state['e'] + value if _finite(state['e']) and _finite(value) else None
            else:
                de = None
        coverage['extrusion_unknown_moves' if de is None else ('extrusion_moves' if de > 0 else 'travel_moves')] += 1
        if not spatial:
            continue
        if all(_finite(start[a]) for a in AXES):
            coverage['known_start_moves'] += 1
        else:
            issue('kinematics', 'unknown_initial_or_modal_state', 'Move start is unresolved; a zero origin is not assumed', number)
            issue('collisions', 'unknown_move_start', 'Travel from an unresolved starting pose is not collision checked', number)
        check_limits(start, state['cs'], number, 'start')
        check_limits(end, state['cs'], number, 'end')
        delta = {a: end[a] - start[a] if _finite(start[a]) and _finite(end[a]) else None for a in AXES}
        delta = {a: value if _finite(value) else None for a, value in delta.items()}
        feed = state['feed'] * state['override'] if _finite(state['feed']) and _finite(state['override']) else None
        interpretable = (state['feed_g'] == 94 and contract.get('feed_mode') == 'linear_xyz_mm_per_min'
                         and (motion == 1 or contract.get('rapid_uses_feed') is True))
        linear_length = math.hypot(math.hypot(delta['X'], delta['Y']), delta['Z']) if all(_finite(delta[a]) for a in LINEAR) else None
        if not _finite(linear_length):
            linear_length = None
        if (interpretable and _finite(feed) and max_feed is not None and feed > max_feed + 1e-9
                and (any(a in words for a in LINEAR) or (linear_length is not None and linear_length > 1e-12))):
            issue('feed_limits', 'feed_limit_exceeded', 'Commanded linear-path feed exceeds the supplied limit', number,
                  'error', value=feed, limit=max_feed)
        if not interpretable or not _finite(feed) or feed <= 0 or linear_length is None or linear_length <= 1e-12:
            issue('feed_limits', 'unresolved_feed_semantics', 'Move feed/rapid timing, initial state or pure rotary feed semantics are unresolved', number)
        elif max_feed is not None or speed_limits:
            coverage['feed_moves_checked'] += 1
            duration_min = linear_length / feed
            for axis, speed in speed_limits.items():
                if not _finite(delta[axis]) or duration_min <= 0 or not math.isfinite(abs(delta[axis]) / duration_min):
                    issue('feed_limits', 'unknown_axis_speed', 'Axis start/end is unresolved', number, axis=axis)
                elif abs(delta[axis]) / duration_min > speed + 1e-9:
                    issue('feed_limits', 'axis_speed_exceeded', 'Joint speed under declared linear-XYZ feed timing exceeds its limit', number,
                          'error', axis=axis, value=abs(delta[axis]) / duration_min, limit=speed)
        start_machine, end_machine = machine_point(start, state['cs']), machine_point(end, state['cs'])
        if geometry is not None:
            if start_machine is None or end_machine is None:
                issue('collisions', 'missing_machine_segment', 'A move cannot be transformed into the geometry machine frame', number)
            elif len(collision_segments) >= max_samples:
                coverage['sample_budget_exhausted'] = True
                issue('collisions', 'collision_budget_exhausted', 'Remaining moves exceed the explicit collision segment budget', number)
            else:
                collision_segments.append({'start': start_machine, 'end': end_machine, 'line': number})
        if pose_point is None:
            continue
        if not all(_finite(start[a]) and _finite(end[a]) for a in AXES) or state['cs'] not in works:
            issue('kinematics', 'unresolved_pose', 'Complete joint endpoints and active work transform are required', number)
            continue
        try:
            omega = sum(abs(math.radians(delta[a])) for a in ('A', 'C'))
            radius = max(float(sum(v*v for v in pose_point(q, state['cs'])[0])) ** .5 for q in (start, end))
            velocity = linear_length or 0.
            curvature = omega ** 2 * radius + 2 * omega * velocity
            count = max(1, int(math.ceil(omega / math.radians(5.))), int(math.ceil(math.sqrt(curvature / (8 * chord_error_mm)))))
            if coverage['pose_samples'] + count + 1 > max_samples:
                coverage['sample_budget_exhausted'] = True
                issue('kinematics', 'pose_budget_exhausted', 'Remaining pose subdivisions exceed explicit analysis budget', number)
                continue
            for index in range(count + 1):
                u = index / float(count)
                q = {a: start[a] + u * delta[a] for a in AXES}
                point, normal = pose_point(q, state['cs'])
                for axis, value in zip(LINEAR, point):
                    limits = evidence['part_point_bounds_mm'].setdefault(axis, [float(value), float(value)])
                    limits[0], limits[1] = min(limits[0], float(value)), max(limits[1], float(value))
                if len(evidence['pose_examples']) < 5:
                    evidence['pose_examples'].append({'line': number, 'fraction': u, 'part_point_mm': point.tolist(), 'nozzle_direction_part': normal.tolist()})
            coverage['pose_samples'] += count + 1
            coverage['pose_moves_checked'] += 1
        except (ValueError, OverflowError, KeyError) as exc:
            issue('kinematics', 'pose_evaluation_failed', str(exc), number, 'error')
    coverage['collision_segments'] = len(collision_segments)
    if not coverage['spatial_moves']:
        for check in ('axis_limits', 'feed_limits', 'kinematics', 'collisions'):
            issue(check, 'no_spatial_moves', 'No explicit G0/G1 spatial trajectory found')
    if geometry is not None:
        from .trajectory_collision import check_trajectory_collisions
        collision_result = check_trajectory_collisions(collision_segments, geometry, max_issues=max_issues)
        checks['collisions']['geometry_result'] = collision_result
        if collision_result['status'] == 'fail':
            checks['collisions']['status'] = 'fail'
            for entry in collision_result.get('issues', []):
                details = {k: v for k, v in entry.items() if k not in ('code', 'line', 'message', 'severity', 'check')}
                issue('collisions', entry.get('code', 'collision_detected'),
                      entry.get('message', 'Supplied tool envelope violates clearance to a static obstacle'),
                      entry.get('line'), 'error', **details)
        elif collision_result['status'] != 'pass':
            issue('collisions', 'incomplete_collision_scope', 'Static-envelope collision result is incomplete; inspect geometry_result')
        if state_uncertain or len(collision_segments) < coverage['spatial_moves']:
            issue('collisions', 'incomplete_trajectory_coverage', 'Unknown/opaque moves are outside checked static geometry coverage')
    checks['axis_limits']['scope'] = 'supplied physical joint ranges at every G0/G1 endpoint, including travel and G92 offsets'
    checks['feed_limits']['scope'] = 'commanded feed and declared joint speeds under explicit linear-XYZ timing; no acceleration guarantees'
    checks['kinematics']['scope'] = 'ideal table_AC forward pose sampling, unwrapped joints; no IK/dynamics certification'
    checks['collisions']['scope'] = 'optional static machine-frame tool-envelope sweeps; incomplete machine/printed-part clearance remains unverified'
    checks['dynamics']['scope'] = 'not modelled'
    return {'schema_version': 1, 'status': 'fail' if any(v['status'] == 'fail' for v in checks.values()) else 'unknown',
            'checks': checks, 'issues': issues, 'coverage': coverage, 'evidence': evidence,
            'physical_print_approval': False, 'full_clearance_verified': False}
