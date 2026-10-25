"""Validate a saved 5D G-code with the same worker used by the installed assistant."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent.core_worker import calculate_trajectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gcode", type=Path)
    parser.add_argument("--inputs", type=Path, help="Planning JSON with trajectory_validation or the three contracts directly")
    parser.add_argument("--mode", choices=("cylindrical", "cylindrical_full"), default="cylindrical")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = gzip.decompress(args.gcode.read_bytes()) if args.gcode.suffix == ".gz" else args.gcode.read_bytes()
    text = raw.decode("utf-8-sig")
    values = json.loads(args.inputs.read_text(encoding="utf-8-sig")) if args.inputs else {}
    values = values.get("trajectory_validation", values)
    allowed = {"machine_contract", "pose_contract", "collision_geometry"}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValueError("Only machine_contract, pose_contract and collision_geometry are accepted")
    started = time.monotonic()
    request = dict(values, request_id=uuid.uuid4().hex, printing_mode=args.mode,
                   gcode_text=text, gcode_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    result = calculate_trajectory(request)["result"]
    result["elapsed_seconds"] = time.monotonic() - started
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": {k: v["status"] for k, v in result["checks"].items()},
                      "moves": result["coverage"]["moves"], "elapsed_seconds": result["elapsed_seconds"]}))
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
