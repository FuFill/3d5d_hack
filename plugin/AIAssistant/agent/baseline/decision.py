# -*- coding: utf-8 -*-
"""Бэйзлайн-решатель: ТЗ + геометрия → Decision (спецификация, разделы 1, 7–9, 12–13).

Порядок совпадает со схемой зависимостей спецификации: требования → материал →
маршрут (режим) → ориентация → слой/стенки/заполнение → скорости → поддержки/адгезия
→ параметры 5D → компиляция патча с проверкой по схеме. Все пороги — явные
политики (P) в словаре POLICY; физические коэффициенты не выдумываются.
"""

import math

import numpy

from . import knowledge
from .geometry import analyze_mesh, load_stl, orientation_metrics, _face_data
from .materials import screen_materials
from .requirements import parse_requirements

VERSION = "baseline-0.1.0"

POLICY = {
    "nozzle_mm": 0.4,
    "full_mode_min_bore_mm": 10.0,      # cylindrical_full: оправка + helix-raft нужны внутри отверстия
    "raft_layers_min": 5,                # слоёв helix-raft между оправкой и деталью
    "min_base_diameter_mm": 6.0,         # default схемы cylindrical_mode_base_diameter
    "rotor_axis_score": 0.6,             # доля площади, совместимой с вращением, для «осевой» детали
    "support_overhang_fraction": 0.01,
    "wall_load_mm": 2.0,
    "wall_default_mm": 1.2,
    "wall_flexible_mm": 1.6,
    "skin_min_mm": 1.2,
    "solid_volume_limit_cm3": 200.0,     # до этого объёма нагруженные детали печатаются сплошными
    "infill_loaded_pct": 50.0,
    "infill_default_pct": 20.0,
    "infill_decorative_pct": 10.0,
    "flexible_speed_max": 25.0,
    "precision_outer_wall_speed": 20.0,
    "plate_clearance_mm": 10.0,
    "orientation_weights": {"overhang": 2.0, "height": 0.3, "stair": 0.2, "stair_surface": 1.0, "contact": 0.6},
}

WARP_PRONE = ("ABS", "PC", "PA6", "PA12", "PP", "ABS/PA")


def _rotation_to_z(up):
    """Матрица поворота, переводящая вектор up модели в +Z принтера (формула Родрига)."""
    up = numpy.asarray(up, dtype=float)
    up = up / numpy.linalg.norm(up)
    z = numpy.array([0.0, 0.0, 1.0])
    c = float(up.dot(z))
    if c > 1 - 1e-9:
        return numpy.eye(3)
    if c < -1 + 1e-9:
        return numpy.diag([1.0, -1.0, -1.0])
    v = numpy.cross(up, z)
    s = numpy.linalg.norm(v)
    k = numpy.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return numpy.eye(3) + k + k.dot(k) * ((1 - c) / (s * s))


def _matrix_to_axis_angle(r):
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (numpy.trace(r) - 1) / 2))))
    if angle < 1e-6:
        return [0.0, 0.0, 1.0], 0.0
    if abs(angle - 180.0) < 1e-6:
        axis = numpy.sqrt(numpy.maximum((numpy.diag(r) + 1) / 2, 0))
        k = int(numpy.argmax(axis))
        for i in range(3):
            if i != k and r[k, i] < 0:
                axis[i] = -axis[i]
    else:
        axis = numpy.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    axis = axis / numpy.linalg.norm(axis)
    return [round(float(x), 6) for x in axis], round(angle, 4)


class _Trace(object):
    def __init__(self):
        self.items = []

    def add(self, step, rule, detail):
        self.items.append({"step": step, "rule": rule, "detail": detail})


def _req_ids(reqset, *props):
    return [r["id"] for r in reqset["requirements"] if r["property"] in props]


def _has(reqset, *props):
    return any(r["property"] in props for r in reqset["requirements"])


def _req_value(reqset, prop):
    vals = [r["value"] for r in reqset["requirements"] if r["property"] == prop]
    return min(vals) if vals else None


def choose_route(reqset, geo, machine, trace):
    """Режим печати: classic / cylindrical (5D Spiral) / cylindrical_full (5D Full)."""
    ra = geo["rotation_axis"]
    has_axis = ra["revolution_score"] >= 0.4
    hint = reqset.get("mode_hint")
    alternatives = []
    route = {"mode": "classic", "params": {}, "reasons": [], "warnings": []}

    def five_d():
        bore_d = 2.0 * ra["bore_radius"] if ra.get("bore_radius") else None
        raft_t = machine["cylindrical_defaults"]["cylindrical_raft_thickness"]
        if bore_d and bore_d >= POLICY["full_mode_min_bore_mm"]:
            base = math.floor(bore_d * 10.0) / 10.0
            mandrel = max(POLICY["min_base_diameter_mm"],
                          math.floor(base - 2 * raft_t * POLICY["raft_layers_min"]))
            return {"mode": "cylindrical_full",
                    "params": {"cylindrical_mode_base_diameter": base, "non_printing_base_diameter": float(mandrel)},
                    "reasons": ["сквозное отверстие Ø%.1f мм ≥ %.0f мм: деталь целиком печатается радиальными слоями "
                                "на оправке Ø%.0f мм с helix-raft до Ø%.1f" % (bore_d, POLICY["full_mode_min_bore_mm"], mandrel, base)],
                    "warnings": ["Наличие оправки Ø%.0f мм у Hybrid 530 нужно подтвердить; иначе подобрать ближайшую." % mandrel]}
        core = ra.get("min_outer_radius") or POLICY["min_base_diameter_mm"] / 2
        base = max(POLICY["min_base_diameter_mm"], math.floor(2.0 * core))
        why = "нет сквозного отверстия" if not bore_d else "отверстие Ø%.1f мм меньше %.0f мм" % (bore_d, POLICY["full_mode_min_bore_mm"])
        return {"mode": "cylindrical",
                "params": {"cylindrical_mode_base_diameter": float(base)},
                "reasons": ["%s: ядро Ø%.0f мм (наименьший наружный диаметр вдоль оси) печатается классически, "
                            "всё снаружи — радиальными слоями 5D Spiral" % (why, base)],
                "warnings": []}

    if hint == "5D":
        if has_axis:
            route = five_d()
            route["reasons"].insert(0, "в задании указан 5D; у детали найдена ось вращения (доля поверхностей вращения %.2f)"
                                    % ra["revolution_score"])
            alternatives.append({"mode": "classic", "why_not": "в задании указан режим 5D"})
        else:
            route["reasons"].append("в задании указан 5D, но оси вращения не найдено — классический режим")
            route["warnings"].append("Подсказка 5D не согласуется с геометрией; требуется решение пользователя.")
    else:
        route["reasons"].append("в задании указан 3D" if hint == "3D" else "режим не указан: по умолчанию classic")
        if has_axis and ra.get("through_bore") and hint is None:
            alt = five_d()
            alternatives.append({"mode": alt["mode"], "params": alt["params"],
                                 "why_not": "5D возможен по геометрии, но не запрошен; предложить пользователю"})
    trace.add("route", "mode_hint+axis", {"hint": hint, "revolution_score": ra["revolution_score"],
                                           "through_bore": ra.get("through_bore"), "mode": route["mode"]})
    route["alternatives"] = alternatives
    return route


def choose_orientation(tris, geo, reqset, route, trace):
    normals, areas = _face_data(tris)
    area = float(areas.sum())
    ra = geo["rotation_axis"]
    axis = numpy.array(ra["vector"])
    candidates = []
    if route["mode"] != "classic" or ra["revolution_score"] >= POLICY["rotor_axis_score"]:
        ups = [axis, -axis]
        rule = "axis_vertical"
    else:
        ups = [numpy.array(o["up"]) for o in geo["orientations"]]
        rule = "min_J"
    w = POLICY["orientation_weights"]
    surface = _has(reqset, "surface_no_layers", "layer_hiding")
    max_dim = max(geo["bbox_size"])
    for up in ups:
        m = orientation_metrics(tris, normals, areas, up)
        ov, st = m["overhang_area"] / area, m["stair_area"] / area
        contact = min(1.0, m["bed_contact_area"] / (0.03 * area))
        j = w["overhang"] * ov + w["height"] * m["height"] / max_dim + \
            (w["stair_surface"] if surface else w["stair"]) * st + w["contact"] * (1 - contact)
        m.update({"up": [round(float(x), 4) for x in up], "J": round(j, 4), "overhang_fraction": round(ov, 4)})
        candidates.append(m)
    if route["mode"] != "classic":
        # В 5D опорой служит торец классического ядра/основания — выбираем торец с большей опорой.
        candidates.sort(key=lambda m: (-m["bed_contact_area"], m["J"]))
    else:
        candidates.sort(key=lambda m: m["J"])
    best = candidates[0]
    r = _rotation_to_z(best["up"])
    pts = tris.reshape(-1, 3).dot(r.T)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    if route["mode"] == "classic":
        shift = numpy.array([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
        placement = "центр стола (затем arrangeAll)"
    else:
        p = r.dot(numpy.array(ra["point"]))
        shift = numpy.array([-p[0], -p[1], -lo[2]])
        placement = "ось детали совмещена с осью вращения 5D (X=0, Y=0)"
    axis_xyz, angle = _matrix_to_axis_angle(r)
    trace.add("orientation", rule, {"chosen_up": best["up"], "J": best["J"], "candidates": len(candidates)})
    return {
        "rule": rule,
        "up_vector_model": best["up"],
        "rotation_matrix": [[round(float(x), 6) for x in row] for row in r],
        "rotation_axis_angle": {"axis": axis_xyz, "angle_deg": angle},
        "translation_mm": [round(float(x), 3) for x in shift],
        "placement": placement,
        "size_after_mm": [round(float(x), 2) for x in (hi - lo)],
        "metrics": best,
        "candidates": candidates[:6],
    }


class _Settings(object):
    def __init__(self):
        self.items = []

    def set(self, key, value, reason, req_ids=None, rule=None):
        for it in self.items:
            if it["key"] == key:
                it.update({"value": value, "reason": reason, "requirement_ids": req_ids or [], "rule": rule})
                return
        self.items.append({"key": key, "value": value, "reason": reason, "requirement_ids": req_ids or [], "rule": rule})


def _quality_for(layer, machine):
    profiles = machine["quality_profiles"]
    return min(profiles, key=lambda p: abs(p["layer_height"] - layer))


def build_settings(reqset, geo, mat, route, orient, machine, trace):
    s = _Settings()
    notes = []
    flags = set(reqset["flags"])
    flexible = bool(mat.get("flexible"))
    w = POLICY["nozzle_mm"]
    five_d = route["mode"] != "classic"
    volume_cm3 = geo["volume_mm3"] / 1000.0

    s.set("printing_mode", route["mode"], "; ".join(route["reasons"]), rule="route")
    for key, value in sorted(route["params"].items()):
        s.set(key, value, route["reasons"][-1], rule="route_5d")

    # Слой: h ≤ допуска (консервативная ступенька c_step ≤ h), спецификация 7.
    tol = _req_value(reqset, "tolerance_mm")
    if flexible:
        layer, why = 0.2, "эластомер: толстый слой стабильнее подаётся и лучше сваривается"
    elif tol is not None and tol <= 0.1 or _has(reqset, "surface_no_layers") or "gear_teeth" in flags:
        layer, why = 0.1, "точная/рабочая поверхность: ступенька слоя c_step ≤ h ≤ 0.1 мм"
    elif tol is not None or _has(reqset, "wall_variation_mm"):
        layer, why = 0.15, "допуск/разностенность ≥ 0.15 мм"
    else:
        layer, why = 0.2, "функциональная деталь без допусков: слой 0.2 мм (h/d = 0.5)"
    if tol is not None and layer > tol:
        layer = tol
    quality = _quality_for(layer, machine)
    if abs(quality["layer_height"] - layer) > 1e-9:
        s.set("layer_height", layer, why, _req_ids(reqset, "tolerance_mm", "surface_no_layers"), rule="layer")
    else:
        notes.append("Высота слоя %.2f мм задаётся профилем качества %s." % (layer, quality["quality_type"]))
    if _has(reqset, "layer_hiding") and not five_d:
        s.set("adaptive_layer_height_enabled", True,
              "скрыть ступеньки на наклонных поверхностях без перехода на 0.06 мм по всей высоте",
              _req_ids(reqset, "layer_hiding"), rule="adaptive_layer")
        s.set("adaptive_layer_height_variation", 0.04, "диапазон слоя %.2f…%.2f мм" % (layer - 0.04, layer + 0.04),
              _req_ids(reqset, "layer_hiding"), rule="adaptive_layer")
    trace.add("layer", "tolerance/surface/material", {"layer": layer, "quality": quality["quality_type"]})

    # Стенки: N = 1 + ceil((t - w0)/wi), спецификация 9.2.
    # Ударная вязкость — свойство материала; конструктивную нагрузку задают силы, вращение и износ.
    loaded = bool(flags & {"load_bearing", "rotor"}) or _has(reqset, "wear")
    if flexible:
        t_wall = POLICY["wall_flexible_mm"]
        why = "эластомер/уплотнение: сплошная оболочка без пор"
    elif loaded:
        t_wall = POLICY["wall_load_mm"]
        why = "нагрузка/вращение/износ: материал у наружного контура (момент инерции сечения)"
    else:
        t_wall = POLICY["wall_default_mm"]
        why = "ненагруженная деталь"
    if tol is not None or _has(reqset, "surface_no_layers", "wear"):
        t_wall += w
        why += "; +1 стенка под точную/изнашиваемую поверхность"
    n_walls = 1 + int(math.ceil(max(0.0, t_wall - w) / w - 1e-9))
    s.set("wall_line_count", n_walls, "%s; t=%.1f мм → N=%d при ширине %.1f мм" % (why, t_wall, n_walls, w),
          _req_ids(reqset, "tolerance_mm", "wear", "impact", "elasticity"), rule="walls")
    skin = max(n_walls * w, POLICY["skin_min_mm"])
    s.set("top_bottom_thickness", round(skin, 2), "кожа не тоньше стенки: %d слоёв по %.2f мм" % (int(math.ceil(skin / layer - 1e-9)), layer),
          rule="skin")

    # Заполнение.
    sealing = _has(reqset, "sealing")
    rotor = "rotor" in flags or _has(reqset, "balance", "balance_gmm")
    hot_wet = _has(reqset, "hot_water", "creep")
    if sealing or rotor or hot_wet or (loaded and volume_cm3 <= POLICY["solid_volume_limit_cm3"]):
        infill = 100.0
        why = []
        if sealing:
            why.append("герметичность")
        if rotor:
            why.append("ротор: однородная плотность для балансировки")
        if hot_wet:
            why.append("горячая среда/ползучесть: без внутренних пустот")
        if loaded and not why:
            why.append("нагруженная деталь объёмом %.0f см³ ≤ %.0f см³" % (volume_cm3, POLICY["solid_volume_limit_cm3"]))
        why = ", ".join(why)
    elif loaded:
        infill, why = POLICY["infill_loaded_pct"], "нагруженная крупная деталь"
    elif _has(reqset, "layer_hiding", "fine_detail"):
        infill, why = POLICY["infill_decorative_pct"], "декоративная деталь: прочность задаёт оболочка"
    else:
        infill, why = POLICY["infill_default_pct"], "по умолчанию"
    s.set("infill_sparse_density", infill, why, _req_ids(reqset, "sealing", "balance", "balance_gmm", "creep", "hot_water"),
          rule="infill")
    if infill < 100.0:
        s.set("infill_pattern", "gyroid", "изотропное заполнение без пересечений дорожек в слое", rule="infill_pattern")

    # Скорости (только ограничения сверху относительно профиля материала).
    speed = mat["profile"].get("speed_print")
    if flexible and speed and speed > POLICY["flexible_speed_max"]:
        s.set("speed_print", POLICY["flexible_speed_max"], "эластомер: ограничение подачи гибкого прутка", rule="speed_flex")
        speed = POLICY["flexible_speed_max"]
    if tol is not None or _has(reqset, "surface_no_layers", "layer_hiding"):
        v0 = min(POLICY["precision_outer_wall_speed"], (speed or 40.0) / 2.0)
        s.set("speed_wall_0", v0, "точная/видимая наружная поверхность", _req_ids(reqset, "tolerance_mm", "surface_no_layers", "layer_hiding"),
              rule="speed_outer")

    # Поддержки и адгезия.
    ov = orient["metrics"]["overhang_fraction"]
    if five_d:
        s.set("support_enable", False, "5D: радиальные слои наращиваются от оси, классические поддержки не используются", rule="support_5d")
    elif ov < POLICY["support_overhang_fraction"]:
        s.set("support_enable", False, "нависаний > support_angle %.1f%% площади" % (100 * ov), rule="support")
    else:
        s.set("support_enable", True, "нависания %.1f%% площади поверхности" % (100 * ov), rule="support")
        inner = geo["rotation_axis"].get("through_bore") and flexible
        if _has(reqset, "no_support_marks") or inner:
            s.set("support_type", "buildplate",
                  "поддержки только от стола: %s" % ("без контакта с видимой поверхностью" if not inner else "внутри полости их не удалить"),
                  _req_ids(reqset, "no_support_marks"), rule="support_type")
        if _has(reqset, "no_support_marks"):
            s.set("support_z_distance", round(2 * layer, 2), "зазор 2 слоя: легче отделять, меньше следов", _req_ids(reqset, "no_support_marks"),
                  rule="support_gap")
            notes.append("«Без следов поддержек» не гарантируется настройками: нужна постобработка мест контакта или иная ориентация.")
    if not five_d:
        m = orient["metrics"]
        slender = m["height"] / max(math.sqrt(max(m["bed_contact_area"], 1e-6)), 1e-6)
        if flexible:
            s.set("adhesion_type", "skirt", "эластомер хорошо держится на столе, кайма мешает снятию", rule="adhesion")
        elif mat.get("polymer") in WARP_PRONE or mat.get("filler") or slender > 3.0:
            s.set("adhesion_type", "brim", "склонность к короблению (%s) или высокая деталь на малой опоре" % mat.get("polymer"),
                  rule="adhesion")
        else:
            s.set("adhesion_type", "skirt", "достаточная опора на столе", rule="adhesion")
    else:
        notes.append("Адгезия в 5D: %s" % (mat.get("adhesion_info_ru") or "по инструкции материала"))

    # Шов: для герметичных и точных деталей прятать в угол (без универсального оптимума, 9.6).
    if sealing or tol is not None:
        s.set("z_seam_type", "sharpest_corner", "шов в углах: меньше влияния на посадку/уплотнение", rule="seam")

    if tol is not None:
        notes.append("Допуск ±%.2f мм: xy_offset не задаём (он меняет и наружный контур); нужна калибровка отверстий/контура и компенсация в CAD." % tol)
    if mat.get("filler", "") and str(mat.get("filler")).startswith(("GF", "CF")):
        notes.append("Наполненный материал (%s): нужно закалённое/износостойкое сопло." % mat["filler"])
    if "hygroscopic" in mat.get("claims", []) or mat.get("polymer") in ("TPU", "PA6", "PA12"):
        notes.append("Материал гигроскопичен: сушка перед печатью обязательна.")
    trace.add("settings", "rules", {"walls": n_walls, "infill": infill, "support": ov})
    return s.items, quality, notes


def compile_patch(items, index):
    """Проверка ключей по схеме STE: наличие, тип, enum, литеральные min/max, scope (раздел 12)."""
    patch, rejected = [], []
    for it in items:
        key, value = it["key"], it["value"]
        meta = index.get(key)
        problem = None
        if meta is None:
            problem = "ключ отсутствует в публичной схеме"
        else:
            t = meta.get("type")
            if t == "bool" and not isinstance(value, bool):
                problem = "ожидается bool"
            elif t in ("float", "int") and (isinstance(value, bool) or not isinstance(value, (int, float))):
                problem = "ожидается число"
            elif t == "int" and int(value) != value:
                problem = "ожидается целое"
            elif t == "enum" and value not in meta.get("options", []):
                problem = "значение не из списка %s" % meta.get("options")
            if problem is None and t in ("float", "int"):
                for bound, cmp in (("minimum_value", lambda a, b: a < b), ("maximum_value", lambda a, b: a > b)):
                    raw = meta.get(bound)
                    try:
                        limit = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if cmp(float(value), limit):
                        problem = "%s=%s нарушено" % (bound, raw)
        if problem:
            rejected.append(dict(it, error=problem))
            continue
        entry = dict(it)
        entry["scope"] = "extruder" if meta.get("settable_per_extruder") else "global"
        entry["type"] = meta.get("type")
        entry["unit"] = meta.get("unit")
        entry["schema_default"] = meta.get("default_value")
        patch.append(entry)
    return patch, rejected


def _qualification_plan(reqset, screening):
    tests = {
        "fatigue": "циклические испытания детали/зуба на рабочем спектре нагрузки",
        "creep": "ползучесть при рабочей температуре и нагрузке (паз/ступица)",
        "sealing": "испытание на утечку при заданном давлении и среде",
        "no_marks": "трение по белой бумаге/картону, контроль следов",
        "dust_free": "износ в паре с бумагой, контроль частиц",
        "esd": "измерение поверхностного сопротивления",
        "food_safe": "подтверждение допуска материала и процесса для пищевого контакта",
        "balance": "балансировка ротора после печати",
        "balance_gmm": "балансировка: остаточный дисбаланс по ТЗ",
        "cavitation": "кавитационная стойкость поверхности лопастей",
        "friction_low": "коэффициент трения в реальной паре (по POM)",
        "friction_high": "коэффициент трения по бумаге/крафту",
        "tolerance_mm": "измерение отверстий/контура тестовой детали, калибровка компенсации",
        "wall_variation_mm": "контроль разностенности лопастей",
    }
    plan = []
    for r in reqset["requirements"]:
        if r["property"] in tests:
            plan.append({"requirement_id": r["id"], "property": r["property"], "test": tests[r["property"]]})
    for c in screening["checks"]:
        if c["status"] == "unknown" and c["property"] not in tests:
            plan.append({"requirement_id": c["requirement_id"], "property": c["property"],
                         "test": "подтвердить свойство по паспорту партии/испытанию (%s)" % c["evidence"]})
    return plan


def _batch(geo_size, qty, machine, mode):
    vol = machine["build_volume_mm"]["classic" if mode == "classic" else "cylindrical"]
    if mode != "classic":
        return {"quantity": qty, "per_plate": 1, "plates": qty, "note": "5D: одна деталь на оси"}
    c = POLICY["plate_clearance_mm"]
    a, b = geo_size[0] + c, geo_size[1] + c
    fit = max(int(vol[0] // a) * int(vol[1] // b), int(vol[0] // b) * int(vol[1] // a))
    fit = max(fit, 1)
    return {"quantity": qty, "per_plate": min(fit, qty), "plates": int(math.ceil(qty / float(fit)))}


def recommend(text, model_path=None, tris=None, hint=None, material_id=None, mode=None):
    """Главная функция бэйзлайна. Возвращает словарь Decision (JSON-сериализуемый)."""
    trace = _Trace()
    machine = knowledge.machine()
    reqset = parse_requirements(text, hint)
    if mode:
        reqset["mode_hint"] = "5D" if mode in ("cylindrical", "cylindrical_full", "5D") else "3D"
    if tris is None:
        tris = load_stl(model_path)
    geo = analyze_mesh(tris)

    ranking = screen_materials(reqset)
    chosen = ranking[0]
    if material_id:
        chosen = [r for r in ranking if r["material_id"] == material_id][0]
        trace.add("material", "user_override", {"material": material_id})
    mat = knowledge.material(chosen["material_id"])
    trace.add("material", "screen_rank", {"material": mat["id"], "fails": chosen["mandatory_fails"], "score": chosen["score"]})

    route = choose_route(reqset, geo, machine, trace)
    if mode in ("classic", "cylindrical", "cylindrical_full") and route["mode"] != mode:
        route["warnings"].append("Режим %s задан пользователем вместо %s." % (mode, route["mode"]))
        route["mode"] = mode
    orient = choose_orientation(tris, geo, reqset, route, trace)
    items, quality, notes = build_settings(reqset, geo, mat, route, orient, machine, trace)
    patch, rejected = compile_patch(items, knowledge.settings_index())

    size_limit = machine["build_volume_mm"]["classic" if route["mode"] == "classic" else "cylindrical"]
    fits = all(orient["size_after_mm"][i] <= size_limit[i] for i in range(3))
    warnings = list(route["warnings"])
    if not fits:
        warnings.append("Деталь %s мм не помещается в область печати %s мм." % (orient["size_after_mm"], size_limit))

    scene_ops = [
        {"op": "load", "path": model_path},
        {"op": "rotate", "matrix": orient["rotation_matrix"], "axis_angle": orient["rotation_axis_angle"],
         "frame": "система STL/принтера, Z вверх; в сцене Cura ось Y вверх — пересчёт поворота проверить по STLReader дистрибутива"},
        {"op": "translate", "vector_mm": orient["translation_mm"], "placement": orient["placement"]},
    ]
    batch = None
    qty = reqset["numbers"].get("batch_qty")
    if qty:
        batch = _batch(orient["size_after_mm"], qty, machine, route["mode"])
        scene_ops.append({"op": "multiply", "copies": batch["per_plate"] - 1, "then": "arrangeAll"})
        warnings.append("Партия %d шт.: %d на стол, нужно %d запусков." % (qty, batch["per_plate"], batch["plates"]))
    elif route["mode"] == "classic":
        scene_ops.append({"op": "arrangeAll"})

    if chosen["mandatory_fails"]:
        status = "infeasible"
        warnings.append("Ни один материал каталога не проходит все обязательные требования; выбран наименее нарушающий.")
    elif reqset["missing_inputs"] and any(c["status"] == "unknown" for c in chosen["checks"]):
        status = "needs_calibration"
    else:
        status = "candidate"

    effective = dict((k, v) for k, v in mat["profile"].items() if v is not None)
    effective.update(dict((p["key"], p["value"]) for p in patch))
    effective["layer_height"] = effective.get("layer_height", quality["layer_height"])

    return {
        "version": VERSION,
        "status": status,
        "evidence_level": "analytical_screen",
        "machine": {"definition_id": machine["definition_id"], "name": machine["name"]},
        "requirements": reqset["requirements"],
        "numbers": reqset["numbers"],
        "flags": reqset["flags"],
        "missing_inputs": reqset["missing_inputs"],
        "assumptions": [r for r in reqset["requirements"] if r["interpretation_status"] in ("derived", "assumed")],
        "geometry": {k: geo[k] for k in ("triangles", "open_edges", "bbox_size", "volume_mm3", "area_mm2", "rotation_axis")},
        "candidates": {
            "materials": [{k: r[k] for k in ("material_id", "label", "mandatory_fails", "unknown", "score")} for r in ranking[:5]],
            "routes": route["alternatives"],
            "orientations": orient["candidates"],
        },
        "chosen_candidate": {
            "printing_mode": route["mode"],
            "material": {"id": mat["id"], "label": mat["label"], "guid": mat["guid"], "checks": chosen["checks"]},
            "quality": quality,
            "orientation": {k: orient[k] for k in ("rule", "up_vector_model", "rotation_axis_angle", "translation_mm",
                                                   "placement", "size_after_mm")},
            "route_reasons": route["reasons"],
            "batch": batch,
        },
        "setting_patch": patch,
        "rejected_settings": rejected,
        "effective_settings": effective,
        "scene_operations": scene_ops,
        "verification": {"patch_valid": not rejected, "fits_build_volume": fits, "slice_verified": False},
        "warnings": warnings,
        "notes": notes,
        "qualification_plan": _qualification_plan(reqset, chosen),
        "policy": {"decision": POLICY, "requirements": reqset["policy"]},
        "trace": trace.items,
    }
