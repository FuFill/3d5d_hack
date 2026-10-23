# -*- coding: utf-8 -*-
"""Собирает базу знаний бэйзлайна в plugin/AIAssistant/agent/baseline/data/.

Источники:
  * data/research/fdmprinter.public.def.json — публичная схема настроек STE Slicer;
  * профили материалов Stereotech из открытого репозитория STE Slicer (resources/materials);
  * определения Hybrid 530 V5.2 (hte530v5.2 -> steprinter530_v5.2 -> steprinter_v5).

Значения профилей материалов копируются как есть (класс S: семантика слайсера).
Свойства полимеров — интервалы типичных значений класса материала (класс E,
evidence = polymer_class_typical). Это не паспорт SKU: интервал служит только для
трёхзначного скрининга pass/fail/unknown, а не для квалификации детали.

Запуск (Python 3.5+):
    python scripts/build_knowledge.py [--materials-dir DIR]
Без --materials-dir XML скачиваются с GitHub.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

try:
    from urllib.request import urlopen
except ImportError:  # pragma: no cover
    from urllib2 import urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "plugin", "AIAssistant", "agent", "baseline", "data")
SCHEMA = os.path.join(ROOT, "data", "research", "fdmprinter.public.def.json")
RAW = "https://raw.githubusercontent.com/stereotech/STE-Slicer/develop/resources/materials/"
NS = {"m": "http://www.ultimaker.com/material"}

# Оценки: excellent / good / fair / poor. claim=True означает, что свойство прямо
# заявлено в описании профиля Stereotech (evidence = ste_material_description).
# Интервалы [min, max] — типичные значения класса полимера, единицы указаны в ключе.
MATERIALS = OrderedDict([
    ("proto_pla", {
        "polymer": "PLA", "filler": None, "flexible": False,
        "typical": {"tg_c": [55, 65], "vicat_b50_c": [55, 65], "hdt_c": [50, 60], "tensile_modulus_mpa": [2500, 3600],
                    "tensile_strength_mpa": [40, 60], "elongation_pct": [2, 6], "water_absorption_24h_pct": [0.3, 0.6]},
        "ratings": {"uv": "fair", "alcohol": "fair", "alkali": "poor", "acids_salts": "fair", "oil": "fair", "fuel": "poor",
                    "ozone": "good", "hot_water": "poor", "impact": "poor", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "excellent", "layer_hiding": "fair", "printability": "excellent"},
    }),
    ("enduse_petg", {
        "polymer": "PETG", "filler": None, "flexible": False,
        "typical": {"tg_c": [75, 85], "vicat_b50_c": [75, 85], "hdt_c": [65, 75], "tensile_modulus_mpa": [1800, 2200],
                    "tensile_strength_mpa": [40, 55], "elongation_pct": [5, 100], "water_absorption_24h_pct": [0.1, 0.3]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "fair", "acids_salts": "good", "oil": "good", "fuel": "fair",
                    "ozone": "good", "hot_water": "fair", "impact": "good", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "good", "layer_hiding": "fair", "printability": "good"},
        "claims": ["impact", "low_shrinkage"],
    }),
    ("enduse_abs", {
        "polymer": "ABS", "filler": None, "flexible": False,
        "typical": {"tg_c": [100, 110], "vicat_b50_c": [95, 105], "hdt_c": [85, 98], "tensile_modulus_mpa": [1800, 2400],
                    "tensile_strength_mpa": [30, 45], "elongation_pct": [3, 30], "water_absorption_24h_pct": [0.2, 0.45]},
        "ratings": {"uv": "poor", "alcohol": "fair", "alkali": "good", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "fair", "hot_water": "fair", "impact": "good", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "good", "layer_hiding": "fair", "printability": "fair"},
        "claims": ["not_uv_resistant", "not_humidity_resistant"],
    }),
    ("enduse_pc", {
        "polymer": "PC", "filler": None, "flexible": False,
        "typical": {"tg_c": [140, 150], "vicat_b50_c": [140, 150], "hdt_c": [130, 140], "tensile_modulus_mpa": [2000, 2500],
                    "tensile_strength_mpa": [55, 70], "elongation_pct": [5, 100], "water_absorption_24h_pct": [0.12, 0.2]},
        "ratings": {"uv": "fair", "alcohol": "fair", "alkali": "poor", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "good", "hot_water": "poor", "impact": "excellent", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "good", "layer_hiding": "fair", "printability": "fair"},
        "claims": ["heat_resistant", "high_strength"],
    }),
    ("enduse_pa6", {
        "polymer": "PA6", "filler": None, "flexible": False,
        "typical": {"tg_c": [45, 60], "vicat_b50_c": [180, 210], "hdt_c": [150, 190], "tensile_modulus_mpa": [1000, 2800],
                    "tensile_strength_mpa": [40, 80], "elongation_pct": [20, 200], "water_absorption_24h_pct": [1.3, 1.9],
                    "melting_c": [215, 225]},
        "ratings": {"uv": "fair", "alcohol": "excellent", "alkali": "excellent", "acids_salts": "good", "oil": "excellent",
                    "fuel": "excellent", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "excellent", "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "fair",
                    "printability": "fair"},
        "claims": ["alkali", "acids", "oil", "alcohol", "fuel", "wear", "impact", "sliding_parts"],
    }),
    ("enduse_pp", {
        "polymer": "PP", "filler": None, "flexible": False,
        "typical": {"tg_c": [-20, 0], "vicat_b50_c": [80, 95], "hdt_c": [80, 100], "tensile_modulus_mpa": [1100, 1600],
                    "tensile_strength_mpa": [25, 35], "elongation_pct": [100, 600], "water_absorption_24h_pct": [0.01, 0.03],
                    "melting_c": [160, 168]},
        "ratings": {"uv": "poor", "alcohol": "excellent", "alkali": "excellent", "acids_salts": "excellent", "oil": "fair",
                    "fuel": "fair", "ozone": "fair", "hot_water": "good", "impact": "good", "wear": "fair",
                    "friction_low": "good", "friction_high": "poor", "fine_detail": "poor", "layer_hiding": "fair",
                    "printability": "poor"},
        "claims": ["chemical_resistance", "elastic"],
    }),
    ("enduse_sbs", {
        "polymer": "SBS", "filler": None, "flexible": False,
        "typical": {"tg_c": [70, 95], "vicat_b50_c": [70, 85], "hdt_c": [65, 75], "tensile_modulus_mpa": [1200, 1700],
                    "tensile_strength_mpa": [20, 30], "elongation_pct": [10, 200], "water_absorption_24h_pct": [0.05, 0.1]},
        "ratings": {"uv": "poor", "alcohol": "fair", "alkali": "good", "acids_salts": "good", "oil": "poor", "fuel": "poor",
                    "ozone": "fair", "hot_water": "fair", "impact": "good", "wear": "fair", "friction_low": "fair",
                    "friction_high": "fair", "fine_detail": "good", "layer_hiding": "good", "printability": "good"},
        "claims": ["food_contact", "impact"],
    }),
    ("enduse_tpu_d70", {
        "polymer": "TPU", "filler": None, "flexible": True, "shore": "70D",
        "typical": {"tg_c": [-40, -20], "tensile_modulus_mpa": [150, 500], "tensile_strength_mpa": [35, 50],
                    "elongation_pct": [200, 450], "water_absorption_24h_pct": [0.5, 1.2], "service_max_c": [80, 100]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "poor", "friction_high": "good", "fine_detail": "fair", "layer_hiding": "fair",
                    "printability": "fair", "elasticity": "fair"},
        "claims": ["alkali", "acids", "oil", "diesel", "seals"],
    }),
    ("fiberpart_abs_g4", {
        "polymer": "ABS", "filler": "GF4", "flexible": False,
        "typical": {"tg_c": [100, 110], "vicat_b50_c": [98, 108], "hdt_c": [90, 100], "tensile_modulus_mpa": [2300, 3200],
                    "tensile_strength_mpa": [35, 50], "elongation_pct": [2, 6], "water_absorption_24h_pct": [0.2, 0.45]},
        "ratings": {"uv": "poor", "alcohol": "fair", "alkali": "good", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "fair", "hot_water": "fair", "impact": "fair", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "good", "printability": "fair"},
        "claims": ["rough_surface"],
    }),
    ("fiberpart_abs_g13", {
        "polymer": "ABS", "filler": "GF13", "flexible": False,
        "typical": {"tg_c": [100, 112], "vicat_b50_c": [100, 112], "hdt_c": [95, 105], "tensile_modulus_mpa": [3500, 5500],
                    "tensile_strength_mpa": [40, 60], "elongation_pct": [1.5, 4], "water_absorption_24h_pct": [0.2, 0.45]},
        "ratings": {"uv": "poor", "alcohol": "fair", "alkali": "good", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "fair", "hot_water": "fair", "impact": "fair", "wear": "fair", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "excellent", "printability": "fair"},
        "claims": ["no_visible_layer_transition", "interlayer_strength", "heat_resistant"],
    }),
    ("fiberpart_abspa_g8", {
        "polymer": "ABS/PA", "filler": "GF8", "flexible": False,
        "typical": {"tg_c": [100, 115], "vicat_b50_c": [100, 120], "hdt_c": [95, 115], "tensile_modulus_mpa": [3000, 5000],
                    "tensile_strength_mpa": [40, 60], "elongation_pct": [2, 5], "water_absorption_24h_pct": [0.5, 1.2]},
        "ratings": {"uv": "good", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "good", "fuel": "fair",
                    "ozone": "good", "hot_water": "fair", "impact": "good", "wear": "good", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "good", "printability": "fair"},
        "claims": ["uv", "impact", "chemical_inert"],
    }),
    ("fiberpart_pa12_g12", {
        "polymer": "PA12", "filler": "GF12", "flexible": False,
        "typical": {"tg_c": [40, 50], "vicat_b50_c": [160, 180], "hdt_c": [150, 175], "tensile_modulus_mpa": [2500, 4500],
                    "tensile_strength_mpa": [45, 65], "elongation_pct": [3, 8], "water_absorption_24h_pct": [0.2, 0.5],
                    "melting_c": [175, 180]},
        "ratings": {"uv": "fair", "alcohol": "excellent", "alkali": "excellent", "acids_salts": "good", "oil": "excellent",
                    "fuel": "excellent", "ozone": "good", "hot_water": "good", "impact": "good", "wear": "excellent",
                    "friction_low": "good", "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "good",
                    "printability": "fair"},
        "claims": ["alcohol", "alkali", "oil", "fuel", "wear", "cyclic_loads", "heat_resistant"],
    }),
    ("fiberpart_pc_g20", {
        "polymer": "PC", "filler": "GF20", "flexible": False,
        "typical": {"tg_c": [145, 155], "vicat_b50_c": [145, 155], "hdt_c": [140, 150], "tensile_modulus_mpa": [5000, 7000],
                    "tensile_strength_mpa": [60, 90], "elongation_pct": [1.5, 3], "water_absorption_24h_pct": [0.1, 0.2]},
        "ratings": {"uv": "fair", "alcohol": "fair", "alkali": "poor", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "good", "hot_water": "poor", "impact": "fair", "wear": "good", "friction_low": "fair",
                    "friction_high": "poor", "fine_detail": "fair", "layer_hiding": "good", "printability": "poor"},
        "claims": ["very_stiff", "wide_temperature_range", "chemical_resistance"],
    }),
    ("fiberpart_pp_g30", {
        "polymer": "PP", "filler": "GF30", "flexible": False,
        "typical": {"tg_c": [-20, 0], "vicat_b50_c": [120, 150], "hdt_c": [140, 160], "tensile_modulus_mpa": [2600, 6000],
                    "tensile_strength_mpa": [40, 70], "elongation_pct": [2, 4], "water_absorption_24h_pct": [0.01, 0.05],
                    "melting_c": [160, 168]},
        "ratings": {"uv": "fair", "alcohol": "excellent", "alkali": "excellent", "acids_salts": "excellent", "oil": "fair",
                    "fuel": "fair", "ozone": "fair", "hot_water": "excellent", "impact": "fair", "wear": "good",
                    "friction_low": "fair", "friction_high": "poor", "fine_detail": "poor", "layer_hiding": "good",
                    "printability": "fair"},
        "claims": ["acids", "solvents", "hydrolysis", "hot_coolant", "frost"],
    }),
    ("fiberpart_tpu_c5", {
        "polymer": "TPU", "filler": "CF5", "flexible": True, "shore": "semi-rigid",
        "typical": {"tg_c": [-40, -20], "tensile_modulus_mpa": [300, 900], "tensile_strength_mpa": [35, 55],
                    "elongation_pct": [50, 300], "water_absorption_24h_pct": [0.5, 1.2], "service_max_c": [80, 100]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "fair", "friction_high": "fair", "fine_detail": "fair", "layer_hiding": "good",
                    "printability": "fair", "elasticity": "poor"},
        "claims": ["gears", "alkali", "acids", "oil", "fuel"],
        "marks_risk": True,
    }),
    ("fiberpart_tpu_g30", {
        "polymer": "TPU", "filler": "GF30", "flexible": False, "shore": "rigid",
        "typical": {"tg_c": [-40, -20], "tensile_modulus_mpa": [800, 2500], "tensile_strength_mpa": [35, 60],
                    "elongation_pct": [5, 50], "water_absorption_24h_pct": [0.5, 1.2], "service_max_c": [90, 110]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "good",
                    "friction_low": "fair", "friction_high": "fair", "fine_detail": "poor", "layer_hiding": "good",
                    "printability": "fair"},
        "claims": ["impact", "heat_resistant", "large_gears"],
    }),
    ("sealant_tpu", {
        "polymer": "TPU", "filler": None, "flexible": True, "shore": "A (не указана)",
        "typical": {"tg_c": [-45, -25], "tensile_modulus_mpa": [10, 60], "tensile_strength_mpa": [25, 45],
                    "elongation_pct": [400, 600], "water_absorption_24h_pct": [0.7, 1.5], "service_max_c": [80, 100]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "poor", "friction_high": "excellent", "fine_detail": "fair", "layer_hiding": "fair",
                    "printability": "fair", "elasticity": "excellent"},
        "claims": ["alkali", "acids", "oil", "diesel", "seals"],
    }),
    ("sealant_tpu93", {
        "polymer": "TPU", "filler": None, "flexible": True, "shore": "93A",
        "typical": {"tg_c": [-45, -25], "tensile_modulus_mpa": [10, 40], "tensile_strength_mpa": [25, 45],
                    "elongation_pct": [450, 650], "water_absorption_24h_pct": [0.7, 1.5], "service_max_c": [80, 100]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "poor", "friction_high": "excellent", "fine_detail": "fair", "layer_hiding": "fair",
                    "printability": "fair", "elasticity": "excellent"},
        "claims": ["high_flexibility", "alkali", "acids", "oil", "diesel", "seals"],
    }),
    ("sealant_tpu_A95", {
        "polymer": "TPU", "filler": None, "flexible": True, "shore": "95A",
        "typical": {"tg_c": [-40, -20], "tensile_modulus_mpa": [20, 60], "tensile_strength_mpa": [30, 50],
                    "elongation_pct": [450, 600], "water_absorption_24h_pct": [0.7, 1.5], "service_max_c": [80, 100]},
        "ratings": {"uv": "fair", "alcohol": "good", "alkali": "good", "acids_salts": "good", "oil": "excellent",
                    "fuel": "good", "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "excellent",
                    "friction_low": "poor", "friction_high": "excellent", "fine_detail": "fair", "layer_hiding": "fair",
                    "printability": "fair", "elasticity": "good"},
        "claims": ["alkali", "acids", "oil", "diesel", "seals", "hygroscopic"],
    }),
    ("sealant_tpe", {
        "polymer": "TPE", "filler": None, "flexible": True, "shore": "A (мягкий)",
        "typical": {"tg_c": [-50, -30], "tensile_modulus_mpa": [3, 20], "tensile_strength_mpa": [5, 15],
                    "elongation_pct": [300, 700], "water_absorption_24h_pct": [0.1, 0.5], "service_max_c": [60, 80]},
        "ratings": {"uv": "fair", "alcohol": "fair", "alkali": "good", "acids_salts": "good", "oil": "fair", "fuel": "poor",
                    "ozone": "good", "hot_water": "fair", "impact": "excellent", "wear": "fair", "friction_low": "poor",
                    "friction_high": "excellent", "fine_detail": "poor", "layer_hiding": "fair", "printability": "poor",
                    "elasticity": "excellent"},
        "claims": ["seals", "flexible"],
    }),
    ("sealant_sebs", {
        "polymer": "SEBS", "filler": None, "flexible": True, "shore": "A (резиноподобный)",
        "typical": {"tg_c": [-60, -40], "tensile_modulus_mpa": [2, 10], "tensile_strength_mpa": [5, 15],
                    "elongation_pct": [400, 800], "water_absorption_24h_pct": [0.01, 0.1], "service_max_c": [60, 80]},
        "ratings": {"uv": "good", "alcohol": "good", "alkali": "excellent", "acids_salts": "excellent", "oil": "poor",
                    "fuel": "poor", "ozone": "excellent", "hot_water": "good", "impact": "excellent", "wear": "fair",
                    "friction_low": "poor", "friction_high": "excellent", "fine_detail": "poor", "layer_hiding": "fair",
                    "printability": "poor", "elasticity": "excellent"},
        "claims": ["rubber_like", "seals", "dampers"],
    }),
    ("sealant_tpv80", {
        "polymer": "TPV", "filler": None, "flexible": True, "shore": "80A",
        "typical": {"tg_c": [-60, -40], "tensile_modulus_mpa": [5, 15], "tensile_strength_mpa": [5, 12],
                    "elongation_pct": [300, 550], "water_absorption_24h_pct": [0.01, 0.1], "service_max_c": [110, 130]},
        "ratings": {"uv": "good", "alcohol": "good", "alkali": "excellent", "acids_salts": "excellent", "oil": "good",
                    "fuel": "good", "ozone": "excellent", "hot_water": "good", "impact": "excellent", "wear": "good",
                    "friction_low": "poor", "friction_high": "excellent", "fine_detail": "poor", "layer_hiding": "fair",
                    "printability": "poor", "elasticity": "excellent"},
        "claims": ["oil", "fat", "fuel", "chemical_seals"],
    }),
])

# Расходники/материалы, не подходящие как основной материал детали на Hybrid 530.
EXCLUDED = {
    "contifiber_cpa": "непрерывное волокно — нужен fiber-экструдер (Fiber 530), не Hybrid",
    "proto_pva": "растворимый материал поддержек",
    "metalcast_316l": "металлонаполненный, требует спекания; не полимерная деталь",
    "metalcast_al99": "керамика, требует спекания",
}

MACHINE = OrderedDict([
    ("definition_id", "hte530v5.2"),
    ("name", "Stereotech Hybrid 530 V5.2"),
    ("source", "STE-Slicer develop: hte530v5.2 -> steprinter530_v5.2 -> steprinter_v5 -> fdmprinter"),
    ("installed_distribution_verified", False),
    ("allowed_printing_modes", ["classic", "cylindrical", "cylindrical_full"]),
    ("build_volume_mm", OrderedDict([("classic", [315.0, 300.3, 300.0]), ("cylindrical", [315.0, 300.3, 250.0])])),
    ("machine_shape", OrderedDict([("classic", "rectangular"), ("cylindrical", "elliptic")])),
    ("machine_center_is_zero_5d", True),
    ("heated_bed", OrderedDict([("classic", True), ("cylindrical", False)])),
    ("extruders", [OrderedDict([("position", 0), ("id", "stereotech_v5_extruder_main"), ("nozzle_mm", 0.4), ("filament_mm", 1.75)]),
                   OrderedDict([("position", 1), ("id", "stereotech_v5_extruder_second"), ("nozzle_mm", 0.4), ("filament_mm", 1.75)])]),
    ("max_feedrate_mm_s", OrderedDict([("x", 150), ("y", 150), ("z", 25), ("e", 45)])),
    ("acceleration_mm_s2", 1000),
    ("default_retraction_mm", 3),
    ("cylindrical_defaults", OrderedDict([("cylindrical_mode_base_diameter", 6), ("non_printing_base_diameter", 6),
                                          ("cylindrical_raft_thickness", 0.3), ("cylindrical_round_segments", 64)])),
    ("quality_profiles", [
        OrderedDict([("quality_type", "high"), ("name", "Extra Fine"), ("layer_height", 0.06)]),
        OrderedDict([("quality_type", "normal"), ("name", "Fine"), ("layer_height", 0.1)]),
        OrderedDict([("quality_type", "fast"), ("name", "Normal"), ("layer_height", 0.15)]),
        OrderedDict([("quality_type", "draft"), ("name", "Draft"), ("layer_height", 0.2)]),
        OrderedDict([("quality_type", "verydraft"), ("name", "Extra Fast"), ("layer_height", 0.3)]),
        OrderedDict([("quality_type", "coarse"), ("name", "Coarse"), ("layer_height", 0.4)]),
    ]),
    ("notes", [
        "Профили качества — глобальные generic-профили публичной ветки; в выданном дистрибутиве 7.x их набор нужно перечитать через MachineManager.",
        "В 5D-режимах высота области печати 250 мм, стол не греется, начало координат в центре.",
    ]),
])


def build_settings_index():
    with open(SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    out = OrderedDict()

    def walk(node):
        for key, val in node.items():
            entry = OrderedDict()
            for field in ("label", "type", "unit", "default_value", "value", "minimum_value", "maximum_value",
                          "minimum_value_warning", "maximum_value_warning", "enabled", "settable_per_extruder",
                          "settable_per_mesh"):
                if field in val:
                    entry[field] = val[field]
            if "options" in val:
                entry["options"] = list(val["options"].keys())
            if val.get("type") != "category":
                out[key] = entry
            if "children" in val:
                walk(val["children"])

    for category in schema["settings"].values():
        walk(category.get("children", {}))
    return OrderedDict([("source", "data/research/fdmprinter.public.def.json (STE-Slicer develop)"),
                        ("installed_distribution_verified", False), ("settings", out)])


def _read_material(xml_text):
    root = ET.fromstring(xml_text)

    def text(path):
        node = root.find(path, NS)
        return node.text.strip() if node is not None and node.text else None

    settings = dict((s.get("key"), s.text) for s in root.findall("m:settings/m:setting", NS))
    num = lambda k: float(settings[k]) if settings.get(k) not in (None, "") else None
    return OrderedDict([
        ("label", text("m:metadata/m:name/m:label")),
        ("brand", text("m:metadata/m:name/m:brand")),
        ("guid", text("m:metadata/m:GUID")),
        ("description_ru", re.sub(r"\s+", " ", text("m:metadata/m:description") or "")),
        ("adhesion_info_ru", re.sub(r"\s+", " ", text("m:metadata/m:adhesion_info") or "")),
        ("density_g_cm3", float(text("m:properties/m:density"))),
        ("diameter_mm", float(text("m:properties/m:diameter"))),
        ("profile", OrderedDict([
            ("material_print_temperature", num("print temperature")),
            ("material_standby_temperature", num("standby temperature")),
            ("material_bed_temperature", num("heated bed temperature")),
            ("cool_fan_speed", num("print cooling")),
            ("retraction_amount", num("retraction amount")),
            ("retraction_speed", num("retraction speed")),
            ("speed_print", num("speed print")),
        ])),
    ])


def build_materials(materials_dir):
    records = []
    for mid in list(MATERIALS.keys()) + sorted(EXCLUDED.keys()):
        name = mid + ".xml.fdm_material"
        if materials_dir:
            with open(os.path.join(materials_dir, name), encoding="utf-8") as f:
                xml_text = f.read()
        else:
            xml_text = urlopen(RAW + name).read().decode("utf-8")
        rec = OrderedDict([("id", mid)])
        rec.update(_read_material(xml_text))
        rec["source_url"] = RAW + name
        if mid in EXCLUDED:
            rec["excluded"] = EXCLUDED[mid]
        else:
            props = MATERIALS[mid]
            rec["polymer"] = props["polymer"]
            rec["filler"] = props["filler"]
            rec["flexible"] = props["flexible"]
            rec["shore"] = props.get("shore")
            rec["marks_risk"] = props.get("marks_risk", False)
            rec["claims"] = props.get("claims", [])
            rec["typical"] = OrderedDict(sorted(props["typical"].items()))
            rec["ratings"] = OrderedDict(sorted(props["ratings"].items()))
            rec["evidence"] = OrderedDict([
                ("profile", "ste_material_profile"),
                ("claims", "ste_material_description"),
                ("typical", "polymer_class_typical: приблизительные интервалы класса полимера, не паспорт SKU"),
                ("ratings", "polymer_class_typical + ste_material_description"),
            ])
        records.append(rec)
    return OrderedDict([("schema_version", "1.0"), ("research_date", "2026-10-23"),
                        ("automatic_qualification_allowed", False), ("materials", records)])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--materials-dir", help="каталог с *.xml.fdm_material (иначе скачать с GitHub)")
    args = parser.parse_args(argv)
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    outputs = [("settings_index.json", build_settings_index()),
               ("materials_ste.json", build_materials(args.materials_dir)),
               ("machine_hybrid530.json", MACHINE)]
    for name, data in outputs:
        path = os.path.join(OUT_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("written", os.path.relpath(path, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
