"""Independent fixed-seed grouped holdout, not physical material evidence.

No benchmark cases tune nuisance models or candidate objectives. A final group
holdout is excluded from fitting, scaling and coefficient estimation. Reports
orthogonal holdout moments and known simulation effects, never train R2 as
physical predictive accuracy. No raw observations or group identifiers saved.
"""
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from ste_planner import surrogate


def main():
    cases = []
    for seed in (104729, 130363, 155921):
        rng = np.random.RandomState(seed)
        groups = np.repeat(np.arange(250), 4)
        xg = rng.normal(size=(250, 2))
        x = np.repeat(xg, 4, axis=0)
        d = x[:, 0] + x[:, 1] ** 2 + np.repeat(rng.normal(size=250), 4)
        y = 2 * d + 3 * x[:, 0] + x[:, 1] ** 2 + np.repeat(rng.normal(scale=.5, size=250), 4)
        held_out = rng.permutation(250)[:50]
        test = np.isin(groups, held_out)
        train = ~test
        fit = surrogate.fit_double_ml(x[train], d[train], y[train], groups[train], n_folds=5, seed=17)
        assert fit.ok
        nuisance = surrogate._dml_predict(x[train], np.column_stack((y[train], d[train])), x[test])
        ry, rd = y[test] - nuisance[:, 0], d[test] - nuisance[:, 1]
        theta = fit.value["coefficient"]
        moment = rd * (ry - theta * rd)
        # Effective independent N is print jobs, not four coupons per job.
        score = np.array([moment[groups[test] == group].mean() for group in held_out])
        se = float(score.std(ddof=1) / np.sqrt(len(score)))
        interval = fit.value["confidence_interval_95"]
        row = {"seed": seed, "source": "known_effect_simulation", "known_effect": 2.,
               "estimated_effect": theta, "absolute_effect_error": abs(theta - 2.),
               "fit_ci95": interval, "known_effect_in_ci95": interval[0] <= 2 <= interval[1],
               "outer_train_groups": 200, "outer_test_groups": 50, "outer_group_overlap": 0,
               "holdout_score_mean": float(score.mean()), "holdout_score_standard_error": se,
               "holdout_score_z": float(score.mean()) / se,
               "split_sha256": hashlib.sha256(test.astype(np.uint8).tobytes()).hexdigest(),
               "cross_fitting": fit.value["diagnostics"]["cross_fitting"]}
        assert not set(groups[train]).intersection(groups[test])
        assert abs(theta - 2) < .15
        assert abs(row["holdout_score_z"]) < 4
        cases.append(row)
    report = {"status": "pass", "physical_test_datasets_available": 0,
              "physical_generalization_accuracy": None,
              "evaluation_protocol": {"outer_holdout_fraction": .2, "split_unit": "print_job",
                  "preprocessing": "train fold only", "hyperparameter_tuning": False,
                  "benchmark_used_for_model_selection": False,
                  "controls": "pre-treatment simulated X only; no D/Y/derived outcomes",
                  "case_seeds_frozen_before_evaluation": [104729, 130363, 155921]},
              "cases": cases,
              "limitations": ["simulation is not physical validation", "group identity must reflect actual dependent prints",
                  "unmeasured confounding and post-treatment controls require experimental design review",
                  "no finite benchmark proves absence of every possible leakage path"]}
    (ROOT / "experiments/leakage_results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "effect_errors": [c["absolute_effect_error"] for c in cases],
                      "outer_group_overlap": [c["outer_group_overlap"] for c in cases]}))


if __name__ == "__main__":
    main()
