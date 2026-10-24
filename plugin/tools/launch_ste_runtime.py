"""Install the runtime extension into an isolated profile and launch real STE.

Does not replace the user's installed plugins, current scene or preferences.
The profile clone remains ignored under .runtime and is never a publish artifact.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[2]


def owned_process_job():
    """Windows closes only this launch's descendants when its job handle closes."""
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        if handle:
            api.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    return api, handle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slicer", default=r"C:\Program Files\Stereotech STE Slicer 7.10")
    parser.add_argument("--job", default=str(ROOT / "experiments" / "ste_runtime" / "job.json"))
    parser.add_argument("--timeout", type=float, default=400., help="bounded lifetime of this isolated application and its engines")
    parser.add_argument("--assistant", action="store_true", help="Include the merged AI Assistant shell for native integration checks")
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
        shutil.copytree(original, profile, ignore=shutil.ignore_patterns("plugins", "cache", "ai_assistant_browser",
                                                                      "*.log", "*.3mf", "*.gcode"))
    plugins = profile / "plugins"
    plugins.mkdir(exist_ok=True)
    target = plugins / "STEPlannerRuntime"
    shutil.copytree(ROOT / "plugin" / "STEPlannerRuntime", target, dirs_exist_ok=True)
    shutil.copytree(ROOT / "plugin" / "ste_planner", target / "ste_planner", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if args.assistant:
        from build_ai_assistant import build
        assistant = build(plugins / "AIAssistant")
        # Application-owned local paths only. Credentials never enter the bundle.
        (assistant / "local_config.json").write_text(json.dumps({"env_file": str(ROOT / ".env")}), encoding="utf-8")
    env = dict(os.environ)
    env.update(APPDATA=str(isolated / "appdata"), LOCALAPPDATA=str(isolated / "local"),
               STE_PLANNER_RUNTIME_JOB=str(job), STE_PLANNER_ISOLATED_ROOT=str(isolated))
    if args.assistant:
        import sys
        env["STE_PLANNER_PYTHON"] = sys.executable
    (isolated / "local").mkdir(parents=True, exist_ok=True)
    api, owned = owned_process_job()
    process = subprocess.Popen([str(Path(args.slicer) / "STESlicerCLI.exe"), "--headless"],
                               cwd=args.slicer, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    if not api.AssignProcessToJobObject(owned, wintypes.HANDLE(int(process._handle))):
        error = ctypes.get_last_error()
        process.terminate()
        api.CloseHandle(owned)
        raise ctypes.WinError(error)
    (job.parent / "process.json").write_text(json.dumps({"pid": process.pid, "isolated_profile": str(profile),
                                                        "job": str(job)}, indent=2), encoding="utf-8")
    print("Isolated STE PID %s; output directory: %s" % (process.pid, job.parent))
    try:
        code = process.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        code = 124
    finally:
        api.CloseHandle(owned)
    (job.parent / "process_exit.json").write_text(json.dumps({"exit_code": code}), encoding="utf-8")
    print("STE exited: %s" % code)
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
