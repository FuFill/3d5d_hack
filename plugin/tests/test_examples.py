# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""End-to-end planning of the seven organiser cases (offline: no slicer runtime).

Expectations that are independent facts: folder hints 3D/5D, patch validity,
honest statuses (gear torque infeasible, no false pass), determinism.
"""

from testutil import example_cases, skip, resources_dir

from ste_planner import planner
from ste_planner.result import FAIL, PASS, to_json

_CACHE = {}


def _decisions():
    if "all" not in _CACHE:
        cases = example_cases()
        if len(cases) != 7:
            skip("seven example cases expected")
        resources_dir()
        _CACHE["all"] = [(name, planner.plan_case_files(docx, stl)) for name, docx, stl in cases]
    return _CACHE["all"]


def test_modes_follow_folder_hints():
    expected = {"1": "classic", "2": "cylindrical", "3": "classic", "4": "classic", "5": "cylindrical",
                "6": "classic", "7": "cylindrical_full"}
    for name, dec in _decisions():
        assert dec["route"]["mode"] == expected[name.split(".")[0]], name
        if "5D" in name:
            assert dec["route"]["mode"] in ("cylindrical", "cylindrical_full")
        if "3D" in name:
            assert dec["route"]["mode"] == "classic"


def test_patches_are_valid_and_scoped():
    for name, dec in _decisions():
        patch = dec["setting_patch"]
        assert patch["errors"] == [], (name, patch["errors"])
        keys = [i["key"] for i in patch["items"]]
        assert "printing_mode" in keys and len(keys) == len(set(keys))
        for i in patch["items"]:
            assert i["validation"] in ("valid", "warning"), (name, i)
            assert i["enabled_after_patch"] is not False, (name, i["key"])
        eff = dec["effective_settings"]
        assert eff["printing_mode"] == dec["route"]["mode"]
        if dec["route"]["mode"] != "classic":
            assert eff["support_enable"] is False
            assert eff["cylindrical_mode_base_diameter"] >= 4.0


def test_honest_statuses():
    by = dict((name.split(".")[0], dec) for name, dec in _decisions())
    gear = by["1"]
    assert gear["status"] == "infeasible"
    torque_fail = [v for v in gear["verification"] if v["property"] == "torque" and v["status"] == FAIL]
    assert torque_fail                                        # analytic bound: teeth cannot carry 25/75 N*m
    assert by["5"]["chosen_material"]["id"] == "fiberpart_pp_g30"
    assert by["3"]["chosen_material"]["id"].startswith("sealant_tpu")
    for name, dec in _decisions():
        for v in dec["verification"]:
            if v["status"] == PASS:
                assert v["evidence_level"] is not None, (name, v)
        quals = [v for v in dec["verification"] if v["property"] in ("fatigue_resistance", "sealing",
                                                                     "low_friction_vs_pom", "creep_resistance")]
        assert all(v["status"] != PASS for v in quals), name  # never claimed without tests
        assert dec["qualification_plan"], name


def test_wilo_balance_grade_reported_against_tz_limit():
    dec = dict((name.split(".")[0], dec) for name, dec in _decisions())["5"]
    bal = [c for c in dec["structural_checks"] if c["check"] == "balance_grade_vs_limit"][0]
    assert bal["U_per_gmm"] < 6.3 and bal["governing_limit_gmm"] == bal["U_per_gmm"]


def test_decisions_are_deterministic():
    cases = example_cases()
    if len(cases) != 7:
        skip("examples not found")
    name, docx, stl = cases[6]
    a = to_json(planner.plan_case_files(docx, stl))
    b = to_json(planner.plan_case_files(docx, stl))
    assert a == b
