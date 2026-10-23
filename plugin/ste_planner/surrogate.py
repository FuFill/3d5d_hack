# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Experimental response models (specification section 10).

- Quadratic response surface y = b0 + sum bi zi + sum bii zi^2 + sum bik zi zk on
  normalised factors z in [-1, 1] (DOE + RSM); coefficients come from data only.
- Gaussian process with an RBF kernel: mu* = k*^T (K + S)^-1 y,
  s*^2 = k** - k*^T (K + S)^-1 k*; hyperparameters are inputs estimated from data.
- Probabilistic constraint mu_g + kappa sigma_g <= 0 and constrained acquisition
  EI(x) * P(feasible(x)) for choosing the next coupon (never a safety proof).
- Double ML PLR with group cross-fitting and cluster-robust uncertainty;
  a change estimate does not certify the absolute outcome of a printed part.
Every prediction carries the training domain; outside it domain_check() fails.
"""

import math

import numpy

from .result import computed, unknown, invalid, MODEL_EXPERIMENTAL, PASS, FAIL, UNKNOWN


def _normalise(x, lo, hi):
    return 2.0 * (numpy.asarray(x, dtype=float) - lo) / (hi - lo) - 1.0


def _quad_design(z):
    z = numpy.atleast_2d(z)
    n, d = z.shape
    cols = [numpy.ones(n)]
    cols += [z[:, i] for i in range(d)]
    cols += [z[:, i] ** 2 for i in range(d)]
    cols += [z[:, i] * z[:, k] for i in range(d) for k in range(i + 1, d)]
    return numpy.stack(cols, axis=1)


class ResponseSurface(object):
    def __init__(self, coef, lo, hi, residual_std, dof):
        self.coef, self.lo, self.hi = coef, lo, hi
        self.residual_std, self.dof = residual_std, dof

    def predict(self, x):
        return _quad_design(_normalise(x, self.lo, self.hi)).dot(self.coef)

    def domain(self):
        return dict(("x%d" % i, (float(a), float(b))) for i, (a, b) in enumerate(zip(self.lo, self.hi)))


def fit_response_surface(x, y):
    """Least-squares quadratic model; needs more runs than coefficients (repeats estimate noise)."""
    x = numpy.atleast_2d(numpy.asarray(x, dtype=float))
    y = numpy.asarray(y, dtype=float)
    lo, hi = x.min(axis=0), x.max(axis=0)
    if numpy.any(hi - lo <= 0):
        return unknown("fit_response_surface", "every factor needs at least two levels")
    a = _quad_design(_normalise(x, lo, hi))
    if len(y) <= a.shape[1]:
        return unknown("fit_response_surface", "need more runs (%d) than coefficients (%d)" % (len(y), a.shape[1]),
                       status="needs_calibration")
    coef, _, rank, _ = numpy.linalg.lstsq(a, y, rcond=None)
    if rank < a.shape[1]:
        return unknown("fit_response_surface", "design is rank deficient")
    res = y - a.dot(coef)
    dof = len(y) - a.shape[1]
    std = float(math.sqrt(float(res.dot(res)) / dof))
    model = ResponseSurface(coef, lo, hi, std, dof)
    return computed(model, None, "fit_response_surface", MODEL_EXPERIMENTAL,
                    inputs={"runs": len(y), "coefficients": a.shape[1]},
                    note="training error is not prediction error; validate on independent coupons")


class GaussianProcess(object):
    def __init__(self, x, y, length_scale, signal_var, noise_var):
        self.x = numpy.atleast_2d(numpy.asarray(x, dtype=float))
        self.y = numpy.asarray(y, dtype=float)
        self.ls = numpy.asarray(length_scale, dtype=float) * numpy.ones(self.x.shape[1])
        self.sv, self.nv = float(signal_var), float(noise_var)
        k = self._k(self.x, self.x) + self.nv * numpy.eye(len(self.y))
        self._l = numpy.linalg.cholesky(k)
        self._alpha = numpy.linalg.solve(self._l.T, numpy.linalg.solve(self._l, self.y))
        self.lo, self.hi = self.x.min(axis=0), self.x.max(axis=0)

    def _k(self, a, b):
        d = (a[:, None, :] - b[None, :, :]) / self.ls
        return self.sv * numpy.exp(-0.5 * (d ** 2).sum(axis=2))

    def predict(self, xs):
        xs = numpy.atleast_2d(numpy.asarray(xs, dtype=float))
        ks = self._k(xs, self.x)
        mu = ks.dot(self._alpha)
        v = numpy.linalg.solve(self._l, ks.T)
        var = numpy.maximum(self.sv - (v ** 2).sum(axis=0), 0.0)
        return mu, numpy.sqrt(var)

    def domain(self):
        return dict(("x%d" % i, (float(a), float(b))) for i, (a, b) in enumerate(zip(self.lo, self.hi)))


def fit_gaussian_process(x, y, length_scale, signal_var, noise_var):
    """GP with given (data-estimated) hyperparameters; they are never invented by the planner."""
    if any(v is None for v in (length_scale, signal_var, noise_var)):
        return unknown("fit_gaussian_process", "hyperparameters must be estimated from data",
                       ["length_scale", "signal_var", "noise_var"], status="needs_calibration")
    if len(y) < 2:
        return unknown("fit_gaussian_process", "at least two observations required")
    return computed(GaussianProcess(x, y, length_scale, signal_var, noise_var), None, "fit_gaussian_process",
                    MODEL_EXPERIMENTAL)


def _dml_splits(groups, n_folds, seed):
    """Keep all observations from a print job together, including in nuisance fitting."""
    unique = numpy.unique(groups)
    shuffled = numpy.random.RandomState(seed).permutation(unique)
    for held_out in numpy.array_split(shuffled, n_folds):
        test = numpy.isin(groups, held_out)
        yield numpy.flatnonzero(~test), numpy.flatnonzero(test)


def _dml_predict(x_train, targets, x_test):
    # Series regression reuses the RSM basis. Constant columns and redundant
    # polynomial terms (e.g. binary controls) need no separately identified coef.
    varying = numpy.ptp(x_train, axis=0) > 0
    train, test = x_train[:, varying], x_test[:, varying]
    if train.shape[1]:
        lo, hi = train.min(axis=0), train.max(axis=0)
        train = _normalise(train, lo, hi)
        test = _normalise(test, lo, hi)
    design, held_out = _quad_design(train), _quad_design(test)
    coef, _, rank, _ = numpy.linalg.lstsq(design, targets, rcond=None)
    if len(train) <= rank:
        raise ValueError("nuisance fit has no residual degrees of freedom")
    return held_out.dot(coef)


def fit_double_ml(x, d, y, groups=None, n_folds=5, seed=0, outcome_unit=None):
    """Cross-fitted PLR partialling-out with quadratic nuisance regressions.

    D is in the caller's declared units (percent means percentage points).
    Cluster-robust asymptotic SE uses print jobs as independent groups. This
    statistical fit does not establish causal identification or qualify a part.
    No sklearn/DoubleML import is needed in the Python 3.5 slicer runtime.
    """
    model_id = "fit_double_ml"
    try:
        x, d, y = (numpy.asarray(v, dtype=float) for v in (x, d, y))
        if x.ndim != 2 or d.ndim != 1 or y.ndim != 1 or not (len(x) == len(d) == len(y)):
            return invalid(model_id, "x must be (n,p), d and y must be (n,)")
        if not all(numpy.isfinite(v).all() for v in (x, d, y)):
            return invalid(model_id, "observations must be finite; missing results are not dropped")
        if isinstance(n_folds, bool) or int(n_folds) != n_folds or n_folds < 2:
            return invalid(model_id, "n_folds must be an integer >= 2")
        n_folds = int(n_folds)
        if isinstance(seed, bool) or not isinstance(seed, (int, numpy.integer)) or not 0 <= seed < 2 ** 32:
            return invalid(model_id, "seed must be an integer in [0, 2**32)")
        seed = int(seed)
        if len(d) < 3:
            return unknown(model_id, "at least three observations required", status="needs_calibration")
        if groups is None:
            groups = numpy.arange(len(d))
        else:
            groups = numpy.asarray(groups)
            if groups.ndim != 1 or len(groups) != len(d):
                return invalid(model_id, "one print-job group is required per observation")
            if any(v is None for v in groups) or (groups.dtype.kind in "fc" and
                                                  not numpy.isfinite(groups).all()):
                return invalid(model_id, "missing/non-finite print-job identifiers")
        group_ids, group_index = numpy.unique(groups, return_inverse=True)
        if len(group_ids) < n_folds:
            return unknown(model_id, "fewer independent print jobs than folds", status="needs_calibration")
        residual_y, residual_d = numpy.empty(len(d)), numpy.empty(len(d))
        for train, test in _dml_splits(groups, n_folds, seed):
            try:
                predicted = _dml_predict(x[train], numpy.column_stack((y[train], d[train])), x[test])
            except ValueError as exc:
                return unknown(model_id, str(exc), status="needs_calibration")
            residual_y[test] = y[test] - predicted[:, 0]
            residual_d[test] = d[test] - predicted[:, 1]
        denominator = float(residual_d.dot(residual_d))
        numerical_floor = numpy.finfo(float).eps * max(float(d.dot(d)), 1.0)
        if not math.isfinite(denominator) or denominator <= numerical_floor:
            return unknown(model_id, "no residual treatment variation after conditioning on x",
                           status="needs_calibration")
        theta = float(residual_d.dot(residual_y) / denominator)
        score = residual_d * (residual_y - theta * residual_d)
        cluster_score = numpy.bincount(group_index, weights=score, minlength=len(group_ids))
        correction = len(group_ids) / float(len(group_ids) - 1)
        se = float(math.sqrt(correction * float(cluster_score.dot(cluster_score))) / denominator)
        if not all(math.isfinite(v) for v in (theta, se)):
            return invalid(model_id, "non-finite effect or uncertainty")
        interval = [theta - 1.959963984540054 * se, theta + 1.959963984540054 * se]
        domain = {"treatment": [float(d.min()), float(d.max())]}
        domain.update(("x%d" % i, [float(x[:, i].min()), float(x[:, i].max())])
                      for i in range(x.shape[1]))
        value = {"coefficient": theta, "standard_error": se, "confidence_interval_95": interval,
                 "outcome_unit": outcome_unit, "observations": len(d), "independent_groups": len(group_ids),
                 "n_folds": n_folds, "seed": seed, "covariate_count": x.shape[1],
                 "nuisance_model": "quadratic_series", "algorithm_version": 1,
                 "numpy_version": numpy.__version__, "diagnostics": {
                     "out_of_fold_rmse_y": float(numpy.sqrt(numpy.mean(residual_y ** 2))),
                     "out_of_fold_rmse_d": float(numpy.sqrt(numpy.mean(residual_d ** 2))),
                     "residual_treatment_variance": denominator / len(d)},
                 "inference": "cluster-robust asymptotic normal; not a prediction interval"}
        return computed(value, None, model_id, MODEL_EXPERIMENTAL, evidence=None, domain=domain,
                        uncertainty={"standard_error": se, "confidence_interval_95": interval},
                        note="requires adequate nuisance fits, conditional treatment variation and a justified "
                             "causal design; does not prove absolute strength or qualification")
    except (TypeError, ValueError, OverflowError, numpy.linalg.LinAlgError) as exc:
        return invalid(model_id, str(exc))


def double_ml_change(fitted, baseline, target, x):
    """Effect of a single setting change inside observed marginal bounds.

    These bounds are necessary checks, not proof of conditional overlap.
    """
    model_id = "double_ml_change"
    if not fitted.ok:
        return unknown(model_id, "DML model is unavailable", status=fitted.status)
    try:
        baseline, target = float(baseline), float(target)
        x = numpy.asarray(x, dtype=float)
        if x.shape != (fitted.value["covariate_count"],) or not numpy.isfinite(x).all():
            return invalid(model_id, "one finite value is required per covariate")
        values = {"treatment": baseline}
        values.update(("x%d" % i, float(v)) for i, v in enumerate(x))
        for key, value in values.items():
            lo, hi = fitted.domain[key]
            if not math.isfinite(value) or value < lo or value > hi:
                return unknown(model_id, "%s outside calibration domain" % key, status="out_of_domain")
        lo, hi = fitted.domain["treatment"]
        if not math.isfinite(target) or target < lo or target > hi:
            return unknown(model_id, "target outside calibration domain", status="out_of_domain")
        delta = target - baseline
        interval = sorted(delta * v for v in fitted.value["confidence_interval_95"])
        return computed({"baseline": baseline, "target": target,
                         "expected_change": delta * fitted.value["coefficient"],
                         "confidence_interval_95": interval}, fitted.value["outcome_unit"],
                        model_id, MODEL_EXPERIMENTAL, evidence=None,
                        uncertainty={"confidence_interval_95": interval}, domain=fitted.domain,
                        note="single-setting contrast at fixed context; no absolute outcome is predicted")
    except (TypeError, ValueError, OverflowError) as exc:
        return invalid(model_id, str(exc))


def probabilistic_constraint(mu, sigma, kappa):
    """mu_g + kappa sigma_g <= 0 (approximate; not a reliability guarantee)."""
    if mu is None or sigma is None or kappa is None:
        return UNKNOWN
    return PASS if mu + kappa * sigma <= 0 else FAIL


def _norm_pdf(z):
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def _norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def constrained_expected_improvement(mu_f, sigma_f, best_f, mu_g, sigma_g):
    """a(x) = EI(x) * P(g(x) <= 0) for minimisation of f; chooses the next experiment only."""
    if sigma_f <= 0:
        ei = max(best_f - mu_f, 0.0)
    else:
        z = (best_f - mu_f) / sigma_f
        ei = (best_f - mu_f) * _norm_cdf(z) + sigma_f * _norm_pdf(z)
    pf = _norm_cdf(-mu_g / sigma_g) if sigma_g > 0 else (1.0 if mu_g <= 0 else 0.0)
    return ei * pf
