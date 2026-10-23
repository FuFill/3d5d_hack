# Бэйзлайн модели плагина AI Assistant

Детерминированная модель «ТЗ + STL → решение по печати» для STE Slicer и Stereotech Hybrid 530 V5.2. Реализует ядро [спецификации](../../../../data/research/02_requirement_to_slicer_system.md) в объёме, который можно честно посчитать без калибровок: разбор требований, скрининг материалов, анализ геометрии, выбор режима 3D/5D, ориентации, профиля и патча настроек с проверкой по схеме STE.

Назначение — отправная точка для LLM-агента. Агент вызывает бэйзлайн как MCP-инструмент, показывает пользователю решение и обоснование, а затем применяет патч через API слайсера. Бэйзлайн не меняет сцену и не нарезает модель.

## Конвейер

| Шаг | Модуль | Что делает | Класс зависимостей |
|---|---|---|---|
| 1 | `requirements.py` | Регулярные правила превращают ТЗ в требования `{property, comparator, value, unit, mandatory, provenance, interpretation_status}`, числа нагрузок и флаги. | LLM не нужна; пороги для качественных слов — политика P |
| 2 | `geometry.py` | STL → габарит, объём, площадь, сквозное отверстие, ось вращения (подгонка прямых нормалей), профиль радиусов, метрики нависаний по ориентациям. | G |
| 3 | `materials.py` | Трёхзначный скрининг 22 материалов Stereotech: интервал против порога даёт pass/fail/unknown. Ранжирование сначала по нарушенным обязательным требованиям. | E/P |
| 4 | `decision.py` → `choose_route` | `classic` / `cylindrical` (5D Spiral) / `cylindrical_full` (5D Full) по подсказке режима и геометрии. | S/P |
| 5 | `decision.py` → `choose_orientation` | Для деталей с осью ось ставится вертикально; иначе выбирается min J = 2·нависания + 0.3·высота + w·ступеньки + 0.6·(1−опора). | G/P |
| 6 | `decision.py` → `build_settings` | Слой, стенки `N = 1 + ceil((t − w0)/wi)`, кожа, заполнение, скорости, поддержки, адгезия, шов, параметры 5D. | A/P |
| 7 | `decision.py` → `compile_patch` | Проверяет каждый ключ по схеме STE: наличие, тип, enum, литеральные min/max, scope (глобальный или экструдер). | S |
| 8 | `explain.py` | Объяснение решения в Markdown, собранное только из `Decision`, без новых фактов. | — |

## Правила выбора режима 5D

Правила основаны на исходниках STE Slicer (`BuildVolume._updateCuttingCylinder`, `GenerateBasementJob`):

- **`cylindrical_full`**: у детали есть сквозное осевое отверстие Ø ≥ 10 мм. Деталь целиком печатается радиальными слоями на оправке. `cylindrical_mode_base_diameter` равен Ø отверстия, `non_printing_base_diameter` = Ø − 2·0.3·5 (helix-raft из 5 слоёв).
- **`cylindrical`** (5D Spiral): отверстия нет или оно меньше 10 мм. Ядро внутри `cylindrical_mode_base_diameter` (наименьший наружный диаметр вдоль оси) печатается классически, всё снаружи — радиально.
- Режим 5D выбирается, только если он указан в задании («5D» в тексте или в имени папки) и у детали найдена ось вращения. Без подсказки бэйзлайн выбирает `classic`, а 5D предлагает как альтернативу.
- В 5D ось детали совмещается с осью вращения (X = 0, Y = 0, центр стола в нуле).

## Результаты на примерах организаторов

Полная таблица и отчёты по каждому кейсу лежат в [`results/baseline/SUMMARY.md`](../../../../results/baseline/SUMMARY.md). Эталонные 3MF жюри недоступны, поэтому это не оценка точности, а воспроизводимая точка отсчёта для следующих версий.

| Кейс | Режим | Материал | Слой | Стенки / заполнение |
|---|---|---|---:|---|
| Шестерня | classic | Fiberpart ABSPA G8 | 0.1 | 5 / 100 % |
| Колесо конвейера | cylindrical_full, оправка Ø12 → Ø15.9 | Enduse TPU D70 | 0.1 | 5 / 100 % |
| Пыльник ШРУСа | classic | Sealant TPV80 | 0.2 | 5 / 100 % |
| Фигурка ×15 | classic, адаптивный слой | Fiberpart ABSPA G8 | 0.1 | 4 / 10 % |
| Колесо насоса Wilo | cylindrical, ядро Ø28 | Fiberpart PP G30 | 0.15 | 5 / 100 % |
| Крыльчатка | classic | Enduse PA6 | 0.2 | 5 / 100 % |
| Ось Krones | cylindrical, ядро Ø17 | Enduse PA6 | 0.1 | 6 / 100 % |

## Использование

```python
import sys
sys.path.insert(0, "plugin/AIAssistant/agent")
from baseline import recommend, explain_decision, call_tool, openai_tools

decision = recommend(tz_text, model_path="part.stl", hint="5D")
print(explain_decision(decision))

# MCP tools/call и описание tools для Chat Completions:
result = call_tool("baseline_recommend_setup", {"model_path": "part.stl", "documents": ["tz.docx"]})
tools = openai_tools()
```

Инструменты: `baseline_analyze_model`, `baseline_parse_requirements`, `baseline_recommend_setup`, `baseline_list_materials`.

Как применять `Decision` в плагине (главный Qt-поток, `app.callLater`):

1. `printing_mode` и параметры 5D записать первыми: от них зависят габарит стола, начало координат и бэкенд.
2. Материал выбрать по `chosen_candidate.material.guid` или `id`, качество — по `chosen_candidate.quality.quality_type`.
3. Записать `setting_patch` (`scope`: `global` → `userChanges` глобального стека, `extruder` → активный экструдер) и перечитать эффективные значения.
4. Выполнить `scene_operations`: поворот задан в системе STL/принтера с осью Z вверх; в сцене Cura ось Y вверх.
5. Нарезать модель. `verification.slice_verified` становится `true` только после проверки G-code.

## Пересборка базы знаний и тесты

```bash
python scripts/build_knowledge.py
python scripts/run_baseline.py
python -m unittest discover -s tests -v
```

`build_knowledge.py` собирает `data/settings_index.json` из `data/research/fdmprinter.public.def.json`, `data/materials_ste.json` из профилей материалов открытого STE Slicer и `data/machine_hybrid530.json` из определений `hte530v5.2`. Тесты проверяют синтаксис Python 3.5 (AST), единицы и проценты, то, что unknown не превращается в pass, то, что жёсткое нарушение нельзя скрыть баллом, проверку схемы, детерминизм, выбор 5D и все 7 примеров.

## Ограничения (честно)

- Свойства в `materials_ste.json.typical` — типичные интервалы класса полимера, а не паспорт SKU Stereotech. Поэтому статус решения не выше `candidate` / `needs_calibration`, а `qualified` бэйзлайн не выдаёт никогда.
- Ресурс, ползучесть, герметичность, трение, пыление, ESD и балансировка всегда `unknown`: они попадают в `qualification_plan`.
- Схема и профили взяты из публичной ветки `develop`, а не из выданного дистрибутива 7.x. Ключи, профили качества и наличие оправок для `cylindrical_full` нужно перечитать в установленном слайсере.
- Пороги политики (`POLICY` в `decision.py` и `requirements.py`) — инженерные допущения. Их стоит подстраивать по эталонным проектам, как только они станут доступны.
- Совместимость с Python 3.5 проверена статически. В интерпретаторе STE Slicer код не запускался.
