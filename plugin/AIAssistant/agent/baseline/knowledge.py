# -*- coding: utf-8 -*-
"""Загрузка базы знаний (data/*.json), собранной scripts/build_knowledge.py."""

import io
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CACHE = {}


def _load(name):
    if name not in _CACHE:
        with io.open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            _CACHE[name] = json.load(f)
    return _CACHE[name]


def materials(include_excluded=False):
    items = _load("materials_ste.json")["materials"]
    return [m for m in items if include_excluded or not m.get("excluded")]


def material(material_id):
    for m in materials(include_excluded=True):
        if m["id"] == material_id:
            return m
    raise KeyError(material_id)


def machine():
    return _load("machine_hybrid530.json")


def settings_index():
    return _load("settings_index.json")["settings"]
