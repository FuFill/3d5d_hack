"""Model tree of a chat: nodes after finished work, branches, switching and restoring per chat."""

import copy
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
    assert node["short"] == "БП" and node["stats"] == "2 ч 40 мин · 31 г · заполнение 10 %"
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
    assert [r["key"] for r in rows] == ["printing_mode", "preset", "material", "objects", "layer_height"]
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
