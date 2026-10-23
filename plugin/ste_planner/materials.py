# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Material catalog and three-valued screening (specification section 4).

Data: Stereotech wiki catalog (data/materials_stereotech.json, built by
tools/build_material_catalog.py), the manufacturer's selection memo
(data/material_selection_guide.json) and the colleague's reference records
(data/research/printing_constants_catalog.json, reference only).

Rules:
- every property keeps its sources; conflicting sources form an interval and
  the check uses the conservative bound (unknown if the sources straddle a limit);
- chemical resistance measured at room temperature is not accepted as proof at
  service temperatures above ROOM_TEMPERATURE_LIMIT_C;
- Tg, HDT and Vicat are separate criteria, none is replaced by another;
- unknown is never turned into pass, and ranking never hides a hard failure.
"""

import json
import os
import re

from .result import PASS, FAIL, UNKNOWN, tri_compare, is_number, EVIDENCE_DOCUMENT

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ROOM_TEMPERATURE_LIMIT_C = 30.0

# canonical conversion of catalog units
_UNIT_FACTORS = {"g/cm3": ("g/mm3", 0.001), "%": ("fraction", 0.01), "MPa": ("MPa", 1.0),
                 "degC": ("degC", 1.0), "kJ/m2": ("kJ/m2", 1.0), "Ohm/m": ("Ohm/m", 1.0)}

# substances of the Stereotech chemical table per requirement
CHEMICAL_GROUPS = {
    "ipa_resistance": ["Изопропанол"],
    "alcohol_wipe_resistance": ["Изопропанол", "Этанол 96%"],
    "alkali_resistance": ["Раствор: NaOH+вода 5%", "Водный раствор калийной щелочи, 10%"],
    "acid_salt_resistance": ["Раствор: HCl 2%", "H2SO4 2%", "Раствор: H3PO4 10%",
                             "Раствор: Уксусная кислота+ Вода 10%"],
    "oil_fuel_resistance": ["Бензин", "Дизельное топливо", "Нефть"],
    "ozone_resistance": ["Озон"],
    "hot_water_resistance": ["Горячая вода"],
}
GUIDE_CHEMICAL = {"alkali_resistance": "alkali", "acid_salt_resistance": "acids",
                  "oil_fuel_resistance": "hydrocarbons"}

# Policy thresholds (class P) used only for qualitative screening; documented in the trace.
POLICY = {
    "ductile_min_charpy_kj_m2": 10.0,
    "ductile_min_elongation": 0.05,
    "esd_max_resistance_ohm": 1e9,
}


class MaterialRecord(object):
    def __init__(self, data, guide=None):
        self.data = data
        self.id = data["id"]
        self.label = data.get("label") or self.id
        self.series = self.label.split()[0] if self.label else None
        self.slicer = data.get("slicer") or {}
        self.excluded = data.get("excluded")
        self.properties = data.get("properties") or {}
        self.process = data.get("process") or {}
        self.chemical = data.get("chemical_resistance")
        self.description = data.get("description") or {}
        self.conflicts = data.get("conflicts") or []
        self.guide = guide
        self.selectable = bool(self.slicer.get("file")) and not self.excluded

    @property
    def base_polymer(self):
        lab = self.label.upper()
        for base in ("ABSPA", "PA12", "PA6", "PETG", "TPU", "SEBS", "SBS", "ABS", "PC", "PP", "PLA", "PVA", "TPE",
                     "TPV"):
            if re.search(r"(?<![A-Z])" + base + r"(?![A-Z0-9])", lab.replace(" ", " ")):
                return base
        return (self.slicer.get("material_type") or "").upper() or None

    @property
    def filler(self):
        m = re.search(r"\b([GC])(\d+)\b", self.label)
        if not m:
            return None
        return {"type": "glass" if m.group(1) == "G" else "carbon", "mass_percent": float(m.group(2))}

    def sources(self, prop):
        return list(self.properties.get(prop) or [])

    def interval(self, prop):
        """Conservative (lo, hi) over all sources in canonical units, or None."""
        lows, highs, unit = [], [], None
        for rec in self.sources(prop):
            u = rec.get("unit")
            cu, f = _UNIT_FACTORS.get(u, (u, 1.0))
            unit = cu
            if rec.get("value") is not None:
                lows.append(rec["value"] * f)
                highs.append(rec["value"] * f)
            else:
                if rec.get("min") is not None:
                    lows.append(rec["min"] * f)
                if rec.get("max") is not None:
                    highs.append(rec["max"] * f)
        if not lows and not highs:
            return None
        lo = min(lows) if lows else None
        hi = max(highs) if highs else None
        return (lo, hi, unit)

    def service_window(self):
        """Operating temperature window; with conflicting sources the narrowest (safe) window."""
        mins, maxs = [], []
        for rec in self.sources("service_temperature"):
            if rec.get("min") is not None:
                mins.append(rec["min"])
            if rec.get("max") is not None:
                maxs.append(rec["max"])
        if not mins and not maxs:
            return None
        return (max(mins) if mins else None, min(maxs) if maxs else None)

    def text(self, prop):
        for rec in self.sources(prop):
            if rec.get("text"):
                return rec["text"]
        return None

    def process_window(self, key):
        rec = self.process.get(key) or {}
        if rec.get("value") is not None:
            return (rec["value"], rec["value"])
        if rec.get("min") is not None or rec.get("max") is not None:
            return (rec.get("min"), rec.get("max"))
        return None

    def in_guide(self, entries):
        """Membership in a list of memo entries (ids, 'series:X', 'base:X')."""
        mid = self.id.lower()
        for e in entries:
            e = e.lower()
            if e == mid:
                return True
            if e.startswith("series:"):
                words = e[7:].split()
                lab = self.label.lower().split()
                if lab[:len(words)] == words or (len(words) == 2 and lab[:1] == words[:1] and words[1] in lab):
                    return True
            if e.startswith("base:") and (self.base_polymer or "").lower() == e[5:]:
                return True
        return False

    def guide_class(self, section):
        if not self.guide:
            return None
        for cls, entries in sorted((self.guide.get(section) or {}).items()):
            if self.in_guide(entries):
                return cls
        return None

    def print_difficulty_rank(self):
        order = (self.guide or {}).get("print_difficulty_hardest_first") or []
        for i, e in enumerate(order):
            if self.in_guide([e]):
                return i + 1  # list is hardest first: larger = easier
        return 0

    def to_summary(self):
        return {"id": self.id, "label": self.label, "series": self.series, "base_polymer": self.base_polymer,
                "filler": self.filler, "slicer_file": self.slicer.get("file"), "wiki_url": self.data.get("wiki_url"),
                "selectable": self.selectable, "excluded": self.excluded}


class MaterialCatalog(object):
    def __init__(self, records, guide=None, references=None):
        self.records = records
        self.guide = guide
        self.references = references or []

    def get(self, mid):
        for r in self.records:
            if r.id.lower() == mid.lower() or r.label.lower() == mid.lower():
                return r
        return None

    def selectable(self):
        return [r for r in self.records if r.selectable]


def load_catalog(path=None, guide_path=None, reference_path=None):
    path = path or os.path.join(_DATA, "materials_stereotech.json")
    guide_path = guide_path or os.path.join(_DATA, "material_selection_guide.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    guide = None
    if os.path.exists(guide_path):
        with open(guide_path, encoding="utf-8") as f:
            guide = json.load(f)
    refs = []
    if reference_path and os.path.exists(reference_path):
        with open(reference_path, encoding="utf-8") as f:
            refs = json.load(f).get("material_records", [])
    return MaterialCatalog([MaterialRecord(m, guide) for m in data["materials"]], guide, refs)


# ---------------------------------------------------------------- printability

def printable(material, machine):
    """Hardware compatibility: filament diameter, temperature windows, nozzle material.

    machine: {"filament_diameter": 1.75, "max_nozzle_temperature": ..., "max_bed_temperature": ...,
              "nozzle_diameters": [...], "hardened_nozzle": True/False/None, "heated_bed": bool}
    """
    checks = []
    if material.excluded:
        checks.append(("route", FAIL, material.excluded))
    d_mat = material.slicer.get("diameter")
    d_machine = machine.get("filament_diameter")
    if is_number(d_mat) and is_number(d_machine):
        ok = abs(d_mat - d_machine) < 0.05
        checks.append(("filament_diameter", PASS if ok else FAIL,
                       "material %.2f mm vs extruder %.2f mm" % (d_mat, d_machine)))
    else:
        checks.append(("filament_diameter", UNKNOWN, "diameter unknown"))
    noz = material.process_window("nozzle_temperature")
    tmax = machine.get("max_nozzle_temperature")
    if noz and noz[0] is not None and is_number(tmax):
        checks.append(("nozzle_temperature", PASS if noz[0] <= tmax else FAIL,
                       "window from %s C, hotend max %s C" % (noz[0], tmax)))
    nd = material.process_window("nozzle_diameter")
    sizes = machine.get("nozzle_diameters") or []
    if nd and nd[0] is not None and sizes:
        ok = any(s >= nd[0] - 1e-9 for s in sizes)
        if ok:
            checks.append(("nozzle_diameter", PASS, "needs >= %s mm, available %s" % (nd[0], sizes)))
        elif machine.get("nozzle_change_possible", True):
            checks.append(("nozzle_diameter", UNKNOWN,
                           "needs nozzle >= %s mm, installed %s: nozzle change required" % (nd[0], sizes)))
        else:
            checks.append(("nozzle_diameter", FAIL, "needs >= %s mm, available %s" % (nd[0], sizes)))
    nm = (material.process.get("nozzle_material") or {}).get("text", "")
    if re.search(r"закал|сталь", nm, re.I):
        hard = machine.get("hardened_nozzle")
        checks.append(("nozzle_material", PASS if hard else (FAIL if hard is False else UNKNOWN),
                       "requires hardened steel nozzle"))
    status = PASS
    if any(c[1] == FAIL for c in checks):
        status = FAIL
    elif any(c[1] == UNKNOWN for c in checks):
        status = UNKNOWN
    return {"status": status, "checks": [{"check": c[0], "status": c[1], "note": c[2]} for c in checks]}


def thermal_window(material, machine=None, profile=None):
    """T in window_material ∩ window_hotend ∩ window_profile (calibration window unknown)."""
    win = material.process_window("nozzle_temperature")
    if not win:
        return {"status": UNKNOWN, "window": None, "note": "no material window"}
    lo, hi = win
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    if machine and is_number(machine.get("max_nozzle_temperature")):
        hi = min(hi, machine["max_nozzle_temperature"])
    if profile and is_number(profile.get("print_temperature")):
        p = profile["print_temperature"]
        inside = lo <= p <= hi
    else:
        inside = None
    if lo > hi:
        return {"status": FAIL, "window": None, "note": "empty intersection"}
    return {"status": PASS, "window": (lo, hi), "profile_value_inside": inside,
            "note": "calibration window not available; profile value preferred"}


# ---------------------------------------------------------------- screening

def _check(req, status, evidence, note, value=None, source=None, basis="direct"):
    return {"requirement_id": req.id, "property": req.property, "status": status, "evidence": evidence,
            "note": note, "material_value": value, "source": source, "basis": basis,
            "mandatory": req.mandatory}


def _numeric(req, material, prop, note_extra=""):
    iv = material.interval(prop)
    if iv is None:
        return _check(req, UNKNOWN, None, "no data for %s%s" % (prop, note_extra))
    lo, hi, unit = iv
    lo = lo if lo is not None else hi
    hi = hi if hi is not None else lo
    status = req.check((lo, hi) if lo != hi else lo)
    srcs = sorted(set(r.get("source") for r in material.sources(prop)))
    note = "typical manufacturer value(s) %s %s%s" % (
        ("%g" % lo) if lo == hi else ("%g..%g" % (lo, hi)), unit or "", note_extra)
    if lo != hi:
        note += "; sources disagree, conservative bound used"
    return _check(req, status, EVIDENCE_DOCUMENT, note, (lo, hi), srcs)


# Keywords of manufacturer text claims per requirement (advantages / disadvantages lists).
TEXT_CLAIMS = {
    "ipa_resistance": r"спирт",
    "alcohol_wipe_resistance": r"спирт",
    "alkali_resistance": r"щелоч",
    "acid_salt_resistance": r"кислот|сол[ея]й|растворам\s+солей",
    "oil_fuel_resistance": r"масл|гсм|бензин|дизел|углеводород",
    "ozone_resistance": r"озон",
}


def text_claim(material, prop):
    """+1 if the manufacturer's advantages claim resistance, -1 if disadvantages deny it, else 0."""
    rx = TEXT_CLAIMS.get(prop)
    if not rx:
        return 0
    adv = " ".join(material.description.get("advantages") or []).lower()
    dis = " ".join(material.description.get("disadvantages") or []).lower()
    if re.search(r"не\s+стоек[^;]*(" + rx + ")", dis):
        return -1
    if re.search(r"(устойчив|стоек|стойк)[^;]*(" + rx + ")", adv):
        return 1
    return 0


def _chemical(req, material, service_temp_max):
    subs = CHEMICAL_GROUPS.get(req.property, [])
    statuses = []
    if material.chemical:
        vals = material.chemical["values"]
        for s in subs:
            statuses.append(vals.get(s, "no_data"))
    guide_key = GUIDE_CHEMICAL.get(req.property)
    in_guide = guide_key and material.guide and material.in_guide(material.guide["chemical"].get(guide_key, []))
    match = material.chemical["match"] if material.chemical else None
    if any(s == "not_resistant" for s in statuses):
        return _check(req, FAIL, EVIDENCE_DOCUMENT, "not resistant to %s (Stereotech table, row %s)" %
                      (", ".join(s for s, v in zip(subs, statuses) if v == "not_resistant"),
                       material.chemical["row"]), statuses, "chemical_table", basis=match)
    proven = statuses and all(s == "resistant" for s in statuses) and match == "label"
    hint = (statuses and all(s in ("resistant", "theoretically_resistant") for s in statuses)) or in_guide
    if proven or (hint and match != "related_polymer_PET_not_PETG"):
        if service_temp_max is not None and service_temp_max > ROOM_TEMPERATURE_LIMIT_C:
            c = _check(req, UNKNOWN, EVIDENCE_DOCUMENT,
                       "resistant at room temperature only; service up to %g C needs a test" % service_temp_max,
                       statuses, "chemical_table", basis=match)
            c["hint"] = True
            return c
        status = PASS if proven else UNKNOWN
        note = "resistant per Stereotech table" if proven else "indicated resistant (theoretical/base polymer/memo)"
        c = _check(req, status, EVIDENCE_DOCUMENT, note, statuses, "chemical_table", basis=match)
        c["hint"] = not proven
        return c
    claim = text_claim(material, req.property)
    if claim > 0:
        c = _check(req, UNKNOWN, EVIDENCE_DOCUMENT, "manufacturer text claims resistance; no table data for %s" %
                   ", ".join(subs), statuses, "wiki_text", basis="text_claim")
        c["hint"] = True
        return c
    return _check(req, UNKNOWN, None, "no chemical data for %s" % ", ".join(subs), statuses, basis=match)


def screen_material(material, reqset, machine=None):
    """Per-requirement pass/fail/unknown for one material."""
    checks = []
    svc = None
    for r in reqset.requirements:
        if r.property == "service_temperature" and isinstance(r.value, tuple):
            svc = r.value if svc is None else (min(svc[0], r.value[0]), max(svc[1], r.value[1]))
        elif r.property == "service_temperature" and is_number(r.value):
            svc = (r.value, r.value) if svc is None else (min(svc[0], r.value), max(svc[1], r.value))
    svc_max = svc[1] if svc else None
    for r in reqset.requirements:
        p = r.property
        if p == "service_temperature":
            win = material.service_window()
            if not win or win[1] is None:
                checks.append(_check(r, UNKNOWN, None, "no operating temperature data"))
                continue
            lo = win[0] if win[0] is not None else -273.0
            status = tri_compare(r.value, "in", (lo, win[1])) if isinstance(r.value, tuple) else \
                tri_compare(r.value, "in", (lo, win[1]))
            checks.append(_check(r, status, EVIDENCE_DOCUMENT,
                                 "manufacturer operating range %g..%g C" % (lo, win[1]), win, "wiki"))
        elif p == "no_softening_temperature":
            win = material.service_window()
            vic = material.interval("vicat_softening_temperature")
            if win and win[1] is not None:
                status = tri_compare(win[1], ">=", r.value)
                note = "operating max %g C" % win[1]
                if vic and status != FAIL:
                    note += ", Vicat %g C" % vic[0]
                checks.append(_check(r, status, EVIDENCE_DOCUMENT, note, win[1], basis="operating_range"))
            else:
                checks.append(_check(r, UNKNOWN, None, "no operating temperature data"))
        elif p == "glass_transition_temperature":
            checks.append(_numeric(r, material, "glass_transition_temperature",
                                   "; Tg is not replaced by Vicat/HDT"))
        elif p == "vicat_softening_temperature":
            checks.append(_numeric(r, material, "vicat_softening_temperature"))
        elif p == "tensile_modulus":
            checks.append(_numeric(r, material, "tensile_modulus_xy", " (XY direction)"))
        elif p == "tensile_strength":
            checks.append(_numeric(r, material, "tensile_strength_xy", " (XY direction)"))
        elif p == "elongation_at_break":
            checks.append(_numeric(r, material, "elongation_xy", " (XY direction)"))
        elif p == "density":
            checks.append(_numeric(r, material, "density"))
        elif p == "water_absorption_24h":
            checks.append(_numeric(r, material, "water_absorption"))
        elif p in CHEMICAL_GROUPS:
            checks.append(_chemical(r, material, svc_max))
        elif p == "uv_resistance" or p == "uv_color_stability":
            uv = material.text("uv_resistance")
            in_memo = material.guide and material.in_guide(material.guide["chemical"]["uv"])
            if uv and re.search(r"не\s+стойк", uv, re.I):
                checks.append(_check(r, FAIL, EVIDENCE_DOCUMENT, "UV: %s" % uv, uv))
            elif p == "uv_color_stability":
                checks.append(_check(r, UNKNOWN, EVIDENCE_DOCUMENT if uv or in_memo else None,
                                     "UV resistant per manufacturer, colour stability needs ΔE criterion" if
                                     (uv or in_memo) else "no UV data"))
            elif (uv and re.search(r"стойк", uv, re.I)) or in_memo:
                checks.append(_check(r, PASS, EVIDENCE_DOCUMENT, "UV resistant per manufacturer", uv))
            else:
                checks.append(_check(r, UNKNOWN, None, "no UV data"))
        elif p == "hydrophobic":
            cls = material.guide_class("moisture_uptake")
            status = {"low": PASS, "high": FAIL}.get(cls, UNKNOWN)
            checks.append(_check(r, status, EVIDENCE_DOCUMENT if cls else None,
                                 "moisture uptake class: %s (Stereotech memo)" % cls, cls, basis="memo"))
        elif p == "abrasion_resistance":
            cls = material.guide_class("abrasion")
            status = {"high": PASS, "low": FAIL}.get(cls, UNKNOWN)
            checks.append(_check(r, status, EVIDENCE_DOCUMENT if cls else None,
                                 "abrasion class: %s (memo); wear in the real pair needs a test" % cls, cls,
                                 basis="memo"))
        elif p == "elasticity_over_range":
            cls = material.guide_class("stiffness")
            status = {"soft_elastic": PASS, "hard_rigid": FAIL, "medium_flexible": FAIL,
                      "soft_inelastic": FAIL}.get(cls, UNKNOWN)
            win = material.service_window()
            if status == PASS and svc and win and win[0] is not None and svc[0] < win[0]:
                status = FAIL
            checks.append(_check(r, status, EVIDENCE_DOCUMENT if cls else None, "stiffness class: %s" % cls, cls,
                                 basis="memo"))
        elif p == "high_modulus":
            cls = material.guide_class("stiffness")
            status = {"hard_rigid": PASS, "soft_elastic": FAIL, "soft_inelastic": FAIL}.get(cls, UNKNOWN)
            checks.append(_check(r, status, EVIDENCE_DOCUMENT if cls else None,
                                 "stiffness class: %s; tooth deflection needs a load model" % cls, cls, basis="memo"))
        elif p in ("ductile_failure", "impact_resistance", "toughness"):
            ch = material.interval("charpy_notched")
            el = material.interval("elongation_xy")
            in_memo = material.guide and material.in_guide(material.guide["strength"]["impact"])
            if ch and ch[0] is not None:
                status = tri_compare((ch[0], ch[1]), ">=", POLICY["ductile_min_charpy_kj_m2"])
                note = "Charpy notched %s kJ/m2 vs policy %g" % (_fmt(ch), POLICY["ductile_min_charpy_kj_m2"])
            elif el and el[0] is not None:
                status = tri_compare((el[0], el[1]), ">=", POLICY["ductile_min_elongation"])
                note = "elongation %s vs policy %g" % (_fmt(el), POLICY["ductile_min_elongation"])
            elif in_memo:
                status, note = PASS, "impact resistant per Stereotech memo"
            else:
                status, note = UNKNOWN, "no impact data"
            if status == PASS:
                status = UNKNOWN if p == "ductile_failure" else PASS
                note += "; fracture mode needs a test" if p == "ductile_failure" else ""
            checks.append(_check(r, status, EVIDENCE_DOCUMENT, note, basis="policy_threshold"))
        elif p == "esd":
            res = material.interval("electrical_resistance")
            if res and res[0] is not None:
                status = tri_compare(res[0], "<=", POLICY["esd_max_resistance_ohm"])
                checks.append(_check(r, status, EVIDENCE_DOCUMENT, "resistance %g Ohm/m" % res[0], res[0]))
            else:
                checks.append(_check(r, UNKNOWN, None, "no electrical data"))
        elif p == "galvanic_inert":
            f = material.filler
            if f and f["type"] == "carbon":
                checks.append(_check(r, FAIL, EVIDENCE_DOCUMENT, "conductive carbon filler", f))
            else:
                checks.append(_check(r, PASS, EVIDENCE_DOCUMENT, "polymer without conductive filler", f))
        elif p == "short_contact_temperature":
            vic = material.interval("vicat_softening_temperature")
            st = [x.get("short_term_max") for x in material.sources("service_temperature") if x.get("short_term_max")]
            refs = [v for v in ([vic[0]] if vic else []) + st if v is not None]
            ref = max(refs) if refs else None
            c = _check(r, UNKNOWN, EVIDENCE_DOCUMENT if ref else None,
                       "short contact %g C: Vicat/short-term limit %s C is only an indicator; drop test required" %
                       (r.upper, ref), ref, basis="indicator")
            c["hint"] = ref is not None and ref >= r.upper
            checks.append(c)
        elif p == "high_friction_vs_paper":
            cls = material.guide_class("stiffness")
            c = _check(r, UNKNOWN, None, "friction of the pair needs a test; elastomer class used only as a hint",
                       cls, basis="indicator")
            c["hint"] = cls == "soft_elastic"
            checks.append(c)
        elif p == "no_visible_layers":
            txt = " ".join([material.description.get("summary") or ""] +
                           (material.description.get("advantages") or [])).lower()
            c = _check(r, UNKNOWN, None, r.note or "requires an observation criterion", basis="indicator")
            c["hint"] = bool(re.search(r"без\s+ярко\s+выраженного\s+перехода\s+между\s+слоями", txt))
            if c["hint"]:
                c["note"] = "manufacturer: surface without pronounced layer transitions; criterion still needed"
                c["evidence"] = EVIDENCE_DOCUMENT
            checks.append(c)
        elif p == "sealing":
            cls = material.guide_class("stiffness")
            checks.append(_check(r, UNKNOWN, None, "leak tightness needs pressure/medium criteria and a test",
                                 cls))
        elif r.kind == "qualitative":
            checks.append(_check(r, UNKNOWN, None, r.note or "requires a dedicated test"))
    return checks


def _fmt(iv):
    lo, hi = iv[0], iv[1]
    return ("%g" % lo) if lo == hi or hi is None else ("%g..%g" % (lo, hi))


# Part type (from the part name) -> keywords of the manufacturer's application lists.
PART_TYPES = [
    ("pump_impeller", r"насос", r"насос|крыльчатк"),
    ("fan_impeller", r"крыльчатк|вентилятор", r"крыльчатк|вентилятор"),
    ("gear", r"шестерн|зубчат", r"шестер|зубчат|механически\s+нагруж"),
    ("wheel_roller", r"колес|ролик", r"колёс|колес|ролик"),
    ("boot_seal", r"пыльник|чехол|уплотн|мембран", r"уплотн|мембран|гибк\w+\s+элемент|гибк\w+\s+издел"),
    ("shaft_cam", r"\bвал\b|\bось\b|копир|кулач", r"втулк|направляющ|\bвал|износ"),
    ("decorative", r"декоратив|фигурк|сувенир", r"лицев|декоратив|визуальн"),
]


def part_type(reqset):
    name = (reqset.part_name or "").lower()
    for t, rx, _ in PART_TYPES:
        if re.search(rx, name):
            return t
    return None


def application_match(material, ptype):
    """Weight 0..1 of the manufacturer listing this kind of part among the material's applications."""
    if not ptype:
        return False
    if ptype == "decorative" and material.guide and material.in_guide(material.guide["part_type"]["decorative"]):
        return 1.0
    rx = dict((t, kw) for t, _, kw in PART_TYPES)[ptype]
    apps = " ".join(material.description.get("applications") or []).lower()
    if not re.search(rx, apps):
        return 0.0
    # decorative use mentioned only as "also face parts" is weaker than the memo's explicit choice
    return 0.5 if ptype == "decorative" else 1.0


def summarize_checks(checks):
    mand = [c for c in checks if c["mandatory"]]
    return {
        "mandatory_fail": [c["requirement_id"] for c in mand if c["status"] == FAIL],
        "mandatory_unknown": [c["requirement_id"] for c in mand if c["status"] == UNKNOWN],
        "mandatory_pass": [c["requirement_id"] for c in mand if c["status"] == PASS],
        "optional_fail": [c["requirement_id"] for c in checks if not c["mandatory"] and c["status"] == FAIL],
        "optional_pass": [c["requirement_id"] for c in checks if not c["mandatory"] and c["status"] == PASS],
        "hinted_unknown": [c["requirement_id"] for c in mand if c["status"] == UNKNOWN and c.get("hint")],
    }


def screen_materials(reqset, catalog, machine=None, preferred=None):
    """Screen all selectable materials; hard failures are never offset by other criteria.

    Ranking among materials without a mandatory failure (policy P, documented):
    fewer mandatory unknowns, more mandatory passes, more optional passes, fewer
    optional fails, easier to print per the Stereotech memo, then id for determinism.
    `preferred` (e.g. material named in the request) moves a feasible material first.
    """
    machine = machine or {}
    evaluated = []
    rejected = []
    ptype = part_type(reqset)
    preferred_l = [p.lower() for p in (preferred or [])]
    for m in catalog.records:
        if not m.slicer.get("file"):
            continue
        if not m.data.get("wiki_url") and m.id.lower() not in preferred_l:
            rejected.append({"material": m.to_summary(), "reason": "no manufacturer property data"})
            continue
        pr = printable(m, machine) if machine else {"status": UNKNOWN, "checks": []}
        if pr["status"] == FAIL:
            rejected.append({"material": m.to_summary(), "reason": "not printable", "printable": pr})
            continue
        checks = screen_material(m, reqset, machine)
        summ = summarize_checks(checks)
        entry = {"material": m.to_summary(), "printable": pr, "checks": checks, "summary": summ,
                 "application_match": application_match(m, ptype)}
        entry["support"] = (len(summ["mandatory_pass"]) + 0.5 * len(summ["hinted_unknown"]) +
                            entry["application_match"] + 0.25 * len(summ["optional_pass"]))
        if summ["mandatory_fail"]:
            entry["reason"] = "mandatory requirement failed"
            rejected.append(entry)
        else:
            evaluated.append(entry)

    def key(e):
        s = e["summary"]
        mid = e["material"]["id"]
        rec = catalog.get(mid)
        pref = 0 if mid.lower() in preferred_l else 1
        printable_unknown = 1 if e["printable"]["status"] == UNKNOWN else 0
        return (pref, -e["support"], len(s["mandatory_unknown"]), len(s["optional_fail"]), printable_unknown,
                -(rec.print_difficulty_rank() if rec else 0), mid)

    evaluated.sort(key=key)
    rejected.sort(key=lambda e: (len(e.get("summary", {}).get("mandatory_fail", [])) or 99, e["material"]["id"]))
    return {"ranked": evaluated, "rejected": rejected, "part_type": ptype,
            "policy": ("hard constraints first (any mandatory fail rejects); ranking by support = passes + "
                       "0.5*hinted unknowns + 1*manufacturer application match + 0.25*optional passes, then "
                       "fewer unknowns, printability, print difficulty (Stereotech memo)")}
