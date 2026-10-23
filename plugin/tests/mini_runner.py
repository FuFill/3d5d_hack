# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Minimal test runner without pytest (for the Python 3.5 interpreter embedded in STE Slicer).

run(tests_dir, out_path, pattern) imports tests/test_*.py, runs every test_* function in
source order, and writes a JSON report {name: {"status": pass|fail|skip, "detail": ...}}.
"""

import glob
import importlib
import inspect
import json
import os
import sys
import time
import traceback
import unittest


def run(tests_dir, out_path, pattern="test_*.py"):
    tests_dir = os.path.abspath(tests_dir)
    for p in (tests_dir, os.path.dirname(tests_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    report = {"python": sys.version, "results": {}}
    try:
        import numpy
        report["numpy"] = numpy.__version__
    except ImportError:
        report["numpy"] = None
    for path in sorted(glob.glob(os.path.join(tests_dir, pattern))):
        modname = os.path.splitext(os.path.basename(path))[0]
        try:
            mod = importlib.import_module(modname)
        except Exception:
            report["results"][modname] = {"status": "fail", "detail": "import: " + traceback.format_exc()}
            continue
        funcs = [(name, f) for name, f in vars(mod).items()
                 if name.startswith("test_") and inspect.isfunction(f) and f.__module__ == modname]
        funcs.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
        for name, f in funcs:
            key = "%s::%s" % (modname, name)
            t0 = time.time()
            try:
                f()
                status, detail = "pass", ""
            except unittest.SkipTest as e:
                status, detail = "skip", str(e)
            except Exception:
                status, detail = "fail", traceback.format_exc()
            report["results"][key] = {"status": status, "detail": detail, "seconds": round(time.time() - t0, 3)}
    counts = {}
    for r in report["results"].values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    report["counts"] = counts
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)
    return report


if __name__ == "__main__":
    rep = run(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)),
              sys.argv[2] if len(sys.argv) > 2 else "mini_report.json")
    print(rep["counts"])
