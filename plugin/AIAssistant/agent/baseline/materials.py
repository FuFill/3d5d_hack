# -*- coding: utf-8 -*-
"""Скрининг материалов (screen_material, спецификация 4): трёхзначная логика pass/fail/unknown.

Интервальная проверка: для минимума L — pass, если нижняя граница интервала ≥ L,
fail, если верхняя граница < L, иначе unknown. Unknown никогда не превращается
в pass; ранжирование сначала по числу нарушенных обязательных требований.
"""

from . import knowledge

GOOD = ("excellent", "good")

# Какое заявление производителя (claims) подкрепляет требование.
CLAIM_SUPPORT = {
    "uv": ["uv"],
    "alkali": ["alkali"],
    "acids_salts": ["acids"],
    "oil": ["oil", "fat"],
    "fuel": ["fuel", "diesel"],
    "alcohol": ["alcohol"],
    "wear": ["wear", "sliding_parts"],
    "impact": ["impact"],
    "hot_water": ["hot_coolant", "hydrolysis"],
    "friction_low": ["sliding_parts"],
    "layer_hiding": ["no_visible_layer_transition", "rough_surface"],
    "elasticity": ["seals", "high_flexibility", "rubber_like", "chemical_seals"],
    "stiffness": ["very_stiff"],
    "fatigue": ["cyclic_loads"],
    "food_safe": ["food_contact"],
}
# Заявления, прямо противоречащие требованию.
CLAIM_CONFLICT = {"uv": ["not_uv_resistant"], "water_absorption_24h_pct": ["not_humidity_resistant", "hygroscopic"]}

PRINTABILITY_BONUS = {"excellent": 0.3, "good": 0.2, "fair": 0.1, "poor": 0.0}


def _interval_check(interval, comparator, value):
    if interval is None:
        return "unknown", "нет данных"
    lo, hi = interval
    text = "типично %g…%g" % (lo, hi)
    if comparator == ">=":
        if lo >= value:
            return "pass", text
        if hi < value:
            return "fail", text
    else:
        if hi <= value:
            return "pass", text
        if lo > value:
            return "fail", text
    return "unknown", text + " (интервал пересекает порог)"


def _rating_check(rating):
    if rating in GOOD:
        return "pass"
    if rating == "poor":
        return "fail"
    return "unknown"


def check(mat, req, policy):
    """Возвращает (status, evidence_text) для одного требования и одного материала."""
    prop, comp, value = req["property"], req["comparator"], req["value"]
    typical = mat.get("typical", {})
    ratings = mat.get("ratings", {})
    filler = mat.get("filler") or ""

    if prop in ("tolerance_mm", "surface_no_layers", "wall_variation_mm"):
        # Точность и поверхность зависят от печатаемости материала (политика P).
        rating = ratings.get("printability")
        status = "pass" if rating in GOOD else "unknown"
        return status, "печатаемость: %s" % (rating or "нет")
    if req["kind"] == "process":
        return None, None
    if prop == "density_g_cm3":
        d = mat["density_g_cm3"]
        return ("pass" if d <= value else "fail"), "профиль: %g г/см³" % d
    if prop in ("tg_c", "vicat_b50_c", "tensile_modulus_mpa", "tensile_strength_mpa", "elongation_pct",
                "water_absorption_24h_pct"):
        status, text = _interval_check(typical.get(prop), comp, value)
        if any(c in mat.get("claims", []) for c in CLAIM_CONFLICT.get(prop, [])) and status != "fail":
            status, text = "fail", text + "; противоречит описанию производителя"
        return status, text
    if prop == "service_temp_max_c":
        need = value + policy["heat_margin_c"]
        key = "service_max_c" if mat.get("flexible") or "hdt_c" not in typical else "hdt_c"
        interval = typical.get(key)
        if interval is None:
            return "unknown", "нет данных о теплостойкости"
        lo, hi = interval
        label = "HDT" if key == "hdt_c" else "T_max эксплуатации"
        if lo >= need:
            return "pass", "%s %g…%g ≥ %g (T+запас)" % (label, lo, hi, need)
        if hi < value:
            return "fail", "%s %g…%g < %g" % (label, lo, hi, value)
        return "unknown", "%s %g…%g, нужно ≥ %g" % (label, lo, hi, need)
    if prop == "short_contact_c":
        interval = typical.get("melting_c") or typical.get("vicat_b50_c") or typical.get("service_max_c")
        return _interval_check(interval, ">=", value)
    if prop == "elasticity":
        if not mat.get("flexible"):
            return "fail", "жёсткий материал"
        st = _rating_check(ratings.get("elasticity"))
        return st, "эластомер, Shore %s" % mat.get("shore")
    if prop == "stiffness":
        if mat.get("flexible"):
            return "fail", "эластомер"
        return _interval_check(typical.get("tensile_modulus_mpa"), ">=", policy["stiff_min_modulus_mpa"])
    if prop == "rigid":
        return ("fail" if mat.get("flexible") else "pass"), ("эластомер" if mat.get("flexible") else "жёсткий")
    if prop == "no_abrasive_filler":
        bad = filler.startswith("GF") or filler.startswith("CF")
        return ("fail" if bad else "pass"), ("наполнитель %s" % filler if filler else "без наполнителя")
    if prop == "no_conductive_filler":
        return ("fail" if filler.startswith("CF") else "pass"), ("наполнитель %s" % filler if filler else "без наполнителя")
    if prop == "no_marks":
        if mat.get("marks_risk"):
            return "fail", "углеродный наполнитель оставляет тёмные следы"
        return "unknown", "требуется испытание на контртеле"
    if prop == "food_safe":
        if "food_contact" in mat.get("claims", []):
            return "pass", "заявлено производителем"
        return "unknown", "нет заявления о пищевом контакте"
    if prop == "esd":
        return "unknown", "антистатические свойства не заявлены"
    if comp == "rating>=":
        rating = ratings.get(prop)
        status = _rating_check(rating)
        if any(c in mat.get("claims", []) for c in CLAIM_CONFLICT.get(prop, [])):
            status = "fail"
        return status, "оценка класса: %s" % (rating or "нет")
    # Ресурс, ползучесть, герметичность и т.п. — только испытаниями.
    return "unknown", "подтверждается только испытаниями"


def screen_materials(reqset, candidates=None):
    policy = reqset["policy"]
    results = []
    for mat in candidates or knowledge.materials():
        checks = []
        fails = unknown = 0
        score = 0.0
        for req in reqset["requirements"]:
            status, evidence = check(mat, req, policy)
            if status is None:
                continue
            claim = any(c in mat.get("claims", []) for c in CLAIM_SUPPORT.get(req["property"], []))
            checks.append({"requirement_id": req["id"], "property": req["property"], "status": status,
                           "evidence": evidence, "manufacturer_claim": claim})
            weight = 1.0 if req["mandatory"] else 0.5
            if status == "fail":
                if req["mandatory"]:
                    fails += 1
                else:
                    score -= 0.5
            elif status == "pass":
                score += weight
            elif req["kind"] != "qualification":
                unknown += 1
                score -= 0.35 * weight
            if claim and status != "fail":
                score += 0.25
        score += PRINTABILITY_BONUS.get(mat.get("ratings", {}).get("printability"), 0.0)
        # Политика P: под нагрузкой с допуском из эластомеров предпочтителен более твёрдый (Shore D).
        if mat.get("flexible") and "load_bearing" in reqset.get("flags", []) and "D" in (mat.get("shore") or ""):
            score += 0.1
        results.append({
            "material_id": mat["id"],
            "label": mat["label"],
            "mandatory_fails": fails,
            "unknown": unknown,
            "score": round(score, 3),
            "checks": checks,
        })
    results.sort(key=lambda r: (r["mandatory_fails"], -r["score"], r["material_id"]))
    return results
