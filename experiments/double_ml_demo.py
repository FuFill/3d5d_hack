"""Synthetic DoubleML experiment; does not estimate effects from the seven STL cases.

Run in a separate modern Python environment with requirements.lock.txt installed.
All variables and the true effect below are dimensionless simulated quantities.
"""

import importlib.metadata
import json
import os
from pathlib import Path
import platform

os.environ.setdefault("OMP_NUM_THREADS", "2")

import numpy as np
from doubleml import DoubleMLData, DoubleMLPLR
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler


def main():
    seed = 20261023
    rng = np.random.default_rng(seed)
    n_observations = 5000
    true_effect = 2.0
    x = rng.normal(size=(n_observations, 5))
    assignment = (
        0.8 * np.sin(x[:, 0])
        + 0.7 * x[:, 1] ** 2
        + 0.5 * x[:, 2]
        + 0.3 * x[:, 3] * x[:, 4]
    )
    background = (
        6.0 * np.sin(x[:, 0])
        + 4.0 * x[:, 1] ** 2
        + 2.0 * x[:, 2]
        + 1.5 * x[:, 3] * x[:, 4]
    )
    d = assignment + rng.normal(size=n_observations)
    y = true_effect * d + background + rng.normal(size=n_observations)

    # Confounded baseline: regress Y on D with an intercept, omitting X.
    baseline_design = np.column_stack((np.ones(n_observations), d))
    naive_effect = float(np.linalg.lstsq(baseline_design, y, rcond=None)[0][1])

    learners = {
        "polynomial_ridge": make_pipeline(
            PolynomialFeatures(degree=5, include_bias=False),
            StandardScaler(),
            Ridge(alpha=1.0),
        ),
        "hist_gradient_boosting": HistGradientBoostingRegressor(
            learning_rate=0.05,
            max_iter=200,
            max_leaf_nodes=15,
            min_samples_leaf=30,
            l2_regularization=1.0,
            random_state=seed,
        ),
    }
    cases = {}
    for name, learner in learners.items():
        # DoubleML uses NumPy's legacy RNG. Both cases receive identical folds.
        np.random.seed(seed)
        model = DoubleMLPLR(
            DoubleMLData.from_arrays(x, y, d),
            ml_l=learner,  # Estimates E[Y | X], not g(X).
            ml_m=learner,  # Estimates E[D | X]; DoubleML clones each estimator.
            n_folds=5,
            n_rep=3,
            score="partialling out",
        )
        model.fit(n_jobs_cv=1)

        # Every prediction must be out of fold, once per row per repetition.
        split_checks = []
        for folds in model.smpls:
            test_count = np.zeros(n_observations, dtype=int)
            disjoint = True
            for train, test in folds:
                disjoint = disjoint and not np.intersect1d(train, test).size
                test_count[test] += 1
            split_checks.append(bool(disjoint and np.all(test_count == 1)))

        estimate = float(model.coef[0])
        standard_error = float(model.se[0])
        interval = model.confint(level=0.95).iloc[0].to_numpy(dtype=float).tolist()
        cases[name] = {
            "effect": estimate,
            "standard_error": standard_error,
            "95_percent_interval": interval,
            "true_effect_inside_95_percent_interval": interval[0] <= true_effect <= interval[1],
            "recovers_known_effect_within_4_standard_errors": (
                abs(estimate - true_effect) <= 4.0 * standard_error
            ),
            "cross_fitting_has_no_train_test_overlap": all(split_checks),
            "nuisance_rmse": {
                key: float(np.mean(value)) for key, value in model.nuisance_loss.items()
            },
            "learner_specification": repr(learner),
        }
        print(name + ": " + json.dumps(cases[name]), flush=True)

    primary = cases["polynomial_ridge"]
    checks = {
        "cross_fitting_has_no_train_test_overlap": all(
            case["cross_fitting_has_no_train_test_overlap"] for case in cases.values()
        ),
        "primary_series_model_recovers_known_effect": primary[
            "recovers_known_effect_within_4_standard_errors"
        ],
        "less_absolute_error_than_unadjusted_baseline": (
            abs(primary["effect"] - true_effect) < abs(naive_effect - true_effect)
        ),
    }
    result = {
        "data_source": "SYNTHETIC_SIMULATION_ONLY",
        "real_printing_outcomes_used": 0,
        "physical_units": "none; all variables are dimensionless",
        "seed": seed,
        "n_observations": n_observations,
        "n_covariates": x.shape[1],
        "true_effect_assigned_by_generator": true_effect,
        "unadjusted_ols_effect": naive_effect,
        "double_ml_comparison": cases,
        "n_folds": model.n_folds,
        "n_rep": model.n_rep,
        "checks": checks,
        "versions": {
            "python": platform.python_version(),
            **{
                name: importlib.metadata.version(name)
                for name in ("doubleml", "scikit-learn", "numpy", "scipy", "pandas")
            },
        },
        "limits": [
            "One simulation is a procedural check, not an empirical coverage study.",
            "This does not estimate physical effects, recommend settings, or compare 3D and 5D.",
            "Observations are iid in this simulation; real print jobs need a cluster-aware design.",
            "Causal identification requires a justified design; cross-fitting cannot provide it.",
            "Learner comparison is exploratory and is not independent validation of model selection.",
        ],
    }
    output = Path(__file__).with_name("double_ml_results.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise SystemExit("Synthetic experiment check failed; inspect the saved results.")


if __name__ == "__main__":
    main()
