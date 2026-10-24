# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Pre-slice estimate of print time and material, to compare variants without touching the project.

Comparing materials and poses must not switch presets or turn parts in the open project
(the user would see every trial). The variants are compared by this estimate instead and
only the chosen result is applied and sliced by STE.

Material: the core's thin-shell split and mass model (process.estimate_part_volumes and
mass_estimate) with the density of the slicer's material file. Time: the extruded volume
over the bead cross-section at the print speed (an efficiency factor covers accelerations
and slower walls), per layer at least the minimal layer time, plus a layer change. About
+-25 % against native STE slices, good enough to rank variants of the same part;
the chat marks these numbers with "≈" and the slicer's own numbers come after slicing.
"""

import json
import os
import re
import sys

# Fitted to native STE slices in experiments/ste_runtime/final (baseline, live_demo, batch): +-25 %.
SPEED_EFFICIENCY = 0.66    # share of speed_print reached on average (accelerations, walls, small features)
MIN_LAYER_SECONDS = 5.0    # a small layer is slowed down to cool (cool_min_layer_time)
LAYER_CHANGE_SECONDS = 1.0  # layer change, travel and retractions per layer
SUPPORT_FILL = 0.15        # printed share of the support region (default support density)
DEFAULT_DENSITY = 1.24     # g/cm3 (PLA) when the material is unknown
DEFAULTS = {"layer_height": 0.2, "line_width": 0.4, "wall_line_count": 2, "top_layers": 4, "bottom_layers": 4,
            "infill_sparse_density": 20.0, "speed_print": 50.0, "support_enable": False,
            "cool_min_layer_time": MIN_LAYER_SECONDS}

_meshes = {}
_densities = None


def _core():
    from .core_worker import core_path
    if core_path() not in sys.path:
        sys.path.insert(0, core_path())
    from ste_planner import geometry, process
    return geometry, process


def _words(text):
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def density(material):
    """g/cm3 of a core material id or an STE material name (e.g. "Enduse PETG"), from the slicer files."""
    global _densities
    if _densities is None:
        from .core_worker import core_path
        path = os.path.join(core_path(), "ste_planner", "data", "materials_stereotech.json")
        _densities = {}
        try:
            with open(path, encoding="utf-8") as handle:
                for item in json.load(handle).get("materials") or []:
                    value = (item.get("slicer") or {}).get("density")
                    if isinstance(value, (int, float)) and value > 0:
                        _densities[item["id"]] = (float(value), _words(item.get("label")) | _words(item["id"]))
        except (OSError, ValueError):
            pass
    if material in _densities:
        return _densities[material][0]
    words = _words(material)
    matches = [(len(w), d) for d, w in _densities.values() if words and words <= w]
    return min(matches)[1] if matches else DEFAULT_DENSITY


def mesh_info(path):
    """(mesh, volume mm3, area mm2) of an STL, cached while the file is unchanged."""
    geometry, _process = _core()
    key = (path, os.path.getmtime(path))
    if key not in _meshes:
        mesh = geometry.load_stl(path)
        info = geometry.analyze_mesh(mesh)
        volume = info.get("volume_mm3")
        if not volume:  # not watertight: half the bounding box as a rough stand-in
            size = info.get("size") or [0, 0, 0]
            volume = 0.5 * size[0] * size[1] * size[2]
        _meshes.clear()
        _meshes[key] = (mesh, float(volume), float(info.get("area_mm2") or 0.0))
    return _meshes[key]


def pose(path, rotation, settings):
    """Height, support volume and base area of the part in `rotation` (printer frame)."""
    geometry, _process = _core()
    mesh, _volume, _area = mesh_info(path)
    evaluated = geometry.evaluate_orientation(mesh, rotation[2], float(settings.get("support_angle") or 50.0),
                                              _number(settings, "layer_height"), rotation=rotation)
    return {"height": evaluated["height"], "support_volume_upper": evaluated["support_volume_upper"],
            "base_area": evaluated["base_area"]}


def _number(settings, key):
    value = settings.get(key)
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = None
    return value if value is not None and value > 0 or key == "infill_sparse_density" and value == 0 \
        else float(DEFAULTS[key])


def estimate(path, settings, placement, material=None):
    """{"print_time_s", "material_g", "estimate": True} of the part with these settings and placement."""
    _geometry, process = _core()
    _mesh, volume, area = mesh_info(path)
    h, w = _number(settings, "layer_height"), _number(settings, "line_width")
    walls = _number(settings, "wall_line_count")
    skins = _number(settings, "top_layers") + _number(settings, "bottom_layers")
    infill = _number(settings, "infill_sparse_density") / 100.0
    speed = _number(settings, "speed_print")
    split = process.estimate_part_volumes(volume, area, walls * w).value or {"V_shell": volume, "V_core": 0.0}
    shell, core = split["V_shell"], split["V_core"]
    skin = min(core, float(placement.get("base_area") or 0.0) * skins * h)
    core -= skin
    supports = settings.get("support_enable")
    support = SUPPORT_FILL * float(placement.get("support_volume_upper") or 0.0) if supports in (True, "true") else 0.0
    mass = process.mass_estimate(density(material) / 1000.0, shell, min(1.0, infill), core, skin, support).value
    extruded = shell + skin + infill * core + support
    height = placement.get("height")
    if not height:  # 5D plans have no layer height along Z: the longest size of the part stands in
        mesh, _v, _a = mesh_info(path)
        height = float((mesh.vertices.max(axis=0) - mesh.vertices.min(axis=0)).max())
    layers = max(1.0, float(height) / h)
    per_layer = extruded / layers / (w * h * speed * SPEED_EFFICIENCY)
    seconds = layers * (max(per_layer, _number(settings, "cool_min_layer_time")) + LAYER_CHANGE_SECONDS)
    return {"print_time_s": int(round(seconds)), "material_g": round(mass["material_g"], 1) if mass else None,
            "estimate": True}
