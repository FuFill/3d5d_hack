"""Real STE regression: saved poses must survive mode changes and delayed physics."""
import copy
import importlib

import numpy as np

from UM.Math.Matrix import Matrix
from UM.Math.Vector import Vector
from UM.Mesh.MeshBuilder import MeshBuilder
from UM.Scene.SceneNode import SceneNode
from steslicer.Scene.SteSlicerSceneNode import SteSlicerSceneNode
from UM.PluginRegistry import PluginRegistry


class TreePoseCheck:
    def __init__(self, app):
        self.app = app
        # Headless STE omits GUI physics; instantiate the installed implementation.
        if app._physics is None:
            from steslicer.PlatformPhysics import PlatformPhysics
            app._physics = PlatformPhysics(app.getController(), app.getBuildVolume())
        extension = PluginRegistry.getInstance().getPluginObject("AIAssistant")
        package = type(extension).__module__.rsplit(".", 1)[0]
        tools = importlib.import_module(package + ".agent.tools")
        versions = importlib.import_module(package + ".agent.versions")
        self.registry = tools.ToolRegistry(app, None, ".")
        self.state_io = versions.StateIO(app, self.registry)
        mesh = MeshBuilder()
        mesh.addCube(20, 20, 20)
        self.node = SteSlicerSceneNode()
        self.node.setName("tree-pose-test")
        self.node.setMeshData(mesh.build())
        # Use the actual decorators to exercise boundary checks and platform physics.
        from steslicer.Scene.SliceableObjectDecorator import SliceableObjectDecorator
        from steslicer.Scene.BuildPlateDecorator import BuildPlateDecorator
        self.node.addDecorator(SliceableObjectDecorator())
        self.node.addDecorator(BuildPlateDecorator(0))
        self.node.setSelectable(True)
        self.node.setParent(app.getController().getScene().getRoot())
        self.node.setPosition(Vector(30, 10, 40))
        app.getPreferences().setValue("physics/automatic_drop_down", True)
        app.getPreferences().setValue("physics/automatic_push_free", False)
        from steslicer.Scene.ZOffsetDecorator import ZOffsetDecorator
        offset = ZOffsetDecorator()
        offset.setZOffset(-7)
        self.node.addDecorator(offset)
        self.saved = self.state_io._capture()
        self.variants = []
        for mode, xyz in (("classic", (30, 10, 40)), ("cylindrical", (0, 0, -50)),
                          ("cylindrical_full", (5, 0, -30))):
            snapshot = copy.deepcopy(self.saved)
            snapshot["mode"] = mode
            snapshot["stacks"][0]["user"]["printing_mode"] = mode
            matrix = np.eye(4)
            matrix[:3, 3] = xyz
            for key in ("matrix", "world_matrix"):
                snapshot["objects"][0][key] = matrix.tolist()
            snapshot["objects"][0]["z_offset"] = None
            snapshot["physics"] = {"automatic_drop_down": mode == "classic", "automatic_push_free": False}
            self.variants.append(snapshot)
        # Even a 5D node captured while 3D auto-drop was on must stay on its rotary axis.
        self.variants[1]["physics"]["automatic_drop_down"] = True
        sunk = copy.deepcopy(self.variants[0])
        sunk["objects"][0]["matrix"][1][3] = 6
        sunk["objects"][0]["world_matrix"][1][3] = 6
        sunk["objects"][0]["z_offset"] = -4
        self.variants.append(sunk)
        floating = copy.deepcopy(self.variants[0])
        floating["objects"][0]["matrix"][1][3] = 25
        floating["objects"][0]["world_matrix"][1][3] = 25
        floating["physics"]["automatic_drop_down"] = False
        self.variants.append(floating)
        self.sequence = [("restore", 0), ("restore", 1), ("undo", 0), ("redo", 1),
                         ("restore", 0), ("restore", 2), ("restore", 1), ("restore", 2),
                         ("restore", 3), ("restore", 1), ("restore", 3), ("restore", 4),
                         ("restore", 0), ("restore", 1), ("restore", 0)]
        self.index = 0
        self.waiting = False
        self.results = []

    def advance(self):
        if self.waiting:
            # Force a normal physics tick as well as waiting for queued Qt events.
            self.node.setOutsideBuildArea(False)
            self.app._physics._onChangeTimerFinished()
            action, variant = self.sequence[self.index]
            expected = self.variants[variant]
            actual = self.node.getWorldTransformation().getData()
            matrix = expected["objects"][0]["world_matrix"]
            delta = float(np.max(np.abs(actual - matrix)))
            history = len(self.app.getOperationStack().getOperations())
            self.results.append({"action": action, "mode": expected["mode"], "error_mm": delta,
                                 "matrix": actual.tolist(),
                                 "pass": delta < 1e-4 and history == self.history_count,
                                 "extra_physics_operations": history - self.history_count,
                                 "bottom": float(self.node.getBoundingBox().bottom)})
            self.index += 1
            self.waiting = False
        if self.index == len(self.sequence):
            contracts = self._contracts()
            return {"status": "pass" if all(r["pass"] for r in self.results) else "fail",
                    "transitions": self.results, "contracts": contracts}
        action, variant = self.sequence[self.index]
        if action == "restore":
            self.state_io._restore(self.variants[variant])
        else:
            getattr(self.app.getOperationStack(), action)()
        self.history_count = len(self.app.getOperationStack().getOperations())
        self.waiting = True
        return None

    def _contracts(self):
        from steslicer.Scene.ZOffsetDecorator import ZOffsetDecorator
        results = []
        # Identical copies must retain their identities even if traversal order changes.
        duplicate = SteSlicerSceneNode()
        duplicate.setName(self.node.getName())
        duplicate.setMeshData(self.node.getMeshData())
        from steslicer.Scene.SliceableObjectDecorator import SliceableObjectDecorator
        from steslicer.Scene.BuildPlateDecorator import BuildPlateDecorator
        duplicate.addDecorator(SliceableObjectDecorator())
        duplicate.addDecorator(BuildPlateDecorator(0))
        duplicate.setParent(self.app.getController().getScene().getRoot())
        duplicate.setPosition(Vector(-40, 10, 40))
        saved = self.state_io._capture()
        original_nodes = self.registry._scene_nodes
        self.registry._scene_nodes = lambda: list(reversed(original_nodes()))
        self.node.setPosition(Vector(80, 10, 0))
        duplicate.setPosition(Vector(-80, 10, 0))
        self.state_io._restore(saved)
        self.registry._scene_nodes = original_nodes
        assert np.allclose(self.node.getWorldTransformation().getData(), saved["objects"][0]["world_matrix"])
        assert np.allclose(duplicate.getPosition().getData(), [-40, 10, 40])
        results.append("identical_copies_keep_identity")

        ambiguous = copy.deepcopy(saved)
        for item in ambiguous["objects"]:
            item.pop("id")
        history = self.app.getOperationStack()
        count = len(history.getOperations())
        try:
            self.state_io._restore(ambiguous)
        except Exception as exc:
            assert "uniquely" in str(exc)
        else:
            raise AssertionError("Ambiguous copies were silently swapped")
        assert len(history.getOperations()) == count
        results.append("ambiguous_legacy_copies_refused_before_changes")
        duplicate.setParent(None)

        # The same world pose must survive a change of parent coordinate frame.
        saved = self.state_io._capture()
        parent = SceneNode()
        parent.setParent(self.app.getController().getScene().getRoot())
        parent.setPosition(Vector(100, 0, 0))
        self.node.setParent(parent)
        self.state_io._restore(saved)
        assert np.allclose(self.node.getWorldTransformation().getData(), saved["objects"][0]["world_matrix"])
        self.node.setParent(self.app.getController().getScene().getRoot())
        parent.setParent(None)
        self.state_io._restore(saved)
        results.append("world_pose_independent_of_parent_frame")

        before = self.state_io._capture()
        count = len(history.getOperations())
        original_transform = self.node.setTransformation
        calls = [0]
        def fail_once(matrix):
            calls[0] += 1
            if calls[0] == 1:
                raise ValueError("injected restore failure")
            return original_transform(matrix)
        self.node.setTransformation = fail_once
        try:
            try:
                self.state_io._restore(self.variants[1])
            except ValueError as exc:
                assert str(exc) == "injected restore failure"
            else:
                raise AssertionError("Restore should have failed")
        finally:
            self.node.setTransformation = original_transform
        after = self.state_io._capture()
        assert len(history.getOperations()) == count
        assert before["mode"] == after["mode"] and before["physics"] == after["physics"]
        assert before["objects"] == after["objects"] and self.app._physics._enabled
        results.append("failed_restore_rolls_back_without_undo_entry")

        # Old chat nodes contain only a local matrix and no ZOffset/physics fields.
        legacy = copy.deepcopy(self.variants[0])
        legacy.pop("physics")
        for key in ("id", "world_matrix", "z_offset"):
            legacy["objects"][0].pop(key, None)
        legacy["stacks"][0]["user"].pop("printing_mode", None)
        offset = ZOffsetDecorator()
        offset.setZOffset(-9)
        self.node.addDecorator(offset)
        self.state_io._restore(legacy)
        self.node.setOutsideBuildArea(False)
        self.app._physics._onChangeTimerFinished()
        assert self.node.callDecoration("getZOffset") is None
        assert abs(self.node.getBoundingBox().bottom) < 1e-4
        assert self.app.getGlobalContainerStack().getProperty("printing_mode", "value") == "classic"
        results.append("legacy_snapshot_clears_stale_offset_and_restores_explicit_mode")

        # An in-place 5D plan swaps the centred mesh for the raw STL and adds batch copies.
        from UM.Mesh.MeshData import MeshData
        def world(node):
            matrix = node.getWorldTransformation().getData()
            return node.getMeshData().getVertices().dot(matrix[:3, :3].T) + matrix[:3, 3]
        root = self.app.getController().getScene().getRoot()
        start = self.state_io._capture()
        start_world = world(self.node)
        centred = self.node.getMeshData()
        self.node.setMeshData(MeshData(vertices=centred.getVertices() + np.float32([7, -3, 11]),
                                       file_name=centred.getFileName()))
        self.node.setPosition(Vector(-50, 0, 20))
        batch = SteSlicerSceneNode()
        batch.setName(self.node.getName() + " #2")
        batch.setMeshData(self.node.getMeshData())
        batch.addDecorator(SliceableObjectDecorator())
        batch.addDecorator(BuildPlateDecorator(0))
        batch.setParent(root)
        batch.setPosition(Vector(50, 0, 20))
        planned = self.state_io._capture()
        planned_world = world(self.node)
        known = set(o["id"] for x in (start, planned) for o in x["objects"])
        self.state_io._restore(start, known)
        assert np.allclose(world(self.node), start_world, atol=1e-4) and batch.getParent() is None
        self.state_io._restore(planned, known)
        assert np.allclose(world(self.node), planned_world, atol=1e-4) and batch.getParent() is root
        self.state_io._meshes.clear()  # another session: the saved mesh is gone, its size still matches
        self.state_io._restore(start, known)
        assert np.allclose(world(self.node), start_world, atol=1e-4) and batch.getParent() is None
        results.append("planned_mesh_and_batch_copies_follow_the_node")
        return results
