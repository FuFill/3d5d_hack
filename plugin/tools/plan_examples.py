# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Plan the organiser example cases and write reports.

Usage: python plan_examples.py [examples_dir] [out_dir]
Writes <out_dir>/<case>.json (full Decision) and <out_dir>/summary.md (overview in Russian).
"""

import glob
import io
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


def main(examples_dir, out_dir):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    lines = ["# Решения планировщика по примерам организаторов", "",
             "Сгенерировано `tools/plan_examples.py`; полные данные — в JSON рядом.", "",
             "| Кейс | Статус | Материал | Режим | Ключевые изменения |", "|---|---|---|---|---|"]
    details = []
    for name, docx, stl in cases(examples_dir):
        dec = planner.plan_case_files(docx, stl)
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


if __name__ == "__main__":
    repo = os.path.dirname(os.path.dirname(HERE))
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo, "data", "examples"),
         sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(HERE), "reports"))
