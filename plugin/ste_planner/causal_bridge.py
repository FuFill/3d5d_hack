# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Read-only bridge to fitted single-treatment DoubleMLPLR models.

No DoubleML dependency in the Python 3.5 slicer runtime. Training/export lives
outside it; reviewed scalar PLR effects can be registered here. An effect is
neither an absolute outcome prediction nor approval to apply a setting.
"""
import math


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


class PLREffect(object):
    def __init__(self, model_id, treatment, treatment_unit, outcome, outcome_unit,
                 coefficient, confidence_interval, treatment_bounds, context,
                 identification_reviewed=False, support_reviewed=False, level=0.95):
        nums = [coefficient, level] + list(confidence_interval) + list(treatment_bounds)
        if (not all(_finite(n) for n in nums) or len(confidence_interval) != 2
                or len(treatment_bounds) != 2 or not 0 < level < 1
                or not confidence_interval[0] <= coefficient <= confidence_interval[1]
                or treatment_bounds[0] >= treatment_bounds[1]):
            raise ValueError("Invalid scalar PLR estimate, interval or support")
        required = {"machine_id", "material_id", "mode", "nozzle_mm"}
        if not required.issubset(context) or any(context[k] is None for k in required):
            raise ValueError("Machine, material, mode and nozzle context required")
        self.model_id, self.treatment, self.outcome = model_id, treatment, outcome
        self.treatment_unit, self.outcome_unit = treatment_unit, outcome_unit
        self.coefficient, self.ci, self.bounds = coefficient, tuple(confidence_interval), tuple(treatment_bounds)
        self.context, self.level = dict(context), level
        self.identification_reviewed = identification_reviewed is True
        self.support_reviewed = support_reviewed is True
        self.reviewed = self.identification_reviewed and self.support_reviewed

    def to_record(self):
        """Portable JSON record, with no fitted Python object or executable state."""
        return {"model_id": self.model_id, "treatment": self.treatment, "treatment_unit": self.treatment_unit,
                "outcome": self.outcome, "outcome_unit": self.outcome_unit, "coefficient": self.coefficient,
                "confidence_interval": list(self.ci), "treatment_bounds": list(self.bounds),
                "context": dict(self.context), "identification_reviewed": self.identification_reviewed,
                "support_reviewed": self.support_reviewed, "level": self.level}

    @classmethod
    def from_fitted_doubleml(cls, fitted, **metadata):
        """Call in the modern training process, not by loading pickles in the slicer.

        Only a single-treatment PLR coefficient is supported. IRM/PLIV/CATE must
        use a separate explicitly specified bridge, not this linear contrast.
        """
        if type(fitted).__name__ != "DoubleMLPLR":
            raise ValueError("Only fitted DoubleMLPLR is supported")
        interval = fitted.confint(level=metadata.get("level", 0.95))
        # Public confint() rows are indexed by treatment; params_names names
        # nuisance learners (ml_l/ml_m), not treatment variables.
        if list(interval.index) != [metadata.get("treatment")]:
            raise ValueError("Treatment metadata does not match the fitted model")
        coef = fitted.coef.tolist()
        ci = interval.values.tolist()
        if len(coef) != 1 or len(ci) != 1 or len(ci[0]) != 2:
            raise ValueError("Exactly one treatment required")
        return cls(coefficient=float(coef[0]), confidence_interval=ci[0], **metadata)

    def estimate(self, before, after, context):
        if not self.reviewed:
            return {"status": "unknown", "reason": "identification_and_support_not_reviewed"}
        if not _finite(before) or not _finite(after):
            return {"status": "invalid_input", "reason": "finite_treatment_values_required"}
        if any(context.get(k) != v for k, v in self.context.items()):
            return {"status": "out_of_domain", "reason": "context_mismatch"}
        if not all(self.bounds[0] <= x <= self.bounds[1] for x in (before, after)):
            return {"status": "out_of_domain", "reason": "treatment_outside_reviewed_support"}
        delta = after - before
        interval = sorted([delta * self.ci[0], delta * self.ci[1]])
        return {"status": "computed", "model_id": self.model_id, "model_type": "DoubleMLPLR",
                "estimand": "constant_partial_linear_effect_contrast", "outcome": self.outcome,
                "unit": self.outcome_unit, "treatment": self.treatment, "treatment_unit": self.treatment_unit,
                "from": before, "to": after, "effect": delta * self.coefficient,
                "confidence_interval": interval, "confidence_level": self.level,
                "absolute_prediction": False, "setting_approval": False,
                "assumptions": ["reviewed identification assumptions", "constant linear effect in reviewed support",
                                "unchanged context and other settings"],
                "note": "Confidence interval concerns the estimated causal contrast, not a part-level prediction interval"}


class CausalRegistry(object):
    def __init__(self, models=None):
        self.models = {}
        for model in models or []:
            if model.model_id in self.models:
                raise ValueError("Duplicate causal model id")
            self.models[model.model_id] = model

    def estimate(self, model_id, treatment, outcome, before, after, context, treatment_unit=None):
        model = self.models.get(model_id)
        if model is None:
            return {"status": "needs_calibration", "reason": "no_registered_fitted_causal_model"}
        if model.treatment != treatment or model.outcome != outcome:
            return {"status": "invalid_input", "reason": "model_treatment_or_outcome_mismatch"}
        if treatment_unit is not None and model.treatment_unit != treatment_unit:
            return {"status": "invalid_input", "reason": "treatment_unit_mismatch"}
        return model.estimate(before, after, context)

    def describe(self):
        return [{"model_id": m.model_id, "treatment": m.treatment, "treatment_unit": m.treatment_unit,
                 "outcome": m.outcome, "outcome_unit": m.outcome_unit, "context": m.context,
                 "treatment_bounds": m.bounds, "identification_and_support_reviewed": m.reviewed}
                for m in sorted(self.models.values(), key=lambda m: m.model_id)]
