"""Continuous clearance checks for explicitly supplied, static machine geometry.

This deliberately small checker does not invent a head or mandrel diameter. A
tool envelope is a union of spheres whose centres are fixed offsets from the
machine TCP. Fixtures are static spheres, capsules or axis-aligned boxes in the
same machine coordinate frame. Linear TCP segments are checked continuously,
including their interiors, rather than by checking only G-code endpoints.

Rotating table/mandrel geometry, the growing printed part, head orientation
changes and controller macros are outside this model. A scoped ``pass`` never
means that the complete machine is collision-free or physically approved.

Geometry contract (all lengths in mm)::

    {"frame": "machine", "clearance_mm": 0.2,
     "tool_spheres": [{"name": "head", "center_mm": [0, 0, 20],
                       "radius_mm": 10}],
     "obstacles": [
         {"name": "clamp", "type": "aabb", "min_mm": [5, -2, 0],
          "max_mm": [8, 2, 15]},
         {"name": "post", "type": "capsule", "start_mm": [0, 0, 0],
          "end_mm": [0, 0, 10], "radius_mm": 2},
         {"name": "ball", "type": "sphere", "center_mm": [4, 0, 0],
          "radius_mm": 1}]}

Segments have ``start``, ``end`` (machine TCP XYZ) and optional ``line``. There
are no implicit contact exemptions: nozzle contact also counts as interference
if the nozzle is included in a supplied envelope intersecting a fixture.
"""
import math


_EPS = 1e-12
_LIMITATIONS = [
    "Only supplied fixed-offset tool spheres and static machine-frame fixtures are checked.",
    "Rotating table/mandrel, evolving printed material and controller macros are not checked.",
    "A scoped geometry pass is not physical print approval.",
]


def _vector(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(label + " must contain three finite coordinates")
    result = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(label + " contains a boolean coordinate")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(label + " must contain finite coordinates")
        result.append(number)
    return result


def _number(value, label, positive=False):
    if isinstance(value, bool):
        raise ValueError(label + " cannot be boolean")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise ValueError(label + (" must be positive and finite" if positive else " must be nonnegative and finite"))
    return number


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _dot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def _clamp(value):
    return max(0., min(1., value))


def _point_segment_distance2(point, start, end):
    direction, delta = _sub(end, start), _sub(point, start)
    length2 = _dot(direction, direction)
    t = _clamp(_dot(delta, direction) / length2) if length2 > 0. else 0.
    difference = [delta[i] - t * direction[i] for i in range(3)]
    return _dot(difference, difference)


def _segment_segment_distance2(p0, p1, q0, q1):
    """Minimum distance between two closed segments, including degeneracies."""
    d1, d2, w = _sub(p1, p0), _sub(q1, q0), _sub(q0, p0)

    def cross(a, b):
        return [a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0]]

    # The constrained minimum is either a line-line stationary point inside
    # both segments, or an endpoint-to-segment minimum. Cross products avoid
    # subtracting nearly equal a*e and b*b for long almost-parallel capsules.
    best = min(_point_segment_distance2(p0, q0, q1),
               _point_segment_distance2(p1, q0, q1),
               _point_segment_distance2(q0, p0, p1),
               _point_segment_distance2(q1, p0, p1))
    normal = cross(d1, d2)
    denominator = _dot(normal, normal)
    if denominator > 0.:
        s = _dot(cross(w, d2), normal) / denominator
        t = _dot(cross(w, d1), normal) / denominator
        if 0. <= s <= 1. and 0. <= t <= 1.:
            delta = [s * d1[i] - t * d2[i] - w[i] for i in range(3)]
            best = min(best, _dot(delta, delta))
    return best


def _segment_aabb_distance2(start, end, low, high):
    """Exact piecewise-quadratic distance, avoiding inflated-box corner errors."""
    direction = _sub(end, start)
    breaks = [0., 1.]
    for i in range(3):
        if abs(direction[i]) > _EPS:
            for bound in (low[i], high[i]):
                t = (bound - start[i]) / direction[i]
                if 0 < t < 1:
                    breaks.append(t)
    breaks = sorted(set(breaks))

    def distance2(t):
        total = 0.
        for i in range(3):
            p = start[i] + t * direction[i]
            delta = low[i] - p if p < low[i] else p - high[i] if p > high[i] else 0.
            total += delta * delta
        return total

    best = min(distance2(t) for t in breaks)
    for left, right in zip(breaks, breaks[1:]):
        mid, numerator, denominator = (left + right) / 2., 0., 0.
        for i in range(3):
            p = start[i] + mid * direction[i]
            bound = low[i] if p < low[i] else high[i] if p > high[i] else None
            if bound is not None:
                numerator += direction[i] * (start[i] - bound)
                denominator += direction[i] ** 2
        if denominator > _EPS:
            t = max(left, min(right, -numerator / denominator))
            best = min(best, distance2(t))
    return best


def _tool_sphere(item, index):
    if not isinstance(item, dict):
        raise ValueError("tool sphere must be an object")
    if item.get("frame", "machine") != "machine":
        raise ValueError("tool sphere must use fixed machine-frame offsets")
    if item.get("contact_exemptions") or item.get("exempt"):
        raise ValueError("contact exemptions are unsupported; no geometry may be silently skipped")
    return {"name": str(item.get("name", "tool_sphere_" + str(index))),
            "center": _vector(item.get("center_mm", item.get("center")), "tool centre offset"),
            "radius": _number(item.get("radius_mm"), "tool radius", positive=True)}


def _obstacle(item, index):
    if not isinstance(item, dict):
        raise ValueError("fixture must be an object")
    if item.get("frame", "machine") != "machine" or item.get("moving", False):
        raise ValueError("only static machine-frame fixtures are supported")
    result = {"name": str(item.get("name", "fixture_" + str(index))), "type": item.get("type")}
    if result["type"] == "sphere":
        result["center"] = _vector(item.get("center_mm", item.get("center")), "fixture centre")
        result["radius"] = _number(item.get("radius_mm"), "fixture radius", positive=True)
    elif result["type"] == "capsule":
        result["start"] = _vector(item.get("start_mm", item.get("start")), "capsule start")
        result["end"] = _vector(item.get("end_mm", item.get("end")), "capsule end")
        result["radius"] = _number(item.get("radius_mm"), "fixture radius", positive=True)
    elif result["type"] == "aabb":
        result["low"] = _vector(item.get("min_mm", item.get("min")), "box minimum")
        result["high"] = _vector(item.get("max_mm", item.get("max")), "box maximum")
        if any(result["low"][i] > result["high"][i] for i in range(3)):
            raise ValueError("box minimum exceeds maximum")
    else:
        raise ValueError("unsupported fixture type: " + str(result["type"]))
    return result


def _clearance(start, end, tool, obstacle):
    p0 = [start[i] + tool["center"][i] for i in range(3)]
    p1 = [end[i] + tool["center"][i] for i in range(3)]
    radius = tool["radius"]
    if obstacle["type"] == "sphere":
        distance2 = _point_segment_distance2(obstacle["center"], p0, p1)
        radius += obstacle["radius"]
    elif obstacle["type"] == "capsule":
        distance2 = _segment_segment_distance2(p0, p1, obstacle["start"], obstacle["end"])
        radius += obstacle["radius"]
    else:
        distance2 = _segment_aabb_distance2(p0, p1, obstacle["low"], obstacle["high"])
    if not math.isfinite(distance2):
        raise ValueError("nonfinite distance arithmetic")
    return math.sqrt(max(0., distance2)) - radius


def check_trajectory_collisions(segments, geometry=None, max_issues=100):
    """Check an iterable of complete linear machine-TCP segments.

    ``status`` describes only the supplied static geometry: ``pass``, ``fail``
    or ``unknown``. Any witnessed collision wins over incomplete input. Issues
    are bounded, but every supplied segment is checked and counted. Unknown
    frames, unsupported fixtures or invalid segments prevent a scoped pass.
    The caller must also invalidate coverage for any unparsed G-code motion.
    """
    result = {
        "status": "unknown", "scope": "explicit_static_machine_geometry",
        "full_clearance_verified": False, "full_machine_collision_verified": False,
        "physical_print_approval": False, "issues": [], "missing": [],
        "limitations": list(_LIMITATIONS), "minimum_clearance_mm": None,
        "required_clearance_mm": None, "checked_segments": 0, "input_segments": 0,
        "checked_pairs": 0, "collision_count": 0, "issues_truncated": False,
        "coverage": {"provided_geometry_complete": False,
                     "all_input_segments_checked": False, "scope": "partial_machine"},
    }
    try:
        if isinstance(max_issues, bool) or int(max_issues) != max_issues or max_issues < 1:
            raise ValueError("max_issues must be a positive integer")
        max_issues = int(max_issues)
    except (TypeError, ValueError, OverflowError):
        result["missing"].append("max_issues must be a positive integer")
        return result
    if not isinstance(geometry, dict):
        result["missing"].append("explicit collision_geometry with tool_spheres and static obstacles required")
        return result
    if geometry.get("frame") != "machine":
        result["missing"].append("collision_geometry.frame must explicitly be machine")
        return result
    try:
        clearance = _number(geometry.get("clearance_mm", 0.), "required clearance")
        result["required_clearance_mm"] = clearance
    except (ValueError, TypeError, OverflowError) as error:
        result["missing"].append(str(error))
        return result
    if geometry.get("contact_exemptions"):
        result["missing"].append("contact exemptions are unsupported; geometry was checked without exemptions")
    tools, obstacles = [], []
    for key, prepare, target in (("tool_spheres", _tool_sphere, tools), ("obstacles", _obstacle, obstacles)):
        items = geometry.get(key)
        if not isinstance(items, (list, tuple)) or not items:
            result["missing"].append("nonempty explicit " + key + " required")
            continue
        for index, item in enumerate(items):
            try:
                target.append(prepare(item, index))
            except (ValueError, TypeError, OverflowError) as error:
                result["missing"].append(key + "[" + str(index) + "]: " + str(error))
    result["coverage"]["provided_geometry_complete"] = not result["missing"]
    if not tools or not obstacles:
        return result
    try:
        iterator = iter(segments)
    except TypeError:
        result["missing"].append("machine-frame trajectory segments required")
        return result
    bad_segments = 0
    for segment in iterator:
        result["input_segments"] += 1
        try:
            if not isinstance(segment, dict):
                raise ValueError("segment must be an object")
            if segment.get("frame", "machine") != "machine":
                raise ValueError("segment frame must be machine")
            start = _vector(segment.get("start"), "segment start")
            end = _vector(segment.get("end"), "segment end")
        except (ValueError, TypeError, OverflowError) as error:
            bad_segments += 1
            if bad_segments <= max_issues:
                result["missing"].append("segment " + str(result["input_segments"]) + ": " + str(error))
            continue
        result["checked_segments"] += 1
        for tool in tools:
            for obstacle in obstacles:
                try:
                    actual = _clearance(start, end, tool, obstacle)
                    if not math.isfinite(actual):
                        raise ValueError("nonfinite clearance arithmetic")
                except (ValueError, OverflowError, ZeroDivisionError):
                    result["missing"].append("clearance arithmetic could not resolve supplied coordinates")
                    continue
                result["checked_pairs"] += 1
                previous = result["minimum_clearance_mm"]
                result["minimum_clearance_mm"] = actual if previous is None else min(previous, actual)
                if actual <= clearance + 1e-9:
                    result["collision_count"] += 1
                    if len(result["issues"]) < max_issues:
                        result["issues"].append({
                            "code": "collision" if actual <= 1e-9 else "insufficient_clearance",
                            "line": segment.get("line"), "tool": tool["name"],
                            "obstacle": obstacle["name"], "clearance_mm": actual,
                            "required_clearance_mm": clearance,
                        })
    result["coverage"]["all_input_segments_checked"] = bad_segments == 0 and result["input_segments"] > 0
    result["issues_truncated"] = result["collision_count"] > len(result["issues"])
    if not result["input_segments"]:
        result["missing"].append("at least one complete machine-frame trajectory segment required")
    if result["collision_count"]:
        result["status"] = "fail"
    elif not result["missing"]:
        result["status"] = "pass"
    return result
