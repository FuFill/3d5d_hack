# Ядро планировщика печати для STE Slicer (`ste_planner`)

Функциональная ориентация, объёмная статика, карты по блокам и LLM-вход:
[FUNCTIONAL.md](FUNCTIONAL.md). Численные демонстрации не являются испытаниями отпечатков.

Детерминированное расчётное ядро по спецификации [02_requirement_to_slicer_system.md](../data/research/02_requirement_to_slicer_system.md) и дополнению [03_constants_effects_and_alternatives.md](../data/research/03_constants_effects_and_alternatives.md). Цепочка: ТЗ → требования → геометрия → материал → режим 3D/5D → ориентация → решения процесса → проверенный patch настроек → верификация и план испытаний.

Совместимость: Python 3.5+ (рантайм STE Slicer 7.10: Python 3.5.7, numpy 1.15). Внешних зависимостей, кроме numpy, нет; scipy используется, если доступен.

Слой плагина (MCP-сервер, чат, применение patch в Qt-потоке, нарезка, экспорт 3MF/G-code) — следующий этап.

## Быстрый старт

```bash
python tools/plan_examples.py
```

Пишет `reports/case_N.json` (полный `Decision` по §1) и `reports/summary.md` по 7 примерам организаторов.

```python
from ste_planner import planner
decision = planner.plan_case_files("ТЗ.docx", "model.stl")
decision["status"], decision["setting_patch"]["payload"], decision["scene_operations"]
```

Каталог ресурсов слайсера ищется в `STE_RESOURCES_DIR`, затем в `../STE-Slicer/STE-Slicer-master/resources`, затем в `C:\Program Files\Stereotech STE Slicer 7.10\resources`.

## Модули и функции спецификации

| Модуль | Функции §11 | Что реализовано |
|---|---|---|
| `result` | общий контракт | `Result{value, unit, status, evidence, uncertainty, domain, trace}`, трёхзначная логика (unknown ≠ pass), статусы решения |
| `units` | normalize_requirements | единицы mm/N/N·mm/MPa/g·mm⁻³/rad·s⁻¹, русские обозначения единиц |
| `requirements` | parse/normalize/validate_requirements, required/missing_inputs | извлечение чисел с цитатой источника; нагрузки peak/nominal; слияние с LLM: числа, которых нет в цитате, получают статус assumed |
| `geometry` | analyze_mesh, orientation_candidates, evaluate_orientation | STL bin/ascii, замкнутость, объём/площадь/инерция, ось вращения (инерция + секторы + соосные цилиндры), отверстие, зубья (гармоника), нависания, ступенчатость, хорды |
| `materials` | inspect_material_catalog, screen_materials, printable, choose_thermal_window | каталог из вики Stereotech с источниками и конфликтами; химстойкость; памятка Stereotech по выбору материала; жёсткий провал никогда не компенсируется рейтингом |
| `mechanics` | solve_structure (аналитика), evaluate_lifetime, solve_elastomer, evaluate_contact_wear | формулы §6: Ft, изгиб, кручение, Льюис, Эйлер, кольцевые напряжения, дисбаланс/ISO 1940, циклы, Майнер, FI, ячейки, Archard, Neo-Hookean; без коэффициентов — unknown |
| `structure` | solve_structure, build_load_cases | скрининг по границам: зуб по Льюису, кручение вала, обод ротора, класс балансировки |
| `process` | choose_layer_schedule, compute_flow_limit, compute_speed_schedule, allocate_tolerance, compensate_dimensions | стенки/кожа, подача, пределы скорости, движение, бюджет допуска, компенсация и знак offset |
| `routes` | enumerate_routes, plan_5d | семантика 5D Spiral/Full из документации STE; диаметр основания = отверстию (Spiral) или сердечник внутри детали (Full) |
| `process_plan` | choose_nozzle, support_plan, adhesion/cooling (из профиля), pack_batch | решения с обоснованием, политики P явно перечислены в `POLICY` |
| `settings_schema` | resolve_settings | схема машины и резолвер, повторяющий стеки STE/Uranium; безопасный AST-вычислитель выражений |
| `compiler` | compile_patch | тип, диапазоны, enabled, scope экструдера, проценты только на границе, whitelist режимов, запрет зависимых пар |
| `gcode` | inspect_toolpath | слои, пустые слои, температуры, E-режим, ось A/C, линейная объёмная подача |
| `optimize`, `surrogate` | optimize_candidates, domain_check, fit_response_model | Парето после отбора допустимых; RSM, GP и cross-fitted Double ML PLR, кластерная неопределённость, ограничение μ+κσ≤0, constrained EI |
| `calibration` | применение измеренных экспериментов | Проверка контекста и единиц, эффекты DML и отдельные patch-кандидаты для предложенных изменений |
| `verification` | verify_requirements, build_qualification_plan, explain_decision | статусы infeasible > needs_input > needs_calibration > candidate |
| `planner` | основной алгоритм §13 | сборка `Decision` |

Не реализованы в ядре (требуют рантайма слайсера или внешних данных): apply_patch_transaction, dry_slice, export_project (следующий этап, плагин), check_kinematics и check_collisions (нет модели головки и оснастки — статус unknown), FEA, seam_plan, адаптивный слой.

## Double ML в планировщике

`surrogate.fit_double_ml(x, d, y, groups=print_job_ids)` реализует PLR partialling-out:
две квадратичные регрессии из существующего RSM-базиса обучаются вне проверяемых групп,
затем оценивается связь остатков. Все образцы одной печати остаются в одном fold.
Стандартная ошибка рассчитывается по независимым печатям, с поправкой G/(G−1).
95% интервал — асимптотический интервал эффекта, не интервал прочности отдельной детали.
Рантайму нужны только Python 3.5 и NumPy; sklearn и пакет DoubleML не импортируются.

Вход `calibration_data` — список JSON-совместимых экспериментов со следующими полями:

| Поле | Содержание |
|---|---|
| `dataset_id`, `source` | Уникальное имя набора; `source` строго `physical_test` |
| `design` | Протокол назначения настройки, исходных контролей и измерения результата |
| `context` | `machine_id`, `material_id`, `printing_mode`, `geometry_sha256`, `rotation` — из `decision["calibration_context"]` |
| `context.fixed_settings` | Непустой словарь остальных зафиксированных настроек, например высота слоя, сопло, температуры и скорости; контролируемые условия эксперимента |
| `treatment` | Ключ изменяемой числовой настройки STE, например `infill_sparse_density` |
| `covariates` | Список ключей остальных наблюдаемых числовых настроек до вмешательства, например `wall_line_count`; пустой список допустим для контролируемого опыта |
| `units` | Единицы всех ключей treatment/covariates из схемы: `%`, `mm`, `count` для целочисленных счётчиков, `1` для остальных безразмерных значений |
| `outcome` | `{name, unit}` измеренного результата, например `{name: "breaking_load", unit: "N"}` |
| `observations` | Список `{sample_id, print_job_id, settings: {ключ: значение}, outcome: измеренное_число}`; settings содержит treatment и все covariates |
| `n_folds`, `seed` | Необязательно; по умолчанию 5 и 0 |

Количество независимых печатей должно позволять разбиение и обучение вспомогательных моделей.
Повторы образцов с одного стола не увеличивают число независимых групп.
Заполнения задаются в **нативных процентах STE**: 80 означает 80%, изменение 80 → 60 — минус 20 процентных пунктов.
Доли 0,8 из физического компилятора сюда не передаются. Настройки, объявленные фиксированными,
не могут одновременно быть treatment/covariates.

```python
import json
from ste_planner import planner

with open("measured_experiments.json", encoding="utf-8") as f:
    experiments = json.load(f)
decision = planner.plan_case_files(
    "task.docx", "part.stl", calibration_data=experiments,
    setting_changes={"infill_sparse_density": 60.0})
analysis = decision["causal_analysis"]
```

Те же данные принимаются существующим CLI (из папки `plugin`):

```bash
python tools/plan_examples.py ../data/examples reports --calibration measured_experiments.json --setting-change infill_sparse_density=60
```

Старые позиционные аргументы CLI сохранены. Наборы для других материалов/деталей отклоняются
по контексту; наличие такого набора в общей базе не мешает использованию подходящего эксперимента.

Сначала обычный вызов `plan_case_files` без калибровки возвращает `calibration_context` с контекстом
и снимком настроек. На этой основе составляется протокол; `fixed_settings` перечисляет действительно
зафиксированные независимые настройки, исключая treatment, covariates и меняющиеся производные поля.
После физических испытаний передаются их наблюдения. Само совпадение machine_id означает совпадение
профиля машины, а не аттестацию экземпляра принтера или партии материала: это проверяет протокол опыта.

В `causal_analysis.value` для каждого набора возвращаются fit, диагностические метрики, хеш исходных
данных, эффект предложенного изменения и отдельный `proposed_patch`. Патч проходит существующий
компилятор и проверку сохранения фиксированных настроек/ковариат; он не применяется автоматически.
Несколько запрошенных изменений оцениваются **по одному относительно исходного плана**;
их эффекты не складываются и не описывают совместное вмешательство.

Без измерений возвращается `needs_calibration`. Синтетика, пропущенные результаты, несовпадение
материала/режима/геометрии/ориентации, неверные единицы и выход за наблюдаемые диапазоны не дают
применимой оценки. Статусы причин объясняются в `trace` и `explanation`. Оценка разницы не содержит
абсолютного исходного значения прочности и не меняет `verification`, `qualification_plan` или основной patch.
Квадратичный базис может быть недостаточным; его ошибки и ненаблюдаемое смешение DML автоматически
не устраняет. Проверка диапазонов необходима, но не доказывает условное перекрытие данных.

Численная сверка с официальной DoubleML выполняется отдельно, в современном окружении:

```bash
python -m pip install -r experiments/requirements.lock.txt
python experiments/check_planner_double_ml.py
```

Это команды из корня репозитория. Пакет DoubleML устанавливается только в отдельное окружение проверки.
Сверяются коэффициент и стандартная ошибка при одинаковых разбиениях, для независимых наблюдений
и для сбалансированных групп повторных образцов. Результат — `experiments/planner_double_ml_crosscheck.json`.
Грамматика добавленного кода проверяется как Python 3.5; запуск встроенного Windows Python 3.5
не входит в проверку на macOS.

## Как проверяется корректность

1. **Тесты формул** (`tests/test_core_math.py`): три численных примера §16 (88,89 и 84,66 mm/s; A=0,081416 mm²; 4 стенки = 1,77 mm; ±0,16 при допуске ±0,10 непригодно) и свойства §17: монотонность предела скорости, знак компенсации, unknown не становится pass, единицы N·m и проценты.
2. **Геометрия** (`tests/test_geometry.py`): точные объёмы аналитических сеток, топологические ошибки. Объём и площадь всех 7 STL сверяются с trimesh как с независимым оракулом.
3. **Разметка 7 ТЗ** (`tests/test_requirements.py`): ожидаемые значения требований с цитатами.
4. **Материалы** (`tests/test_materials.py`): конфликты источников дают консервативный интервал; химстойкость при комнатной температуре не принимается при +85 °C; для Wilo допустим только PP G30.
5. **Настройки против настоящего Uranium.** `tools/uranium_crosscheck.py` строит эталонные стеки на классах Uranium из репозитория STE с правилами GlobalStack/ExtruderStack STE и сравнивает все 660 ключей в 9 сценариях: **5940 значений, 0 расхождений**.
6. **Интеграция** (`tests/test_examples.py`): режимы совпадают с метками папок 3D/5D, patch без ошибок, честные статусы (шестерня: момент недостижим по нижней оценке напряжения), детерминизм.
7. **Python 3.5.** `tools/run_py35_tests.py` запускает весь набор во встроенном интерпретаторе дистрибутива STE (через `python35.dll`). Дополнительно проверка `vermin -t=3.5-`.

Команды:

```bash
python -m pytest tests -q
```

```bash
python tools/run_py35_tests.py
```

```bash
RUN_URANIUM_CHECK=1 URANIUM_PYTHON=../.venv38/Scripts/python.exe python -m pytest tests/test_settings.py -q
```

Для сверки с Uranium нужен Python ≤ 3.9 с PyQt5, scipy, trimesh: старый Uranium несовместим с Python 3.10+. Встроенный PyQt5 дистрибутива во внешнем процессе не инициализируется.

## Каталог материалов

`ste_planner/data/materials_stereotech.json` собирается из официальной вики Stereotech и файлов `.fdm_material` слайсера:

```bash
python tools/crawl_stereotech_wiki.py <cache_dir>
```

```bash
python tools/build_material_catalog.py <cache_dir>/wiki_pages.json ../STE-Slicer/STE-Slicer-master/resources/materials ste_planner/data/materials_stereotech.json
```

Расхождения между страницей материала и общей таблицей сохраняются рядом (14 конфликтов), без усреднения. Памятка Stereotech по выбору материала перенесена в `data/material_selection_guide.json`. Записи коллеги (Polymaker/BASF) используются как справочные.

## Известные ограничения

- Свойства материалов — типовые значения производителя, не партия; квалификация изделия требует испытаний из `qualification_plan`.
- Объёмная подача Q не откалибрована: скорости берутся из профиля и паспорта и помечены needs_calibration.
- Выбор режима 5D — явная политика (документированные преимущества ≥ 2), её можно переопределить параметром `forced_mode`.
- Модель 564 тыс. граней (фигурка) обрабатывается около 30 с; в плагине нужна децимация для ориентации.
