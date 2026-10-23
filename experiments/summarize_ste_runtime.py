"""Check native artifacts and publish bounded metrics; fail on incomplete evidence."""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from ste_planner import gcode, geometry


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    base = ROOT / "experiments/ste_runtime/final"
    guards = base / "failures"
    cases = {}
    for name in ("baseline", "live_demo", "enforcer", "blocker", "batch"):
        directory = base / name
        applied, sliced = read(directory / "002_apply.json"), read(directory / "004_slice.json")
        assert applied["status"] in ("applied", "applied_demo"), applied
        input_decision = read(base / "inputs" / (name + ".json"))
        assert applied["decision_sha256"] == hashlib.sha256(json.dumps(input_decision, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
        assert sliced["status"] == "slice_completed", sliced
        assert sliced["backend"] == "CuraEngineBackend"
        assert applied["qt_thread"]
        for operation in input_decision["scene_operations"]:
            if operation["op"] == "add_modifier_mesh":
                matching = [n for n in applied["nodes"] if n["name"].startswith(operation["role"])]
                assert matching
                assert all(all(n["modifier"][k] == v for k, v in operation["settings"].items()) for n in matching)
        for report in (applied["readback"], sliced["readback"], read(directory / "007_readback.json")):
            assert report["status"] == "pass" and not report["differences"], report
        assert not any(c["status"] == "fail" for c in sliced["inspection"]["checks"])
        assert read(directory / "005_undo.json")["nodes"] == []
        # Undo/Redo must restore all model and modifier geometry, not just count.
        assert applied["nodes"] == read(directory / "006_redo.json")["nodes"]
        for item, suffix in (("gcode", ".gcode"), ("project", ".3mf")):
            assert hashlib.sha256((directory / ("part" + suffix)).read_bytes()).hexdigest() == sliced[item]["sha256"]
        with zipfile.ZipFile(directory / "part.3mf") as archive:
            assert archive.testzip() is None
            assert any(".model" in p for p in archive.namelist())
        unresolved = [c["check"] for c in sliced["inspection"]["checks"] if c["status"] == "unknown" and c["check"] != "volumetric_flow"]
        cases[name] = {"status": "slice_completed_needs_review" if unresolved else "slice_verified_unqualified", "layers": sliced["inspection"]["summary"]["layers"],
            "readback_differences": 0, "native_time_estimate_s": float(sliced["inspection"]["summary"]["time_header"]),
            "native_filament_estimate": sliced["inspection"]["summary"]["filament_used_header"],
            "positive_extrusion_volume_mm3": sliced["inspection"]["summary"]["filament_volume_mm3"],
            "volume_note": "sum of positive E including priming/unretraction; use native filament header for cost estimates",
            "gcode": sliced["gcode"], "project": sliced["project"],
            "unknown_checks": [c["check"] for c in sliced["inspection"]["checks"] if c["status"] == "unknown"],
            "undo_redo_geometry": "pass"}

    shelf = geometry.load_stl(str(base / "inputs/shelf.stl"))
    transform = read(base / "inputs/blocker.json")["scene_operations"][1]
    placed = shelf.triangles + np.asarray(transform["translation"])
    rb = read(base / "blocker/003_readback.json")["actual"]
    origin = np.array([0 if rb["machine_center_is_zero"] else rb["machine_width"] / 2,
                       0 if rb["machine_center_is_zero"] else rb["machine_depth"] / 2, 0])
    placed += origin
    for name in ("enforcer", "blocker"):
        regions = [{"id": "left_underside", "role": "required", "triangles_mm": placed[[14, 15]].tolist()},
                   {"id": "right_underside", "role": "forbidden" if name == "blocker" else "required",
                    "triangles_mm": placed[[16, 17]].tolist()}]
        contacts = gcode.inspect_support_contacts((base / name / "part.gcode").read_text(encoding="utf-8"), regions,
                    rb["support_line_width"], rb["support_top_distance"], rb["layer_height"])
        cases[name]["contact_check"] = contacts
        assert all(r["status"] == "pass" for r in contacts["regions"]), contacts
    original = read(base / "blocker/002_apply.json")["nodes"]
    moved = read(base / "blocker/008_move_test.json")["nodes"]
    assert len(moved) == len(original) == 3
    for old, new in zip(original, moved):
        delta = np.asarray(new["matrix"])[:3, 3] - np.asarray(old["matrix"])[:3, 3]
        assert np.allclose(delta, [5, 0, -3])
    assert read(base / "blocker/009_undo.json")["nodes"] == original
    duplicated = read(base / "blocker/010_duplicate_test.json")["nodes"]
    assert len(duplicated) == 6
    assert read(base / "blocker/011_undo.json")["nodes"] == original
    cases["blocker"]["parent_move_duplicate_and_undo"] = "pass"
    batch = read(base / "batch/002_apply.json")["nodes"]
    assert len(batch) == 6
    assert sum(n["parent"] == "Root" for n in batch) == 2
    cases["batch"]["copies_and_parented_modifiers"] = "2 models, 2 enforcers, 2 blockers"
    batch_regions = []
    for index, node in enumerate(n for n in batch if n["parent"] == "Root"):
        matrix = np.asarray(node["matrix"])
        printer_shift = np.array([matrix[0, 3], -matrix[2, 3], matrix[1, 3]])
        delta = printer_shift - np.asarray(transform["translation"])
        for role, ids in (("required", [14, 15]), ("forbidden", [16, 17])):
            batch_regions.append({"id": "copy_%d_%s" % (index + 1, role), "role": role,
                                  "triangles_mm": (placed[ids] + delta).tolist()})
    contacts = gcode.inspect_support_contacts((base / "batch/part.gcode").read_text(encoding="utf-8"), batch_regions,
                rb["support_line_width"], rb["support_top_distance"], rb["layer_height"])
    assert all(r["status"] == "pass" for r in contacts["regions"]), contacts
    cases["batch"]["contact_check_all_copies"] = contacts

    before = read(guards / "001_state.json")
    refusals = []
    for index, reason in ((2, "SHA-256"), (4, "readback"), (6, "5D"), (8, "5D")):
        refused = read(guards / ("%03d_apply.json" % index))
        assert refused["status"] == "error" and reason in refused["message"], refused
        assert read(guards / ("%03d_state.json" % (index + 1))) == before
        refusals.append({"scenario": reason, "status": "refused_and_exact_state_preserved"})
    assert read(guards / "012_apply.json")["status"] == "error"
    assert read(guards / "011_state.json") == read(guards / "013_state.json")
    refusals.append({"scenario": "existing_project", "status": "refused_and_exact_state_preserved"})

    decision = read(base / "inputs/live_demo.json")
    value = decision["functional_analysis"]["value"]
    feasible = [c for c in value["candidates"] if c.get("mechanics") and not c["rejected_reasons"]]
    selected = value["selected"]
    oracle = sorted(feasible, key=lambda c: (tuple(c["objective"]), c["id"]))[0]
    assert selected["id"] == oracle["id"]
    assert selected["source"] == "software_fixture" and value["applied"] is False
    old, new = value["baseline"]["mechanics"]["cases"][0], selected["mechanics"]["cases"][0]
    report = {"status": "pass", "native_slicing_successes": len(cases), "native_slicing_scenarios": len(cases),
        "runtime": read(base / "baseline/001_inspect.json"), "cases": cases, "refusal_tests": refusals,
        "functional_live_path": {"llm_intake_status": decision["llm_intake"]["status"],
            "source": selected["source"], "core_applied": value["applied"], "demo_profile_isolation": True,
            "candidate_count": value["candidate_count"], "feasible_count": len(feasible),
            "finite_candidate_oracle_match": True, "selected_objective": selected["objective"],
            "objective_order": ["visible geometric staircase", "support volume proxy", "compliance", "height"],
            "global_continuous_optimum": None, "physical_optimum": None,
            "baseline_mechanics": old, "selected_mechanics": new, "mesh_convergence": value["mesh_convergence"]},
        "cost_tradeoff": {"baseline_native_time_s": cases["baseline"]["native_time_estimate_s"],
            "selected_native_time_s": cases["live_demo"]["native_time_estimate_s"],
            "time_change_percent": 100 * (cases["live_demo"]["native_time_estimate_s"] / cases["baseline"]["native_time_estimate_s"] - 1),
            "physical_print_time": None, "note": "Current objective prioritizes surface/mechanics; print time and material are reported, not optimized"},
        "physical_metrics": {"Ra_um": None, "flatness_mm": None, "breaking_load_N": None,
            "measured_stiffness_N_per_mm": None, "calibrated_Q_limit_mm3_s": None},
        "limitations": ["finite candidate optimality only under software constitutive assumptions",
            "linear elastic simulation does not qualify a printed part", "support checker covers controlled horizontal patches only",
            "native headless Qt/engine validation, not a manual GUI acceptance test",
            "Spiral/Full are refused without real kinematics, head/mandrel and full trajectory checks"]}
    (ROOT / "experiments/ste_runtime_results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "pass", "native_slices": len(cases), "refusals": len(refusals),
                      "contacts": {n: [{k: r[k] for k in ("id", "status", "contact_segments")}
                                       for r in cases[n]["contact_check"]["regions"]] for n in ("enforcer", "blocker")},
                      "cost_tradeoff": report["cost_tradeoff"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
