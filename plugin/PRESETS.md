# Native-пресеты STE как база плана

Задание: [presets_creation.md](../presets_creation.md). Реализация: `ste_planner/presets.py`,
слои контейнеров в `settings_schema.SettingResolver`, выбор в `planner.plan_case`,
снимок каталога и применение в `runtime_adapter.py`, передача через
`AIAssistant/agent/core_bridge.py` и `core_worker.py`.

## Что изменилось

Раньше runtime подставлял синтетический `Planner process profile` вместо quality,
обнулял variant/definition_changes и закреплял все `PROCESS_KEYS`. Теперь при наличии
каталога ядро выбирает совместимую базу P из установленного STE и пишет в user-контейнер
только минимальный Δ:

```text
B = Resolve(P)        baseline_effective_settings
E = Resolve(P, Δ)     effective_settings
Δ = setting_patch     только обоснованные, независимые, доступные изменения
```

Порядок слоёв повторяет `_ContainerIndexes` STE: user → quality_changes → quality →
material → variant → definition_changes → definition, для глобального стека и каждого
экструдера. Значение `"=выражение"` в контейнере вычисляется как SettingFunction; строковый
литерал приводится к типу определения (`SettingDefinition.settingValueFromString`).
Любое значение в глобальном instance-контейнере отключает `resolve`
(`GlobalStack._shouldResolve` по состоянию `InstanceState.User`). Правила взяты из
открытых исходников STE-Slicer/Uranium; совпадение с установленным 7.10.12 подтверждает
только readback после применения.

## Каталог

`runtime_adapter.build_preset_catalog(app)` в Qt-потоке читает через публичные менеджеры
активный принтер, экструдеры, установленные variant, доступные материалы
(`getAvailableMaterialsForMachineExtruder`), quality и quality_changes. Активный проект
не меняется. Каждый контейнер сериализуется с id, метаданными совместимости, значениями
и SHA-256. Снимок (`ste_preset_catalog/1`) передаётся worker в JSON вместе с запросом;
ключи API, cookies и файлы проекта в него не входят. Хеш каталога входит в подпись проекта:
смена профиля, сопла, материала или содержимого контейнера делает план устаревшим.

`presets.catalog_from_resources` строит упрощённый офлайн-каталог из папки `resources`
(только поставляемые профили, без пользовательских и машинно-специфичных материалов)
для CLI и тестов. Это предпросмотр, а не замена снимку из STE.

## Выбор базы

1. Совместимость: тот же `definition_id`; включённый экструдер; установленное сопло не
   меньше требуемого материалом (иначе `needs_input`, аппаратная конфигурация молча не
   меняется); материал по id файла и GUID, а не по отображаемому имени; совпадение
   диаметра филамента; группа quality доступна для сопла/материала каждого включённого
   экструдера (порт `QualityManager.getQualityGroups`); тот же `setting_version`; профиль
   не фиксирует другой `printing_mode`.
2. Обязательные требования: Δ плана поверх кандидата проходит проверку схемы (типы,
   границы, enabled). Кандидат с ошибками отклоняется с причиной.
3. Предпочтение: меньше изменений Δ → `preferred_quality_type` машины → штатный профиль
   раньше пользовательского → `quality_type`/имя. Текущий профиль GUI не является критерием.

Пользовательский quality_changes рассматривается только при явном разрешении в
`allowed_quality_changes`. Если совместимой базы нет, решение получает `needs_input`
с причиной и вариантами, а patch — ошибку, блокирующую применение. Самодельный профиль
в этом случае остаётся только предпросмотром анализа.

## Минимальный Δ

Позиция patch остаётся в Δ, если она обоснована и отличается от B:

| Обоснование | Источник |
|---|---|
| `requirement` | есть `reason_requirement_ids` из ТЗ |
| `explicit` | `printing_mode` (переключение backend) и `material_bed_temperature` (STE берёт max по экструдерам) — пишутся даже при совпадении |
| `constraint` | маршрут/5D, поддержки, окно паспорта материала, функциональный/калиброванный профиль |
| `load_class:<класс>` | policy-семена, оправданные классом нагрузки из ТЗ (structural/elastic: слой, стенки, кожа, заполнение; decorative: слой) |
| `policy` | общее policy-значение — не пишется, наследуется значение базы |

Вычисляемые поля (`wall_thickness` и т. п.) не записываются. `setting_patch.changes`
содержит key, scope, extruder, base/requested/effective value, единицу, причину,
требования и validation; `setting_patch.inherited_items` — отброшенные позиции и причину;
`setting_provenance` разделяет наследованные, изменённые и зависимые вычисленные значения.

## Контракт решения

| Поле | Содержание |
|---|---|
| `base_presets` | контракт `native_preset_base/1`, хеш каталога, версия STE и схемы, ID/имена/хеши machine/extruder/variant/material/quality/quality_changes/definition_changes по стекам |
| `preset_selection` | статус, выбранный профиль, критерий, альтернативы с числом изменений, причины отказов |
| `baseline_effective_settings` | Resolve(P) до Δ |
| `setting_patch`, `effective_settings` | минимальный Δ и Resolve(P, Δ) |
| `calibration_context.preset_base` | хеши контейнеров P для контроля переносимости DML |

Без каталога (CLI, старые вызовы) решение прежнее и помечено
`preset_selection.status = "not_requested"`, `base_presets = null`. Сохранённые ранее
решения без этих полей применяются старым путём и показываются как
`legacy_without_provenance`; native-пресетом они не считаются.

## Применение

`create_operation` находит каждый контейнер P по id, сверяет хеш содержимого (иначе
«stale plan») и в одной операции Undo ставит реальные контейнеры в индексы 1–5 и Δ
в user. `PROCESS_KEYS` поверх базы не закрепляются. Ошибка → rollback исходных
контейнеров и сцены. `readback` сверяет фактические значения с E и id контейнеров
стеков с `base_presets`; асинхронная замена quality со стороны STE даёт отказ проверки.
Требование пустого проекта сохраняется.

### Открытый проект

`create_settings_operation(app, decision, allow_unverified_5d=False)` ставит P и Δ в стеки проекта,
который уже открыт, не загружая STL и не трогая объекты сцены. `settings_preflight` повторяет
проверки `preflight` для базы (контракт, машина, `same_base` с `calibration_context`, ошибки patch,
согласованность payload с `effective_settings`, `infeasible`), а проверки источника и transform
к этому пути не относятся. Каждый контейнер P ищется по id со сверкой хеша (`native_container`),
`readback` обязан пройти, иначе контейнеры возвращаются. Используется оболочкой в
`apply_plan_to_project`; полный `apply_plan` (с ориентацией) по-прежнему предпочтителен, когда
в проекте одна модель из исходного STL.

`allow_unverified_5d` (и в `preflight`/`create_operation`) снимает блокировку 5D только по явному
согласию пользователя; по умолчанию она сохраняется.

## DML

Если план построен на базе P, эксперимент обязан указать тот же
`context.preset_base` (хеши контейнеров). Без него — `needs_input`, с другим — `out_of_domain`.

## Настройки оболочки

В `local_config.json` плагина (не из чата и не через модель):

```json
{"native_presets": true, "allowed_quality_changes": ["Имя профиля"]}
```

`native_presets: false` возвращает прежний `Planner process profile`; ошибка чтения каталога
при включённом режиме останавливает расчёт с сообщением, а не подменяется самодельным профилем.

## Проверки и ограничения

`tests/test_presets.py`: порядок слоёв и `resolve`, доступность групп quality, сопоставление
материала по id/GUID, неоднозначность, смена сопла/машины/диаметра, второй экструдер,
разрешённые пользовательские профили, конфликт режима и `setting_version`, минимальный patch и
происхождение значений, подмена/устаревание контейнеров, контекст DML, отказ runtime и сверка
стеков. Синтетическая машина проверяет только программные контракты. Интеграционные тесты
используют ресурсы STE (`hte530v5.2.4`, иначе открытый `hte530v5.2`): выбор детерминирован,
наследованные поля равны базе, шестерня остаётся `infeasible`.

`tools/uranium_preset_crosscheck.py` сверяет Resolve(P, Δ) с настоящим Uranium: стеки
`ContainerStack` с правилами GlobalStack/ExtruderStack, quality из файлов `.inst.cfg` через
`InstanceContainer.deserialize`, остальные слои через `InstanceContainer.setProperty`:

```bash
STE_RESOURCES_DIR=<resources> PYTHONPATH="plugin;plugin/tools;<Uranium-steslicer>" python plugin/tools/uranium_preset_crosscheck.py report.json [machine]
```

На открытых ресурсах `hte530v5.2` (Python 3.13, PyQt5 5.15.11): 126 сценариев (3 материала
× 7 групп quality × 3 режима × с синтетическими variant/quality_changes/definition_changes и
без них), 80 136 значений, 0 расхождений.

Первый прогон нашёл одно расхождение, существовавшее и в `main`: `raft_surface_layers` в 5D
(int-формула `(3.5 − base − interface) / surface` даёт 29.9). Uranium не округляет int: результат
формулы возвращается как есть, строка из контейнера читается через `ast.literal_eval`. STE
отправляет движку `str(value)`, а CuraEngine Stereotech читает `get<size_t>` через `std::stoul`,
то есть печатает 29 слоёв; `BuildVolume` при этом считает высоту рафта с 29.9. Резолвер раньше
округлял до 30 — не совпадало ни с readback, ни с движком. Теперь `coerce` не округляет дробные
int (целые по-прежнему `int`), поэтому прогноз совпадает с `getProperty` в STE. Других дробных
int-формул в определениях Stereotech открытого репозитория нет.
Для `hte530v5.2.4` сверку нужно повторить на ресурсах установленного STE.

Не проверено в этой работе: настоящий STE 7.10.12 (снимок реестра, применение реальных
контейнеров, G-code/3MF, Undo/Redo, повторная нарезка). Сравнение штатного пресета и базы
из pipeline в изолированном профиле STE остаётся обязательным шагом перед использованием.
