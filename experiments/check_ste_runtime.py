"""Reproduce isolated native STE tests; no modifications of the user's project.

Requires installed STE 7.10 and the functional worker environment. --prepare-only
creates decisions/jobs. Launches one fresh profile per scenario otherwise.
Mechanical fixture demonstrations remain explicitly unqualified.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugin"), str(ROOT / "plugin" / "tests")]
from ste_planner import compiler, functional, geometry, planner
from ste_planner.settings_schema import load_machine_schema, SettingResolver, material_settings_from_file
from ste_planner.result import to_json
from functional_fixtures import write_stl


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(data, indent=2) + "\n", encoding="utf-8")


def compile_values(decision, resources, values):
    schema = load_machine_schema(str(resources), decision["calibration_context"]["machine_id"])
    material = planner.materials.load_catalog().get(decision["chosen_material"]["id"])
    resolver = SettingResolver(schema, {"printing_mode": "classic"},
                               material_settings=material_settings_from_file(material.slicer))
    previous = decision["setting_patch"]["payload"]
    settings = dict(previous["global"])
    settings.update(previous["extruders"].get("0", {}))
    settings.update(values)
    patch = compiler.compile_patch([functional._native_decision(k, v) for k, v in sorted(settings.items())], resolver)
    if patch["errors"]:
        raise ValueError(patch["errors"])
    decision["setting_patch"] = {k: v for k, v in patch.items() if k != "resolver"}
    decision["setting_patch"]["payload"] = compiler.patch_to_slicer_payload(patch)
    decision["effective_settings"] = compiler.effective_settings(patch["resolver"], planner.READBACK_KEYS)
    return decision


def shelf_mesh():
    # Closed L shelf, with two separate underside patches split at X=20.
    polygon = [(0, 0), (4, 0), (4, 20), (20, 20), (30, 20), (30, 24), (0, 24)]
    vertices = [[x, y, z] for y in (0, 10) for x, z in polygon]
    front = [[0, 1, 2], [0, 2, 6], [2, 3, 6], [3, 5, 6], [3, 4, 5]]
    faces = list(front) + [[a + 7 for a in reversed(t)] for t in front]
    for i in range(7):
        j = (i + 1) % 7
        faces.extend([[i, i + 7, j + 7], [i, j + 7, j]])
    mesh = geometry.Mesh(vertices, faces)
    assert geometry.signed_volume(mesh) > 0
    return mesh


def prepare(output, resources, live_decision):
    inputs = output / "inputs"
    beam = ROOT / "experiments/functional_artifacts/beam.stl"
    baseline = planner.plan_case("Рабочая консоль", str(beam), resources_dir=str(resources), forced_mode="classic")
    write(inputs / "baseline.json", baseline)
    shelf = shelf_mesh()
    shelf_path = inputs / "shelf.stl"
    inputs.mkdir(parents=True, exist_ok=True)
    write_stl(shelf, str(shelf_path))
    overhang = planner.plan_case("Рабочая полка", str(shelf_path), resources_dir=str(resources), forced_mode="classic")
    _, shift = geometry.place_on_bed(shelf, np.eye(3))
    overhang["scene_operations"][1].update(rotation=np.eye(3).tolist(), translation=shift.tolist())
    overhang["calibration_context"]["rotation"] = np.eye(3).tolist()
    overhang["validation_demonstration"] = "controlled support adapter fixture; not a mechanically optimized orientation"
    compile_values(overhang, resources, {"support_enable": True, "support_type": "everywhere"})
    for name, required, blocked in (("enforcer", [14, 15, 16, 17], []), ("blocker", [14, 15], [16, 17])):
        case = copy.deepcopy(overhang)
        selected = {"mode": "classic", "rotation": np.eye(3).tolist(), "surface": {
            "support_plan": {"required_face_ids": required, "blocked_face_ids": blocked}}}
        effective = case["effective_settings"]
        selected["support_clearance_mm"] = effective["support_xy_distance"] + effective["support_line_width"] / 2 + functional.SUPPORT_NUMERICAL_CLEARANCE_MM
        selected["support_depth_mm"] = effective["support_top_distance"] + effective["layer_height"] + effective["support_line_width"] / 2
        case["scene_operations"].extend(functional.support_modifiers(shelf, selected))
        write(inputs / (name + ".json"), case)
    batch = copy.deepcopy(case)
    batch["scene_operations"] = [op for op in batch["scene_operations"] if op["op"] != "arrange"]
    batch["scene_operations"].append({"op": "multiply", "count": 2})
    write(inputs / "batch.json", batch)
    live = json.loads(live_decision.read_text(encoding="utf-8"))
    value = live["functional_analysis"]["value"]
    if live["llm_intake"]["status"] != "computed" or not value.get("selected"):
        raise ValueError("live intake did not yield a validated functional recommendation")
    selected = value["selected"]
    assert selected["source"] == "software_fixture" and not value["applied"]
    live["scene_operations"][0]["path"] = str(beam)
    live["scene_operations"][1].update(rotation=selected["rotation"], translation=selected["translation"])
    # Recompute placement-owned volumes with the current kernel (including
    # exclusion of redundant blocker prisms below build-plate contact faces).
    value["support_modifier_operations"] = functional.support_modifiers(geometry.load_stl(str(beam)), selected)
    live["scene_operations"].extend(value["support_modifier_operations"])
    live["calibration_context"]["rotation"] = selected["rotation"]
    process = json.loads((ROOT / "experiments/functional_artifacts/beam_spec.json").read_text(encoding="utf-8"))
    profile = next(p for p in process["profiles"] if p["id"] == selected["profile_id"])
    compile_values(live, resources, profile["settings"])
    live["validation_demonstration"] = "isolated software fixture, unqualified; source and applied=False preserved"
    write(inputs / "live_demo.json", live)
    bad = copy.deepcopy(baseline)
    bad["calibration_context"]["geometry_sha256"] = "0" * 64
    write(inputs / "bad_sha.json", bad)
    bad = copy.deepcopy(baseline)
    bad["effective_settings"]["wall_thickness"] += 123
    write(inputs / "bad_readback.json", bad)
    for mode in ("cylindrical", "cylindrical_full"):
        bad = copy.deepcopy(baseline)
        bad["route"]["mode"] = mode
        write(inputs / (mode + ".json"), bad)

    jobs = {}
    for name in ("baseline", "live_demo", "enforcer", "blocker", "batch"):
        folder = output / name
        folder.mkdir(exist_ok=True)
        mesh = shelf_path if name in ("enforcer", "blocker", "batch") else beam
        commands = [{"command": "inspect"}, {"command": "apply", "decision": str(inputs / (name + ".json")),
                    "mesh": str(mesh), "demo": name == "live_demo"}, {"command": "readback"},
                    {"command": "slice", "output_prefix": str(folder / "part")},
                    {"command": "undo"}, {"command": "redo"}, {"command": "readback"}]
        if name == "blocker":
            commands += [{"command": "move_test", "printer_delta_mm": [5, 3, 0]}, {"command": "undo"},
                         {"command": "duplicate_test", "printer_delta_mm": [45, 0, 0]}, {"command": "undo"}]
        commands.append({"command": "quit"})
        jobs[name] = folder / "job.json"
        write(jobs[name], {"commands": commands})
    folder = output / "failures"
    folder.mkdir(exist_ok=True)
    commands = [{"command": "state"}]
    for name in ("bad_sha", "bad_readback", "cylindrical", "cylindrical_full"):
        commands += [{"command": "apply", "decision": str(inputs / (name + ".json")), "mesh": str(beam), "demo": True},
                     {"command": "state"}]
    commands += [{"command": "load_stock", "mesh": str(beam)}, {"command": "state"},
                 {"command": "apply", "decision": str(inputs / "baseline.json"), "mesh": str(beam)},
                 {"command": "state"},
                 {"command": "quit"}]
    jobs["failures"] = folder / "job.json"
    write(jobs["failures"], {"commands": commands, "abort_on_error": False})
    return jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "experiments/ste_runtime/final")
    parser.add_argument("--resources", type=Path, default=Path(planner.default_resources_dir()))
    parser.add_argument("--live-decision", type=Path, default=ROOT / ".runtime/live_functional_decision.json")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--case", choices=("baseline", "live_demo", "enforcer", "blocker", "batch", "failures"))
    args = parser.parse_args()
    jobs = prepare(args.output.resolve(), args.resources, args.live_decision)
    if not args.prepare_only:
        for name, path in jobs.items():
            if args.case and name != args.case:
                continue
            print("Native STE scenario: " + name, flush=True)
            subprocess.run([sys.executable, str(ROOT / "plugin/tools/launch_ste_runtime.py"), "--job", str(path)], check=True, timeout=300)


if __name__ == "__main__":
    main()
