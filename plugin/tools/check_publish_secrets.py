"""Check staged files/3MF contents for local secret values; never print secrets."""
import base64
import gzip
import io
import json
import re
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    secrets = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and any(word in key.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")) and len(value) >= 8:
                raw = value.encode("utf-8")
                secrets.extend([raw, base64.b64encode(raw)])
    names = subprocess.check_output(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=str(ROOT)).decode("utf-8").split("\0")
    problems, count, archives = [], 0, 0
    for name in filter(None, names):
        parts = Path(name).parts
        if ((Path(name).name.startswith(".env") and Path(name).name != ".env.example") or
                any(p in (".runtime", "STE-Slicer") or p.startswith(".venv") for p in parts)):
            problems.append({"file": name, "reason": "private runtime/environment path staged"})
            continue
        data = subprocess.check_output(["git", "show", ":" + name], cwd=str(ROOT))
        buffers = [data]
        if name.lower().endswith(".3mf"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                buffers.extend(archive.read(member) for member in archive.namelist())
            archives += 1
        elif name.lower().endswith(".gcode.gz"):
            buffers.append(gzip.decompress(data))
        if any(secret in buffer for buffer in buffers for secret in secrets):
            problems.append({"file": name, "reason": "local secret value detected"})
        key_block = re.compile(br"-----BEGIN (?:RSA )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{64,}\r?\n-----END (?:RSA )?PRIVATE KEY-----")
        if any(key_block.search(buffer) for buffer in buffers):
            problems.append({"file": name, "reason": "private key marker detected"})
        count += 1
    report = {"status": "fail" if problems else "pass", "staged_files_checked": count,
              "three_mf_archives_checked": archives, "local_secret_values_available": bool(secrets),
              "problems": problems, "scope": "exact local secret values/base64 and private-key markers; not a universal DLP guarantee"}
    print(json.dumps(report, indent=2))
    if not secrets:
        raise SystemExit("No local secret values available for the promised exact-value check")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
