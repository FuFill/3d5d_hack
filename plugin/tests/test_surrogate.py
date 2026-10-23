# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Response surface and Gaussian process on synthetic data with known answers."""

import itertools

import numpy

from testutil import assert_close

from ste_planner import surrogate as SG, optimize as O
from ste_planner.result import PASS, FAIL, UNKNOWN


def _truth(t, h):
    # synthetic response in physical units: temperature 220..260, layer 0.1..0.3
    zt, zh = (t - 240.0) / 20.0, (h - 0.2) / 0.1
    return 10.0 + 2.0 * zt - 1.5 * zh + 0.8 * zt ** 2 + 0.3 * zh ** 2 - 0.5 * zt * zh


def test_response_surface_recovers_coefficients():
    pts = list(itertools.product([220.0, 240.0, 260.0], [0.1, 0.2, 0.3]))
    x = numpy.array(pts + pts[:3])             # repeats
    y = numpy.array([_truth(t, h) for t, h in x])
    m = SG.fit_response_surface(x, y)
    assert m.ok
    coef = m.value.coef
    for got, want in zip(coef, [10.0, 2.0, -1.5, 0.8, 0.3, -0.5]):
        assert_close(got, want, abs_tol=1e-9)
    assert_close(float(m.value.predict([[250.0, 0.25]])[0]), _truth(250.0, 0.25), abs_tol=1e-9)
    assert O.domain_check({"x0": 250.0, "x1": 0.25}, m.value.domain()) == PASS
    assert O.domain_check({"x0": 280.0, "x1": 0.25}, m.value.domain()) == FAIL   # no extrapolation


def test_response_surface_refuses_small_designs():
    x = numpy.array([[220.0, 0.1], [260.0, 0.3], [240.0, 0.2]])
    assert SG.fit_response_surface(x, [1, 2, 3]).status == "needs_calibration"


def test_gaussian_process_interpolates_and_reports_uncertainty():
    x = numpy.array([[0.0], [1.0], [2.0], [3.0]])
    y = numpy.sin(x[:, 0])
    gp = SG.fit_gaussian_process(x, y, length_scale=1.0, signal_var=1.0, noise_var=1e-8).value
    mu, sd = gp.predict(x)
    assert numpy.allclose(mu, y, atol=1e-4) and numpy.all(sd < 1e-3)
    mu_far, sd_far = gp.predict([[10.0]])
    assert sd_far[0] > 0.9                      # far from data: prior uncertainty
    assert SG.fit_gaussian_process(x, y, None, 1.0, 0.1).status == "needs_calibration"


def test_probabilistic_constraint_and_acquisition():
    assert SG.probabilistic_constraint(-1.0, 0.3, 2.0) == PASS
    assert SG.probabilistic_constraint(-0.2, 0.3, 2.0) == FAIL
    assert SG.probabilistic_constraint(None, 0.3, 2.0) == UNKNOWN
    a_safe = SG.constrained_expected_improvement(1.0, 0.5, 1.2, -2.0, 0.5)
    a_risky = SG.constrained_expected_improvement(1.0, 0.5, 1.2, 2.0, 0.5)
    assert a_safe > a_risky > 0.0
