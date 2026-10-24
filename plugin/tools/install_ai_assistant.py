"""Install a built plugin, backing up a previous shell. Writes local paths, no keys."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from build_ai_assistant import build, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=str(Path(os.environ["APPDATA"]) / "steslicer/7.10"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    args = parser.parse_args()
    profile = Path(args.profile).resolve(strict=True)
    plugins = profile / "plugins"
    plugins.mkdir(exist_ok=True)
    target = plugins / "AIAssistant"
    # Validate the exact destination before moving any existing directory.
    if target.resolve().parent != plugins.resolve() or target.is_symlink():
        raise ValueError("Plugin target must be directly within the selected profile/plugins")
    python = Path(args.python).resolve(strict=True)
    bundle = build(ROOT / ".runtime" / ("assistant_install_" + uuid.uuid4().hex))
    local = {"worker_python": str(python)}
    env_file = Path(args.env_file).resolve()
    if env_file.is_file():
        local["env_file"] = str(env_file)
    (bundle / "local_config.json").write_text(json.dumps(local, indent=2), encoding="utf-8")
    if target.exists():
        backup = ROOT / ".runtime" / ("assistant_backup_" + uuid.uuid4().hex)
        shutil.move(str(target), str(backup))
        print("Previous plugin backed up: %s" % backup)
    shutil.copytree(bundle, target)
    print("Installed: %s" % target)
    print("Restart STE Slicer; Extensions -> AI Assistant -> Open chat")


if __name__ == "__main__":
    main()
