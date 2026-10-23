# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Experimental response models (specification section 10).

- Quadratic response surface y = b0 + sum bi zi + sum bii zi^2 + sum bik zi zk on
  normalised factors z in [-1, 1] (DOE + RSM); coefficients come from data only.
- Gaussian process with an RBF kernel: mu* = k*^T (K + S)^-1 y,
  s*^2 = k** - k*^T (K + S)^-1 k*; hyperparameters are inputs estimated from data.
- Probabilistic constraint mu_g + kappa sigma_g <= 0 and constrained acquisition
  EI(x) * P(feasible(x)) for choosing the next coupon (never a safety proof).
Every prediction carries the training domain; outside it domain_check() fails.
"""

import math

import numpy

from .result import computed, unknown, MODEL_EXPERIMENTAL, PASS, FAIL, UNKNOWN


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
