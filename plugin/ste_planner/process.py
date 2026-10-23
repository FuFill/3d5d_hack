# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Process quantities: walls, skins, flow, speed, motion, layers, tolerances, compensation.

Specification sections 7 and 9, document 03 section 5. Inputs are physical
(mm, mm/s, mm^3/s, fractions). Percent values for the slicer are produced by
the settings compiler only.
"""

import math

from .result import (computed, unknown, invalid, is_number, MODEL_GEOMETRIC, MODEL_ANALYTIC,
                     MODEL_EXPERIMENTAL, MODEL_POLICY, FAIL, PASS)

_CEIL_EPS = 1e-9  # protects ceil() against binary noise such as 2.0000000001


def _ceil(x):
    return int(math.ceil(x - _CEIL_EPS))


def wall_count(t_required, w_outer, w_inner=None, allow_zero=False):
    """N = 1 + ceil(max(0, t - w0) / wi); t_shell = w0 + (N-1) wi.

    Discretises an already justified thickness; it does not prove strength.
    """
    w_inner = w_outer if w_inner is None else w_inner
    if not all(is_number(x) for x in (t_required, w_outer, w_inner)):
        return unknown("wall_count", "missing inputs", ["t_required", "w0", "wi"])
    if w_outer <= 0 or w_inner <= 0 or t_required < 0:
        return invalid("wall_count", "widths must be > 0 and thickness >= 0")
    if t_required == 0 and allow_zero:
        n = 0
        actual = 0.0
    else:
        n = 1 + _ceil(max(0.0, t_required - w_outer) / w_inner)
        actual = w_outer + (n - 1) * w_inner
    return computed({"count": n, "thickness": actual}, "count|mm", "wall_count", MODEL_GEOMETRIC,
                    inputs={"t_required": t_required, "w0": w_outer, "wi": w_inner},
                    note="Effective thickness must be checked in the slicer (overlap, gap fill)")


def skin_layers(t_required, layer_height=None, layer_heights=None):
    """N = ceil(t / h) for uniform layers; for variable layers sum actual heights."""
    if not is_number(t_required) or t_required < 0:
        return invalid("skin_layers", "thickness must be >= 0")
    if layer_heights:
        total, n = 0.0, 0
        for h in layer_heights:
            if total >= t_required - _CEIL_EPS:
                break
            total += h
            n += 1
        if total < t_required - _CEIL_EPS:
            return unknown("skin_layers", "not enough layers to reach the thickness")
        return computed({"count": n, "thickness": total}, "count|mm", "skin_layers", MODEL_GEOMETRIC)
    if not is_number(layer_height) or layer_height <= 0:
        return invalid("skin_layers", "layer height must be > 0")
    n = _ceil(t_required / layer_height)
    return computed({"count": n, "thickness": n * layer_height}, "count|mm", "skin_layers", MODEL_GEOMETRIC,
                    inputs={"t_required": t_required, "h": layer_height})


def bead_area(width, height, model="rectangle"):
    """Track cross-section: rectangle w*h or stadium h(w-h) + pi h^2/4 (w >= h)."""
    if not (is_number(width) and is_number(height)) or width <= 0 or height <= 0:
        return invalid("bead_area", "w and h must be > 0")
    if model == "rectangle":
        a = width * height
    elif model == "stadium":
        if width < height:
            return invalid("bead_area", "stadium model requires w >= h")
        a = height * (width - height) + math.pi * height ** 2 / 4.0
    else:
        return invalid("bead_area", "unknown model %r" % (model,))
    return computed(a, "mm^2", "bead_area", MODEL_GEOMETRIC, inputs={"w": width, "h": height, "model": model})


def filament_area(diameter):
    return math.pi * diameter ** 2 / 4.0


def volumetric_flow(flow_factor, area, speed):
    """Q = k A v."""
    if not all(is_number(x) and x > 0 for x in (flow_factor, area, speed)):
        return invalid("volumetric_flow", "k, A, v must be > 0")
    return computed(flow_factor * area * speed, "mm3/s", "volumetric_flow", MODEL_GEOMETRIC,
                    inputs={"k": flow_factor, "A": area, "v": speed})


def filament_feed_rate(q, filament_diameter):
    """v_feed = Q / A_filament."""
    if not (is_number(q) and is_number(filament_diameter)) or filament_diameter <= 0 or q < 0:
        return invalid("filament_feed_rate", "Q >= 0 and d > 0 required")
    return computed(q / filament_area(filament_diameter), "mm/s", "filament_feed_rate", MODEL_GEOMETRIC)


def speed_limit_from_flow(q_limit, flow_factor, area):
    """v <= Q_limit / (k A). Q_limit must be a calibrated lower bound."""
    if not is_number(q_limit):
        return unknown("speed_limit_from_flow", "Q_limit requires calibration of hotend/nozzle/material",
                       ["Q_limit"], status="needs_calibration")
    if not all(is_number(x) and x > 0 for x in (q_limit, flow_factor, area)):
        return invalid("speed_limit_from_flow", "Q_limit, k, A must be > 0")
    return computed(q_limit / (flow_factor * area), "mm/s", "speed_limit_from_flow", MODEL_ANALYTIC,
                    inputs={"Q_limit": q_limit, "k": flow_factor, "A": area})


def role_speed(limits):
    """v_role = min of known limits; reports which limits were unknown.

    limits: {"profile": v, "quality": v, "flow": v, "motion": v, "cooling": v} (None = unknown).
    """
    known = dict((k, float(v)) for k, v in limits.items() if is_number(v) and v > 0)
    if not known:
        return unknown("role_speed", "no speed limit known")
    governing = min(sorted(known), key=lambda k: known[k])
    missing = sorted(k for k, v in limits.items() if not is_number(v))
    r = computed(known[governing], "mm/s", "role_speed", MODEL_ANALYTIC,
                 inputs=limits, note="governed by %s" % governing)
    r.domain = {"governing": governing, "unknown_limits": missing}
    return r


def curvature_speed_limit(radius, a_normal):
    """v^2 / R <= a_normal."""
    if not all(is_number(x) and x > 0 for x in (radius, a_normal)):
        return invalid("curvature_speed_limit", "R and a must be > 0")
    return computed(math.sqrt(a_normal * radius), "mm/s", "curvature_speed_limit", MODEL_ANALYTIC)


def move_time(length, v_max, accel):
    """Rest-to-rest move with constant acceleration (triangular/trapezoidal).

    L_acc = v^2 / a; t = 2 sqrt(L/a) for L <= L_acc else L/v + v/a.
    """
    if not all(is_number(x) for x in (length, v_max, accel)) or length < 0 or v_max <= 0 or accel <= 0:
        return invalid("move_time", "L >= 0, v > 0, a > 0 required")
    l_acc = v_max ** 2 / accel
    if length <= l_acc:
        t = 2.0 * math.sqrt(length / accel)
        profile = "triangular"
    else:
        t = length / v_max + v_max / accel
        profile = "trapezoidal"
    r = computed(t, "s", "move_time", MODEL_ANALYTIC, inputs={"L": length, "v": v_max, "a": accel},
                 note="No lookahead/jerk; firmware simulation needed for real time")
    r.domain = {"profile": profile, "L_acc": l_acc}
    return r


def extrusion_time_lower_bound(volume, q_limit):
    """t_extrusion >= V_command / Q_limit."""
    if not is_number(q_limit):
        return unknown("extrusion_time_lower_bound", "Q_limit unknown", ["Q_limit"])
    if not (is_number(volume) and volume >= 0 and q_limit > 0):
        return invalid("extrusion_time_lower_bound", "V >= 0, Q > 0")
    return computed(volume / q_limit, "s", "extrusion_time_lower_bound", MODEL_ANALYTIC)


def max_layer_height_for_step(eps_layer, surface_normals, layer_normal=(0.0, 0.0, 1.0)):
    """h <= eps / |dot(n_s, n_l)| over the critical inclined surfaces."""
    if not is_number(eps_layer) or eps_layer <= 0:
        return invalid("max_layer_height_for_step", "eps must be > 0")
    nl = _unit(layer_normal)
    worst = 0.0
    for n in surface_normals:
        d = abs(sum(a * b for a, b in zip(_unit(n), nl)))
        worst = max(worst, d)
    if worst < 1e-9:
        return computed(float("inf"), "mm", "max_layer_height_for_step", MODEL_ANALYTIC,
                        note="All critical surfaces are parallel to the build direction")
    return computed(eps_layer / worst, "mm", "max_layer_height_for_step", MODEL_ANALYTIC,
                    inputs={"eps": eps_layer, "max_abs_dot": worst})


def choose_layer_height(h_max, allowed, window=None):
    """Largest allowed h <= h_max inside the calibrated window.

    If the requirement needs h below the window, the result is infeasible:
    another route, nozzle or post-processing is needed (no silent clamping).
    """
    allowed = sorted(float(h) for h in allowed if is_number(h) and h > 0)
    if window is not None:
        lo, hi = window
        allowed = [h for h in allowed if lo - 1e-12 <= h <= hi + 1e-12]
    if not allowed:
        return invalid("choose_layer_height", "no layer height in the allowed window")
    if not is_number(h_max) and h_max != float("inf"):
        return unknown("choose_layer_height", "h_max unknown")
    feasible = [h for h in allowed if h <= h_max + 1e-12]
    if not feasible:
        r = unknown("choose_layer_height", "required h %.4f below the calibrated window" % h_max,
                    status="infeasible")
        r.domain = {"min_allowed": allowed[0], "h_max": h_max}
        return r
    return computed(feasible[-1], "mm", "choose_layer_height", MODEL_ANALYTIC,
                    inputs={"h_max": h_max, "allowed": allowed})


def tolerance_budget(total, components, method="sum"):
    """eps_mesh + eps_layer + eps_path + eps_shrink + eps_machine + eps_measure <= eps_total.

    method "sum" is the conservative sum of absolute bounds; "rss" is allowed
    only for independent centred random errors with a known model.
    Unknown components make the result unknown (not a pass).
    """
    missing = sorted(k for k, v in components.items() if not is_number(v))
    if missing:
        r = unknown("tolerance_budget", "unknown error components", missing)
        r.domain = {"total": total}
        return r
    vals = [abs(float(v)) for v in components.values()]
    used = math.sqrt(sum(v * v for v in vals)) if method == "rss" else sum(vals)
    remaining = total - used
    r = computed({"used": used, "remaining": remaining, "feasible": remaining >= -1e-12}, "mm",
                 "tolerance_budget", MODEL_ANALYTIC if method == "sum" else MODEL_EXPERIMENTAL,
                 inputs={"total": total, "components": components, "method": method})
    if remaining < -1e-12:
        r.status = "infeasible"
    return r


def process_error_check(tolerance, process_error_bound):
    """Measured upper bound of the total process error against a symmetric tolerance."""
    if not (is_number(tolerance) and is_number(process_error_bound)):
        return unknown("process_error_check", "tolerance or process error unknown")
    return PASS if abs(process_error_bound) <= abs(tolerance) else FAIL


def compensate_dimension(target, a, b):
    """d_measured = a d_command + b  ->  d_command = (d_target - b) / a, a != 0."""
    if not all(is_number(x) for x in (target, a, b)):
        return unknown("compensate_dimension", "calibration a, b required", ["a", "b"],
                       status="needs_calibration")
    if abs(a) < 1e-12:
        return invalid("compensate_dimension", "a must be non-zero")
    return computed((target - b) / a, "mm", "compensate_dimension", MODEL_EXPERIMENTAL,
                    inputs={"target": target, "a": a, "b": b})


def shrink_scale(shrinkage):
    """scale = 1 / (1 - s) for proven uniform shrinkage s."""
    if not is_number(shrinkage) or not 0 <= shrinkage < 1:
        return invalid("shrink_scale", "0 <= s < 1")
    return computed(1.0 / (1.0 - shrinkage), "fraction", "shrink_scale", MODEL_EXPERIMENTAL)


def offset_diameter_change(xy_offset):
    """Material expansion delta: outer diameter +2 delta, hole diameter -2 delta."""
    if not is_number(xy_offset):
        return invalid("offset_diameter_change", "offset required")
    return computed({"outer": 2.0 * xy_offset, "hole": -2.0 * xy_offset}, "mm", "offset_diameter_change",
                    MODEL_GEOMETRIC)


def xy_offset_plan(outer_error=None, hole_error=None, independent_hole_offset=False):
    """xy_offset from measured diameter errors (measured - nominal).

    Outer error e_o needs delta = -e_o/2; hole error e_h needs delta = +e_h/2.
    Without an independent hole offset a single value cannot fix opposite
    demands; the result is then 'unsupported'.
    """
    deltas = {}
    if is_number(outer_error):
        deltas["outer"] = -outer_error / 2.0
    if is_number(hole_error):
        deltas["hole"] = hole_error / 2.0
    if not deltas:
        return unknown("xy_offset_plan", "no measured dimensional errors", ["outer_error", "hole_error"],
                       status="needs_calibration")
    if len(deltas) == 2 and not independent_hole_offset and abs(deltas["outer"] - deltas["hole"]) > 1e-6:
        r = unknown("xy_offset_plan", "one global xy_offset cannot correct both errors", status="unsupported")
        r.domain = deltas
        return r
    value = deltas.get("outer", deltas.get("hole"))
    return computed({"xy_offset": value, "per_feature": deltas}, "mm", "xy_offset_plan", MODEL_EXPERIMENTAL)


def filament_diameter_correction(d_nominal, d_actual):
    """k_corr = (d_nom / d_actual)^2; do not apply on top of a flow calibration that includes it."""
    if not all(is_number(x) and x > 0 for x in (d_nominal, d_actual)):
        return invalid("filament_diameter_correction", "diameters must be > 0")
    return computed((d_nominal / d_actual) ** 2, "fraction", "filament_diameter_correction", MODEL_GEOMETRIC)


def orca_flow_legacy(k_old, modifier_percent):
    """k_new = k_old * (100 + modifier) / 100 (Legacy Pass 1/2)."""
    return computed(k_old * (100.0 + modifier_percent) / 100.0, "fraction", "orca_flow_legacy", MODEL_EXPERIMENTAL)


def orca_flow_yolo(k_old, modifier_decimal):
    """k_new = k_old + modifier (YOLO)."""
    return computed(k_old + modifier_decimal, "fraction", "orca_flow_yolo", MODEL_EXPERIMENTAL)


def estimate_part_volumes(volume, area, shell_thickness, skin_thickness_total=0.0):
    """Thin-shell split of a closed part: V_shell ~ A t, the rest is core.

    A pre-slice estimate only; the slicer's extrusion volume is authoritative.
    """
    if not all(is_number(x) and x >= 0 for x in (volume, area, shell_thickness)):
        return invalid("estimate_part_volumes", "inputs must be >= 0")
    shell = min(volume, area * shell_thickness)
    skin = 0.0
    core = max(volume - shell - skin, 0.0)
    return computed({"V_shell": shell, "V_skin": skin, "V_core": core}, "mm3", "estimate_part_volumes",
                    MODEL_ANALYTIC, note="Shell includes top/bottom skins; double counting avoided by min()")


def mass_estimate(density, v_shell, infill_fraction, v_core, v_skin=0.0, v_support=0.0, v_aux=0.0):
    """mass ~ rho (V_shell + phi V_core + V_skin + V_support + V_aux)."""
    if not is_number(density):
        return unknown("mass_estimate", "density unknown", ["density"])
    if not is_number(infill_fraction) or not 0 <= infill_fraction <= 1:
        return invalid("mass_estimate", "phi must be within [0, 1]")
    part = density * (v_shell + infill_fraction * v_core + v_skin)
    total = part + density * (v_support + v_aux)
    return computed({"part_g": part, "material_g": total}, "g", "mass_estimate", MODEL_ANALYTIC,
                    note="Part mass excludes supports; material use includes them")


def cost_estimate(mass_g, time_s, c_material_per_g=None, c_machine_per_h=None, c_energy=0.0,
                  c_post=0.0, c_qa=0.0, c_setup=0.0, batch=1):
    """cost = c_material mass + c_machine time + energy + post + QA + setup/batch."""
    if not (is_number(c_material_per_g) and is_number(c_machine_per_h)):
        return unknown("cost_estimate", "cost rates are a policy input", ["c_material", "c_machine"])
    c = c_material_per_g * mass_g + c_machine_per_h * time_s / 3600.0 + c_energy + c_post + c_qa + \
        c_setup / max(batch, 1)
    return computed(c, "currency", "cost_estimate", MODEL_POLICY)


def _unit(v):
    n = math.sqrt(sum(float(x) ** 2 for x in v))
    return [float(x) / n for x in v]
