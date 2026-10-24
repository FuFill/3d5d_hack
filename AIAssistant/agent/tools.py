# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""MCP tools over the STE Slicer API: settings and profiles, scene, slicing and export.

Coordinates are given in printer axes (millimetres, Z up, origin at the centre of the
build plate); the Uranium scene is Y-up and is converted here. Every change returns a
"changes" list with old and new values so the chat window can highlight it.
"""

import contextlib
import math
import os
import threading
import time

from .mainthread import run_on_main

ALLOWED_MODES = ("classic", "cylindrical", "cylindrical_full")
MODE_NAMES = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}

# Backend states, see UM.Backend.Backend.BackendState.
STATE_NAMES = {1: "not_started", 2: "processing", 3: "done", 4: "error", 5: "disabled"}


class ToolError(Exception):
    pass


class Tool:
    def __init__(self, name, description, properties, func, required=(), on_main=True):
        self.name = name
        self.description = description
        self.schema = {"type": "object", "properties": properties, "required": list(required)}
        self.func = func
        self.on_main = on_main


class ToolRegistry:
    def __init__(self, app, slice_monitor, output_dir, core=None):
        self._app = app
        self._slices = slice_monitor
        self._output_dir = output_dir
        self.core = core
        self.upload_sources = lambda: []
        self._catalog = None
        self._nodes = {}
        self._node_ids = {}
        self._next_node_id = 1
        self._tools = []
        self._by_name = {}
        self._build()

    # ------------------------------------------------------------------ registry

    def has(self, name):
        return name in self._by_name

    def mcp_tools(self):
        return [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in self._tools]

    def openai_tools(self):
        return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
                for t in self._tools]

    def call(self, name, arguments, context=None):
        tool = self._by_name[name]
        if not isinstance(arguments, dict):
            return {"ok": False, "result": {"error": "Arguments must be a JSON object"}}
        try:
            if tool.on_main:
                result = run_on_main(self._app, tool.func, arguments, context)
            else:
                result = tool.func(arguments, context)
            return {"ok": True, "result": result if isinstance(result, dict) else {"value": result}}
        except ToolError as e:
            return {"ok": False, "result": {"error": str(e)}}
        except Exception as e:
            try:
                from UM.Logger import Logger
                Logger.logException("e", "AI Assistant tool %s failed", name)
            except Exception:
                pass
            return {"ok": False, "result": {"error": "%s: %s" % (type(e).__name__, e)}}

    def _add(self, *args, **kwargs):
        tool = Tool(*args, **kwargs)
        self._tools.append(tool)
        self._by_name[tool.name] = tool

    def _build(self):
        extruder = {"type": "integer", "description": "Extruder position (0 = first). Default: active extruder."}
        object_id = {"type": "integer", "description": "Object id from list_objects."}

        if self.core is not None:
            plan_id = {"type": "string", "description": "Application-owned id returned by plan_print."}
            self._add("list_model_sources", "List actual uploaded files and original files of scene objects. "
                      "Use attachment_id or object_id from here in plan_print. Never guess filesystem paths.",
                      {}, self._list_model_sources, on_main=False)
            self._add("plan_print", "Translate the original specification and STL into a plan using the existing "
                      "deterministic core (materials, orientation, settings, DML and uncertainty checks). "
                      "Does not change the project. Select exactly one source: attachment_id, object_id or an actual "
                      "absolute mesh_path. Object input reads the ORIGINAL STL, not its current scene transform. "
                      "Do not paraphrase the specification. If the model is already on the plate, use its object_id: "
                      "apply then reorients that object in place (only it may be in the project); an attachment "
                      "is loaded by apply into an empty project.",
                      {"task_text": {"type": "string"},
                       "attachment_id": {"type": "string", "description": "Id from the message attachment or list_model_sources."},
                       "object_id": {"type": "integer", "description": "Scene object id from list_objects/list_model_sources."},
                       "mesh_path": {"type": "string", "description": "Existing absolute path returned by a tool; never just a filename."},
                       "mode": {"type": "string", "enum": list(ALLOWED_MODES)},
                       "llm_items": {"type": "array", "description": "Optional cited requirement proposals; the core "
                                     "validates fragment, value, unit and comparator against task_text. Never invent facts.",
                                     "items": {"type": "object"}}},
                      self._plan_print, required=("task_text",), on_main=False)
            self._add("get_plan", "Read the current core proposal, limitations and explanations.",
                      {"plan_id": plan_id}, lambda a, c: self.core.summary(a["plan_id"]), required=("plan_id",), on_main=False)
            self._add("apply_plan", "Apply exactly the core plan as one Undo operation. A plan by object_id reorients "
                      "that loaded object in place and needs no other models; a plan by attachment needs an empty project. "
                      "Rejects stale source/project, infeasible and unqualified 5D plans. Never delete user objects to bypass it.",
                      {"plan_id": plan_id}, self.core.apply, required=("plan_id",))
            self._add("verify_plan", "Read back actual STE settings, source and scene; status must pass before slicing.",
                      {"plan_id": plan_id}, self.core.verify, required=("plan_id",))

        self._add("get_state",
                  "Current printer, printing mode (3D/5D), quality profile, material per extruder, "
                  "settings changed by the user and number of objects. Call this first.",
                  {}, self._get_state)
        self._add("find_settings",
                  "Search slicer settings by key, English or localized label, or description. "
                  "Returns keys, types, units, allowed options and current values.",
                  {"query": {"type": "string", "description": "Words to search, e.g. 'infill', 'support', 'temperature'."},
                   "limit": {"type": "integer", "description": "Maximum results, default 15."}},
                  self._find_settings, required=("query",))
        self._add("get_settings",
                  "Read current values of settings by key (global or per extruder).",
                  {"keys": {"type": "array", "items": {"type": "string"}}, "extruder": extruder},
                  self._get_settings, required=("keys",))
        self._add("set_settings",
                  "Change one or more settings. Per-extruder settings go to the extruder, the rest to the "
                  "global stack. value=null resets a setting to the profile value. Returns old -> new values.",
                  {"changes": {"type": "array", "items": {"type": "object", "properties": {
                      "key": {"type": "string"},
                      "value": {"description": "New value (number, boolean, string or enum option key); null to reset."},
                      "extruder": extruder}, "required": ["key", "value"]}}},
                  self._set_settings, required=("changes",))
        self._add("set_print_mode",
                  "Switch the printing mode: classic = 3D, cylindrical = 5D Spiral, cylindrical_full = 5D. "
                  "Only these three modes are allowed.",
                  {"mode": {"type": "string", "enum": list(ALLOWED_MODES)}},
                  self._set_print_mode, required=("mode",))
        self._add("list_profiles",
                  "List quality profiles (quality_type) and custom profiles available for the current printer, "
                  "material and mode, with the active one marked.",
                  {}, self._list_profiles)
        self._add("select_profile",
                  "Activate a quality profile by quality_type, or a custom profile by name. User changes are kept.",
                  {"quality_type": {"type": "string"}, "custom_name": {"type": "string"}},
                  self._select_profile)
        self._add("create_profile",
                  "Create a custom profile from the current profile plus the user changes and activate it.",
                  {"name": {"type": "string"}}, self._create_profile, required=("name",))
        self._add("save_profile",
                  "Save the current user changes into the active custom profile.",
                  {}, self._save_profile)
        self._add("list_materials",
                  "List materials available for an extruder of the current printer (id, name, brand, type).",
                  {"query": {"type": "string", "description": "Optional filter, e.g. 'PETG'."}, "extruder": extruder},
                  self._list_materials)
        self._add("set_material",
                  "Select a material for an extruder by id from list_materials.",
                  {"material_id": {"type": "string"}, "extruder": extruder},
                  self._set_material, required=("material_id",))

        self._add("load_model",
                  "Load a model file (STL, OBJ, 3MF) from a local path into the scene and wait until it is placed.",
                  {"path": {"type": "string"}}, self._load_model, required=("path",), on_main=False)
        self._add("list_objects",
                  "List objects in the scene with id, name, position (bounding-box centre X/Y and bottom Z), "
                  "size X/Y/Z, scale and whether they fit the build volume. Use it to read model dimensions.",
                  {}, self._list_objects)
        self._add("move_object",
                  "Move an object. Absolute: X/Y of the bounding-box centre and Z of its bottom, in mm from the "
                  "build plate centre. With relative=true the values are offsets.",
                  {"id": object_id, "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
                   "relative": {"type": "boolean"}},
                  self._move_object, required=("id",))
        self._add("rotate_object",
                  "Rotate an object around a printer axis through its centre, in degrees (counterclockwise "
                  "looking from the positive axis). In 3D mode the object drops back onto the plate.",
                  {"id": object_id, "axis": {"type": "string", "enum": ["x", "y", "z"]}, "angle": {"type": "number"}},
                  self._rotate_object, required=("id", "axis", "angle"))
        self._add("scale_object",
                  "Scale an object. Either percent (uniform) or per-axis percent x/y/z, or target size_mm "
                  "for one axis (uniform). 100 = original size.",
                  {"id": object_id, "percent": {"type": "number"},
                   "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"},
                   "size_mm": {"type": "number"}, "axis": {"type": "string", "enum": ["x", "y", "z"]}},
                  self._scale_object, required=("id",))
        self._add("arrange_all",
                  "Automatically arrange all objects on the build plate and wait for the result.",
                  {}, self._arrange_all, on_main=False)
        self._add("delete_object",
                  "Delete an object by id, or all objects with all=true.",
                  {"id": object_id, "all": {"type": "boolean"}}, self._delete_object)

        self._add("slice",
                  "Start slicing with the current settings and wait for the result: print time and material.",
                  {}, self._slice, on_main=False)
        self._add("get_slice_status",
                  "Current slicing state (not_started, processing, done, error, disabled) and progress 0..1.",
                  {}, self._get_slice_status)
        self._add("get_slice_result",
                  "Print time and material usage of the last finished slice.",
                  {}, self._get_slice_result)
        self._add("export_gcode",
                  "Write the sliced G-code to a file. Default: <output folder>/<job name>/result.gcode.",
                  {"path": {"type": "string"}}, self._export_gcode, on_main=False)
        self._add("save_project",
                  "Save the STE Slicer project (3MF with models, printer, profile and settings). "
                  "Default: <output folder>/<job name>/project.3mf.",
                  {"path": {"type": "string"}}, self._save_project, on_main=False)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _source_info(path):
        absolute = isinstance(path, str) and bool(path) and os.path.isabs(path)
        exists = bool(absolute and os.path.isfile(path))
        extension = os.path.splitext(path)[1].lower() if isinstance(path, str) else ""
        return {"source_path": os.path.normpath(path) if absolute else None,
                "source_exists": exists, "source_format": extension.lstrip("."),
                "can_plan_original_stl": bool(exists and extension == ".stl")}

    def _node_source(self, node):
        mesh = node.getMeshData()
        return self._source_info(mesh.getFileName() if mesh is not None else None)

    def _list_model_sources(self, args, ctx):
        attachments = [dict(self._source_info(item["path"]), attachment_id=item["id"], name=item["name"])
                       for item in self.upload_sources()]
        def scene_sources():
            return [dict(self._node_source(node), object_id=self._node_id(node), name=node.getName())
                    for node in self._scene_nodes()]
        return {"attachments": attachments, "scene_models": run_on_main(self._app, scene_sources),
                "note": "Use a source id. The core reads the original STL. A model already on the plate: plan with "
                        "its object_id and apply reorients it in place; other models must not be in the project."}

    def _plan_print(self, args, ctx):
        selectors = [key for key in ("attachment_id", "object_id", "mesh_path") if args.get(key) is not None]
        if len(selectors) != 1:
            raise ToolError("Choose exactly one attachment_id, object_id or mesh_path from list_model_sources")
        key = selectors[0]
        if key == "attachment_id":
            item = next((item for item in self.upload_sources() if item["id"] == args[key]), None)
            if item is None:
                raise ToolError("Attachment not found in this session; use list_model_sources or attach the STL again")
            source = self._source_info(item["path"])
        elif key == "object_id":
            source = run_on_main(self._app, lambda: self._node_source(self._node(args[key])))
        else:
            source = self._source_info(args[key])
        if not source["can_plan_original_stl"]:
            raise ToolError("An existing original STL is required. Use list_model_sources and its attachment_id/object_id, "
                            "or attach the STL with the chat paperclip. Do not guess folders or use only a filename. "
                            "OBJ/3MF remain available through manual tools.")
        request = {k: v for k, v in args.items() if k not in ("attachment_id", "object_id", "target_object_id")}
        request["mesh_path"] = source["source_path"]
        if key == "object_id":
            # Apply then reorients this loaded object instead of loading a second copy.
            request["target_object_id"] = int(args[key])
        result = self.core.plan(request, ctx)
        result["input_source"] = source
        return result

    def _global(self):
        stack = self._app.getGlobalContainerStack()
        if stack is None:
            raise ToolError("No printer is configured in STE Slicer")
        return stack

    def _extruder(self, position=None):
        stack = self._global()
        extruders = stack.extruders
        if position is None:
            from steslicer.Settings.ExtruderManager import ExtruderManager
            active = ExtruderManager.getInstance().getActiveExtruderStack()
            if active is not None:
                return active
            position = 0
        key = str(int(position))
        if key not in extruders:
            raise ToolError("Extruder %s does not exist; available: %s" % (key, ", ".join(sorted(extruders))))
        return extruders[key]

    def _translate(self, key, text, suffix="label"):
        if self._catalog is None:
            self._catalog = False
            try:
                from UM.i18n import i18nCatalog
                from UM.Settings.ContainerRegistry import ContainerRegistry
                definition = self._global().getBottom()
                suffix_ext = ContainerRegistry.getMimeTypeForContainer(type(definition)).preferredSuffix
                names = [definition.getId() + "." + suffix_ext] + [os.path.basename(f) for f in definition.getInheritedFiles()]
                for name in names:
                    catalog = i18nCatalog(os.path.basename(name))
                    if catalog.hasTranslationLoaded():
                        self._catalog = catalog
            except Exception:
                self._catalog = False
        if not self._catalog or not text:
            return text
        return self._catalog.i18nc(key + " " + suffix, text)

    def _definition(self, key):
        found = self._global().getBottom().findDefinitions(key=key)
        if not found:
            raise ToolError("Unknown setting '%s'. Use find_settings to look up keys." % key)
        return found[0]

    def _stack_for(self, key, extruder=None):
        global_stack = self._global()
        if global_stack.getProperty(key, "settable_per_extruder") and global_stack.extruders:
            return self._extruder(extruder), "extruder"
        return global_stack, "global"

    def _scope_name(self, stack):
        if stack is self._global():
            return "global"
        return "extruder %s" % stack.getMetaDataEntry("position", "0")

    def _describe(self, key, stack):
        label = stack.getProperty(key, "label")
        options = stack.getProperty(key, "options")
        info = {
            "key": key,
            "label": label,
            "label_local": self._translate(key, label),
            "type": stack.getProperty(key, "type"),
            "unit": stack.getProperty(key, "unit") or "",
            "value": _plain(stack.getProperty(key, "value")),
            "enabled": bool(stack.getProperty(key, "enabled")),
            "scope": self._scope_name(stack),
        }
        if options:
            info["options"] = dict((k, v) for k, v in options.items())
        return info

    def _change(self, key, stack, old, new):
        label = stack.getProperty(key, "label")
        options = stack.getProperty(key, "options") or {}
        return {
            "key": key,
            "label": label,
            "label_local": self._translate(key, label),
            "scope": self._scope_name(stack),
            "old": _plain(old),
            "new": _plain(new),
            "old_text": options.get(old, None) if isinstance(old, str) else None,
            "new_text": options.get(new, None) if isinstance(new, str) else None,
            "unit": stack.getProperty(key, "unit") or "",
        }

    @contextlib.contextmanager
    def _keep_user_changes(self):
        """Profile and material switches would otherwise ask the user to keep or discard changes."""
        preferences = self._app.getPreferences()
        key = "steslicer/choice_on_profile_override"
        previous = preferences.getValue(key)
        preferences.setValue(key, "always_keep")
        try:
            yield
        finally:
            preferences.setValue(key, previous)

    def _mode(self):
        stack = self._global()
        mode = stack.getProperty("printing_mode", "value")
        return {"mode": mode, "name": MODE_NAMES.get(mode, mode)}

    # ------------------------------------------------------------------ settings and profiles

    def _get_state(self, args, ctx):
        stack = self._global()
        machine_manager = self._app.getMachineManager()
        extruders = []
        for position in sorted(stack.extruders):
            extruder = stack.extruders[position]
            extruders.append({
                "position": int(position),
                "material_id": extruder.material.getMetaDataEntry("base_file", extruder.material.getId()),
                "material": extruder.material.getName(),
                "nozzle": extruder.variant.getName(),
                "enabled": bool(extruder.isEnabled) if hasattr(extruder, "isEnabled") else True,
            })
        user_changes = []
        for user_stack in [stack] + [stack.extruders[p] for p in sorted(stack.extruders)]:
            for key in sorted(user_stack.userChanges.getAllKeys()):
                user_changes.append({"key": key, "scope": self._scope_name(user_stack),
                                     "value": _plain(user_stack.getProperty(key, "value"))})
        custom = machine_manager.activeQualityChangesGroup
        return {
            "machine": stack.getName(),
            "machine_definition": stack.definition.getName(),
            "printing_mode": self._mode(),
            "allowed_modes": [{"mode": m, "name": MODE_NAMES[m]} for m in ALLOWED_MODES],
            "five_axis": bool(stack.getProperty("machine_five_axis", "value")),
            "quality_type": machine_manager.activeQualityType,
            "profile": machine_manager.activeQualityOrQualityChangesName,
            "custom_profile": custom.name if custom else None,
            "extruders": extruders,
            "user_changes": user_changes,
            "objects": len(self._scene_nodes()),
            "output_folder": self._output_dir(),
        }

    def _find_settings(self, args, ctx):
        query = str(args.get("query", "")).strip().lower()
        if not query:
            raise ToolError("query is required")
        limit = max(1, min(int(args.get("limit") or 15), 50))
        words = query.split()
        global_stack = self._global()
        scored = []
        for definition in global_stack.getBottom().findDefinitions():
            if definition.type == "category":
                continue
            label = definition.label or ""
            local = self._translate(definition.key, label)
            haystack = " ".join([definition.key, label, local or "", definition.description or ""]).lower()
            if not all(w in haystack for w in words):
                continue
            title = " ".join([definition.key, label, local or ""]).lower()
            score = sum(2 if w in title else 1 for w in words) + (3 if query in title else 0)
            scored.append((-score, definition.key))
        scored.sort()
        results = []
        for _, key in scored[:limit]:
            stack, _scope = self._stack_for(key)
            results.append(self._describe(key, stack))
        return {"count": len(scored), "settings": results}

    def _get_settings(self, args, ctx):
        keys = args.get("keys") or []
        if not isinstance(keys, list) or not keys:
            raise ToolError("keys must be a non-empty list")
        values, errors = [], []
        for key in keys[:60]:
            try:
                self._definition(key)
                stack, _scope = self._stack_for(key, args.get("extruder"))
                values.append(self._describe(key, stack))
            except ToolError as e:
                errors.append(str(e))
        return {"settings": values, "errors": errors}

    def _set_settings(self, args, ctx):
        items = args.get("changes")
        if not isinstance(items, list) or not items:
            raise ToolError("changes must be a non-empty list of {key, value}")
        changes, errors, warnings = [], [], []
        for item in items:
            try:
                key = item.get("key")
                if key == "printing_mode":
                    result = self._set_print_mode({"mode": item.get("value")}, ctx)
                    changes.extend(result["changes"])
                    continue
                self._definition(key)
                stack, _scope = self._stack_for(key, item.get("extruder"))
                old = stack.getProperty(key, "value")
                if item.get("value") is None:
                    stack.userChanges.removeInstance(key)
                else:
                    value = _coerce(item.get("value"), stack.getProperty(key, "type"), stack.getProperty(key, "options"))
                    stack.userChanges.setProperty(key, "value", value)
                new = stack.getProperty(key, "value")
                if _plain(old) != _plain(new):
                    changes.append(self._change(key, stack, old, new))
                warnings.extend(_limit_warnings(key, stack, new))
                if not stack.getProperty(key, "enabled"):
                    warnings.append("%s is disabled by other settings and has no effect now" % key)
            except (ToolError, ValueError, TypeError) as e:
                errors.append("%s: %s" % (item.get("key") if isinstance(item, dict) else item, e))
        if errors and not changes:
            raise ToolError("; ".join(errors))
        return {"changes": changes, "errors": errors, "warnings": warnings}

    def _set_print_mode(self, args, ctx):
        mode = args.get("mode")
        if mode not in ALLOWED_MODES:
            raise ToolError("Mode must be one of: classic (3D), cylindrical (5D Spiral), cylindrical_full (5D)")
        stack = self._global()
        options = stack.getProperty("printing_mode", "options") or {}
        if options and mode not in options:
            raise ToolError("The current printer does not support mode %s" % mode)
        if mode != "classic" and not stack.getProperty("machine_five_axis", "value"):
            raise ToolError("The current printer is not five-axis; only classic (3D) is available")
        old = stack.getProperty("printing_mode", "value")
        if old != mode:
            stack.userChanges.setProperty("printing_mode", "value", mode)
        new = stack.getProperty("printing_mode", "value")
        change = self._change("printing_mode", stack, old, new)
        change["old_text"] = MODE_NAMES.get(old, change["old_text"])
        change["new_text"] = MODE_NAMES.get(new, change["new_text"])
        return {"mode": new, "name": MODE_NAMES.get(new, new), "changes": [change] if old != new else []}

    def _list_profiles(self, args, ctx):
        stack = self._global()
        machine_manager = self._app.getMachineManager()
        quality_manager = self._app.getQualityManager()
        profiles = []
        for quality_type, group in sorted(quality_manager.getQualityGroups(stack).items()):
            layer_height = None
            node = group.node_for_global
            container = node.getContainer() if node else None
            if container is not None and container.hasProperty("layer_height", "value"):
                layer_height = _plain(container.getProperty("layer_height", "value"))
            profiles.append({"quality_type": quality_type, "name": group.name, "available": bool(group.is_available),
                             "layer_height": layer_height,
                             "active": quality_type == machine_manager.activeQualityType})
        custom = []
        active_custom = machine_manager.activeQualityChangesGroup
        for name, group in sorted(quality_manager.getQualityChangesGroups(stack).items()):
            custom.append({"name": name, "quality_type": group.quality_type, "available": bool(group.is_available),
                           "active": active_custom is not None and active_custom.name == name})
        return {"profiles": profiles, "custom_profiles": custom,
                "active": machine_manager.activeQualityOrQualityChangesName}

    def _select_profile(self, args, ctx):
        stack = self._global()
        machine_manager = self._app.getMachineManager()
        quality_manager = self._app.getQualityManager()
        old = machine_manager.activeQualityOrQualityChangesName
        with self._keep_user_changes():
            if args.get("custom_name"):
                groups = quality_manager.getQualityChangesGroups(stack)
                group = groups.get(args["custom_name"])
                if group is None:
                    raise ToolError("Custom profile not found; available: %s" % ", ".join(sorted(groups)))
                machine_manager.setQualityChangesGroup(group, no_dialog=True)
            elif args.get("quality_type"):
                groups = quality_manager.getQualityGroups(stack)
                group = groups.get(args["quality_type"])
                if group is None:
                    raise ToolError("Quality type not found; available: %s" % ", ".join(sorted(groups)))
                if not group.is_available:
                    raise ToolError("Profile %s is not available for the current material/nozzle" % group.name)
                machine_manager.setQualityGroup(group, no_dialog=True)
            else:
                raise ToolError("Pass quality_type or custom_name")
        new = machine_manager.activeQualityOrQualityChangesName
        return {"profile": new, "quality_type": machine_manager.activeQualityType,
                "changes": [_simple_change("profile", "Profile", "Профиль", old, new)] if old != new else []}

    def _create_profile(self, args, ctx):
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolError("name is required")
        stack = self._global()
        machine_manager = self._app.getMachineManager()
        quality_manager = self._app.getQualityManager()
        old = machine_manager.activeQualityOrQualityChangesName
        before = set(quality_manager.getQualityChangesGroups(stack))
        quality_manager.createQualityChanges(name)
        groups = quality_manager.getQualityChangesGroups(stack)
        created = [n for n in groups if n not in before]
        if not created:
            raise ToolError("The profile was not created")
        with self._keep_user_changes():
            machine_manager.setQualityChangesGroup(groups[created[0]], no_dialog=True)
        return {"profile": created[0],
                "changes": [_simple_change("profile", "Profile", "Профиль", old, created[0])]}

    def _save_profile(self, args, ctx):
        machine_manager = self._app.getMachineManager()
        group = machine_manager.activeQualityChangesGroup
        if group is None:
            raise ToolError("The active profile is a built-in one; use create_profile to save changes")
        from steslicer.Settings.ContainerManager import ContainerManager
        manager = ContainerManager.getInstance()
        if not manager.updateQualityChanges():
            raise ToolError("Could not update the profile")
        manager.clearUserContainers()
        return {"profile": group.name, "saved": True}

    def _materials(self, extruder):
        nodes = self._app.getMaterialManager().getAvailableMaterialsForMachineExtruder(self._global(), extruder) or {}
        return nodes

    def _list_materials(self, args, ctx):
        extruder = self._extruder(args.get("extruder"))
        query = str(args.get("query") or "").strip().lower()
        active = extruder.material.getMetaDataEntry("base_file", extruder.material.getId())
        materials = []
        for root_id, node in self._materials(extruder).items():
            item = {
                "id": root_id,
                "name": node.getMetaDataEntry("name", root_id),
                "brand": node.getMetaDataEntry("brand", ""),
                "type": node.getMetaDataEntry("material", ""),
                "color": node.getMetaDataEntry("color_name", ""),
                "active": root_id == active,
            }
            text = " ".join(str(v) for v in item.values()).lower()
            if query and not all(w in text for w in query.split()):
                continue
            materials.append(item)
        materials.sort(key=lambda m: (m["brand"], m["type"], m["name"]))
        return {"extruder": int(extruder.getMetaDataEntry("position", "0")), "active": active,
                "count": len(materials), "materials": materials[:80]}

    def _set_material(self, args, ctx):
        extruder = self._extruder(args.get("extruder"))
        position = extruder.getMetaDataEntry("position", "0")
        material_id = str(args.get("material_id") or "")
        available = self._materials(extruder)
        if material_id not in available:
            raise ToolError("Material '%s' is not available; use list_materials" % material_id)
        old = extruder.material.getName()
        with self._keep_user_changes():
            self._app.getMachineManager().setMaterialById(position, material_id)
        extruder = self._global().extruders[position]
        new = extruder.material.getName()
        return {"extruder": int(position), "material": new,
                "profile": self._app.getMachineManager().activeQualityOrQualityChangesName,
                "changes": [_simple_change("material", "Material", "Материал", old, new, scope="extruder %s" % position)]
                if old != new else []}

    # ------------------------------------------------------------------ scene

    def _scene_nodes(self):
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        nodes = []
        for node in DepthFirstIterator(self._app.getController().getScene().getRoot()):
            if not node.callDecoration("isSliceable") and not node.callDecoration("isGroup"):
                continue
            parent = node.getParent()
            if parent is not None and parent.callDecoration("isGroup"):
                continue
            nodes.append(node)
        return nodes

    def _node_id(self, node):
        key = id(node)
        if key not in self._node_ids or self._nodes.get(self._node_ids[key]) is not node:
            self._node_ids[key] = self._next_node_id
            self._nodes[self._next_node_id] = node
            self._next_node_id += 1
        return self._node_ids[key]

    def _node(self, object_id):
        try:
            node = self._nodes.get(int(object_id))
        except (TypeError, ValueError):
            node = None
        if node is None or node not in self._scene_nodes():
            raise ToolError("Object %s not found; use list_objects" % object_id)
        return node

    def _object_info(self, node):
        box = node.getBoundingBox()
        scale = node.getScale()
        info = dict(self._node_source(node), id=self._node_id(node), name=node.getName())
        if box is not None:
            info["position"] = {"x": _r(box.center.x), "y": _r(-box.center.z), "z": _r(box.bottom)}
            info["size"] = {"x": _r(box.width), "y": _r(box.depth), "z": _r(box.height)}
        info["scale_percent"] = {"x": _r(abs(scale.x) * 100), "y": _r(abs(scale.z) * 100), "z": _r(abs(scale.y) * 100)}
        outside = getattr(node, "isOutsideBuildArea", None)
        if outside is not None:
            info["fits_build_volume"] = not outside()
        return info

    def _list_objects(self, args, ctx):
        stack = self._global()
        volume = {"x": _plain(stack.getProperty("machine_width", "value")),
                  "y": _plain(stack.getProperty("machine_depth", "value")),
                  "z": _plain(stack.getProperty("machine_height", "value"))}
        return {"objects": [self._object_info(n) for n in self._scene_nodes()], "build_volume": volume,
                "printing_mode": self._mode()}

    def _load_model(self, args, ctx):
        path = os.path.abspath(os.path.expanduser(str(args.get("path") or "")))
        if not os.path.isfile(path):
            raise ToolError("File not found: %s" % path)
        if os.path.splitext(path)[1].lower() not in (".stl", ".obj", ".3mf"):
            raise ToolError("Only STL, OBJ and 3MF models are supported")
        done = threading.Event()
        state = {}

        class Listener:
            def completed(self, file_name):
                done.set()

        listener = Listener()

        def start():
            from PyQt5.QtCore import QUrl
            state["before"] = set(id(n) for n in self._scene_nodes())
            self._app.fileCompleted.connect(listener.completed)
            self._app.readLocalFile(QUrl.fromLocalFile(path), True)

        def finish():
            try:
                self._app.fileCompleted.disconnect(listener.completed)
            except Exception:
                pass
            new_nodes = [n for n in self._scene_nodes() if id(n) not in state.get("before", set())]
            return [self._object_info(n) for n in new_nodes]

        run_on_main(self._app, start)
        loaded = done.wait(180)
        time.sleep(0.5)  # Let the scene settle (drop to plate, arrange on load).
        objects = run_on_main(self._app, finish)
        if not loaded and not objects:
            raise ToolError("The model did not finish loading in 3 minutes")
        return {"loaded": os.path.basename(path), "objects": objects}

    def _move_object(self, args, ctx):
        from UM.Math.Vector import Vector
        from UM.Operations.TranslateOperation import TranslateOperation
        node = self._node(args.get("id"))
        before = self._object_info(node)
        box = node.getBoundingBox()
        relative = bool(args.get("relative"))
        dx = dy = dz = 0.0
        if args.get("x") is not None:
            dx = float(args["x"]) if relative else float(args["x"]) - box.center.x
        if args.get("y") is not None:
            dy = float(args["y"]) if relative else float(args["y"]) - (-box.center.z)
        if args.get("z") is not None:
            dz = float(args["z"]) if relative else float(args["z"]) - box.bottom
        TranslateOperation(node, Vector(dx, dz, -dy)).push()
        after = self._object_info(node)
        return {"object": after, "changes": [_simple_change("position", "Position", "Положение", _xyz(before["position"]),
                                                            _xyz(after["position"]), scope=node.getName(), unit="mm")]}

    def _rotate_object(self, args, ctx):
        from UM.Math.Quaternion import Quaternion
        from UM.Math.Vector import Vector
        from UM.Operations.RotateOperation import RotateOperation
        node = self._node(args.get("id"))
        axes = {"x": Vector(1, 0, 0), "y": Vector(0, 0, -1), "z": Vector(0, 1, 0)}
        axis = str(args.get("axis", "")).lower()
        if axis not in axes:
            raise ToolError("axis must be x, y or z")
        angle = float(args.get("angle"))
        before = self._object_info(node)
        rotation = Quaternion.fromAngleAxis(math.radians(angle), axes[axis])
        RotateOperation(node, rotation, rotate_around_point=node.getBoundingBox().center).push()
        after = self._object_info(node)
        return {"object": after, "changes": [_simple_change("rotation", "Rotation", "Поворот", None,
                                                            "%s %+g°" % (axis.upper(), angle), scope=node.getName()),
                                             _simple_change("size", "Size", "Габариты", _xyz(before["size"]),
                                                            _xyz(after["size"]), scope=node.getName(), unit="mm")]}

    def _scale_object(self, args, ctx):
        from UM.Math.Vector import Vector
        from UM.Operations.ScaleOperation import ScaleOperation
        node = self._node(args.get("id"))
        before = self._object_info(node)
        current = before["scale_percent"]
        target = dict(current)
        if args.get("size_mm") is not None:
            axis = str(args.get("axis") or "z").lower()
            size = before["size"].get(axis)
            if not size:
                raise ToolError("Cannot read the current size")
            factor = float(args["size_mm"]) / size
            target = dict((k, v * factor) for k, v in current.items())
        elif args.get("percent") is not None:
            target = dict((k, float(args["percent"])) for k in current)
        else:
            for k in ("x", "y", "z"):
                if args.get(k) is not None:
                    target[k] = float(args[k])
        if min(target.values()) <= 0:
            raise ToolError("Scale must be positive")
        old_scale = node.getScale()
        signs = [1 if old_scale.x >= 0 else -1, 1 if old_scale.y >= 0 else -1, 1 if old_scale.z >= 0 else -1]
        scale = Vector(signs[0] * target["x"] / 100.0, signs[1] * target["z"] / 100.0, signs[2] * target["y"] / 100.0)
        ScaleOperation(node, scale, set_scale=True).push()
        after = self._object_info(node)
        return {"object": after, "changes": [
            _simple_change("scale", "Scale", "Масштаб", _xyz(before["scale_percent"]), _xyz(after["scale_percent"]),
                           scope=node.getName(), unit="%"),
            _simple_change("size", "Size", "Габариты", _xyz(before["size"]), _xyz(after["size"]),
                           scope=node.getName(), unit="mm")]}

    def _arrange_all(self, args, ctx):
        done = threading.Event()

        class Listener:
            def finished(self, job):
                done.set()

        listener = Listener()

        def start():
            from steslicer.Arranging.ArrangeObjectsJob import ArrangeObjectsJob
            nodes = [n for n in self._scene_nodes() if n.isSelectable()]
            if not nodes:
                raise ToolError("The scene is empty")
            before = [self._object_info(n) for n in nodes]
            min_offset = self._app.getBuildVolume().getEdgeDisallowedSize() + 2
            job = ArrangeObjectsJob(nodes, [], min_offset=max(min_offset, 8))
            job.finished.connect(listener.finished)
            job.start()
            return before

        before = run_on_main(self._app, start)
        done.wait(60)
        time.sleep(0.3)
        after = run_on_main(self._app, lambda: [self._object_info(n) for n in self._scene_nodes()])
        old_positions = dict((o["id"], o.get("position")) for o in before)
        changes = []
        for obj in after:
            old = old_positions.get(obj["id"])
            if old and old != obj.get("position"):
                changes.append(_simple_change("position", "Position", "Положение", _xyz(old), _xyz(obj["position"]),
                                              scope=obj["name"], unit="mm"))
        return {"objects": after, "changes": changes}

    def _delete_object(self, args, ctx):
        from UM.Operations.GroupedOperation import GroupedOperation
        from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
        if args.get("all"):
            nodes = self._scene_nodes()
        else:
            nodes = [self._node(args.get("id"))]
        if not nodes:
            return {"deleted": []}
        operation = GroupedOperation()
        for node in nodes:
            operation.addOperation(RemoveSceneNodeOperation(node))
        names = [n.getName() for n in nodes]
        operation.push()
        return {"deleted": names,
                "changes": [_simple_change("object", "Object", "Объект", name, None) for name in names]}

    # ------------------------------------------------------------------ slicing and export

    def _slice(self, args, ctx):
        def start():
            if not self._scene_nodes():
                raise ToolError("The scene is empty; load a model first")
            outside = [n.getName() for n in self._scene_nodes()
                       if getattr(n, "isOutsideBuildArea", None) and n.isOutsideBuildArea()]
            if outside:
                raise ToolError("Objects outside the build volume: %s. Move, rotate or scale them." % ", ".join(outside))
            generation = self._slices.generation()
            self._app.getBackend().forceSlice()
            return generation

        generation = run_on_main(self._app, start)
        self._slices.watch((ctx or {}).get("event_seq"))
        try:
            state = self._slices.wait_finished(generation, timeout=1800)
        finally:
            self._slices.watch(None)
        if state != 3:
            raise ToolError("Slicing did not finish: %s. Check settings shown in red in the slicer."
                            % STATE_NAMES.get(state, "timeout"))
        for _ in range(30):  # Print time arrives slightly after the backend reports Done.
            result = run_on_main(self._app, self._print_info)
            if result.get("print_time_s"):
                break
            time.sleep(0.2)
        result["printing_mode"] = run_on_main(self._app, self._mode)
        return result

    def _get_slice_status(self, args, ctx):
        state, progress = self._slices.status()
        return {"state": STATE_NAMES.get(state, str(state)), "progress": progress, "printing_mode": self._mode()}

    def _print_info(self):
        from UM.Qt.Duration import DurationFormat
        info = self._app.getPrintInformation()
        duration = info.currentPrintTime
        seconds = int(duration) if duration is not None and duration.valid else 0
        weights = [float(w) for w in (info.materialWeights or [])]
        lengths = [float(l) for l in (info.materialLengths or [])]
        costs = [float(c) for c in (info.materialCosts or [])]
        return {
            "print_time_s": seconds,
            "print_time": duration.getDisplayString(DurationFormat.Format.Long) if seconds else "",
            "material_g": _r(sum(weights)),
            "material_m": _r(sum(lengths), 2),
            "material_cost": _r(sum(costs), 2) if any(costs) else None,
            "materials": [_plain(n) for n in (info.materialNames or [])],
            "job_name": info.jobName,
        }

    def _get_slice_result(self, args, ctx):
        state, _progress = self._slices.status()
        if state != 3:
            raise ToolError("No finished slice (state: %s). Call slice first." % STATE_NAMES.get(state, state))
        return self._print_info()

    def _target(self, path, file_name):
        if path:
            path = os.path.abspath(os.path.expanduser(str(path)))
        else:
            job = self._app.getPrintInformation().jobName or "project"
            job = "".join(c if c.isalnum() or c in "-_. " else "_" for c in job).strip() or "project"
            path = os.path.join(self._output_dir(), job, file_name)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        return path

    def _export_gcode(self, args, ctx):
        from UM.Mesh.MeshWriter import MeshWriter
        def prepare():
            state, _progress = self._slices.status()
            if state != 3:
                raise ToolError("Slice first: there is no G-code yet")
            return (self._app.getMeshFileHandler().getWriter("GCodeWriter"),
                    self._target(args.get("path"), "result.gcode"),
                    [self._app.getController().getScene().getRoot()])
        writer, path, nodes = run_on_main(self._app, prepare)
        if writer is None:
            raise ToolError("GCodeWriter plugin is not available")
        with open(path, "w", encoding="utf-8", newline="") as stream:
            ok = writer.write(stream, nodes, MeshWriter.OutputMode.TextMode)
        if not ok:
            raise ToolError(writer.getInformation() or "G-code was not written")
        return {"path": path, "size_kb": _r(os.path.getsize(path) / 1024.0, 1)}

    def _save_project(self, args, ctx):
        from UM.Workspace.WorkspaceWriter import WorkspaceWriter
        def prepare():
            return (self._app.getWorkspaceFileHandler().getWriter("3MFWriter"),
                    self._target(args.get("path"), "project.3mf"),
                    self._app.getController().getScene().getRoot().getChildren())
        writer, path, nodes = run_on_main(self._app, prepare)
        if writer is None:
            raise ToolError("3MFWriter plugin is not available")
        with open(path, "wb") as stream:
            ok = writer.write(stream, nodes, WorkspaceWriter.OutputMode.BinaryMode)
        if ok is False:
            raise ToolError("The project was not written")
        return {"path": path, "size_kb": _r(os.path.getsize(path) / 1024.0, 1)}


class SliceMonitor:
    """Follows state and progress of every slicing backend (3D and 5D).

    Progress is pushed into the chat event of the running `slice` tool call only, so slices
    started by the user in the slicer itself do not show up in the chat.
    """

    def __init__(self, app, bus):
        self._app = app
        self._bus = bus
        self._cond = threading.Condition()
        self._state = 1
        self._progress = 0.0
        self._generation = 0
        self._watch_seq = None
        self._last_sent = -1.0
        self._listeners = []

    def connect(self):
        try:
            backends = list(self._app.getBackendManager().getBackends().values())
        except Exception:
            backends = [self._app.getBackend()]
        for backend in backends:
            listener = _BackendListener(self, backend)
            backend.backendStateChange.connect(listener.state)
            backend.processingProgress.connect(listener.progress)
            self._listeners.append(listener)

    def _is_current(self, backend):
        try:
            return backend is self._app.getBackend()
        except Exception:
            return True

    def on_state(self, backend, state):
        if not self._is_current(backend):
            return
        state = int(state)
        with self._cond:
            if state == self._state and state != 2:
                return
            self._state = state
            self._generation += 1
            if state == 2:
                self._progress = 0.0
            elif state == 3:
                self._progress = 1.0
            self._cond.notify_all()
        self._push()

    def on_progress(self, backend, amount):
        if not self._is_current(backend):
            return
        with self._cond:
            self._progress = max(0.0, min(1.0, float(amount)))
        if abs(self._progress - self._last_sent) >= 0.02:
            self._push()

    def _push(self):
        seq = self._watch_seq
        if seq is not None:
            self._last_sent = self._progress
            self._bus.update(seq, slice_state=STATE_NAMES.get(self._state, ""), progress=round(self._progress, 3))

    def watch(self, seq):
        self._watch_seq = seq
        self._last_sent = -1.0

    def generation(self):
        with self._cond:
            return self._generation

    def status(self):
        with self._cond:
            return self._state, round(self._progress, 3)

    def wait_finished(self, generation, timeout):
        """Wait for Done/Error/Disabled after `generation`.

        NotStarted that lasts for a few seconds means the slice was cancelled or refused
        (backends report setting errors that way too); no state change at all means it never started.
        """
        start = time.time()
        deadline = start + timeout
        idle_since = None
        with self._cond:
            while True:
                now = time.time()
                if self._generation > generation:
                    if self._state in (3, 4, 5):
                        return self._state
                    if self._state == 1:
                        idle_since = idle_since or now
                        if now - idle_since > 5:
                            return 1
                    else:
                        idle_since = None
                elif now - start > 30:
                    return None
                if now >= deadline:
                    return None
                self._cond.wait(min(deadline - now, 1.0))


class _BackendListener:
    """Uranium signals keep weak references, so listeners are stored objects with bound methods."""

    def __init__(self, monitor, backend):
        self._monitor = monitor
        self._backend = backend

    def state(self, state):
        self._monitor.on_state(self._backend, state)

    def progress(self, amount):
        self._monitor.on_progress(self._backend, amount)


# ---------------------------------------------------------------------- value helpers

def _r(value, digits=1):
    try:
        return round(float(value), digits) + 0.0  # + 0.0 turns -0.0 into 0.0
    except (TypeError, ValueError):
        return None


def _xyz(d):
    if not d:
        return None
    return "%g × %g × %g" % (d["x"], d["y"], d["z"])


def _plain(value):
    """Convert setting values to JSON-friendly types."""
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), _plain(v)) for k, v in value.items())
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return str(value)


def _simple_change(key, label, label_local, old, new, scope="", unit=""):
    return {"key": key, "label": label, "label_local": label_local, "scope": scope,
            "old": old, "new": new, "old_text": None, "new_text": None, "unit": unit}


def _coerce(value, setting_type, options):
    if setting_type == "bool":
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1", "yes", "on", "да"):
                return True
            if text in ("false", "0", "no", "off", "нет"):
                return False
            raise ValueError("expected a boolean")
        return bool(value)
    if setting_type == "float":
        return float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    if setting_type == "int":
        return int(round(float(str(value).replace(",", "."))))
    if setting_type == "enum":
        text = str(value)
        if options and text not in options:
            for option_key, option_label in options.items():
                if text.strip().lower() in (str(option_key).lower(), str(option_label).lower()):
                    return option_key
            raise ValueError("allowed options: %s" % ", ".join("%s (%s)" % (k, v) for k, v in options.items()))
        return text
    if setting_type in ("extruder", "optional_extruder"):
        return str(int(value))
    if setting_type and setting_type.startswith("[") and isinstance(value, (list, tuple)):
        return str(list(value))
    return str(value) if not isinstance(value, str) else value


def _limit_warnings(key, stack, value):
    warnings = []
    try:
        number = float(value)
    except (TypeError, ValueError):
        return warnings
    checks = (("minimum_value", "below the minimum", lambda v, l: v < l),
              ("maximum_value", "above the maximum", lambda v, l: v > l),
              ("minimum_value_warning", "below the recommended minimum", lambda v, l: v < l),
              ("maximum_value_warning", "above the recommended maximum", lambda v, l: v > l))
    for prop, text, broken in checks:
        try:
            limit = stack.getProperty(key, prop)
            if limit is not None and broken(number, float(limit)):
                warnings.append("%s = %g is %s %g" % (key, number, text, float(limit)))
                if prop in ("minimum_value", "maximum_value"):
                    break
        except (TypeError, ValueError):
            continue
    return warnings
