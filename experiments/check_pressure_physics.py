"""Read-only audit of original 5D native slices and pressure-field exact solutions.

Reuses saved STE exports, never retrains ML or invents physical measurements.
Outputs a NEW report; previous native evidence is not overwritten.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugin"), str(ROOT / "plugin/tests")]
from ste_planner import elasticity as E, toolpath_physics as P
from ste_planner.sliced_search import decision_digest
from functional_fixtures import structured_bar, axial_inputs


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_checks():
    records = []
    for order in (1, 2):
        mesh = structured_bar(order=order)
        elastic, strength, normals, _, fixed = axial_inputs(mesh)
        loads = [{"id": "compression", "on": {"axis": 0, "value_mm": 10.}, "pressure_MPa": 10.}]
        solved = E.solve(mesh, elastic, strength, normals, loads, fixed)
        if not solved.ok:
            raise AssertionError(solved.to_dict())
        case = solved.value["cases"][0]
        maps = case["maps"]
        stress_error = float(np.max(np.abs(np.asarray(maps["element_stress_tensor_MPa"]) - np.diag([-10., 0, 0]))))
        p_error = float(np.max(np.abs(np.asarray(maps["element_pressure_MPa"]) - 10./3.)))
        boundary_error = float(np.max(np.abs(np.asarray(maps["boundary_pressure_MPa"]) -
                                           10 * np.asarray(solved.value["boundary_normals"])[:, 0] ** 2)))
        record = {"order": order, "elements": len(mesh["elements"]),
                  "exact_stress_xx_MPa": -10., "exact_bulk_pressure_MPa": 10./3.,
                  "exact_loaded_face_pressure_MPa": 10., "exact_max_displacement_mm": .05,
                  "stress_max_absolute_error_MPa": stress_error, "bulk_pressure_max_absolute_error_MPa": p_error,
                  "surface_pressure_max_absolute_error_MPa": boundary_error,
                  "displacement_absolute_error_mm": abs(case["max_displacement_mm"] - .05),
                  "equilibrium_relative_residual": case["equilibrium_relative_residual"],
                  "energy_relative_error": case["energy_relative_error"],
                  "reaction_force_absolute_error_N": float(np.max(np.abs(np.asarray(case["reaction_N"]) - [40., 0, 0])))}
        record["status"] = "pass" if max(stress_error, p_error, boundary_error) < 1e-8 and record["displacement_absolute_error_mm"] < 1e-10 else "fail"
        records.append(record)
    return records


def finalize_report(report):
    """Recheck artifact bindings and numerical invariants before publication.

    May also finalize an in-memory report produced earlier by the SAME toolpath
    implementation. Native summaries have no FE/material fields; references are
    solved afresh with the current elasticity implementation.
    """
    physics_path = "plugin/ste_planner/toolpath_physics.py"
    if report["code_sha256"][physics_path] != digest(ROOT / physics_path):
        raise ValueError("Toolpath implementation changed: recompute native summaries")
    folder = ROOT / "experiments/real_toolpath_physics"
    manifest = {item["id"]: item for item in read(folder / "manifest.json")}
    for record in report["records"]:
        case = folder / record["id"]
        item = manifest[record["id"]]
        decision, applied, sliced = [read(case / name) for name in ("decision.json", "002_apply.json", "004_slice.json")]
        raw_hash = hashlib.sha256(gzip.decompress((case / "part.gcode.gz").read_bytes())).hexdigest()
        record["source_binding"] = digest(ROOT / item["stl"]) == item["stl_sha256"] == record["source_stl_sha256"] == decision["calibration_context"]["geometry_sha256"]
        record["decision_binding"] = decision_digest(decision) == applied["decision_sha256"] == record["evaluated_decision_sha256"]
        record["gcode_binding"] = (raw_hash == sliced["gcode"]["sha256"] and
                                   digest(case / "part.gcode.gz") == record["gcode_archive_sha256"])
        record["native_project_binding"] = digest(case / "part.3mf") == sliced["project"]["sha256"] == record["native_project_sha256"]
        record["previous_conditional_status"] = read(case / "physics.json").get("conditional_nominal_pose", {}).get("status")
        value = record["conditional_nominal_pose"].get("value")
        if value:
            if value.get("conditional_coarse_FE") or value.get("material_field"):
                raise ValueError("Native-summary audit must not reuse FE/material fields")
            record["gcode_binding"] &= raw_hash == value["gcode_bytes_sha256"]
            summary = value["deposition"]
            tangent, normal = [np.asarray(summary[key], float) for key in ("tangent_structure_tensor", "normal_structure_tensor")]
            traces = [float(np.trace(t)) for t in (tangent, normal)]
            minima = [float(np.linalg.eigvalsh(t).min()) for t in (tangent, normal)]
            max_pair = float(np.linalg.eigvalsh(tangent + normal).max())
            # Every road has orthonormal t,n: trace(A2)=trace(N2)=1,
            # A2,N2 >= 0 and A2+N2 <= I; volume-weighted means preserve these.
            passed = (np.allclose(traces, 1., atol=1e-7) and min(minima) >= -1e-7 and
                      max_pair <= 1.+1e-7 and summary["part_volume_mm3"] > 0 and summary["part_path_length_mm"] > 0)
            record["kinematic_summary_checks"] = {"status": "pass" if passed else "fail",
                "tangent_and_normal_traces": traces, "minimum_eigenvalues": minima,
                "maximum_eigenvalue_tangent_plus_normal": max_pair,
                "meaning": "necessary numerical consistency; not measured kinematic accuracy"}
        if item["mode"] == "cylindrical_full":
            resets = Counter()
            with gzip.open(str(case / "part.gcode.gz"), "rt", encoding="utf-8") as handle:
                for line in handle:
                    command = line.partition(";")[0].strip()
                    if command.upper().startswith("G92 "):
                        resets["".join(sorted(field[0].upper() for field in command.split()[1:]))] += 1
            record["g92_reset_axes_counts"] = dict(resets)
    report["analytical_pressure_checks"] = exact_checks()
    report["code_sha256"] = {path: digest(ROOT / path) for path in report["code_sha256"]}
    report["software_audit_status"] = "pass" if (all(r["status"] == "pass" for r in report["analytical_pressure_checks"]) and report["records"] and
        all(r["source_binding"] and r["decision_binding"] and r["gcode_binding"] and r["native_project_binding"] and
            r["native_readback"] == "pass" and r["strict"]["status"] == "needs_input" and
            r["conditional_nominal_pose"]["status"] in ("needs_input", "needs_calibration") and
            r.get("kinematic_summary_checks", {}).get("status") != "fail" for r in report["records"])) else "fail"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--case", action="append", help="specific manifest id; default: every saved 5D candidate")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new report path")
    output.parent.mkdir(parents=True, exist_ok=True)
    folder = ROOT / "experiments/real_toolpath_physics"
    records = []
    for item in read(folder / "manifest.json"):
        if item.get("mode", "classic") == "classic" or (args.case and item["id"] not in args.case):
            continue
        case = folder / item["id"]
        if not (case / "004_slice.json").is_file():
            continue
        print("Auditing saved STE slice: " + item["id"], flush=True)
        decision, applied, sliced = [read(case / p) for p in ("decision.json", "002_apply.json", "004_slice.json")]
        diameter = float(sliced["physics_context"]["filament_diameter_mm"])
        pose = {"type": "table_AC", "angle_unit": "degree", "source": "analysis_assumption",
                "rotary_g92": "preserve_physical_angle_global", "interpolation": "linear_unwrapped_joints",
                "table_pivot_mm": [0., 0., 0.], "nozzle_axis_machine": [0., 0., 1.],
                "work_offsets": {k: np.eye(4).tolist() for k in ("G54", "G55", "G56")}}
        path = case / "part.gcode.gz"
        strict = P.analyze_slice(str(path), decision, applied, sliced, diameter)
        conditional = P.analyze_slice(str(path), decision, applied, sliced, diameter, pose_contract=pose)
        gcode_binding = hashlib.sha256(gzip.decompress(path.read_bytes())).hexdigest() == sliced["gcode"]["sha256"]
        record = {"id": item["id"], "mode": item["mode"], "source_stl_sha256": digest(ROOT / item["stl"]),
                  "gcode_archive_sha256": digest(path), "native_project_sha256": digest(case / "part.3mf"),
                  "source_binding": digest(ROOT / item["stl"]) == item["stl_sha256"] == decision["calibration_context"]["geometry_sha256"],
                  "decision_binding": decision_digest(decision) == applied["decision_sha256"],
                  "evaluated_decision_sha256": decision_digest(decision),
                  "gcode_binding": gcode_binding,
                  "native_project_binding": digest(case / "part.3mf") == sliced["project"]["sha256"],
                  "native_readback": sliced["readback"]["status"],
                  "native_failed_checks": [c["check"] for c in sliced["inspection"]["checks"] if c["status"] == "fail"],
                  "native_unknown_checks": [c["check"] for c in sliced["inspection"]["checks"] if c["status"] == "unknown"],
                  "strict": strict.to_dict(), "conditional_nominal_pose": conditional.to_dict(),
                  "physical_print_approval": False}
        # Preserve diagnostics for known coarse-grid limitations, not a new FE result.
        old = read(case / "physics.json")
        record["previous_conditional_status"] = old.get("conditional_nominal_pose", {}).get("status")
        if item["mode"] == "cylindrical_full":
            resets = Counter()
            with gzip.open(str(path), "rt", encoding="utf-8") as handle:
                for line in handle:
                    command = line.partition(";")[0].strip()
                    if command.upper().startswith("G92 "):
                        resets["".join(sorted(field[0].upper() for field in command.split()[1:]))] += 1
            record["g92_reset_axes_counts"] = dict(resets)
        field = (old.get("conditional_nominal_pose", {}).get("value") or {}).get("material_field")
        if field:
            record["previous_material_field_diagnostic"] = field["summary"]
            record["previous_material_field_recomputed"] = False
        records.append(record)
        print("  strict=%s; nominal=%s" % (strict.status, conditional.status), flush=True)
    checks = exact_checks()
    report = {"scope": "numerical verification + immutable saved native 5D slices; no physical qualification",
              "code_sha256": {p: digest(ROOT / p) for p in ("plugin/ste_planner/elasticity.py", "plugin/ste_planner/toolpath_physics.py")},
              "analytical_pressure_checks": checks, "native_5d_candidates": len(records), "records": records,
              "data_leakage": {"model_fitted": False, "constants_fitted_to_outputs": False,
                               "organizer_parts_are_blind_holdout": False, "physical_labels_available": False},
              "pressure_capability": {"bulk_pressure_and_surface_normal_stress": True,
                                      "contact_pressure": False, "melt_pressure": False,
                                      "automatically_executed_from_chat": False},
              "physical_print_approval": False, "physical_5d_stress_prediction_validated": False}
    finalize_report(report)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"software_audit_status": report["software_audit_status"],
                      "native_5d_candidates": len(records), "physical_5d_stress_prediction_validated": False}))
    if report["software_audit_status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
