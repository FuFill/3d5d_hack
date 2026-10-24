# Решения планировщика по примерам организаторов

**Исторический отчёт, навигация обновлена 24.10.2026:** числа и выводы ниже
относятся к указанному эксперименту; они не заменяют текущую сводку
интеграции и не подтверждают физический оптимум.
Текущий запуск/API: [README](../../README.md); [проверка оболочки](../../experiments/shell_integration/REPORT.md).
Будущий выбор native-пресетов: [промпт без реализации](../../presets_creation.md).

Сгенерировано `tools/plan_examples.py`; полные данные — в JSON рядом.

| Кейс | Статус | Материал | Режим | Ключевые изменения |
|---|---|---|---|---|
| 1. Шестерня - 3D | infeasible | Fiberpart TPU C5 | classic | bottom_layers=6, infill_sparse_density=100.0, layer_height=0.2, top_layers=6, wall_line_count=4, material_bed_temperature=95.0, speed_print=40.0, support_enable=False |
| 2. Колесо конвейерной линии - 5D | needs_input | Sealant TPU A95 | cylindrical | bottom_layers=6, infill_sparse_density=100.0, layer_height=0.2, top_layers=6, wall_line_count=4, speed_print=30.0, support_enable=False, cylindrical_mode_base_diameter=15.99, non_printing_base_diameter=15.99 |
| 3. Пыльник Шруса - 3D | needs_input | Sealant TPU A95 | classic | bottom_layers=5, infill_sparse_density=100.0, layer_height=0.2, top_layers=5, wall_line_count=3, material_bed_temperature=45.0, speed_print=30.0, support_enable=True |
| 4. Фигурка | candidate | Fiberpart TPU C5 | classic | bottom_layers=10, infill_sparse_density=25.0, layer_height=0.1, top_layers=10, wall_line_count=3, material_bed_temperature=95.0, speed_print=40.0, support_enable=True |
| 5. Рабочее колесо насоса Wilo - 5D | needs_input | Fiberpart PP G30 | cylindrical | machine_nozzle_size=0.6, bottom_layers=4, infill_sparse_density=100.0, layer_height=0.3, top_layers=4, wall_line_count=3, speed_print=40.0, support_enable=False, cylindrical_mode_base_diameter=7.98, non_printing_base_diameter=7.98 |
| 6. Крыльчатка охлаждения электродвигателя - 3D | needs_input | Enduse PP | classic | bottom_layers=6, infill_sparse_density=100.0, layer_height=0.2, top_layers=6, wall_line_count=4, material_bed_temperature=78.0, material_print_temperature=240.0, speed_print=30.0, support_enable=True |
| 7. Ось вращения передающих звёзд формовщика коробов Krones - 5D | needs_calibration | Enduse PA6 | cylindrical_full | bottom_layers=12, infill_sparse_density=100.0, layer_height=0.1, top_layers=12, wall_line_count=4, speed_print=50.0, support_enable=False, cylindrical_mode_base_diameter=6.2, non_printing_base_diameter=6.0 |

## 1. Шестерня - 3D

- Статус: infeasible; уровень доказательности: analytical_screen.
- Материал: Fiberpart TPU C5 — выбран из 7 допустимых; отклонено 13 (обязательное требование не выполнено).
- Режим: classic. no documented 5D benefit above threshold; classic is simpler
-   printing_mode = classic (было classic): route selected by documented policy
-   bottom_layers = 6 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class structural
-   layer_height = 0.2 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 6 (было 8): skin_layers, policy:skin
-   wall_line_count = 4 (было 2): shell 1.60 mm -> 4 walls (1.60 mm)
-   material_bed_temperature = 95.0 (было 95.0): material profile value; explicit because STE takes max() over extruders
-   speed_print = 40.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = False (было False): overhang 150 mm2, support volume bound 8 mm3 (thresholds 25 mm2, 200 mm3)
- Не выполнено: torque (lower-bound bending stress 493 MPa exceeds upper-bound strength 47 MPa (material data sheet): the tooth cannot carry 25 N*m in this size/material); torque (lower-bound bending stress 1478 MPa exceeds upper-bound strength 47 MPa (material data sheet): the tooth cannot carry 75 N*m in this size/material)
- Не подтверждено расчётом/данными (нужны испытания): fatigue_resistance, acid_salt_resistance, gear_accuracy, hydrophobic, glass_transition_temperature
- Недостающие входные данные: gear_torque.contact, gear_torque.load_spectrum, gear_torque.fixture, lifetime.load_history, lifetime.duration, lifetime.environment, lifetime.allowed_failure_probability
- План испытаний: Испытание на статический и циклический момент на напечатанных деталях; Испытание на статический и циклический момент на напечатанных деталях; S–N испытания образцов/деталей; Выдержка в растворах кислот/солей; Контроль профиля и шага зубьев; Равновесная влажность при 85–98% RH

## 2. Колесо конвейерной линии - 5D

- Статус: needs_input; уровень доказательности: analytical_screen.
- Материал: Sealant TPU A95 — выбран из 16 допустимых; отклонено 4 (обязательное требование не выполнено).
- Режим: cylindrical. axis: rotational symmetry (inertia + sector radii); cylindrical working surface (tread/journal): cylindrical layers follow it; TZ reports delamination; cylindrical layers remove radial layer interfaces; through bore 15.99 mm: mandrel defines the bore, base cylinder removes no material
-   printing_mode = cylindrical (было cylindrical): route selected by documented policy
-   bottom_layers = 6 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class structural
-   layer_height = 0.2 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 6 (было 8): skin_layers, policy:skin
-   wall_line_count = 4 (было 2): shell 1.60 mm -> 4 walls (1.60 mm)
-   speed_print = 30.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = False (было False): 5D route prints the part without supports
-   cylindrical_mode_base_diameter = 15.99 (было 6.0): Spiral: equals the bore; Full: core inside the smallest section
-   non_printing_base_diameter = 15.99 (было 6.0): tooling diameter (printed mandrel of the bore diameter (manual 3.4.14))
- Не подтверждено расчётом/данными (нужны испытания): operating_hours, ipa_resistance, low_voc, dimensional_tolerance, no_play, high_friction_vs_paper, no_marking, fatigue_resistance, ductile_failure
- Недостающие входные данные: hole_accuracy.nominal_diameter, hole_accuracy.fit_length, hole_accuracy.mating_part, hole_accuracy.measurement_method, lifetime.load_history, lifetime.environment, lifetime.allowed_failure_probability, safe_rotation.balancing, safe_rotation.max_acceleration
- План испытаний: Циклы протирки 70% IPA; Эмиссия VOC (камера); Калибровка размеров: контрольные контуры и отверстия; Контроль посадки на вал; Трибометр: пара с бумагой/крафтом; Тест переноса материала на белую бумагу

## 3. Пыльник Шруса - 3D

- Статус: needs_input; уровень доказательности: document.
- Материал: Sealant TPU A95 — выбран из 3 допустимых; отклонено 17 (обязательное требование не выполнено).
- Режим: classic. no documented 5D benefit above threshold; classic is simpler
-   printing_mode = classic (было classic): route selected by documented policy
-   bottom_layers = 5 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class elastic
-   layer_height = 0.2 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 5 (было 8): skin_layers, policy:skin
-   wall_line_count = 3 (было 2): shell 1.20 mm -> 3 walls (1.20 mm)
-   material_bed_temperature = 45.0 (было 60.0): material profile value; explicit because STE takes max() over extruders
-   speed_print = 30.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = True (было False): overhang 10568 mm2, support volume bound 541954 mm3 (thresholds 25 mm2, 200 mm3)
- Не подтверждено расчётом/данными (нужны испытания): acid_salt_resistance, oil_fuel_resistance, sealing
- Недостающие входные данные: sealing.pressure, sealing.medium, sealing.max_leak_rate, sealing.time, elastic_boot.resistance_force, elastic_boot.bellows_geometry, elastic_boot.allowed_strain, elastic_boot.lifetime
- План испытаний: Выдержка в растворах кислот/солей; Выдержка в маслах/топливе/СОЖ; Испытание герметичности давлением/вакуумом

## 4. Фигурка

- Статус: candidate; уровень доказательности: document.
- Материал: Fiberpart TPU C5 — выбран из 13 допустимых; отклонено 7 (обязательное требование не выполнено).
- Режим: classic. no documented 5D benefit above threshold; classic is simpler
-   printing_mode = classic (было classic): route selected by documented policy
-   bottom_layers = 10 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 25.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class decorative
-   layer_height = 0.1 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 10 (было 8): skin_layers, policy:skin
-   wall_line_count = 3 (было 2): shell 1.20 mm -> 3 walls (1.20 mm)
-   material_bed_temperature = 95.0 (было 95.0): material profile value; explicit because STE takes max() over extruders
-   speed_print = 40.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = True (было False): overhang 3352 mm2, support volume bound 36737 mm3 (thresholds 25 mm2, 200 mm3)
- Не подтверждено расчётом/данными (нужны испытания): no_visible_layers, no_support_marks, alcohol_wipe_resistance, uv_color_stability
- План испытаний: Оценка поверхности по согласованному критерию; Визуальный контроль зон контакта поддержек; Циклы протирки спиртовыми салфетками; UV старение с измерением ΔE

## 5. Рабочее колесо насоса Wilo - 5D

- Статус: needs_input; уровень доказательности: analytical_screen.
- Материал: Fiberpart PP G30 — выбран из 1 допустимых; отклонено 19 (обязательное требование не выполнено).
- Режим: cylindrical. axis: rotational symmetry (inertia + sector radii); Stereotech prints pump impellers in 5D without supports (5dtech.pro/printers/solutions/pumpwheels); through bore 7.98 mm: mandrel defines the bore, base cylinder removes no material
-   machine_nozzle_size = 0.6 (было 0.4): material requires nozzle >= 0.6 mm: install it before printing
-   printing_mode = cylindrical (было cylindrical): route selected by documented policy
-   bottom_layers = 4 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class structural
-   layer_height = 0.3 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 4 (было 8): skin_layers, policy:skin
-   wall_line_count = 3 (было 2): shell 1.60 mm -> 3 walls (1.80 mm)
-   speed_print = 40.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = False (было False): 5D route prints the part without supports
-   cylindrical_mode_base_diameter = 7.98 (было 6.0): Spiral: equals the bore; Full: core inside the smallest section
-   non_printing_base_diameter = 7.98 (было 6.0): tooling diameter (metal rod in ER11 collet)
- Не подтверждено расчётом/данными (нужны испытания): alkali_resistance, cavitation_resistance, acid_salt_resistance, operating_hours, creep_resistance, fatigue_resistance, balance_grade, residual_unbalance, blade_thickness_variation, water_absorption_24h, ductile_failure, vicat_softening_temperature
- Недостающие входные данные: lifetime.load_history, lifetime.environment, lifetime.allowed_failure_probability, safe_rotation.balancing, safe_rotation.max_acceleration
- План испытаний: Выдержка в рабочей среде при рабочей температуре; Кавитационный износ (стенд); Выдержка в растворах кислот/солей; Ползучесть при +85 °C под нагрузкой; S–N испытания образцов/деталей; Балансировка на станке

## 6. Крыльчатка охлаждения электродвигателя - 3D

- Статус: needs_input; уровень доказательности: document.
- Материал: Enduse PP — выбран из 7 допустимых; отклонено 13 (обязательное требование не выполнено).
- Режим: classic. no documented 5D benefit above threshold; classic is simpler
-   printing_mode = classic (было classic): route selected by documented policy
-   bottom_layers = 6 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class structural
-   layer_height = 0.2 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 6 (было 8): skin_layers, policy:skin
-   wall_line_count = 4 (было 2): shell 1.60 mm -> 4 walls (1.60 mm)
-   material_bed_temperature = 78.0 (было 60.0): slicer profile 50 C outside data-sheet window 75..80 C; explicit because STE takes max() over extruders
-   material_print_temperature = 240.0 (было 225.0): slicer profile 225 C outside data-sheet window 240..240 C
-   speed_print = 30.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = True (было False): overhang 5785 mm2, support volume bound 63652 mm3 (thresholds 25 mm2, 200 mm3)
- Не подтверждено расчётом/данными (нужны испытания): oil_fuel_resistance, ozone_resistance, ductile_failure, fatigue_resistance
- Недостающие входные данные: lifetime.load_history, lifetime.duration, lifetime.environment, lifetime.allowed_failure_probability, safe_rotation.balancing, safe_rotation.max_acceleration
- План испытаний: Выдержка в маслах/топливе/СОЖ; Озоновое старение (ISO 1431 аналог); Ударное нагружение детали/кожуха; S–N испытания образцов/деталей

## 7. Ось вращения передающих звёзд формовщика коробов Krones - 5D

- Статус: needs_calibration; уровень доказательности: analytical_screen.
- Материал: Enduse PA6 — выбран из 16 допустимых; отклонено 4 (обязательное требование не выполнено).
- Режим: cylindrical_full. axis: coaxial cylindrical surfaces (29% of area); working profile must have no layer steps; 5D layers follow the profile; core 6.2 mm inside min section radius 4.12 mm (80% / 1 mm margin policy)
-   printing_mode = cylindrical_full (было cylindrical_full): route selected by documented policy
-   bottom_layers = 12 (было 8): skin_layers, policy:skin
-   infill_sparse_density = 100.0 (было 20.0): no calibrated relation load -> infill exists; policy by load class structural
-   layer_height = 0.1 (было 0.1): choose_layer_height, policy:layer
-   top_layers = 12 (было 8): skin_layers, policy:skin
-   wall_line_count = 4 (было 2): shell 1.60 mm -> 4 walls (1.60 mm)
-   speed_print = 50.0 (было 60.0): material profile/data-sheet speed; Q_limit uncalibrated (needs_calibration)
-   support_enable = False (было False): 5D route prints the part without supports
-   cylindrical_mode_base_diameter = 6.2 (было 6.0): Spiral: equals the bore; Full: core inside the smallest section
-   non_printing_base_diameter = 6.0 (было 6.0): tooling diameter (6 mm rod)
- Не подтверждено расчётом/данными (нужны испытания): torque, alkali_resistance, dimensional_tolerance, low_friction_vs_pom, short_contact_temperature, non_toxic
- План испытаний: Испытание на статический и циклический момент на напечатанных деталях; Выдержка в рабочей среде при рабочей температуре; Калибровка размеров: контрольные контуры и отверстия; Трибометр: пара с POM-роликом без смазки; Оценка пищевого контакта процесса печати
