"""Apply onto the model already loaded in STE instead of requiring an empty project.

Pure contracts only: mesh identity with Uranium's STL reader, scene guards and
bridge plumbing. The Qt operation itself (transform, Undo/Redo, readback) needs
the real STE and is checked by check_ai_assistant_runtime.py.
"""
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin/tests"))
from agent.core_bridge import CoreBridge
from agent.tools import ToolRegistry
from ste_planner import geometry
from ste_planner import runtime_adapter as A
from testutil import example_cases


def stl_reader_vertices(path):
    """Uranium STLReader._loadBinary: one vertex per facet corner, (x, z, -y), no centring."""
    data = Path(path).read_bytes()
    count = struct.unpack("<I", data[80:84])[0]
    out = []
    for i in range(count):
        v = struct.unpack("<ffffffffffffH", data[84 + 50 * i:84 + 50 * (i + 1)])
        for x, y, z in (v[3:6], v[6:9], v[9:12]):
            out.append((x, z, -y))
    return np.asarray(out, dtype=np.float32)


def organizer_stl():
    cases = [c for c in example_cases() if Path(c[2]).is_file()]
    if not cases:
        pytest.skip("organizer STL not available")
    path = cases[0][2]
    if Path(path).read_bytes()[80:84] == b"" or len(Path(path).read_bytes()) != \
            84 + 50 * struct.unpack("<I", Path(path).read_bytes()[80:84])[0]:
        pytest.skip("binary STL required for the reader emulation")
    return path


def test_loaded_mesh_is_recognised_as_the_planned_stl():
    path = organizer_stl()
    source = geometry.load_stl(path)
    vertices = stl_reader_vertices(path)
    sequential = np.arange(len(vertices), dtype=np.int32).reshape(-1, 3)
    assert A.source_mesh_matches(vertices, sequential, source)           # MeshBuilder.addFaceByPoints
    assert A.source_mesh_matches(vertices, None, source)                 # numpy-stl: unindexed
    shuffled = vertices.reshape(-1, 9)[np.random.RandomState(0).permutation(len(vertices) // 3)]
    assert not A.source_mesh_matches(shuffled.reshape(-1, 3), None, source)  # not the reader's facet order
    moved = vertices.copy()
    moved[5] += 0.05
    assert not A.source_mesh_matches(moved, None, source)                # edited geometry
    assert not A.source_mesh_matches(vertices * 1.01, None, source)      # baked scale
    assert not A.source_mesh_matches(vertices[:-3], None, source)        # facet removed
    bad = sequential.copy()
    bad[0, 0] = len(vertices)
    assert not A.source_mesh_matches(vertices, bad, source)


def test_native_centering_is_not_a_geometry_edit():
    path = organizer_stl()
    source = geometry.load_stl(path)
    raw = stl_reader_vertices(path)
    centre = (raw.min(axis=0) + raw.max(axis=0)) / 2
    centred = raw - centre
    offset = A.source_mesh_offset(centred, None, source)
    assert offset is not None and np.allclose(offset, -centre, atol=1e-4)
    edited = centred.copy()
    edited[5] += 0.05
    assert not A.source_mesh_matches(edited, None, source)
    assert not A.source_mesh_matches(centred * 1.01, None, source)
    assert not A.source_mesh_matches(raw + [2, 3, 4], None, source)


class Container:
    def __init__(self, keys=()):
        self._keys = set(keys)

    def getAllKeys(self):
        return self._keys


class Node:
    def __init__(self, path=None, vertices=None, parent=None, sliceable=True, group=False, settings=()):
        self.parent, self.children = parent, []
        if parent is not None:
            parent.children.append(self)
        self.mesh = None if vertices is None else SimpleNamespace(
            getFileName=lambda: path, getVertices=lambda: vertices, getIndices=lambda: None)
        self.decorations = {"isSliceable": sliceable, "isGroup": group,
                            "getStack": SimpleNamespace(getTop=lambda: Container(settings))}

    def callDecoration(self, name):
        return self.decorations.get(name)

    def getParent(self):
        return self.parent

    def getMeshData(self):
        return self.mesh

    def getAllChildren(self):
        out = []
        for child in self.children:
            out.append(child)
            out.extend(child.getAllChildren())
        return out


def scene_with_target(path, **kwargs):
    root = Node(sliceable=False)
    Node(parent=root, sliceable=False)                  # build volume/convex hull helpers
    target = Node(path, stl_reader_vertices(path), parent=root, **kwargs)
    return root, target


def test_scene_guards_keep_unplanned_models_untouched(tmp_path):
    path = organizer_stl()
    source = geometry.load_stl(path)
    root, target = scene_with_target(path)
    A.check_target_node(target, root, path, source)

    other = Node(path, stl_reader_vertices(path), parent=root)
    with pytest.raises(A.ApplyError, match="other models"):
        A.check_target_node(target, root, path, source)
    root.children.remove(other)

    modifier = Node(path, stl_reader_vertices(path), parent=target)
    with pytest.raises(A.ApplyError, match="modifier"):
        A.check_target_node(target, root, path, source)
    target.children.remove(modifier)

    _, with_settings = scene_with_target(path, settings=("infill_sparse_density",))
    with pytest.raises(A.ApplyError, match="per-object"):
        A.check_target_node(with_settings, with_settings.parent, path, source)

    copy = tmp_path / "copy.stl"
    copy.write_bytes(Path(path).read_bytes())
    with pytest.raises(A.ApplyError, match="not loaded from the planned STL"):
        A.check_target_node(target, root, str(copy), source)

    edited_root, edited = scene_with_target(path)
    edited.mesh.getVertices = lambda: stl_reader_vertices(path) * 1.01
    with pytest.raises(A.ApplyError, match="differs"):
        A.check_target_node(edited, edited_root, path, source)

    group = Node(parent=root, group=True)
    child = Node(path, stl_reader_vertices(path), parent=group)
    with pytest.raises(A.ApplyError, match="top-level"):
        A.check_target_node(child, root, path, source)


class Operation:
    readback = {"status": "pass"}

    def redo(self):
        pass

    def push(self):
        pass


def recorded_bridge(target_object_id):
    bridge = CoreBridge(None, lambda: {})
    bridge._snapshot = lambda: {"signature": "same", "scene_signature": "scene"}
    bridge._plans["p"] = {"decision": {}, "generation": bridge._generation, "applied": False,
                          "request": {"signature": "same", "mesh_path": "C:/part.stl", "inputs": None,
                                      "geometry_sha256": "0" * 64, "target_object_id": target_object_id}}
    return bridge


def test_apply_reuses_the_planned_scene_object(monkeypatch):
    calls = []
    monkeypatch.setattr(A, "create_operation", lambda app, decision, path, **kw: calls.append(kw) or Operation())
    bridge = recorded_bridge(7)
    node = object()
    bridge.resolve_node = lambda object_id: node if object_id == 7 else None
    assert bridge.summary("p")["apply_target"] == {"object_id": 7, "in_place": True}
    assert bridge.apply({"plan_id": "p"})["status"] == "applied"
    assert calls == [{"target_node": node}]

    loaded = recorded_bridge(None)
    assert loaded.summary("p")["apply_target"]["requires_empty_project"] is True
    loaded.apply({"plan_id": "p"})
    assert calls[-1] == {"target_node": None}

    unresolved = recorded_bridge(7)
    with pytest.raises(ValueError, match="cannot be resolved"):
        unresolved.apply({"plan_id": "p"})


def test_target_id_is_application_owned():
    with pytest.raises(ValueError, match="target_object_id"):
        CoreBridge(None, lambda: {}).plan({"task_text": "TZ", "mesh_path": organizer_stl(), "target_object_id": True})

    class Core:
        calls = []

        def plan(self, args, context):
            self.calls.append(args)
            return {}

        def apply(self, args, context):
            raise AssertionError("planning must not apply")

        verify = summary = apply

    core = Core()
    registry = ToolRegistry(None, None, lambda: "", core=core)
    path = organizer_stl()
    registry.upload_sources = lambda: [{"id": "u1", "path": path, "name": "part.stl"}]
    outcome = registry.call("plan_print", {"task_text": "TZ", "attachment_id": "u1", "target_object_id": 3})
    assert outcome["ok"] and "target_object_id" not in core.calls[0]
