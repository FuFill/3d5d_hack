"""Build a relocatable Lenaz shell + byte-identical canonical core bundle.

No credentials, virtual environments, user profiles or training artifacts are copied.
Configure STE_PLANNER_PYTHON (modern Python + requirements-functional.txt) on install.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def build(output):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new output directory: %s" % output)
    output.parent.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "core", "bundle_manifest.json", "local_config.json")
    shutil.copytree(ROOT / "AIAssistant", output, ignore=ignore)
    core = output / "core" / "ste_planner"
    shutil.copytree(ROOT / "plugin" / "ste_planner", core, ignore=ignore)
    manifest = {}
    for path in sorted(core.rglob("*")):
        if path.is_file():
            source = ROOT / "plugin" / "ste_planner" / path.relative_to(core)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == original
            manifest[str(path.relative_to(output)).replace("\\", "/")] = original
    (output / "bundle_manifest.json").write_text(json.dumps({"canonical_core_sha256": manifest}, indent=2), encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(build(args.output))


if __name__ == "__main__":
    main()
