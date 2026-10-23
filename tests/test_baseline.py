# -*- coding: utf-8 -*-
"""Проверки бэйзлайна (раздел 17 спецификации): единицы, проценты, unknown, схема, детерминизм, 5D.

    python -m unittest discover -s tests -v
"""

import ast
import json
import math
import os
import sys
import tokenize
import unittest

import numpy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "plugin", "AIAssistant", "agent", "baseline")
sys.path.insert(0, os.path.dirname(PKG))

from baseline import knowledge  # noqa: E402
from baseline.decision import compile_patch, recommend  # noqa: E402
from baseline.documents import find_case_files, long_path, read_text  # noqa: E402
from baseline.geometry import analyze_mesh  # noqa: E402
from baseline.materials import check, screen_materials  # noqa: E402
from baseline.requirements import parse_requirements  # noqa: E402


def tube(r_out, r_in, height, n=96):
    """Замкнутая сетка трубы (r_in=0 — сплошной цилиндр), ось Y как в STL из SolidWorks."""
    tris = []
    ang = [2 * math.pi * i / n for i in range(n)]
    ring = lambda r, y: [(r * math.cos(a), y, r * math.sin(a)) for a in ang]
    ob, ot = ring(r_out, 0.0), ring(r_out, height)
    for i in range(n):
        j = (i + 1) % n
        tris += [(ob[i], ot[i], ot[j]), (ob[i], ot[j], ob[j])]
        if r_in > 0:
            ib, it = ring(r_in, 0.0), ring(r_in, height)
            tris += [(ib[i], it[j], it[i]), (ib[i], ib[j], it[j])]
            tris += [(ob[i], ob[j], ib[j]), (ob[i], ib[j], ib[i])]
            tris += [(ot[i], it[j], ot[j]), (ot[i], it[i], it[j])]
        else:
            cb, ct = (0.0, 0.0, 0.0), (0.0, height, 0.0)
            tris += [(cb, ob[j], ob[i]), (ct, ot[i], ot[j])]
    arr = numpy.array(tris, dtype=float)
    # Единая наружная ориентация граней: исправляем знак по объёму.
    a, b, c = arr[:, 0], arr[:, 1], arr[:, 2]
    if numpy.einsum("ij,ij->i", a, numpy.cross(b, c)).sum() < 0:
        arr = arr[:, ::-1]
    return arr


def box(x, y, z):
    v = numpy.array([[0, 0, 0], [x, 0, 0], [x, y, 0], [0, y, 0], [0, 0, z], [x, 0, z], [x, y, z], [0, y, z]], dtype=float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6),
         (3, 0, 4), (3, 4, 7)]
    return numpy.array([[v[i] for i in face] for face in f])


class Python35Compat(unittest.TestCase):
    """Код плагина исполняется интерпретатором STE Slicer (Python 3.5)."""

    def test_no_newer_syntax(self):
        banned = tuple(getattr(ast, n) for n in ("JoinedStr", "AnnAssign", "NamedExpr") if hasattr(ast, n))
        for name in sorted(os.listdir(PKG)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(PKG, name)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                self.assertNotIsInstance(node, banned, "%s:%s" % (name, getattr(node, "lineno", "?")))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertFalse(node.args.posonlyargs, "%s: positional-only args" % name)
                    self.assertIsNone(node.returns, "%s: return annotation in %s" % (name, node.name))
                    for a in node.args.args + node.args.kwonlyargs:
                        self.assertIsNone(a.annotation, "%s: annotation in %s" % (name, node.name))
            with open(path, "rb") as f:
                for tok in tokenize.tokenize(f.readline):
                    if tok.type == tokenize.NUMBER:
                        self.assertNotIn("_", tok.string, "%s: numeric underscore" % name)


class Geometry(unittest.TestCase):
    def test_box_volume_and_overhang(self):
        g = analyze_mesh(box(20, 10, 5))
        self.assertAlmostEqual(g["volume_mm3"], 1000.0, places=6)
        self.assertEqual(g["open_edges"], 0)
        up_z = [o for o in g["orientations"] if o["up"] == [0.0, 0.0, 1.0]][0]
        self.assertEqual(up_z["overhang_area"], 0.0)
        self.assertAlmostEqual(up_z["bed_contact_area"], 200.0, places=6)

    def test_tube_axis_and_bore(self):
        g = analyze_mesh(tube(30.0, 8.0, 25.0))
        ra = g["rotation_axis"]
        self.assertEqual(ra["vector"], [0.0, 1.0, 0.0])
        self.assertTrue(ra["axisymmetric"])
        self.assertTrue(ra["through_bore"])
        self.assertAlmostEqual(ra["bore_radius"], 8.0, delta=0.05)
        exact = math.pi * (30.0 ** 2 - 8.0 ** 2) * 25.0
        self.assertLess(abs(g["volume_mm3"] - exact) / exact, 0.01)

    def test_solid_cylinder_has_no_bore(self):
        ra = analyze_mesh(tube(10.0, 0.0, 40.0))["rotation_axis"]
        self.assertFalse(ra["through_bore"])
        self.assertIsNone(ra["bore_radius"])


class Requirements(unittest.TestCase):
    def test_numeric_limits_and_units(self):
        r = parse_requirements("Модуль упругости при растяжении: не менее 4500 МПа.\n"
                               "Плотность: Не более 1,2 г/см³.\n"
                               "Водопоглощение за 24 часа погружения должно составлять не более 0.05%.")
        got = dict((x["property"], (x["comparator"], x["value"], x["unit"])) for x in r["requirements"])
        self.assertEqual(got["tensile_modulus_mpa"], (">=", 4500.0, "MPa"))
        self.assertEqual(got["density_g_cm3"], ("<=", 1.2, "g/cm3"))
        self.assertEqual(got["water_absorption_24h_pct"], ("<=", 0.05, "percent"))

    def test_temperature_ranges_not_negative(self):
        r = parse_requirements("Температура на поверхности может держаться около 60-65°C.\nТемпературный режим: Строго +20°C ± 2°C.")
        self.assertEqual(r["numbers"]["service_temp_max_c"], 65.0)
        self.assertEqual(r["numbers"]["service_temp_min_c"], 18.0)

    def test_preference_is_not_mandatory_and_has_provenance(self):
        r = parse_requirements("Желательно, чтобы материал обладал антистатическими (ESD) свойствами.")
        esd = [x for x in r["requirements"] if x["property"] == "esd"][0]
        self.assertFalse(esd["mandatory"])
        self.assertIn("ESD", esd["provenance"])

    def test_torque_units(self):
        r = parse_requirements("Номинальный крутящий момент: 25 Н·м.\nПиковый (стопорный) момент: до 75 Н·м.")
        self.assertEqual(r["numbers"]["torque_nominal_nm"], 25.0)
        self.assertEqual(r["numbers"]["torque_peak_nm"], 75.0)


class MaterialScreening(unittest.TestCase):
    policy = parse_requirements("")["policy"]

    def req(self, prop, comp, value, kind="material", mandatory=True):
        return {"id": "R1", "property": prop, "comparator": comp, "value": value, "kind": kind, "mandatory": mandatory}

    def test_interval_crossing_threshold_is_unknown(self):
        pp = knowledge.material("fiberpart_pp_g30")  # E типично 2600…6000
        self.assertEqual(check(pp, self.req("tensile_modulus_mpa", ">=", 4500.0), self.policy)[0], "unknown")
        self.assertEqual(check(pp, self.req("tensile_modulus_mpa", ">=", 2000.0), self.policy)[0], "pass")
        self.assertEqual(check(pp, self.req("tensile_modulus_mpa", ">=", 7000.0), self.policy)[0], "fail")

    def test_qualification_properties_never_pass(self):
        for m in knowledge.materials():
            for prop in ("fatigue", "creep", "sealing", "cavitation"):
                self.assertEqual(check(m, self.req(prop, "verified", True, kind="qualification"), self.policy)[0], "unknown")

    def test_hard_failure_cannot_be_hidden_by_score(self):
        reqset = parse_requirements("Плотность: не более 1.0 г/см³. Материал должен быть масло и бензостойким. "
                                    "Материал должен быть эластичным.")
        ranking = screen_materials(reqset)
        fails = [r["mandatory_fails"] for r in ranking]
        self.assertEqual(fails, sorted(fails))


class Patch(unittest.TestCase):
    index = knowledge.settings_index()

    def item(self, key, value):
        return {"key": key, "value": value, "reason": "test", "requirement_ids": [], "rule": "t"}

    def test_schema_validation(self):
        patch, rejected = compile_patch([
            self.item("infill_sparse_density", 100.0),
            self.item("printing_mode", "spherical"),
            self.item("no_such_key", 1),
            self.item("support_angle", 120.0),
            self.item("wall_line_count", 2.5),
            self.item("material_print_temperature", 240.0),
        ], self.index)
        self.assertEqual([p["key"] for p in patch], ["infill_sparse_density", "material_print_temperature"])
        self.assertEqual(len(rejected), 4)
        self.assertEqual(patch[0]["unit"], "%")
        self.assertEqual(patch[1]["scope"], "extruder")


class Decisions(unittest.TestCase):
    def test_route_selection_and_determinism(self):
        wheel = tube(30.0, 8.0, 25.0)
        d1 = recommend("Колесо. Допуск отверстия ±0.1 мм.", tris=wheel, hint="5D")
        d2 = recommend("Колесо. Допуск отверстия ±0.1 мм.", tris=wheel, hint="5D")
        self.assertEqual(json.dumps(d1, sort_keys=True), json.dumps(d2, sort_keys=True))
        self.assertEqual(d1["chosen_candidate"]["printing_mode"], "cylindrical_full")
        patch = dict((p["key"], p["value"]) for p in d1["setting_patch"])
        self.assertLessEqual(patch["cylindrical_mode_base_diameter"], 16.0)
        self.assertEqual(d1["chosen_candidate"]["orientation"]["translation_mm"][:2], [0.0, 0.0])

        shaft = recommend("Вал.", tris=tube(10.0, 0.0, 40.0), hint="5D")
        self.assertEqual(shaft["chosen_candidate"]["printing_mode"], "cylindrical")
        plain = recommend("Деталь.", tris=wheel)
        self.assertEqual(plain["chosen_candidate"]["printing_mode"], "classic")
        self.assertTrue(plain["candidates"]["routes"])

    def test_percent_boundary(self):
        d = recommend("Деталь должна быть абсолютно герметична.", tris=tube(20.0, 5.0, 10.0))
        patch = dict((p["key"], p["value"]) for p in d["setting_patch"])
        self.assertEqual(patch["infill_sparse_density"], 100.0)
        self.assertTrue(d["verification"]["patch_valid"])


EXAMPLES = os.path.join(ROOT, "data", "examples")


@unittest.skipUnless(os.path.isdir(EXAMPLES), "нет data/examples")
class OrganizerExamples(unittest.TestCase):
    def test_all_cases(self):
        for case in sorted(os.listdir(long_path(EXAMPLES))):
            model, docs, _ = find_case_files(os.path.join(EXAMPLES, case))
            d = recommend("\n".join(read_text(p) for p in docs), model_path=model, hint=case)
            mode = d["chosen_candidate"]["printing_mode"]
            if "5D" in case:
                self.assertIn(mode, ("cylindrical", "cylindrical_full"), case)
            else:
                self.assertEqual(mode, "classic", case)
            self.assertTrue(d["verification"]["patch_valid"], case)
            self.assertTrue(d["verification"]["fits_build_volume"], case)
            self.assertNotEqual(d["status"], "qualified")


if __name__ == "__main__":
    unittest.main()
