"""Independent oracle check; run in the modern requirements.lock.txt environment.

The production estimator imports only NumPy. Official DoubleML and sklearn are
used here exclusively to verify its PLR coefficient and standard error.
"""

import importlib.metadata
import json
from pathlib import Path
import sys

import numpy as np
from doubleml import DoubleMLData, DoubleMLPLR
from sklearn.linear_model import LinearRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from ste_planner.surrogate import fit_double_ml, _dml_splits


def compare(clustered):
    rng = np.random.RandomState(17)
    n_groups, repeats = (200, 3) if clustered else (600, 1)
    x0 = rng.normal(size=(n_groups, 2))
    d0 = x0[:, 0] + x0[:, 1] ** 2 + rng.normal(size=n_groups)
    y0 = 2.0 * d0 + 3.0 * x0[:, 0] + x0[:, 1] ** 2 + rng.normal(size=n_groups)
    x, d, y = (np.repeat(a, repeats, axis=0) for a in (x0, d0, y0))
    groups = np.repeat(np.arange(n_groups), repeats)
    native = fit_double_ml(x, d, y, groups=groups, seed=0)
    if not native.ok:
        raise AssertionError(native.to_dict())
    data = (DoubleMLData.from_arrays(x, y, d, cluster_vars=groups) if clustered
            else DoubleMLData.from_arrays(x, y, d))
    learner = make_pipeline(PolynomialFeatures(degree=2, include_bias=False), LinearRegression())
    reference = DoubleMLPLR(data, learner, learner, n_folds=5, n_rep=1,
                           draw_sample_splitting=False, score="partialling out")
    splits = list(_dml_splits(groups, 5, 0))
    cluster_splits = ([[([np.unique(groups[train])], [np.unique(groups[test])])
                       for train, test in splits]] if clustered else None)
    reference.set_sample_splitting(splits, all_smpls_cluster=cluster_splits)
    reference.fit(n_jobs_cv=1)
    theta, se = float(reference.coef[0]), float(reference.se[0])
    # The runtime reports the documented finite-cluster G/(G-1) correction;
    # DoubleML's asymptotic variance does not include that correction.
    corrected_se = se * np.sqrt(n_groups / (n_groups - 1.0))
    np.testing.assert_allclose(native.value["coefficient"], theta, rtol=1e-9, atol=1e-10)
    np.testing.assert_allclose(native.value["standard_error"], corrected_se, rtol=1e-9, atol=1e-10)
    return {"groups": n_groups, "observations": len(d), "native_coefficient": native.value["coefficient"],
            "doubleml_coefficient": theta, "absolute_coefficient_difference": abs(theta - native.value["coefficient"]),
            "native_standard_error": native.value["standard_error"],
            "doubleml_standard_error_with_finite_group_correction": corrected_se, "passed": True}


def main():
    results = {"data_source": "SYNTHETIC_ORACLE_CHECK_ONLY", "iid": compare(False), "clustered": compare(True),
               "versions": {name: importlib.metadata.version(name)
                            for name in ("numpy", "scikit-learn", "doubleml")}}
    Path(__file__).with_name("planner_double_ml_crosscheck.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
