"""Analyze a saved native STE candidate and matching readback/artifacts.

python plugin/tools/analyze_sliced_physics.py case_folder --diameter 1.75
    [--inputs measured_pose_mesh_loads.json] --output physics.json
Optional inputs: pose_contract, volume_mesh, measured_material, load_cases, fixed, service_conditions.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ste_planner.toolpath_physics import analyze_slice
from ste_planner.result import to_json


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    parser.add_argument("--diameter", type=float, required=True)
    parser.add_argument("--inputs")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    inputs = read(args.inputs) if args.inputs else {}
    paths = lambda name: os.path.join(args.folder, name)
    gcode = paths("part.gcode") if os.path.exists(paths("part.gcode")) else paths("part.gcode.gz")
    result = analyze_slice(gcode, read(paths("decision.json")),
        read(paths("002_apply.json")), read(paths("004_slice.json")), args.diameter, **inputs)
    with io.open(args.output, "w", encoding="utf-8") as handle:
        handle.write(to_json(result.to_dict(), indent=2))
    print(result.status)


if __name__ == "__main__":
    main()
