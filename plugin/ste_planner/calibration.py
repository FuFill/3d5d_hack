# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Measured process experiments -> scoped DML effects and proposed single-setting patches.

Python 3.5 compatible. Effects never enter requirement verification as proof of
absolute strength and never silently replace the deterministic plan's settings.
"""

import hashlib
import json
from collections import Counter

from . import compiler, presets, surrogate
from .result import computed, unknown, invalid, is_number, MODEL_EXPERIMENTAL

CONTEXT_KEYS = ("machine_id", "material_id", "printing_mode", "geometry_sha256", "rotation")


def _same(a, b):
    if is_number(a) and is_number(b):
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))
    return a == b


def _unit(resolver, key):
    definition = resolver.schema.definition(key)
    return definition.get("unit") or ("count" if definition.type == "int" else "1")


def _proposal(resolver, key, value, dataset_id):
    # The public request uses slicer units. The compiler alone owns conversion
    # from physical fractions, so convert percent -> fraction at this boundary.
    quantity, converted = "dml_change", value
    for name, spec in compiler.QUANTITIES.items():
        if spec[0] == key:
            quantity = name
            converted = value / 100.0 if spec[2] == "percent" else value
            break
    decision = compiler.Decision(quantity, converted, key=key, model_ids=["double_ml:" + dataset_id])
    return compiler.compile_patch([decision], resolver)


def _assess_one(data, context, resolver, changes):
    model_id = "assess_double_ml"
    if not isinstance(data, dict):
        return invalid(model_id, "each experiment must be an object")
    dataset_id = data.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        return invalid(model_id, "a dataset_id is required for provenance")
    if data.get("source") != "physical_test":
        return unknown(model_id, "only measured physical-test outcomes may inform a print plan; "
                       "synthetic examples and TDS requirements are not calibration", status="needs_calibration")
    if not isinstance(data.get("design"), str) or not data["design"].strip():
        return unknown(model_id, "describe assignment, pre-treatment controls and measurement protocol",
                       ["design"], status="needs_input")
    training_context = data.get("context")
    if not isinstance(training_context, dict):
        return invalid(model_id, "experiment context must be an object")
    for key in CONTEXT_KEYS:
        if key not in training_context or training_context[key] is None:
            return unknown(model_id, "missing calibration context: " + key, [key], status="needs_input")
        if not _same(training_context[key], context.get(key)):
            return unknown(model_id, "calibration context differs: " + key, status="out_of_domain")
    if context.get("preset_base") is not None:
        # A plan over a native preset base: the experiment must name the same containers.
        if training_context.get("preset_base") is None:
            return unknown(model_id, "declare the native preset base (calibration_context.preset_base) of the experiment",
                           ["preset_base"], status="needs_input")
        if not presets.same_base(training_context["preset_base"], context["preset_base"]):
            return unknown(model_id, "calibration preset base differs", status="out_of_domain")
    fixed = training_context.get("fixed_settings")
    if not isinstance(fixed, dict) or not fixed:
        return unknown(model_id, "declare the non-varied process settings", ["fixed_settings"],
                       status="needs_input")
    for key, value in fixed.items():
        if not resolver.schema.has(key) or not _same(value, resolver.value(key, extruder="0")):
            return unknown(model_id, "fixed process setting differs: " + key, status="out_of_domain")

    treatment, controls = data.get("treatment"), data.get("covariates")
    if not isinstance(treatment, str) or not isinstance(controls, list):
        return invalid(model_id, "treatment must be a setting key and covariates a list of setting keys")
    keys = [treatment] + controls
    if not all(isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
        return invalid(model_id, "treatment and pre-treatment covariates must be distinct keys")
    timing = data.get("covariate_timing")
    if controls and (not isinstance(timing, dict) or any(k not in timing for k in controls)):
        return unknown(model_id, "declare when every control was assigned/measured; exclude post-treatment outcomes",
                       ["covariate_timing"], status="needs_input")
    if controls and any(timing[k] not in ("pre_treatment", "assigned_control") for k in controls):
        return invalid(model_id, "post-treatment or unspecified control timing risks outcome leakage")
    if set(fixed).intersection(keys):
        return invalid(model_id, "varied treatment/covariates cannot also be declared fixed")
    units = data.get("units")
    if not isinstance(units, dict):
        return invalid(model_id, "declare native slicer units for treatment and covariates")
    for key in keys:
        if not resolver.schema.has(key) or resolver.schema.definition(key).type not in ("int", "float"):
            return invalid(model_id, "numeric installed-slicer setting required: " + key)
        if units.get(key) != _unit(resolver, key):
            return invalid(model_id, "unit mismatch for " + key)
    if resolver.enabled(treatment, extruder="0") is not True:
        return unknown(model_id, "treatment is not enabled in this configuration", status="out_of_domain")
    outcome = data.get("outcome")
    if not isinstance(outcome, dict) or not all(isinstance(outcome.get(k), str) and outcome[k].strip()
                                                for k in ("name", "unit")):
        return invalid(model_id, "outcome needs a measured property name and unit")
    observations = data.get("observations")
    if not isinstance(observations, list) or not observations:
        return unknown(model_id, "no measured observations", status="needs_calibration")
    x, d, y, groups, samples, job_groups = [], [], [], [], set(), {}
    for row in observations:
        if not isinstance(row, dict) or not isinstance(row.get("settings"), dict):
            return invalid(model_id, "each observation needs settings and measured outcome")
        sample, group = row.get("sample_id"), row.get("print_job_id")
        if not all(isinstance(v, str) and v.strip() for v in (sample, group)) or sample in samples:
            return invalid(model_id, "unique sample_id and nonempty print_job_id required")
        samples.add(sample)
        independent = row.get("independence_group_id", group)
        if not isinstance(independent, str) or not independent.strip():
            return invalid(model_id, "nonempty independence_group_id required when supplied")
        if group in job_groups and job_groups[group] != independent:
            return invalid(model_id, "one print job cannot be split across independence groups")
        job_groups[group] = independent
        values = [row["settings"].get(key) for key in keys] + [row.get("outcome")]
        if not all(is_number(v) for v in values):
            return invalid(model_id, "missing/non-finite observed values; do not silently discard failed prints")
        if any(resolver.schema.definition(key).type == "int" and int(value) != value
               for key, value in zip(keys, values[:-1])):
            return invalid(model_id, "integer-valued setting has a non-integer observed value")
        # Fixed values are cohort metadata; if repeated in a row they must agree.
        if any(key in row["settings"] and not _same(value, row["settings"][key])
               for key, value in fixed.items()):
            return invalid(model_id, "observation contradicts the fixed-settings protocol")
        d.append(float(values[0]))
        x.append([float(v) for v in values[1:-1]])
        y.append(float(values[-1]))
        groups.append(independent)
    fitted = surrogate.fit_double_ml(x, d, y, groups=groups, n_folds=data.get("n_folds", 5),
                                    seed=data.get("seed", 0), outcome_unit=outcome["unit"])
    if not fitted.ok:
        return fitted
    fitted.unit = outcome["unit"] + "/" + units[treatment]
    # Confirm that the requested current context is inside all observed bounds.
    baseline = resolver.value(treatment, extruder="0")
    current_x = [resolver.value(key, extruder="0") for key in controls]
    target = changes.get(treatment, baseline)
    if not is_number(target):
        return invalid(model_id, "target must be a finite value in native slicer units")
    contrast = surrogate.double_ml_change(fitted, baseline, target, current_x)
    if not contrast.ok:
        return contrast
    proposal = _proposal(resolver, treatment, target, dataset_id) if treatment in changes else None
    if proposal and (proposal["errors"] or any(i["enabled_after_patch"] is not True for i in proposal["items"])):
        return invalid(model_id, "proposed setting change fails slicer validation: " + str(proposal["errors"]))
    controlled = dict(fixed)
    controlled.update(zip(controls, current_x))
    if proposal and any(not _same(value, proposal["resolver"].value(key, extruder="0"))
                        for key, value in controlled.items()):
        return unknown(model_id, "proposed change also changes a controlled process setting",
                       status="out_of_domain")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
                            .encode("utf-8")).hexdigest()
    value = {"dataset_id": dataset_id, "dataset_sha256": digest, "source": "physical_test",
             "design": data["design"], "treatment": treatment, "treatment_unit": units[treatment],
             "covariate_timing": timing or {},
             "cross_fit_grouping": "independence_group_id when supplied, otherwise print_job_id",
             "outcome": outcome, "fit": fitted.to_dict(),
             "change": contrast.to_dict() if treatment in changes else None,
             "proposed_patch": ({"payload": compiler.patch_to_slicer_payload(proposal),
                                  "warnings": proposal["warnings"]} if proposal else None)}
    return computed(value, outcome["unit"], model_id, MODEL_EXPERIMENTAL, evidence=None,
                    note="scoped statistical effect; assignment assumptions remain to be justified; "
                         "proposed patches are not applied and do not qualify requirements")


def assess_double_ml(calibration_data, context, resolver, setting_changes=None):
    """Assess each experiment independently; setting_changes uses native slicer values."""
    model_id = "assess_double_ml"
    if calibration_data is None:
        return unknown(model_id, "no measured print experiments supplied", ["calibration_data"],
                       status="needs_calibration")
    if not isinstance(calibration_data, list):
        return invalid(model_id, "calibration_data must be a list of experiments")
    if not calibration_data:
        return unknown(model_id, "no measured print experiments supplied", ["calibration_data"],
                       status="needs_calibration")
    changes = {} if setting_changes is None else setting_changes
    if not isinstance(changes, dict):
        return invalid(model_id, "setting_changes must map setting keys to native slicer values")
    if not all(isinstance(key, str) and is_number(value) for key, value in changes.items()):
        return invalid(model_id, "setting_changes needs string keys and finite numeric targets")
    ids = [data.get("dataset_id") for data in calibration_data
           if isinstance(data, dict) and isinstance(data.get("dataset_id"), str)]
    duplicates = set(key for key, count in Counter(ids).items() if count > 1)
    results = []
    for data in calibration_data:
        dataset_id = data.get("dataset_id") if isinstance(data, dict) else None
        try:
            if isinstance(dataset_id, str) and dataset_id in duplicates:
                result = invalid(model_id, "duplicate dataset_id")
            else:
                result = _assess_one(data, context, resolver, changes)
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            result = invalid(model_id, str(exc))
        entry = result.to_dict()
        entry["dataset_id"] = dataset_id if isinstance(dataset_id, str) else None
        results.append(entry)
    covered = set(data.get("treatment") for data in calibration_data
                  if isinstance(data, dict) and isinstance(data.get("treatment"), str))
    for key in sorted(set(changes) - covered):
        results.append(unknown(model_id, "no experiment for requested setting: " + str(key),
                               status="needs_calibration").to_dict())
    usable = [r["value"]["treatment"] for r in results if r["status"] == "computed"]
    status = "computed" if usable and set(changes).issubset(usable) else "needs_calibration"
    result = computed(results, None, model_id, MODEL_EXPERIMENTAL, evidence=None)
    result.status = status
    return result
