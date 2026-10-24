"""Actual organizer STL/TZ -> every orientation -> installed STE -> physics input.

No material/fixture values are manufactured. Native 5D generation is offline
analysis only; interpreting rotary coordinates still requires a kinematic map.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugin"), str(ROOT / "plugin/tests")]
from ste_planner import geometry, planner
from ste_planner.requirements import read_docx_text
from ste_planner.result import to_json
from testutil import example_cases
from check_ste_runtime import compile_values


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(value, indent=2), encoding="utf-8")


def prepare(output, case_filter=None, mode_filter=None):
    jobs = []
    manifest = []
    for index, (name, docx, source) in enumerate(example_cases(), 1):
        if case_filter and index != case_filter:
            continue
        text = read_docx_text(docx)
        modes = ["classic"] + (["cylindrical", "cylindrical_full"] if "5D" in name else [])
        if mode_filter:
            modes = [mode_filter]
        for mode in modes:
            try:
                base = planner.plan_case(text, source, forced_mode=mode)
            except (ValueError, KeyError) as exc:
                manifest.append({"id": "case_%02d_%s" % (index, mode), "organizer_case": name,
                    "status": "route_refused_before_slicing", "reason": str(exc), "physical_print_approval": False})
                continue
            selected_route = next((r for r in base["candidates"]["routes"] if r["mode"] == mode), None)
            if mode != "classic" and not (selected_route and selected_route["feasible"]):
                manifest.append({"id": "case_%02d_%s" % (index, mode), "organizer_case": name,
                    "status": "route_infeasible_not_sliced", "reason": "no admissible core/tooling route",
                    "physical_print_approval": False})
                continue
            mesh = geometry.load_stl(source)
            for orientation in (0, 90):
                decision = copy.deepcopy(base)
                angle = np.deg2rad(orientation)
                yaw = (np.array([[1, 0, 0], [0, np.cos(angle), -np.sin(angle)], [0, np.sin(angle), np.cos(angle)]])
                       if mode == "classic" else np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]))
                transform = decision["scene_operations"][1]
                rotation = yaw.dot(np.asarray(transform["rotation"]))
                if mode == "classic":
                    _, shift = geometry.place_on_bed(mesh, rotation)
                else:
                    # A phase about the same existing tooling axis, never a new
                    # arbitrary fixture axis invented for a better score.
                    shift = yaw.dot(np.asarray(transform["translation"]))
                transform.update(rotation=rotation.tolist(), translation=shift.tolist())
                decision["scene_operations"] = [op for op in decision["scene_operations"] if op["op"] != "multiply"]
                if mode == "classic":
                    detail = geometry.evaluate_orientation(mesh, rotation[2], rotation=rotation,
                        layer_height=decision["effective_settings"]["layer_height"], build_volume=[351, 300.3, 300])
                    compile_values(decision, Path(planner.default_resources_dir()),
                                   {"support_enable": bool(detail["overhang_area"] > 0)})
                    decision["candidate_geometry"] = detail
                decision["calibration_context"]["rotation"] = rotation.tolist()
                folder = output / ("case_%02d_%s_%03d" % (index, mode, orientation))
                folder.mkdir(parents=True, exist_ok=True)
                write(folder / "decision.json", decision)
                job = {"commands": [{"command": "inspect"}, {"command": "apply", "analysis_only": True,
                        "decision": str(folder / "decision.json"), "mesh": source}, {"command": "readback"},
                        {"command": "slice", "output_prefix": str(folder / "part"), "timeout_seconds": 360},
                        {"command": "quit"}]}
                write(folder / "job.json", job)
                manifest.append({"id": folder.name, "organizer_case": name, "mode": mode,
                    "orientation_phase_deg": orientation, "stl": str(Path(source).relative_to(ROOT)),
                    "orientation_parameter": "build tilt about X" if mode == "classic" else "phase about retained tooling axis",
                    "specimens_per_slice": 1,
                    "stl_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                    "tz": str(Path(docx).relative_to(ROOT)), "planner_status": decision["status"],
                    "analysis_only": True, "physical_print_approval": False})
                jobs.append(folder / "job.json")
    previous_path = output / "manifest.json"
    if previous_path.exists() and (case_filter or mode_filter):
        replacing = set(item["id"] for item in manifest)
        manifest = [item for item in json.loads(previous_path.read_text(encoding="utf-8")) if item["id"] not in replacing] + manifest
        manifest.sort(key=lambda item: item["id"])
    write(previous_path, manifest)
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=int)
    parser.add_argument("--mode", choices=("classic", "cylindrical", "cylindrical_full"))
    parser.add_argument("--phase", type=int, choices=(0, 90))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--retry-failed", action="store_true", help="keep successes; retain first failed native response before retry")
    parser.add_argument("--output", type=Path, default=ROOT / "experiments/real_toolpath_physics")
    args = parser.parse_args()
    jobs = prepare(args.output.resolve(), args.case, args.mode)
    if not args.prepare_only:
        for job in jobs:
            if args.mode and ("_%s_" % args.mode) not in job.parent.name:
                continue
            if args.phase is not None and not job.parent.name.endswith("_%03d" % args.phase):
                continue
            previous = job.parent / "004_slice.json"
            if args.retry_failed and previous.exists():
                if json.loads(previous.read_text(encoding="utf-8")).get("status") == "slice_completed":
                    continue
                saved = job.parent / "earlier_attempt_slice.json"
                if not saved.exists():
                    saved.write_bytes(previous.read_bytes())
            print("Native real-case slice: " + job.parent.name, flush=True)
            subprocess.run([sys.executable, str(ROOT / "plugin/tools/launch_ste_runtime.py"), "--job", str(job)], check=True, timeout=420)


if __name__ == "__main__":
    main()
