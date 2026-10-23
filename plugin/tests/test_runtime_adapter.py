"""Coordinate/hash/closed-volume guards, without pretending mocks prove a slice."""
import copy
import os

import numpy as np

from testutil import make_box, raises, assert_close
from test_qwen_integration import box_model
from ste_planner import planner, gcode
from ste_planner import runtime_adapter as A


def test_scene_axis_conversion_and_placed_modifier_parenting():
    rotation = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    parent = A.scene_transform(rotation, [12, -7, 5])
    mesh = make_box(2, 3, 4, origin=(10, 20, 30))
    operation = {"op": "add_modifier_mesh", "role": "support_blocker", "parent_model_index": 0,
                 "vertices_mm": mesh.vertices.tolist(), "faces": mesh.faces.tolist(),
                 "settings": {"anti_overhang_mesh": True}}
    local, _ = A.modifier_local_vertices(operation, parent)
    restored = local.dot(parent[:3, :3].T) + parent[:3, 3]
    assert np.allclose(restored, mesh.vertices.dot(A.PRINTER_TO_SCENE.T))
    moved = parent.copy()
    moved[:3, 3] += [6, 0, -2]
    assert np.allclose(local.dot(moved[:3, :3].T) + moved[:3, 3] - restored, [6, 0, -2])
    assert np.allclose(A.PRINTER_TO_SCENE.dot([1, 2, 3]), [1, 3, -2])


def test_modifiers_reject_open_or_relabelled_volumes():
    mesh = make_box(2, 3, 4)
    operation = {"op": "add_modifier_mesh", "role": "support_enforcer", "parent_model_index": 0,
                 "vertices_mm": mesh.vertices.tolist(), "faces": mesh.faces.tolist(),
                 "settings": {"support_mesh": True, "support_mesh_drop_down": True}}
    A.modifier_local_vertices(operation, np.eye(4))
    bad = copy.deepcopy(operation)
    bad["faces"].pop()
    raises(A.ApplyError, A.modifier_local_vertices, bad, np.eye(4))
    bad = copy.deepcopy(operation)
    bad["settings"] = {"infill_mesh": True}
    raises(A.ApplyError, A.modifier_local_vertices, bad, np.eye(4))


def test_runtime_hash_context_and_5d_are_checked_before_writes():
    with box_model() as path:
        decision = planner.plan_case("Тестовая деталь", path, forced_mode="classic")
        A.preflight(decision, path, planner.DEFAULT_MACHINE)
        bad = copy.deepcopy(decision)
        bad["calibration_context"]["geometry_sha256"] = "0" * 64
        raises(A.ApplyError, A.preflight, bad, path, planner.DEFAULT_MACHINE)
        bad = copy.deepcopy(decision)
        bad["calibration_context"]["material_id"] = "other"
        raises(A.ApplyError, A.preflight, bad, path, planner.DEFAULT_MACHINE)
        for mode in ("cylindrical", "cylindrical_full"):
            bad = copy.deepcopy(decision)
            bad["route"]["mode"] = mode
            raises(A.ApplyError, A.preflight, bad, path, planner.DEFAULT_MACHINE, True)


def test_reject_reflection_or_nonfinite_scene_transform():
    raises(A.ApplyError, A.scene_transform, np.diag([1, 1, -1]), [0, 0, 0])
    raises(A.ApplyError, A.scene_transform, np.eye(3), [float("nan"), 0, 0])


def test_partial_engine_stream_with_missing_initial_layers_is_not_pass():
    text = ";LAYER:20\nG1 X1 Z4.2 E1 F100\n;LAYER:21\nG1 X2 Z4.4 E2\n"
    result = gcode.inspect_toolpath(text, {"printing_mode": "classic"})
    assert next(c for c in result["checks"] if c["check"] == "layer_sequence")["status"] == "fail"


def test_support_contact_uses_segment_geometry_line_width_and_z_gap():
    region = {"id": "patch", "role": "required", "triangles_mm": [[[0, 0, 5], [10, 0, 5], [0, 10, 5]]]}
    text = "G90\nM82\nG0 X-1 Y1 Z4.6\n;TYPE:SUPPORT\nG1 X5 E1 F100\n"
    assert gcode.inspect_support_contacts(text, [region], .4, .2, .2)["regions"][0]["status"] == "pass"
    # Identical feature comments away from the patch do not prove contact.
    text = text.replace("Y1", "Y-2")
    assert gcode.inspect_support_contacts(text, [region], .4, .2, .2)["regions"][0]["status"] == "fail"
    region["role"] = "forbidden"
    assert gcode.inspect_support_contacts(text, [region], .4, .2, .2)["regions"][0]["status"] == "pass"
    # The line radius extends outside its centreline; this still touches.
    text = text.replace("Y-2", "Y-0.1")
    assert gcode.inspect_support_contacts(text, [region], .4, .2, .2)["regions"][0]["status"] == "fail"
    result = gcode.inspect_support_contacts("G92 X0\n" + text, [region], .4, .2, .2)
    assert result["regions"][0]["status"] == "unknown"


def test_explicit_support_prohibition_cannot_be_silently_omitted():
    from ste_planner import assistant
    quote = "Передняя сторона видимая, без следов поддержек."
    intent = {"purpose": "Консоль", "regions": [{"surface_id": "front", "role": "visible", "quote": quote}],
              "loads": [], "missing_inputs": []}
    assert assistant.validate_intent(intent, quote, ["front"]).status == "invalid_input"


def test_invalid_mass_only_intent_still_requests_physical_load_without_accepting_roles():
    from ste_planner import assistant
    import json
    original = assistant.QwenClient
    class InvalidRoleClient(object):
        def __init__(self, *args, **kwargs):
            pass
        def complete(self, *args, **kwargs):
            proposal = {"purpose": "Консоль", "regions": [{"surface_id": "tip", "role": "visible", "quote": "груз массой 2 кг"}],
                        "loads": [], "missing_inputs": ["load_magnitude_N", "load_direction_axis"]}
            return {"tool_calls": [{"id": "fixture", "function": {"name": "describe_functional_intent", "arguments": json.dumps(proposal)}}]}
    assistant.QwenClient = InvalidRoleClient
    try:
        result = assistant.extract_intent("Консоль, груз массой 2 кг", [{"id": "tip"}],
                    config={"LLM_BASE_URL": "https://api.timeweb.ai/v1", "LLM_MODEL": "test", "LLM_API_KEY": "test"})
    finally:
        assistant.QwenClient = original
    assert result.status == "needs_input" and result.value is None
    assert any(t.get("missing") == ["load_magnitude_N", "load_direction_axis"] for t in result.trace)
