"""Purpose-aware planning with optional live LLM and Gmsh preprocessing.

Modern worker usage (from repo root):
 python plugin/tools/plan_functional.py part.stl task.txt --spec measured.json
     --surfaces surfaces.json --llm --mesh-size 6 --output decision.json
"""

import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ste_planner import assistant, planner, volume_mesh
from ste_planner.result import to_json


def read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mesh")
    parser.add_argument("task", help="UTF-8 text file")
    parser.add_argument("--spec", required=True, help="scoped measured process profiles, loads, regions and limits")
    parser.add_argument("--surfaces", help="application-owned surface catalogue for LLM intake")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--env", default=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))
    parser.add_argument("--mesh-size", type=float, help="generate coarse/finer conforming meshes in mm")
    parser.add_argument("--resources", help="STE resources directory")
    parser.add_argument("--mode", choices=("classic", "cylindrical", "cylindrical_full"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--noise", help="JSON with explicit source and interval bounds; never inferred by LLM")
    parser.add_argument("--robustness-limits", help="JSON of measured/design tolerance intervals")
    args = parser.parse_args()
    spec = read_json(args.spec)
    with io.open(args.task, encoding="utf-8") as f:
        text = f.read()
    if args.mesh_size is not None:
        for key, size in (("volume_mesh", args.mesh_size), ("convergence_mesh", args.mesh_size * 0.67)):
            result = volume_mesh.tetrahedralize(args.mesh, size)
            if not result.ok:
                parser.error(result.trace[0]["reason"])
            spec[key] = result.value
        spec["geometry_sha256"] = spec["volume_mesh"]["geometry_sha256"]
    options = {"resources_dir": args.resources, "forced_mode": args.mode}
    if args.noise:
        options["noise_spec"] = read_json(args.noise)
    if args.robustness_limits:
        options["robustness_limits"] = read_json(args.robustness_limits)
    if args.llm:
        if not args.surfaces:
            parser.error("--llm requires application-owned --surfaces")
        if os.path.isfile(args.env):
            assistant.load_env(args.env)
        decision = assistant.plan_from_text(text, args.mesh, spec, read_json(args.surfaces), **options)
    else:
        decision = planner.plan_case(text, args.mesh, functional_spec=spec, **options)
    with io.open(args.output, "w", encoding="utf-8") as f:
        f.write(to_json(decision, indent=1))
    print("status=%s; report=%s" % (decision["status"], args.output))


if __name__ == "__main__":
    main()
