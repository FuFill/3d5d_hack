# STE: техническое задание → план печати → слайсер

Актуально на 24 октября 2026 года. Основная ветка — `main`.

Система принимает исходный STL и текст ТЗ. Qwen/Timeweb вызывает детерминированное
ядро, которое выбирает материал, режим и ориентацию, формирует настройки,
проверяет ограничения и объясняет результат. Оболочка Lenaz применяет допустимый
план через штатный API STE одной операцией Undo, сверяет фактические значения,
запускает нарезку и сохраняет G-code/3MF. Математика ядра при интеграции оболочки
и исправлении путей к моделям не изменялась.

## Запуск на этом компьютере

Плагин установлен в `%APPDATA%\steslicer\7.10\plugins\AIAssistant`.

1. Полностью перезапустите STE Slicer 7.10.12.
2. Выберите **Stereotech Hybrid 530 V5.2.4** и создайте пустой проект.
3. Откройте **Расширения → AI Assistant → Open chat**.
4. В **⋯ → Настройки** проверьте URL `https://api.timeweb.ai/v1`, модель
   `dashscope/qwen3.8-flash` и Python ядра:
   `C:\Users\Ilya\Working projects\3d5d_hack\.venv-functional\Scripts\python.exe`.
   Ключ читается из локального `.env`; кнопка **Проверить** проверяет соединение.
5. Прикрепите STL скрепкой в чате, вставьте полный текст ТЗ сообщением и отправьте.

Оболочка передаёт загруженный файл по `attachment_id`. Если модель уже находится
на столе, `list_model_sources` возвращает её `object_id` и реальный исходный путь;
по этому ID можно рассчитать предложение из оригинального STL. Текущие повороты
и масштаб объекта на столе в такой расчёт не подставляются. Для применения
по-прежнему нужен пустой проект и новый расчёт после изменения сцены.

## Установка из репозитория

Используйте современный Python; зависимости worker зафиксированы в
`experiments/functional_requirements.lock.txt`. Во встроенный Python 3.5 STE
новые зависимости не устанавливаются.

```powershell
.\.venv-functional\Scripts\python.exe plugin/tools/install_ai_assistant.py --python "C:\Users\Ilya\Working projects\3d5d_hack\.venv-functional\Scripts\python.exe"
```

Установщик создаёт сборку с точной копией `plugin/ste_planner`, резервирует прежний
плагин в `.runtime`, подключает локальные пути к Python и `.env`. После установки
нужен перезапуск STE. Для другого профиля используйте `--profile`; для другого
окружения — соответствующий абсолютный путь `--python`.

## Поток данных и ограничения

`list_model_sources → plan_print → apply_plan → verify_plan → slice → export_gcode → save_project`.

- `plan_print` выбирает ровно один источник: `attachment_id`, `object_id` либо
  существующий абсолютный `mesh_path`. Относительное имя и угаданный путь не принимаются.
- Worker работает в современном Python; изменение сцены и readback — в Qt.
  Отмена, изменение проекта/исходного SHA и устаревший plan_id блокируют применение.
- DoubleML оценивает эффекты только при пригодных данных и совпадении контекста;
  без измерений сохраняются `needs_input` / `needs_calibration`.
- `infeasible` не применяется. Обычное 5D-применение блокируется до проверки
  кинематики/коллизий; изолированный исследовательский 5D не доступен чату.
- Полный тираж фигурки из примера 4 не помещается с резервами адаптера;
  количество автоматически не уменьшается.
- Успешная нарезка не подтверждает физический оптимум, прочность или стабильность:
  `physical_print_approval=false`. Native FE-поиск остаётся отдельным API/CLI.

Сейчас ядро использует defaults определения принтера и данные профиля материала,
а runtime формирует `Planner process profile`. Автоматический выбор полной
совместимой базы native-пресетов printer/variant/material/quality и минимального
patch поверх неё — **будущая задача**, только описанная в
[presets_creation.md](presets_creation.md). В этой работе она не реализована.

## Проверки

Последний полный прогон ядра: **186 pass, 1 skip**. После исправления источников
моделей: **24 pass** для оболочки. В настоящем STE проверены загрузка STL через
HTTP, выбор по attachment/object ID, worker, apply/readback, CuraEngine,
G-code/3MF, единый Undo/Redo и соединение с Timeweb.

```powershell
python -m pytest plugin/tests -q
python -m pytest plugin/shell_tests -q
python plugin/tools/check_ai_assistant_runtime.py
```

Сводка и ограничения доказательности: [отчёт оболочки](experiments/shell_integration/REPORT.md).
Ключи, пользовательские профили, временные сессии и окружения в Git не публикуются.

## Документация

| Тема | Документ |
|---|---|
| Установка, UI, настройка и ошибки путей | [AIAssistant/README.md](AIAssistant/README.md) |
| Ядро, схема решений и тесты | [plugin/README.md](plugin/README.md) |
| Runtime, Qt, readback и Undo | [plugin/RUNTIME.md](plugin/RUNTIME.md) |
| Qwen и причинный DoubleML | [plugin/QWEN_INTEGRATION.md](plugin/QWEN_INTEGRATION.md) |
| Функциональные нагрузки и FE | [plugin/FUNCTIONAL.md](plugin/FUNCTIONAL.md) |
| Физика фактических траекторий | [plugin/TOOLPATH_PHYSICS.md](plugin/TOOLPATH_PHYSICS.md) |
| Границы интеграции и незавершённые исследования | [plugin/SHELL_INTEGRATION_HANDOFF.md](plugin/SHELL_INTEGRATION_HANDOFF.md) |
| Данные, источники, формулы и исследования | [data/README.md](data/README.md) |
| Будущее использование пресетов: промпт, без реализации | [presets_creation.md](presets_creation.md) |

Исследовательские тексты и ранние отчёты сохраняют исходные даты, числа и
условия экспериментов; текущий статус запуска и API приведён здесь.
