"""Native slice -> toolpath physics for EACH candidate, outside the Qt core.

The caller supplies the actual STE worker. This module never rotates cached
roads, shares a previous candidate's G-code, or selects unknown physics.
"""
import copy
import hashlib
import json

from . import toolpath_physics
from .result import computed, invalid, MODEL_ANALYTIC, EVIDENCE_SIMULATION


def decision_digest(decision):
    return hashlib.sha256(json.dumps(decision, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def evaluate_candidates(candidates, native_slice, analysis_inputs=None):
    """native_slice(id, decision) returns path/applied/sliced/diameter.

    analysis_inputs(id, decision) supplies optional measured road properties,
    volume mesh, service loads/fixtures, and an explicit machine pose contract.
    Every invocation calls STE anew, including repeated orientations/settings.
    No physical optimum is claimed while the coarse road model still requires
    bead/mesh convergence and material calibration.
    """
    try:
        if not isinstance(candidates, list) or not candidates or len(candidates) > 256:
            raise ValueError("one to 256 explicit candidates required")
        ids, outcomes = set(), []
        for candidate in candidates:
            name = candidate["id"]
            if not isinstance(name, str) or not name or name in ids:
                raise ValueError("distinct candidate ids required")
            ids.add(name)
            decision = copy.deepcopy(candidate["decision"])
            digest = decision_digest(decision)
            native = native_slice(name, copy.deepcopy(decision))
            options = analysis_inputs(name, copy.deepcopy(decision)) if analysis_inputs else {}
            result = toolpath_physics.analyze_slice(native["gcode_path"], decision, native["applied"],
                native["sliced"], native["filament_diameter_mm"], **options)
            outcomes.append({"id": name, "decision_sha256": digest, "physics": result.to_dict()})
        value = {"candidates": outcomes, "native_slice_invocations": len(outcomes),
                 "selected": None, "physical_print_approval": False,
                 "optimality_claim": "none; unresolved physical inputs are not feasible candidates",
                 "next_stage": "calibrate and verify bead/mesh physics, then compare identical service load cases"}
        result = computed(value, None, "native_sliced_search", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION)
        result.status = "needs_calibration"
        return result
    except (ValueError, TypeError, KeyError, IOError) as exc:
        return invalid("native_sliced_search", str(exc))
