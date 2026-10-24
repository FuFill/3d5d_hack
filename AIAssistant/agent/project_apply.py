# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Apply a core plan to the project that is already open, keeping its objects.

Native preset plans (decision.base_presets) go through the core's own runtime adapter:
the real STE profile containers of the chosen base P are put into the stacks (content
digests checked), only the minimal Delta is written to the user container, readback must
pass, and the switch is one Undo operation. Objects are not moved: the planned orientation
of a single loaded model is applied by apply_plan (core, in place). Plans without a native
base (native_presets off) fall back to visible user changes. revert_plan_changes undoes it.

The same change description is used after apply_plan (new project), so the chat always
shows which preset became the base and what was changed on top of it.
"""

import sys

import numpy

from .tools import ToolError, _simple_change

# Mirrors ste_planner.functional.PROCESS_KEYS: what the legacy planner process profile pins
# (shell_tests/test_project_apply.py keeps both lists equal).
PROCESS_KEYS = ("machine_nozzle_size", "line_width", "layer_height", "wall_line_count", "top_layers",
                "bottom_layers", "infill_sparse_density", "infill_pattern", "material_print_temperature",
                "material_bed_temperature", "speed_print", "cool_fan_speed")
MODE_NAMES = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}


def _same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    return a == b


def _runtime_adapter():
    from .core_worker import core_path
    if core_path() not in sys.path:
        sys.path.insert(0, core_path())
    from ste_planner import runtime_adapter
    return runtime_adapter


# ---------------------------------------------------------------------- plan helpers

def planned_settings(decision):
    """(key, extruder position or None, value) of a plan without a native base, without printing_mode."""
    payload = (decision.get("setting_patch") or {}).get("payload") or {}
    effective = decision.get("effective_settings") or {}
    assigned = {}
    for key, value in (payload.get("global") or {}).items():
        assigned[(key, None)] = value
    for position, values in (payload.get("extruders") or {}).items():
        for key, value in values.items():
            assigned[(key, int(position))] = value
    for key in PROCESS_KEYS:
        if key in effective and (key, 0) not in assigned and (key, None) not in assigned:
            assigned[(key, 0)] = effective[key]
    return [(key, position, value) for (key, position), value in sorted(assigned.items(), key=lambda i: str(i[0]))
            if key != "printing_mode"]


def delta_keys(decision):
    """(key, extruder position or None) written by the plan's minimal Delta."""
    payload = (decision.get("setting_patch") or {}).get("payload") or {}
    keys = [(key, None) for key in (payload.get("global") or {})]
    for position, values in (payload.get("extruders") or {}).items():
        keys.extend((key, int(position)) for key in values)
    return keys


def material_root_id(app, decision):
    material_file = (decision.get("chosen_material") or {}).get("slicer_file") or ""
    container_id = material_file.split("/")[-1].split("\\")[-1].replace(".xml.fdm_material", "").replace(".fdm_material", "")
    if not container_id:
        return None
    found = app.getContainerRegistry().findInstanceContainersMetadata(id=container_id)
    if found:
        return found[0].get("base_file", container_id)
    return container_id


def plan_rotation(decision):
    for operation in decision.get("scene_operations") or []:
        if operation.get("op") == "set_transform":
            return numpy.asarray(operation["rotation"], dtype=float)
    return None


def rotation_text(rotation):
    """Axis and angle of a printer-frame rotation, e.g. "X +90°"."""
    rotation = numpy.asarray(rotation, dtype=float)
    angle = numpy.degrees(numpy.arccos(numpy.clip((numpy.trace(rotation) - 1) / 2.0, -1.0, 1.0)))
    if angle < 0.01:
        return "0°"
    axis = numpy.array([rotation[2, 1] - rotation[1, 2], rotation[0, 2] - rotation[2, 0], rotation[1, 0] - rotation[0, 1]])
    if numpy.linalg.norm(axis) < 1e-9:  # 180°: axis from the symmetric part
        axis = numpy.sqrt(numpy.clip((numpy.diag(rotation) + 1) / 2.0, 0, 1))
    axis = axis / numpy.linalg.norm(axis)
    index = int(numpy.argmax(numpy.abs(axis)))
    if abs(axis[index]) > 0.999:
        return "%s %+.0f°" % ("XYZ"[index], angle * numpy.sign(axis[index]))
    return "%.0f° (%.2f, %.2f, %.2f)" % (angle, axis[0], axis[1], axis[2])


def preset_label(decision):
    base = decision.get("base_presets")
    return base.get("label") if base else None


# ---------------------------------------------------------------------- project snapshot / changes

class ProjectSnapshot:
    """Values of the open project that a plan may change, read before and after applying it."""

    def __init__(self, registry, decision):
        stack = registry._global()
        extruder = stack.extruders["0"]
        self.profile = registry._app.getMachineManager().activeQualityOrQualityChangesName
        self.material = extruder.material.getName()
        self.mode = stack.getProperty("printing_mode", "value")
        keys = set(delta_keys(decision))
        for target in [stack] + [stack.extruders[p] for p in sorted(stack.extruders)]:
            position = None if target is stack else int(target.getMetaDataEntry("position", 0))
            for key in target.userChanges.getAllKeys():
                keys.add((key, position))
        keys.discard(("printing_mode", None))
        self.values = {}
        for key, position in keys:
            target = stack if position is None else stack.extruders.get(str(position))
            if target is not None:
                self.values[(key, position)] = target.getProperty(key, "value")


def describe_changes(registry, decision, before):
    """Highlighted rows: preset base, material, mode, the Delta and user changes the preset replaced."""
    stack = registry._global()
    after = ProjectSnapshot(registry, decision)
    rows = []
    new_profile = preset_label(decision) or after.profile
    if new_profile != before.profile:
        rows.append(_simple_change("profile", "STE preset", "Пресет STE", before.profile, new_profile))
    if after.material != before.material:
        rows.append(_simple_change("material", "Material", "Материал", before.material, after.material,
                                   scope="extruder 0"))
    if after.mode != before.mode:
        row = registry._change("printing_mode", stack, before.mode, after.mode)
        row["old_text"], row["new_text"] = MODE_NAMES.get(before.mode, before.mode), MODE_NAMES.get(after.mode, after.mode)
        rows.append(row)
    delta = set(delta_keys(decision))
    for key, position in sorted(set(before.values) | set(after.values), key=lambda k: (k not in delta, str(k))):
        target = stack if position is None else stack.extruders.get(str(position))
        if target is None:
            continue
        old, new = before.values.get((key, position)), target.getProperty(key, "value")
        if _same(old, new):
            continue
        row = registry._change(key, target, old, new)
        if (key, position) not in delta:
            row["inherited"] = True  # a previous user change replaced by the preset value
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- applier

class InPlaceApplier:
    def __init__(self, registry, core):
        self._registry = registry
        self._core = core
        self._applied = {}

    # Runs on the Qt main thread (registered with on_main=True).
    def apply(self, args, ctx):
        registry = self._registry
        plan_id = args.get("plan_id")
        decision = self._core.decision_record(plan_id)["decision"]
        if plan_id in self._applied:
            raise ToolError("This plan is already applied to the project; use revert_plan_changes first")
        if decision.get("status") == "infeasible":
            raise ToolError("The plan is infeasible and cannot be applied; explain the failed requirements")
        before = ProjectSnapshot(registry, decision)
        undo = {"operation": None, "legacy": None}
        warnings, errors, skipped = [], [], []
        if decision.get("base_presets") is not None:
            adapter = _runtime_adapter()
            try:
                operation = adapter.create_settings_operation(registry._app, decision,
                                                              bool(args.get("confirm_unverified_5d")))
                operation.redo()
            except adapter.ApplyError as e:
                raise ToolError(str(e))
            operation.push()
            undo["operation"] = operation
            readback = operation.readback
        else:
            legacy = self._apply_legacy(decision, ctx, warnings, errors, skipped)
            undo["legacy"] = legacy["undo"]
            readback = {"status": "pass" if not legacy["differences"] else "fail",
                        "differences": legacy["differences"]}
        mode = (decision.get("route") or {}).get("mode")
        if mode and mode != "classic":
            warnings.append("5D: machine kinematics and collisions are not verified by the core")
        self._applied[plan_id] = undo
        changes = describe_changes(registry, decision, before)
        for operation in decision.get("scene_operations") or []:
            if operation.get("op") in ("add_modifier_mesh", "multiply"):
                skipped.append(operation["op"])
        rotation = plan_rotation(decision)
        outside = [n.getName() for n in registry._scene_nodes()
                   if getattr(n, "isOutsideBuildArea", None) and n.isOutsideBuildArea()]
        return {"plan_id": plan_id, "status": "applied_to_current_project",
                "base_preset": preset_label(decision) or "planner process profile (native presets off)",
                "native_preset": decision.get("base_presets") is not None,
                "readback": readback.get("status"), "readback_differences": readback.get("differences"),
                "changes": changes, "warnings": warnings, "errors": errors, "skipped": sorted(set(skipped)),
                "planned_orientation": rotation_text(rotation) if rotation is not None else "0°",
                "objects_moved": False,
                "orientation_note": "objects keep their position and orientation; the planned orientation of a "
                                    "single loaded model is applied by apply_plan with its object_id",
                "objects_kept": len(registry._scene_nodes()), "outside_build_volume": outside,
                "physical_print_approval": False}

    def _apply_legacy(self, decision, ctx, warnings, errors, skipped):
        """Plan without a native base: mode, material and settings as visible user changes."""
        registry = self._registry
        stack = registry._global()
        undo = {"user": [], "material": None}
        mode = (decision.get("route") or {}).get("mode")
        if mode and mode != stack.getProperty("printing_mode", "value"):
            had = stack.userChanges.hasProperty("printing_mode", "value")
            undo["user"].append((None, "printing_mode", had,
                                 stack.userChanges.getProperty("printing_mode", "value") if had else None))
            registry._set_print_mode({"mode": mode}, ctx)
        root = material_root_id(registry._app, decision)
        extruder = stack.extruders["0"]
        current_root = extruder.material.getMetaDataEntry("base_file", extruder.material.getId())
        if root and root != current_root:
            try:
                registry._set_material({"material_id": root, "extruder": 0}, ctx)
                undo["material"] = current_root
            except ToolError as e:
                errors.append("material %s: %s" % (root, e))
        assignments = planned_settings(decision)
        items = []
        for key, position, value in assignments:
            try:
                registry._definition(key)
            except ToolError:
                skipped.append(key)
                continue
            target, _scope = registry._stack_for(key, position if position is not None else 0)
            had = target.userChanges.hasProperty(key, "value")
            undo["user"].append((target.getMetaDataEntry("position", None) if target is not stack else None,
                                 key, had, target.userChanges.getProperty(key, "value") if had else None))
            items.append({"key": key, "value": value, "extruder": position if position is not None else 0})
        if items:
            result = registry._set_settings({"changes": items}, ctx)
            warnings.extend(result["warnings"])
            errors.extend(result["errors"])
        differences = []
        for key, position, value in assignments:
            if key in skipped:
                continue
            target, _scope = registry._stack_for(key, position if position is not None else 0)
            if not _same(target.getProperty(key, "value"), value):
                differences.append({"key": key, "expected": value, "actual": target.getProperty(key, "value")})
        return {"undo": undo, "differences": differences}

    # Runs on the Qt main thread.
    def revert(self, args, ctx):
        registry = self._registry
        plan_id = args.get("plan_id")
        undo = self._applied.pop(plan_id, None)
        if undo is None:
            raise ToolError("Nothing to revert for this plan_id")
        decision = self._core.decision_record(plan_id)["decision"]
        before = ProjectSnapshot(registry, decision)
        if undo["operation"] is not None:
            undo["operation"].undo()
        if undo["legacy"] is not None:
            stack = registry._global()
            if undo["legacy"]["material"]:
                registry._set_material({"material_id": undo["legacy"]["material"], "extruder": 0}, ctx)
            for position, key, had, value in reversed(undo["legacy"]["user"]):
                target = stack if position is None else stack.extruders[str(position)]
                if had:
                    target.userChanges.setProperty(key, "value", value)
                else:
                    target.userChanges.removeInstance(key)
        changes = describe_changes(registry, {"setting_patch": {}}, before)
        return {"plan_id": plan_id, "reverted": True, "changes": changes}
