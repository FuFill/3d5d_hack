"""Frozen additional synthetic holdout; do not use failures to tune this run."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from check_functional_llm import ROOT, assistant, run_case

CASES = [
    {"id": "holdout_positive_y", "text": "Нужна консоль. Левый торец зафиксировать. На правый торец действует 7 Н по +Y.",
     "force": [0, 7, 0], "roles": [("root", "fixture")]},
    {"id": "holdout_negative_x", "text": "Рабочая консоль: левый торец зажат. К правому торцу приложить силу 12 Н по -X. Передняя сторона плоская, контакт поддержек запрещён.",
     "force": [-12, 0, 0], "roles": [("root", "fixture"), ("front", "flat"), ("front", "no_support")]},
    {"id": "holdout_fraction", "text": "Консоль. Закреплён левый торец. На правый торец приложена сила 0,75 N по -Z. Передняя сторона видимая.",
     "force": [0, 0, -.75], "roles": [("root", "fixture"), ("front", "visible")]},
    {"id": "holdout_unknown_direction", "text": "Консоль. Левый торец закрепить. На правом торце сила 8 Н, направление ещё не определено.", "missing": True},
]


def main():
    assistant.load_env(str(ROOT / ".env"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(run_case, CASES))
    unsafe = [r["id"] for r in rows if r["status"] == "computed" and not r["correct"]]
    report = {"evaluation_split": "additional frozen synthetic holdout; no tuning on this result",
              "case_sha256": hashlib.sha256(json.dumps(CASES, sort_keys=True).encode("utf-8")).hexdigest(),
              "physical_generalization_accuracy": None, "cases": rows,
              "correct": sum(r["correct"] for r in rows), "count": len(rows),
              "accepted_incorrect_cases": unsafe,
              "note": "Four synthetic texts cannot establish physical or broad linguistic generalization"}
    (ROOT / "experiments/llm_holdout_results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("correct", "count", "accepted_incorrect_cases")}))
    if unsafe:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
