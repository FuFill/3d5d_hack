"""Offline Qt -> tool -> modern worker -> native writer regression, no printer motion."""
import importlib
import json
import os
import threading
import time
from types import SimpleNamespace

from UM.PluginRegistry import PluginRegistry


class TrajectoryCheck:
    def __init__(self, app, command):
        self.app, self.command = app, command
        extension = PluginRegistry.getInstance().getPluginObject("AIAssistant")
        package = type(extension).__module__.rsplit(".", 1)[0]
        tools = importlib.import_module(package + ".agent.tools")
        bridge = importlib.import_module(package + ".agent.core_bridge")
        self.folder = command["output_dir"]
        os.makedirs(self.folder, exist_ok=True)
        self.inputs = os.path.join(self.folder, "inputs.json")
        settings = lambda: {"worker_python": os.environ["STE_PLANNER_PYTHON"], "planning_inputs_file": self.inputs}
        self.registry = tools.ToolRegistry(app, SimpleNamespace(status=lambda: (3, 1.), generation=lambda: 1),
                                           lambda: self.folder, core=bridge.CoreBridge(app, settings))
        self.results, self.stage, self.thread, self.box = [], 0, None, {}
        app.getGlobalContainerStack().userChanges.setProperty("printing_mode", "value", "cylindrical")
        app.getBackendManager()._onSettingChanged("printing_mode", "value")
        self.ready_at = time.monotonic() + 2.
        self.contract = {"machine_contract": {"source": "software_fixture_only",
            "initial_state": {"axes": {"X": 0., "Y": 0., "Z": 10., "A": 0., "C": 0.},
                              "coordinate_system": "G54", "units": "mm", "absolute": True,
                              "extrusion_absolute": False, "e": 0., "feed_mm_min": 600.},
            "axis_limits": {"X": [-100, 100], "Y": [-100, 100], "Z": [0, 100],
                            "A": [-90, 90], "C": [-720, 720]},
            "axis_limits_frame": "work", "axis_limits_coordinate_system": "G54",
            "max_feed_mm_min": 1200., "feed_mode": "linear_xyz_mm_per_min", "rapid_uses_feed": True},
            "pose_contract": {"type": "table_AC", "source": "analysis_assumption", "angle_unit": "degree",
                "interpolation": "linear_unwrapped_joints", "table_pivot_mm": [0, 0, 0],
                "nozzle_axis_machine": [0, 0, 1], "work_offsets": {
                    "G54": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]}},
            "collision_geometry": {"frame": "machine", "clearance_mm": .2,
                "tool_spheres": [{"name": "head_fixture", "center_mm": [0, 0, 0], "radius_mm": .5}],
                "obstacles": [{"name": "clamp_fixture", "type": "sphere", "center_mm": [10, 0, 10], "radius_mm": 1}]}}

    def advance(self):
        if time.monotonic() < self.ready_at:
            return None
        if self.thread is not None:
            if self.thread.is_alive():
                return None
            if "error" in self.box:
                raise ValueError(self.box["error"])
            validation = self.box["validation"]
            assert validation.get("validation_available"), validation
            assert validation["physical_print_approval"] is False
            if self.stage == 0:
                assert validation["checks"]["axis_limits"]["status"] == "fail", validation
                assert validation["checks"]["collisions"]["status"] == "fail", validation
                assert not self.box["export"]["ok"] and "blocked" in self.box["export"]["result"]["error"]
                assert not os.path.exists(os.path.join(self.folder, "blocked.gcode"))
            else:
                assert validation["checks"]["axis_limits"]["status"] == "pass", validation
                assert validation["checks"]["collisions"]["geometry_result"]["status"] == "pass", validation
                assert self.box["export"]["ok"], self.box["export"]
                exported = self.box["export"]["result"]
                assert os.path.isfile(exported["path"]) and os.path.isfile(exported["validation_report_path"])
            self.results.append(self.box)
            self.stage += 1
            self.thread = None
            if self.stage == 2:
                return {"status": "pass", "checks": ["real_Qt_worker_transport", "axis_violation",
                        "continuous_static_collision", "failed_export_blocked", "native_analysis_export_and_sidecar"],
                        "software_fixture_only": True, "results": self.results}
        with open(self.inputs, "w", encoding="utf-8") as handle:
            json.dump({"trajectory_validation": self.contract}, handle)
        text = ("G21\nG90\nM83\nG54\nG1 X20 Y0 Z10 A120 C0 E1 F600\n" if self.stage == 0 else
                "G21\nG90\nM83\nG54\nG1 X-20 Y0 Z10 A20 C0 E1 F600\nG0 Y10\n")
        scene = self.app.getController().getScene()
        scene.gcode_dict = {self.app.getMultiBuildPlateModel().activeBuildPlate: [text]}
        self.box = {}
        filename = "blocked.gcode" if self.stage == 0 else "analysis.gcode"
        def work():
            try:
                result = self.registry.call("validate_5d_trajectory", {})
                if not result["ok"]:
                    raise ValueError(result)
                self.box["validation"] = result["result"]
                self.box["export"] = self.registry.call("export_gcode", {"path": os.path.join(self.folder, filename)})
            except Exception as exc:
                self.box["error"] = str(exc)
        self.thread = threading.Thread(target=work)
        self.thread.daemon = True
        self.thread.start()
        return None
