# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Requirement extraction: golden values from the seven TZ documents, provenance, LLM merge rules."""

import math

from testutil import assert_close, example_cases, skip, raises

from ste_planner import requirements as R
from ste_planner import units


def _sets():
    cases = example_cases()
    if len(cases) != 7:
        skip("seven example cases expected")
    return dict((name.split(".")[0], R.parse_requirements(R.read_docx_text(docx), source=docx))
                for name, docx, _ in cases)


def _one(rs, prop, **kw):
    found = [r for r in rs.requirements if r.property == prop and all(getattr(r, k) == v for k, v in kw.items())]
    assert found, "%s %s not found in %s" % (prop, kw, rs.part_name)
    return found[0]


def test_gear_requirements():
    rs = _sets()["1"]
    assert "шестерня" in rs.part_name.lower()
    assert_close(_one(rs, "torque", load_case="nominal").value, 25000.0)
    assert_close(_one(rs, "torque", load_case="peak").value, 75000.0)
    speed = _one(rs, "rotation_speed").value
    assert_close(speed[0], units.rpm_to_rad_s(4))
    assert_close(speed[1], units.rpm_to_rad_s(6))
    assert _one(rs, "service_temperature").value == (10.0, 70.0)
    assert _one(rs, "service_humidity").value == (0.85, 0.98)
    tg = _one(rs, "glass_transition_temperature")
    assert tg.comparator == ">=" and tg.value == (85.0, 90.0)
    assert tg.check(88.0) == "unknown"                 # inside "85-90": conservative bound is 90
    assert tg.check(95.0) == "pass" and tg.check(80.0) == "fail"
    for p in ("uv_resistance", "hydrophobic", "high_modulus", "fatigue_resistance"):
        assert rs.has(p), p


def test_wheel_requirements():
    rs = _sets()["2"]
    tol = _one(rs, "dimensional_tolerance")
    assert tol.region == "bore" and tol.comparator == "abs_dev_le" and tol.value == 0.1
    load = _one(rs, "radial_load")
    assert load.interpretation_status == R.DERIVED and abs(load.value - 18 * 9.80665) < 1e-9
    assert _one(rs, "service_temperature").value == (18.0, 22.0)
    assert_close(_one(rs, "operating_hours").value, 8000 * 3600.0)
    assert not _one(rs, "esd").mandatory              # "Желательно" -> preference
    assert _one(rs, "ipa_resistance").mandatory


def test_boot_and_figure_requirements():
    sets = _sets()
    rs = sets["3"]
    assert _one(rs, "bend_angle").value == 42.0
    assert _one(rs, "axial_travel").comparator == "abs_dev_le" and _one(rs, "axial_travel").value == 30.0
    assert _one(rs, "service_temperature").value == (0.0, 75.0)
    assert rs.has("sealing") and rs.has("oil_fuel_resistance")
    fig = sets["4"]
    assert _one(fig, "batch_quantity").value == 15.0
    assert fig.has("no_visible_layers") and fig.has("no_support_marks") and fig.has("uv_color_stability")


def test_wilo_requirements():
    rs = _sets()["5"]
    assert_close(_one(rs, "tensile_modulus").value, 4500.0)
    assert_close(_one(rs, "tensile_strength").value, 50.0)
    assert_close(_one(rs, "elongation_at_break").value, 0.025)
    assert_close(_one(rs, "water_absorption_24h").value, 0.0005)
    assert_close(_one(rs, "vicat_softening_temperature").value, 130.0)
    assert_close(_one(rs, "density").value, 0.0012)
    assert_close(_one(rs, "residual_unbalance").value, 6.3)
    assert_close(_one(rs, "balance_grade").value, 2.5)
    assert_close(_one(rs, "pressure").value, 1.6)
    assert _one(rs, "chemical_ph").value == (9.5, 10.5)
    assert_close(_one(rs, "torque", region="keyway").value, 12000.0)
    assert_close(_one(rs, "blade_thickness_variation").value, 0.3)


def test_fan_and_krones_requirements():
    sets = _sets()
    fan = sets["6"]
    assert_close(_one(fan, "density").value, 0.00125)
    assert_close(_one(fan, "no_softening_temperature").value, 65.0)
    speeds = [r for r in fan.requirements if r.property == "rotation_speed"]
    assert len(speeds) == 1                            # "50 Гц" is mains frequency, not a speed
    kr = sets["7"]
    tol = _one(kr, "dimensional_tolerance")
    assert tol.region == "cam_profile" and tol.value == 0.15
    assert_close(_one(kr, "torque", load_case="peak").value, 18000.0)
    assert len([r for r in kr.requirements if r.property == "torque"]) == 1
    assert _one(kr, "short_contact_temperature").value == 160.0
    assert kr.has("low_friction_vs_pom") and kr.has("non_toxic")


def test_every_requirement_has_provenance():
    for key, rs in sorted(_sets().items()):
        for r in rs.requirements:
            frag = r.provenance.get("fragment")
            assert frag and frag in rs.text, (key, r.id)
            if r.kind != R.KIND_QUALITATIVE and isinstance(r.value, float) and r.interpretation_status == R.EXPLICIT:
                assert any(ch.isdigit() for ch in frag)


def test_no_contradictions_in_examples():
    for key, rs in sorted(_sets().items()):
        assert R.validate_requirements(rs) == [], key


def test_contradiction_detection():
    rs = R.RequirementSet([R.Requirement("A", "density", ">=", 0.0013, "g/mm3"),
                           R.Requirement("B", "density", "<=", 0.0012, "g/mm3")])
    issues = R.validate_requirements(rs)
    assert issues and issues[0]["ids"] == ["A", "B"]
    # peak and nominal torques are different load cases and never conflict
    rs = R.RequirementSet([R.Requirement("A", "torque", ">=", 75000.0, "N*mm", load_case="peak"),
                           R.Requirement("B", "torque", "<=", 25000.0, "N*mm", load_case="nominal")])
    assert R.validate_requirements(rs) == []


def test_missing_inputs_for_sealing():
    rs = R.parse_requirements("Наименование детали: чехол\nУзел должен быть абсолютно герметичен.")
    miss = R.missing_inputs(rs)
    assert {"requirement_type": "sealing", "field": "max_leak_rate"} in miss


def test_llm_merge_rules():
    rs = R.parse_requirements("Наименование детали: X\nМатериал должен быть прочным.\nМасса не более 50 г.")
    rejected = R.merge_llm_requirements(rs, [
        {"property": "mass", "fragment": "Масса не более 80 г.", "value": 80, "comparator": "<="},   # not in text
        {"property": "tensile_strength", "fragment": "Материал должен быть прочным.", "value": 60,
         "unit": "MPa", "comparator": ">="},                                                    # invented number
        {"property": "part_mass", "fragment": "Масса не более 50 г.", "value": 50, "unit": "g",
         "comparator": "<="},                                                                   # supported
    ])
    assert len(rejected) == 1 and "not found" in rejected[0]["reason"]
    ts = [r for r in rs.requirements if r.property == "tensile_strength"][0]
    assert ts.interpretation_status == R.ASSUMED and not ts.mandatory
    pm = [r for r in rs.requirements if r.property == "part_mass"][0]
    assert pm.interpretation_status == R.DERIVED and pm.mandatory and pm.provenance["by"] == "llm"


def test_quantity_parser_forms():
    q = R.find_quantities("от +10°C ночью до +70°C днем")
    assert q[0].value() == (10.0, 70.0) and q[0].unit == "degC"
    q = R.find_quantities("Строго +20°C ± 2°C")
    assert q[0].value() == (18.0, 22.0)
    q = R.find_quantities("допуск ±0.1 мм")
    assert q[0].comparator == "abs_dev_le" and q[0].value() == 0.1
    q = R.find_quantities("не менее 4500 МПа")
    assert q[0].comparator == ">=" and q[0].unit == "MPa"
    q = R.find_quantities("ISO 1940 класс G2.5, MoS2")
    assert q == []
    q = R.find_quantities("4–6 об/мин")
    assert q[0].value() == (4.0, 6.0) and q[0].unit == "rpm"


def test_normalize_rejects_bad_units():
    rs = R.RequirementSet([R.Requirement("A", "x", "<=", 1.0, "parsec")])
    raises(units.UnitError, R.normalize_requirements, rs)
    rs = R.RequirementSet([R.Requirement("A", "x", "<=", 2.0, "N*m")])
    R.normalize_requirements(rs)
    assert rs.requirements[0].unit == "N*mm" and rs.requirements[0].value == 2000.0
