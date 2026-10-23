# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Plan the organiser example cases and write reports.

Usage: python plan_examples.py [examples_dir] [out_dir]
       [--calibration measured.json] [--setting-change SETTING=VALUE]
Writes <out_dir>/<case>.json (full Decision) and <out_dir>/summary.md (overview in Russian).
"""

import argparse
import glob
import io
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ste_planner import planner  # noqa: E402
from ste_planner.result import to_json  # noqa: E402


def cases(examples_dir):
    out = []
    for folder in sorted(glob.glob(os.path.join(examples_dir, "*"))):
        docx = glob.glob(os.path.join(folder, "*.docx"))
        stl = [p for p in glob.glob(os.path.join(folder, "*")) if p.lower().endswith(".stl")]
        if docx and stl:
            out.append((os.path.basename(folder), docx[0], stl[0]))
    return out


def main(examples_dir, out_dir, calibration_data=None, setting_changes=None):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    lines = ["# Решения планировщика по примерам организаторов", "",
             "Сгенерировано `tools/plan_examples.py`; полные данные — в JSON рядом.", "",
             "| Кейс | Статус | Материал | Режим | Ключевые изменения |", "|---|---|---|---|---|"]
    details = []
    for name, docx, stl in cases(examples_dir):
        dec = planner.plan_case_files(docx, stl, calibration_data=calibration_data, setting_changes=setting_changes)
        slug = name.split(".")[0].strip()
        with io.open(os.path.join(out_dir, "case_%s.json" % slug), "w", encoding="utf-8") as f:
            f.write(to_json(dec, indent=1))
        items = dec["setting_patch"]["items"]
        short = ", ".join("%s=%s" % (i["key"], i["value"]) for i in items if i["key"] != "printing_mode")
        mat = dec["chosen_material"]["label"] if dec["chosen_material"] else "—"
        lines.append("| %s | %s | %s | %s | %s |" % (name, dec["status"], mat, dec["route"]["mode"], short))
        details += ["", "## %s" % name, ""] + ["- " + l for l in dec["explanation"]]
        if dec["missing_inputs"]:
            details.append("- Недостающие входные данные: " + ", ".join(
                "%s.%s" % (m["requirement_type"], m["field"]) for m in dec["missing_inputs"]))
        details.append("- План испытаний: " + "; ".join(p["test"] for p in dec["qualification_plan"][:6]))
    with io.open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines + details) + "\n")
    print("written to", out_dir)


def _setting_change(text):
    try:
        key, raw = text.split("=", 1)
        value = float(raw)
        if not key.strip() or not math.isfinite(value):
            raise ValueError()
        return key.strip(), value
    except ValueError:
        raise argparse.ArgumentTypeError("expected SETTING=FINITE_NUMBER in native slicer units")


if __name__ == "__main__":
    repo = os.path.dirname(os.path.dirname(HERE))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("examples_dir", nargs="?", default=os.path.join(repo, "data", "examples"))
    parser.add_argument("out_dir", nargs="?", default=os.path.join(os.path.dirname(HERE), "reports"))
    parser.add_argument("--calibration", help="JSON list of measured experiments for Double ML")
    parser.add_argument("--setting-change", type=_setting_change, action="append", default=[],
                        help="independent proposed change, e.g. infill_sparse_density=60 (not automatically applied)")
    args = parser.parse_args()
    changes = dict(args.setting_change)
    if len(changes) != len(args.setting_change):
        parser.error("each setting may be requested only once")
    data = None
    if args.calibration:
        with io.open(args.calibration, encoding="utf-8") as f:
            data = json.load(f)
    main(args.examples_dir, args.out_dir, calibration_data=data, setting_changes=changes)
