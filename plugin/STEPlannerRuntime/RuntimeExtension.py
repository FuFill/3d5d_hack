"""Local, bounded STE runtime commands; all scene actions run on the Qt thread."""
import hashlib
import inspect
import json
import os
import platform
import sys
import traceback
import time
import threading

from PyQt5.QtCore import QObject, QTimer, QThread, QUrl, QT_VERSION_STR, PYQT_VERSION_STR
from UM.Extension import Extension
from UM.PluginRegistry import PluginRegistry

sys.path.insert(0, os.path.dirname(__file__))


def _signature(method):
    try:
        return str(inspect.signature(method))
    except (ValueError, TypeError):
        return "Qt builtin"


class RuntimeExtension(QObject, Extension):
    def __init__(self, app):
        QObject.__init__(self)
        Extension.__init__(self)
        self.app = app
        self.index = 0
        self.busy = False
        self.halted = False
        self.pending = None
        self.operation = None
        self.decision = None
        self.last_backend_state = None
        self.last_change = time.monotonic()
        self.addMenuItem("Apply planner decision…", self.apply_dialog)
        self.addMenuItem("Runtime diagnostics", self.diagnostics)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.app.initializationFinished.connect(self.start)

    def start(self):
        # All initializationFinished listeners must complete before touching
        # backend preferences; another backend may still be initializing.
        QTimer.singleShot(2000, self.ready)

    def ready(self):
        try:
            stack = self.app.getGlobalContainerStack()
            if stack is None:
                QTimer.singleShot(1000, self.ready)
                return
            for s in [stack] + list(stack.extruders.values()):
                s.propertyChanged.connect(self.configuration_changed)
                s.containersChanged.connect(self.configuration_changed)
            self.app.getController().getScene().sceneChanged.connect(self.configuration_changed)
            if self.isolated():
                self.app.getPreferences().setValue("general/auto_slice", False)
            self.timer.start()
        except Exception:
            manifest = os.environ.get("STE_PLANNER_RUNTIME_JOB")
            if manifest:
                with open(os.path.join(os.path.dirname(manifest), "boot_error.txt"), "w") as handle:
                    handle.write(traceback.format_exc())

    def configuration_changed(self, *args):
        self.last_change = time.monotonic()

    def apply_dialog(self):
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(None, "Planner decision", "", "JSON (*.json)")
        if path:
            try:
                with open(path, encoding="utf-8") as handle:
                    decision = json.load(handle)
                self.apply(decision, decision["scene_operations"][0]["path"], False)
            except Exception as exc:
                from UM.Message import Message
                Message(str(exc), title="Planner: application refused").show()

    def isolated(self):
        from UM.Resources import Resources
        expected = os.environ.get("STE_PLANNER_ISOLATED_ROOT")
        return bool(expected and os.path.commonpath([os.path.realpath(expected),
                    os.path.realpath(Resources.getConfigStoragePath())]) == os.path.realpath(expected))

    def apply(self, decision, mesh, demo):
        from ste_planner.runtime_adapter import create_operation
        if demo and not self.isolated():
            raise ValueError("software demonstration requires an isolated profile")
        operation = create_operation(self.app, decision, mesh, demonstration=demo)
        operation.redo()  # Failed redo rolls back and is never pushed into undo history.
        operation.push()
        self.operation, self.decision = operation, decision
        return {"status": "applied_demo" if demo else "applied", "readback": operation.readback,
                "decision_sha256": hashlib.sha256(json.dumps(decision, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest(),
                "nodes": self.nodes(), "qt_thread": QThread.currentThread() == self.app.thread()}

    def nodes(self):
        import numpy as np
        def mesh_bounds(node):
            # getBoundingBox() can include children before STE asynchronously
            # classifies modifiers as non-printing. Use this node's actual mesh.
            matrix = node.getWorldTransformation().getData()
            world = node.getMeshData().getVertices().dot(matrix[:3, :3].T) + matrix[:3, 3]
            return [np.min(world, axis=0).tolist(), np.max(world, axis=0).tolist()]
        return [{"name": n.getName(), "parent": n.getParent().getName() if n.getParent() else None,
                 "matrix": n.getWorldTransformation().getData().tolist(),
                 "vertices": n.getMeshData().getVertexCount(),
                 "world_bounds": mesh_bounds(n),
                 "mesh_sha256": hashlib.sha256(n.getMeshData().getVertices().tobytes() + n.getMeshData().getIndices().tobytes()).hexdigest(),
                 "modifier": {k: n.callDecoration("getStack").getProperty(k, "value")
                              for k in ("support_mesh", "support_mesh_drop_down", "anti_overhang_mesh")}}
                for n in self.app.getController().getScene().getRoot().getAllChildren()
                if n.callDecoration("isSliceable") and n.getMeshData()]

    def _backend_state(self, state):
        self.last_backend_state = int(state)

    def export(self, command, project_ready=False):
        from ste_planner import gcode
        from ste_planner.runtime_adapter import file_sha256, readback
        scene = self.app.getController().getScene()
        plate = self.app.getMultiBuildPlateModel().activeBuildPlate
        lines = getattr(scene, "gcode_dict", {}).get(plate, [])
        text = "".join(lines)
        if not text.strip():
            raise ValueError("empty G-code is not a successful slice")
        stack = self.app.getGlobalContainerStack()
        ext = stack.extruders["0"]
        expected = {"printing_mode": ext.getProperty("printing_mode", "value"),
                    "nozzle_temperature": ext.getProperty("material_print_temperature", "value"),
                    "bed_temperature": ext.getProperty("material_bed_temperature", "value"),
                    "layer_height": ext.getProperty("layer_height", "value"),
                    "xy_centered": bool(stack.getProperty("machine_center_is_zero", "value")),
                    "build_volume": [stack.getProperty(k, "value") for k in ("machine_width", "machine_depth", "machine_height")]}
        import numpy as np
        model_nodes = self.operation.nodes if self.operation else [n for n in scene.getRoot().getChildren() if n.getMeshData()]
        tops = []
        for node in model_nodes:
            matrix = node.getWorldTransformation().getData()
            vertices = node.getMeshData().getVertices()
            world = vertices.dot(matrix[:3, :3].T) + matrix[:3, 3]
            tops.append(float(np.max(world[:, 1])))
        if tops:
            expected["model_top_z"] = max(tops)
        output = command["output_prefix"]
        with open(output + ".gcode", "w", encoding="utf-8") as handle:
            handle.write(text)
        if not project_ready:
            # Installed 7.10's workspace writer always queues to Qt and waits,
            # even if called from Qt. Invoke its wrapper from a worker so the
            # event loop remains free; native project serialization stays in Qt.
            writer = self.app.getWorkspaceFileHandler().getWriter("3MFWriter")
            nodes = scene.getRoot().getChildren()
            event, outcome = threading.Event(), {}
            def save_project():
                try:
                    with open(output + ".3mf", "wb") as handle:
                        if not writer.write(handle, nodes):
                            raise ValueError("STE project export failed")
                    outcome["ok"] = True
                except Exception as exc:
                    outcome["error"] = str(exc)
                finally:
                    event.set()
            self.pending = {"kind": "project", "command": command, "started": time.monotonic(),
                            "event": event, "outcome": outcome}
            worker = threading.Thread(target=save_project)
            worker.daemon = True
            worker.start()
            return None
        inspection = gcode.inspect_toolpath(text, expected)
        rb = readback(self.app, self.decision) if self.decision else None
        return {"status": "slice_completed", "backend": type(self.app.getBackend()).__name__,
                "backend_state": self.last_backend_state, "inspection": inspection, "expected": expected,
                "readback": rb, "nodes": self.nodes(),
                "gcode": {"path": os.path.basename(output + ".gcode"), "bytes": len(text.encode("utf-8")),
                          "sha256": file_sha256(output + ".gcode")},
                "project": {"path": os.path.basename(output + ".3mf"), "sha256": file_sha256(output + ".3mf")}}

    def execute(self, command):
        name = command["command"]
        if name == "state":
            from ste_planner.planner import READBACK_KEYS
            stack = self.app.getGlobalContainerStack()
            history = self.app.getOperationStack()
            return {"status": "computed", "nodes": self.nodes(),
                "containers": [[c.getId() for c in s.getContainers()] for s in [stack] + list(stack.extruders.values())],
                "actual": {k: stack.extruders["0"].getProperty(k, "value") for k in READBACK_KEYS},
                "backend": type(self.app.getBackend()).__name__,
                "history_count": len(history.getOperations()), "can_undo": history.canUndo(), "can_redo": history.canRedo()}
        if name == "inspect":
            return self.diagnostics()
        if name == "apply":
            with open(command["decision"], encoding="utf-8") as handle:
                decision = json.load(handle)
            return self.apply(decision, command["mesh"], command.get("demo", False))
        if name == "readback":
            from ste_planner.runtime_adapter import readback
            return readback(self.app, self.decision)
        if name in ("undo", "redo"):
            getattr(self.app.getOperationStack(), name)()
            return {"status": "computed", "nodes": self.nodes()}
        if name in ("move_test", "duplicate_test"):
            if not self.isolated() or not self.operation:
                raise ValueError("scene mutation test requires an isolated applied project")
            from UM.Math.Vector import Vector
            from ste_planner.runtime_adapter import PRINTER_TO_SCENE
            node = self.operation.nodes[0]
            vector = Vector(*PRINTER_TO_SCENE.dot(command["printer_delta_mm"]))
            if name == "move_test":
                from UM.Operations.TranslateOperation import TranslateOperation
                TranslateOperation(node, vector).push()
            else:
                from copy import deepcopy
                from UM.Operations.AddSceneNodeOperation import AddSceneNodeOperation
                node = deepcopy(node)
                node.translate(vector, node.TransformSpace.World)
                AddSceneNodeOperation(node, self.app.getController().getScene().getRoot()).push()
            return {"status": "computed", "nodes": self.nodes()}
        if name == "load_stock":
            if not self.isolated():
                raise ValueError("stock validation requires isolated profile")
            self.app.getGlobalContainerStack().setProperty("printing_mode", "value", "classic")
            self.app.readLocalFile(QUrl.fromLocalFile(command["mesh"]))
            self.pending = {"kind": "load", "command": command, "started": time.monotonic()}
            return None
        if name == "reset_test_scene":
            if not self.isolated():
                raise ValueError("reset is available only in the isolated validation profile")
            root = self.app.getController().getScene().getRoot()
            for node in list(root.getChildren()):
                if node.callDecoration("isSliceable"):
                    node.setParent(None)
            self.operation, self.decision = None, None
            self.app.getController().getScene().sceneChanged.emit(root)
            return {"status": "computed", "nodes": self.nodes()}
        if name == "slice":
            if self.decision:
                from ste_planner.runtime_adapter import readback
                rb = readback(self.app, self.decision)
                if rb["status"] != "pass":
                    self.operation.undo()
                    raise ValueError("deferred setting readback failed; rolled back")
            # Settings changes propagate asynchronously and invalidate slicing.
            # Wait for quiescence before starting the single requested job.
            self.last_change = time.monotonic()
            self.pending = {"kind": "settle", "command": command, "started": time.monotonic()}
            return None
        if name == "quit":
            QTimer.singleShot(500, self.app.quit)
            return {"status": "computed"}
        raise ValueError("unknown runtime command")

    def wait_pending(self):
        pending = self.pending
        if pending["kind"] == "settle" and time.monotonic() - self.last_change > 2.5:
            if self.decision:
                from ste_planner.runtime_adapter import readback
                if readback(self.app, self.decision)["status"] != "pass":
                    self.operation.undo()
                    raise ValueError("settled readback differs; application rolled back")
            backend = self.app.getBackend()
            self.last_backend_state = None
            backend.backendStateChange.connect(self._backend_state)
            self.pending["kind"] = "slice"
            backend.forceSlice()
            return None
        if pending["kind"] == "project" and pending["event"].is_set():
            self.pending = None
            if not pending["outcome"].get("ok"):
                raise ValueError(pending["outcome"].get("error", "project export failed"))
            return self.export(pending["command"], project_ready=True)
        if pending["kind"] == "load" and self.nodes() and time.monotonic() - pending["started"] > 3:
            self.pending = None
            return {"status": "loaded_stock", "nodes": self.nodes()}
        if pending["kind"] == "slice" and self.last_backend_state == 3:
            self.pending = None
            return self.export(pending["command"])
        if pending["kind"] == "slice" and self.last_backend_state == 4:
            self.pending = None
            raise ValueError("STE backend reported slicing error")
        if time.monotonic() - pending["started"] > pending["command"].get("timeout_seconds", 180):
            self.pending = None
            raise ValueError("STE runtime command timed out")
        return None

    def diagnostics(self):
        stack = self.app.getGlobalContainerStack()
        backends = self.app.getBackendManager().getBackends()
        install = os.path.dirname(sys.executable)
        engines = {}
        for name in ("STESlicer.exe", "python35.dll", "CuraEngine.exe", "DiscreteSlicer.exe", "glicer_console.exe", "CliParser.exe"):
            path = os.path.join(install, name)
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            engines[name] = {"sha256": digest, "bytes": os.path.getsize(path)}
        sources = {}
        kernel = os.path.join(os.path.dirname(__file__), "ste_planner")
        for name in sorted(os.listdir(kernel)):
            if name.endswith(".py"):
                with open(os.path.join(kernel, name), "rb") as handle:
                    sources[name] = hashlib.sha256(handle.read()).hexdigest()
        classes = {"application": self.app, "backend": self.app.getBackend(), "global_stack": stack}
        api = {}
        for label, obj in classes.items():
            api[label] = {"class": type(obj).__name__, "module": type(obj).__module__,
                          "methods": {name: _signature(getattr(obj, name))
                                      for name in dir(type(obj)) if name.startswith(("get", "set", "force", "write"))
                                      and callable(getattr(obj, name))}}
        return {"status": "computed", "os": platform.platform(), "python": sys.version,
                "ste_version": self.app.getVersion(), "qt": QT_VERSION_STR, "pyqt": PYQT_VERSION_STR,
                "qt_thread": QThread.currentThread() == self.app.thread(), "engines": engines,
                "loaded_kernel_source_sha256": sources,
                "machine": {"id": stack.getId(), "definition": stack.definition.getId(),
                            "extruders": {k: {"id": e.getId(), "material": e.material.getId(),
                                              "variant": e.variant.getId()} for k, e in stack.extruders.items()}},
                "material_api": _signature(self.app.getMachineManager().setMaterialById),
                "backends": {k: {"class": type(v).__name__, "methods": [n for n in dir(v) if n in
                              ("slice", "forceSlice", "stopSlicing", "backendStateChange", "processingProgress")]}
                             for k, v in backends.items()}, "api": api}

    def poll(self):
        if self.busy or self.halted:
            return
        self.busy = True
        try:
            self.poll_once()
        finally:
            self.busy = False

    def poll_once(self):
        manifest = os.environ.get("STE_PLANNER_RUNTIME_JOB")
        if not manifest or not os.path.isfile(manifest):
            return
        root, name = os.path.realpath(os.path.dirname(manifest)), "invalid"
        job = {}
        try:
            with open(manifest, encoding="utf-8") as handle:
                job = json.load(handle)
            root = os.path.realpath(os.path.dirname(manifest))
            commands = job.get("commands", [])
            if self.pending:
                name = self.pending["command"]["command"]
                result = self.wait_pending()
                if result is None:
                    return
            elif self.index >= len(commands):
                return
            else:
                command = commands[self.index]
                self.index += 1
                name = command["command"]
                result = self.execute(command)
                if result is None:
                    return
        except Exception as exc:
            result = {"status": "error", "error": type(exc).__name__, "message": str(exc),
                      "traceback": traceback.format_exc()}
            self.pending = None
            if job.get("abort_on_error", True):
                self.halted = True
                if self.isolated():
                    QTimer.singleShot(1000, self.app.quit)
        output = os.path.join(root, "%03d_%s.json" % (self.index, name))
        with open(output + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(output + ".tmp", output)
