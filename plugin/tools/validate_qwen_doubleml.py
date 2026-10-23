# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Live Timeweb -> core Double ML protocol check, using SYNTHETIC software fixtures.

No physical measurements or part qualification. The test controller forces each
tool stage; Qwen supplies the actual arguments and receives real core results.
No raw observations, STL or credentials are included in messages or reports.
"""
import argparse
import json
import os
import sys

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN)
sys.path.insert(0, os.path.join(PLUGIN, "tests"))
from test_qwen_integration import box_model
from test_qwen_doubleml import fixture_data, TZ, KEY, OUTCOME, DATASET
from ste_planner import planner
from ste_planner.qwen_assistant import PlanningAssistant
from ste_planner.qwen_client import QwenClient


class ProbeClient(object):
    def __init__(self, client, target):
        self.client, self.target, self.round = client, target, 0

    def complete(self, messages, tools=None, **kwargs):
        messages = [dict(m) for m in messages]
        messages[0]["content"] += ("\nПрограммная проверка на синтетике. После планирования перечисли модели; "
                                  "затем запроси estimate_setting_effect с model_id=calibration:%s, "
                                  "treatment=%s, outcome=%s, to_value=%g. Заверши кратким пояснением, "
                                  "что это проверка программной связи, а не испытания печати." %
                                  (DATASET, KEY, OUTCOME, self.target))
        sequence = ["propose_requirements", "plan_print", "list_causal_models", "estimate_setting_effect"]
        if self.round < len(sequence):
            kwargs["tool_choice"] = {"type": "function", "function": {"name": sequence[self.round]}}
        self.round += 1
        return self.client.complete(messages, tools=tools, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(PLUGIN, "reports", "qwen_doubleml_live.json"))
    args = parser.parse_args()
    client = QwenClient.from_env(os.path.join(os.path.dirname(PLUGIN), ".env"))
    evaluations = []
    def record_core(*args, **kwargs):
        decision = planner.plan_case(*args, **kwargs)
        evaluations.append(decision)
        return decision
    with box_model() as path:
        baseline = planner.plan_case(TZ, path)
        records = fixture_data(baseline)
        before = baseline["effective_settings"][KEY]
        target = before - 20 if before >= 20 else before + 20
        result = PlanningAssistant(ProbeClient(client, target), plan_fn=record_core,
                                   calibration_data=[records]).run(TZ, path)
    effects = result["causal_estimates"]
    effect = effects[0] if len(effects) == 1 else {}
    computed = effect.get("status") == "computed" and effect.get("backend") == "core_double_ml"
    expected = 2 * (target - before)
    close = computed and abs(effect["effect"] - expected) < 0.1
    stable_fields = ("status", "setting_patch", "effective_settings", "verification", "qualification_plan")
    # Compare with the actual baseline after the grounded extraction stage:
    # an accepted LLM requirement can legitimately alter verification vs. parse-only.
    stable = bool(evaluations) and all(result["decision"][k] == evaluations[0][k] for k in stable_fields)
    process_stable = result["decision"]["effective_settings"] == baseline["effective_settings"]
    patch_ok = computed and effect["proposed_patch"]["payload"]["extruders"]["0"][KEY] == target
    report = {"data_source": "SYNTHETIC_SOFTWARE_FIXTURE_ONLY", "physical_print_tests": False,
              "model": client.model, "validation_type": "live_forced_tool_sequence_native_double_ml",
              "tools_used": result["tools_used"], "llm_error": result["llm_error"],
              "core_status": result["decision"]["causal_analysis"]["status"],
              "baseline_unchanged": stable, "fixture_process_unchanged": process_stable,
              "native_percent_patch_correct": patch_ok,
              "expected_fixture_effect": expected, "observed_effect": effect.get("effect"),
              "contrast": effect,
              "verified": bool(not result["llm_error"] and close and stable and process_stable and patch_ok)}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print("Qwen -> native Double ML: verified=%s, effect=%s, expected=%s" %
          (report["verified"], report["observed_effect"], expected))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
