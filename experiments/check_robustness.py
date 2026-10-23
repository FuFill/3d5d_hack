"""User's +/-0.05 mm Z scenario; no assumed printer-noise distribution."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from ste_planner import robustness


def main():
    baseline = json.loads((ROOT / "experiments/ste_runtime/final/inputs/baseline.json").read_text(encoding="utf-8"))
    selected = json.loads((ROOT / "experiments/ste_runtime/final/inputs/live_demo.json").read_text(encoding="utf-8"))
    value = selected["functional_analysis"]["value"]
    candidates = [{"id": "baseline", "settings": baseline["effective_settings"], "mechanics": value["baseline"]["mechanics"]["cases"][0]},
                  {"id": "selected", "settings": selected["effective_settings"], "mechanics": value["selected"]["mechanics"]["cases"][0]}]
    noise = {"source": "user_scenario", "bounds": {"z_offset_mm": [-.05, .05]}}
    result = robustness.choose_stable_candidate(candidates, noise, {})
    result["note"] = "No measured noise bounds/tolerances or settings-to-print-quality response supplied; no robust optimum can be certified"
    (ROOT / "experiments/robustness_results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert result["conditional_selected_id"] is None
    print("Z scenario: first layer 0.25..0.35 mm; no physical stability/robust optimum asserted")


if __name__ == "__main__":
    main()
