# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Explicit live Qwen validation on the seven organiser cases. No slicer writes."""
import argparse
import glob
import json
import os
import sys
import time

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(PLUGIN)
sys.path.insert(0, PLUGIN)
from ste_planner.qwen_client import QwenClient
from ste_planner.qwen_assistant import PlanningAssistant
from ste_planner.requirements import read_docx_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=os.path.join(PLUGIN, "reports", "qwen_validation.json"))
    parser.add_argument("--case-number", choices=[str(i) for i in range(1, 8)])
    args = parser.parse_args()
    client = QwenClient.from_env(os.path.join(ROOT, ".env"))
    report = {"model": client.model, "validation_type": "live_timeweb_tools_offline_core",
              "physical_print_tests": False, "fitted_causal_models_registered": 0, "cases": []}
    for directory in sorted(glob.glob(os.path.join(ROOT, "data", "examples", "*"))):
        if args.case_number and os.path.basename(directory).split(".")[0] != args.case_number:
            continue
        tzs = glob.glob(os.path.join(directory, "*.docx"))
        meshes = [p for p in glob.glob(os.path.join(directory, "*")) if p.lower().endswith(".stl")]
        if not tzs or not meshes:
            continue
        started = time.time()
        result = PlanningAssistant(client).run(read_docx_text(tzs[0]), meshes[0],
                                                {"source": os.path.basename(tzs[0])})
        decision = result["decision"]
        expected_flow = "propose_requirements" in result["tools_used"] and "plan_print" in result["tools_used"]
        row = {"case": os.path.basename(directory), "decision_status": decision["status"],
               "route": decision["route"]["mode"], "material": (decision.get("chosen_material") or {}).get("id"),
               "tools_used": result["tools_used"], "llm_error": result["llm_error"],
               "patch_errors": decision["setting_patch"]["errors"],
               "verified": expected_flow and not result["llm_error"] and not decision["setting_patch"]["errors"],
               "llm_merge": next((t for t in decision["trace"] if t["step"] == "merge_llm_requirements"), None),
               "seconds": round(time.time() - started, 2)}
        report["cases"].append(row)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        print("case %s: verified=%s status=%s" % (row["case"].split(".")[0], row["verified"], row["decision_status"]), flush=True)
    return 0 if len(report["cases"]) == (1 if args.case_number else 7) and all(r["verified"] for r in report["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
