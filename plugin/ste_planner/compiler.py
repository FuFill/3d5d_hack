# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Settings compiler: physical decisions -> validated STE Slicer patch (section 12).

Each patch item is {key, scope, target, value, unit, reason_requirement_ids,
model_ids, old_effective_value, stage}. The compiler:
- writes only keys that exist in the installed schema, with the right type;
- converts fractions to percent only here (material_flow 0.98 -> 98);
- refuses printing modes outside the task whitelist;
- refuses writing both a parent and a dependent child (e.g. wall_thickness and
  wall_line_count) and reports writes to settings disabled in the candidate;
- validates every value with the schema bounds after the whole patch is applied
  (minimum/maximum are errors, *_warning are warnings);
- orders items by the dependency stages of section 12.
"""

from .result import is_number
from .settings_schema import SettingResolver, EvalError

ALLOWED_MODES = ("classic", "cylindrical", "cylindrical_full")

STAGES = ["configuration", "geometry", "thermal_flow", "motion", "cooling_retraction", "support_adhesion",
          "compensation", "five_axis"]

# physical quantity -> (setting key, unit of the decision, conversion to slicer, stage)
QUANTITIES = {
    "printing_mode": ("printing_mode", None, None, "configuration"),
    "nozzle_size": ("machine_nozzle_size", "mm", None, "configuration"),
    "layer_height": ("layer_height", "mm", None, "geometry"),
    "initial_layer_height": ("layer_height_0", "mm", None, "geometry"),
    "initial_layer_height_5d": ("cylindrical_layer_height_0", "mm", None, "geometry"),
    "line_width": ("line_width", "mm", None, "geometry"),
    "wall_count": ("wall_line_count", "count", "int", "geometry"),
    "wall_thickness": ("wall_thickness", "mm", None, "geometry"),
    "top_thickness": ("top_thickness", "mm", None, "geometry"),
    "bottom_thickness": ("bottom_thickness", "mm", None, "geometry"),
    "top_layers": ("top_layers", "count", "int", "geometry"),
    "bottom_layers": ("bottom_layers", "count", "int", "geometry"),
    "infill_fraction": ("infill_sparse_density", "fraction", "percent", "geometry"),
    "infill_pattern": ("infill_pattern", None, None, "geometry"),
    "nozzle_temperature": ("material_print_temperature", "degC", None, "thermal_flow"),
    "initial_nozzle_temperature": ("material_print_temperature_layer_0", "degC", None, "thermal_flow"),
    "bed_temperature": ("material_bed_temperature", "degC", None, "thermal_flow"),
    "flow_factor": ("material_flow", "fraction", "percent", "thermal_flow"),
    "speed_print": ("speed_print", "mm/s", None, "motion"),
    "speed_wall_outer": ("speed_wall_0", "mm/s", None, "motion"),
    "speed_wall_inner": ("speed_wall_x", "mm/s", None, "motion"),
    "speed_infill": ("speed_infill", "mm/s", None, "motion"),
    "speed_topbottom": ("speed_topbottom", "mm/s", None, "motion"),
    "speed_travel": ("speed_travel", "mm/s", None, "motion"),
    "speed_initial_layer": ("speed_layer_0", "mm/s", None, "motion"),
    "fan_enabled": ("cool_fan_enabled", None, "bool", "cooling_retraction"),
    "fan_fraction": ("cool_fan_speed", "fraction", "percent", "cooling_retraction"),
    "min_layer_time": ("cool_min_layer_time", "s", None, "cooling_retraction"),
    "retraction_enabled": ("retraction_enable", None, "bool", "cooling_retraction"),
    "retraction_amount": ("retraction_amount", "mm", None, "cooling_retraction"),
    "retraction_speed": ("retraction_speed", "mm/s", None, "cooling_retraction"),
    "support_enabled": ("support_enable", None, "bool", "support_adhesion"),
    "support_angle": ("support_angle", "deg", None, "support_adhesion"),
    "support_type": ("support_type", None, None, "support_adhesion"),
    "support_z_distance": ("support_z_distance", "mm", None, "support_adhesion"),
    "support_xy_distance": ("support_xy_distance", "mm", None, "support_adhesion"),
    "adhesion_type": ("adhesion_type", None, None, "support_adhesion"),
    "brim_width": ("brim_width", "mm", None, "support_adhesion"),
    "seam_type": ("z_seam_type", None, None, "support_adhesion"),
    "xy_offset": ("xy_offset", "mm", None, "compensation"),
    "base_diameter_5d": ("cylindrical_mode_base_diameter", "mm", None, "five_axis"),
    "non_printing_base_diameter": ("non_printing_base_diameter", "mm", None, "five_axis"),
    "overlap_5d": ("cylindrical_mode_overlap", "mm", None, "five_axis"),
    "round_segments_5d": ("cylindrical_round_segments", None, None, "five_axis"),
}

# Pairs where one setting is computed from the other by default: write only one of them.
EXCLUSIVE = [
    ("wall_thickness", "wall_line_count"),
    ("top_thickness", "top_layers"),
    ("bottom_thickness", "bottom_layers"),
    ("top_bottom_thickness", "top_layers"),
    ("infill_sparse_density", "infill_line_distance"),
]


class Decision(object):
    """A physical decision with justification."""

    def __init__(self, quantity, value, reason_requirement_ids=None, model_ids=None, note=None, key=None,
                 extruder=None):
        self.quantity = quantity
        self.value = value
        self.reason_requirement_ids = list(reason_requirement_ids or [])
        self.model_ids = list(model_ids or [])
        self.note = note
        self.key = key            # explicit setting key (bypasses the quantity table)
        self.extruder = extruder  # explicit extruder position for per-extruder keys

    def to_dict(self):
        return dict(self.__dict__)


def _convert(value, conversion):
    if conversion == "percent":
        if not is_number(value):
            raise ValueError("fraction expected")
        if value > 1.5:
            raise ValueError("value %r looks like a percentage; decisions use fractions" % (value,))
        return round(float(value) * 100.0, 6)
    if conversion == "int":
        if not is_number(value) or int(round(value)) != value:
            raise ValueError("integer expected")
        return int(round(value))
    if conversion == "bool":
        if not isinstance(value, bool):
            raise ValueError("bool expected")
        return value
    return value


def compile_patch(decisions, resolver, extruder="0"):
    """Compile decisions into a validated patch against the resolver's schema."""
    schema = resolver.schema
    items, errors, warnings = [], [], []
    seen = {}
    for d in decisions:
        if d.key:
            key, unit, conv, stage = d.key, None, None, "configuration"
            for q, spec in QUANTITIES.items():
                if spec[0] == d.key:
                    unit, conv, stage = spec[1], spec[2], spec[3]
        elif d.quantity in QUANTITIES:
            key, unit, conv, stage = QUANTITIES[d.quantity]
        else:
            errors.append({"quantity": d.quantity, "error": "unknown physical quantity"})
            continue
        if not schema.has(key):
            errors.append({"key": key, "error": "setting not present in the installed schema"})
            continue
        if key == "printing_mode" and d.value not in ALLOWED_MODES:
            errors.append({"key": key, "error": "mode %r is not allowed by the task (%s)" %
                           (d.value, ", ".join(ALLOWED_MODES))})
            continue
        try:
            value = _convert(d.value, conv)
        except ValueError as e:
            errors.append({"key": key, "error": str(e), "value": d.value})
            continue
        definition = schema.definition(key)
        if definition.type == "float" and is_number(value):
            value = float(value)
        per_ext = definition.per_extruder
        target_ext = (d.extruder if d.extruder is not None else extruder) if per_ext else None
        if key in seen:
            errors.append({"key": key, "error": "written twice in one patch"})
            continue
        seen[key] = True
        try:
            old = resolver.value(key, extruder=target_ext)
        except EvalError:
            old = None
        items.append({
            "key": key, "scope": "extruder" if per_ext else "global",
            "target": ("extruder_%s" % target_ext) if per_ext else "global",
            "value": value, "unit": definition.get("unit") or unit, "decision_unit": unit,
            "reason_requirement_ids": d.reason_requirement_ids, "model_ids": d.model_ids,
            "old_effective_value": old, "stage": stage, "note": d.note,
        })
    patched = validate_items(items, resolver, errors, warnings)
    return {"items": items, "errors": errors, "warnings": warnings, "ok": not errors,
            "resolver": patched}


def validate_items(items, resolver, errors, warnings):
    """Order compiled items, check dependent pairs, bounds and enabled state over the whole patch.

    Items are already in slicer units. Returns the patched resolver; errors and
    warnings are appended in place.
    """
    keys = set(i["key"] for i in items)
    for a, b in EXCLUSIVE:
        if a in keys and b in keys:
            errors.append({"keys": [a, b], "error": "dependent settings written together; choose one "
                                                     "independent variable"})
    items.sort(key=lambda i: (STAGES.index(i["stage"]), i["key"]))
    # validate after applying the whole patch (bounds depend on other settings)
    user_global = dict((i["key"], i["value"]) for i in items if i["scope"] == "global")
    user_ext = {}
    for i in items:
        if i["scope"] == "extruder":
            user_ext.setdefault(i["target"].split("_")[1], {})[i["key"]] = i["value"]
    patched = resolver.copy_with(user_global, user_ext)
    for i in items:
        ext = i["target"].split("_")[1] if i["scope"] == "extruder" else None
        state, msgs = patched.validate(i["key"], i["value"], extruder=ext)
        i["validation"] = state
        if state == "error":
            errors.append({"key": i["key"], "error": "; ".join(msgs), "value": i["value"]})
        elif state == "warning":
            warnings.append({"key": i["key"], "warning": "; ".join(msgs), "value": i["value"]})
        en = patched.enabled(i["key"], extruder=ext)
        i["enabled_after_patch"] = en
        if en is False:
            warnings.append({"key": i["key"], "warning": "setting is disabled in this configuration; "
                                                         "the value will not take effect"})
    return patched


def effective_settings(resolver, keys, extruder="0"):
    """Read back effective values (offline preview of read-after-write)."""
    out = {}
    for k in keys:
        try:
            out[k] = resolver.value(k, extruder=extruder)
        except EvalError as e:
            out[k] = {"error": str(e)}
    return out


def patch_to_slicer_payload(patch):
    """Plain payload for the plugin's apply_patch tool: {global: {...}, extruders: {pos: {...}}}."""
    payload = {"global": {}, "extruders": {}}
    for i in patch["items"]:
        if i["scope"] == "global":
            payload["global"][i["key"]] = i["value"]
        else:
            payload["extruders"].setdefault(i["target"].split("_")[1], {})[i["key"]] = i["value"]
    return payload


def diff_with_user_changes(patch, existing_user_changes):
    """Existing user changes may override the profile; list them explicitly with the patch."""
    keys = set(i["key"] for i in patch["items"])
    return {"overwritten": sorted(k for k in existing_user_changes if k in keys),
            "kept": sorted(k for k in existing_user_changes if k not in keys)}


__all__ = ["Decision", "compile_patch", "effective_settings", "patch_to_slicer_payload", "ALLOWED_MODES",
           "QUANTITIES", "SettingResolver"]
