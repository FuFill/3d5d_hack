"""Python 3.5 shell -> modern worker -> canonical Qt runtime adapter.

Only application-owned decisions can be applied. A cancelled/stale worker cannot
replace a newer plan, and a changed scene, printer or source invalidates apply.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .core_worker import core_path, digest
from .mainthread import run_on_main


def default_python():
    configured = os.environ.get("STE_PLANNER_PYTHON")
    if configured:
        return configured
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for path in (os.path.join(root, ".venv-functional", "Scripts", "python.exe"),
                 os.path.join(root, ".venv", "Scripts", "python.exe")):
        if os.path.isfile(path):
            return path
    return shutil.which("python3") or shutil.which("python") or ""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


class CoreBridge:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self._lock = threading.RLock()
        self._generation = 0
        self._plans = {}
        self._process = None
        # Set by the shell: scene object id -> live node (Qt thread), for in-place apply.
        self.resolve_node = None

    def _inputs(self):
        path = self.settings().get("planning_inputs_file") or ""
        if not path:
            return {}, None
        if not os.path.isabs(path) or not os.path.isfile(path) or os.path.getsize(path) > 10 * 1024 * 1024:
            raise ValueError("Planning inputs must be an existing absolute JSON path (max 10 MiB)")
        before = digest(path)
        with open(path, encoding="utf-8-sig") as handle:
            values = json.load(handle)
        allowed = {"calibration_data", "functional_spec", "noise_spec", "robustness_limits"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("Planning inputs allow only calibration_data, functional_spec, noise_spec, robustness_limits")
        if digest(path) != before:
            raise ValueError("Planning inputs changed while reading")
        return values, {"path": path, "sha256": before}

    def cancel(self):
        with self._lock:
            self._generation += 1
            process = self._process
            self._process = None
        if process is not None and process.poll() is None:
            process.terminate()

    def _snapshot(self):
        # Called only in Qt. READBACK_KEYS is shared with the original adapter.
        sys.path.insert(0, core_path()) if core_path() not in sys.path else None
        from ste_planner.planner import READBACK_KEYS
        stack = self.app.getGlobalContainerStack()
        if stack is None:
            raise ValueError("Configure a printer before planning")
        nodes = []
        for node in self.app.getController().getScene().getRoot().getAllChildren():
            mesh = node.getMeshData()
            if mesh is not None and node.callDecoration("isSliceable"):
                indices = mesh.getIndices()
                nodes.append({"name": node.getName(), "matrix": node.getWorldTransformation().getData().tolist(),
                              "mesh": hashlib.sha256(mesh.getVertices().tobytes() +
                                       (indices.tobytes() if indices is not None else b"")).hexdigest()})
        resources = os.path.join(os.path.dirname(sys.executable), "resources")
        # STE's Resources enum differs between builds; definitions are the authority.
        if not os.path.isdir(os.path.join(resources, "definitions")):
            from ste_planner.planner import default_resources_dir
            resources = default_resources_dir()
        catalog = None
        if self.settings().get("native_presets", True):
            # Installed registry snapshot (no API keys, no project files); the
            # worker selects the base only from these application-owned ids.
            from ste_planner.runtime_adapter import build_preset_catalog
            from ste_planner.presets import catalog_digest
            try:
                catalog = build_preset_catalog(self.app)
            except Exception as exc:
                raise ValueError("Cannot read the STE preset catalog (%s: %s); disable native_presets "
                                 "only to fall back to the planner process profile" % (type(exc).__name__, exc))
        state = {"machine_id": stack.definition.getId(), "nodes": nodes,
                 "plate": self.app.getMultiBuildPlateModel().activeBuildPlate,
                 "containers": [[c.getId() for c in s.getContainers()]
                                for s in [stack] + list(stack.extruders.values())],
                 "settings": [{k: s.getProperty(k, "value") for k in READBACK_KEYS}
                              for s in [stack] + list(stack.extruders.values())],
                 "preset_catalog_sha256": catalog_digest(catalog) if catalog is not None else None}
        # Pose without the horizontal position (scene x and z; Y is up): moving a part on the plate
        # does not change its classic slicing, rotation/height/scale/mesh do.
        poses = [dict(n, matrix=[[0.0 if (r, c) in ((0, 3), (2, 3)) else v for c, v in enumerate(row)]
                                 for r, row in enumerate(n["matrix"])]) for n in nodes]
        return {"signature": _hash(state), "scene_signature": _hash({"nodes": nodes, "plate": state["plate"]}),
                "pose_signature": _hash({"nodes": poses, "plate": state["plate"]}),
                "machine_id": state["machine_id"], "resources_dir": resources, "preset_catalog": catalog}

    def plan(self, args, context=None):
        text, path = args.get("task_text"), args.get("mesh_path")
        if not isinstance(text, str) or not text.strip() or len(text) > 100000:
            raise ValueError("task_text must be a nonempty source specification (max 100000 characters)")
        if not isinstance(path, str) or not os.path.isabs(path) or not path.lower().endswith(".stl") or not os.path.isfile(path):
            raise ValueError("Select an existing absolute STL path; OBJ/3MF remain available through manual tools")
        mode = args.get("mode")
        if mode is not None and mode not in ("classic", "cylindrical", "cylindrical_full"):
            raise ValueError("Unsupported printing mode")
        target = args.get("target_object_id")
        if target is not None and (isinstance(target, bool) or not isinstance(target, int)):
            raise ValueError("target_object_id must be a scene object id")
        orientation = args.get("orientation")
        if not (orientation is None or orientation in ("balanced", "strength") or
                (isinstance(orientation, dict) and set(orientation) == {"rotation"})):
            raise ValueError("orientation must be balanced, strength or the current pose of the model")
        snapshot = run_on_main(self.app, self._snapshot)
        options, inputs = self._inputs()
        request_id = uuid.uuid4().hex
        allowed = self.settings().get("allowed_quality_changes") or []
        if not isinstance(allowed, list) or not all(isinstance(n, str) for n in allowed):
            raise ValueError("allowed_quality_changes must be a list of custom profile names")
        request = dict(snapshot, request_id=request_id, task_text=text, mesh_path=path,
                       geometry_sha256=digest(path), mode=mode, llm_items=args.get("llm_items"),
                       planner_options=options, inputs=inputs, allowed_quality_changes=allowed,
                       target_object_id=target, orientation=orientation,
                       preferred_materials=[str(m) for m in (args.get("preferred_materials") or [])][:3] or None)
        with self._lock:
            if self._process is not None:
                raise ValueError("Planning is already running; cancel it first")
            self._generation += 1
            generation = self._generation
        python = self.settings().get("worker_python") or default_python()
        if not python or not os.path.isfile(python):
            raise ValueError("Set worker_python to modern Python with the project requirements installed")
        with tempfile.TemporaryDirectory(prefix="ste_planner_request_") as folder:
            input_path, output_path = os.path.join(folder, "request.json"), os.path.join(folder, "response.json")
            with open(input_path, "w", encoding="utf-8") as handle:
                json.dump(request, handle, ensure_ascii=False, allow_nan=False)
            command = [python, os.path.join(os.path.dirname(__file__), "core_worker.py"),
                       "--request", input_path, "--response", output_path]
            with self._lock:
                if generation != self._generation:
                    raise ValueError("Planning cancelled")
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           creationflags=0x08000000 if os.name == "nt" else 0)
                self._process = process
            try:
                started = time.monotonic()
                while process.poll() is None:
                    if ((context or {}).get("cancelled", lambda: False)() or
                            time.monotonic() - started > 180):
                        self.cancel()
                        raise ValueError("Planning cancelled or exceeded 180 seconds")
                    time.sleep(0.05)
                if generation != self._generation:
                    raise ValueError("Planning cancelled")
                if not os.path.isfile(output_path):
                    raise ValueError("Planner worker failed; check modern Python and installed dependencies")
                with open(output_path, encoding="utf-8") as handle:
                    result = json.load(handle)
                if result.get("error"):
                    raise ValueError(result["error"])
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait()
                with self._lock:
                    if self._process is process:
                        self._process = None
        if (result.get("request_id") != request_id or digest(path) != request["geometry_sha256"] or
                self._inputs()[1] != inputs):
            raise ValueError("Stale response or changed source STL")
        current = run_on_main(self.app, self._snapshot)
        with self._lock:
            if generation != self._generation or current["signature"] != snapshot["signature"]:
                raise ValueError("Project changed during planning; calculate a new plan")
            self._plans = {request_id: {"decision": result["decision"], "request": request,
                                       "generation": generation, "applied": False}}
            return self.summary(request_id)

    def _record(self, plan_id):
        record = self._plans.get(plan_id)
        if record is None or record["generation"] != self._generation:
            raise ValueError("Unknown, cancelled or superseded plan_id; plan again")
        return record

    def decision_record(self, plan_id):
        """Copies of the application-owned decision and request (used to improve an open project)."""
        with self._lock:
            record = self._record(plan_id)
            return {"decision": copy.deepcopy(record["decision"]), "request": copy.deepcopy(record["request"])}

    def summary(self, plan_id):
        with self._lock:
            record = self._record(plan_id)
            decision = record["decision"]
            base = decision.get("base_presets")
            selection = decision.get("preset_selection") or {"status": "legacy_without_provenance",
                                                             "native_preset": False}
            return dict({k: copy.deepcopy(decision.get(k)) for k in
                         ("status", "part_name", "chosen_material", "route", "orientation", "effective_settings",
                          "missing_inputs", "explanation", "prototype_only")},
                        plan_id=plan_id, applied=record["applied"], physical_print_approval=False,
                        # Display names and reasons only; container ids/stack indices stay internal.
                        base_preset={"native_preset": bool(base), "status": selection.get("status"),
                                     "profile": base.get("label") if base else None,
                                     "reason": selection.get("reason"), "options": selection.get("options"),
                                     "rejected": [{"label": a.get("candidate") or a.get("quality_changes") or
                                                            a.get("quality_type") or a.get("id"),
                                                   "reason": a.get("reason")}
                                                  for a in selection.get("rejected") or []],
                                     "alternatives": [a["label"] for a in selection.get("alternatives") or []]},
                        setting_changes=[{k: c.get(k) for k in ("key", "base_value", "effective_value", "unit",
                                                                "reason", "justification")}
                                         for c in (decision.get("setting_patch") or {}).get("changes") or []],
                        # Feasible core materials (ids for plan_print preferred_material).
                        material_options=[{"id": m.get("id"), "summary": m.get("summary")}
                                          for m in ((decision.get("candidates") or {}).get("materials") or [])[:6]],
                        geometry_sha256=record["request"]["geometry_sha256"],
                        decision_sha256=_hash(decision),
                        # The loaded model is reoriented in place; otherwise apply loads the source.
                        apply_target=({"object_id": record["request"]["target_object_id"], "in_place": True}
                                      if record["request"].get("target_object_id") is not None else
                                      {"in_place": False, "requires_empty_project": True}),
                        assessments={k: {"status": decision.get(k, {}).get("status")}
                                     for k in ("causal_analysis", "functional_analysis", "robustness_analysis")},
                        next_step="Use the current task's existing execution consent, or ask once if it has not been "
                                  "granted, then apply_plan, verify_plan, slice, export_gcode and save_project. "
                                  "Reading/recalculating this task does not require renewed consent; "
                                  "keep unresolved qualification requirements visible")

    def apply(self, args, context=None):
        # ToolRegistry invokes this on Qt; holding the lock fences cancellation.
        with self._lock:
            record = self._record(args.get("plan_id"))
            if (context or {}).get("cancelled", lambda: False)():
                raise ValueError("Cancelled before apply")
            if record["applied"]:
                raise ValueError("Plan already applied; use slicer Undo/Redo")
            if self._snapshot()["signature"] != record["request"]["signature"]:
                raise ValueError("Project changed since planning; calculate a new plan")
            if self._inputs()[1] != record["request"].get("inputs"):
                raise ValueError("Calibration/functional/noise inputs changed; calculate a new plan")
            target = None
            if record["request"].get("target_object_id") is not None:
                if self.resolve_node is None:
                    raise ValueError("Scene objects cannot be resolved in this shell; plan from an attachment")
                target = self.resolve_node(record["request"]["target_object_id"])
            from ste_planner.runtime_adapter import create_operation
            options = {"target_node": target}
            if args.get("confirm_unverified_5d") is True:
                options["allow_unverified_5d"] = True  # the user accepted the unverified 5D warning in chat
            operation = create_operation(self.app, record["decision"], record["request"]["mesh_path"], **options)
            operation.redo()
            operation.push()
            record["operation"], record["applied"] = operation, True
            # STE may replace equivalent quality-container ids asynchronously.
            # Check geometry here and actual values through canonical readback.
            applied = self._snapshot()
            record["applied_signature"] = applied["scene_signature"]
            record["applied_pose_signature"] = applied.get("pose_signature")
            return {"plan_id": args["plan_id"], "status": "applied", "readback": operation.readback,
                    "physical_print_approval": False, "undoable": True}

    def verify(self, args, context=None):
        with self._lock:
            record = self._record(args.get("plan_id"))
            if not record["applied"]:
                raise ValueError("Apply the plan first")
            from ste_planner.runtime_adapter import readback
            result = readback(self.app, record["decision"])
            result["source_unchanged"] = digest(record["request"]["mesh_path"]) == record["request"]["geometry_sha256"]
            current = self._snapshot()
            if ((record["decision"].get("route") or {}).get("mode") == "classic" and
                    record.get("applied_pose_signature") is not None):
                # Classic: the part may be moved on the plate; rotation, height and mesh must stay planned.
                result["scene_unchanged"] = current.get("pose_signature") == record["applied_pose_signature"]
                result["moved_on_plate"] = current["scene_signature"] != record["applied_signature"]
            else:
                # 5D: the position defines the part axis on the tooling axis.
                result["scene_unchanged"] = current["scene_signature"] == record["applied_signature"]
            if not result["source_unchanged"] or not result["scene_unchanged"]:
                result["status"] = "fail"
            result["physical_print_approval"] = False
            return result
