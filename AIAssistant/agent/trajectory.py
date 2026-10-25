"""Qt-safe post-slice trajectory validation through the modern Python worker.

The shell remains Python 3.5 compatible. Actual active-plate G-code, live state
and explicitly supplied machine contracts are bound to every cached result.
Build volume/profile dimensions are observations, never calibrated axis bounds.
"""
import copy
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import uuid

from .core_bridge import default_python
from .core_worker import digest
from .mainthread import run_on_main


WORKER_TIMEOUT = 120.0
MAX_GCODE_BYTES = 128 * 1024 * 1024
CONTRACT_KEYS = {"machine_contract", "pose_contract", "collision_geometry"}
PROFILE_KEYS = ("printing_mode", "machine_width", "machine_depth", "machine_height",
                "machine_center_is_zero", "machine_five_axis", "material_diameter",
                "machine_nozzle_size", "machine_nozzle_tip_outer_diameter",
                "machine_nozzle_head_distance", "cylindrical_mode_base_diameter",
                "non_printing_base_diameter", "cylindrical_mode_overlap") + tuple(
                    "machine_max_" + kind + "_" + axis
                    for kind in ("feedrate", "acceleration") for axis in ("x", "y", "z", "a", "c", "e"))


def _plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "non_finite"
    if isinstance(value, dict):
        return dict((str(k), _plain(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "tolist"):
        return _plain(value.tolist())
    return str(value)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def _contracts(registry):
    core = getattr(registry, "core", None)
    options, provenance = core._inputs() if core is not None else ({}, None)
    value = options.get("trajectory_validation", {})
    if not isinstance(value, dict) or set(value) - CONTRACT_KEYS:
        raise ValueError("trajectory_validation allows only machine_contract, pose_contract, collision_geometry")
    for key in CONTRACT_KEYS:
        if key in value and value[key] is not None and not isinstance(value[key], dict):
            raise ValueError("trajectory_validation.%s must be a JSON object" % key)
    return copy.deepcopy(value), provenance


def _property(stack, key):
    try:
        return _plain(stack.getProperty(key, "value"))
    except Exception:
        return None


def _profile(stack):
    keys = set(PROFILE_KEYS)
    getter = getattr(stack, "getAllKeys", None)
    if callable(getter):
        keys.update(getter())
    return dict((key, _property(stack, key)) for key in sorted(keys))


def _applied_sources(registry):
    core = getattr(registry, "core", None)
    if core is None or not hasattr(core, "_plans"):
        return []
    sources = []
    with core._lock:
        for plan_id, record in sorted(core._plans.items()):
            if not record.get("applied"):
                continue
            request = record.get("request") or {}
            path = request.get("mesh_path")
            current = digest(path) if path and os.path.isfile(path) else None
            sources.append({"plan_id": plan_id, "generation": record.get("generation"),
                            "current_generation": core._generation,
                            "expected_geometry_sha256": request.get("geometry_sha256"),
                            "actual_geometry_sha256": current})
    return sources


def _snapshot(registry):
    """Only called in Qt; no copies of native objects escape this function."""
    mode_value = registry._mode()
    mode = mode_value.get("mode") if isinstance(mode_value, dict) else mode_value
    if mode == "classic":
        return {"mode": mode}
    app = registry._app
    state, _progress = registry._slices.status()
    if state != 3:
        raise ValueError("No finished slice available for trajectory validation")
    scene = app.getController().getScene()
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    chunks = getattr(scene, "gcode_dict", {}).get(plate, [])
    if isinstance(chunks, str):
        text = chunks
    else:
        text = "".join(chunks)
    if not text.strip():
        raise ValueError("Finished slice has no active-plate G-code")
    raw = text.encode("utf-8")
    if len(raw) > MAX_GCODE_BYTES:
        raise ValueError("Active-plate G-code exceeds the 128 MiB validation limit")
    stack = app.getGlobalContainerStack()
    if stack is None:
        raise ValueError("No active printer")
    stacks = [stack] + [s for _key, s in sorted(stack.extruders.items())]
    profiles = [_profile(s) for s in stacks]
    containers = [[c.getId() for c in s.getContainers()] for s in stacks]
    nodes = []
    for node in registry._scene_nodes():
        mesh = node.getMeshData()
        if mesh is None:
            continue
        indices = mesh.getIndices()
        mesh_hash = hashlib.sha256(mesh.getVertices().tobytes() +
                                   (indices.tobytes() if indices is not None else b"")).hexdigest()
        nodes.append({"name": node.getName(), "mesh_sha256": mesh_hash,
                      "matrix": _plain(node.getWorldTransformation().getData())})
    machine_id = stack.definition.getId()
    return {"mode": mode, "gcode_text": text, "gcode_sha256": hashlib.sha256(raw).hexdigest(),
            "scene_sha256": _hash(nodes), "profile_sha256": _hash([profiles, containers]),
            "active_plate": plate, "machine_id": machine_id,
            "slice_generation": registry._slices.generation(),
            "applied_sources": _applied_sources(registry),
            "profile_observations": {"source": "active_slicer_profile", "machine_id": machine_id,
                                     "calibrated_machine_contract": False,
                                     "values": dict((key, profiles[0].get(key)) for key in PROFILE_KEYS)},
            "filament_diameter_mm": (profiles[1].get("material_diameter") if len(profiles) > 1
                                     else profiles[0].get("material_diameter"))}


def _binding(snapshot, contracts, provenance):
    return dict((key, snapshot.get(key)) for key in
                ("mode", "gcode_sha256", "scene_sha256", "profile_sha256", "active_plate",
                 "machine_id", "slice_generation", "applied_sources")), _hash({"contracts": contracts, "inputs": provenance})


def _deposition_input(registry, snapshot):
    """Use original-part registration only from a still-valid applied core plan."""
    core = getattr(registry, "core", None)
    if core is None:
        return None
    with core._lock:
        for plan_id, record in list(core._plans.items()):
            if not record.get("applied") or record.get("generation") != core._generation:
                continue
            decision = record.get("decision") or {}
            if (decision.get("route") or {}).get("mode") != snapshot["mode"]:
                continue
            try:
                verified = core.verify({"plan_id": plan_id})
            except Exception:
                continue
            if verified.get("status") == "pass" and verified.get("source_unchanged") and verified.get("scene_unchanged"):
                return copy.deepcopy(decision)
    return None


def cached_validation(registry):
    """Return a defensive copy only while G-code, scene, settings and contract match.

    Safe on Qt (get_slice_result/export prepare) and on the tool worker.
    """
    cached = getattr(registry, "_trajectory_validation", None)
    if not cached:
        return None
    try:
        snapshot = run_on_main(registry._app, _snapshot, registry)
        contracts, provenance = _contracts(registry)
        if cached["binding"] != _binding(snapshot, contracts, provenance):
            return None
    except Exception:
        return None
    return copy.deepcopy(cached["result"])


def _unavailable(message, mode=None):
    return {"status": "unknown", "validation_available": False,
            "printing_mode": mode, "physical_print_approval": False,
            "issues": [{"code": "validation_unavailable", "message": str(message)}],
            "checks": {"kinematics": {"status": "unknown"}, "collisions": {"status": "unknown"}}}


def validate_slice(registry, ctx=None):
    """Validate actual native output; cancellation never publishes a partial result."""
    cancelled = (ctx or {}).get("cancelled", lambda: False)
    if cancelled():
        raise ValueError("Trajectory validation cancelled")
    previous = cached_validation(registry)
    if previous is not None:
        return previous
    registry._trajectory_validation = None
    snapshot = None
    try:
        snapshot = run_on_main(registry._app, _snapshot, registry)
        if snapshot["mode"] == "classic":
            return None
        contracts, provenance = _contracts(registry)
        binding = _binding(snapshot, contracts, provenance)
        core = getattr(registry, "core", None)
        settings = core.settings() if core is not None else {}
        python = settings.get("worker_python") or default_python()
        if not python or not os.path.isfile(python):
            raise ValueError("Set worker_python to modern Python with project requirements installed")
        request_id = uuid.uuid4().hex
        request = dict(contracts, request_id=request_id, gcode_text=snapshot["gcode_text"],
                       gcode_sha256=snapshot["gcode_sha256"], printing_mode=snapshot["mode"],
                       profile_observations=snapshot["profile_observations"],
                       filament_diameter_mm=snapshot["filament_diameter_mm"])
        decision = run_on_main(registry._app, _deposition_input, registry, snapshot)
        if decision is not None:
            request["deposition_decision"] = decision
        with tempfile.TemporaryDirectory(prefix="ste_trajectory_") as folder:
            input_path, output_path = os.path.join(folder, "request.json"), os.path.join(folder, "response.json")
            with open(input_path, "w", encoding="utf-8") as handle:
                json.dump(request, handle, ensure_ascii=False, allow_nan=False)
            command = [python, os.path.join(os.path.dirname(__file__), "core_worker.py"),
                       "--trajectory-request", input_path, "--response", output_path]
            if cancelled():
                raise ValueError("Trajectory validation cancelled")
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                started = time.monotonic()
                while process.poll() is None:
                    if cancelled():
                        raise ValueError("Trajectory validation cancelled")
                    if time.monotonic() - started > WORKER_TIMEOUT:
                        raise ValueError("Trajectory validation exceeded 120 seconds")
                    time.sleep(0.05)
                if cancelled():
                    raise ValueError("Trajectory validation cancelled")
                if not os.path.isfile(output_path):
                    raise ValueError("Trajectory worker failed; check worker_python and dependencies")
                with open(output_path, encoding="utf-8") as handle:
                    response = json.load(handle)
                if response.get("error"):
                    raise ValueError(response["error"])
                if response.get("request_id") != request_id or not isinstance(response.get("result"), dict):
                    raise ValueError("Stale or malformed trajectory worker response")
                result = response["result"]
                if result.get("gcode_sha256") != snapshot["gcode_sha256"]:
                    raise ValueError("Trajectory worker G-code hash does not match the sliced output")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait()
        current = run_on_main(registry._app, _snapshot, registry)
        current_contracts, current_provenance = _contracts(registry)
        if binding != _binding(current, current_contracts, current_provenance):
            raise ValueError("Scene, settings, G-code or machine contract changed during trajectory validation")
        if cancelled():
            raise ValueError("Trajectory validation cancelled")
        result.update({"physical_print_approval": False, "validation_available": True,
                       "printing_mode": snapshot["mode"],
                       "gcode_sha256": snapshot["gcode_sha256"], "contract_sha256": binding[1],
                       "profile_observations": snapshot["profile_observations"]})
        registry._trajectory_validation = {"binding": binding, "result": copy.deepcopy(result)}
        return result
    except Exception as exc:
        if cancelled():
            raise ValueError("Trajectory validation cancelled")
        return _unavailable(exc, snapshot.get("mode") if snapshot else None)
