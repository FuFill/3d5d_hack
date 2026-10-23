"""LLM boundary: citation, force units, tool authority and deterministic core binding."""

import copy

from ste_planner import assistant, planner
from testutil import resources_dir
from functional_fixtures import planning_fixture

TEXT = "Консоль. Закрепить левый торец. На правый торец сила 10 Н по -Z. Передняя сторона видимая."
INTENT = {"purpose": "Консоль", "missing_inputs": [],
          "regions": [{"surface_id": "root", "role": "fixture", "quote": "Закрепить левый торец"},
                      {"surface_id": "front", "role": "visible", "quote": "Передняя сторона видимая"}],
          "loads": [{"surface_id": "tip", "force_N": [0, 0, -10], "quote": "На правый торец сила 10 Н по -Z"}]}


def test_valid_intent_has_exact_force_direction_and_provenance():
    result = assistant.validate_intent(copy.deepcopy(INTENT), TEXT, ["root", "tip", "front"])
    assert result.ok and result.evidence is None
    assert result.value["loads"][0]["force_N"] == [0, 0, -10]


def test_no_invented_force_axis_region_or_quote_is_accepted():
    mutations = []
    bad = copy.deepcopy(INTENT)
    bad["loads"][0]["force_N"] = [0, 0, -100]
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["loads"][0]["force_N"] = [10, 0, 0]
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["loads"][0]["quote"] = "сила 100 Н"
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["regions"][0]["surface_id"] = "unknown"
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["elastic"] = {"E_xy_MPa": 999999}
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["loads"][0]["force_N"] = [True, 0, 0]
    mutations.append(bad)
    bad = copy.deepcopy(INTENT)
    bad["regions"].append({"surface_id": "tip", "role": "no_support", "quote": "На правый торец сила 10 Н по -Z"})
    mutations.append(bad)
    for bad in mutations:
        assert assistant.validate_intent(bad, TEXT, ["root", "tip", "front"]).status == "invalid_input"


def test_missing_numeric_load_or_fixture_never_becomes_a_default():
    data = copy.deepcopy(INTENT)
    data["loads"] = []
    assert assistant.validate_intent(data, TEXT, ["root", "tip", "front"]).status == "needs_input"
    data = copy.deepcopy(INTENT)
    data["regions"] = []
    assert assistant.validate_intent(data, TEXT, ["root", "tip", "front"]).status == "needs_input"


def test_binding_uses_application_mesh_profiles_and_surface_selectors():
    _, template, _, surfaces = planning_fixture()
    validated = assistant.validate_intent(copy.deepcopy(INTENT), TEXT, [s["id"] for s in surfaces])
    bound = assistant.bind_spec(validated, template, surfaces)
    assert bound.ok
    assert bound.value["profiles"] == template["profiles"]
    assert bound.value["volume_mesh"] == template["volume_mesh"]
    assert bound.value["load_cases"][0]["on"] == surfaces[1]["selector"]
    assert bound.value["fixed"][0]["on"] == surfaces[0]["selector"]


def test_assistant_to_core_success_and_failure_paths_preserve_safety():
    path, template, initial, surfaces = planning_fixture()
    original = assistant.extract_intent
    try:
        assistant.extract_intent = lambda *args, **kwargs: assistant.validate_intent(copy.deepcopy(INTENT), TEXT, [s["id"] for s in surfaces])
        decision = assistant.plan_from_text(TEXT, path, template, surfaces, resources_dir=resources_dir(), forced_mode="classic")
        assert decision["llm_intake"]["status"] == "computed"
        assert decision["functional_analysis"]["value"]["selected"] is not None
        bad = copy.deepcopy(INTENT)
        bad["loads"] = []
        assistant.extract_intent = lambda *args, **kwargs: assistant.validate_intent(bad, TEXT, [s["id"] for s in surfaces])
        decision = assistant.plan_from_text(TEXT, path, template, surfaces, resources_dir=resources_dir(), forced_mode="classic")
        assert decision["status"] == "needs_input"
        assert decision["effective_settings"] == initial["effective_settings"]
    finally:
        assistant.extract_intent = original


def test_LLM_configuration_missing_or_non_https_is_safe():
    assert assistant.extract_intent("", [], config={"LLM_MODEL": "example"}).status == "needs_input"
    config = {"LLM_BASE_URL": "http://example.org", "LLM_API_KEY": "test", "LLM_MODEL": "test"}
    assert assistant.extract_intent("", [], config=config).status == "invalid_input"
