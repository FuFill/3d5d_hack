# -*- coding: utf-8 -*-
"""Прогон бэйзлайна по примерам организаторов: results/baseline/<кейс>/decision.json, report.md и SUMMARY.md.

    python scripts/run_baseline.py [--examples data/examples] [--out results/baseline]
"""

import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin", "AIAssistant", "agent"))

from baseline.decision import recommend  # noqa: E402
from baseline.documents import find_case_files, long_path, read_text  # noqa: E402
from baseline.explain import MODE_LABEL, explain_decision  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", default=os.path.join(ROOT, "data", "examples"))
    parser.add_argument("--out", default=os.path.join(ROOT, "results", "baseline"))
    args = parser.parse_args(argv)

    rows = []
    for case in sorted(os.listdir(long_path(args.examples))):
        folder = os.path.join(args.examples, case)
        model, docs, _ = find_case_files(folder)
        if model is None:
            continue
        text = "\n".join(read_text(p) for p in docs)
        t0 = time.time()
        decision = recommend(text, model_path=model, hint=case)
        elapsed = time.time() - t0
        out_dir = long_path(os.path.join(args.out, case))
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        decision["scene_operations"][0]["path"] = os.path.relpath(model.replace("\\\\?\\", ""), ROOT).replace("\\", "/")
        with io.open(os.path.join(out_dir, "decision.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(decision, ensure_ascii=False, indent=1, sort_keys=True))
        with io.open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(explain_decision(decision, title=case))
        c = decision["chosen_candidate"]
        patch = dict((p["key"], p["value"]) for p in decision["setting_patch"])
        rows.append((case, MODE_LABEL[c["printing_mode"]], c["material"]["label"], c["quality"]["quality_type"],
                     decision["effective_settings"]["layer_height"], patch.get("wall_line_count"),
                     patch.get("infill_sparse_density"), patch.get("support_enable"),
                     c["orientation"]["up_vector_model"], decision["status"], elapsed))
        print("%-70s %s | %s | %.1fs" % (case, c["printing_mode"], c["material"]["id"], elapsed))

    lines = ["# Бэйзлайн: решения по примерам", "",
             "Сгенерировано `scripts/run_baseline.py`. Эталонные 3MF жюри недоступны, поэтому это не оценка точности, "
             "а воспроизводимая отправная точка для агента и для сравнения следующих версий.", "",
             "| Кейс | Режим | Материал | Профиль | Слой, мм | Стенки | Заполнение, % | Поддержки | Ось модели → +Z | Статус | Время, с |",
             "|---|---|---|---|---:|---:|---:|---|---|---|---:|"]
    for r in rows:
        lines.append("| [%s](%s/report.md) | %s | %s | %s | %s | %s | %s | %s | %s | `%s` | %.1f |" % (
            r[0], r[0].replace(" ", "%20"), r[1], r[2], r[3], r[4], r[5], r[6], "да" if r[7] else "нет", r[8], r[9], r[10]))
    with io.open(long_path(os.path.join(args.out, "SUMMARY.md")), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
