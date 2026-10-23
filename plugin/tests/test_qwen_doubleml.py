# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Qwen tools -> native core calibration. Synthetic fixtures are not print evidence."""
import copy
import json

from testutil import resources_dir, assert_close, raises
from test_qwen_integration import FakeClient, call, box_model, model
from ste_planner import planner
from ste_planner.causal_bridge import CausalRegistry
from ste_planner.qwen_assistant import PlanningAssistant

TZ = "Наименование детали: Проверочный куб\nМасса не более 50 г."
KEY = "infill_sparse_density"
OUTCOME = "breaking_load"
DATASET = "software_fixture_only"


def fixture_data(baseline):
    """Software records only; source exercises the physical-test validation path."""
    import numpy
    rng = numpy.random.RandomState(23)
    context = copy.deepcopy(baseline["calibration_context"])
    settings = baseline["effective_settings"]
    context["fixed_settings"] = dict((k, settings[k]) for k in ("layer_height", "machine_nozzle_size"))
    density = settings[KEY]
    levels = [max(0.0, density - 20), density, min(100.0, density + 20)]
    walls = settings["wall_line_count"]
    rows = []
    for job in range(90):
        d, w = levels[job % 3], walls + (job // 3) % 4
        noise = float(rng.normal(scale=0.1))
        for sample in range(2):
            rows.append({"sample_id": "%s:%s" % (job, sample), "print_job_id": str(job),
                         "settings": {KEY: d, "wall_line_count": w}, "outcome": 2 * d + 20 * w + noise})
    return {"dataset_id": DATASET, "source": "physical_test",
            "design": "SYNTHETIC SOFTWARE FIXTURE ONLY, not measured print evidence",
            "context": context, "treatment": KEY, "covariates": ["wall_line_count"],
            "units": {KEY: "%", "wall_line_count": "count"},
            "outcome": {"name": OUTCOME, "unit": "N"}, "observations": rows}


def effect_call(target, outcome=OUTCOME, dataset=DATASET):
    return call("estimate_setting_effect", {"model_id": "calibration:" + dataset,
                "treatment": KEY, "outcome": outcome, "to_value": target}, "effect")


def client_for(*effects):
    return FakeClient([{"tool_calls": [call("propose_requirements", {"items": []})]},
                       {"tool_calls": [call("plan_print", {}, "plan")]},
                       {"tool_calls": [call("list_causal_models", {}, "list")]}] +
                      [{"tool_calls": [item]} for item in effects] + [{"content": "Оценка готова."}])


def test_native_tools_fit_contrast_and_keep_baseline_unchanged():
    with box_model() as path:
        baseline = planner.plan_case(TZ, path, resources_dir=resources_dir())
        data = fixture_data(baseline)
        before = baseline["effective_settings"][KEY]
        target = before - 20 if before >= 20 else before + 20
        client = client_for(effect_call(target), effect_call(before))
        result = PlanningAssistant(client, calibration_data=[data]).run(TZ, path)
    assert result["llm_error"] is None
    models = result["events"][2]["result"]["models"]
    assert models[0]["model_id"] == "calibration:" + DATASET and models[0]["status"] == "computed"
    assert models[0]["independent_groups"] == 90
    effect, unchanged = result["causal_estimates"]
    assert effect["status"] == "computed" and effect["backend"] == "core_double_ml"
    assert abs(effect["effect"] - 2 * (target - before)) < 0.1
    assert effect["confidence_interval"][0] <= effect["effect"] <= effect["confidence_interval"][1]
    assert_close(effect["proposed_patch"]["payload"]["extruders"]["0"][KEY], target)
    assert unchanged["effect"] == 0 and unchanged["from"] == before
    assert not effect["absolute_prediction"] and not effect["setting_approval"]
    assert result["decision"]["causal_analysis"]["status"] == "computed"
    for field in ("status", "setting_patch", "effective_settings", "verification", "qualification_plan"):
        assert result["decision"][field] == baseline[field], field
    # Tool responses contain summaries, not raw sample records.
    assert "sample_id" not in json.dumps(client.requests)
    assert "observations\": [" not in json.dumps(client.requests)


def test_native_tools_reject_extrapolation_mismatched_outcomes_and_injected_data():
    with box_model() as path:
        baseline = planner.plan_case(TZ, path)
        data = fixture_data(baseline)
        client = client_for(effect_call(150), effect_call(80, outcome="fabricated"))
        result = PlanningAssistant(client, calibration_data=[data]).run(TZ, path)
    assert result["causal_estimates"][0]["status"] == "out_of_domain"
    assert result["events"][-1]["result"]["status"] == "invalid_input"
    assert result["decision"]["effective_settings"] == baseline["effective_settings"]
    injected = FakeClient([{"tool_calls": [call("propose_requirements", {"items": [], "calibration_data": [data]})]},
                           {"content": "OK"}])
    with box_model() as path:
        rejected = PlanningAssistant(injected).run(TZ, path)
    assert rejected["events"][0]["result"]["status"] == "invalid_input"
    assert rejected["decision"]["causal_analysis"]["status"] == "needs_calibration"


def test_native_failure_statuses_are_not_replaced_by_external_effects():
    with box_model() as path:
        baseline = planner.plan_case(TZ, path)
        data = fixture_data(baseline)
        variants = []
        wrong = copy.deepcopy(data)
        wrong["context"]["geometry_sha256"] = "different"
        variants.append((wrong, "out_of_domain"))
        wrong = copy.deepcopy(data)
        wrong["units"][KEY] = "fraction"
        variants.append((wrong, "invalid_input"))
        wrong = copy.deepcopy(data)
        wrong["source"] = "synthetic"
        variants.append((wrong, "needs_calibration"))
        for wrong, expected in variants:
            result = PlanningAssistant(client_for(effect_call(80)), CausalRegistry([model()]),
                                       calibration_data=[wrong]).run(TZ, path)
            assert result["events"][2]["result"]["models"][0]["status"] == expected
            assert result["causal_estimates"][0]["status"] == expected


def test_native_contrast_detects_a_changed_plan():
    with box_model() as path:
        baseline = planner.plan_case(TZ, path)
        data = fixture_data(baseline)
        def changing_core(*args, **kwargs):
            decision = planner.plan_case(*args, **kwargs)
            if kwargs.get("setting_changes"):
                decision["calibration_context"]["geometry_sha256"] = "changed"
            return decision
        result = PlanningAssistant(client_for(effect_call(80)), plan_fn=changing_core,
                                   calibration_data=[data]).run(TZ, path)
    assert result["causal_estimates"][0]["reason"] == "planned_context_changed"
    assert result["decision"]["calibration_context"] == baseline["calibration_context"]


def test_core_and_exported_models_have_separate_namespaces():
    exported = model()
    exported.model_id = "calibration:" + DATASET
    raises(ValueError, CausalRegistry, [exported])


def test_native_api_failure_still_runs_calibration():
    from ste_planner.qwen_client import QwenError
    with box_model() as path:
        baseline = planner.plan_case(TZ, path)
        data = fixture_data(baseline)
        result = PlanningAssistant(FakeClient([QwenError("network", "Unavailable")]),
                                   calibration_data=[data]).run(TZ, path)
    assert result["llm_error"]["code"] == "network"
    assert result["decision"]["causal_analysis"]["status"] == "computed"
    assert result["decision"]["effective_settings"] == baseline["effective_settings"]
