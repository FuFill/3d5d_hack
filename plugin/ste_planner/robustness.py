"""Bounded process sensitivity. No invented noise law or settings->strength fit.

Constant Z bias affects the first-layer gap; independent layer-position errors
affect increments through e_i-e_(i-1). Constitutive multipliers may be supplied
only as explicit scenarios/measurements; Z bias is never converted to modulus.
Uniform elastic/load/strength scaling is exact for the existing linear model,
not for nonlinear failure, buckling, fatigue, or changed bead bonding.
"""
import itertools
import math

from .result import computed, unknown, invalid, MODEL_ANALYTIC

VARIABLES = {"z_offset_mm": (0., 0.), "layer_position_error_mm": (0., 0.),
             "flow_scale": (1., 1.), "speed_scale": (1., 1.),
             "elastic_scale": (1., 1.), "strength_scale": (1., 1.), "load_scale": (1., 1.)}


def assess_process_stability(effective, mechanics, noise=None, limits=None):
    """Worst-case interval envelope over the declared uncertainty box.

    An all-pass return is conditional on supplied tolerances and noise bounds;
    it is not physical qualification or a probability-of-success estimate.
    Missing quality-response measurements always stay explicit.
    """
    model = "process_stability"
    if not isinstance(noise, dict) or not noise.get("bounds"):
        return unknown(model, "measured noise bounds and quality tolerances required",
                       ["noise_bounds", "first_layer_gap_limits", "process_response_calibration"], status="needs_calibration")
    try:
        bounds = dict(VARIABLES)
        supplied = noise["bounds"]
        if set(supplied) - set(VARIABLES):
            raise ValueError("unsupported noise variable; do not infer a response model")
        for key, pair in supplied.items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2 or any(isinstance(v, bool) for v in pair):
                raise ValueError("each bound must be two finite ordered numbers")
            lo, hi = map(float, pair)
            if not math.isfinite(lo) or not math.isfinite(hi) or lo > hi:
                raise ValueError("finite ordered bounds required")
            if key.endswith("scale") and lo <= 0:
                raise ValueError("linear scaling requires positive multipliers")
            bounds[key] = (lo, hi)
        if noise.get("source") not in ("physical_test", "user_scenario", "software_fixture"):
            raise ValueError("explicit noise provenance required")
        if noise["source"] == "physical_test" and not noise.get("protocol"):
            raise ValueError("measurement protocol required for physical noise bounds")
        h, first = float(effective["layer_height"]), float(effective["layer_height_0"])
        q = h * float(effective["line_width"]) * float(effective["speed_print"])
        if not all(math.isfinite(v) and v > 0 for v in (h, first, q)):
            raise ValueError("positive finite nominal layer/flow settings required")
        keys = sorted(bounds)
        cases = []
        # Endpoints suffice for these monotonic positive scaling relationships.
        for values in itertools.product(*[sorted(set(bounds[k])) for k in keys]):
            p = dict(zip(keys, values))
            e, s, load = p["elastic_scale"], p["strength_scale"], p["load_scale"]
            cases.append({"first_layer_gap_mm": first + p["z_offset_mm"] + p["layer_position_error_mm"],
                "commanded_flow_proxy_mm3_s": q * p["flow_scale"] * p["speed_scale"],
                "effective_stiffness_N_per_mm": mechanics["effective_stiffness_N_per_mm"] * e,
                "compliance_Nmm": mechanics["compliance_Nmm"] * load ** 2 / e,
                "max_displacement_mm": mechanics["max_displacement_mm"] * load / e,
                "safety_factor_min": mechanics["safety_factor_min"] * s / load})
        envelope = {k: [min(c[k] for c in cases), max(c[k] for c in cases)] for k in cases[0]}
        jitter = bounds["layer_position_error_mm"]
        envelope["layer_increment_mm"] = [h + jitter[0] - jitter[1], h + jitter[1] - jitter[0]]
        checks, missing = [], []
        for metric, key in (("first_layer_gap_mm", "first_layer_gap_mm"), ("layer_increment_mm", "layer_increment_mm"),
                            ("commanded_flow_proxy_mm3_s", "flow_mm3_s"), ("max_displacement_mm", "displacement_mm"),
                            ("safety_factor_min", "safety_factor")):
            allowed = (limits or {}).get(key)
            if allowed is None:
                missing.append(key)
                checks.append({"metric": metric, "status": "unknown", "margin": None})
                continue
            if len(allowed) != 2 or any(v is None or isinstance(v, bool) or not math.isfinite(float(v)) for v in allowed) or allowed[0] > allowed[1]:
                raise ValueError("two finite ordered tolerance limits required")
            low_margin = envelope[metric][0] - allowed[0]
            high_margin = allowed[1] - envelope[metric][1]
            margin = min(low_margin, high_margin)
            checks.append({"metric": metric, "status": "pass" if margin >= 0 else "fail", "margin": margin,
                           "allowed": list(allowed), "envelope": envelope[metric]})
        value = {"source": noise["source"], "protocol": noise.get("protocol"), "declared_bounds": supplied,
                 "unvaried_dimensions_assumed_fixed": sorted(set(VARIABLES) - set(supplied)),
                 "corner_scenarios": len(cases), "envelope": envelope, "checks": checks,
                 "worst_case_violation_count": sum(c["status"] == "fail" for c in checks),
                 "success_probability": None, "physical_stability_verified": False,
                 "setting_approval": False, "missing_tolerances": missing,
                 "response_calibration_required": ["Z/flow/speed -> bead bonding, porosity, Ra, warp and constitutive properties"],
                 "interpretation": "conditional interval sensitivity, not independent printing evidence"}
        result = computed(value, None, model, MODEL_ANALYTIC, evidence=None)
        result.status = "needs_calibration"
        return result
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return invalid(model, str(exc))


def choose_stable_candidate(candidates, noise, limits):
    """Select within tested candidates by worst-case compliance after all gates.

    Only candidates whose complete interval checks pass are ranked; unknown
    tolerances cannot win. This is a conditional proposal, never auto-applied.
    """
    evaluated = []
    for candidate in candidates:
        result = assess_process_stability(candidate["settings"], candidate["mechanics"], noise, limits)
        value = result.value or {}
        eligible = bool(value.get("checks")) and all(c["status"] == "pass" for c in value["checks"])
        evaluated.append({"id": candidate["id"], "eligible_under_declared_bounds": eligible,
                          "assessment": result.to_dict()})
    feasible = [c for c in evaluated if c["eligible_under_declared_bounds"]]
    chosen = min(feasible, key=lambda c: (c["assessment"]["value"]["envelope"]["compliance_Nmm"][1], c["id"])) if feasible else None
    return {"status": "needs_calibration", "conditional_selected_id": chosen["id"] if chosen else None,
            "candidates": evaluated, "physical_optimum": None, "setting_approval": False,
            "scope": "finite candidates; declared independent box uncertainty; unknown checks excluded"}
