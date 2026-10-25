"""Independent continuous path/fixture checks and honest coverage states."""
import copy
import math

from ste_planner.trajectory_collision import check_trajectory_collisions


def geometry(obstacle, radius=1., offset=None, clearance=0.):
    return {"frame": "machine", "clearance_mm": clearance,
            "tool_spheres": [{"name": "head", "center_mm": offset or [0, 0, 0], "radius_mm": radius}],
            "obstacles": [obstacle]}


def segment(start, end, line=7):
    return {"start": start, "end": end, "line": line}


def sphere(center=None, radius=1.):
    return {"type": "sphere", "name": "fixture", "center_mm": center or [0, 0, 0], "radius_mm": radius}


def test_missing_geometry_does_not_invent_dimensions_or_approve():
    for value in (None, {}, {"frame": "part"}, {"frame": "machine"}):
        result = check_trajectory_collisions([], value)
        assert result["status"] == "unknown"
        assert result["missing"]
        assert result["full_clearance_verified"] is False
        assert result["physical_print_approval"] is False


def test_sphere_sweep_finds_interior_collision_with_safe_endpoints():
    result = check_trajectory_collisions([segment([-5, 0, 0], [5, 0, 0])], geometry(sphere()))
    assert result["status"] == "fail"
    assert result["minimum_clearance_mm"] == -2
    assert result["issues"][0]["line"] == 7
    assert result["issues"][0]["obstacle"] == "fixture"


def test_clearance_margin_and_tangency_are_checked():
    path = [segment([-5, 2.1, 0], [5, 2.1, 0])]
    assert check_trajectory_collisions(path, geometry(sphere()))["status"] == "pass"
    result = check_trajectory_collisions(path, geometry(sphere(), clearance=.2))
    assert result["status"] == "fail"
    assert result["issues"][0]["code"] == "insufficient_clearance"
    assert abs(result["minimum_clearance_mm"] - .1) < 1e-10
    tangent = [segment([-5, 2, 0], [5, 2, 0])]
    assert check_trajectory_collisions(tangent, geometry(sphere()))["status"] == "fail"


def test_stationary_rotation_segment_still_checks_static_head_position():
    result = check_trajectory_collisions([segment([0, 0, 0], [0, 0, 0])], geometry(sphere()))
    assert result["status"] == "fail" and result["checked_segments"] == 1


def test_capsule_crossing_and_parallel_paths():
    capsule = {"type": "capsule", "start_mm": [0, -2, 0], "end_mm": [0, 2, 0], "radius_mm": .5}
    assert check_trajectory_collisions([segment([-4, 0, 0], [4, 0, 0])], geometry(capsule))["status"] == "fail"
    result = check_trajectory_collisions([segment([3, -5, 0], [3, 5, 0])], geometry(capsule))
    assert result["status"] == "pass" and result["minimum_clearance_mm"] == 1.5
    capsule["end_mm"] = capsule["start_mm"]
    result = check_trajectory_collisions([segment([-4, -2, 0], [4, -2, 0])], geometry(capsule))
    assert result["status"] == "fail"


def test_aabb_sweep_and_rounded_corner_distance():
    box = {"type": "aabb", "min_mm": [-1, -1, -1], "max_mm": [1, 1, 1]}
    hit = check_trajectory_collisions([segment([-5, 0, 0], [5, 0, 0])], geometry(box))
    assert hit["status"] == "fail"
    # Inflating a box by the sphere radius would incorrectly reject this path.
    miss = check_trajectory_collisions([segment([-5, 1.8, 1.8], [5, 1.8, 1.8])], geometry(box))
    assert miss["status"] == "pass"
    assert abs(miss["minimum_clearance_mm"] - (math.sqrt(1.28) - 1.)) < 1e-10


def test_aabb_piecewise_minimum_is_inside_interval_and_not_endpoint():
    box = {"type": "aabb", "min_mm": [0, 0, 0], "max_mm": [1, 1, 1]}
    result = check_trajectory_collisions([segment([-2, 1, 0], [1, 4, 0])], geometry(box, radius=.2))
    # Closest point is [-1, 2, 0], distance sqrt(2), not an endpoint.
    assert abs(result["minimum_clearance_mm"] - (math.sqrt(2) - .2)) < 1e-10


def test_head_offset_is_relative_to_machine_tcp():
    path = [segment([-5, 0, 0], [5, 0, 0])]
    assert check_trajectory_collisions(path, geometry(sphere()))["status"] == "fail"
    result = check_trajectory_collisions(path, geometry(sphere(), offset=[0, 0, 10]))
    assert result["status"] == "pass" and result["minimum_clearance_mm"] == 8
    assert result["coverage"]["scope"] == "partial_machine"
    assert result["full_machine_collision_verified"] is False


def test_unknown_moving_fixture_does_not_silently_pass_and_fail_still_wins():
    spec = geometry(sphere([100, 0, 0]))
    spec["obstacles"].append({"type": "mesh", "name": "mandrel", "frame": "table"})
    result = check_trajectory_collisions([segment([-2, 0, 0], [2, 0, 0])], spec)
    assert result["status"] == "unknown"
    assert result["coverage"]["provided_geometry_complete"] is False
    spec["obstacles"][0] = sphere()
    assert check_trajectory_collisions([segment([-2, 0, 0], [2, 0, 0])], spec)["status"] == "fail"


def test_invalid_segments_and_nan_cannot_create_a_clearance_pass():
    path = [segment([0, 3, 0], [1, 3, 0]), segment([0, 0, float("nan")], [1, 0, 0])]
    result = check_trajectory_collisions(path, geometry(sphere()))
    assert result["status"] == "unknown" and result["checked_segments"] == 1
    assert result["coverage"]["all_input_segments_checked"] is False
    for bad in (-1, float("inf"), True):
        spec = geometry(sphere(), radius=bad)
        assert check_trajectory_collisions(path, spec)["status"] == "unknown"


def test_no_implicit_nozzle_contact_exemption():
    spec = geometry(sphere())
    spec["tool_spheres"][0]["name"] = "nozzle_tip"
    path = [dict(segment([-3, 0, 0], [3, 0, 0]), deposition=True)]
    assert check_trajectory_collisions(path, spec)["status"] == "fail"
    spec["contact_exemptions"] = [{"tool": "nozzle_tip", "obstacle": "fixture"}]
    result = check_trajectory_collisions(path, spec)
    assert result["status"] == "fail" and result["missing"]


def test_issue_budget_does_not_stop_later_segments_and_generator_works():
    path = (segment([-3, 0, 0], [3, 0, 0], line) for line in range(25))
    result = check_trajectory_collisions(path, geometry(sphere()), max_issues=2)
    assert result["status"] == "fail"
    assert result["checked_segments"] == 25 and result["collision_count"] == 25
    assert len(result["issues"]) == 2 and result["issues_truncated"] is True


def test_empty_trajectory_is_unknown_and_input_is_not_modified():
    spec = geometry(sphere())
    original = copy.deepcopy(spec)
    result = check_trajectory_collisions([], spec)
    assert result["status"] == "unknown"
    assert spec == original


def test_degenerate_box_and_capsule_still_detect_contacts():
    fixtures = [
        {"type": "aabb", "min_mm": [0, 0, 0], "max_mm": [0, 0, 0]},
        {"type": "capsule", "start_mm": [0, 0, 0], "end_mm": [0, 0, 0], "radius_mm": .1},
    ]
    for fixture in fixtures:
        assert check_trajectory_collisions([segment([-2, 0, 0], [2, 0, 0])], geometry(fixture))["status"] == "fail"


def test_submicron_move_still_checks_the_entire_segment():
    # A short movement must not be replaced by its safe starting endpoint.
    result = check_trajectory_collisions(
        [segment([2.00000005, 0, 0], [1.99999995, 0, 0])], geometry(sphere()))
    assert result["status"] == "fail"


def test_almost_parallel_capsule_crossing_cannot_be_treated_as_parallel():
    fixture = {"type": "capsule", "start_mm": [-1000000, -.1, 0],
               "end_mm": [1000000, .1, 0], "radius_mm": .01}
    result = check_trajectory_collisions(
        [segment([-1000000, 0, 0], [1000000, 0, 0])], geometry(fixture, radius=.01))
    assert result["status"] == "fail"
    assert abs(result["minimum_clearance_mm"] + .02) < 1e-10
