"""Publish all real-case outcomes, verify hashes, never choose missing physics."""
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin"))
from ste_planner.sliced_search import decision_digest


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    root = ROOT / "experiments/real_toolpath_physics"
    records, errors = [], []
    for item in read(root / "manifest.json"):
        record = {"id": item["id"], "organizer_case": item["organizer_case"]}
        folder = root / item["id"]
        if not (folder / "004_slice.json").exists():
            record.update(status=item.get("status", "missing_native_result"), reason=item.get("reason"))
            if "status" not in item:
                errors.append(item["id"] + ": no native response")
            records.append(record)
            continue
        native, decision = read(folder / "004_slice.json"), read(folder / "decision.json")
        applied = read(folder / "002_apply.json")
        binding = (digest(ROOT / item["stl"]) == item["stl_sha256"] and
                   decision_digest(decision) == applied.get("decision_sha256"))
        record.update(mode=item["mode"], phase_deg=item["orientation_phase_deg"], status=native["status"],
                      decision_and_source_binding="pass" if binding else "fail",
                      earlier_failed_attempt_preserved=(folder / "earlier_attempt_slice.json").exists())
        if not binding:
            errors.append(item["id"] + ": decision/source mismatch")
        if (folder / "physics.json").exists():
            physics = read(folder / "physics.json")
            record["strict_physics_status"] = physics["strict"]["status"]
        else:
            errors.append(item["id"] + ": missing physics outcome")
            physics = {}
        if native["status"] == "slice_completed":
            archive = folder / "part.gcode.gz"
            if not archive.exists():
                errors.append(item["id"] + ": missing lossless G-code archive")
            else:
                raw_hash = hashlib.sha256(gzip.decompress(archive.read_bytes())).hexdigest()
                provenance = physics.get("archive", {})
                verified = (raw_hash == native["gcode"]["sha256"] == provenance.get("uncompressed_sha256") and
                            digest(archive) == provenance.get("sha256"))
                record["gcode_archive_hash"] = "pass" if verified else "fail"
                if not verified:
                    errors.append(item["id"] + ": stale G-code/physics archive")
            if digest(folder / "part.3mf") != native["project"]["sha256"]:
                errors.append(item["id"] + ": 3MF digest mismatch")
            condition = physics.get("conditional_nominal_pose", physics.get("strict", {}))
            value = condition.get("value") or {}
            record["nominal_deposition"] = value.get("deposition")
            record["kinematics_assumed"] = "conditional_nominal_pose" in physics
            record["failed_native_checks"] = [c for c in native["inspection"]["checks"] if c["status"] == "fail"]
            record["unknown_native_checks"] = [c for c in native["inspection"]["checks"] if c["status"] == "unknown"]
            record["native_physics_context"] = native.get("physics_context")
        else:
            record["reason"] = native.get("message")
            record["native_geometry_errors"] = native.get("native_geometry_errors", [])
        # Keep only diagnostically relevant log entries, not large engine CLI dumps.
        process_path = folder / "process.json"
        if process_path.exists():
            log = Path(read(process_path)["isolated_profile"]) / "steslicer.log"
            if log.exists():
                diagnostics = {"log_sha256": digest(log),
                    "error_lines": [s[:3000] for s in log.read_text(encoding="utf-8", errors="replace").splitlines() if " - ERROR - " in s]}
                (folder / "native_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        records.append(record)
    summary = {"data_scope": "seven original organizer STL/TZ; development geometry, not blind physical holdout",
               "native_candidate_contexts": sum("mode" in r for r in records),
               "completed_native_slices": sum(r["status"] == "slice_completed" for r in records),
               "native_errors": sum(r["status"] == "error" for r in records),
               "refused_routes": sum(r["status"] == "route_refused_before_slicing" for r in records),
               "physics_optimum_established": False, "physical_print_approval": False,
               "provenance_check": "pass" if not errors else "fail", "provenance_errors": errors, "records": records}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    lines = ["# Реальные детали: STE → дорожки → физический вход", "",
        "Исходные семь STL/ТЗ организаторов; по два наклона 3D и две фазы каждого допустимого 5D маршрута.",
        "Каждый кандидат нарезан отдельно. Один образец на слайс. Результаты не являются испытаниями отпечатков.", "",
        "Нативных контекстов: %s; завершённых слайсов: %s; ошибок backend: %s; отказов маршрутов до слайсинга: %s." %
        tuple(summary[k] for k in ("native_candidate_contexts", "completed_native_slices", "native_errors", "refused_routes")),
        "Проверка хэшей исходных STL, decision, 3MF и распакованных G-code: **%s**." % summary["provenance_check"], "",
        "| Кандидат | STE | Физический статус | Номинальный материал детали, cm³ | Дорожек/хорд |", "|---|---|---|---:|---:|"]
    for r in records:
        d = r.get("nominal_deposition") or {}
        volume = "%.3f" % (d["part_volume_mm3"]/1000) if d else "—"
        lines.append("| %s | %s | %s | %s | %s |" % (r["id"], r["status"], r.get("strict_physics_status", "—"), volume, d.get("road_count", "—")))
    lines += ["", "Объём — расчёт по командам подачи E после погашения ретракции, а не взвешивание готовой детали.",
        "Для 5D численные направления/длины даны отдельно при номинальном AC-контракте; строгий режим без измеренной кинематики отказывает.",
        "Расход между 3D/5D нельзя объявлять преимуществом, пока не проверены полнота геометрии, режим построения и эквивалентность свойств.", "",
        "## Обнаруженные ограничения", "",
        "- Исправлена передача индексированной сетки в CLIParserBackend, который повторно применял старые индексы после разворачивания вершин. Передаются те же треугольники с последовательными индексами, исходный STL не меняется.",
        "- Прежний Boolean-fallback колеса (2.432 cm³) исключён из физических выводов и сохранён в representation_regression. После исправления расход порядка 47 cm³; исходные отказы и повторные проверки сохранены.",
        "- Наклон 90° крыльчатки имеет `model_height_coverage=fail`; это открытая геометрическая проверка, а не разрешённая печать.",
        "- После таймаутов штатные движки оставались запущены. Изолированный launcher теперь ограничивает их Windows Job Object; пользовательские процессы не входят в него.",
        "- Для исходной шестерни Gmsh обнаружил пересекающиеся грани. Этот отказ сохранён; исходный STL не исправлялся молча.",
        "- Для колеса предусмотрен символический оператор на фоновой сетке из 1536 тетраэдров. Актуальные диагностические числа находятся в physics.json; прежний оператор повреждённой сетки исключён. Материал в пустые ячейки не добавляется.",
        "- Числа абсолютной жёсткости/прочности и физический оптимум отсутствуют: измеренные road/interface свойства и закрепления не заданы.", "",
        "## Необходимые сведения по назначению", "",
        "1. Шестерня: силовые контакты зубьев/ступицы, циклы, температурно-влажностные свойства и усталость.",
        "2. TPU-колесо: контактная нагрузка и закрепление; нелинейный закон, гистерезис и циклические испытания.",
        "3. Сильфон: большие деформации, контакт, допустимое усилие и ресурс; линейная статика недостаточна.",
        "4. Декоративная фигурка: критерии поверхности/опор; силовой расчёт требует отдельно заданного сценария.",
        "5. Крыльчатка: закрепление, плотность, центробежная/гидродинамическая нагрузка, свойства при рабочей температуре.",
        "6. Вентилятор: плотность и ось вращения, закрепление и аэродинамическая нагрузка; массовые силы пока не реализованы в этом FE интерфейсе.",
        "7. Ось: пятна контакта, закрепление, сочетание сил/моментов, усталость и износ.", "",
        "## Воспроизведение и отсутствие утечки", "",
        "Рецепт: [TOOLPATH_PHYSICS.md](../../plugin/TOOLPATH_PHYSICS.md); [формулы и статьи](../../data/research/07_toolpath_physics_math.md).",
        "`summary.json` содержит все исходы, хэши, failed/unknown проверки и условные тензоры направлений.",
        "`part.gcode.gz` сохраняет исходные байты; `004_slice.json` хранит исходный SHA-256. Нельзя менять окончания строк после распаковки.",
        "Эти детали не обозначены blind holdout. ML здесь не обучался, параметры материалов не подгонялись по результатам слайсинга.",
        "Программные тесты на точных решениях и синтетических причинных моделях остаются верификацией кода, а не физическими метками этих деталей."]
    (root / "REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("native_candidate_contexts", "completed_native_slices", "native_errors", "refused_routes", "provenance_check", "provenance_errors")}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
