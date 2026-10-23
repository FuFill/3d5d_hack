# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Requirement verification, decision status and qualification plan (sections 1, 11, 17).

Verification merges evidence from material screening, structural screening,
geometry/route checks and the sliced toolpath. A requirement passes only if
some evidence passes it and none fails it; unknown never turns into pass.
"""

from .result import (PASS, FAIL, UNKNOWN, DECISION_NEEDS_INPUT, DECISION_INFEASIBLE, DECISION_NEEDS_CALIBRATION,
                     DECISION_CANDIDATE, DECISION_SLICE_VERIFIED, EVIDENCE_ORDER)

# property -> (test, note) for the qualification plan
QUALIFICATION_TESTS = {
    "torque": ("Испытание на статический и циклический момент на напечатанных деталях",
               "в рабочей ориентации, с ответной деталью, при +70 °C и влажности"),
    "fatigue_resistance": ("S–N испытания образцов/деталей", "та же ориентация слоёв и режим печати"),
    "glass_transition_temperature": ("DSC/DMA по ISO 11357 / ISO 6721", "Tg не заменяется Vicat/HDT"),
    "vicat_softening_temperature": ("Vicat B50 по ISO 306", "на образцах из напечатанного материала"),
    "tensile_modulus": ("Растяжение по ISO 527 в XY и Z", "после выдержки в среде при рабочей температуре"),
    "tensile_strength": ("Растяжение по ISO 527 в XY и Z", "после выдержки в среде"),
    "elongation_at_break": ("Растяжение по ISO 527", "в направлении нагрузки детали"),
    "water_absorption_24h": ("Водопоглощение по ISO 62", "24 h погружения, на напечатанных образцах"),
    "density": ("Плотность по ISO 1183 и масса детали", "масса детали — основной критерий"),
    "alkali_resistance": ("Выдержка в рабочей среде при рабочей температуре", "изменение массы, размеров, прочности"),
    "acid_salt_resistance": ("Выдержка в растворах кислот/солей", "изменение массы и прочности"),
    "oil_fuel_resistance": ("Выдержка в маслах/топливе/СОЖ", "изменение массы, твёрдости, прочности"),
    "ipa_resistance": ("Циклы протирки 70% IPA", "внешний вид, масса, прочность"),
    "alcohol_wipe_resistance": ("Циклы протирки спиртовыми салфетками", "внешний вид"),
    "ozone_resistance": ("Озоновое старение (ISO 1431 аналог)", "растрескивание под нагрузкой"),
    "uv_resistance": ("Ускоренное старение UV (ISO 4892-2)", "прочность и цвет до/после"),
    "uv_color_stability": ("UV старение с измерением ΔE", "критерий ΔE согласовать"),
    "hydrophobic": ("Равновесная влажность при 85–98% RH", "изменение размеров и модуля"),
    "abrasion_resistance": ("Износ в паре и среде (абразивная пыль)", "износ обеих поверхностей"),
    "low_friction_vs_pom": ("Трибометр: пара с POM-роликом без смазки", "μ и износ обеих сторон"),
    "high_friction_vs_paper": ("Трибометр: пара с бумагой/крафтом", "μ при 45% RH"),
    "no_marking": ("Тест переноса материала на белую бумагу", "визуально, после истирания"),
    "low_voc": ("Эмиссия VOC (камера)", "по требованиям заказчика"),
    "non_toxic": ("Оценка пищевого контакта процесса печати", "допуск сырья не квалифицирует процесс"),
    "sealing": ("Испытание герметичности давлением/вакуумом", "среда, давление, время, допустимая утечка"),
    "elasticity_over_range": ("Усилие изгиба/хода при 0 °C и +75 °C", "на собранном узле"),
    "creep_resistance": ("Ползучесть при +85 °C под нагрузкой", "в среде"),
    "cavitation_resistance": ("Кавитационный износ (стенд)", "по режиму насоса"),
    "ductile_failure": ("Ударное нагружение детали/кожуха", "характер разрушения: изгиб/отрыв лопасти"),
    "impact_resistance": ("Удар по Шарпи/детали при рабочей температуре", "в направлении нагрузки"),
    "toughness": ("Изгиб тонких элементов до разрушения", "на напечатанной детали"),
    "dimensional_tolerance": ("Калибровка размеров: контрольные контуры и отверстия", "bias и интервал по ориентации"),
    "blade_thickness_variation": ("Измерение толщины лопастей", "КИМ/3D сканирование"),
    "residual_unbalance": ("Балансировка на станке", "по допуску ТЗ и классу G"),
    "balance_grade": ("Балансировка на станке", "U_per из класса G, массы и частоты"),
    "no_visible_layers": ("Оценка поверхности по согласованному критерию", "расстояние/освещение/Ra"),
    "no_support_marks": ("Визуальный контроль зон контакта поддержек", "или согласованная постобработка"),
    "esd": ("Поверхностное сопротивление (IEC 61340)", "диапазон 1e6–1e9 Ohm"),
    "galvanic_inert": ("Проверка отсутствия токопроводящего наполнителя", "сопротивление"),
    "gear_accuracy": ("Контроль профиля и шага зубьев", "класс точности по ТЗ"),
    "no_play": ("Контроль посадки на вал", "люфт/натяг"),
}


def verify_requirements(reqset, evidence):
    """evidence: list of {"requirement_id", "status", "evidence", "source", "note"}."""
    by_req = {}
    for e in evidence:
        rid = e.get("requirement_id")
        if rid:
            by_req.setdefault(rid, []).append(e)
    out = []
    for r in reqset.requirements:
        items = by_req.get(r.id, [])
        statuses = [i["status"] for i in items]
        if FAIL in statuses:
            status = FAIL
        elif PASS in statuses:
            status = PASS
        else:
            status = UNKNOWN
        levels = [i.get("evidence") for i in items if i.get("status") == status and i.get("evidence")]
        level = max(levels, key=lambda x: EVIDENCE_ORDER.index(x) if x in EVIDENCE_ORDER else -1) if levels else None
        if r.kind == "condition" and not items:
            status = "input"   # service conditions are model inputs, not verifiable limits
        out.append({"requirement_id": r.id, "property": r.property, "mandatory": r.mandatory, "status": status,
                    "evidence_level": level, "sources": [i.get("source") for i in items],
                    "notes": [i.get("note") for i in items if i.get("note")],
                    "fragment": r.provenance.get("fragment")})
    return out


def decision_status(verification, missing_inputs, calibration_needed, sliced=False):
    """Status precedence: infeasible > needs_input > needs_calibration > candidate/slice_verified."""
    if any(v["status"] == FAIL and v["mandatory"] for v in verification):
        return DECISION_INFEASIBLE
    if missing_inputs:
        return DECISION_NEEDS_INPUT
    if calibration_needed:
        return DECISION_NEEDS_CALIBRATION
    return DECISION_SLICE_VERIFIED if sliced else DECISION_CANDIDATE


def build_qualification_plan(verification):
    plan = []
    for v in verification:
        if v["status"] in (PASS, "input"):
            continue
        test = QUALIFICATION_TESTS.get(v["property"])
        if not test or not test[0]:
            continue
        plan.append({"requirement_id": v["requirement_id"], "property": v["property"],
                     "reason": "fail" if v["status"] == FAIL else "unverified", "test": test[0], "conditions": test[1],
                     "mandatory": v["mandatory"]})
    plan.sort(key=lambda p: (not p["mandatory"], p["reason"] != "fail", p["requirement_id"]))
    return plan


def explain_decision(decision):
    """Short Russian explanation assembled only from the decision trace (no new facts)."""
    lines = []
    m = decision.get("chosen_material") or {}
    route = decision.get("route") or {}
    lines.append("Статус: %s; уровень доказательности: %s." % (decision.get("status"), decision.get("evidence_level")))
    if m:
        lines.append("Материал: %s — выбран из %d допустимых; отклонено %d (обязательное требование не выполнено)." %
                     (m.get("label"), decision.get("materials_considered", 0), decision.get("materials_rejected", 0)))
    if route:
        lines.append("Режим: %s. %s" % (route.get("mode"), route.get("note") or ""))
    for item in decision.get("setting_patch", {}).get("items", []):
        why = item.get("note") or ", ".join(item.get("model_ids") or [])
        lines.append("  %s = %s (было %s): %s" % (item["key"], item["value"], item["old_effective_value"], why))
    fails = [v for v in decision.get("verification", []) if v["status"] == FAIL]
    if fails:
        lines.append("Не выполнено: " + "; ".join("%s (%s)" % (v["property"], "; ".join(v["notes"][:1]))
                                                  for v in fails))
    unk = [v["property"] for v in decision.get("verification", []) if v["status"] == UNKNOWN and v["mandatory"]]
    if unk:
        lines.append("Не подтверждено расчётом/данными (нужны испытания): " + ", ".join(unk))
    causal = decision.get("causal_analysis") or {}
    functional = decision.get("functional_analysis") or {}
    if functional.get("value"):
        value = functional["value"]
        selected = value.get("selected")
        if selected:
            lines.append("Функциональная ориентация: %s, профиль %s; проверено %d кандидатов. %s" %
                         (functional["status"], selected["profile_id"], value["candidate_count"],
                          "Предложение включено в план." if value["applied"] else "Основной план сохранён; предложенный вариант требует подтверждающих данных."))
            for case in selected["mechanics"]["cases"]:
                lines.append("  %s: жёсткость %.4g Н/мм, перемещение %.4g мм, запас %.4g; расчётная оценка." %
                             (case["id"], case["effective_stiffness_N_per_mm"], case["max_displacement_mm"], case["safety_factor_min"]))
            lines.append("Гладкость оценивается геометрической ступенчатостью; Ra и плоскостность без калибровки не подтверждены.")
    if causal.get("status") != "computed":
        lines.append("Double ML: оценка отсутствует или часть изменений не подтверждена; "
                     "нужны измеренные испытания в тех же условиях.")
    for entry in causal.get("value") or []:
        if entry.get("status") != "computed":
            reasons = [t["reason"] for t in entry.get("trace", []) if t.get("reason")]
            if reasons:
                lines.append("Double ML (%s): %s." % (entry.get("dataset_id") or "данные", reasons[0]))
            continue
        value = entry["value"]
        change = value.get("change")
        if change:
            contrast = change["value"]
            lo, hi = contrast["confidence_interval_95"]
            lines.append("Double ML (%s): %s %g -> %g %s; изменение %s: %+.4g %s "
                         "(95%% интервал [%+.4g; %+.4g]). Это оценка разницы, не подтверждение "
                         "прочности детали; предложенный patch не применён." %
                         (value["dataset_id"], value["treatment"], contrast["baseline"], contrast["target"],
                          value["treatment_unit"], value["outcome"]["name"], contrast["expected_change"],
                          value["outcome"]["unit"], lo, hi))
        else:
            lines.append("Double ML (%s): оценка эффекта %s доступна; целевое изменение не запрошено." %
                         (value["dataset_id"], value["treatment"]))
    return lines
