"""Post-slice worker transport and strict live-state binding regressions."""
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent import trajectory


class Array:
    def __init__(self, value):
        self.value = value

    def tobytes(self):
        return str(self.value).encode("ascii")

    def tolist(self):
        return self.value


class Stack:
    def __init__(self):
        self.values = {"machine_width": 300, "material_diameter": 1.75,
                       "printing_mode": "cylindrical", "custom_profile_setting": 7}
        self.extruders = {}
        self.definition = SimpleNamespace(getId=lambda: "test_machine")

    def getProperty(self, key, kind):
        return self.values.get(key)

    def getAllKeys(self):
        return list(self.values)

    def getContainers(self):
        return [SimpleNamespace(getId=lambda: "live_profile")]


def registry():
    stack = Stack()
    scene = SimpleNamespace(gcode_dict={0: ["G90\nG1 X2 A10 C0 F300\n"]})
    state = {"mode": "cylindrical", "generation": 1, "slice_state": 3}
    plate = SimpleNamespace(activeBuildPlate=0)
    matrix = Array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    vertices = Array([0, 1, 2])
    mesh = SimpleNamespace(getVertices=lambda: vertices, getIndices=lambda: None)
    node = SimpleNamespace(getMeshData=lambda: mesh, getName=lambda: "part",
                           getWorldTransformation=lambda: SimpleNamespace(getData=lambda: matrix))
    app = SimpleNamespace(getGlobalContainerStack=lambda: stack,
                          getController=lambda: SimpleNamespace(getScene=lambda: scene),
                          getMultiBuildPlateModel=lambda: plate)
    result = SimpleNamespace(_app=app, core=None,
                             _mode=lambda: {"mode": state["mode"], "name": "5D"},
                             _slices=SimpleNamespace(status=lambda: (state["slice_state"], 1),
                                                     generation=lambda: state["generation"]),
                             _scene_nodes=lambda: [node])
    return result, state, scene, stack, plate, matrix, vertices


def fake_worker(monkeypatch, mutate=None, response=None, running=False):
    requests, processes = [], []

    class Process:
        def __init__(self, command, **kwargs):
            self.running = running
            self.terminated = False
            self.killed = False
            self.waited = False
            processes.append(self)
            assert "--trajectory-request" in command
            assert len(command) == 6
            assert kwargs["stdout"] == trajectory.subprocess.DEVNULL
            path = Path(command[3])
            request = json.loads(path.read_text(encoding="utf-8"))
            requests.append(request)
            value = response(request) if response else {"request_id": request["request_id"],
                        "result": {"status": "unknown", "checks": {"joint_limits": {"status": "needs_input"}}}}
            if isinstance(value.get("result"), dict):
                value["result"].setdefault("gcode_sha256", request["gcode_sha256"])
            Path(command[5]).write_text(json.dumps(value), encoding="utf-8")
            if mutate:
                mutate()

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminated = True
            self.running = False

        def kill(self):
            self.killed = True
            self.running = False

        def wait(self, **kwargs):
            self.waited = True
            return 0

    monkeypatch.setattr(trajectory.subprocess, "Popen", Process)
    monkeypatch.setattr(trajectory, "default_python", lambda: sys.executable)
    return requests, processes


def test_native_gcode_reaches_worker_and_profile_dimensions_are_only_observations(monkeypatch):
    value, _, scene, _, _, _, _ = registry()
    requests, _ = fake_worker(monkeypatch)
    result = trajectory.validate_slice(value)
    request = requests[0]
    assert request["gcode_text"] == "".join(scene.gcode_dict[0])
    assert len(request["gcode_sha256"]) == 64
    assert "machine_contract" not in request
    assert "pose_contract" not in request
    assert request["profile_observations"]["calibrated_machine_contract"] is False
    assert result["physical_print_approval"] is False
    assert trajectory.cached_validation(value) == result
    result["checks"]["joint_limits"]["status"] = "pass"
    assert trajectory.cached_validation(value)["checks"]["joint_limits"]["status"] == "needs_input"


@pytest.mark.parametrize("change", ["gcode", "mode", "plate", "profile", "mesh", "matrix", "generation", "state"])
def test_cached_result_invalidated_by_any_relevant_live_state(monkeypatch, change):
    value, state, scene, stack, plate, matrix, vertices = registry()
    fake_worker(monkeypatch)
    trajectory.validate_slice(value)
    assert trajectory.cached_validation(value) is not None
    if change == "gcode":
        scene.gcode_dict[0].append("G1 X3\n")
    elif change == "mode":
        state["mode"] = "classic"
    elif change == "plate":
        scene.gcode_dict[1] = scene.gcode_dict[0]
        plate.activeBuildPlate = 1
    elif change == "profile":
        stack.values["custom_profile_setting"] += 1
    elif change == "mesh":
        vertices.value.append(8)
    elif change == "matrix":
        matrix.value[1][3] = -7
    elif change == "generation":
        state["generation"] += 1
    else:
        state["slice_state"] = 2
    assert trajectory.cached_validation(value) is None


def test_contract_and_inputs_provenance_are_bound(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    config = {"trajectory_validation": {"machine_contract": {"source": "machine_calibration"}}}
    source = {"path": "calibration.json", "sha256": "original"}
    value.core = SimpleNamespace(_inputs=lambda: (config, source), settings=lambda: {"worker_python": sys.executable})
    monkeypatch.setattr(trajectory, "_deposition_input", lambda *_args: None)
    requests, _ = fake_worker(monkeypatch)
    trajectory.validate_slice(value)
    assert requests[0]["machine_contract"] == config["trajectory_validation"]["machine_contract"]
    assert trajectory.cached_validation(value)
    source["sha256"] = "changed"
    assert trajectory.cached_validation(value) is None


def test_scene_changed_during_worker_cannot_publish_a_valid_result(monkeypatch):
    value, _, _, stack, _, _, _ = registry()
    fake_worker(monkeypatch, mutate=lambda: stack.values.update(custom_profile_setting=8))
    result = trajectory.validate_slice(value)
    assert result["status"] == "unknown"
    assert "changed during" in result["issues"][0]["message"]
    assert trajectory.cached_validation(value) is None


def test_classic_mode_does_not_launch_worker(monkeypatch):
    value, state, _, _, _, _, _ = registry()
    state["mode"] = "classic"
    requests, _ = fake_worker(monkeypatch)
    assert trajectory.validate_slice(value) is None
    assert not requests


def test_cancelled_worker_is_terminated_and_cannot_cache(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    cancelled = {"value": False}
    _, processes = fake_worker(monkeypatch, mutate=lambda: cancelled.update(value=True), running=True)
    with pytest.raises(ValueError, match="cancelled"):
        trajectory.validate_slice(value, {"cancelled": lambda: cancelled["value"]})
    assert processes[0].terminated
    assert processes[0].waited
    assert trajectory.cached_validation(value) is None


def test_timeout_is_unknown_and_terminates_worker(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    _, processes = fake_worker(monkeypatch, running=True)
    ticks = iter([0, 121])
    monkeypatch.setattr(trajectory.time, "monotonic", lambda: next(ticks))
    result = trajectory.validate_slice(value)
    assert result["status"] == "unknown"
    assert "exceeded 120" in result["issues"][0]["message"]
    assert processes[0].terminated
    assert trajectory.cached_validation(value) is None


def test_worker_response_id_is_checked(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    fake_worker(monkeypatch, response=lambda request: {"request_id": "wrong", "result": {"status": "pass"}})
    result = trajectory.validate_slice(value)
    assert result["status"] == "unknown"
    assert "Stale" in result["issues"][0]["message"]
    assert not value._trajectory_validation


def test_worker_response_gcode_hash_is_checked_before_publishing(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    fake_worker(monkeypatch, response=lambda request: {"request_id": request["request_id"],
                         "result": {"status": "pass", "gcode_sha256": "incorrect"}})
    result = trajectory.validate_slice(value)
    assert result["validation_available"] is False
    assert "hash does not match" in result["issues"][0]["message"]
    assert not value._trajectory_validation


def test_revalidating_unchanged_slice_reuses_existing_worker_result(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    requests, _ = fake_worker(monkeypatch)
    initial = trajectory.validate_slice(value)
    assert trajectory.validate_slice(value) == initial
    assert len(requests) == 1


def test_changing_original_applied_source_invalidates_cache(monkeypatch, tmp_path):
    import threading
    value, _, _, _, _, _, _ = registry()
    path = tmp_path / "part.stl"
    path.write_bytes(b"original")
    value.core = SimpleNamespace(_inputs=lambda: ({}, None), settings=lambda: {"worker_python": sys.executable},
                    _lock=threading.RLock(), _generation=1,
                    _plans={"current": {"applied": True, "generation": 1,
                                        "request": {"mesh_path": str(path), "geometry_sha256": "original_hash"}}})
    monkeypatch.setattr(trajectory, "_deposition_input", lambda *_args: None)
    fake_worker(monkeypatch)
    trajectory.validate_slice(value)
    assert trajectory.cached_validation(value)
    path.write_bytes(b"changed")
    assert trajectory.cached_validation(value) is None


def test_actual_violation_status_is_preserved_and_never_physically_approved(monkeypatch):
    value, _, _, _, _, _, _ = registry()
    fake_worker(monkeypatch, response=lambda request: {"request_id": request["request_id"],
                "result": {"status": "fail", "physical_print_approval": True, "issues": [{"code": "axis_limit"}]}})
    result = trajectory.validate_slice(value)
    assert result["status"] == "fail"
    assert result["physical_print_approval"] is False
    assert trajectory.cached_validation(value)["status"] == "fail"


def test_real_worker_checks_native_travel_rotary_violation(monkeypatch):
    value, _, scene, _, _, _, _ = registry()
    scene.gcode_dict[0] = ["G21\nG90\nG54\nG1 X2 Y0 Z0 A10 C0 F300\n"]
    machine = {"initial_state": {"axes": {axis: 0 for axis in "XYZAC"},
                                  "coordinate_system": "G54", "absolute": True,
                                  "extrusion_absolute": True, "units": "mm", "e": 0,
                                  "feed_mm_min": 300},
               "axis_limits": dict({axis: [-100, 100] for axis in "XYZC"}, A=[0, 5]),
               "axis_limits_frame": "work", "axis_limits_coordinate_system": "G54"}
    value.core = SimpleNamespace(_inputs=lambda: ({"trajectory_validation": {"machine_contract": machine}}, None),
                                 settings=lambda: {"worker_python": sys.executable})
    monkeypatch.setattr(trajectory, "_deposition_input", lambda *_args: None)
    result = trajectory.validate_slice(value)
    assert result["validation_available"] is True, result
    assert result["status"] == "fail", result
    assert result["checks"]["axis_limits"]["status"] == "fail"
    assert result["physical_print_approval"] is False
    assert result["existing_deposition"]["status"] == "needs_input"
    assert trajectory.cached_validation(value)["gcode_sha256"] == result["gcode_sha256"]


def test_slice_tool_automatically_validates_native_dict_mode():
    from agent.tools import ToolRegistry
    value, _, _, _, _, _, _ = registry()
    value._mode_key = ToolRegistry._mode_key
    calls = []
    value._app.getBackend = lambda: SimpleNamespace(forceSlice=lambda: None)
    value._slices.watch = lambda seq: None
    value._slices.wait_finished = lambda *args, **kwargs: 3
    value._wait_settled = lambda *args: None
    value._print_info = lambda: {"print_time_s": 1}
    value._validate_5d_trajectory = lambda args, ctx: calls.append(ctx) or {"status": "unknown"}
    result = ToolRegistry._slice(value, {}, {"event_seq": 7})
    assert calls == [{"event_seq": 7}]
    assert result["trajectory_validation"]["status"] == "unknown"
    assert result["physical_print_approval"] is False


def test_slice_result_tool_includes_current_validation_with_native_dict_mode(monkeypatch):
    from agent.tools import ToolRegistry
    value, _, _, _, _, _, _ = registry()
    value._mode_key = ToolRegistry._mode_key
    value._print_info = lambda: {"print_time_s": 1}
    fake_worker(monkeypatch)
    initial = trajectory.validate_slice(value)
    result = ToolRegistry._get_slice_result(value, {}, None)
    assert result["trajectory_validation"] == initial
    assert result["physical_print_approval"] is False


@pytest.mark.parametrize("validation, error", [
    ({"status": "fail", "validation_available": True,
      "issues": [{"severity": "error", "line": 7, "message": "A exceeds limit"}]}, "export blocked"),
    ({"status": "unknown", "validation_available": False,
      "issues": [{"code": "validation_unavailable", "message": "Worker missing"}]}, "unavailable")])
def test_export_blocks_failed_or_unavailable_validation_before_writer_access(monkeypatch, validation, error):
    from agent.tools import ToolRegistry, ToolError
    value, _, _, _, _, _, _ = registry()
    value._mode_key = ToolRegistry._mode_key
    value._validate_5d_trajectory = lambda args, ctx: validation
    value._app.getMeshFileHandler = lambda: pytest.fail("Export writer accessed before validation")
    module = SimpleNamespace(MeshWriter=SimpleNamespace(OutputMode=SimpleNamespace(TextMode=1)))
    monkeypatch.setitem(sys.modules, "UM", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "UM.Mesh", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "UM.Mesh.MeshWriter", module)
    with pytest.raises(ToolError, match=error):
        ToolRegistry._export_gcode(value, {}, None)


def test_bridge_remains_python35_compatible():
    ast.parse((ROOT / "AIAssistant/agent/trajectory.py").read_text(encoding="utf-8"), feature_version=(3, 5))
