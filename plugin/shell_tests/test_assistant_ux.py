"""Chat shell contracts: specification files, questions, step captions, setting lookup, presets in an open project.

Fakes stand in for Uranium/STE objects; the Qt operations themselves need the real
slicer (check_ai_assistant_runtime.py). These tests pin the shell's own logic.
"""
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin/tests"))
from agent import agent as agent_module
from agent import project_apply
from agent.documents import extract_text
from agent.events import EventBus
from agent.llm import LLMError
from agent.mcp import MCPServer
from agent.questions import QuestionBox, QuestionCancelled
from agent.server import ChatService
from agent.tools import ToolError, ToolRegistry
from ste_planner import functional, presets as P
from ste_planner import runtime_adapter as A
from ste_planner.requirements import read_docx_text
from testutil import example_cases


# ---------------------------------------------------------------- fakes

DEFS = {
    "layer_height": dict(type="float", label="Layer Height", unit="mm", per_ext=True, value=0.2),
    "infill_sparse_density": dict(type="float", label="Infill Density", unit="%", per_ext=True, value=20.0),
    "infill_pattern": dict(type="enum", label="Infill Pattern", per_ext=True, value="grid",
                           options={"grid": "Grid", "gyroid": "Gyroid"}),
    "support_enable": dict(type="bool", label="Generate Support", per_ext=False, value=False),
    "wall_line_count": dict(type="int", label="Wall Line Count", per_ext=True, value=2),
    "printing_mode": dict(type="enum", label="Printing Mode", per_ext=False, value="classic",
                          options={"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}),
    "machine_five_axis": dict(type="bool", label="5 axis", per_ext=False, value=True),
}


class Definition:
    def __init__(self, key):
        self.key, self.type, self.label = key, DEFS[key]["type"], DEFS[key]["label"]
        self.description = DEFS[key]["label"] + " of the print"


class Bottom:
    def getId(self):
        return "hte530v5.2.4"

    def getInheritedFiles(self):
        return []

    def findDefinitions(self, key=None):
        if key is not None:
            return [Definition(key)] if key in DEFS else []
        return [Definition(k) for k in DEFS]


class User:
    def __init__(self):
        self.values = {}

    def setProperty(self, key, prop, value):
        self.values[key] = value

    def getProperty(self, key, prop):
        return self.values.get(key)

    def hasProperty(self, key, prop):
        return key in self.values

    def removeInstance(self, key):
        self.values.pop(key, None)

    def getAllKeys(self):
        return set(self.values)


class Stack:
    def __init__(self, parent=None, position=None):
        self.userChanges, self.parent, self.position = User(), parent, position
        self.base = {}  # what the active preset resolves to
        self.extruders = {}
        self.material = SimpleNamespace(getName=lambda: "Generic PLA", getId=lambda: "generic_pla",
                                        getMetaDataEntry=lambda k, d=None: "generic_pla" if k == "base_file" else d)
        self.definition = SimpleNamespace(getId=lambda: "hte530v5.2.4", getName=lambda: "Hybrid 530")

    def getBottom(self):
        return Bottom()

    def getMetaDataEntry(self, key, default=None):
        return self.position if key == "position" else default

    def getProperty(self, key, prop):
        d = DEFS.get(key)
        if d is None:
            return None
        if prop == "value":
            for s in [self] + ([self.parent] if self.parent else []):
                if key in s.userChanges.values:
                    return s.userChanges.values[key]
                if key in s.base:
                    return s.base[key]
            return d["value"]
        return {"settable_per_extruder": d.get("per_ext", False), "enabled": True, "options": d.get("options"),
                "label": d["label"], "unit": d.get("unit", ""), "type": d["type"]}.get(prop)


def registry(core=None, bus=None):
    stack = Stack()
    stack.extruders = {"0": Stack(stack, "0")}
    app = SimpleNamespace(getGlobalContainerStack=lambda: stack,
                          getMachineManager=lambda: SimpleNamespace(activeQualityOrQualityChangesName="Normal"))
    value = ToolRegistry(app, None, lambda: "", core=core, bus=bus)
    value._catalog = False
    value._scene_nodes = lambda: []
    value._extruder = lambda position=None: stack.extruders["0"]
    return value, stack


# ---------------------------------------------------------------- specification files

def test_docx_specification_text_is_exactly_the_cores():
    cases = [c for c in example_cases() if Path(c[1]).is_file()]
    if not cases:
        pytest.skip("organizer DOCX not available")
    for _, docx, _ in cases:
        assert extract_text(docx) == read_docx_text(docx).strip()


def test_text_specification_encodings_and_errors(tmp_path):
    utf = tmp_path / "tz.txt"
    utf.write_bytes("﻿Шестерня, PETG\r\nнагрузка 20 Н".encode("utf-8"))
    assert extract_text(str(utf)) == "Шестерня, PETG\nнагрузка 20 Н"
    cp = tmp_path / "tz_cp.txt"
    cp.write_bytes("Кронштейн".encode("cp1251"))
    assert extract_text(str(cp)) == "Кронштейн"
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"  \n")
    with pytest.raises(ValueError, match="empty"):
        extract_text(str(empty))
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="unreadable"):
        extract_text(str(broken))


def test_upload_accepts_specs_and_names_the_rejection_reason(tmp_path):
    service = ChatService(EventBus(), None, None, None, None)
    data = "Ось вращения, PA6".encode("utf-8")
    item = service.add_upload("ТЗ%20оси.txt", io.BytesIO(data), len(data))
    assert item["kind"] == "spec" and item["text"] == "Ось вращения, PA6" and item["name"] == "ТЗ%20оси.txt"
    assert service.spec_uploads()[0]["id"] == item["id"]
    with pytest.raises(ValueError, match="unsupported"):
        service.add_upload("model.step", io.BytesIO(b"x"), 1)
    with pytest.raises(ValueError, match="empty"):
        service.add_upload("blank.txt", io.BytesIO(b" "), 1)
    assert len(service.spec_uploads()) == 1
    service.stop()


def test_spec_attachments_preserve_the_users_separate_instructions():
    agent = agent_module.Agent(None, None, None, None)
    content = agent._user_content("Оставь положение и используй PETG", [
        {"kind": "spec", "name": "first.docx", "id": "s1", "text": "Первое ТЗ"},
        {"kind": "spec", "name": "second.txt", "id": "s2", "text": "Второе ТЗ"}])
    assert content.startswith("Оставь положение и используй PETG")
    assert "Первое ТЗ" in content and "Второе ТЗ" in content
    assert "spec_attachment_id: s1" in content and "spec_attachment_id: s2" in content


def test_undefined_native_entries_do_not_invalidate_the_preset_snapshot():
    class NativeContainer:
        def __init__(self):
            self.values = {"layer_height": 0.2, "support_enable": False, "transient": None}
        def getId(self): return "test-native-quality"
        def getName(self): return "Normal"
        def getMetaData(self): return {"type": "quality", "definition": "tiny"}
        def getAllKeys(self): return list(self.values)
        def getProperty(self, key, prop): return self.values.get(key)
    container = NativeContainer()
    before = A.catalog_entry(None, container, "quality")
    assert before["values"] == {"layer_height": 0.2, "support_enable": False}
    del container.values["transient"]  # STE's lazy cleanup, no actual setting changed.
    assert A.catalog_entry(None, container, "quality")["digest"] == before["digest"]
    container.values["layer_height"] = 0.21
    assert A.catalog_entry(None, container, "quality")["digest"] != before["digest"]


def test_plan_print_uses_the_original_spec_text_and_user_material():
    class Core:
        apply = verify = None

        def plan(self, args, ctx):
            self.args = args
            return {"plan_id": "p", "setting_changes": []}
    core = Core()
    value, _ = registry(core=core)
    value._app = None
    value.spec_sources = lambda: [{"id": "s1", "text": "ТЗ: шестерня, момент 2 Н·м"}]
    stl = example_cases()[0][2]
    value.upload_sources = lambda: [{"id": "m1", "path": stl, "name": "g.stl"}]
    value._plan_print({"spec_attachment_id": "s1", "task_text": "печатать быстрее", "attachment_id": "m1",
                       "preferred_material": "enduse_petg"}, {})
    assert core.args["task_text"] == "ТЗ: шестерня, момент 2 Н·м\n\nпечатать быстрее"
    assert core.args["preferred_materials"] == ["enduse_petg"] and "preferred_material" not in core.args
    with pytest.raises(ToolError, match="Specification attachment"):
        value._plan_print({"spec_attachment_id": "nope", "attachment_id": "m1"}, {})


# ---------------------------------------------------------------- questions

def test_question_waits_for_the_answer_and_reports_the_option():
    bus = EventBus()
    value, _ = registry(bus=bus)
    results = {}
    worker = threading.Thread(target=lambda: results.update(value._ask_user(
        {"question": "Какой материал?", "options": ["PETG", "PLA"]}, {"cancelled": lambda: False})))
    worker.start()
    for _ in range(100):
        if value.questions.pending():
            break
        time.sleep(0.01)
    seq = value.questions.pending()[0]
    assert not value.questions.answer(seq + 100, "x")
    assert value.questions.answer(seq, "PETG", 0)
    worker.join(2)
    assert results == {"answer": "PETG", "option": "PETG", "custom": False}
    event = [e for e in bus.snapshot()[0] if e["type"] == "question"][0]
    assert event["status"] == "answered" and event["answer"] == "PETG"


def test_question_cancel_unblocks_the_tool():
    bus = EventBus()
    box = QuestionBox(bus)
    stop = {"value": False}
    errors = []

    def ask():
        try:
            box.ask("?", ["a", "b"], True, lambda: stop["value"])
        except QuestionCancelled:
            errors.append("cancelled")
    worker = threading.Thread(target=ask)
    worker.start()
    time.sleep(0.05)
    stop["value"] = True
    worker.join(2)
    assert errors == ["cancelled"] and not box.pending()
    assert [e for e in bus.snapshot()[0] if e["type"] == "question"][0]["status"] == "cancelled"


def test_question_needs_two_options():
    value, _ = registry(bus=EventBus())
    with pytest.raises(ToolError):
        value._ask_user({"question": "?", "options": ["only"]}, {})


# ---------------------------------------------------------------- settings lookup

def test_find_settings_matches_most_words_and_batches_topics():
    value, _ = registry()
    found = value._find_settings({"queries": ["infill density pattern", "support"]}, {})["results"]
    keys = [s["key"] for s in found["infill density pattern"]]
    assert "infill_sparse_density" in keys and "infill_pattern" in keys
    assert found["support"][0]["key"] == "support_enable"


def test_unknown_key_suggests_the_real_one():
    value, _ = registry()
    with pytest.raises(ToolError, match="support_enable"):
        value._definition("support_enabled")
    errors = value._get_settings({"keys": ["wall_line_count", "infill_sparse_densty"]}, {})["errors"]
    assert "infill_sparse_density" in errors[0]


# ---------------------------------------------------------------- presets into an open project

def test_shell_process_keys_mirror_the_core():
    assert project_apply.PROCESS_KEYS == functional.PROCESS_KEYS


def decision(mode="classic", status="candidate", base=True):
    return {"status": status, "route": {"mode": mode},
            "base_presets": {"label": "Fine (normal)", "contract": P.CONTRACT} if base else None,
            "setting_patch": {"payload": {"global": {"printing_mode": mode},
                                          "extruders": {"0": {"infill_sparse_density": 60.0, "wall_line_count": 4}}},
                              "errors": []},
            "effective_settings": {"infill_sparse_density": 60.0, "wall_line_count": 4, "printing_mode": mode},
            "scene_operations": [{"op": "set_transform", "rotation": [[1, 0, 0], [0, 0, -1], [0, 1, 0]],
                                  "translation": [0, 0, 0]}]}


def test_open_project_gets_the_preset_and_only_the_delta(monkeypatch):
    value, stack = registry()
    extruder = stack.extruders["0"]
    extruder.userChanges.setProperty("layer_height", "value", 0.3)  # the user's manual change
    plan = decision()
    applied = {}

    class Operation:
        readback = {"status": "pass", "differences": []}

        def redo(self):
            # Real adapter: P into the stacks, Delta into a fresh user container.
            extruder.userChanges = project_apply_user({"infill_sparse_density": 60.0, "wall_line_count": 4})
            extruder.base = {"layer_height": 0.15}
            applied["redo"] = True

        def push(self):
            applied["push"] = True

        def undo(self):
            applied["undo"] = True

    fake = SimpleNamespace(ApplyError=A.ApplyError,
                           create_settings_operation=lambda app, d, allow: applied.update(allow=allow) or Operation())
    monkeypatch.setattr(project_apply, "_runtime_adapter", lambda: fake)
    core = SimpleNamespace(decision_record=lambda plan_id: {"decision": plan})
    applier = project_apply.InPlaceApplier(value, core)
    result = applier.apply({"plan_id": "p"}, {})
    assert applied == {"allow": False, "redo": True, "push": True}
    assert result["native_preset"] and result["base_preset"] == "Fine (normal)" and result["readback"] == "pass"
    rows = dict((r["key"], r) for r in result["changes"])
    assert rows["profile"]["old"] == "Normal" and rows["profile"]["new"] == "Fine (normal)"
    assert rows["infill_sparse_density"]["old"] == 20.0 and rows["infill_sparse_density"]["new"] == 60.0
    assert rows["layer_height"]["old"] == 0.3 and rows["layer_height"]["new"] == 0.15
    assert rows["layer_height"].get("inherited") and not rows["infill_sparse_density"].get("inherited")
    assert result["planned_orientation"] == "X +90°" and result["objects_moved"] is False
    with pytest.raises(ToolError, match="already applied"):
        applier.apply({"plan_id": "p"}, {})
    applier.revert({"plan_id": "p"}, {})
    assert applied["undo"]


def project_apply_user(values):
    user = User()
    user.values.update(values)
    return user


def test_open_project_refuses_infeasible_and_maps_adapter_errors(monkeypatch):
    value, _ = registry()
    core = SimpleNamespace(decision_record=lambda plan_id: {"decision": decision(status="infeasible")})
    with pytest.raises(ToolError, match="infeasible"):
        project_apply.InPlaceApplier(value, core).apply({"plan_id": "p"}, {})

    def refuse(app, d, allow):
        raise A.ApplyError("needs_input: 5D requires machine kinematics")
    monkeypatch.setattr(project_apply, "_runtime_adapter",
                        lambda: SimpleNamespace(ApplyError=A.ApplyError, create_settings_operation=refuse))
    core = SimpleNamespace(decision_record=lambda plan_id: {"decision": decision(mode="cylindrical_full")})
    with pytest.raises(ToolError, match="5D"):
        project_apply.InPlaceApplier(value, core).apply({"plan_id": "p"}, {})


def test_settings_preflight_keeps_the_cores_guards():
    from test_presets import _base, catalog
    _, selected, _ = _base()
    record = P.base_presets_record(catalog(), selected["candidate"])
    plan = {"status": "candidate", "route": {"mode": "cylindrical_full"}, "base_presets": record,
            "calibration_context": {"preset_base": P.base_identity(record)},
            "setting_patch": {"payload": {"extruders": {"0": {"wall_line_count": 4}}}, "errors": []},
            "effective_settings": {"wall_line_count": 4}}
    with pytest.raises(A.ApplyError, match="5D"):
        A.settings_preflight(plan, "tiny")
    assert A.settings_preflight(plan, "tiny", allow_unverified_5d=True) is record
    with pytest.raises(A.ApplyError, match="another machine"):
        A.settings_preflight(plan, "other", allow_unverified_5d=True)
    mismatch = dict(plan, effective_settings={"wall_line_count": 5})
    with pytest.raises(A.ApplyError, match="disagrees"):
        A.settings_preflight(mismatch, "tiny", allow_unverified_5d=True)
    with pytest.raises(A.ApplyError, match="infeasible"):
        A.settings_preflight(dict(plan, status="infeasible"), "tiny", allow_unverified_5d=True)
    with pytest.raises(A.ApplyError, match="native preset"):
        A.settings_preflight(dict(plan, base_presets=None), "tiny", allow_unverified_5d=True)
    with pytest.raises(A.ApplyError, match="calibration context"):
        A.settings_preflight(dict(plan, calibration_context={}), "tiny", allow_unverified_5d=True)


def test_full_apply_keeps_5d_blocked_without_consent():
    with pytest.raises(A.ApplyError, match="5D"):
        A.preflight({"route": {"mode": "cylindrical_full"}}, "x.stl", "hte530v5.2.4")


def test_rotation_text():
    assert project_apply.rotation_text([[1, 0, 0], [0, 1, 0], [0, 0, 1]]) == "0°"
    assert project_apply.rotation_text([[0, -1, 0], [1, 0, 0], [0, 0, 1]]) == "Z +90°"


# ---------------------------------------------------------------- agent loop

class FakeClient:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def chat(self, messages, tools, on_reasoning, on_content, should_stop):
        self.requests.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        for chunk in reply.get("stream", []):
            on_content(chunk)
        return {"content": "".join(reply.get("stream", [])), "reasoning": "", "tool_calls": reply.get("calls", [])}


def run_agent(monkeypatch, replies, registry_value=None):
    bus = EventBus()
    value = registry_value or registry(bus=bus)[0]
    value._get_state = lambda a, c: {"preset": {"profile": "Normal"}}
    for tool in value._tools:
        if tool.name == "get_state":
            tool.func, tool.on_main = value._get_state, False
    client = FakeClient(replies)
    monkeypatch.setattr(agent_module, "LLMClient", lambda *a, **k: client)
    monkeypatch.setattr(agent_module, "RETRY_DELAYS", (0, 0))
    agent = agent_module.Agent(MCPServer(value), value, bus, lambda: {"language": "ru"})
    agent.submit("сделай прочнее", [])
    agent._thread.join(5)
    return agent, bus, client


def call(name, arguments="{}", call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def test_turn_starts_by_reading_the_project_and_short_text_becomes_the_caption(monkeypatch):
    agent, bus, client = run_agent(monkeypatch, [
        {"stream": ["Ищу настройки ", "прочности"], "calls": [call("find_settings", '{"queries": ["wall"]}')]},
        {"stream": ["Готово."]}])
    events = bus.snapshot()[0]
    tools = [e for e in events if e["type"] == "tool"]
    assert tools[0]["name"] == "get_state" and tools[0]["call_id"].startswith("context_")
    notes = [e for e in events if e["type"] == "message" and e.get("note")]
    assert notes[0].get("auto") and notes[1]["text"] == "Ищу настройки прочности"
    final = [e for e in events if e["type"] == "message" and not e.get("note")]
    assert [e["text"] for e in final] == ["Готово."]
    first = client.requests[0]
    assert first[1]["role"] == "user" and first[2]["tool_calls"][0]["function"]["name"] == "get_state"
    assert first[3]["role"] == "tool"
    assert events[-1]["type"] == "done" and events[-1]["status"] == "done"


def test_transient_errors_are_retried_but_auth_errors_are_not(monkeypatch):
    agent, bus, client = run_agent(monkeypatch, [LLMError("HTTP 503 busy"), {"stream": ["ok"]}])
    assert [e["status"] for e in bus.snapshot()[0] if e["type"] == "done"] == ["done"]
    agent, bus, client = run_agent(monkeypatch, [LLMError("HTTP 401 bad key"), {"stream": ["never"]}])
    errors = [e for e in bus.snapshot()[0] if e["type"] == "error"]
    assert errors and "401" in errors[0]["text"] and len(client.requests) == 1


def test_earlier_turns_drop_images_and_long_tool_output(monkeypatch):
    bus = EventBus()
    value = registry(bus=bus)[0]
    agent = agent_module.Agent(MCPServer(value), value, bus, lambda: {"language": "en"})
    agent._messages = [
        {"role": "user", "content": [{"type": "text", "text": "photo"}, {"type": "image_url", "image_url": {"url": "x"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "y" * 5000},
        {"role": "user", "content": [{"type": "text", "text": "now"}, {"type": "image_url", "image_url": {"url": "z"}}]}]
    messages = agent._request_messages({"language": "en"})
    assert messages[1]["content"][1] == {"type": "text", "text": "[image shown earlier]"}
    assert len(messages[2]["content"]) < 3100
    assert messages[3]["content"][1]["type"] == "image_url"
    assert agent._messages[0]["content"][1]["type"] == "image_url"  # history itself is unchanged
