"""Run only runtime-owned contracts in embedded 3.5; FEA belongs to modern worker."""
import glob
import json
import os
import sys

import numpy
import mini_runner

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXCLUDED = {"test_assistant.py", "test_elasticity.py", "test_functional.py", "test_pressure_fields.py"}
tests = os.path.join(ROOT, "plugin", "tests")
scratch = os.path.join(ROOT, ".runtime", "embedded_contracts")
os.makedirs(scratch, exist_ok=True)
results = {}
for path in sorted(glob.glob(os.path.join(tests, "test_*.py"))):
    name = os.path.basename(path)
    if name not in EXCLUDED:
        partial = mini_runner.run(tests, os.path.join(scratch, name + ".json"), name)
        results.update(partial["results"])
counts = {}
for row in results.values():
    counts[row["status"]] = counts.get(row["status"], 0) + 1
report = {"python": sys.version, "numpy": numpy.__version__, "counts": counts, "results": results,
          "scope": "embedded runtime contracts; modern-worker FEA/meshing tested separately",
          "excluded_worker_modules": sorted(EXCLUDED)}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
