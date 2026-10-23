# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Feasibility, Pareto front and explicit choice policy (specification section 10).

Hard constraints are checked first; a candidate with any hard failure never
enters the front, whatever its objectives. Objectives are minimised and
standardised before weighting.
"""

from .result import FAIL, UNKNOWN, PASS


def feasible(candidates):
    """Split into (feasible, rejected) by hard checks; unknown does not reject."""
    ok, rejected = [], []
    for c in candidates:
        if any(h == FAIL for h in c.get("hard", {}).values()):
            rejected.append(c)
        else:
            ok.append(c)
    return ok, rejected


def dominates(a, b, objectives):
    better = False
    for k in objectives:
        if a["objectives"][k] > b["objectives"][k]:
            return False
        if a["objectives"][k] < b["objectives"][k]:
            better = True
    return better


def pareto_front(candidates, objectives):
    """Non-dominated feasible candidates (all objectives minimised)."""
    ok, _ = feasible(candidates)
    ok = [c for c in ok if all(c.get("objectives", {}).get(k) is not None for k in objectives)]
    return [c for c in ok if not any(dominates(o, c, objectives) for o in ok if o is not c)]


def choose_by_policy(front, weights):
    """Weighted sum of min-max standardised objectives; ties broken by id (determinism)."""
    if not front:
        return None
    keys = sorted(weights)
    lo = dict((k, min(c["objectives"][k] for c in front)) for k in keys)
    hi = dict((k, max(c["objectives"][k] for c in front)) for k in keys)

    def score(c):
        s = 0.0
        for k in keys:
            span = hi[k] - lo[k]
            s += weights[k] * ((c["objectives"][k] - lo[k]) / span if span > 0 else 0.0)
        return s

    return sorted(front, key=lambda c: (score(c), str(c.get("id"))))[0]


def domain_check(x, domain):
    """in / out / unknown: every variable must lie inside the validated domain of a model."""
    if not domain:
        return UNKNOWN
    status = PASS
    for k, (lo, hi) in sorted(domain.items()):
        v = x.get(k)
        if v is None:
            status = UNKNOWN
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return FAIL
    return status
