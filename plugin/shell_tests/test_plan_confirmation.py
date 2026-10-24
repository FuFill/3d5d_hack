"""Real Agent/MCP/QuestionBox: plan proposals cannot execute before a card answer.

Only the scene operations and LLM are scripted. These are interaction contracts,
not print-quality measurements or claims about material properties.
"""
import copy
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent import agent as module
from agent.events import EventBus
from agent.mcp import MCPServer
from agent.tools import ToolRegistry, ToolError
from test_assistant_ux import FakeClient, call


def proposal(plan_id="p1", mode="classic", status="candidate", preset_status="selected"):
    return {"plan_id": plan_id, "decision_sha256": plan_id + "-hash", "status": status,
            "base_preset": {"status": preset_status, "profile": "Fine"},
            "chosen_material": {"label": "PETG"}, "route": {"mode": mode}, "applied": False}


def start(monkeypatch, replies, plans=None, language="ru"):
    bus, mutations, reads = EventBus(), [], []
    registry = ToolRegistry(None, None, lambda: "", bus=bus)
    plan_queue, known = list(plans or [proposal()]), {}

    def calculate(args, ctx):
        value = copy.deepcopy(plan_queue.pop(0))
        known[value["plan_id"]] = value
        return copy.deepcopy(value)

    def get_plan(args, ctx):
        reads.append(args.get("plan_id"))
        if args.get("plan_id") not in known:
            raise ToolError("Unknown plan")
        return copy.deepcopy(known[args["plan_id"]])

    registry._add("plan_print", "Calculate", {}, calculate, on_main=False)
    registry._add("get_plan", "Read", {}, get_plan, on_main=False)
    for name in ("apply_plan", "apply_plan_to_project"):
        registry._add(name, "Apply", {}, None, on_main=False)
    for tool in registry._tools:
        if tool.name in ("plan_print", "get_plan", "ask_user"):
            continue
        if tool.name.startswith(("get_", "list_", "find_")):
            tool.func, tool.on_main = lambda a, c: {}, False
        else:
            def mutate(args, ctx, name=tool.name):
                mutations.append((name, copy.deepcopy(args)))
                return {"applied": True}
            tool.func, tool.on_main = mutate, False
    client = FakeClient(replies)
    monkeypatch.setattr(module, "LLMClient", lambda *a, **k: client)
    agent = module.Agent(MCPServer(registry), registry, bus, lambda: {"language": language})
    assert agent.submit("Подбери настройки", [])
    return agent, registry, bus, mutations, client


def pending(agent, registry, bus, after=0):
    deadline = time.time() + 3
    while time.time() < deadline:
        questions = [e for e in bus.snapshot()[0] if e["type"] == "question" and e["status"] == "pending" and e["seq"] > after]
        if questions and registry.questions.pending():
            return questions[-1]
        if not agent.busy():
            break
        time.sleep(.005)
    pytest.fail("No execution confirmation: %s" % bus.snapshot()[0])


def answer(registry, question, choice):
    option = question["questions"][0]["options"][choice]
    assert registry.questions.answer(question["seq"], [{"choice": choice, "text": option}])


def finish(agent, bus):
    agent._thread.join(3)
    assert not agent.busy()
    assert not [e for e in bus.snapshot()[0] if e["type"] == "error"]


@pytest.mark.parametrize("apply_tool", ["apply_plan", "apply_plan_to_project", "set_settings", "slice"])
def test_refusal_stops_every_queued_action(monkeypatch, apply_tool):
    agent, registry, bus, mutations, client = start(monkeypatch, [
        {"calls": [call("plan_print"), call(apply_tool, '{"plan_id":"p1"}', "c2"),
                   call("export_gcode", call_id="c3")]}])
    card = pending(agent, registry, bus)
    assert not mutations
    assert not [e for e in bus.snapshot()[0] if e["type"] == "tool" and e["name"] == apply_tool]
    assert "Перейти к выполнению" in card["questions"][0]["text"]
    if apply_tool == "apply_plan_to_project":
        assert "всех моделей" in card["questions"][0]["text"]
    answer(registry, card, 1)
    finish(agent, bus)
    assert not mutations and len(client.requests) == 1
    replies = [m for m in agent.messages() if m["role"] == "tool"]
    assert {"c1", "c2", "c3"} <= {m["tool_call_id"] for m in replies}
    assert "stay in planning" in replies[-1]["content"]


@pytest.mark.parametrize("language,mode", [("ru", "classic"), ("en", "cylindrical"), ("ru", "cylindrical_full")])
def test_approval_runs_the_plan_once_and_5d_flag_follows_the_warning(monkeypatch, language, mode):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a"), call("slice", call_id="s")]},
        {"stream": ["Готово."]}], plans=[proposal(mode=mode)], language=language)
    card = pending(agent, registry, bus)
    text = card["questions"][0]["text"]
    assert ("не проверены" in text or "unverified" in text) == (mode != "classic")
    assert not mutations
    answer(registry, card, 0)
    finish(agent, bus)
    assert [m[0] for m in mutations] == ["apply_plan", "slice"]
    assert mutations[0][1].get("confirm_unverified_5d", False) == (mode != "classic")
    assert len([e for e in bus.snapshot()[0] if e["type"] == "question"]) == 1


def test_new_plan_requires_its_own_consent(monkeypatch):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a")]},
        {"calls": [call("plan_print", call_id="p2"), call("apply_plan", '{"plan_id":"p2"}', "b")]}],
        plans=[proposal(), proposal("p2")])
    first = pending(agent, registry, bus)
    answer(registry, first, 0)
    second = pending(agent, registry, bus, after=first["seq"])
    assert second["seq"] != first["seq"]
    answer(registry, second, 1)
    finish(agent, bus)
    assert mutations == [("apply_plan", {"plan_id": "p1"})]


def test_extending_consent_to_all_models_requires_a_scope_warning(monkeypatch):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a"),
                   call("apply_plan_to_project", '{"plan_id":"p1"}', "b")]}])
    first = pending(agent, registry, bus)
    original = registry._by_name["get_plan"].func
    def read_applied(args, ctx):
        result = original(args, ctx)
        result["applied"] = bool(mutations)
        return result
    registry._by_name["get_plan"].func = read_applied
    answer(registry, first, 0)
    second = pending(agent, registry, bus, after=first["seq"])
    assert "всех моделей" in second["questions"][0]["text"]
    answer(registry, second, 1)
    finish(agent, bus)
    assert [m[0] for m in mutations] == ["apply_plan"]


def test_ready_plan_without_tool_calls_still_offers_execution(monkeypatch):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print")]}, {"stream": ["План подобран: PETG, Fine."]},
        {"calls": [call("apply_plan", '{"plan_id":"p1"}')]}, {"stream": ["Применено."]}])
    card = pending(agent, registry, bus)
    assert not mutations
    answer(registry, card, 0)
    finish(agent, bus)
    assert [m[0] for m in mutations] == ["apply_plan"]


def test_cancel_while_confirming_does_not_execute(monkeypatch):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a")]}])
    pending(agent, registry, bus)
    agent.cancel()
    finish(agent, bus)
    assert not mutations
    assert [e["status"] for e in bus.snapshot()[0] if e["type"] == "done"] == ["cancelled"]


@pytest.mark.parametrize("status,preset", [("infeasible", "needs_input"), ("candidate", "needs_input"), ("infeasible", "selected")])
def test_unusable_presets_do_not_offer_to_execute(monkeypatch, status, preset):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print")]}, {"stream": ["Нужно другое сопло или материал."]}],
        plans=[proposal(status=status, preset_status=preset)])
    finish(agent, bus)
    assert not mutations
    assert not any(e["type"] == "question" for e in bus.snapshot()[0])


@pytest.mark.parametrize("text,allowed", [("да", True), ("применяй!", True), ("yes", True),
                                         ("нет", False), ("да, но сначала поменяй материал", False)])
def test_typed_confirmation_requires_an_unconditional_answer(monkeypatch, text, allowed):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a")]},
        {"stream": ["Готово."]}])
    card = pending(agent, registry, bus)
    assert registry.questions.answer(card["seq"], [{"choice": None, "text": text}])
    finish(agent, bus)
    assert bool(mutations) is allowed


def test_reopened_chat_does_not_reuse_an_old_execution_answer(monkeypatch):
    agent, registry, bus, mutations, client = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a")]},
        {"stream": ["Готово."]}])
    first = pending(agent, registry, bus)
    answer(registry, first, 0)
    finish(agent, bus)
    mutations[:] = []
    agent.load(agent.messages())
    client.replies[:] = [{"calls": [call("apply_plan", '{"plan_id":"p1"}', "b")]}]
    assert agent.submit("Примени план", [])
    card = pending(agent, registry, bus, after=first["seq"])
    assert not mutations
    answer(registry, card, 1)
    finish(agent, bus)
    assert not mutations


def test_bridge_exposes_rejected_profile_reasons_for_the_ui():
    from agent.core_bridge import CoreBridge
    bridge = CoreBridge(None, lambda: {})
    decision = {"status": "candidate", "base_presets": {"label": "Fine"}, "preset_selection": {
        "status": "selected", "rejected": [{"quality_type": "normal", "reason": "defined for another machine",
                                              "errors": ["private full diagnostic"]}]}}
    bridge._plans["p1"] = {"generation": 0, "decision": decision, "applied": False,
                           "request": {"geometry_sha256": "source-hash"}}
    summary = bridge.summary("p1")
    assert summary["base_preset"]["rejected"] == [{"label": "normal", "reason": "defined for another machine"}]
    summary["base_preset"]["rejected"][0]["reason"] = "edited in UI"
    assert decision["preset_selection"]["rejected"][0]["reason"] == "defined for another machine"


def test_explicit_manual_command_without_a_plan_is_not_interrupted(monkeypatch):
    agent, registry, bus, mutations, _ = start(monkeypatch, [
        {"calls": [call("set_settings", '{"settings":{"wall_line_count":4}}')]}, {"stream": ["Готово."]}])
    finish(agent, bus)
    assert [m[0] for m in mutations] == ["set_settings"]
    assert not any(e["type"] == "question" for e in bus.snapshot()[0])


def test_question_after_a_declined_plan_does_not_reopen_confirmation(monkeypatch):
    agent, registry, bus, mutations, client = start(monkeypatch, [
        {"calls": [call("plan_print"), call("apply_plan", '{"plan_id":"p1"}', "a")]},
        {"stream": ["Тонкий слой увеличивает число проходов."]}])
    card = pending(agent, registry, bus)
    answer(registry, card, 1)
    finish(agent, bus)
    assert agent.submit("Почему так долго?", [])
    finish(agent, bus)
    assert client.toolsets[-1] is None and not mutations
    assert len([e for e in bus.snapshot()[0] if e["type"] == "question"]) == 1
