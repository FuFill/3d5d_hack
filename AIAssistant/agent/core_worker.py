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
                         **request.get("planner_options", {}))
    if digest(path) != request["geometry_sha256"]:
        raise ValueError("Source STL changed during planning")
    return {"request_id": request["request_id"], "decision": decision}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args()
    try:
        with open(args.request, encoding="utf-8") as handle:
            result = calculate(json.load(handle))
    except Exception as exc:
        result = {"error": "%s: %s" % (type(exc).__name__, exc)}
    temporary = args.response + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temporary, args.response)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
