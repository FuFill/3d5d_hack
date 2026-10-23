# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Common result contract of the planner (specification section 11).

Every computation returns a Result with value, unit, status, evidence,
uncertainty, domain and trace. Requirement checks use three-valued logic:
an unknown value never turns into a pass.

The module must stay compatible with Python 3.5 (STE Slicer 7.10 runtime).
"""

import json
import math

# Status of a single computation.
COMPUTED = "computed"
UNKNOWN = "unknown"
INVALID_INPUT = "invalid_input"
OUT_OF_DOMAIN = "out_of_domain"
NEEDS_INPUT = "needs_input"
NEEDS_CALIBRATION = "needs_calibration"

# Three-valued requirement check.
PASS = "pass"
FAIL = "fail"

# Decision statuses (specification section 1).
DECISION_NEEDS_INPUT = "needs_input"
DECISION_INFEASIBLE = "infeasible"
DECISION_NEEDS_CALIBRATION = "needs_calibration"
DECISION_SEARCH_INCOMPLETE = "search_incomplete"
DECISION_CANDIDATE = "candidate"
DECISION_SLICE_VERIFIED = "slice_verified"
DECISION_QUALIFIED = "qualified"

# Evidence levels; a successful slice never raises them automatically.
EVIDENCE_DOCUMENT = "document"
EVIDENCE_ANALYTICAL = "analytical_screen"
EVIDENCE_SIMULATION = "simulation"
EVIDENCE_COUPON = "coupon_test"
EVIDENCE_PART = "part_test"
EVIDENCE_ORDER = [EVIDENCE_DOCUMENT, EVIDENCE_ANALYTICAL, EVIDENCE_SIMULATION, EVIDENCE_COUPON, EVIDENCE_PART]

# Dependency classes (specification section 2).
MODEL_GEOMETRIC = "G"
MODEL_ANALYTIC = "A"
MODEL_EXPERIMENTAL = "E"
MODEL_POLICY = "P"
MODEL_SLICER = "S"


class Result(object):
    """Value with unit, status and provenance."""

    def __init__(self, value=None, unit=None, status=COMPUTED, evidence=None, uncertainty=None,
                 domain=None, trace=None, model_id=None, model_class=None):
        self.value = value
        self.unit = unit
        self.status = status
        self.evidence = evidence
        self.uncertainty = uncertainty
        self.domain = domain if domain is not None else {}
        self.trace = trace if trace is not None else []
        self.model_id = model_id
        self.model_class = model_class

    @property
    def ok(self):
        return self.status == COMPUTED

    def require(self):
        """Return the value or raise if the computation did not succeed."""
        if not self.ok:
            raise ValueError("%s: %s" % (self.model_id, self.status))
        return self.value

    def to_dict(self):
        return {
            "value": _jsonable(self.value),
            "unit": self.unit,
            "status": self.status,
            "evidence": self.evidence,
            "uncertainty": _jsonable(self.uncertainty),
            "domain": _jsonable(self.domain),
            "trace": _jsonable(self.trace),
            "model_id": self.model_id,
            "model_class": self.model_class,
        }

    def __repr__(self):
        return "Result(%r %s, %s, %s)" % (self.value, self.unit or "", self.status, self.model_id)


def computed(value, unit, model_id, model_class, inputs=None, note=None, evidence=EVIDENCE_ANALYTICAL,
             uncertainty=None, domain=None):
    trace = [{"model_id": model_id, "inputs": inputs or {}, "note": note}]
    return Result(value, unit, COMPUTED, evidence, uncertainty, domain, trace, model_id, model_class)


def unknown(model_id, reason, missing=None, status=UNKNOWN, model_class=None):
    trace = [{"model_id": model_id, "reason": reason, "missing": list(missing or [])}]
    return Result(None, None, status, None, None, None, trace, model_id, model_class)


def invalid(model_id, reason, model_class=None):
    return unknown(model_id, reason, status=INVALID_INPUT, model_class=model_class)


def is_number(x):
    if isinstance(x, bool):
        return False
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def tri_and(values):
    """Conjunction in three-valued logic: fail dominates, then unknown."""
    values = list(values)
    if FAIL in values:
        return FAIL
    if UNKNOWN in values or not values:
        return UNKNOWN
    return PASS


def tri_or(values):
    values = list(values)
    if PASS in values:
        return PASS
    if UNKNOWN in values or not values:
        return UNKNOWN
    return FAIL


def tri_compare(value, comparator, limit):
    """Compare a scalar or an interval (lo, hi) with a limit conservatively.

    For an interval the check passes only if the whole interval satisfies the
    limit and fails only if no point of it does; otherwise it is unknown.
    """
    if value is None or limit is None:
        return UNKNOWN
    if isinstance(value, (tuple, list)):
        lo, hi = value
    else:
        lo = hi = value
    if not (is_number(lo) and is_number(hi)):
        return UNKNOWN
    lo, hi = float(lo), float(hi)
    if isinstance(limit, (tuple, list)):
        llo, lhi = float(limit[0]), float(limit[1])
    else:
        llo = lhi = float(limit)
    if comparator in (">=", "ge"):
        if lo >= lhi:
            return PASS
        if hi < llo:
            return FAIL
        return UNKNOWN
    if comparator in ("<=", "le"):
        if hi <= llo:
            return PASS
        if lo > lhi:
            return FAIL
        return UNKNOWN
    if comparator in ("in", "within"):
        if lo >= llo and hi <= lhi:
            return PASS
        if hi < llo or lo > lhi:
            return FAIL
        return UNKNOWN
    raise ValueError("Unsupported comparator %r" % (comparator,))


def constraint_margin(value, comparator, limit):
    """g_j(x) <= 0 form of a constraint (specification section 3); None if unknown."""
    if not (is_number(value) and is_number(limit)):
        return None
    if comparator in ("<=", "le"):
        return float(value) - float(limit)
    if comparator in (">=", "ge"):
        return float(limit) - float(value)
    raise ValueError("Unsupported comparator %r" % (comparator,))


def tolerance_margin(measured, nominal, tolerance):
    """g = |d - d_nom| - eps."""
    if not (is_number(measured) and is_number(nominal) and is_number(tolerance)):
        return None
    return abs(float(measured) - float(nominal)) - float(tolerance)


def _jsonable(obj):
    if isinstance(obj, Result):
        return obj.to_dict()
    if isinstance(obj, dict):
        return dict((str(k), _jsonable(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "tolist"):
        return obj.tolist()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if hasattr(obj, "item"):
        return obj.item()
    return obj


def to_json(obj, indent=None):
    """Deterministic JSON (sorted keys; Python 3.5 dicts are unordered)."""
    return json.dumps(_jsonable(obj), sort_keys=True, ensure_ascii=False, indent=indent)
