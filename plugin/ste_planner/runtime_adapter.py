"""STE 7.10 scene/settings adapter. Qt imports are lazy; no second planner.

Coordinates: printer (X,Y,Z) -> scene (X,Z,-Y). The engine alone applies
the bed-origin offset. Modifier vertices already contain planner placement.
"""
import hashlib
import os
import uuid

import numpy as np

from . import geometry, functional

PRINTER_TO_SCENE = np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]])


class ApplyError(ValueError):
    pass


def sequential_triangles(vertices, faces):
    """Preserve triangles for 7.10 CLIParser's double-index preprocessing.

    That backend flattens v[f] and then uses the ORIGINAL f again. Sequential
    triangle indices make both passes identical, without geometry repair or the
    backend's off-by-one allocation for completely unindexed mesh data.
    """
    v, f = np.asarray(vertices), np.asarray(faces, int)
    return v[f].reshape(-1, 3), np.arange(f.size, dtype=np.int32).reshape(-1, 3)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scene_transform(rotation, translation):
    rotation, translation = np.asarray(rotation, float), np.asarray(translation, float)
    if (rotation.shape != (3, 3) or translation.shape != (3,) or
            not np.isfinite(rotation).all() or not np.isfinite(translation).all() or
            not np.allclose(rotation.T.dot(rotation), np.eye(3), atol=1e-7) or
            not abs(np.linalg.det(rotation) - 1) < 1e-7):
        raise ApplyError("finite proper rotation and translation required")
    matrix = np.eye(4)
    matrix[:3, :3] = PRINTER_TO_SCENE.dot(rotation).dot(PRINTER_TO_SCENE.T)
    matrix[:3, 3] = PRINTER_TO_SCENE.dot(translation)
    return matrix


def modifier_local_vertices(operation, parent_matrix):
    if operation.get("op") != "add_modifier_mesh" or operation.get("parent_model_index") != 0:
        raise ApplyError("modifier must belong to the planned source model")
    allowed = {"support_enforcer": {"support_mesh": True, "support_mesh_drop_down": True},
               "support_blocker": {"anti_overhang_mesh": True}}
    if operation.get("settings") != allowed.get(operation.get("role")):
        raise ApplyError("invalid modifier settings")
    mesh = geometry.Mesh(operation["vertices_mm"], operation["faces"])
    topology = geometry.edge_topology(mesh)
    if any(topology[k] for k in ("boundary_edges", "nonmanifold_edges", "duplicate_directed_edges")):
        raise ApplyError("modifier is not a closed outward-oriented volume")
    if not np.isfinite(mesh.vertices).all() or geometry.signed_volume(mesh) <= 1e-10:
        raise ApplyError("modifier has invalid or degenerate volume")
    # Coordinates are already placed in printer space. Undo the parent's scene
    # transform here, then normal parenting follows future movement/duplication.
    world = mesh.vertices.dot(PRINTER_TO_SCENE.T)
    inverse = np.linalg.inv(parent_matrix)
    local = world.dot(inverse[:3, :3].T) + inverse[:3, 3]
    return local, mesh.faces


def preflight(decision, mesh_path, machine_definition, demonstration=False, analysis_only=False):
    context = decision.get("calibration_context") or {}
    mode = decision.get("route", {}).get("mode")
    if mode != "classic" and not (analysis_only and mode in ("cylindrical", "cylindrical_full")):
        raise ApplyError("needs_input: 5D requires machine kinematics, head/mandrel geometry and full trajectory collision checks")
    if context.get("machine_id") != machine_definition or context.get("printing_mode") != mode:
        raise ApplyError("machine/mode context mismatch")
    if file_sha256(mesh_path) != context.get("geometry_sha256"):
        raise ApplyError("source STL SHA-256 differs")
    if (decision.get("chosen_material") or {}).get("id") != context.get("material_id"):
        raise ApplyError("material context mismatch")
    if decision.get("setting_patch", {}).get("errors"):
        raise ApplyError("planner patch contains errors")
    payload = decision.get("setting_patch", {}).get("payload", {})
    assigned = dict(payload.get("global", {}))
    assigned.update(payload.get("extruders", {}).get("0", {}))
    for key, value in assigned.items():
        if key in decision.get("effective_settings", {}) and not _equal(value, decision["effective_settings"][key]):
            raise ApplyError("compiled patch disagrees with expected readback: " + key)
    if decision.get("status") == "infeasible" and not analysis_only:
        raise ApplyError("infeasible plan cannot be applied")
    result = decision.get("functional_analysis") or {}
    value = result.get("value") or {}
    if value.get("selected") and not value.get("applied") and not demonstration and not analysis_only:
        raise ApplyError("functional recommendation is not applicable; use an isolated software demonstration")
    operations = decision.get("scene_operations") or []
    if not operations or operations[0].get("op") != "load_mesh":
        raise ApplyError("source load operation required")
    transforms = [op for op in operations if op.get("op") == "set_transform"]
    if len(transforms) != 1:
        raise ApplyError("one model transform required")
    scene_transform(transforms[0]["rotation"], transforms[0]["translation"])
    if not np.allclose(np.asarray(context.get("rotation"), float), np.asarray(transforms[0]["rotation"], float), atol=1e-7):
        raise ApplyError("orientation differs from calibration context")
    return context


def _equal(a, b):
    if isinstance(a, (float, int)) and not isinstance(a, bool) and isinstance(b, (float, int)) and not isinstance(b, bool):
        return abs(a - b) <= 1e-6 * max(1., abs(a), abs(b))
    return a == b


def readback(app, decision):
    global_stack = app.getGlobalContainerStack()
    extruder = global_stack.extruders["0"]
    expected = dict(decision["effective_settings"])
    value = (decision.get("functional_analysis") or {}).get("value") or {}
    if value.get("applied"):
        # The selected profile may include fields beyond the old readback list.
        selected = value.get("selected") or {}
        expected.update(value.get("selected_process_settings") or {})
    keys = set(expected) | set(functional.PROCESS_KEYS)
    actual = dict((key, extruder.getProperty(key, "value")) for key in sorted(keys))
    differences = [{"key": k, "expected": v, "actual": actual.get(k)}
                   for k, v in sorted(expected.items()) if not _equal(actual.get(k), v)]
    missing = sorted(set(functional.PROCESS_KEYS) - set(expected))
    if missing:
        differences.append({"missing_expected_process_keys": missing})
    context = decision.get("calibration_context") or {}
    if global_stack.definition.getId() != context.get("machine_id"):
        differences.append({"machine_context_changed": global_stack.definition.getId()})
    material_file = (decision.get("chosen_material") or {}).get("slicer_file", "")
    material_id = os.path.basename(material_file).replace(".xml.fdm_material", "").replace(".fdm_material", "")
    if extruder.material.getId() != material_id:
        differences.append({"material_context_changed": extruder.material.getId(), "expected": material_id})
    expected_backend = app.getBackendManager().getBackendByType(expected.get("printing_mode"))
    if app.getBackend() is not expected_backend:
        differences.append({"backend": type(app.getBackend()).__name__, "expected_mode": expected.get("printing_mode"),
                            "global_mode": global_stack.getProperty("printing_mode", "value")})
    return {"status": "pass" if not differences else "fail", "actual": actual,
            "differences": differences, "machine_definition": global_stack.definition.getId(),
            "material_id": extruder.material.getId(), "quality_id": extruder.quality.getId()}


def create_operation(app, decision, mesh_path, demonstration=False, analysis_only=False):
    """Stage an operation without touching the existing scene or stack contents.

    Caller must redo/check before pushing to the Uranium undo stack: push()
    itself records an operation before invoking redo(), including failed ones.
    """
    from PyQt5.QtCore import QThread
    from UM.Resources import Resources
    from UM.Math.Matrix import Matrix
    from UM.Mesh.MeshData import MeshData
    from UM.Operations.Operation import Operation
    from UM.Settings.InstanceContainer import InstanceContainer
    from steslicer.Scene.SteSlicerSceneNode import SteSlicerSceneNode
    from steslicer.Scene.BuildPlateDecorator import BuildPlateDecorator
    from steslicer.Scene.SliceableObjectDecorator import SliceableObjectDecorator
    from steslicer.Scene.ConvexHullDecorator import ConvexHullDecorator
    from steslicer.Settings import steslicer_empty_instance_containers as empty

    if QThread.currentThread() != app.thread():
        raise ApplyError("Qt thread required")
    if demonstration or analysis_only:
        isolated_root = os.environ.get("STE_PLANNER_ISOLATED_ROOT")
        if not isolated_root or os.path.commonpath([os.path.realpath(isolated_root),
                os.path.realpath(Resources.getConfigStoragePath())]) != os.path.realpath(isolated_root):
            raise ApplyError("software fixture may be applied only in the isolated validation profile")
    stack = app.getGlobalContainerStack()
    preflight(decision, mesh_path, stack.definition.getId(), demonstration, analysis_only)
    scene = app.getController().getScene()
    # Global settings affect all plates. Require a clean project instead of
    # silently modifying settings for pre-existing models on another plate.
    if any(n.callDecoration("isSliceable") for n in scene.getRoot().getAllChildren()):
        raise ApplyError("open a new project before applying a complete process plan")
    source = geometry.load_stl(mesh_path)
    transform = next(op for op in decision["scene_operations"] if op["op"] == "set_transform")
    matrix = scene_transform(transform["rotation"], transform["translation"])
    modifiers = [op for op in decision["scene_operations"] if op["op"] == "add_modifier_mesh"]
    modifier_meshes = [(op, modifier_local_vertices(op, matrix)) for op in modifiers]
    material_file = (decision.get("chosen_material") or {}).get("slicer_file")
    if not material_file:
        raise ApplyError("installed material file required")
    material_id = os.path.basename(material_file).replace(".xml.fdm_material", "").replace(".fdm_material", "")
    materials = app.getContainerRegistry().findInstanceContainers(id=material_id)
    if len(materials) != 1 or materials[0].getMetaDataEntry("type") != "material":
        raise ApplyError("unique installed material container required: " + material_id)
    material = materials[0]
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    if plate < 0:
        plate = 0
    count = next((int(op["count"]) for op in decision["scene_operations"] if op["op"] == "multiply"), 1)
    if not 1 <= count <= 256:
        raise ApplyError("batch count outside supported bounds")
    placed = source.vertices.dot(np.asarray(transform["rotation"]).T)
    size = placed.max(axis=0) - placed.min(axis=0)
    width, depth = (float(stack.getProperty(k, "value")) for k in ("machine_width", "machine_depth"))
    nx, ny = int((width - 20) // (size[0] + 10)), int((depth - 20) // (size[1] + 10))
    if nx < 1 or ny < 1 or count > nx * ny:
        raise ApplyError("planned batch cannot fit with the adapter's reserved clearances")
    nodes = []

    def node_from_mesh(vertices, faces, name):
        node = SteSlicerSceneNode(name=name)
        if decision["route"]["mode"] != "classic":
            vertices, faces = sequential_triangles(vertices, faces)
        node.setMeshData(MeshData(vertices=np.asarray(vertices, dtype=np.float32),
                                  indices=np.asarray(faces, dtype=np.int32), file_name=mesh_path))
        node.setSelectable(True)
        node.addDecorator(BuildPlateDecorator(plate))
        node.addDecorator(SliceableObjectDecorator())
        node.addDecorator(ConvexHullDecorator())
        node.callDecoration("setActiveExtruder", stack.extruders["0"].getId())
        return node

    for i in range(count):
        label = os.path.basename(mesh_path) + (" #" + str(i + 1) if count > 1 else "")
        node = node_from_mesh(source.vertices.dot(PRINTER_TO_SCENE.T), source.faces, label)
        local_matrix = matrix.copy()
        if count > 1:
            columns = min(nx, count)
            rows = int(np.ceil(count / float(columns)))
            offset = np.array([(i % columns - (columns - 1) / 2.) * (size[0] + 10),
                               (i // columns - (rows - 1) / 2.) * (size[1] + 10), 0.])
            local_matrix[:3, 3] += PRINTER_TO_SCENE.dot(offset)
        node.setTransformation(Matrix(local_matrix))
        for op, (vertices, faces) in modifier_meshes:
            child = node_from_mesh(vertices, faces, op["role"] + (" #" + str(i + 1) if count > 1 else ""))
            child.setParent(node)
            child.callDecoration("getStack").getTop().setDefinition(stack.definition.getId())
            for key, setting in op["settings"].items():
                child.callDecoration("getStack").getTop().setProperty(key, "value", setting)
        nodes.append(node)

    class PlanOperation(Operation):
        def __init__(self):
            Operation.__init__(self)
            self.nodes, self.active = nodes, False
            self.stacks = [stack] + list(stack.extruders.values())
            self.before = [list(s.getContainers()) for s in self.stacks]
            self.after = []
            self.old_plate = app.getMultiBuildPlateModel().activeBuildPlate
            self.old_gravity = app.getPreferences().getValue("physics/automatic_drop_down")
            for s, containers in zip(self.stacks, self.before):
                containers = list(containers)
                user = InstanceContainer("ste_planner_user_" + uuid.uuid4().hex)
                user.setMetaDataEntry("type", "user")
                user.setMetaDataEntry("setting_version", app.SettingVersion)
                # Per-extruder user overrides reference the complete machine
                # definition; fdmextruder alone does not define wall/infill keys.
                user.setDefinition(stack.definition.getId())
                app.getContainerRegistry().addContainer(user)
                quality = InstanceContainer("ste_planner_quality_" + uuid.uuid4().hex)
                quality.setMetaDataEntry("type", "quality")
                quality.setMetaDataEntry("setting_version", app.SettingVersion)
                quality.setName("Planner process profile")
                quality.setMetaDataEntry("quality_type", "normal")
                quality.setMetaDataEntry("supported", True)
                quality.setDefinition(stack.definition.getId())
                app.getContainerRegistry().addContainer(quality)
                containers[0], containers[1], containers[2] = user, empty.empty_quality_changes_container, quality
                # Drop non-calibrated nozzle/buildplate/definition overrides; all
                # old container references remain available to undo exactly.
                containers[4], containers[5] = empty.empty_variant_container, empty.empty_definition_changes_container
                if s is not stack:
                    containers[3] = material
                self.after.append(containers)
            payload = decision["setting_patch"]["payload"]
            for key, value in payload.get("global", {}).items():
                self.after[0][0].setProperty(key, "value", value)
            for index, s in enumerate(self.stacks[1:], 1):
                position = s.getMetaDataEntry("position")
                for key, value in payload.get("extruders", {}).get(str(position), {}).items():
                    self.after[index][0].setProperty(key, "value", value)
            # Pin the planned independent process profile over a previously
            # selected UI quality profile. Never pin derived wall thickness.
            main_index = self.stacks.index(stack.extruders["0"])
            self.profile_assignments = {}
            for key in functional.PROCESS_KEYS:
                expected = decision["effective_settings"][key]
                target = main_index if stack.getProperty(key, "settable_per_extruder") else 0
                self.after[target][0].setProperty(key, "value", expected)
                self.profile_assignments[key] = expected

        def _containers(self, snapshots):
            for s, containers in zip(self.stacks, snapshots):
                for index, container in enumerate(containers[:-1]):
                    s.replaceContainer(index, container)
            # Replacing a container does not emit an individual setting change.
            # BackendManager listens to this event to switch the actual engine.
            stack.propertyChanged.emit("printing_mode", "value")
            # STE 7.10 uses a queued ContainerStack signal. Dispatch the installed
            # manager's normal setting handler on Qt before synchronous readback.
            manager = app.getBackendManager()
            handler = getattr(manager, "_onSettingChanged", None)
            if not callable(handler):
                raise ApplyError("unsupported STE backend dispatcher")
            handler("printing_mode", "value")

        def redo(self):
            if self.active:
                return
            try:
                self._containers(self.after)
                app.getMultiBuildPlateModel().setActiveBuildPlate(plate)
                if self.old_gravity is not None:
                    app.getPreferences().setValue("physics/automatic_drop_down", False)
                for node in self.nodes:
                    node.setParent(scene.getRoot())
                    app.getBuildVolume().checkBoundsAndUpdate(node)
                    if node.isOutsideBuildArea():
                        raise ApplyError("transformed model/batch is outside the actual build area")
                self.active = True
                self.readback = readback(app, decision)
                if self.readback["status"] != "pass":
                    raise ApplyError("effective setting readback differs: " + str(self.readback["differences"]))
                scene.sceneChanged.emit(self.nodes[0])
            except Exception:
                self.undo()
                raise

        def undo(self):
            for node in self.nodes:
                node.setParent(None)
            self._containers(self.before)
            app.getMultiBuildPlateModel().setActiveBuildPlate(self.old_plate)
            if self.old_gravity is not None:
                app.getPreferences().setValue("physics/automatic_drop_down", self.old_gravity)
            self.active = False
            scene.sceneChanged.emit(scene.getRoot())

    return PlanOperation()
