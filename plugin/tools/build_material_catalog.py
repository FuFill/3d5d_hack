# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Build the material catalog from the Stereotech wiki cache and the slicer material files.

Usage:
  python build_material_catalog.py <cache_dir>/wiki_pages.json <slicer resources/materials> <out.json>

Every property keeps its source (material page or the general table) and raw
text. When two sources disagree both values are kept and the conflict is
listed; nothing is averaged. Values marked "(требуется уточнение)" get the
flag needs_clarification.
"""

import html as htmlmod
import json
import os
import re
import sys

PROPERTY_LABELS = [
    ("tensile_strength_zx", r"(прочност|разрыв).*поперек"),
    ("tensile_modulus_zx", r"модуль упругости при растяжении поперек"),
    ("elongation_zx", r"удлинени.*поперек"),
    ("tensile_modulus_xy", r"модуль упругости при растяжении"),
    ("flexural_modulus", r"модуль упругости (на|при) изгиб"),
    ("compressive_modulus", r"модуль упругости на сжатие"),
    ("flexural_strength", r"прочность на изгиб"),
    ("compressive_strength", r"прочность на сжатие"),
    ("tensile_strength_xy", r"прочност\w* при растяжении|прочность на разрыв|прочность при разрыве"),
    ("elongation_xy", r"удлинени"),
    ("vicat_softening_temperature", r"вика"),
    ("heat_deflection_temperature", r"изгиба под нагрузкой|\bhdt\b"),
    ("glass_transition_temperature", r"стеклован"),
    ("water_absorption", r"водопоглощ"),
    ("shrinkage", r"усадк"),
    ("hardness", r"твёрдост|твердост"),
    ("charpy_notched", r"шарпи"),
    ("uv_resistance", r"ультрафиолет"),
    ("electrical_resistance", r"сопротивлен"),
    ("density", r"плотност"),
    ("service_temperature", r"температура эксплуатации"),
    ("composition", r"^состав|полимерная основа"),
    ("article", r"артикул"),
    ("note", r"примечание"),
]

UNITS = {
    "density": "g/cm3", "service_temperature": "degC", "tensile_strength_xy": "MPa",
    "tensile_strength_zx": "MPa", "flexural_strength": "MPa", "compressive_strength": "MPa",
    "tensile_modulus_xy": "MPa", "tensile_modulus_zx": "MPa", "flexural_modulus": "MPa",
    "compressive_modulus": "MPa", "elongation_xy": "%", "elongation_zx": "%", "shrinkage": "%",
    "vicat_softening_temperature": "degC", "heat_deflection_temperature": "degC",
    "glass_transition_temperature": "degC", "water_absorption": "%", "charpy_notched": "kJ/m2",
    "electrical_resistance": "Ohm/m",
}

TEXT_PROPERTIES = ("hardness", "uv_resistance", "composition", "article", "note")

PROCESS_LABELS = [
    ("nozzle_diameter", r"диаметр сопла", "mm"),
    ("layer_height_min", r"высота слоя", "mm"),
    ("retraction_speed", r"скорость ретракта", "mm/s"),
    ("print_speed", r"скорость печати", "mm/s"),
    ("retraction_amount", r"ретракт", "mm"),
    ("infill_min", r"заполнение", "%"),
    ("nozzle_temperature", r"температура сопла", "degC"),
    ("bed_temperature", r"температура стола", "degC"),
    ("adhesion_classic", r"адгезия к столу", None),
    ("adhesion_5d", r"адгезия к основанию", None),
    ("fan", r"обдув", "%"),
    ("nozzle_material", r"материал сопла", None),
    ("window_position", r"окна", None),
]

# slicer material file -> (wiki path, mapping quality)
SLICER_TO_WIKI = {
    "enduse_abs": ("/ru/fdm-materials/enduse/abs", "exact"),
    "enduse_pa6": ("/ru/fdm-materials/enduse/pa6", "exact"),
    "enduse_pc": ("/ru/fdm-materials/enduse/pc", "exact"),
    "enduse_petg": ("/ru/fdm-materials/enduse/petg", "exact"),
    "enduse_pp": ("/ru/fdm-materials/enduse/pp_d60", "exact"),
    "enduse_sbs": ("/ru/fdm-materials/enduse/sbs", "exact"),
    "enduse_tpu_d70": ("/ru/fdm-materials/enduse/tpu-d70", "exact"),
    "fiberpart_abs_g13": ("/ru/fdm-materials/fiberpart/abs-g12", "assumed_name_differs"),
    "fiberpart_abs_g4": ("/ru/fdm-materials/fiberpart/abs-g4", "exact"),
    "fiberpart_abspa_g8": ("/ru/fdm-materials/fiberpart/abspa-g8", "exact"),
    "fiberpart_pa12_g12": ("/ru/fdm-materials/fiberpart/pa12-g12", "exact"),
    "fiberpart_pc_g20": ("/ru/fdm-materials/fiberpart/pc-g-20", "exact"),
    "fiberpart_pp_g30": ("/ru/fdm-materials/fiberpart/pp-g30", "exact"),
    "fiberpart_tpu_c5": ("/ru/fdm-materials/fiberpart/tpu-c5", "exact"),
    "fiberpart_tpu_g30": ("/ru/fdm-materials/fiberpart/tpu-g30", "exact"),
    "sealant_sebs": ("/ru/fdm-materials/sealant/sebs", "exact"),
    "sealant_tpu": ("/ru/fdm-materials/sealant/tpu", "exact"),
    "sealant_tpu_A95": ("/ru/fdm-materials/sealant/tpu", "assumed_same_page_as_sealant_tpu"),
    "sealant_tpu93": ("/ru/fdm-materials/sealant/tpu-93", "exact"),
    "proto_pla": ("/ru/fdm-materials/proto/pla", "exact"),
    "proto_pva": ("/ru/fdm-materials/proto/pva", "exact"),
    "contifiber_cpa": ("/ru/fdm-materials/contifiber/contifiber-cpa", "exact"),
    "metalcast_316l": ("/ru/fdm-materials/metalcast/metalcast-316l", "exact"),
}

# Rows of the chemical resistance table used for each material; base-polymer rows are proxies.
CHEM_ROWS = {
    "enduse_pa6": ("Enduse PA", "label"), "enduse_pp": ("Enduse PP", "label"),
    "enduse_abs": ("Enduse ABS", "label"), "enduse_sbs": ("Enduse SBS", "label"),
    "fiberpart_abspa_g8": ("Fiberpart ABSPA G8", "label"), "fiberpart_tpu_g30": ("Fiberpart TPU G30", "label"),
    "fiberpart_tpu_c5": ("Fiberpart TPU C5", "label"), "proto_pla": ("Proto PLA", "label"),
    "fiberpart_abs_g4": ("ABS G4", "label"), "sealant_sebs": ("SEBS", "label"),
    "sealant_tpu": ("TPU", "base_polymer"), "sealant_tpu_A95": ("TPU", "base_polymer"),
    "sealant_tpu93": ("TPU93", "label"), "enduse_tpu_d70": ("TPU", "base_polymer"),
    "enduse_pc": ("PC", "base_polymer"), "fiberpart_pc_g20": ("PC", "base_polymer"),
    "fiberpart_pa12_g12": ("PA 11, 12", "base_polymer"), "enduse_petg": ("PET", "related_polymer_PET_not_PETG"),
    "fiberpart_abs_g13": ("ABS G4", "related_grade"), "fiberpart_pp_g30": ("Enduse PP", "base_polymer"),
    "proto_pva": ("PVA", "base_polymer"),
}

NOT_FOR_HYBRID = {
    "contifiber_cpa": "continuous fibre needs the Fiber printer head",
    "metalcast_316l": "metal-polymer feedstock needs debinding/sintering",
    "metalcast_al99": "metal-polymer feedstock needs debinding/sintering",
    "proto_pva": "water-soluble support material",
}


def strip(cell):
    cell = re.sub(r"<br\s*/?>", " ", cell)
    cell = re.sub(r"<[^>]+>", "", cell)
    return re.sub(r"\s+", " ", htmlmod.unescape(cell)).strip()


def tables(body):
    out = []
    for t in re.findall(r"<table.*?</table>", body, re.S):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", t, re.S):
            cells = [strip(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            if any(cells):
                rows.append(cells)
        out.append(rows)
    return out


_NUM = r"[+\-−]?\d+(?:[.,]\d+)?(?:[eE][+\-]?\d+)?"


def num(s):
    return float(s.replace(",", ".").replace("−", "-").replace("+", ""))


def parse_value(raw):
    """Parse '1,18', 'от -20 до +120', '10-50', 'до 0,8', '>25', '90 (требуется уточнение)'."""
    text = raw.strip()
    rec = {"raw": raw}
    flags = []
    if re.search(r"требуется\s+уточнени", text, re.I):
        flags.append("needs_clarification")
        text = re.sub(r"\(?\s*требуется\s+уточнени\w*\s*\)?", "", text, flags=re.I).strip()
    m = re.search(r"кратковременно\s+до\s*(" + _NUM + ")", text, re.I)
    if m:
        rec["short_term_max"] = num(m.group(1))
        text = text[:m.start()].strip(" ,")
    # units written inside the cell
    text = re.sub(r"\s*(г/[сc]м[3³]|мпа|°\s*[сc]|кдж/м2|мм/[сc]|мм|ом/м)\.?", "", text, flags=re.I)
    text = re.sub(r"(?<=\d)\s*[cс](?![а-яa-z])", "", text, flags=re.I).replace("~", "").strip()
    low = text.lower()
    if low in ("", "-", "—", "н/д", "нет данных", "n/a"):
        rec["value"] = None
    elif re.fullmatch(r"от\s*(" + _NUM + r")\s*(?:°\s*[сc])?\s*до\s*(" + _NUM + r").*", low):
        m = re.match(r"от\s*(" + _NUM + r")\s*(?:°\s*[сc])?\s*до\s*(" + _NUM + ")", low)
        rec["min"], rec["max"] = num(m.group(1)), num(m.group(2))
    elif re.fullmatch(r"(" + _NUM + r")\s*[-–]\s*(" + _NUM + r")\s*%?", low):
        m = re.match(r"(" + _NUM + r")\s*[-–]\s*(" + _NUM + ")", low)
        a, b = num(m.group(1)), num(m.group(2))
        rec["min"], rec["max"] = min(a, b), max(a, b)
    elif re.fullmatch(r"(?:до|<|≤)\s*(" + _NUM + r")\s*%?", low):
        rec["max"] = num(re.search(_NUM, low).group(0))
    elif re.fullmatch(r"(?:от|>|≥)\s*(" + _NUM + r")\s*%?", low):
        rec["min"] = num(re.search(_NUM, low).group(0))
    elif re.fullmatch(r"(" + _NUM + r")\s*%?", low):
        rec["value"] = num(re.search(_NUM, low).group(0))
    elif re.fullmatch(r"(" + _NUM + r")\s*[^\d]{0,12}", low):
        rec["value"] = num(re.match(_NUM, low).group(0))
        flags.append("unit_text_stripped")
    else:
        nums = re.findall(_NUM, low)
        rec["text"] = text
        if len(nums) == 2 and re.search(r"\d\s*[-–]\s*\d", low):
            a, b = num(nums[0]), num(nums[1])
            rec["min"], rec["max"] = min(a, b), max(a, b)
            flags.append("parsed_from_text")
        elif len(nums) == 1:
            rec["value"] = num(nums[0])
            flags.append("parsed_from_text")
    if flags:
        rec["flags"] = flags
    return rec


def classify(label, table):
    low = label.lower()
    for key, rx in table:
        if re.search(rx, low):
            return key
    return None


def lists_after(text, header):
    m = re.search(header + r"\s*:?(.*?)(?:Недостатки|¶|Также могут подойти|$)", text, re.S | re.I)
    if not m:
        return []
    return [re.sub(r"\s+", " ", x).strip(" ;.") for x in re.findall(r"[–-]\s*([^–\n]+)", m.group(1)) if x.strip()]


def page_text(body):
    t = re.sub(r"</(p|li|h\d|tr)>", "\n", body)
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", t))


def parse_material_page(page):
    body = page["html"]
    props, process = {}, {}
    for rows in tables(body):
        for row in rows:
            cells = [c for c in row if c]
            if cells and re.fullmatch(r"\d+\.?", cells[0]):
                cells = cells[1:]
            if len(cells) < 2:
                continue
            label, raw = cells[0], cells[1]
            pkey = classify(label, [(k, rx) for k, rx, _ in PROCESS_LABELS])
            key = classify(label, PROPERTY_LABELS)
            if pkey:
                if pkey in ("adhesion_classic", "adhesion_5d", "nozzle_material", "window_position"):
                    process[pkey] = {"text": raw}
                else:
                    rec = parse_value(raw)
                    rec["unit"] = dict((k, u) for k, _, u in PROCESS_LABELS)[pkey]
                    process[pkey] = rec
                continue
            if key:
                if key in TEXT_PROPERTIES:
                    props[key] = {"text": raw, "source": "wiki_page", "url": page["url"], "label": label}
                else:
                    rec = parse_value(raw)
                    rec.update({"unit": UNITS.get(key), "source": "wiki_page", "url": page["url"], "label": label})
                    props[key] = rec
    text = page_text(body)
    m = re.search(r"Сушка материала\s*(.*?)(?:Признак|$)", text, re.S)
    drying = None
    if m:
        d = m.group(1)
        t = re.search(r"(" + _NUM + r")\s*[-–]\s*(" + _NUM + r")\s*°", d)
        h = re.search(r"(" + _NUM + r")\s*[-–]\s*(" + _NUM + r")\s*час", d)
        drying = {"text": re.sub(r"\s+", " ", d).strip(),
                  "temperature": [num(t.group(1)), num(t.group(2))] if t else None,
                  "hours": [num(h.group(1)), num(h.group(2))] if h else None}
    desc = {}
    m = re.search(r"Описание\s*(.*?)(?:Преимущества|¶|$)", text, re.S)
    if m:
        desc["summary"] = re.sub(r"\s+", " ", m.group(1)).strip()
    desc["advantages"] = lists_after(text, "Преимущества")
    desc["disadvantages"] = lists_after(text, "Недостатки")
    desc["applications"] = lists_after(text, r"предназначен для следующих типов изделий")
    desc["alternatives"] = lists_after(text, r"Также могут подойти для аналогичных задач")
    return props, process, drying, desc


def parse_general_table(page):
    out = {}
    for rows in tables(page["html"]):
        if not rows or "Наименование" not in rows[0][0]:
            continue
        header = rows[0]
        for row in rows[1:]:
            if len(row) < 3:
                continue
            name = row[0]
            rec = {}
            for label, raw in zip(header, row):
                key = classify(label, PROPERTY_LABELS)
                if not key:
                    continue
                if key in TEXT_PROPERTIES:
                    rec[key] = {"text": raw, "source": "wiki_general_table", "url": page["url"], "label": label}
                else:
                    v = parse_value(raw)
                    v.update({"unit": UNITS.get(key), "source": "wiki_general_table", "url": page["url"],
                              "label": label})
                    rec[key] = v
            out[name] = rec
    return out


def parse_chemical_table(page):
    for rows in tables(page["html"]):
        hdr = None
        for i, row in enumerate(rows):
            if row and row[0].startswith("Материал/Название"):
                hdr = i
                break
        if hdr is None:
            continue
        substances = rows[hdr][1:]
        out = {}
        for row in rows[hdr + 1:]:
            if not row or not row[0]:
                continue
            name = re.sub(r"\s*\(.*$", "", row[0]).strip()
            vals = {}
            for s, v in zip(substances, row[1:]):
                v = v.strip()
                vals[s] = {"+": "resistant", "(+)": "theoretically_resistant", "-": "not_resistant"}.get(
                    v, "no_data")
            out[name] = vals
        return out
    return {}


def read_slicer_material(path):
    s = open(path, encoding="utf-8").read()

    def tag(t):
        m = re.search(r"<%s>([^<]*)</%s>" % (t, t), s)
        return m.group(1).strip() if m else None

    settings = dict(re.findall(r'<setting key="([^"]+)">([^<]*)</setting>', s))
    return {
        "file": os.path.basename(path), "label": tag("label"), "brand": tag("brand"),
        "material_type": tag("material"), "guid": tag("GUID"),
        "density": float(tag("density")) if tag("density") else None,
        "diameter": float(tag("diameter")) if tag("diameter") else None,
        "settings": dict((k, settings[k]) for k in sorted(settings)),
    }


def value_of(rec):
    if rec.get("value") is not None:
        return (rec["value"], rec["value"])
    if "min" in rec or "max" in rec:
        return (rec.get("min"), rec.get("max"))
    return None


def _decimals(raw):
    nums = re.findall(r"\d+(?:[.,](\d+))?", raw or "")
    return min((len(d) for d in nums), default=0) if nums else 0


def same_value(rec_a, rec_b):
    """Equal up to rounding of the published figures: |a - b| <= half a unit of the coarser last digit
    (38 vs 38.1 agree; 1.15 vs 1.18 do not)."""
    a, b = value_of(rec_a), value_of(rec_b)
    if a is None or b is None:
        return False
    tol = 0.5 * 10 ** (-min(_decimals(rec_a.get("raw")), _decimals(rec_b.get("raw")))) + 1e-12
    for x, y in zip(a, b):
        if (x is None) != (y is None):
            return False
        if x is not None and abs(x - y) > tol:
            return False
    return True


def main(wiki_json, slicer_dir, out_path):
    pages = json.load(open(wiki_json, encoding="utf-8"))
    general = parse_general_table(pages["/ru/fdm-materials/general-material-characteristics"])
    chem = parse_chemical_table(pages["/ru/fdm-materials/material-chemical-resistance"])
    materials = []
    for fname in sorted(os.listdir(slicer_dir)):
        if not fname.endswith(".fdm_material"):
            continue
        mid = fname.split(".")[0]
        slicer = read_slicer_material(os.path.join(slicer_dir, fname))
        rec = {"id": mid, "label": slicer["label"] or mid, "slicer": slicer, "wiki_url": None,
               "mapping": "none", "properties": {}, "process": {}, "drying": None, "description": {},
               "chemical_resistance": None, "conflicts": []}
        if mid.startswith("generic_"):
            rec["label"] = "Generic " + (slicer["material_type"] or mid[8:])
        if mid in NOT_FOR_HYBRID:
            rec["excluded"] = NOT_FOR_HYBRID[mid]
        wiki = SLICER_TO_WIKI.get(mid)
        if wiki and wiki[0] in pages:
            page = pages[wiki[0]]
            rec["wiki_url"] = page["url"]
            rec["mapping"] = wiki[1]
            props, process, drying, desc = parse_material_page(page)
            rec["process"], rec["drying"], rec["description"] = process, drying, desc
            for k, v in props.items():
                rec["properties"][k] = [v]
            gname = None
            for name in general:
                if name.lower().replace(" ", "") == (slicer["label"] or "").lower().replace(" ", ""):
                    gname = name
            if gname is None and mid == "fiberpart_abs_g13":
                gname = "Fiberpart ABS G12"
            if gname:
                for k, v in general[gname].items():
                    lst = rec["properties"].setdefault(k, [])
                    if k in TEXT_PROPERTIES:
                        if not lst:
                            lst.append(v)
                        continue
                    if value_of(v) is None:
                        continue
                    same = [x for x in lst if same_value(x, v)]
                    if same:
                        same[0].setdefault("confirmed_by", []).append("wiki_general_table")
                        continue
                    if [x for x in lst if value_of(x) is not None]:
                        rec["conflicts"].append({"property": k, "values": [x.get("raw") for x in lst] + [v.get("raw")],
                                                 "resolution": "kept both; conservative bound used in screening"})
                    lst.append(v)
            # drop empty records
            for k in list(rec["properties"]):
                rec["properties"][k] = [x for x in rec["properties"][k] if x.get("text") or value_of(x) is not None]
                if not rec["properties"][k]:
                    del rec["properties"][k]
        row = CHEM_ROWS.get(mid)
        if row and row[0] in chem:
            rec["chemical_resistance"] = {"row": row[0], "match": row[1], "url": pages[
                "/ru/fdm-materials/material-chemical-resistance"]["url"], "values": chem[row[0]]}
        materials.append(rec)
    out = {
        "schema_version": "1.0",
        "source": "https://wiki.stereotech.org/ru/fdm-materials",
        "retrieved": sorted(set(p["retrieved"] for p in pages.values())),
        "note": ("Manufacturer wiki data are typical values from internal tests; they do not qualify a part. "
                 "Conflicting sources are kept side by side."),
        "materials": materials,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("materials:", len(materials), "with wiki:", sum(1 for m in materials if m["wiki_url"]),
          "conflicts:", sum(len(m["conflicts"]) for m in materials))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
