# STE AI Assistant: техническое задание → план печати → STE Slicer

Плагин для STE Slicer 7.10 добавляет в окно слайсера чат с ИИ. Пользователь прикладывает
модель STL и техническое задание (DOCX/TXT/MD или текстом), а ассистент:

- рассчитывает план печати детерминированным ядром (`plugin/ste_planner`): материал, режим 3D/5D,
  ориентацию, штатный пресет STE и минимальные изменения поверх него;
- недостающие условия уточняет карточками с вариантами, а если материал не задан, предлагает
  сравнить 3 материала с оценкой времени и расхода;
- после одного согласия применяет план одной операцией Undo, проверяет фактические значения,
  нарезает, сохраняет G-code и проект 3MF;
- проверяет положение каждой детали на столе, а 5D-траектории — на нарушения перед экспортом;
- хранит дерево моделей чата: каждый результат — узел, клик возвращает слайсер и чат к нему.

ИИ (Qwen через Timeweb AI) только ведёт диалог и вызывает инструменты; решения принимает ядро.

## Быстрый старт (Windows)

Нужно:

- **STE Slicer 7.10.12**, принтер **Stereotech Hybrid 530 V5.2.4**;
- **Python 3.12 или новее** для расчётов ядра (проверено на 3.13; Python 3.5 внутри STE не меняется);
- **ключ Timeweb AI** (выдают организаторы).

Все команды — в PowerShell из корня репозитория:

```powershell
git clone https://github.com/FuFill/3d5d_hack.git
cd 3d5d_hack

# 1. Окружение для ядра
py -3.13 -m venv .venv-functional
.\.venv-functional\Scripts\python.exe -m pip install -r experiments/functional_requirements.lock.txt

# 2. Ключ ИИ: скопируйте шаблон и впишите ключ в LLM_API_KEY
Copy-Item .env.example .env
notepad .env

# 3. Установка плагина в STE (профиль %APPDATA%\steslicer\7.10)
.\.venv-functional\Scripts\python.exe plugin/tools/install_ai_assistant.py --python .\.venv-functional\Scripts\python.exe
```

Дальше:

1. Полностью перезапустите STE Slicer и выберите принтер Stereotech Hybrid 530 V5.2.4.
2. Откройте **Расширения → AI Assistant → Open chat** — чат появится панелью справа.
3. В чате **⋯ → Настройки → Проверить**: должно появиться «Связь с моделью есть». URL и модель по умолчанию:
   `https://api.timeweb.ai/v1`, `dashscope/qwen3.8-flash`.
4. Попробуйте: прикрепите скрепкой `data/examples/1. Шестерня - 3D/Шестерня.stl` и ТЗ из той же папки,
   напишите «Подготовь к печати».

G-code и проект сохраняются в `Документы\STE AI Assistant\<имя задания>\`.

**После `git pull`** снова запустите шаг 3 и перезапустите STE: установленный плагин — это копия,
перезагрузка окна чата не обновляет Python-код. Прежняя версия плагина сохраняется в `.runtime`.

Ключ можно ввести и прямо в настройках чата. Ни ключ, ни `.env`, ни локальные пути в Git не попадают.

## Проверки

```powershell
.\.venv-functional\Scripts\python.exe -m pytest plugin/tests plugin/shell_tests -q
```

Тесты берут файлы STE из `C:\Program Files\Stereotech STE Slicer 7.10\resources`; если слайсер стоит
в другом месте, задайте `$env:STE_RESOURCES_DIR = "<папка STE>\resources"`.
Проверка в настоящем STE (изолированный профиль, нарезка, экспорт, Undo/Redo):
`python plugin/tools/check_ai_assistant_runtime.py`.

## Как устроено

```
STE Slicer (Python 3.5, Qt)                       отдельный Python 3.12+
┌────────────────────────────────────────┐        ┌──────────────────────────────┐
│ AIAssistant: чат (web/), агент, MCP     │ ─────▶ │ core_worker → plugin/        │
│ инструменты: пресеты, сцена, нарезка    │ ◀───── │ ste_planner (ядро, FE, 5D)   │
└────────────────────────────────────────┘        └──────────────────────────────┘
        ▲ OpenAI-совместимый API (Timeweb AI, Qwen)
```

Обычный ход задачи: `list_model_sources → plan_print → (карточка согласия) → apply_plan →
verify_plan → slice → export_gcode → save_project`.

| Папка | Что внутри |
|---|---|
| `AIAssistant/` | плагин STE: чат, агент, инструменты, дерево моделей ([README](AIAssistant/README.md)) |
| `plugin/ste_planner/` | ядро: материалы, режим, ориентация, пресеты, FE и проверки 5D ([README](plugin/README.md)) |
| `plugin/STEPlannerRuntime/` | проверки внутри настоящего STE |
| `plugin/tools/` | установка, сборка, проверки и CLI ядра |
| `plugin/tests/`, `plugin/shell_tests/` | тесты ядра и плагина |
| `data/examples/` | 7 примеров организаторов (ТЗ, фото, STL) |
| `data/research/` | исследования, константы, схемы настроек ([README](data/README.md)) |
| `experiments/` | воспроизводимые эксперименты и отчёты с исходными данными |

## Ограничения

- План применяется к пустому проекту или к одной модели на столе; другие детали плагин не удаляет.
  Для нескольких моделей есть `apply_plan_to_project` — только пресет и настройки, детали не двигаются.
- 5D применяется только после явного согласия: кинематика станка до нарезки не проверена.
  После нарезки траектория проверяется, найденное нарушение блокирует экспорт.
- Без измерений и нагрузок ядро честно возвращает `needs_input` / `needs_calibration`, а не придумывает.
- Успешная нарезка не подтверждает прочность и физическую пригодность: `physical_print_approval=false`.

## Документация

| Тема | Документ |
|---|---|
| Плагин: установка, интерфейс, настройки, дерево моделей, MCP | [AIAssistant/README.md](AIAssistant/README.md) |
| Ядро, схема решений и тесты | [plugin/README.md](plugin/README.md) |
| Runtime STE: Qt, readback, Undo | [plugin/RUNTIME.md](plugin/RUNTIME.md) |
| Пресеты STE как база плана | [plugin/PRESETS.md](plugin/PRESETS.md), задание — [presets_creation.md](presets_creation.md) |
| Qwen и DoubleML | [plugin/QWEN_INTEGRATION.md](plugin/QWEN_INTEGRATION.md) |
| Нагрузки и FE | [plugin/FUNCTIONAL.md](plugin/FUNCTIONAL.md) |
| Физика траекторий | [plugin/TOOLPATH_PHYSICS.md](plugin/TOOLPATH_PHYSICS.md) |
| Проверка 5D-траекторий | [plugin/TRAJECTORY_VALIDATION.md](plugin/TRAJECTORY_VALIDATION.md) |
| Границы интеграции и открытые исследования | [plugin/SHELL_INTEGRATION_HANDOFF.md](plugin/SHELL_INTEGRATION_HANDOFF.md) |
| Данные, источники, формулы | [data/README.md](data/README.md) |

Отчёты в `experiments/` сохраняют даты и числа своих прогонов; актуальный запуск описан здесь.
