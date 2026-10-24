"""Model tree of a chat: nodes after finished work, branches, switching and restoring per chat."""

import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "AIAssistant"))

from agent import agent as agent_module
from agent.chats import ChatStore
from agent.events import EventBus
from agent.server import ChatService
from agent.versions import ModelTree, abbreviation, changes, duration_text, same_state


def state(preset="Normal", infill=20, layer=0.2, mode="classic", matrix=None, material="Generic PLA"):
    return {"stacks": [{"position": None, "containers": ["q_" + preset], "user": {"infill": infill}}],
            "mode": mode, "preset": preset, "materials": {"0": material}, "nozzles": {"0": "0.4 mm"},
            "effective": {"layer_height": layer, "infill_sparse_density": infill, "wall_line_count": 2,
                          "speed_print": 60, "support_enable": False, "adhesion_type": "brim"},
            "labels": {"layer_height": {"label": "Layer Height", "label_local": "Высота слоя", "unit": "mm"},
                       "infill_sparse_density": {"label": "Infill", "label_local": "Заполнение", "unit": "%"},
                       "support_enable": {"label": "Support", "label_local": "Поддержки", "unit": ""},
                       "adhesion_type": {"label": "Adhesion", "label_local": "Прилипание", "unit": "",
                                         "options": {"brim": "Кайма", "skirt": "Юбка"}}},
            "objects": [{"name": "gear.stl", "source": "/m/gear.stl", "matrix": matrix or [[1, 0, 0, 0], [0, 1, 0, 0],
                                                                                         [0, 0, 1, 0], [0, 0, 0, 1]]}]}


class FakeIO:
    def __init__(self):
        self.state = state()
        self.restored = []

    def capture(self):
        return copy.deepcopy(self.state)

    def restore(self, snapshot):
        self.state = copy.deepcopy(snapshot)
        self.restored.append(snapshot["preset"])
        return {"missing_objects": 0}

    def needs_slice(self):
        return False

    def labels(self, keys):
        known = state()["labels"]
        return dict((k, known[k]) for k in keys if k in known)

    def stats(self, should_stop=None):
        return {"print_time_s": 2 * 3600 + 40 * 60, "material_g": 31.0}


class Agent:
    def __init__(self):
        self.notes = []

    def busy(self):
        return False

    def note(self, text):
        self.notes.append(text)

    def messages(self):
        return [{"role": "user", "content": "x"}]

    def reset(self):
        return True

    def load(self, messages):
        pass


def service(tmp_path=None):
    store = ChatStore(str(tmp_path)) if tmp_path is not None else None
    s = ChatService(EventBus(), Agent(), None, None, None, store=store)
    s.state_io = FakeIO()
    return s


def work(s, name, **change):
    s.before_work()
    s.state_io.state = state(**change)
    return s.after_work(lambda rows: name)


def test_abbreviations_and_durations():
    assert abbreviation("Быстрая печать") == "БП" and abbreviation("Fast First") == "FF"
    assert abbreviation("Прочная") == "Пр" and abbreviation("") == "•"
    assert duration_text(2 * 3600 + 40 * 60) == "2 ч 40 мин" and duration_text(59 * 60 + 50, "en") == "1 h"


def test_finished_work_becomes_a_node_under_the_start():
    s = service()
    fast = work(s, "Быстрая печать", preset="Fast", infill=10, layer=0.28)
    view = s.tree_view()
    assert [n["name"] for n in view["nodes"]] == ["Исходная", "Быстрая печать"]
    start, node = view["nodes"]
    assert start["kind"] == "start" and node["parent"] == start["id"] and node["selected"]
    assert node["short"] == "БП" and node["stats"] == "3D · 2 ч 40 мин · 31 г · заполнение 10 %"
    rows = [(r["label"], r["old"], r["new"]) for r in node["changes"]]
    assert rows[0] == ("Пресет", "Normal", "Fast")
    assert ("Высота слоя", "0.2 mm", "0.28 mm") in rows and ("Заполнение", "20%", "10%") in rows
    assert fast["id"] == view["selected"]


def test_no_node_without_a_change_and_at_most_five_changes():
    s = service()
    s.before_work()
    assert s.after_work(lambda rows: "x") is None and s.tree.nodes == []
    rows = changes(state(), state(preset="Fine", infill=60, layer=0.12, mode="cylindrical_full", material="PETG",
                                  matrix=[[0, 1, 0, 0], [-1, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]))
    assert [r["key"] for r in rows] == ["printing_mode", "preset", "material", "objects", "layer_height",
                                        "infill_sparse_density"]
    assert rows[3]["new"] == "повёрнута"


def test_switching_restores_the_node_and_new_work_branches_from_it():
    s = service()
    work(s, "Быстрая печать", preset="Fast")
    work(s, "Качество подольше", preset="Fine")
    start = s.tree.nodes[0]
    result = s.select_node(start["id"])
    assert result["node"] == start["id"] and s.state_io.restored == ["Normal"]
    assert "switched the slicer" in s.agent.notes[-1]
    branch = work(s, "Прочная", preset="Normal", infill=80)
    assert branch["parent"] == start["id"]                       # a new branch from the chosen node
    assert len([n for n in s.tree.nodes if n["parent"] == start["id"]]) == 2


def test_each_chat_keeps_its_tree_and_opens_on_its_last_model(tmp_path):
    s = service(tmp_path)
    s.bus.emit("user", text="Сделай быстро", attachments=[])
    work(s, "Быстрая печать", preset="Fast")
    first = s.chat_id
    assert s.new_chat() and s.tree.nodes == []
    s.bus.emit("user", text="Сделай качественно", attachments=[])
    work(s, "Качество", preset="Fine")
    assert s.open_chat(first)
    assert s.state_io.restored[-1] == "Fast" and s.restored["node"] == s.tree.selected
    assert [n["name"] for n in s.tree_view()["nodes"]] == ["Исходная", "Быстрая печать"]
    s.stop()


def test_saved_tree_round_trip():
    tree = ModelTree()
    tree.add(state(), "Исходная", kind="start")
    tree.add(state(preset="Fast"), "Быстрая печать", {"print_time_s": 60})
    copy_ = ModelTree(tree.to_dict())
    assert copy_.view()["nodes"] == tree.view()["nodes"] and same_state(copy_.current()["snapshot"], state(preset="Fast"))


def test_the_agent_saves_a_model_after_finished_work(monkeypatch):
    from test_assistant_ux import FakeClient, registry
    from agent.mcp import MCPServer
    bus = EventBus()
    value = registry(bus=bus)[0]
    client = FakeClient([{"stream": ["Готово: пресет Fast."]}, {"stream": ["Готово: пресет Fine."]}],
                        route="act", proofread="OK")
    original = client.text

    def text(messages, **kwargs):
        if "Name the print variant" in messages[0]["content"]:
            return "«Быстрая печать»."
        return original(messages, **kwargs)
    client.text = text
    monkeypatch.setattr(agent_module, "LLMClient", lambda *a, **k: client)
    agent = agent_module.Agent(MCPServer(value), value, bus, lambda: {"language": "ru"})
    s = ChatService(bus, agent, MCPServer(value), value, None)
    io = FakeIO()
    s.state_io = io
    real_capture = io.capture
    calls = []

    def capture():
        calls.append(1)
        if len(calls) > 1:
            io.state = state(preset="Fast")           # the agent's work changed the project
        return real_capture()
    io.capture = capture
    agent.submit("Сделай мне быструю модель", [])
    agent._thread.join(10)
    names = [n["name"] for n in s.tree_view()["nodes"]]
    assert names == ["Исходная", "Быстрая печать"]
    s.select_node(s.tree.nodes[0]["id"])
    agent.submit("А теперь подольше, но качественнее", [])
    agent._thread.join(10)
    told = [m["content"] for m in agent.messages() if m["role"] == "user"][-1]
    assert told.startswith("А теперь подольше") and "switched the slicer" in told
    s.stop()


def test_a_model_of_another_printer_is_not_restored():
    import pytest
    from agent.tools import ToolError
    from agent.versions import StateIO

    class Stack:
        extruders = {}

        def getId(self):
            return "Hybrid 530 #2"

    class Registry:
        def _global(self):
            return Stack()

    io = StateIO(None, Registry())
    with pytest.raises(ToolError, match="another printer"):
        io._restore(dict(state(), machine="Hybrid 530 #1"))


def test_the_card_text_follows_the_window_language():
    tree = ModelTree()
    tree.add(state(), "Start", kind="start")
    assert tree.view("en")["nodes"][0]["empty"] == "The project before the first change"
    assert tree.view("ru")["nodes"][0]["empty"] == "Исходное состояние проекта"


def test_any_changed_setting_is_shown_explicit_ones_first_and_at_most_ten():
    from agent.versions import describe, diff
    old = state()
    new = state(preset="Fine", layer=0.12)
    new["stacks"][0]["user"] = {"support_angle": 40, "infill": 20}
    for key in ("support_angle", "speed_wall_0", "brim_width", "a1", "a2", "a3", "a4", "a5", "a6", "a7"):
        old["effective"][key] = 1
        new["effective"][key] = 2
    old["effective"]["support_angle"], new["effective"]["support_angle"] = 50, 40
    rows = diff(old, new)
    assert [r.get("key", r["kind"]) for r in rows[:3]] == ["preset", "support_angle", "layer_height"]
    lines, more = describe(rows, {"support_angle": {"label": "Support Overhang Angle", "label_local": "Угол поддержек",
                                                    "unit": "°"}})
    assert len(lines) == 10 and more == "и ещё 2"
    assert lines[1] == {"key": "support_angle", "label": "Угол поддержек", "old": "50 °", "new": "40 °", "tag": "явно"}
    assert lines[2]["tag"] == "пресет"


def test_a_setting_outside_the_main_list_is_not_lost():
    s = service()
    s.before_work()
    changed = state()
    changed["effective"]["support_z_distance"] = 0.3
    s.state_io.state = state()
    s.state_io.state["effective"]["support_z_distance"] = 0.2
    s.tree.start = {"snapshot": s.state_io.capture()}
    changed["stacks"][0]["user"]["support_z_distance"] = 0.3
    s.state_io.state = changed
    node = s.after_work(lambda rows: "Поддержки ближе")
    view = s.tree_view()["nodes"][-1]
    assert node is not None and [(c["key"], c["old"], c["new"], c["tag"]) for c in view["changes"]] == \
        [("support_z_distance", "0.2", "0.3", "явно")]


def test_the_chat_follows_the_branch_of_the_chosen_model(tmp_path, monkeypatch):
    from test_assistant_ux import FakeClient, registry
    from agent.mcp import MCPServer
    bus = EventBus()
    value = registry(bus=bus)[0]
    replies = [{"stream": ["Готово: быстрая."]}, {"stream": ["Около часа."]}, {"stream": ["Готово: качество."]},
               {"stream": ["Готово: прочная."]}]
    client = FakeClient(replies, route="act", proofread="OK")
    original = client.text
    client.text = lambda messages, **kw: "Модель" if "Name the print variant" in messages[0]["content"] \
        else original(messages, **kw)
    monkeypatch.setattr(agent_module, "LLMClient", lambda *a, **k: client)
    agent = agent_module.Agent(MCPServer(value), value, bus, lambda: {"language": "ru"})
    s = ChatService(bus, agent, MCPServer(value), value, None, store=ChatStore(str(tmp_path)))
    s.state_io = FakeIO()
    finish = agent.after_work
    upcoming = []

    def after_work(*args):
        s.state_io.state = upcoming.pop(0)  # what the agent's work did to the project
        return finish(*args)
    agent.after_work = after_work

    def say(text, result=None):
        if result is not None:
            upcoming.append(result)
        assert agent.submit(text, [])
        agent._thread.join(10)
        assert not agent.busy()

    def feed():
        return [e["text"] for e in bus.snapshot()[0] if e["type"] == "user"]

    def history():
        return [m["content"].split("\n\n")[0] for m in agent.messages() if m["role"] == "user"]

    say("Сделай быструю модель", state(preset="Fast"))
    say("Сколько печатается?")                                  # a question: no model
    say("Сделай качественнее", state(preset="Fine"))
    start, fast, fine = s.tree.nodes
    assert feed() == ["Сделай быструю модель", "Сколько печатается?", "Сделай качественнее"]

    s.select_node(fast["id"])                                    # back to the fast model
    assert feed() == ["Сделай быструю модель", "Сколько печатается?"]
    assert history() == ["Сделай быструю модель", "Сколько печатается?"]
    assert "Готово: качество." not in json.dumps(agent.messages(), ensure_ascii=False)

    say("Сделай прочнее", state(preset="Fast", infill=80))       # a new branch from the fast model
    strong = s.tree.nodes[-1]
    assert strong["parent"] == fast["id"]
    assert feed() == ["Сделай быструю модель", "Сколько печатается?", "Сделай прочнее"]

    s.select_node(fine["id"])                                    # the other branch is intact
    assert feed() == ["Сделай быструю модель", "Сколько печатается?", "Сделай качественнее"]
    s.select_node(start["id"])                                   # before the first work
    assert feed() == [] and agent.messages() == []
    s.select_node(strong["id"])
    assert feed()[-1] == "Сделай прочнее"

    chat = s.chat_id
    assert s.new_chat() and feed() == []
    assert s.open_chat(chat)                                     # reopened on the same branch
    assert feed() == ["Сделай быструю модель", "Сколько печатается?", "Сделай прочнее"]
    assert s.tree.selected == strong["id"]
    s.stop()


def test_a_chat_saved_before_branches_opens_as_one_branch(tmp_path):
    store = ChatStore(str(tmp_path))
    events = [{"type": "user", "seq": 1, "text": "Старый чат"}, {"type": "message", "seq": 2, "text": "Ответ"}]
    store.save("20260101-000000-aaaaaa", events, [{"role": "user", "content": "Старый чат"}])
    s = service(tmp_path)
    assert s.open_chat("20260101-000000-aaaaaa")
    assert [e["text"] for e in s.bus.snapshot()[0] if e["type"] == "user"] == ["Старый чат"]
    assert s.head == "legacy" and [t["id"] for t in s.turns] == ["legacy"]
    s.stop()


def test_side_plans_leave_the_current_plan_valid():
    from agent.core_bridge import CoreBridge
    bridge = CoreBridge(None, lambda: {})
    bridge._plans, bridge._generation = {"p1": {"generation": 1, "decision": {}, "request": {}}}, 1
    with bridge.side_plans():
        bridge._generation += 1
        bridge._plans = {"p2": {"generation": 2, "decision": {}, "request": {}}}
    assert bridge.decision_record("p1") == {"decision": {}, "request": {}}
