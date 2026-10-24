"""Regression: scene filenames must never become guessed filesystem paths."""
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
sys.path.insert(0, str(ROOT / "plugin/tests"))
from agent.agent import Agent
from agent.server import ChatService
from agent.tools import ToolRegistry
from testutil import example_cases


class RecordingCore:
    def __init__(self):
        self.calls = []

    def plan(self, args, context):
        self.calls.append(args)
        return {"status": "candidate"}

    def apply(self, args, context):
        raise AssertionError("Source lookup must not modify the scene")

    verify = apply


class Node:
    def __init__(self, path):
        self.path = path

    def getName(self):
        return Path(self.path).name

    def getMeshData(self):
        return SimpleNamespace(getFileName=lambda: self.path)

    def getBoundingBox(self):
        return None

    def getScale(self):
        return SimpleNamespace(x=1, y=1, z=1)


def registry():
    core = RecordingCore()
    value = ToolRegistry(None, None, lambda: "", core=core)
    value._scene_nodes = lambda: []
    return value, core


def test_scene_object_id_resolves_actual_original_stl():
    stl = example_cases()[0][2]
    value, core = registry()
    node = Node(stl)
    value._scene_nodes = lambda: [node]
    sources = value.call("list_model_sources", {})["result"]["scene_models"]
    info = value._object_info(node)
    assert info["source_path"] == stl
    assert info["can_plan_original_stl"]
    outcome = value.call("plan_print", {"task_text": "Original specification", "object_id": sources[0]["object_id"]})
    assert outcome["ok"]
    assert core.calls[0] == {"task_text": "Original specification", "mesh_path": stl}


def test_chat_upload_id_resolves_without_llm_path_retyping():
    value, core = registry()
    service = ChatService(None, None, None, value, None)
    try:
        # Exact bytes of an original organizer STL; Unicode filename is owned by the server.
        path = Path(example_cases()[0][2])
        data = path.read_bytes()
        upload = service.add_upload(path.name, io.BytesIO(data), len(data))
        value.upload_sources = service.model_uploads
        outcome = value.call("plan_print", {"task_text": "Original specification", "attachment_id": upload["id"]})
        assert outcome["ok"]
        assert core.calls[0]["mesh_path"] == upload["path"]
        content = Agent(None, value, None, None)._user_content("Original specification", [upload])
        assert "attachment_id: " + upload["id"] in content
    finally:
        service.stop()


@pytest.mark.parametrize("arguments", [
    {"mesh_path": "Шестерня.stl"},
    {"mesh_path": r"C:\nonexistent\guessed\Шестерня.stl"},
    {"attachment_id": "not-owned-by-this-session"},
    {"object_id": 98765},
    {},
    {"attachment_id": "one", "mesh_path": "two.stl"},
])
def test_invalid_or_ambiguous_source_never_reaches_core(arguments):
    value, core = registry()
    outcome = value.call("plan_print", dict(arguments, task_text="Original specification"))
    assert not outcome["ok"]
    assert not core.calls


def test_deleted_scene_source_is_reported_without_searching_for_names(tmp_path):
    value, core = registry()
    node = Node(str(tmp_path / "missing.stl"))
    value._scene_nodes = lambda: [node]
    sources = value.call("list_model_sources", {})["result"]["scene_models"]
    assert not sources[0]["source_exists"]
    assert not value.call("plan_print", {"task_text": "TZ", "object_id": sources[0]["object_id"]})["ok"]
    assert not core.calls
