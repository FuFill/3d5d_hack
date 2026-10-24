"""Recompute actual organizer-part toolpath metrics; no training or fabricated properties."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from ste_planner import geometry, materials, toolpath_physics as physics, volume_mesh
from ste_planner.result import to_json


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def nominal_pose():
    return {"type": "table_AC", "angle_unit": "degree", "source": "analysis_assumption",
            "rotary_g92": "preserve_physical_angle_global",
            "interpolation": "linear_unwrapped_joints", "table_pivot_mm": [0, 0, 0],
            "nozzle_axis_machine": [0, 0, 1],
            "work_offsets": {k: np.eye(4).tolist() for k in ("G54", "G55", "G56")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=int)
    parser.add_argument("--mode")
    parser.add_argument("--phase", type=int)
    parser.add_argument("--field-case", type=int)
    parser.add_argument("--background-field", action="store_true", help="explicit deposition integration grid, not STL solid repair")
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    folder = ROOT / "experiments/real_toolpath_physics"
    catalog = materials.load_catalog()
    for item in read(folder / "manifest.json"):
        if args.case and not item["id"].startswith("case_%02d_" % args.case):
            continue
        if args.mode and item.get("mode") != args.mode:
            continue
        if args.phase is not None and item.get("orientation_phase_deg") != args.phase:
            continue
        case = folder / item["id"]
        if not (case / "004_slice.json").exists():
            continue
        if args.skip_existing and (case / "physics.json").exists():
            continue
        print("Actual toolpath physics: " + item["id"], flush=True)
        decision = read(case / "decision.json")
        sliced, applied = read(case / "004_slice.json"), read(case / "002_apply.json")
        if sliced.get("status") != "slice_completed":
            result = {"id": item["id"], "input": item, "native_slice": sliced,
                      "strict": {"status": "needs_slice_verification", "value": None}, "physical_print_approval": False}
            (case / "physics.json").write_text(to_json(result, indent=2), encoding="utf-8")
            continue
        native_diameter = sliced.get("physics_context", {}).get("filament_diameter_mm")
        diameter = float(native_diameter if native_diameter is not None else catalog.get(decision["calibration_context"]["material_id"]).slicer["diameter"])
        path = case / "part.gcode"
        if not path.exists():
            path = case / "part.gcode.gz"
        domain = None
        mesh_diagnostic = None
        if args.field_case and item["id"].startswith("case_%02d_" % args.field_case):
            if args.background_field:
                mesh = geometry.load_stl(str(ROOT / item["stl"]))
                bounds = [mesh.vertices.min(axis=0)-.5, mesh.vertices.max(axis=0)+.5]
                meshed = volume_mesh.deposition_background(bounds, 8., item["stl_sha256"])
            else:
                meshed = volume_mesh.tetrahedralize(str(ROOT / item["stl"]), 8., order=1)
            mesh_diagnostic = {"status": meshed.status, "trace": meshed.trace,
                               "domain_role": meshed.value.get("domain_role", "STL solid") if meshed.ok else None,
                               "element_count": len(meshed.value["elements"]) if meshed.ok else None}
            if meshed.ok:
                domain = meshed.value
        strict = physics.analyze_slice(str(path), decision, applied, sliced, diameter, volume_mesh=domain)
        result = {"id": item["id"], "input": item, "filament_diameter_mm": diameter,
                  "diameter_source": "native effective stack" if native_diameter is not None else "nominal selected STE material profile, not measured filament",
                  "strict": strict.to_dict(), "physical_print_approval": False}
        if mesh_diagnostic is not None:
            result["meshing"] = mesh_diagnostic
        if item["mode"] != "classic":
            assumed = physics.analyze_slice(str(path), decision, applied, sliced, diameter,
                pose_contract=nominal_pose(), volume_mesh=domain)
            result["nominal_pose_assumption"] = nominal_pose()
            result["conditional_nominal_pose"] = assumed.to_dict()
        # Large symbolic fields are reproducible from the original source and
        # G-code; publish diagnostics, never silently discard their limitations.
        for key in ("strict", "conditional_nominal_pose"):
            value = result.get(key, {}).get("value")
            if value and value.get("material_field"):
                value["material_field"] = {"summary": value["material_field"]["summary"],
                    "operator_coefficients": value["material_field"]["operator_coefficients"],
                    "coarse_element_count": len(domain["elements"]),
                    "constitutive_operator": "recompute with --field-case; no modulus fitted"}
        if path.exists() and path.suffix != ".gz":
            raw = path.read_bytes()
            with (case / "part.gcode.gz").open("wb") as target:
                with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as archive:
                    archive.write(raw)
            result["archive"] = {"path": "part.gcode.gz", "uncompressed_sha256": hashlib.sha256(raw).hexdigest(),
                                 "uncompressed_bytes": len(raw), "sha256": hashlib.sha256((case / "part.gcode.gz").read_bytes()).hexdigest()}
        (case / "physics.json").write_text(to_json(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
