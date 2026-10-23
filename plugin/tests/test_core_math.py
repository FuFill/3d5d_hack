# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Result contract, units, process formulas (section 16 examples, section 17 properties), mechanics."""

import math

from testutil import assert_close, raises

from ste_planner import result, units, process, mechanics
from ste_planner.result import PASS, FAIL, UNKNOWN


# ---------------------------------------------------------------- three-valued logic

def test_tri_compare_scalar_and_interval():
    assert result.tri_compare(5, ">=", 4) == PASS
    assert result.tri_compare(3, ">=", 4) == FAIL
    assert result.tri_compare((3, 5), ">=", 4) == UNKNOWN      # straddles the limit
    assert result.tri_compare((4.5, 5), ">=", 4) == PASS
    assert result.tri_compare((1, 2), "<=", 4) == PASS
    assert result.tri_compare((3, 6), "<=", 4) == UNKNOWN
    assert result.tri_compare((10, 70), "in", (-40, 70)) == PASS
    assert result.tri_compare((75, 85), "in", (-40, 70)) == FAIL
    assert result.tri_compare((60, 80), "in", (-40, 70)) == UNKNOWN


def test_unknown_never_becomes_pass():
    assert result.tri_compare(None, ">=", 4) == UNKNOWN
    assert result.tri_compare(float("nan"), ">=", 4) == UNKNOWN
    assert result.tri_and([PASS, UNKNOWN]) == UNKNOWN
    assert result.tri_and([PASS, UNKNOWN, FAIL]) == FAIL
    assert result.tri_and([]) == UNKNOWN
    assert result.tri_or([UNKNOWN, FAIL]) == UNKNOWN


def test_constraint_margins():
    assert result.constraint_margin(3.0, "<=", 4.0) == -1.0
    assert result.constraint_margin(5.0, ">=", 4.0) == -1.0
    assert result.constraint_margin(None, "<=", 4.0) is None
    assert_close(result.tolerance_margin(10.12, 10.0, 0.1), 0.02)


def test_deterministic_json():
    a = result.to_json({"b": 1, "a": [1.5, {"z": 2, "y": 3}]})
    b = result.to_json({"a": [1.5, {"y": 3, "z": 2}], "b": 1})
    assert a == b


# ---------------------------------------------------------------- units

def test_unit_conversions():
    assert_close(units.convert(75, "N*m", "N*mm"), 75000.0)
    assert_close(units.convert(1.2, "g/cm3", "g/mm3"), 0.0012)
    assert_close(units.convert(16, "bar", "MPa"), 1.6)
    assert_close(units.convert(60, "rpm", "rad/s"), 2 * math.pi)
    assert_close(units.convert(98, "%", "fraction"), 0.98)
    assert_close(units.convert(2.2, "m/s", "mm/s"), 2200.0)
    assert_close(units.convert(25, "degC", "K"), 298.15)
    assert_close(units.mass_to_weight(18000.0), 18 * 9.80665)


def test_incompatible_units_raise():
    raises(units.UnitError, units.convert, 1, "N*m", "MPa")
    raises(units.UnitError, units.convert, 1, "furlong", "mm")


def test_unit_tokens():
    assert units.match_unit(" Н·м")[0] == "N*m"
    assert units.match_unit("Нм")[0] == "N*m"
    assert units.match_unit(" г/см³")[0] == "g/cm3"
    assert units.match_unit(" об/мин")[0] == "rpm"
    assert units.match_unit(" МПа")[0] == "MPa"
    assert units.match_unit("°C")[0] == "degC"
    assert units.match_unit(" г·мм")[0] == "g*mm"
    assert units.match_unit(" мм")[0] == "mm"


# ---------------------------------------------------------------- section 16: numerical examples

def test_example_flow_limit():
    """w=0.45, h=0.20, k=1.00, Q=8 mm3/s -> v <= 88.89 mm/s; k=1.05 -> 84.66 mm/s."""
    a = process.bead_area(0.45, 0.20).value
    assert_close(process.speed_limit_from_flow(8.0, 1.00, a).value, 88.888888, rel=1e-6)
    assert_close(process.speed_limit_from_flow(8.0, 1.05, a).value, 84.656084, rel=1e-6)
    assert_close(process.bead_area(0.45, 0.20, "stadium").value, 0.081416, rel=1e-5)


def test_example_walls():
    """shell >= 1.50 mm, w0=0.42, wi=0.45 -> N = 4, thickness 1.77 mm; 3 walls give 1.32 mm."""
    r = process.wall_count(1.50, 0.42, 0.45)
    assert r.value["count"] == 4
    assert_close(r.value["thickness"], 1.77)
    three = 0.42 + 2 * 0.45
    assert three < 1.50
    assert process.wall_count(1.2, 0.4).value["count"] == 3   # equal widths: ceil(t/w)
    assert process.wall_count(0.0, 0.4, allow_zero=True).value["count"] == 0


def test_example_tolerance():
    """Tolerance +-0.10 with measured process error bound +-0.16 is not achievable."""
    assert process.process_error_check(0.10, 0.16) == FAIL
    assert process.process_error_check(0.10, 0.08) == PASS
    b = process.tolerance_budget(0.10, {"layer": 0.02, "shrink": 0.05, "machine": 0.04})
    assert b.status == "infeasible" and b.value["remaining"] < 0
    u = process.tolerance_budget(0.10, {"layer": 0.02, "shrink": None})
    assert u.status == UNKNOWN


# ---------------------------------------------------------------- section 17 properties

def test_speed_limit_monotonic():
    a = process.bead_area(0.4, 0.2).value
    vs = [process.speed_limit_from_flow(q, 1.0, a).value for q in (4, 6, 8, 10)]
    assert all(x < y for x, y in zip(vs, vs[1:]))           # grows with Q
    areas = [process.bead_area(0.4, h).value for h in (0.1, 0.2, 0.3)]
    vs = [process.speed_limit_from_flow(8, 1.0, ar).value for ar in areas]
    assert all(x > y for x, y in zip(vs, vs[1:]))           # falls with A
    assert process.speed_limit_from_flow(None, 1.0, a).status == "needs_calibration"


def test_compensation_sign():
    d = process.offset_diameter_change(0.05).value
    assert d["outer"] > 0 and d["hole"] < 0
    plan = process.xy_offset_plan(outer_error=+0.10)          # outer printed too big
    assert plan.value["xy_offset"] < 0
    plan = process.xy_offset_plan(hole_error=-0.10)           # hole printed too small
    assert plan.value["xy_offset"] < 0
    both = process.xy_offset_plan(outer_error=+0.10, hole_error=+0.10)
    assert both.status == "unsupported"                       # one global offset cannot fix both
    assert_close(process.compensate_dimension(20.0, 0.99, 0.05).value, (20.0 - 0.05) / 0.99)
    assert process.compensate_dimension(20.0, None, None).status == "needs_calibration"


def test_layer_choice_never_clamps_silently():
    r = process.choose_layer_height(0.05, [0.1, 0.15, 0.2])
    assert r.status == "infeasible"
    assert process.choose_layer_height(0.17, [0.1, 0.15, 0.2]).value == 0.15
    hmax = process.max_layer_height_for_step(0.05, [(0, 0.6, 0.8)])
    assert_close(hmax.value, 0.05 / 0.8)


def test_skin_and_motion():
    assert process.skin_layers(1.0, 0.2).value["count"] == 5
    assert process.skin_layers(0.6, layer_heights=[0.3, 0.2, 0.1, 0.1]).value["count"] == 3
    l_acc = 100.0 ** 2 / 1000.0
    t1 = process.move_time(l_acc, 100.0, 1000.0)
    assert t1.domain["profile"] == "triangular"
    assert_close(t1.value, 2 * math.sqrt(l_acc / 1000.0))
    t2 = process.move_time(50.0, 100.0, 1000.0)
    assert_close(t2.value, 50.0 / 100.0 + 100.0 / 1000.0)
    # continuity at L_acc: both formulas agree
    assert_close(2 * math.sqrt(l_acc / 1000.0), l_acc / 100.0 + 100.0 / 1000.0)


def test_role_speed_reports_unknown_limits():
    r = process.role_speed({"profile": 60, "flow": None, "quality": 45})
    assert r.value == 45 and r.domain["governing"] == "quality" and r.domain["unknown_limits"] == ["flow"]


def test_mass_estimate_excludes_supports_from_part_mass():
    m = process.mass_estimate(0.00125, 1000, 0.2, 5000, v_support=400).value
    assert_close(m["part_g"], 0.00125 * (1000 + 1000))
    assert_close(m["material_g"], 0.00125 * (1000 + 1000 + 400))


# ---------------------------------------------------------------- mechanics

def test_mechanics_formulas():
    assert_close(mechanics.tangential_force(75000.0, 15.0).value, 5000.0)
    j = mechanics.polar_moment_hollow(20.0, 10.0).value
    assert_close(j, math.pi * (20 ** 4 - 10 ** 4) / 32)
    t = mechanics.shaft_torsion(18000.0, 20.0, 0.0, 50.0, 1000.0).value
    assert_close(t["tau_max"], 16 * 18000.0 / (math.pi * 20.0 ** 3))
    assert_close(t["phi_rad"], 18000.0 * 50.0 / (1000.0 * math.pi * 20.0 ** 4 / 32))
    assert_close(mechanics.cantilever_deflection(10.0, 50.0, 2000.0, 100.0).value, 10 * 50 ** 3 / (3 * 2000 * 100))
    assert_close(mechanics.euler_buckling(2000.0, 100.0, 50.0).value, math.pi ** 2 * 2000 * 100 / 2500)
    assert_close(mechanics.hoop_stress(1.6, 30.0, 2.0).value, 24.0)
    assert_close(mechanics.thin_wall_torsion(1000.0, 100.0, 2.0).value, 2.5)


def test_balancing_relations():
    per = mechanics.permissible_unbalance(2.5, 0.175, 2900.0).value
    assert_close(per["U_per_gmm"], 9549.296586 * 2.5 * 0.175 / 2900.0, rel=1e-6)
    assert_close(per["e_per_mm"], 2.5 / (2 * math.pi * 2900 / 60.0))
    f = mechanics.unbalance_force(100.0, 1.0, 3000.0).value
    assert_close(f, 0.1 * 0.001 * (2 * math.pi * 50) ** 2)
    u = mechanics.unbalance_vector([10, 10], [[5, 0, 0], [-5, 0, 3]]).value
    assert_close(u["magnitude"], 0.0, abs_tol=1e-12)


def test_rotating_ring_stress_units():
    # 1200 kg/m3 at 24 m/s -> 0.6912 MPa
    assert_close(mechanics.rotating_ring_stress(0.0012, 24000.0).value, 0.6912, rel=1e-9)


def test_missing_coefficients_give_unknown():
    assert mechanics.lewis_bending_stress(100, 10, 1, None, 1).status == UNKNOWN
    assert mechanics.sn_cycles_to_failure(20, 80, None).status == UNKNOWN
    assert mechanics.cellular_property(2000, 0.3).status == UNKNOWN
    assert mechanics.cellular_property(2000, 0.0).value == 0.0
    assert mechanics.archard_wear_volume(None, 10, 100, 50).status == UNKNOWN
    assert mechanics.traction_check(10, None, 100) == UNKNOWN
    assert mechanics.traction_check(10, 0.3, 100) == PASS
    assert mechanics.creep_strain_linear(None, 5).status == UNKNOWN
    assert mechanics.neo_hookean_energy(None, 100, [[1, 0, 0], [0, 1, 0], [0, 0, 1]]).status == UNKNOWN
    assert mechanics.tangential_force(None, 10).status == UNKNOWN
    assert mechanics.tangential_force(-5, 10).status == "invalid_input"


def test_failure_index_and_miner():
    fi = mechanics.max_stress_failure_index({"s1": 20, "s3": -10, "t13": 5},
                                            {"Xt": 40, "Zc": 50, "S13": 10}).value
    assert_close(fi["FI"], 0.5)
    assert fi["governing"] in ("s1+", "t13")
    assert mechanics.max_stress_failure_index({"s3": 5}, {"Xt": 40}).status == UNKNOWN
    assert mechanics.safety_check(0.4, 2.0) == PASS and mechanics.safety_check(0.6, 2.0) == FAIL
    assert mechanics.safety_check(None, 2.0) == UNKNOWN
    assert_close(mechanics.miner_damage([(1e5, 1e6), (5e4, 1e5)]).value, 0.6)
    assert mechanics.miner_damage([(1e5, None)]).status == UNKNOWN
    assert_close(mechanics.rotation_cycles(450, 8000).value, 450 * 60 * 8000)


def test_neo_hookean_identity_is_zero():
    w = mechanics.neo_hookean_energy(1.0, 100.0, [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
    assert_close(w.value, 0.0, abs_tol=1e-12)
