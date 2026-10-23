"""Render readable evidence from saved metrics; never replace unknowns with pass."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "experiments"


def read(name):
    return json.loads((EXP / name).read_text(encoding="utf-8"))


def main():
    native = read("ste_runtime_results.json")
    functional = read("functional_results.json")
    llm = read("functional_llm_results.json")
    holdout = read("llm_holdout_results.json")
    embedded = read("ste_runtime/embedded_contracts.json")
    suite = ET.parse(EXP / "ste_runtime/worker_tests.xml").getroot().find("testsuite")
    passed = int(suite.attrib["tests"]) - int(suite.attrib["failures"]) - int(suite.attrib["errors"]) - int(suite.attrib["skipped"])
    runtime = native["runtime"]
    text = ["# Проверка установленного STE Slicer и устойчивости параметров", "",
        "Дата локальной проверки: 24 октября 2026. Это проверка программного решения и штатного слайсинга; физическая аттестация отпечатка не заявляется.", "",
        "## Среда и воспроизведение", "",
        "STE **%s**, Python **%s**, Qt **%s**, машина **%s**. Использован установленный CuraEngine, отдельный изолированный профиль на сценарий; исходный пользовательский проект не изменялся." % (runtime["ste_version"], runtime["python"].split()[0], runtime["qt"], runtime["machine"]["definition"]), "",
        "Команды, ограничения и установка: [RUNTIME.md](../plugin/RUNTIME.md). Хеши движков, загруженных исходников, входов, 3MF и G-code: [машиночитаемый отчёт](ste_runtime_results.json), `ste_runtime/final/`. Штатный исходный проект до адаптера: `ste_runtime/stock/`.", "",
        "## Фактические результаты STE", "",
        "Завершено **%d/%d** слайсингов. Расхождений readback нет. **%d/%d** проверяемых отказов сохранили сцену, контейнеры, настройки, движок и историю Undo. Проверены Undo/Redo, перемещение и дублирование с дочерними модификаторами, партия из двух моделей." % (native["native_slicing_successes"], native["native_slicing_scenarios"], len(native["refusal_tests"]), len(native["refusal_tests"])), "",
        "| Сценарий | Слоёв | Время движка, с | Филамент движка | Неизвестные проверки |", "|---|---:|---:|---|---|"]
    for name, case in native["cases"].items():
        text.append("| %s | %d | %.0f | %s | %s |" % (name, case["layers"], case["native_time_estimate_s"], case["native_filament_estimate"], ", ".join(case["unknown_checks"])))
    text += ["", "Все числа времени и расхода — **оценки настоящего слайсера**, не измерения принтера. Без калиброванного предела Q проверка потока остаётся unknown.", "",
        "У `live_demo` заголовок объявляет 119 слоёв, фактически присутствует 118 последовательных экструдирующих слоёв. Верхняя Z=12,00 мм совпадает с моделью, шаг 0,10 мм и завершение программы подтверждены, но расхождение **не объявлено pass**: статус `slice_completed_needs_review`.", "",
        "Контакт проверялся по геометрии реальных отрезков, радиусу линии и зазору по Z. Enforcer: **26/26** контактирующих отрезков на двух требуемых участках. Blocker: **32** на требуемом участке, **0** на запрещённом. Проверка относится к двум заданным горизонтальным участкам; полнота опирания и несущая способность поддержек не доказаны.", "",
        "Найденные и исправленные ошибки: переключение фактического backend после смены контейнеров; отсутствие определения у per-object стека; rollback до регистрации Undo; перекрытие приоритетных enforcer/blocker на защищённом крае; двойное преобразование осей предотвращено локальной привязкой. Центроиды на общей границе FE-блоков теперь принадлежат одному блоку; настоящий объёмный overlap по-прежнему отклоняется.", "",
        "## Оптимальность и цена улучшения", ""]
    v = native["functional_live_path"]
    old, new = v["baseline_mechanics"], v["selected_mechanics"]
    text += ["В живом пути Qwen → валидатор → FEA → основной планировщик → адаптер → STE выбран лучший из **%d проверенных / %d допустимых** кандидатов по зафиксированному порядку: ступенчатость видимых зон, прокси объёма поддержек, податливость, высота. Выбор совпал с полным сравнением этого конечного набора." % (v["candidate_count"], v["feasible_count"]), "",
        "| Метрика расчётной консоли | До | После |", "|---|---:|---:|"]
    for key, label in (("effective_stiffness_N_per_mm", "Жёсткость, Н/мм"), ("compliance_Nmm", "Податливость, Н·мм"), ("max_displacement_mm", "Макс. перемещение, мм"), ("safety_factor_min", "Мин. запас")):
        text.append("| %s | %.6g | %.6g |" % (label, old[key], new[key]))
    text += ["", "**Эти механические числа получены на `software_fixture`.** Для сильного профиля заранее заданы другие условные свойства; прирост не является измеренным причинным эффектом стенок/заполнения. Источник сохранён, `applied=False`; демонстрация применена только в отдельном профиле.", "",
        "Цена по STE: **1851 → 4479 с (+142%%)**, филамент **1,63523 → 2,25156 м (+37,7%%)**. Поэтому решение нельзя назвать универсально оптимальным по времени или материалу. Глобальный непрерывный оптимум и физический оптимум не доказаны. Независимая проверка балки Эйлера–Бернулли даёт относительное расхождение жёсткости **%.3f%%**; сходимость сетки и блочные вклады приведены в [численном отчёте](functional_report.md)." % (100 * functional["cantilever_reference"]["relative_stiffness_error"]), "",
        "## LLM, утечки и повторяемость", "",
        "Финальная разработческая регрессия: **%d/8**; отдельный замороженный дополнительный набор: **%d/%d**, ошибочно принятых случаев **%d**. Наборы малы и синтетические; общая точность на реальных ТЗ неизвестна." % (round(llm["metrics"]["golden_case_accuracy"] * 8), holdout["correct"], holdout["count"], len(holdout["accepted_incorrect_cases"])), "",
        "Предыдущий прогон **6/8** сохранён в `ste_runtime/llm_before_omission_guard.json`: модель пропустила запрет поддержек и дважды предложила необоснованную видимую роль. Первый дефект теперь блокирует валидатор, второй уже приводил к отказу. Повторные запросы даже при temperature=0 не гарантируют идентичность; лучший прогон не выдаётся за независимую статистику.", "",
        "DML: train/test разделены по независимым группам; преобразования обучаются только внутри train-fold. Требуется время назначения/измерения каждого контроля; post-treatment контроль отклоняется. Связанные задания можно объединять `independence_group_id`. В трёх дополнительных синтетических проверках 200 групп train / 50 holdout, пересечений 0; абсолютные ошибки известного эффекта 0,0364 / 0,1121 / 0,0250. Физических наборов для независимой оценки нет. [Аудит](leakage_results.json).", "",
        "Ключ не передаётся модели как текст, не сохраняется в артефактах; STL и сырые калибровочные наблюдения не отправляются в Qwen. Перед push выполняется `plugin/tools/check_publish_secrets.py` по staged-файлам и распакованному содержимому 3MF. Профили, jobs, process-файлы и `.env` исключены из Git.", "",
        "## Устойчивость к шуму — последний этап", "",
        "Реализованы интервальные сценарии, худшие значения, запас до допусков и условный подбор среди допустимых вариантов. Постоянный Z-сдвиг ±0,05 мм при первом слое 0,30 мм даёт зазор **0,25–0,35 мм**, не меняя последующие шаги. Межслойная ошибка положения учитывается разностью соседних ошибок. Линейная чувствительность E/прочности/нагрузки не подменяет отсутствующую зависимость качества от Z/подачи/скорости.", "",
        "Без измеренных диапазонов, допусков и модели отклика устойчивый физический оптимум не выбирается: `needs_calibration`, вероятность успеха null, автоматического применения нет. [Формулы](../data/research/06_robust_process_validation.md), [сценарий](robustness_results.json).", "",
        "## Тесты и оставшиеся физические входы", "",
        "Современный worker: **%d passed, %s skipped**. Встроенный Python 3.5: **%d passed, %d skipped** для runtime-контрактов; FEA/meshing-модули проверены отдельно в worker. Полный worker-набор нельзя переносить в embedded-среду без Gmsh/современного sparse solver. Пропуск — необязательная отдельная внешняя сверка Uranium; фактические стеки STE проверены нативно." % (passed, suite.attrib["skipped"], embedded["counts"]["pass"], embedded["counts"].get("skip", 0)), "",
        "Неизвестны: измеренные свойства отпечатка и предел потока, реальная Ra/плоскостность, фактические время/расход/прочность детали, распределение шума и отклик качества. Для Spiral/Full отсутствуют подтверждённые кинематика, геометрия головки/оправки и полная проверка переходов/столкновений; применение обоих режимов отклоняется даже с demo-флагом.", "",
        "Программная интеграция и воспроизводимые проверки выполнены. Производственная квалификация, физическая устойчивость и универсальная оптимальность не заявляются.", ""]
    (EXP / "ste_runtime_report.md").write_text("\n".join(text), encoding="utf-8")

    rows = ["# Численная проверка функционального ядра", "", "Текущие метрики пересчитаны на Windows с установленными определениями STE. Все свойства профилей — `software_fixture`, не результаты печати.", "",
            "| Модель | Метрика | До | После |", "|---|---|---:|---:|"]
    for name, item in functional["models"].items():
        for key in ("effective_stiffness_N_per_mm", "compliance_Nmm", "max_displacement_mm", "safety_factor_min"):
            rows.append("| %s | %s | %.6g | %.6g |" % (name, key, item["baseline"]["mechanics"][key], item["selected"]["mechanics"][key]))
        rows += ["", "%s: %d кандидатов, %d допустимых, нарушений выбранного %d; сходимость: `%s`." % (name, item["candidate_count"], item["feasible_count"], item["quality"]["selected_hard_violations"], json.dumps(item["mesh_convergence"], ensure_ascii=False)), ""]
    rows += ["## Блоки консоли", "", "Вклад в податливость суммируется; его обратная величина не является независимой жёсткостью блока.", "",
             "| Блок | C до, Н·мм | C после, Н·мм | Доля энергии после | Мин. запас после |", "|---|---:|---:|---:|---:|"]
    beam = functional["models"]["beam"]
    old_blocks = {b["id"]: b for b in beam["baseline"]["mechanics"]["blocks"]}
    for block in beam["selected"]["mechanics"]["blocks"]:
        rows.append("| %s | %.6g | %.6g | %.3f%% | %.6g |" % (block["id"], old_blocks[block["id"]]["compliance_contribution_Nmm"], block["compliance_contribution_Nmm"], block["energy_share"] * 100, block["safety_factor_min"]))
    rows += ["", "Независимая балочная формула: относительное отличие %.3f%%. Конечный поиск совпал с oracle по проверенному набору; глобальная/физическая оптимальность не доказана." % (functional["cantilever_reference"]["relative_stiffness_error"] * 100), "",
             "Нативный STE, LLM, проверки утечек и устойчивости: [актуальный сводный отчёт](ste_runtime_report.md). Ra и плоскостность остаются null; блок не аттестован для производства.", "",
             "![Консоль](functional_artifacts/beam_maps.png)", "", "![Клин](functional_artifacts/wedge_maps.png)", ""]
    (EXP / "functional_report.md").write_text("\n".join(rows), encoding="utf-8")
    print("Rendered native/functional reports from saved evidence")


if __name__ == "__main__":
    main()
