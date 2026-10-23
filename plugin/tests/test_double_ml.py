# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Known-effect simulations, clustered observations and failure contracts."""

import numpy

from ste_planner import surrogate as SG
from testutil import assert_close


def _sample():
    rng = numpy.random.RandomState(17)
    x = rng.normal(size=(1000, 2))
    d = x[:, 0] + x[:, 1] ** 2 + rng.normal(size=len(x))
    y = 2.0 * d + 3.0 * x[:, 0] + x[:, 1] ** 2 + rng.normal(size=len(x))
    return x, d, y


def test_dml_recovers_known_effect_and_removes_confounding():
    x, d, y = _sample()
    fit = SG.fit_double_ml(x, d, y, outcome_unit="N")
    assert fit.ok
    theta, se = fit.value["coefficient"], fit.value["standard_error"]
    naive = numpy.linalg.lstsq(numpy.column_stack((numpy.ones(len(d)), d)), y, rcond=None)[0][1]
    assert abs(theta - 2.0) < 4.0 * se
    assert abs(theta - 2.0) < abs(naive - 2.0)
    assert fit.evidence is None                 # inference is not qualification
    assert fit.value["independent_groups"] == len(d)
    assert fit.value["diagnostics"]["residual_treatment_variance"] > 0
    assert_close(SG.fit_double_ml(x, d, y).value["coefficient"], theta)


def test_dml_groups_never_cross_training_and_validation():
    groups = numpy.repeat(numpy.arange(23), 4)
    tested = numpy.zeros(len(groups), dtype=int)
    for train, test in SG._dml_splits(groups, 5, 31):
        assert not set(groups[train]).intersection(groups[test])
        assert not set(train).intersection(test)
        tested[test] += 1
    assert numpy.all(tested == 1)


def test_dml_preprocessing_and_nuisance_fit_ignore_held_out_targets():
    x, d, y = _sample()
    groups = numpy.repeat(numpy.arange(len(d) // 4), 4)
    train, test = next(SG._dml_splits(groups, 5, 31))
    targets = numpy.column_stack((y, d))
    first = SG._dml_predict(x[train], targets[train], x[test])
    targets[test] += 1e9  # A held-out label cannot affect its nuisance prediction.
    second = SG._dml_predict(x[train], targets[train], x[test])
    assert numpy.array_equal(first, second)
    # Training-only min/max and column selection agree with an independent basis.
    lo, hi = x[train].min(axis=0), x[train].max(axis=0)
    a = SG._quad_design(2 * (x[train] - lo) / (hi - lo) - 1)
    b = SG._quad_design(2 * (x[test] - lo) / (hi - lo) - 1)
    independent = b.dot(numpy.linalg.lstsq(a, targets[train], rcond=None)[0])
    assert numpy.allclose(second, independent)
    fit = SG.fit_double_ml(x, d, y, groups)
    audit = fit.value["diagnostics"]["cross_fitting"]
    assert audit["test_coverage_exactly_once"]
    assert all(f["group_overlap"] == 0 for f in audit["folds"])


def test_dml_cluster_uncertainty_does_not_count_coupons_as_print_jobs():
    rng = numpy.random.RandomState(123)
    count, repeats = 120, 8
    groups = numpy.repeat(numpy.arange(count), repeats)
    d = numpy.repeat(rng.normal(size=count), repeats)
    y = 2.0 * d + numpy.repeat(rng.normal(size=count), repeats)
    x = numpy.empty((len(d), 0))
    grouped = SG.fit_double_ml(x, d, y, groups=groups)
    iid = SG.fit_double_ml(x, d, y)
    assert grouped.ok and iid.ok
    assert grouped.value["independent_groups"] == count
    assert grouped.value["standard_error"] > 2 * iid.value["standard_error"]


def test_dml_supports_binary_and_constant_controls():
    rng = numpy.random.RandomState(5)
    walls = rng.randint(0, 2, size=600)
    x = numpy.column_stack((walls, numpy.ones(len(walls))))
    d = 3.0 * walls + rng.normal(size=len(walls))
    y = 2.0 * d + 8.0 * walls + 0.1 * rng.normal(size=len(walls))
    fit = SG.fit_double_ml(x, d, y)
    assert fit.ok
    assert abs(fit.value["coefficient"] - 2) < 0.03


def test_dml_refuses_missing_or_unidentified_data():
    x, d, y = _sample()
    assert SG.fit_double_ml(x, d[:-1], y).status == "invalid_input"
    broken = y.copy()
    broken[2] = float("nan")
    assert SG.fit_double_ml(x, d, broken).status == "invalid_input"
    assert SG.fit_double_ml(x, d, y, n_folds=1).status == "invalid_input"
    assert SG.fit_double_ml(x, d, y, n_folds=2.5).status == "invalid_input"
    assert SG.fit_double_ml(x, d, y, seed=None).status == "invalid_input"
    assert SG.fit_double_ml(x, d, y, groups=[None] * len(d)).status == "invalid_input"
    assert SG.fit_double_ml(x, d, y, groups=numpy.zeros(len(d))).status == "needs_calibration"
    deterministic = 3.0 + x[:, 0] + x[:, 1] ** 2
    assert SG.fit_double_ml(x, deterministic, y).status == "needs_calibration"
    assert SG.fit_double_ml(numpy.empty((0, 1)), [], []).status == "needs_calibration"


def test_dml_contrasts_respect_units_sign_and_domain():
    x, d, y = _sample()
    fit = SG.fit_double_ml(x, d, y, outcome_unit="N")
    change = SG.double_ml_change(fit, 1.0, -1.0, [0.0, 0.0])
    assert change.ok and change.unit == "N" and change.evidence is None
    assert_close(change.value["expected_change"], -2 * fit.value["coefficient"])
    assert change.value["confidence_interval_95"][0] < change.value["confidence_interval_95"][1] < 0
    assert SG.double_ml_change(fit, 0, 100, [0, 0]).status == "out_of_domain"
    assert SG.double_ml_change(fit, 0, 1, [100, 0]).status == "out_of_domain"
    assert SG.double_ml_change(fit, 0, 1, [0]).status == "invalid_input"


def test_dml_unit_rescaling_and_outcome_offset_preserve_effect():
    x, d, y = _sample()
    original = SG.fit_double_ml(x, d, y)
    scaled = SG.fit_double_ml(x, 100 * d, y + 1000)
    assert scaled.ok
    assert_close(scaled.value["coefficient"] * 100, original.value["coefficient"], abs_tol=1e-9)
    assert_close(scaled.value["standard_error"] * 100, original.value["standard_error"], abs_tol=1e-9)
