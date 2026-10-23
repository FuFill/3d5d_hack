# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Structuring of technical requirements (specification section 3).

parse_requirements() is a deterministic extractor: every number is taken from
the source text together with the fragment it came from. Interpretations
proposed by an LLM are merged with merge_llm_requirements(), which refuses
numbers that do not occur in the cited fragment.
"""

import re

from . import units
from .result import (PASS, FAIL, UNKNOWN, tri_compare, is_number)

EXPLICIT = "explicit"
DERIVED = "derived"
ASSUMED = "assumed"
UNKNOWN_INTERPRETATION = "unknown"

KIND_CONDITION = "condition"      # service condition, an input of models
KIND_CONSTRAINT = "constraint"    # verifiable limit
KIND_QUALITATIVE = "qualitative"  # needs an observation criterion or test


class Requirement(object):
    def __init__(self, id, property, comparator, value, unit, kind=KIND_CONSTRAINT, region=None,
                 load_case=None, environment=None, duration=None, cycles=None, mandatory=True,
                 provenance=None, interpretation_status=EXPLICIT, note=None, conditions=None):
        self.id = id
        self.property = property
        self.comparator = comparator
        self.value = value          # float, (lo, hi) or True for qualitative
        self.unit = unit
        self.kind = kind
        self.region = region
        self.load_case = load_case
        self.environment = environment
        self.duration = duration
        self.cycles = cycles
        self.mandatory = mandatory
        self.provenance = provenance or {}
        self.interpretation_status = interpretation_status
        self.note = note
        self.conditions = conditions or {}

    def to_dict(self):
        d = dict(self.__dict__)
        if isinstance(self.value, tuple):
            d["value"] = list(self.value)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        if isinstance(d.get("value"), list):
            d["value"] = tuple(d["value"])
        return cls(**d)

    @property
    def lower(self):
        return self.value[0] if isinstance(self.value, tuple) else self.value

    @property
    def upper(self):
        return self.value[1] if isinstance(self.value, tuple) else self.value

    def check(self, measured):
        """Three-valued check of a measured/predicted value (scalar or interval)."""
        if self.kind == KIND_QUALITATIVE:
            if measured is None:
                return UNKNOWN
            return PASS if measured is True else FAIL
        if self.comparator == "abs_dev_le":
            return tri_compare(measured, "<=", self.value)
        if self.comparator in (">=", "<=", "in"):
            # An interval limit ("not lower than 85-90 degC") is itself uncertain: the check passes
            # only against its strict end and fails only against its lenient end.
            return tri_compare(measured, self.comparator, self.value)
        return UNKNOWN

    def __repr__(self):
        return "Requirement(%s %s %s %r %s)" % (self.id, self.property, self.comparator, self.value, self.unit)


class RequirementSet(object):
    def __init__(self, requirements=None, part_name=None, source=None, text=None):
        self.requirements = list(requirements or [])
        self.part_name = part_name
        self.source = source
        self.text = text

    def by_property(self, prop):
        return [r for r in self.requirements if r.property == prop]

    def first(self, prop, region=None, load_case=None):
        for r in self.requirements:
            if r.property != prop:
                continue
            if region is not None and r.region != region:
                continue
            if load_case is not None and r.load_case != load_case:
                continue
            return r
        return None

    def has(self, prop):
        return any(r.property == prop for r in self.requirements)

    def to_dict(self):
        return {"part_name": self.part_name, "source": self.source,
                "requirements": [r.to_dict() for r in self.requirements]}


# ---------------------------------------------------------------- quantities

_NUM = r"[+\-−]?\d+(?:[.,]\d+)?"
_RANGE_SEP = r"\s*(?:\.\.\.|…|–|—|-|÷)\s*"


class Quantity(object):
    def __init__(self, lo, hi, unit, comparator, start, end, text):
        self.lo, self.hi, self.unit, self.comparator = lo, hi, unit, comparator
        self.start, self.end, self.text = start, end, text

    @property
    def dimension(self):
        try:
            return units.dimension(self.unit)
        except units.UnitError:
            return None

    def value(self):
        return self.lo if self.lo == self.hi else (self.lo, self.hi)

    def canonical(self):
        v, u = units.to_canonical(self.value(), self.unit)
        return v, u


_UPPER_WORDS = r"(?:не\s+более|не\s+превыша\w*|не\s+тяжелее|не\s+выше|до|максимум|max|≤|<=|<)"
_LOWER_WORDS = r"(?:не\s+менее|не\s+ниже|не\s+меньше|от|минимум|min|≥|>=|>)"


def find_quantities(line):
    """Find number(+range)(+unit) expressions in a line, with bound words."""
    out = []
    low = line.lower()
    pos = 0
    num_re = re.compile(_NUM)
    while True:
        m = num_re.search(line, pos)
        if not m:
            break
        start, end = m.start(), m.end()
        # Skip identifiers such as G2.5, MoS2, ISO 1940, Class 8, 2-полюсного.
        prev = line[start - 1] if start > 0 else " "
        if prev.isalpha() and m.group(0)[0] not in "+-−":
            pos = end
            continue
        lo = hi = units.parse_number(m.group(0))
        sym = None
        # symmetric tolerance: "± 0.1 мм"
        before = line[max(0, start - 3):start]
        if "±" in before:
            sym = True
        unit1, ulen1 = units.match_unit(line[end:])
        cur = end + ulen1
        comparator = None
        # range "a – b unit" / "a unit ... b unit"
        r = re.match(_RANGE_SEP + "(" + _NUM + ")", line[cur:])
        if r and not sym:
            hi = units.parse_number(r.group(1))
            cur2 = cur + r.end()
            unit2, ulen2 = units.match_unit(line[cur2:])
            if unit2 is not None and (unit1 is None or unit1 == unit2):
                unit1 = unit2
                cur = cur2 + ulen2
                comparator = "in"
            elif unit1 is not None and unit2 is None:
                cur = cur2
                comparator = "in"
            else:
                hi = lo
        # "от a до b"
        if comparator is None:
            r2 = re.match(r"\s*(?:\S+\s+){0,2}?до\s+(" + _NUM + ")", line[cur:], re.IGNORECASE)
            if r2 and re.search(r"от\s*$", low[:start]):
                hi = units.parse_number(r2.group(1))
                cur2 = cur + r2.end()
                unit2, ulen2 = units.match_unit(line[cur2:])
                if unit2 is not None:
                    unit1 = unit1 or unit2
                    cur = cur2 + ulen2
                else:
                    cur = cur2
                comparator = "in"
        # "c unit ± d unit"
        if comparator is None:
            r3 = re.match(r"\s*±\s*(" + _NUM + ")", line[cur:])
            if r3 and not sym:
                dev = abs(units.parse_number(r3.group(1)))
                cur2 = cur + r3.end()
                unit2, ulen2 = units.match_unit(line[cur2:])
                unit1 = unit1 or unit2
                cur = cur2 + ulen2
                lo, hi = lo - dev, lo + dev
                comparator = "in"
        if sym:
            comparator = "abs_dev_le"
            lo = hi = abs(lo)
        if comparator is None:
            head = low[max(0, start - 25):start]
            if re.search(_UPPER_WORDS + r"\s*[~≈]?\s*$", head):
                comparator = "<="
            elif re.search(_LOWER_WORDS + r"\s*[~≈]?\s*$", head):
                comparator = ">="
            else:
                comparator = "=="
        if unit1 is not None:
            if lo > hi:
                lo, hi = hi, lo
            out.append(Quantity(lo, hi, unit1, comparator, start, cur, line[start:cur]))
        pos = max(cur, end)
    return out


# ---------------------------------------------------------------- rules

# (property, keyword regex, dimension, kind, default comparator or None to keep the parsed one)
_NUMERIC_RULES = [
    ("glass_transition_temperature", r"стеклован|\btg\b", "temperature", KIND_CONSTRAINT, ">="),
    ("vicat_softening_temperature", r"вика|vicat", "temperature", KIND_CONSTRAINT, ">="),
    ("short_contact_temperature", r"клея|расплав", "temperature", KIND_CONDITION, "<="),
    ("no_softening_temperature", r"размягча", "temperature", KIND_CONSTRAINT, ">="),
    ("tensile_modulus", r"модуль\s+упругости", "stress", KIND_CONSTRAINT, ">="),
    ("tensile_strength", r"предел\s+прочности|прочност\w*\s+при\s+растяжении", "stress", KIND_CONSTRAINT, ">="),
    ("elongation_at_break", r"удлинени", "ratio", KIND_CONSTRAINT, ">="),
    ("water_absorption_24h", r"водопоглощ", "ratio", KIND_CONSTRAINT, "<="),
    ("density", r"плотност|тяжелее|легким", "density", KIND_CONSTRAINT, "<="),
    ("residual_unbalance", r"дисбаланс", "unbalance", KIND_CONSTRAINT, "<="),
    ("blade_thickness_variation", r"разностенност", "length", KIND_CONSTRAINT, "<="),
    ("service_humidity", r"влажност", "ratio", KIND_CONDITION, None),
    ("rotation_speed", r"об\s*/?\s*мин|скорост\w*\s+вращени", "angular_speed", KIND_CONDITION, None),
    ("peripheral_speed", r"окружн|скорость\s+линии", "speed", KIND_CONDITION, None),
    ("torque", r"момент", "torque", KIND_CONDITION, None),
    ("radial_load", r"радиальн\w*\s+нагрузк", None, KIND_CONDITION, "<="),
    ("transported_mass", r"масса\s+одной", "mass", KIND_CONDITION, None),
    ("operating_hours", r"моточас|часов\s+в\s+год|режим\s+работы", "time", KIND_CONDITION, None),
    ("bend_angle", r"угол|изгиб", "angle", KIND_CONDITION, "<="),
    ("axial_travel", r"осевой\s+ход", "length", KIND_CONDITION, "abs_dev_le"),
    ("pressure", r"давлени", "stress", KIND_CONDITION, "<="),
    ("flow_rate", r"производительност", "flow", KIND_CONDITION, None),
    ("pump_head", r"напор", "length", KIND_CONDITION, "<="),
    ("abrasive_particle_size", r"частиц", "length", KIND_CONDITION, "<="),
    ("service_temperature", r"температур|°\s*c|°\s*с", "temperature", KIND_CONDITION, "in"),
]

_TOLERANCE_REGIONS = [
    (r"отверсти", "bore"),
    (r"кулач|контур|профил", "cam_profile"),
]

# Qualitative requirements: (property, regex, note)
_QUALITATIVE_RULES = [
    ("hydrophobic", r"гидрофоб", "Нужен критерий: водопоглощение/равновесная влажность в условиях эксплуатации"),
    ("uv_resistance", r"ультрафиолет|солнечн\w*\s+луч|уф-стабильн|\bуф\b", "Нужны данные старения UV для SKU"),
    ("uv_color_stability", r"желтеть", "Нужен критерий цвета (ΔE) и режим старения"),
    ("high_modulus", r"высокий\s+модуль", "Нужна допускаемая деформация зуба"),
    ("esd", r"\besd\b|антистатич\w*\s+\(esd\)|антистатическими\s+\(esd\)", "Нужен диапазон поверхностного сопротивления"),
    ("sealing", r"герметич", "Нужны давление, среда, температура, допустимая утечка и время"),
    ("elasticity_over_range", r"эластичност", "Нужна допустимая жёсткость/усилие при крайних температурах"),
    ("oil_fuel_resistance", r"масло\s*и\s*бензостойк|бензин|масл\w+\s+туман|впитывать\s+масла", "Нужны данные химстойкости SKU к конкретной среде"),
    ("ipa_resistance", r"\bipa\b|изопропил", "Нужна стойкость к 70% IPA при режиме протирки"),
    ("alcohol_wipe_resistance", r"спиртовыми\s+салфетками", "Нужен состав салфеток и режим протирки"),
    ("no_visible_layers", r"видимых\s+сло", "Нужен критерий наблюдения (расстояние, освещение, Ra) или разрешение постобработки"),
    ("no_support_marks", r"следов\s+от\s+поддержек", "Нужна зона без контакта поддержек или разрешённая постобработка"),
    ("low_friction_vs_pom", r"низкий\s+коэффициент\s+трения", "μ определяется парой материалов; нужен трибологический тест по POM"),
    ("high_friction_vs_paper", r"высоким\s+коэффициентом\s+трения", "μ по бумаге/крафту требует испытания пары"),
    ("abrasion_resistance", r"абразивн\w+\s+износ|стойкость\s+к\s+абразив|микропыль|пыления", "Нужен тест износа в среде"),
    ("non_toxic", r"токсичн|пищев", "Допуск сырья не квалифицирует процесс печати"),
    ("low_voc", r"\bvoc\b|летучие\s+органическ", "Нужны данные эмиссии VOC"),
    ("no_marking", r"следов\s+пластификатор|черных\s+или\s+цветных\s+следов", "Нужно испытание на перенос материала"),
    ("ductile_failure", r"хрупк|осколк|раскалыват", "Нужны ударная вязкость/характер разрушения по испытанию"),
    ("ozone_resistance", r"озон", "Нужны данные озонового старения"),
    ("creep_resistance", r"ползучест|ползти", "Нужна модель ползучести при рабочей температуре"),
    ("alkali_resistance", r"щелоч|ph\s*\d", "Нужна стойкость к среде при рабочей температуре"),
    ("acid_salt_resistance", r"кислот|сол[еи]|хлорид", "Нужна стойкость к среде при рабочей температуре"),
    ("toughness", r"обладать\s+вязкостью|не\s+отламывались", "Нужна ударная вязкость тонких элементов"),
    ("cavitation_resistance", r"кавитац", "Нужно испытание кавитационного износа"),
    ("galvanic_inert", r"гальваническ|диэлектрик", "Исключить токопроводящие наполнители"),
    ("fatigue_resistance", r"усталост|циклическ\w+\s+нагрузк|постоянный\s+изгиб", "Нужна S–N зависимость материала/процесса"),
    ("impact_resistance", r"удар", "Нужна ударная вязкость в рабочих условиях"),
    ("gear_accuracy", r"эвольвент|шаг\s+зубьев", "Нужны модуль, число зубьев и класс точности"),
    ("no_play", r"люфт\s+недопустим", "Нужна посадка и ответная деталь"),
]

_PREFERENCE_RE = re.compile(r"желательно|нежелательно|предпочтительно", re.IGNORECASE)


def _split_lines(text):
    lines = []
    for raw in re.split(r"[\r\n]+", text):
        raw = raw.strip()
        if not raw:
            continue
        # keep sentences together but split very long paragraphs at sentence ends
        parts = re.split(r"(?<=[.!?])\s+(?=[А-ЯA-Z])", raw)
        lines.extend(p.strip() for p in parts if p.strip())
    return lines


def _load_case(line):
    low = line.lower()
    if re.search(r"пиков|стопор|в\s+пике|микроудар", low):
        return "peak"
    if re.search(r"номинальн", low):
        return "nominal"
    return None


def _region(line, part_name):
    low = line.lower()
    if "паз" in low:
        return "keyway"
    if re.search(r"зуб", low):
        return "teeth"
    if re.search(r"ступиц|клипс", low):
        return "hub"
    if re.search(r"лопаст|лопат", low):
        return "blades"
    if re.search(r"скручиван|на\s+ось", low):
        return "shaft"
    if re.search(r"отверсти", low):
        return "bore"
    return None


def parse_requirements(text, source=None, part_name=None):
    """Extract requirements with provenance from TZ text.

    Returns a RequirementSet. Every numeric requirement has interpretation
    'explicit' if the number and its unit come directly from the text and
    'derived' if a conversion with an assumption was needed (e.g. kg -> N).
    """
    lines = _split_lines(text)
    if part_name is None:
        for line in lines:
            m = re.match(r"наименование\s+детали\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                part_name = m.group(1).strip()
                break
    reqs = []
    counter = [0]

    def add(prop, comparator, value, unit, kind, line, **kw):
        counter[0] += 1
        prov = {"file": source, "fragment": line}
        reqs.append(Requirement("R%02d" % counter[0], prop, comparator, value, unit, kind=kind,
                                provenance=prov, **kw))

    for line in lines:
        low = line.lower()
        mandatory = not _PREFERENCE_RE.search(low)
        quantities = find_quantities(line)
        used = set()

        # tolerances first: "±0.1 мм" with a tolerance keyword
        if re.search(r"допуск|отклонени|точност", low):
            for i, q in enumerate(quantities):
                if q.comparator == "abs_dev_le" and q.dimension == "length":
                    region = None
                    for rx, reg in _TOLERANCE_REGIONS:
                        if re.search(rx, low):
                            region = reg
                            break
                    v, u = q.canonical()
                    add("dimensional_tolerance", "abs_dev_le", v, u, KIND_CONSTRAINT, line,
                        region=region, mandatory=mandatory)
                    used.add(i)

        m = re.search(r"\bG\s*(\d+(?:[.,]\d+)?)", line)
        if m and re.search(r"балансир|дисбаланс|iso\s*1940", low):
            add("balance_grade", "<=", units.parse_number(m.group(1)), "mm/s", KIND_CONSTRAINT, line,
                mandatory=mandatory, note="Класс G без массы и частоты не эквивалентен g·mm")

        m = re.search(r"парти\w*\s+из\s+(\d+)", low)
        if m:
            add("batch_quantity", "==", float(m.group(1)), "count", KIND_CONDITION, line)

        m = re.search(r"\bph\b[^0-9]{0,12}(\d+(?:[.,]\d+)?)(?:\s*[–-]\s*(\d+(?:[.,]\d+)?))?", low)
        if m:
            lo = units.parse_number(m.group(1))
            hi = units.parse_number(m.group(2)) if m.group(2) else lo
            add("chemical_ph", "in", (lo, hi) if hi != lo else lo, "pH", KIND_CONDITION, line)

        for prop, kw, dim, kind, comp in _NUMERIC_RULES:
            if not re.search(kw, low):
                continue
            for i, q in enumerate(quantities):
                if i in used:
                    continue
                qdim = q.dimension
                if prop == "radial_load":
                    if qdim not in ("mass", "force"):
                        continue
                elif qdim != dim:
                    continue
                if prop == "service_temperature" and re.search(r"стеклован|вика|клея|размягча|расплав", low):
                    continue
                if prop == "operating_hours" and q.unit != "h":
                    continue
                if prop == "rotation_speed" and q.unit != "rpm":
                    continue
                if prop == "density" and qdim != "density":
                    continue
                v, u = q.canonical()
                interp = EXPLICIT
                note = None
                comparator = comp or q.comparator
                if comp is None and q.comparator == "==" and isinstance(v, tuple):
                    comparator = "in"
                if comp in (">=", "<=") and q.comparator in (">=", "<=") and q.comparator != comp:
                    comparator = q.comparator
                if prop == "service_temperature" and not isinstance(v, tuple):
                    comparator = "<=" if q.comparator == "<=" else "=="
                if prop == "radial_load" and qdim == "mass":
                    mass = v
                    v = units.mass_to_weight(mass) if not isinstance(mass, tuple) else \
                        tuple(units.mass_to_weight(x) for x in mass)
                    u = "N"
                    interp = DERIVED
                    note = "Нагрузка задана массой %s g; принято F = m·g" % (mass,)
                extra = {}
                if kind == KIND_CONDITION and prop in ("torque", "radial_load"):
                    extra["load_case"] = _load_case(line) or ("peak" if q.comparator == "<=" else None)
                    extra["region"] = _region(line, part_name)
                if prop == "operating_hours":
                    extra["duration"] = v
                add(prop, comparator, v, u, kind, line, interpretation_status=interp, note=note,
                    mandatory=mandatory, **extra)
                used.add(i)
                if prop not in ("torque", "service_temperature", "rotation_speed", "radial_load"):
                    break

        for prop, rx, note in _QUALITATIVE_RULES:
            if re.search(rx, low):
                if any(r.property == prop for r in reqs):
                    continue
                add(prop, "qual", True, None, KIND_QUALITATIVE, line, mandatory=mandatory,
                    interpretation_status=UNKNOWN_INTERPRETATION, note=note)

    return RequirementSet(_dedupe(reqs), part_name=part_name, source=source, text=text)


def _dedupe(reqs):
    seen = set()
    out = []
    # A load restated without a load case duplicates the one with a case.
    cased = set((r.property, repr(r.value)) for r in reqs if r.load_case)
    for r in reqs:
        if r.kind == KIND_CONDITION and r.load_case is None and (r.property, repr(r.value)) in cased:
            continue
        key = (r.property, r.comparator, repr(r.value), r.region, r.load_case)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------- normalisation/validation

def normalize_requirements(reqset):
    """Convert all values to canonical units; raises units.UnitError on bad units."""
    for r in reqset.requirements:
        if r.kind == KIND_QUALITATIVE or r.unit in (None, "mm/s", "pH", "count"):
            continue
        if r.value is None:
            continue
        v, u = units.to_canonical(r.value, r.unit)
        r.value, r.unit = v, u
        if isinstance(r.value, tuple) and r.value[0] > r.value[1]:
            raise ValueError("Interval lower bound exceeds upper bound in %s" % r.id)
    return reqset


def validate_requirements(reqset):
    """Find contradictions; never mixes peak and nominal load cases."""
    issues = []
    groups = {}
    for r in reqset.requirements:
        if r.kind != KIND_CONSTRAINT or not (is_number(r.lower) and is_number(r.upper)):
            continue
        groups.setdefault((r.property, r.region, r.load_case), []).append(r)
    for key in sorted(groups, key=lambda k: tuple(str(x) for x in k)):
        lows = [r for r in groups[key] if r.comparator == ">="]
        highs = [r for r in groups[key] if r.comparator == "<="]
        for a in lows:
            for b in highs:
                if float(a.upper if isinstance(a.value, tuple) else a.value) > float(b.lower):
                    issues.append({"type": "contradiction", "ids": [a.id, b.id], "property": key[0]})
    return issues


# Mandatory additional data per requirement type (specification section 3).
REQUIRED_INPUTS = {
    "gear_torque": ["pitch_radius", "face_width", "module", "tooth_geometry", "contact", "load_spectrum", "fixture"],
    "hole_accuracy": ["nominal_diameter", "axis", "fit_length", "mating_part", "measurement_method"],
    "lifetime": ["load_history", "duration", "temperature", "environment", "allowed_failure_probability"],
    "sealing": ["pressure", "medium", "temperature", "max_leak_rate", "time"],
    "safe_rotation": ["axis", "speed", "mass", "center_of_mass", "balancing", "max_acceleration"],
    "elastic_boot": ["kinematics", "resistance_force", "bellows_geometry", "allowed_strain", "lifetime"],
}


def requirement_types(reqset):
    """Which analysis types are triggered by the requirement set."""
    types = []
    props = set(r.property for r in reqset.requirements)
    name = (reqset.part_name or "").lower()
    if "torque" in props and re.search(r"шестерн|зуб", name + " " + (reqset.text or "").lower()):
        types.append("gear_torque")
    if any(r.property == "dimensional_tolerance" and r.region == "bore" for r in reqset.requirements):
        types.append("hole_accuracy")
    if "operating_hours" in props or "fatigue_resistance" in props:
        types.append("lifetime")
    if "sealing" in props:
        types.append("sealing")
    if "rotation_speed" in props and ("residual_unbalance" in props or "peripheral_speed" in props):
        types.append("safe_rotation")
    if "bend_angle" in props and "axial_travel" in props:
        types.append("elastic_boot")
    return types


def missing_inputs(reqset, known=None):
    """List of missing mandatory inputs per triggered analysis type."""
    known = dict(known or {})
    present = set(known.keys())
    for r in reqset.requirements:
        if r.property == "rotation_speed":
            present.add("speed")
        if r.property in ("service_temperature",):
            present.add("temperature")
        if r.property == "operating_hours":
            present.update(["duration"])
        if r.property == "pressure":
            present.add("pressure")
        if r.property == "bend_angle":
            present.add("kinematics")
    out = []
    for t in requirement_types(reqset):
        for field in REQUIRED_INPUTS[t]:
            if field not in present:
                out.append({"requirement_type": t, "field": field})
    return out


# ---------------------------------------------------------------- LLM merge

def merge_llm_requirements(reqset, llm_items):
    """Merge LLM-proposed requirements.

    Each item must cite a fragment that occurs in the source text. Numbers in
    the item must occur in that fragment; otherwise the item is kept with
    interpretation 'assumed' and is not mandatory. Explicit numbers already
    extracted deterministically win over LLM numbers for the same property.
    """
    text = reqset.text or ""
    norm_text = re.sub(r"\s+", " ", text).lower()
    rejected = []
    next_id = len(reqset.requirements) + 1
    for item in llm_items:
        frag = (item.get("fragment") or "").strip()
        if not frag or re.sub(r"\s+", " ", frag).lower() not in norm_text:
            rejected.append({"item": item, "reason": "fragment not found in source"})
            continue
        prop = item.get("property")
        if not prop:
            rejected.append({"item": item, "reason": "no property"})
            continue
        value = item.get("value")
        interp = item.get("interpretation_status") or DERIVED
        mandatory = bool(item.get("mandatory", True))
        if value is not None and value is not True:
            vals = value if isinstance(value, (list, tuple)) else [value]
            frag_numbers = [units.parse_number(x) for x in re.findall(_NUM, frag)]
            raw = item.get("value_as_written")
            check_vals = raw if raw is not None else vals
            check_vals = check_vals if isinstance(check_vals, (list, tuple)) else [check_vals]
            if not all(any(abs(float(v) - fn) < 1e-9 for fn in frag_numbers) for v in check_vals):
                interp = ASSUMED
                mandatory = False
        if any(r.property == prop and r.interpretation_status == EXPLICIT and
               r.region == item.get("region") for r in reqset.requirements):
            rejected.append({"item": item, "reason": "explicit extraction exists"})
            continue
        value_t = tuple(value) if isinstance(value, list) else value
        unit = item.get("unit")
        if unit and value_t is not None and value_t is not True:
            value_t, unit = units.to_canonical(value_t, unit)
        reqset.requirements.append(Requirement(
            "L%02d" % next_id, prop, item.get("comparator", "qual"), value_t, unit,
            kind=item.get("kind", KIND_QUALITATIVE if value is True else KIND_CONSTRAINT),
            region=item.get("region"), load_case=item.get("load_case"), mandatory=mandatory,
            provenance={"file": reqset.source, "fragment": frag, "by": "llm"},
            interpretation_status=interp, note=item.get("note")))
        next_id += 1
    return rejected


def read_docx_text(path):
    """Paragraph text of a .docx file (zip + XML, no dependencies)."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    lines = []
    for p in paras:
        t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        t = (t.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&amp;", "&"))
        if t.strip():
            lines.append(t)
    return "\n".join(lines)
