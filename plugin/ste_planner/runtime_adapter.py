"""STE 7.10 scene/settings adapter. Qt imports are lazy; no second planner.

Coordinates: printer (X,Y,Z) -> scene (X,Z,-Y). The engine alone applies
the bed-origin offset. Modifier vertices already contain planner placement.
"""
import hashlib
import os
import uuid

import numpy as np

from . import geometry, functional, presets

PRINTER_TO_SCENE = np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]])
EMPTY_IDS = ("empty_quality_changes", "empty_quality", "empty_material", "empty_variant", "empty_definition_changes")


class ApplyError(ValueError):
    pass


# ---------------------------------------------------------------- native preset catalog (Qt thread)

def _json_value(value):
    if type(value).__name__ == "SettingFunction":
        return str(value)               # "=expression", the form stored in .inst.cfg
    if isinstance(value, (bool, str)) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if not isinstance(value, float) or value == value else None
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return str(value)


def catalog_entry(app, container, kind):
    """Serialise one instance container exactly like the worker expects (digest included)."""
    if container is None or container.getId() in EMPTY_IDS or container.getId().startswith("empty_"):
        return None
    meta = container.getMetaData()
    entry = {"id": container.getId(), "type": meta.get("type", kind), "name": container.getName(),
             "definition": meta.get("definition"),
             "values": dict((key, _json_value(container.getProperty(key, "value")))
                            for key in sorted(container.getAllKeys()))}
    for key in ("quality_type", "variant", "buildplate", "position"):
        if meta.get(key) is not None:
            entry[key] = str(meta[key])
    if kind == "quality":
        if meta.get("material") is not None:
            entry["material"] = str(meta["material"])
        entry["global_quality"] = str(meta.get("global_quality", False)).lower() in ("true", "1", "yes")
        if meta.get("weight") is not None:
            entry["weight"] = int(meta["weight"])
    if meta.get("setting_version") is not None:
        entry["setting_version"] = int(meta["setting_version"])
    if kind == "material":
        manager = app.getMaterialManager()
        base = meta.get("base_file", container.getId())
        entry.update({"base_file": base, "guid": meta.get("GUID"), "brand": meta.get("brand"),
                      "material_type": meta.get("material"), "approximate_diameter": meta.get("approximate_diameter"),
                      "root_material_id": manager.getRootMaterialIDWithoutDiameter(base),
                      "fallback_root_ids": list(manager.getFallBackMaterialIdsByMaterial(container) or [])})
    entry["digest"] = presets.container_digest(entry)
    return entry


def build_preset_catalog(app):
    """Read-only snapshot of the installed registry for the active printer; Qt thread only.

    Reads definitions, nozzle variants, materials, quality and quality_changes
    through the public managers; it never changes the active project.
    """
    from steslicer.Machines.QualityManager import getMachineDefinitionIDForQualitySearch
    stack = app.getGlobalContainerStack()
    if stack is None:
        raise ApplyError("Configure a printer before planning")
    registry = app.getContainerRegistry()
    definition = stack.definition
    quality_definition = getMachineDefinitionIDForQualitySearch(definition)
    materials = app.getMaterialManager()
    extruders = {}
    for position, extruder in sorted(stack.extruders.items()):
        available = []
        for root_id, node in sorted((materials.getAvailableMaterialsForMachineExtruder(stack, extruder) or {}).items()):
            container = node.getContainer()
            if container is not None:
                available.append(catalog_entry(app, container, "material"))
        extruders[str(position)] = {
            "stack_id": extruder.getId(), "definition_id": extruder.definition.getId(),
            "enabled": bool(extruder.isEnabled),
            "variant": catalog_entry(app, extruder.variant, "variant"),
            "definition_changes": catalog_entry(app, extruder.definitionChanges, "definition_changes"),
            "nozzle_size": _json_value(extruder.getProperty("machine_nozzle_size", "value")),
            "material_diameter": _json_value(extruder.getProperty("material_diameter", "value")),
            "available_materials": available,
            "current_material": catalog_entry(app, extruder.material, "material")}
    qualities = []
    for meta in registry.findContainersMetadata(type="quality"):
        if meta.get("id") == "empty_quality" or meta.get("definition") not in (quality_definition, "fdmprinter"):
            continue
        found = registry.findInstanceContainers(id=meta["id"])
        if len(found) == 1:
            qualities.append(catalog_entry(app, found[0], "quality"))
    quality_changes = []
    for name, group in sorted(app.getQualityManager().getQualityChangesGroups(stack).items()):
        node = group.node_for_global
        global_entry = catalog_entry(app, node.getContainer() if node else None, "quality_changes")
        quality_changes.append({
            "name": name, "quality_type": group.quality_type, "origin": "user",
            "definition": (global_entry or {}).get("definition"), "global": global_entry,
            "extruders": dict((str(p), catalog_entry(app, n.getContainer(), "quality_changes"))
                              for p, n in group.nodes_for_extruders.items() if n is not None)})
    try:
        buildplate = stack.getBuildplateName()
    except AttributeError:
        buildplate = None
    return {"schema": presets.CATALOG_SCHEMA, "source": "ste_runtime", "ste_version": app.getVersion(),
            "setting_version": int(app.SettingVersion),
            "machine": {"definition_id": definition.getId(), "stack_id": stack.getId(),
                        "quality_definition_id": quality_definition,
                        "has_machine_quality": str(definition.getMetaDataEntry("has_machine_quality", False)).lower() == "true",
                        "has_variants": str(definition.getMetaDataEntry("has_variants", False)).lower() == "true",
                        "preferred_quality_type": definition.getMetaDataEntry("preferred_quality_type"),
                        "buildplate_name": buildplate,
                        "variant": catalog_entry(app, stack.variant, "variant"),
                        "definition_changes": catalog_entry(app, stack.definitionChanges, "definition_changes")},
            "extruders": extruders, "qualities": qualities, "quality_changes": quality_changes}


def native_container(app, ref, index):
    """Registry container for a recorded base reference; the content digest must be unchanged."""
    from steslicer.Settings import steslicer_empty_instance_containers as empty
    if ref is None:
        return getattr(empty, EMPTY_IDS[index - 1] + "_container")
    found = app.getContainerRegistry().findInstanceContainers(id=ref["id"])
    if len(found) != 1:
        raise ApplyError("stale plan: base container %s is missing or ambiguous" % ref["id"])
    kind = presets.STACK_LAYERS[index - 1]
    entry = catalog_entry(app, found[0], kind)
    if entry is None or entry["digest"] != ref["digest"]:
        raise ApplyError("stale plan: base container %s changed since planning" % ref["id"])
    return found[0]


def check_native_stacks(stack, record):
    """Differences between the actual stacks (indices 1..5) and the recorded base P."""
    differences = []
    parts = [(None, stack)] + [(pos, stack.extruders.get(pos)) for pos in sorted(record["extruders"])]
    for position, s in parts:
        if s is None:
            differences.append({"missing_extruder": position})
            continue
        expected = presets.stack_ids(record, position)
        actual = [c.getId() for c in s.getContainers()[1:6]]
        for index, (want, have) in enumerate(zip(expected, actual), 1):
            want = want or EMPTY_IDS[index - 1]
            if have != want:
                differences.append({"stack": "global" if position is None else "extruder_" + position,
                                    "layer": presets.STACK_LAYERS[index - 1], "expected": want, "actual": have})
    return differences


def sequential_triangles(vertices, faces):
    """Preserve triangles for 7.10 CLIParser's double-index preprocessing.

    That backend flattens v[f] and then uses the ORIGINAL f again. Sequential
    triangle indices make both passes identical, without geometry repair or the
    backend's off-by-one allocation for completely unindexed mesh data.
    """
    v, f = np.asarray(vertices), np.asarray(faces, int)
    return v[f].reshape(-1, 3), np.arange(f.size, dtype=np.int32).reshape(-1, 3)


def source_mesh_matches(vertices, indices, source):
    """Whether a loaded node's local mesh is the planned source STL.

    Uranium's STLReader stores every facet, in file order, as three vertices in
    scene axes (x, z, -y), without centring; position, rotation and auto-scale
    live only in the node transformation. A repaired, decimated, reordered or
    other mesh does not match, and apply refuses rather than guessing.
    """
    v = np.asarray(vertices, dtype=float)
    if indices is None:
        if v.ndim != 2 or v.shape[1] != 3 or len(v) % 3:
            return False
        triangles = v.reshape(-1, 3, 3)
    else:
        f = np.asarray(indices, dtype=int)
        if f.ndim != 2 or f.shape[1] != 3 or (f.size and (f.min() < 0 or f.max() >= len(v))):
            return False
        triangles = v[f]
    expected = source.triangles.dot(PRINTER_TO_SCENE.T)
    if triangles.shape != expected.shape:
        return False
    tolerance = 1e-4 * max(1.0, float(np.abs(expected).max()) if expected.size else 1.0)
    return bool(np.allclose(triangles, expected, rtol=0.0, atol=tolerance))


def check_target_node(target, scene_root, mesh_path, source):
    """The loaded object may replace the empty project only when the plan alone governs it.

    Global settings affect every model on every plate, so the scene must hold only
    this object; its own modifier meshes or per-object settings were not planned.
    """
    sliceable = [n for n in scene_root.getAllChildren() if n.callDecoration("isSliceable")]
    if target not in sliceable or target.getParent() is not scene_root or target.callDecoration("isGroup"):
        raise ApplyError("the planned object is no longer a top-level model in the scene; plan again")
    own = [n for n in target.getAllChildren() if n.getMeshData() is not None]
    if own:
        raise ApplyError("the object has its own modifier/support meshes; remove them or plan a new project")
    others = [n for n in sliceable if n is not target]
    if others:
        raise ApplyError("the project contains other models (%d); the plan writes global settings for every "
                         "model, so keep only the planned object or open a new project" % len(others))
    stack = target.callDecoration("getStack")
    if stack is not None and stack.getTop() is not None and stack.getTop().getAllKeys():
        raise ApplyError("the object has per-object settings that the plan did not consider; "
                         "reset them or plan a new project")
    mesh = target.getMeshData()
    if mesh is None or os.path.normcase(os.path.abspath(mesh.getFileName() or "")) != \
            os.path.normcase(os.path.abspath(mesh_path)):
        raise ApplyError("the object was not loaded from the planned STL")
    if not source_mesh_matches(mesh.getVertices(), mesh.getIndices(), source):
        raise ApplyError("the object's mesh differs from the planned STL (edited, repaired or reloaded)")


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
    record = decision.get("base_presets")
    if record is not None:
        if record.get("contract") != presets.CONTRACT:
            raise ApplyError("unsupported preset contract: %s" % record.get("contract"))
        if record["machine"]["definition_id"] != machine_definition:
            raise ApplyError("stale plan: base presets belong to another machine")
        if not presets.same_base(presets.base_identity(record), context.get("preset_base")):
            raise ApplyError("preset base differs from the calibration context")
    elif (decision.get("preset_selection") or {}).get("status") not in (None, "not_requested"):
        raise ApplyError("needs_input: no compatible native preset base was selected")
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
    record = decision.get("base_presets")
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
    if record is not None:
        differences.extend(check_native_stacks(global_stack, record))
        material_id = record["extruders"]["0"]["material"]["id"]
    else:
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


def create_operation(app, decision, mesh_path, demonstration=False, analysis_only=False, target_node=None):
    """Stage an operation without touching the existing scene or stack contents.

    target_node: the loaded scene object of the planned STL. It is reoriented in
    place (its old transform, extruder, plate and mesh are restored by undo)
    instead of loading a second copy; batch copies and modifiers are added to it.
    Without it the project must be empty and the source is loaded.

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
    source = geometry.load_stl(mesh_path)
    if target_node is not None:
        # The model already on the plate becomes the planned part in place.
        check_target_node(target_node, scene.getRoot(), mesh_path, source)
    elif any(n.callDecoration("isSliceable") for n in scene.getRoot().getAllChildren()):
        # Global settings affect all plates: never retune models the plan did not consider.
        raise ApplyError("the project already contains models; plan with the object_id of the loaded model "
                         "(list_model_sources) or open a new project")
    transform = next(op for op in decision["scene_operations"] if op["op"] == "set_transform")
    matrix = scene_transform(transform["rotation"], transform["translation"])
    modifiers = [op for op in decision["scene_operations"] if op["op"] == "add_modifier_mesh"]
    modifier_meshes = [(op, modifier_local_vertices(op, matrix)) for op in modifiers]
    record = decision.get("base_presets")
    native = {}
    if record is not None:
        # Resolve and verify every container of P before staging anything.
        for s in [stack] + list(stack.extruders.values()):
            position = None if s is stack else s.getMetaDataEntry("position")
            if position is not None and str(position) not in record["extruders"]:
                raise ApplyError("stale plan: extruder %s is not part of the planned base" % position)
            refs = presets.stack_refs(record, position)
            native[s.getId()] = [native_container(app, ref, index) for index, ref in enumerate(refs, 1)]
        material = None
    else:
        material_file = (decision.get("chosen_material") or {}).get("slicer_file")
        if not material_file:
            raise ApplyError("installed material file required")
        material_id = os.path.basename(material_file).replace(".xml.fdm_material", "").replace(".fdm_material", "")
        materials = app.getContainerRegistry().findInstanceContainers(id=material_id)
        if len(materials) != 1 or materials[0].getMetaDataEntry("type") != "material":
            raise ApplyError("unique installed material container required: " + material_id)
        material = materials[0]
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    if target_node is not None and (target_node.callDecoration("getBuildPlateNumber") or 0) >= 0:
        plate = target_node.callDecoration("getBuildPlateNumber") or 0
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

    target = None       # (node, planned Matrix, old state) of the reused scene object
    target_children = []  # modifiers of the reused object: attached on redo, detached on undo
    for i in range(count):
        label = os.path.basename(mesh_path) + (" #" + str(i + 1) if count > 1 else "")
        local_matrix = matrix.copy()
        if count > 1:
            columns = min(nx, count)
            rows = int(np.ceil(count / float(columns)))
            offset = np.array([(i % columns - (columns - 1) / 2.) * (size[0] + 10),
                               (i // columns - (rows - 1) / 2.) * (size[1] + 10), 0.])
            local_matrix[:3, 3] += PRINTER_TO_SCENE.dot(offset)
        if i == 0 and target_node is not None:
            node = target_node
            old_mesh = node.getMeshData()
            new_mesh = None
            if decision["route"]["mode"] != "classic":
                # Same geometry, indexed like node_from_mesh for 7.10 CLIParser.
                vertices, faces = sequential_triangles(source.vertices.dot(PRINTER_TO_SCENE.T), source.faces)
                new_mesh = MeshData(vertices=np.asarray(vertices, dtype=np.float32),
                                    indices=np.asarray(faces, dtype=np.int32), file_name=mesh_path)
            target = (node, Matrix(local_matrix), {
                "matrix": Matrix(np.array(node.getLocalTransformation().getData(), dtype=float)),
                "extruder": node.callDecoration("getActiveExtruder"),
                "plate": node.callDecoration("getBuildPlateNumber"), "mesh": old_mesh, "new_mesh": new_mesh})
        else:
            node = node_from_mesh(source.vertices.dot(PRINTER_TO_SCENE.T), source.faces, label)
            node.setTransformation(Matrix(local_matrix))
            nodes.append(node)
        for op, (vertices, faces) in modifier_meshes:
            child = node_from_mesh(vertices, faces, op["role"] + (" #" + str(i + 1) if count > 1 else ""))
            if node is target_node:
                target_children.append(child)
            else:
                child.setParent(node)
            child.callDecoration("getStack").getTop().setDefinition(stack.definition.getId())
            for key, setting in op["settings"].items():
                child.callDecoration("getStack").getTop().setProperty(key, "value", setting)

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
                if record is not None:
                    # Native base P: real quality_changes/quality/material/variant/
                    # definition_changes containers; only Delta goes to the user container.
                    containers[0] = user
                    containers[1:6] = native[s.getId()]
                    self.after.append(containers)
                    continue
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
            # A native base keeps its inherited values; nothing beyond Delta is pinned.
            for key in (functional.PROCESS_KEYS if record is None else ()):
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
                self.target_changed = target is not None
                if target is not None:
                    node, planned, old = target
                    if old["new_mesh"] is not None:
                        node.setMeshData(old["new_mesh"])
                    node.setTransformation(planned)
                    node.callDecoration("setActiveExtruder", stack.extruders["0"].getId())
                    node.callDecoration("setBuildPlateNumber", plate)
                    for child in target_children:
                        child.setParent(node)
                for node in self.nodes:
                    node.setParent(scene.getRoot())
                for node in ([target[0]] if target is not None else []) + self.nodes:
                    app.getBuildVolume().checkBoundsAndUpdate(node)
                    if node.isOutsideBuildArea():
                        raise ApplyError("transformed model/batch is outside the actual build area")
                self.active = True
                self.readback = readback(app, decision)
                if self.readback["status"] != "pass":
                    raise ApplyError("effective setting readback differs: " + str(self.readback["differences"]))
                scene.sceneChanged.emit(target[0] if target is not None else self.nodes[0])
            except Exception:
                self.undo()
                raise

        def undo(self):
            for node in self.nodes:
                node.setParent(None)
            if target is not None and getattr(self, "target_changed", False):
                # Restore the user's object exactly: transform, extruder, plate, mesh.
                node, _, old = target
                for child in target_children:
                    child.setParent(None)
                if old["new_mesh"] is not None:
                    node.setMeshData(old["mesh"])
                node.setTransformation(old["matrix"])
                if old["extruder"] is not None:
                    node.callDecoration("setActiveExtruder", old["extruder"])
                if old["plate"] is not None:
                    node.callDecoration("setBuildPlateNumber", old["plate"])
                app.getBuildVolume().checkBoundsAndUpdate(node)
                self.target_changed = False
            self._containers(self.before)
            app.getMultiBuildPlateModel().setActiveBuildPlate(self.old_plate)
            if self.old_gravity is not None:
                app.getPreferences().setValue("physics/automatic_drop_down", self.old_gravity)
            self.active = False
            scene.sceneChanged.emit(scene.getRoot())

    return PlanOperation()
