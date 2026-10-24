"""Freeze current native evidence at user stop; never publish stale physics."""
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/real_toolpath_physics"


def main():
    pending = []
    for item in json.loads((OUT / "manifest.json").read_text(encoding="utf-8")):
        folder = OUT / item["id"]
        path = folder / "004_slice.json"
        if not path.exists():
            continue
        native = json.loads(path.read_text(encoding="utf-8"))
        if native.get("status") != "slice_completed":
            continue
        report = folder / "physics.json"
        previous = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        if previous.get("archive", {}).get("uncompressed_sha256") == native["gcode"]["sha256"]:
            continue
        raw = (folder / "part.gcode").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == native["gcode"]["sha256"]
        archive = folder / "part.gcode.gz"
        with archive.open("wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as packed:
                packed.write(raw)
        value = {"id": item["id"], "input": item, "strict": {"status": "analysis_pending", "value": None},
                 "reason": "stopped at user time limit; prior physics did not match the current corrected native slice",
                 "physical_print_approval": False, "archive": {"path": archive.name,
                    "uncompressed_sha256": hashlib.sha256(raw).hexdigest(), "uncompressed_bytes": len(raw),
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}
        report.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        pending.append(item["id"])
    print(json.dumps({"analysis_pending": pending}))


if __name__ == "__main__":
    main()
