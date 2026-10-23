# -*- coding: utf-8 -*-
"""explain_decision: текстовое объяснение из trace/Decision без новых фактов."""

MODE_LABEL = {"classic": "3D (classic)", "cylindrical": "5D Spiral (cylindrical)", "cylindrical_full": "5D Full (cylindrical_full)"}
STATUS_LABEL = {
    "candidate": "кандидат: расчётный вариант, свойства детали не квалифицированы",
    "needs_calibration": "кандидат, требуются калибровки/испытания и недостающие данные",
    "infeasible": "ни один материал не прошёл все обязательные требования",
    "needs_input": "не хватает обязательных данных",
}
CHECK_MARK = {"pass": "✅", "fail": "❌", "unknown": "❔"}


def _fmt(v):
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def explain_decision(d, title=None):
    c = d["chosen_candidate"]
    lines = []
    if title:
        lines.append("# %s" % title)
        lines.append("")
    lines.append("**Статус:** `%s` — %s. Уровень доказательности: `%s`." % (d["status"], STATUS_LABEL.get(d["status"], ""), d["evidence_level"]))
    lines.append("")
    lines.append("| Решение | Значение |")
    lines.append("|---|---|")
    lines.append("| Принтер | %s |" % d["machine"]["name"])
    lines.append("| Режим | %s |" % MODE_LABEL.get(c["printing_mode"], c["printing_mode"]))
    lines.append("| Материал | %s (`%s`) |" % (c["material"]["label"], c["material"]["id"]))
    lines.append("| Профиль качества | %s (`%s`, слой %s мм) |" % (c["quality"]["name"], c["quality"]["quality_type"], _fmt(c["quality"]["layer_height"])))
    o = c["orientation"]
    lines.append("| Ориентация | вектор модели %s → +Z; поворот %s° вокруг %s |" % (o["up_vector_model"], _fmt(o["rotation_axis_angle"]["angle_deg"]), o["rotation_axis_angle"]["axis"]))
    lines.append("| Размещение | %s; габарит %s мм |" % (o["placement"], o["size_after_mm"]))
    if c.get("batch"):
        b = c["batch"]
        lines.append("| Партия | %d шт.: %d на стол, %d запусков |" % (b["quantity"], b["per_plate"], b["plates"]))
    lines.append("")
    lines.append("## Почему такой режим")
    for r in c["route_reasons"]:
        lines.append("- %s" % r)
    for alt in d["candidates"]["routes"]:
        lines.append("- Альтернатива `%s`: %s" % (alt["mode"], alt["why_not"]))
    lines.append("")
    lines.append("## Материал")
    lines.append("| Кандидат | Нарушено обязательных | Неизвестно | Балл |")
    lines.append("|---|---:|---:|---:|")
    for m in d["candidates"]["materials"]:
        lines.append("| %s | %d | %d | %s |" % (m["label"], m["mandatory_fails"], m["unknown"], _fmt(m["score"])))
    lines.append("")
    req_by_id = dict((r["id"], r) for r in d["requirements"])
    lines.append("Проверки выбранного материала:")
    lines.append("")
    for chk in c["material"]["checks"]:
        r = req_by_id[chk["requirement_id"]]
        claim = " (заявлено производителем)" if chk["manufacturer_claim"] else ""
        value = "" if r["value"] is True else " %s %s" % (r["comparator"], _fmt(r["value"]))
        optional = "" if r["mandatory"] else " (желательно)"
        lines.append("- %s `%s` %s%s%s — %s%s" % (CHECK_MARK[chk["status"]], chk["requirement_id"], r["property"], value, optional,
                                                 chk["evidence"], claim))
    lines.append("")
    lines.append("## Изменённые настройки")
    lines.append("| Ключ | Значение | Область | Обоснование |")
    lines.append("|---|---|---|---|")
    for p in d["setting_patch"]:
        refs = (" [%s]" % ", ".join(p["requirement_ids"])) if p["requirement_ids"] else ""
        lines.append("| `%s` | %s | %s | %s%s |" % (p["key"], _fmt(p["value"]), p["scope"], p["reason"], refs))
    if d["rejected_settings"]:
        lines.append("")
        lines.append("Отклонено проверкой схемы: " + "; ".join("`%s`: %s" % (r["key"], r["error"]) for r in d["rejected_settings"]))
    lines.append("")
    lines.append("Температуры, обдув и ретракт — из профиля материала: сопло %s °C, стол %s °C, обдув %s %%, ретракт %s мм." % (
        _fmt(d["effective_settings"].get("material_print_temperature")), _fmt(d["effective_settings"].get("material_bed_temperature")),
        _fmt(d["effective_settings"].get("cool_fan_speed")), _fmt(d["effective_settings"].get("retraction_amount"))))
    if d["warnings"] or d["notes"]:
        lines.append("")
        lines.append("## Предупреждения")
        for wtext in d["warnings"] + d["notes"]:
            lines.append("- %s" % wtext)
    if d["missing_inputs"]:
        lines.append("")
        lines.append("## Не хватает данных")
        for m in d["missing_inputs"]:
            lines.append("- %s" % m)
    if d["qualification_plan"]:
        lines.append("")
        lines.append("## Что подтвердить испытаниями")
        for q in d["qualification_plan"]:
            lines.append("- `%s` %s: %s" % (q["requirement_id"], q["property"], q["test"]))
    lines.append("")
    return "\n".join(lines)
