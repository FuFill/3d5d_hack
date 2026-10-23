# -*- coding: utf-8 -*-
"""Правила извлечения требований из текста ТЗ (parse/normalize, спецификация, раздел 3).

Бэйзлайн детерминированный: регулярные выражения и словари ключевых слов. Каждое
требование хранит цитату-источник (provenance) и статус интерпретации:
explicit — число/условие прямо в тексте; derived — выведено правилом из
формулировки; assumed — порог политики (P), которого в тексте нет.
LLM может уточнять результат, но не должна добавлять числа без источника.
"""

import re

NUM = r"[-+]?\d+(?:[.,]\d+)?"

# Пороги политики для качественных формулировок (класс P, не законы природы).
POLICY = {
    "stiff_min_modulus_mpa": 3000.0,
    "hydrophobic_max_water_pct": 0.5,
    "heat_margin_c": 10.0,
    "non_brittle_min_elongation_pct": 3.0,
}


def _num(text):
    return float(text.replace(",", ".").replace("+", ""))


def _lines(text):
    out = []
    for raw in re.split(r"[\r\n]+", text):
        for part in re.split(r"(?<=[.;])\s+(?=[А-ЯA-Z])", raw):
            part = part.strip()
            if part:
                out.append(part)
    return out


class _Collector(object):
    def __init__(self):
        self.items = []
        self._seen = set()

    def add(self, prop, comparator, value, unit, line, status="explicit", mandatory=None, note=None, kind="material"):
        key = (prop, comparator, value)
        if key in self._seen:
            return
        self._seen.add(key)
        if mandatory is None:
            mandatory = not re.search(r"желательно", line, re.I)
        self.items.append({
            "id": "R%d" % (len(self.items) + 1),
            "property": prop,
            "comparator": comparator,
            "value": value,
            "unit": unit,
            "kind": kind,
            "mandatory": bool(mandatory),
            "interpretation_status": status,
            "provenance": line[:220],
            "note": note,
        })


# (свойство, регулярное выражение, сравнение, единица)
_NUMERIC = [
    ("tg_c", r"(?:Tg|стеклован)[^\n]*?(?:не ниже|не менее|выше)\s*\+?(" + NUM + r")(?:\s*[-–]\s*(" + NUM + r"))?", ">=", "degC"),
    ("vicat_b50_c", r"Вика[^\n]*?не ниже\s*\+?(" + NUM + r")", ">=", "degC"),
    ("tensile_modulus_mpa", r"[Мм]одуль упругости[^\n]*?не менее\s*(" + NUM + r")\s*МПа", ">=", "MPa"),
    ("tensile_strength_mpa", r"[Пп]редел прочности[^\n]*?не менее\s*(" + NUM + r")\s*МПа", ">=", "MPa"),
    ("elongation_pct", r"удлинение[^\n]*?не менее\s*(" + NUM + r")\s*%", ">=", "percent"),
    ("water_absorption_24h_pct", r"[Вв]одопоглощение[^\n]*?не более\s*(" + NUM + r")\s*%", "<=", "percent"),
    ("density_g_cm3", r"(?:не более|не тяжелее|не выше)\s*(" + NUM + r")\s*г\s*/?\s*см", "<=", "g/cm3"),
]

# (свойство, шаблон, комментарий). Качественные требования проверяются по ratings.
_QUALITATIVE = [
    ("uv", r"УФ|ультрафиолет|солнечн", None),
    ("alcohol", r"спирт|IPA|изопропил|перекис", None),
    ("alkali", r"щелоч|pH\s*(?:9|10|11)", None),
    ("acids_salts", r"кислот|\bсол(?:и|ей|ям)\b|хлорид|реагент", None),
    ("oil", r"масл|СОЖ|смазк|жир", None),
    ("fuel", r"бензин|дизел|топлив", None),
    ("ozone", r"озон", None),
    ("hot_water", r"теплоносител|гликол|гидролиз", None),
    ("elasticity", r"эластичн|гибк\w+|жестких материалов не допускается", "деталь должна быть эластичной"),
    ("stiffness", r"высок\w* модул\w* упругости|«?подгибал|сохранения геометрии лопаст|достаточно жестк", "высокая жёсткость"),
    ("impact", r"хрупк|осколк|удар|раскалыв|отламывал", "отсутствие хрупкого разрушения"),
    ("wear", r"износ|истиран|абразив|выработк", "износостойкость"),
    ("friction_low", r"низк\w* коэффициент\w* трения", "низкое трение"),
    ("friction_high", r"высок\w* коэффициент\w* трения|проскальзыв", "высокое трение"),
    ("fine_detail", r"тонкие элементы|мелк\w+ детал", "мелкие элементы"),
    ("layer_hiding", r"видимых сло[её]в", "поверхность без видимых слоёв"),
]

_UNKNOWN_ONLY = [
    ("fatigue", r"усталост|циклическ\w+ нагрузк|непрерывн\w+|24/7|старт-стоп", "ресурс/усталость: только испытания"),
    ("creep", r"ползуч|«ползти»|ползти", "ползучесть: только испытания"),
    ("sealing", r"герметич", "герметичность: только испытания"),
    ("no_marks", r"(?:черных|цветных) следов|пластификатор", "отсутствие следов на контртеле"),
    ("dust_free", r"микропыл|пылени|чешуек", "отсутствие пыления"),
    ("esd", r"\bESD\b", "антистатические свойства"),
    ("food_safe", r"пищев|токсичн", "пищевой контакт/нетоксичность"),
    ("balance", r"дисбаланс|балансиров", "балансировка ротора"),
    ("cavitation", r"кавитац", "кавитационная стойкость"),
]


def parse_requirements(text, hint=None):
    """text — ТЗ и/или сообщение пользователя; hint — подпись к файлу/папке (например «5D»)."""
    c = _Collector()
    numbers = {}
    flags = set()
    lines = _lines(text or "")

    for line in lines:
        low = line.lower()
        for prop, pattern, comp, unit in _NUMERIC:
            m = re.search(pattern, line, re.I)
            if not m:
                continue
            if prop == "density_g_cm3" and not re.search(r"плотн|тяжел|легк", low):
                continue
            value = _num(m.group(1))
            note = None
            if prop == "tg_c" and m.group(2):
                note = "диапазон %s–%s; принят нижний предел, верхний — желательный" % (m.group(1), m.group(2))
            c.add(prop, comp, value, unit, line, note=note)

        # Рабочая температура: максимумы по строкам с °C, кроме Tg/Вика/кратковременного контакта.
        if "°c" in low.replace("°с", "°c") and not re.search(r"tg|стеклован|вика|сушк", low):
            # «60-65°C» — диапазон, а не отрицательная температура; «20°C ± 2°C» — допуск.
            tline = re.sub(r"(\d)\s*[-–]\s*(?=\d)", r" ", line)
            m_pm = re.search(r"(" + NUM + r")\s*°\s*[CС]\s*±\s*(" + NUM + r")\s*°?\s*[CС]?", tline)
            if m_pm:
                tline = tline.replace(m_pm.group(0), " ")
            temps = [_num(t) for t in re.findall(r"(" + NUM + r")\s*°\s*[CС]", tline)]
            if m_pm:
                base, dev = _num(m_pm.group(1)), _num(m_pm.group(2))
                temps.extend([base - dev, base + dev])
            if temps:
                if re.search(r"кратковрем|капани|клея", low):
                    c.add("short_contact_c", ">=", max(temps), "degC", line,
                          note="кратковременный контакт: материал не должен плавиться")
                else:
                    numbers["service_temp_max_c"] = max(numbers.get("service_temp_max_c", -1e9), max(temps))
                    numbers["service_temp_min_c"] = min(numbers.get("service_temp_min_c", 1e9), min(temps))
                    numbers.setdefault("_service_lines", []).append(line)

        for prop, pattern, note in _QUALITATIVE:
            if re.search(pattern, line, re.I):
                status = "explicit" if prop not in ("stiffness", "impact", "elasticity") else "derived"
                c.add(prop, "rating>=", "good", None, line, status=status, note=note)
        for prop, pattern, note in _UNKNOWN_ONLY:
            if re.search(pattern, line, re.I):
                c.add(prop, "verified", True, None, line, status="explicit", note=note, kind="qualification")

        if re.search(r"гидрофоб|влажност\w*:?\s*8\d", line, re.I) or ("туман" in low and "масл" not in low):
            c.add("water_absorption_24h_pct", "<=", POLICY["hydrophobic_max_water_pct"], "percent", line,
                  status="assumed", note="«гидрофобный»/высокая влажность → порог политики %.1f%%" % POLICY["hydrophobic_max_water_pct"])
        if re.search(r"не должна вызывать ускоренный износ ответн", line, re.I):
            c.add("no_abrasive_filler", "==", True, None, line, status="derived",
                  note="абразивный наполнитель изнашивает контртело")
        elif re.search(r"стачивают ответные", line, re.I):
            # Из описания проблемы аналога, а не из требований: мягкое предпочтение.
            c.add("no_abrasive_filler", "==", True, None, line, status="derived", mandatory=False,
                  note="проблема аналога: износ ответной детали")
        if re.search(r"лопасти начнут изгибаться|центробежн\w+ сил", line, re.I):
            c.add("rigid", "==", True, None, line, status="derived", note="деталь не должна быть эластомером")
        if re.search(r"токопроводящ|металлическ\w+ композит|графит|углеволок", line, re.I):
            c.add("no_conductive_filler", "==", True, None, line, status="derived",
                  mandatory=not re.search(r"нежелательно", line, re.I), note="риск гальванической пары")

        # Технологические и геометрические требования
        m = re.search(r"±\s*(" + NUM + r")\s*мм", line)
        if m and re.search(r"точност|допуск|отклонен|контур", low):
            c.add("tolerance_mm", "<=", _num(m.group(1)), "mm", line, kind="process")
        m = re.search(r"[Рр]азностенност\w*[^\n]*?(" + NUM + r")\s*мм", line)
        if m:
            c.add("wall_variation_mm", "<=", _num(m.group(1)), "mm", line, kind="process")
        if re.search(r"видимых сло[её]в|«?ступеньк\w*»? от сло|ступенек", low):
            c.add("surface_no_layers", "==", True, None, line, kind="process")
        if re.search(r"следов от поддерж", low):
            c.add("no_support_marks", "==", True, None, line, kind="process")
        if re.search(r"точность эвольвенты|шаг зубьев", low):
            flags.add("gear_teeth")
        m = re.search(r"парти\w* из (\d+)", low)
        if m:
            numbers["batch_qty"] = int(m.group(1))
        m = re.search(r"(" + NUM + r")\s*г\s*·?\s*мм", line)
        if m and "дисбаланс" in low:
            c.add("balance_gmm", "<=", _num(m.group(1)), "g*mm", line, kind="qualification")
        for m in re.finditer(r"(" + NUM + r")\s*об\s*/?\s*мин", line):
            numbers["rpm"] = max(numbers.get("rpm", 0.0), _num(m.group(1)))
        for m in re.finditer(r"(" + NUM + r")\s*Н\s*·\s*м", line):
            value = _num(m.group(1))
            if re.search(r"пиков|стопор", low):
                numbers["torque_peak_nm"] = max(numbers.get("torque_peak_nm", 0.0), value)
            elif re.search(r"номинал", low):
                numbers["torque_nominal_nm"] = value
            numbers["torque_nm"] = max(numbers.get("torque_nm", 0.0), value)
        m = re.search(r"нагрузк\w*[^\n]*?(" + NUM + r")\s*кг", line)
        if m:
            numbers["load_n"] = max(numbers.get("load_n", 0.0), _num(m.group(1)) * 9.81)
        for m in re.finditer(r"(" + NUM + r")\s*Н\b(?!\s*·)", line):
            if "вика" not in low:
                numbers["load_n"] = max(numbers.get("load_n", 0.0), _num(m.group(1)))
        m = re.search(r"(" + NUM + r")\s*бар", line)
        if m:
            numbers["pressure_bar"] = _num(m.group(1))

    service_lines = numbers.pop("_service_lines", [])
    if "service_temp_max_c" in numbers:
        c.add("service_temp_max_c", ">=", numbers["service_temp_max_c"], "degC", " | ".join(service_lines),
              status="derived", note="теплостойкость ≥ T_max + запас %.0f °C (политика)" % POLICY["heat_margin_c"])

    mode_hint = None
    for source in (hint or "", text or ""):
        if re.search(r"\b5D\b", source):
            mode_hint = "5D"
            break
        if re.search(r"\b3D\b", source):
            mode_hint = "3D"
            break

    props_now = set(r["property"] for r in c.items)
    if numbers.get("rpm", 0) >= 300:
        # У эластичного чехла обороты задают частоту деформаций, а не ротор под балансировку.
        flags.add("cyclic_deformation" if "elasticity" in props_now else "rotor")
    if any(k in numbers for k in ("torque_nm", "torque_peak_nm", "load_n")) or numbers.get("pressure_bar"):
        flags.add("load_bearing")

    props = set(r["property"] for r in c.items)
    missing = []
    if "gear_teeth" in flags:
        missing.append("модуль, число зубьев и допуск зацепления (для расчёта зуба по Lewis и компенсации)")
    if "load_bearing" in flags:
        missing.append("закрепление и контакт (без них напряжения по STL не определены, раздел 6)")
    if "sealing" in props:
        missing.append("давление, среда и допустимая утечка для герметичности")
    if "balance_gmm" in props or "balance" in props:
        missing.append("масса ротора и метод балансировки (класс G и г·мм не эквивалентны без массы и частоты)")
    if "friction_low" in props or "friction_high" in props:
        missing.append("пара трения и условия испытания (μ определяется парой, не материалом)")

    return {
        "requirements": c.items,
        "numbers": numbers,
        "flags": sorted(flags),
        "mode_hint": mode_hint,
        "missing_inputs": missing,
        "policy": dict(POLICY),
    }
