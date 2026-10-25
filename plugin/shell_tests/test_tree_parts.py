"""Model tree after a plan applied in place: the mesh and the set of parts follow the node.

The core replaces the centred mesh by the raw STL for 5D and adds batch copies and
modifier meshes; a saved matrix belongs to the mesh it was saved with. Minimal stand-ins
for Uranium's Matrix/SceneNode/Operation (the real ones: tree_pose_check.py in STE).
"""
import copy
from pathlib import Path
import sys
from types import ModuleType

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "AIAssistant"))
from agent import versions
from agent.versions import ModelTree, StateIO

P = np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]])


class Matrix:
    def __init__(self, data=None):
        self._data = np.array(data if data is not None else np.eye(4), dtype=float)

    def getData(self):
        return self._data

    def getInverse(self):
        return Matrix(np.linalg.inv(self._data))

    def multiply(self, other, copy=False):
        return Matrix(self._data.dot(other.getData()))

    def setByTranslation(self, vector):
        self._data = np.eye(4)
        self._data[:3, 3] = vector.getData()


class Vector:
    def __init__(self, x, y, z):
        self._data = np.array([x, y, z], dtype=float)

    def getData(self):
        return self._data


class Operation:
    pass


class Mesh:
    def __init__(self, vertices, file_name):
        self._vertices, self._file = np.asarray(vertices, dtype=np.float32), file_name

    def getVertices(self):
        return self._vertices

    def getFileName(self):
        return self._file


class Node:
    def __init__(self, name=None, mesh=None, parent=None, position=(0, 0, 0)):
        self._name, self._mesh, self._parent, self.children = name, mesh, None, []
        self._local = np.eye(4)
        self._local[:3, 3] = position
        if parent is not None:
            self.setParent(parent)

    def getName(self):
        return self._name

    def getMeshData(self):
        return self._mesh

    def setMeshData(self, mesh):
        self._mesh = mesh

    def getParent(self):
        return self._parent

    def setParent(self, parent):
        if self._parent is not None:
            self._parent.children.remove(self)
        self._parent = parent
        if parent is not None:
            parent.children.append(self)

    def getLocalTransformation(self):
        return Matrix(self._local.copy())

    def getWorldTransformation(self):
        parent = self._parent.getWorldTransformation().getData() if self._parent is not None else np.eye(4)
        return Matrix(parent.dot(self._local))

    def setTransformation(self, matrix):
        self._local = np.array(matrix.getData(), dtype=float)

    def callDecoration(self, name):
        return {"isSliceable": self._mesh is not None}.get(name)

    def getDecorator(self, kind):
        return None


class Prefs:
    def __init__(self):
        self.values = {"physics/automatic_drop_down": True, "physics/automatic_push_free": False}

    def getValue(self, key):
        return self.values.get(key)

    def setValue(self, key, value):
        self.values[key] = value


class Signal:
    def emit(self, *args):
        pass


class Container:
    def __init__(self, container_id="user"):
        self._id, self.values = container_id, {}

    def getId(self):
        return self._id

    def setMetaDataEntry(self, key, value):
        pass

    def setDefinition(self, definition):
        pass

    def setProperty(self, key, name, value):
        self.values[key] = value

    def getProperty(self, key, name):
        return self.values.get(key)

    def getAllKeys(self):
        return list(self.values)


class Containers:
    def __init__(self):
        self.all = dict((c.getId(), c) for c in (Container("quality_normal"), Container("generic_pla"), Container("e")))

    def addContainer(self, container):
        self.all[container.getId()] = container

    def findInstanceContainers(self, id):
        return [self.all[id]] if id in self.all else []


class Stack:
    """The global stack: the user container on top holds printing_mode."""
    propertyChanged = Signal()
    extruders = {}
    definition = type("Definition", (), {"getId": lambda self: "definition"})()

    def __init__(self):
        self.containers = [Container(), Container("quality_normal"), Container("generic_pla"), Container("e"),
                           Container("e"), Container("e"), Container("definition")]

    @property
    def userChanges(self):
        return self.containers[0]

    def getId(self):
        return "machine"

    def getProperty(self, key, name):
        return self.containers[0].values.get(key, "classic") if key == "printing_mode" else None

    def getContainers(self):
        return list(self.containers)

    def replaceContainer(self, index, container):
        self.containers[index] = container


class App:
    def __init__(self, root):
        self.root, self.prefs, self.history, self._physics = root, Prefs(), [], None
        self.SettingVersion, self.containers = 5, Containers()
        scene = type("Scene", (), {"getRoot": lambda s: root, "sceneChanged": Signal()})()
        self._controller = type("Controller", (), {"getScene": lambda s: scene})()

    def getController(self):
        return self._controller

    def getPreferences(self):
        return self.prefs

    def getOperationStack(self):
        return type("History", (), {"push": lambda s, operation: self.history.append(operation)})()

    def getContainerRegistry(self):
        return self.containers

    def getBackendManager(self):
        return None

    def getMachineManager(self):
        return type("Manager", (), {"activeQualityOrQualityChangesName": "Normal"})()


class Registry:
    def __init__(self, root):
        self.root, self.stack = root, Stack()
        self._in_place = type("InPlace", (), {})()
        self._in_place._applied = {"plan": {}}

    def _global(self):
        return self.stack

    def _all_keys(self):
        return []

    def _scene_nodes(self):
        found = []

        def walk(node):
            for child in node.children:
                if child.callDecoration("isSliceable"):
                    found.append(child)
                walk(child)
        walk(self.root)
        return found

    def _node_source(self, node):
        return {"source_path": node.getMeshData().getFileName() if node.getMeshData() else None}


@pytest.fixture
def scene(monkeypatch):
    for name, attrs in (("UM.Math.Matrix", {"Matrix": Matrix}), ("UM.Math.Vector", {"Vector": Vector}),
                        ("UM.Operations.Operation", {"Operation": Operation}),
                        ("UM.Settings.InstanceContainer", {"InstanceContainer": Container})):
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(versions, "run_on_main", lambda app, function: function())
    monkeypatch.setattr(StateIO, "_set_z_offset", staticmethod(lambda node, value: None))
    root = Node()
    app, registry = App(root), Registry(root)
    return root, app, registry, StateIO(app, registry)


def world(node):
    matrix = node.getWorldTransformation().getData()
    return np.round(node.getMeshData().getVertices().astype(float).dot(matrix[:3, :3].T) + matrix[:3, 3], 3)


def plate(registry):
    return sorted((n.getName(), world(n).tobytes()) for n in registry._scene_nodes())


def restore(io, snapshot, tree_snapshots):
    known = ModelTree({"nodes": [{"id": str(i), "snapshot": s} for i, s in enumerate(tree_snapshots)]}).object_ids()
    return io._restore(copy.deepcopy(snapshot), known)


def in_place_5d_plan(root, part):
    """What runtime_adapter.create_operation does to a loaded part: raw STL, planned matrix, copies, modifier."""
    raw = part.getMeshData().raw
    part.setMeshData(Mesh(raw, part.getMeshData().getFileName()))
    planned = np.eye(4)
    planned[:3, :3] = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    planned[:3, 3] = [-120, 3, 60]
    part.setTransformation(Matrix(planned))
    Node("infill_mesh", Mesh(raw[:9], "/m/gear.stl"), part, (1, 2, 3))
    return [Node("gear.stl #%d" % i, Mesh(raw, "/m/gear.stl"), root, (60 * i, 0, 0)) for i in (2, 3)]


def loaded_part(root):
    source = np.random.RandomState(1).rand(30, 3) * [40, 20, 10] + [100, 50, 0]  # far from the STL origin
    raw = source.dot(P.T)
    mesh = Mesh(raw - (raw.min(axis=0) + raw.max(axis=0)) / 2, "/m/gear.stl")  # the reader centres it
    mesh.raw = raw
    return Node("gear.stl", mesh, root, (10, 5, -20))


def test_a_part_keeps_its_place_when_the_plan_swapped_its_mesh(scene):
    root, app, registry, io = scene
    part = loaded_part(root)
    start = io._capture()
    on_start = plate(registry)
    copies = in_place_5d_plan(root, part)
    planned = io._capture()
    on_plan = plate(registry)
    tree = [start, planned]

    result = restore(io, start, tree)
    assert plate(registry) == on_start and result["removed"] == 3
    assert all(c.getParent() is None for c in copies) and not part.children
    assert registry._in_place._applied == {}  # no plan revert over another model's setup

    result = restore(io, planned, tree)
    assert plate(registry) == on_plan and result["returned"] == 3

    app.history[-1].undo()
    assert plate(registry) == on_start
    app.history[-1].redo()
    assert plate(registry) == on_plan


def test_without_the_saved_mesh_the_matrix_follows_the_mesh_offset(scene):
    root, app, registry, io = scene
    part = loaded_part(root)
    start = io._capture()
    on_start = plate(registry)
    in_place_5d_plan(root, part)
    planned = io._capture()
    on_plan = plate(registry)
    restore(io, start, [start, planned])
    io._meshes.clear()  # another session: only the size and centre of the saved mesh are known
    restore(io, planned, [start, planned])
    assert plate(registry) == on_plan  # the modifier keeps its place on the shifted parent too
    io._meshes.clear()
    restore(io, start, [start, planned])
    assert plate(registry) == on_start


def test_a_part_the_user_added_after_the_last_model_stays(scene):
    root, app, registry, io = scene
    part = loaded_part(root)
    start = io._capture()
    in_place_5d_plan(root, part)
    planned = io._capture()
    own = Node("bracket.stl", Mesh(np.eye(3) * 5, "/m/bracket.stl"), root, (0, 0, 80))
    restore(io, start, [start, planned])
    assert own.getParent() is root


class Agent:
    def __init__(self):
        self.notes = []

    def busy(self):
        return False

    def note(self, text):
        self.notes.append(text)

    def load(self, messages):
        pass


def test_switching_between_prepared_branches_restores_each_model(scene):
    """A tree with several branches made in advance; every switch to an older one restores it exactly."""
    from agent.events import EventBus
    from agent.server import ChatService
    root, app, registry, io = scene
    io.needs_slice, io.stats = (lambda: False), (lambda should_stop=None: {})
    service = ChatService(EventBus(), Agent(), None, None, None)
    service.state_io = io
    part = loaded_part(root)
    expected = {}

    def work(name, change):
        service.before_work()
        change()
        node = service.after_work(lambda rows: name)
        expected[node["id"]] = (plate(registry), registry.stack.getProperty("printing_mode", "value"))
        return node

    def turn(matrix_rows, position):
        matrix = np.eye(4)
        matrix[:3, :3], matrix[:3, 3] = matrix_rows, position
        part.setTransformation(Matrix(matrix))

    def five_d():
        in_place_5d_plan(root, part)
        registry.stack.userChanges.setProperty("printing_mode", "value", "cylindrical_full")

    def batch():
        turn([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], [0, 15, 0])
        for i in (2, 3, 4):
            Node("gear.stl #%d" % i, Mesh(part.getMeshData().getVertices(), "/m/gear.stl"), root, (50 * i, 15, 0))

    def modifier():
        turn([[1, 0, 0], [0, 0, -1], [0, 1, 0]], [30, 10, 30])
        Node("support_blocker", Mesh(np.eye(3) * 4, "/m/gear.stl"), part, (0, 3, 0))

    spiral = work("5D", five_d)
    start = service.tree.nodes[0]
    expected[start["id"]] = None  # filled below: the plate before the first work
    service.select_node(start["id"])
    expected[start["id"]] = (plate(registry), "classic")
    copies = work("Партия", batch)
    service.select_node(start["id"])
    turned = work("Поворот", modifier)
    deeper = work("Сдвиг", lambda: turn([[1, 0, 0], [0, 0, -1], [0, 1, 0]], [-40, 10, 30]))
    assert [n["parent"] for n in (spiral, copies, turned, deeper)] == [start["id"]] * 3 + [turned["id"]]

    def check(node):
        result = service.select_node(node["id"])
        assert result["node"] == node["id"] and result["missing_objects"] == 0 and service.tree.selected == node["id"]
        assert (plate(registry), registry.stack.getProperty("printing_mode", "value")) == expected[node["id"]], \
            node["name"]

    for node in (spiral, start, deeper, copies, spiral, turned, copies, start, deeper, spiral, deeper):
        check(node)
    app.history[-1].undo()  # Ctrl+Z after a switch: the model before it (5D)
    assert (plate(registry), registry.stack.getProperty("printing_mode", "value")) == expected[spiral["id"]]
    app.history[-1].redo()
    io._meshes.clear()  # meshes no longer kept: only their size and centre
    for node in (copies, spiral, start, turned, spiral, deeper):
        check(node)
    # A new branch from an old model, then back over every branch again.
    check(copies)
    more = work("Партия 5D", lambda: registry.stack.userChanges.setProperty("printing_mode", "value", "cylindrical"))
    assert more["parent"] == copies["id"]
    for node in (spiral, more, start, copies, deeper, more, turned):
        check(node)
