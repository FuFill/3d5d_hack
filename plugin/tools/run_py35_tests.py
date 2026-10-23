# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Run the planner tests inside the Python 3.5 interpreter shipped with STE Slicer (Windows).

The distribution is frozen (python35.dll + python35.zip with numpy 1.15, scipy, trimesh);
there is no python.exe. This host script (any CPython >= 3.8, 64-bit) loads python35.dll
with ctypes in the same process, points it to the distribution's library zip and runs
tests/mini_runner.py. The two interpreters are separate DLLs and do not share state.

Usage: python run_py35_tests.py [--slicer "C:\\Program Files\\Stereotech STE Slicer 7.10"]
                               [--pattern test_*.py] [--out report.json]
"""

import argparse
import ctypes
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
DEFAULT_SLICER = r"C:\Program Files\Stereotech STE Slicer 7.10"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slicer", default=os.environ.get("STE_SLICER_DIR", DEFAULT_SLICER))
    ap.add_argument("--pattern", default="test_*.py")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "ste_planner_py35_report.json"))
    ap.add_argument("--script", help="run this file in Python 3.5 instead of the tests (argv: --out path)")
    args = ap.parse_args()
    dll = os.path.join(args.slicer, "python35.dll")
    if not os.path.exists(dll):
        print("python35.dll not found in %s" % args.slicer)
        return 2
    os.add_dll_directory(args.slicer)
    os.environ["PATH"] = args.slicer + os.pathsep + os.environ.get("PATH", "")
    os.environ["STE_RESOURCES_DIR"] = os.path.join(args.slicer, "resources")
    py = ctypes.PyDLL(dll)
    py.Py_SetPythonHome.argtypes = [ctypes.c_wchar_p]
    py.Py_SetPath.argtypes = [ctypes.c_wchar_p]
    py.Py_SetPath(os.pathsep.join([os.path.join(args.slicer, "python35.zip"), args.slicer]))
    py.Py_SetPythonHome(args.slicer)
    py.Py_Initialize()
    py.PyRun_SimpleString.argtypes = [ctypes.c_char_p]
    log = args.out + ".log"
    code = "\n".join([
        "import sys, os, traceback",
        "_log = open(%r, 'w', encoding='utf-8')" % log,
        "sys.stdout = sys.stderr = _log",
        "os.environ['STE_RESOURCES_DIR'] = %r" % os.environ["STE_RESOURCES_DIR"],
        "sys.path.insert(0, %r)" % os.path.join(PLUGIN, "tests"),
        "sys.path.insert(0, %r)" % PLUGIN,
        "try:",
        ("    import runpy; sys.argv = [%r, %r]; runpy.run_path(%r, run_name='__main__')" %
         (args.script, args.out, args.script)) if args.script else
        "    import mini_runner; mini_runner.run(%r, %r, %r)" % (os.path.join(PLUGIN, "tests"), args.out,
                                                                args.pattern),
        "except BaseException:",
        "    traceback.print_exc()",
        "_log.close()",
    ])
    rc = py.PyRun_SimpleString(code.encode("utf-8"))
    if not os.path.exists(args.out):
        print("runner failed (rc=%s); log:" % rc)
        if os.path.exists(log):
            print(open(log, encoding="utf-8").read())
        return 1
    if args.script:
        print(open(args.out, encoding="utf-8").read())
        return 0
    with open(args.out, encoding="utf-8") as f:
        rep = json.load(f)
    print(rep["python"].split()[0], "numpy", rep.get("numpy"), rep["counts"])
    for name, r in sorted(rep["results"].items()):
        if r["status"] != "pass":
            print("%s %s\n%s" % (r["status"].upper(), name, r["detail"][-1500:]))
    return 0 if not rep["counts"].get("fail") else 1


if __name__ == "__main__":
    sys.exit(main())
