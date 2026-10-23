"""Reproducible kernel benchmark. Every constitutive value here is a software fixture.

Reports numerical mechanics/geometry, not measured printing quality or a generic
ML accuracy score. Uses actual STE definitions; carries source/version hashes.
"""

import ast
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "plugin"), str(ROOT / "plugin" / "tests")]

from ste_planner import functional, planner
from ste_planner.result import to_json
from functional_fixtures import planning_fixture
from testutil import resources_dir


def summarize(decision):
    value = decision["functional_analysis"]["value"]
    baseline, selected = value["baseline"], value["selected"]
    if not selected:
        raise AssertionError("no feasible selected candidate")
    old, new = baseline["mechanics"]["cases"][0], selected["mechanics"]["cases"][0]
    failed = functional._limits(selected, baseline, {"safety_factor": 1.5, "max_displacement_mm": 10}, True)
    if failed:
        raise AssertionError(failed)
    feasible = [c for c in value["candidates"] if c.get("mechanics") and not c["rejected_reasons"]]
    oracle = sorted(feasible, key=lambda c: (tuple(c["objective"]), c["id"]))[0]
    return {"status": decision["functional_analysis"]["status"], "source": "software_fixture",
            "candidate_count": value["candidate_count"], "feasible_count": value["feasible_count"],
            "selected_profile": selected["profile_id"], "selected_settings": value["proposed_patch"]["payload"],
            "baseline": {"mechanics": old, "surface": baseline["surface"]},
            "selected": {"mechanics": new, "surface": selected["surface"]},
            "mesh_convergence": value["mesh_convergence"],
            "quality": {"selected_hard_violations": len(failed), "matches_exhaustive_tested_candidate_oracle": oracle["id"] == selected["id"],
                        "slicer_setting_errors": len(value["proposed_patch"]["errors"]),
                        "software_fixture_applied": value["applied"]},
            "changes": {"stiffness_percent": 100 * (new["effective_stiffness_N_per_mm"] / old["effective_stiffness_N_per_mm"] - 1),
                        "compliance_percent": 100 * (new["compliance_Nmm"] / old["compliance_Nmm"] - 1),
                        "max_displacement_percent": 100 * (new["max_displacement_mm"] / old["max_displacement_mm"] - 1),
                        "visible_staircase_before_mm": baseline["surface"]["visible_staircase_mm"],
                        "visible_staircase_after_mm": selected["surface"]["visible_staircase_mm"]}}


def main():
    output = ROOT / "experiments" / "functional_artifacts"
    output.mkdir(exist_ok=True)
    shapes = {}
    for shape in ("beam", "wedge"):
        start = time.time()
        path, spec, _, surfaces = planning_fixture(shape)
        decision = planner.plan_case("Рабочая консоль", path, resources_dir=resources_dir(),
                                     forced_mode="classic", functional_spec=spec)
        shapes[shape] = summarize(decision)
        shapes[shape]["seconds_including_meshing"] = round(time.time() - start, 3)
        (output / (shape + "_decision.json")).write_text(to_json(decision, indent=1) + "\n", encoding="utf-8")
        # A runnable input example with no secrets or real material claims.
        if shape == "beam":
            recipe = dict((key, value) for key, value in spec.items() if key not in ("volume_mesh", "convergence_mesh"))
            (output / "beam_spec.json").write_text(to_json(recipe, indent=1) + "\n", encoding="utf-8")
            (output / "beam_surfaces.json").write_text(to_json(surfaces, indent=1) + "\n", encoding="utf-8")
            (output / "beam.stl").write_bytes(Path(path).read_bytes())
            (output / "beam_task.txt").write_text("Консоль. Закрепить левый торец. На правый торец приложена сила 10 Н по -Z. Передняя сторона видимая, без следов поддержек.\n", encoding="utf-8")
    beam = shapes["beam"]["selected"]["mechanics"]
    theoretical_k = 3 * 2000 * (12 * 8 ** 3 / 12) / 60 ** 3
    stiffness_error = abs(beam["effective_stiffness_N_per_mm"] - theoretical_k) / theoretical_k
    if stiffness_error > 0.03:
        raise AssertionError("cantilever response exceeds 3 percent of independent Euler-Bernoulli reference")
    hashes = {}
    grammar = []
    for path in sorted((ROOT / "plugin" / "ste_planner").glob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 5))
        grammar.append(path.name)
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {"source": "numerical_software_benchmarks_not_physical_prints", "models": shapes,
              "cantilever_reference": {"method": "Euler-Bernoulli solid beam; transverse shear and clamp effects omitted",
                                       "stiffness_N_per_mm": theoretical_k, "relative_stiffness_error": stiffness_error},
              "python35_grammar_modules": len(grammar), "source_sha256": hashes,
              "Ra_um": None, "flatness_mm": None,
              "not_verified": ["physical material calibration", "actual STE runtime modifier/G-code application",
                               "5D head/fixture collision clearance", "physical surface Ra and thermal flatness",
                               "embedded Windows Python 3.5 execution"]}
    (ROOT / "experiments" / "functional_results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    module = importlib.util.spec_from_file_location("render_functional_maps", ROOT / "plugin" / "tools" / "render_functional_maps.py")
    renderer = importlib.util.module_from_spec(module)
    module.loader.exec_module(renderer)
    renderer.render(json.loads((output / "beam_decision.json").read_text(encoding="utf-8")), output / "beam_maps.png")
    renderer.render(json.loads((output / "wedge_decision.json").read_text(encoding="utf-8")), output / "wedge_maps.png")
    print(json.dumps({"cantilever_relative_stiffness_error": stiffness_error,
                      "beam_changes": shapes["beam"]["changes"], "wedge_changes": shapes["wedge"]["changes"],
                      "grammar_modules": len(grammar)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
