"""Modern Python process boundary. Calls the canonical planner without changing it.

Requests contain manufacturing inputs only; credentials stay in the chat process.
This file is also runnable directly from an installed plugin bundle.
"""
import argparse
import hashlib
import json
import os
import sys


def core_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (os.path.join(root, "core"), os.path.join(os.path.dirname(root), "plugin")):
        if os.path.isfile(os.path.join(candidate, "ste_planner", "planner.py")):
            return candidate
    raise RuntimeError("Canonical ste_planner package missing; build the plugin bundle")


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def calculate(request):
    sys.path.insert(0, core_path())
    from ste_planner.planner import plan_case
    path = request["mesh_path"]
    if digest(path) != request["geometry_sha256"]:
        raise ValueError("Source STL changed before planning")
    decision = plan_case(request["task_text"], path, resources_dir=request["resources_dir"],
                         machine_id=request["machine_id"], forced_mode=request.get("mode"),
                         llm_items=request.get("llm_items"), source="AI Assistant",
                         preset_catalog=request.get("preset_catalog"),
                         allowed_quality_changes=request.get("allowed_quality_changes"),
                         preferred_materials=request.get("preferred_materials"),
                         orientation=request.get("orientation"),
                         **request.get("planner_options", {}))
    if digest(path) != request["geometry_sha256"]:
        raise ValueError("Source STL changed during planning")
    return {"request_id": request["request_id"], "decision": decision}


def calculate_trajectory(request):
    """Validate the exact native G-code without changing the scene or planning settings."""
    sys.path.insert(0, core_path())
    from ste_planner.trajectory_validation import validate_trajectory
    text = request["gcode_text"]
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if checksum != request.get("gcode_sha256"):
        raise ValueError("Trajectory G-code hash mismatch")
    result = validate_trajectory(text, machine_contract=request.get("machine_contract"),
                                 pose_contract=request.get("pose_contract"),
                                 collision_geometry=request.get("collision_geometry"))
    result["gcode_sha256"] = checksum
    result["printing_mode"] = request.get("printing_mode")
    result["profile_observations"] = request.get("profile_observations") or {}
    decision = request.get("deposition_decision")
    if decision and request.get("pose_contract") and request.get("filament_diameter_mm"):
        from ste_planner.toolpath_physics import deposition
        roads = deposition(text, decision, request["filament_diameter_mm"],
                           pose_contract=request["pose_contract"], retain_roads=False)
        result["existing_deposition"] = roads.to_dict()
    else:
        result["existing_deposition"] = {"status": "needs_input",
            "missing": ["matched applied plan, explicit pose contract and filament diameter"]}
    result["physical_print_approval"] = False
    return {"request_id": request["request_id"], "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    requests = parser.add_mutually_exclusive_group(required=True)
    requests.add_argument("--request")
    requests.add_argument("--trajectory-request")
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    try:
        with open(args.request or args.trajectory_request, encoding="utf-8") as handle:
            request = json.load(handle)
        result = calculate(request) if args.request else calculate_trajectory(request)
    except Exception as exc:
        result = {"error": "%s: %s" % (type(exc).__name__, exc)}
    temporary = args.response + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temporary, args.response)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
