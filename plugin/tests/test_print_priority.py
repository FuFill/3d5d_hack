# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Infill penalty and the user's speed/quality priority (policy class P, uncalibrated)."""

from testutil import raises
from test_qwen_integration import box_model
from test_presets import _ste_machine

from ste_planner import process_plan as PP, presets, materials, planner
from ste_planner.llm_requirements import prepare_requirements

CAPS = {"nozzle_diameters": [0.4], "max_nozzle_temperature": 350.0}
PETG = materials.load_catalog().get("enduse_petg")      # print speed window 40..80 mm/s, profile 45


def _plan(text, priority=None, checks=(), part_type=None):
    reqset = prepare_requirements(text, None, "test")[0]
    ptype = part_type or materials.part_type(reqset)
    decisions, notes = PP.plan_process(reqset, PETG, "classic", ptype, {}, CAPS, None, None,
                                       print_priority=priority, structural_checks=list(checks))
    return dict((d.quantity, d) for d in decisions), dict((n["item"], n) for n in notes)


def test_infill_above_the_soft_cap_is_penalised():
    for priority, expected in (("balanced", 0.5), ("quality", 0.7), ("speed", 0.4)):
        result = PP.choose_infill(1.0, priority)
        assert result["chosen"] == expected, (priority, result["chosen"])
        assert result["candidates"][-1]["infill"] == 1.0 and result["candidates"][-1]["penalty"] > 0
    for priority in PP.PRINT_PRIORITIES:
        general = PP.choose_infill(0.3, priority)
        assert general["chosen"] == 0.3 and general["penalty"] == 0    # below every cap: no penalty, target kept


def test_hard_minimum_is_never_undercut_even_when_penalised():
    solid = PP.choose_infill(1.0, "speed", hard_min=1.0)
    assert solid["chosen"] == 1.0 and solid["penalty"] > 0 and [c["infill"] for c in solid["candidates"]] == [1.0]
    material_min = PP.choose_infill(0.15, "balanced", hard_min=0.25)
    assert material_min["chosen"] == 0.25


def test_solid_section_screens_and_tightness_keep_the_part_solid():
    gear = "Шестерня зубчатая, передаваемый момент 2 Н·м"
    d, notes = _plan(gear, checks=[{"check": "gear_tooth_bending"}])
    assert d["infill_fraction"].value == 1.0
    assert "solid_section_assumption" in d["infill_fraction"].model_ids
    assert "solid section" in notes["infill"]["selection"]["hard_reasons"][0]
    d, notes = _plan(gear, checks=[{"check": "rotating_rim"}])
    assert d["infill_fraction"].value == 0.5
    assert "penalised" in d["infill_fraction"].note


def test_unstated_priority_keeps_the_balanced_policy_without_labels():
    d, notes = _plan("Кронштейн", None)
    assert notes["print_priority"]["source"] == "default" and notes["print_priority"]["value"] == "balanced"
    assert d["layer_height"].value == 0.2 and d["speed_print"].value == 45.0
    assert not any(m.startswith("user_priority:") for x in d.values() for m in x.model_ids)


def test_stated_priority_moves_layer_and_speed_inside_the_material_window():
    fast, _ = _plan("Кронштейн", "speed")
    assert fast["layer_height"].value == 0.25 and fast["speed_print"].value == 80.0
    assert "user_priority:speed" in fast["layer_height"].model_ids
    assert "user_priority:speed" in fast["speed_print"].model_ids
    fine, _ = _plan("Кронштейн", "quality")
    assert fine["layer_height"].value == 0.15
    assert fine["speed_print"].value == 45.0                       # profile 45 <= window middle 60
    assert not any(m.startswith("user_priority") for m in fine["speed_print"].model_ids)
    raises(ValueError, _plan, "Кронштейн", "fastest")


def test_decorative_part_follows_a_speed_priority_but_not_a_visible_layer_requirement():
    d, _ = _plan("Фигурка декоративная", "speed", part_type="decorative")
    assert d["layer_height"].value == 0.25
    d, _ = _plan("Фигурка декоративная", None, part_type="decorative")
    assert d["layer_height"].value == 0.1


def test_priority_changes_are_kept_over_the_base_preset():
    item = {"key": "layer_height", "model_ids": ["choose_layer_height", "policy:layer", "user_priority:speed"]}
    assert presets.justification(item, "general") == "user_priority"
    item = {"key": "infill_sparse_density", "model_ids": ["policy:infill", "infill_penalty"]}
    assert presets.justification(item, "general") == "policy"
    assert presets.justification(item, "structural") == "load_class:structural"


def test_plan_reports_the_priority_and_asks_for_it_when_unknown():
    res, machine = _ste_machine()
    with box_model() as path:
        unknown = planner.plan_case("Тестовая деталь", path, resources_dir=res, machine_id=machine,
                                    forced_mode="classic")
        stated = planner.plan_case("Тестовая деталь", path, resources_dir=res, machine_id=machine,
                                   forced_mode="classic", print_priority="speed")
    assert unknown["print_priority"] == {"value": "balanced", "source": "default",
                                         "effects": unknown["print_priority"]["effects"]}
    question = unknown["clarifications"][0]
    assert question["id"] == "print_priority" and [o["value"] for o in question["options"]] == \
        ["quality", "balanced", "speed"]
    assert stated["clarifications"] == [] and stated["print_priority"]["source"] == "user"
    assert stated["effective_settings"]["layer_height"] > unknown["effective_settings"]["layer_height"]
    assert unknown["infill_selection"]["chosen"] <= unknown["infill_selection"]["soft_cap"] + 1e-9
    assert any(line.startswith("Приоритет печати") for line in unknown["explanation"])
