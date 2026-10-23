"""Install the runtime extension into an isolated profile and launch real STE.

Does not replace the user's installed plugins, current scene or preferences.
The profile clone remains ignored under .runtime and is never a publish artifact.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slicer", default=r"C:\Program Files\Stereotech STE Slicer 7.10")
    parser.add_argument("--job", default=str(ROOT / "experiments" / "ste_runtime" / "job.json"))
    args = parser.parse_args()
    job = Path(args.job).resolve()
    job.parent.mkdir(parents=True, exist_ok=True)
    if not job.exists():
        job.write_text(json.dumps({"commands": [{"command": "inspect"}]}), encoding="utf-8")
    # Every invocation starts from a clean clone, so no prior validation
    # changes/quality containers can contaminate the next baseline.
    isolated = ROOT / ".runtime" / "ste" / ("run_" + uuid.uuid4().hex[:10])
    profile = isolated / "appdata" / "steslicer" / "7.10"
    if not profile.exists():
        original = Path(os.environ["APPDATA"]) / "steslicer" / "7.10"
        shutil.copytree(original, profile, ignore=shutil.ignore_patterns("plugins", "cache", "*.log", "*.3mf", "*.gcode"))
    plugins = profile / "plugins"
    plugins.mkdir(exist_ok=True)
    target = plugins / "STEPlannerRuntime"
    shutil.copytree(ROOT / "plugin" / "STEPlannerRuntime", target, dirs_exist_ok=True)
    shutil.copytree(ROOT / "plugin" / "ste_planner", target / "ste_planner", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = dict(os.environ)
    env.update(APPDATA=str(isolated / "appdata"), LOCALAPPDATA=str(isolated / "local"),
               STE_PLANNER_RUNTIME_JOB=str(job), STE_PLANNER_ISOLATED_ROOT=str(isolated))
    (isolated / "local").mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([str(Path(args.slicer) / "STESlicerCLI.exe"), "--headless"],
                               cwd=args.slicer, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    (job.parent / "process.json").write_text(json.dumps({"pid": process.pid, "isolated_profile": str(profile),
                                                        "job": str(job)}, indent=2), encoding="utf-8")
    print("Isolated STE PID %s; output directory: %s" % (process.pid, job.parent))
    code = process.wait()
    (job.parent / "process_exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
    print("STE exited: %s" % code)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
