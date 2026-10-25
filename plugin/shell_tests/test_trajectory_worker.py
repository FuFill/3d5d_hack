"""Worker dispatch reuses existing deposition instead of replacing its physics."""
import copy
import hashlib
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent.core_bridge import CoreBridge
from agent.core_worker import calculate_trajectory


def request():
    text = "G21\nG90\nM83\nG56\nG0 X0 Y0 Z10 A90 C0\n;TYPE:WALL-OUTER\nG1 C360 E4 F600\n"
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    return {"request_id": "worker-test", "gcode_text": text,
            "gcode_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "printing_mode": "cylindrical", "filament_diameter_mm": 2.,
            "pose_contract": {"type": "table_AC", "source": "analysis_assumption", "angle_unit": "degree",
                "interpolation": "linear_unwrapped_joints", "table_pivot_mm": [0, 0, 0],
                "nozzle_axis_machine": [0, 0, 1], "work_offsets": {"G56": identity}},
            "deposition_decision": {"route": {"mode": "cylindrical"},
                "effective_settings": {"machine_center_is_zero": True},
                "scene_operations": [{"op": "set_transform", "rotation": [r[:3] for r in identity[:3]],
                                      "translation": [0, 0, 0]}]}}


def test_worker_connects_existing_rotary_deposition_and_all_move_validation():
    result = calculate_trajectory(request())["result"]
    assert result["coverage"]["moves"] == 2
    assert result["existing_deposition"]["status"] == "computed"
    summary = result["existing_deposition"]["value"]["summary"]
    import math
    assert summary["part_volume_mm3"] == pytest.approx(4 * math.pi)
    assert summary["part_path_length_mm"] == pytest.approx(20 * math.pi, rel=.001)
    assert summary["pose_source"] == "analysis_assumption"
    assert result["checks"]["kinematics"]["status"] == "unknown"
    assert result["physical_print_approval"] is False


def test_worker_rejects_modified_gcode_before_validation():
    value = request()
    value["gcode_text"] += "G0 A200\n"
    with pytest.raises(ValueError, match="hash mismatch"):
        calculate_trajectory(value)


def test_trajectory_contract_is_application_owned_input(tmp_path):
    import json
    path = tmp_path / "inputs.json"
    values = {"trajectory_validation": {"pose_contract": request()["pose_contract"]}}
    path.write_text(json.dumps(values), encoding="utf-8")
    bridge = CoreBridge(None, lambda: {"planning_inputs_file": str(path)})
    options, provenance = bridge._inputs()
    assert options == values and provenance["sha256"]
    path.write_text(json.dumps({"trajectory_validation": "fake"}), encoding="utf-8")
    with pytest.raises(ValueError, match="contract object"):
        bridge._inputs()
