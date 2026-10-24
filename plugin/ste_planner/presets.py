# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Native STE preset base: catalog snapshot, compatible base selection, minimal override.

The Qt side (runtime_adapter.build_preset_catalog) serialises the installed
registry into a JSON catalog; this module never imports Qt and runs in the
modern worker as well as in STE's Python 3.5.

    P  ordered container stacks of the chosen base (quality_changes, quality,
       material, variant, definition_changes for the global and extruder stacks)
    B  = Resolve(P)        baseline effective settings
    E  = Resolve(P, Delta) effective settings after the user container
    Delta = only independent, available and justified changes

Resolve is SettingResolver with the stack order of STE's _ContainerIndexes;
enum/bool/formula/per-extruder values follow its rules, not numeric addition.

Quality groups reproduce steslicer/Machines/QualityManager.getQualityGroups of
the public STE-Slicer source. Its equivalence with the installed 7.10.12 is
checked by read-back of the applied containers, not assumed here.
"""

import configparser
import hashlib
import json
import os
import xml.etree.ElementTree as ET

from . import compiler
from .settings_schema import SettingResolver, EvalError, MATERIAL_SETTING_MAP

CATALOG_SCHEMA = "ste_preset_catalog/1"
CONTRACT = "native_preset_base/1"
DEFAULT_QUALITY_DEFINITION = "fdmprinter"
STACK_LAYERS = ("quality_changes", "quality", "material", "variant", "definition_changes")

# Kept even when equal to the base: the mode is selected explicitly for the
# backend switch; STE takes max() of the bed temperature over all extruders.
EXPLICIT_KEYS = {"printing_mode": "explicit printing mode selects the STE backend",
                 "material_bed_temperature": "explicit because STE takes max() over extruders"}
# Models that encode a requirement of the geometry, route, machine or material.
CONSTRAINT_MODELS = ("choose_route", "plan_5d", "support_plan", "thermal_window", "material_datasheet",
                     "role_speed")
# Generic policy seeds. They change the base only where the TZ load class needs
# them; otherwise the verified preset value is inherited.
POLICY_MODELS = ("wall_count", "skin_layers", "choose_layer_height", "infill_penalty")
CLASS_JUSTIFIED = {"structural": ("layer_height", "wall_line_count", "top_layers", "bottom_layers",
                                  "infill_sparse_density"),
                   "elastic": ("layer_height", "wall_line_count", "top_layers", "bottom_layers",
                               "infill_sparse_density"),
                   "decorative": ("layer_height",),
                   "general": ()}


class PresetError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def container_digest(entry):
    """SHA-256 of a serialised container: id, type, compatibility metadata and values."""
    body = dict((k, entry.get(k)) for k in sorted(entry) if k not in ("digest",))
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def catalog_digest(catalog):
    return hashlib.sha256(_canonical(catalog).encode("utf-8")).hexdigest()


def _with_digest(entry):
    entry = dict(entry)
    entry["digest"] = container_digest(entry)
    return entry


def validate_catalog(catalog):
    """Structural errors of a catalog snapshot (empty list when valid)."""
    errors = []
    if not isinstance(catalog, dict) or catalog.get("schema") != CATALOG_SCHEMA:
        return ["preset catalog schema %s required" % CATALOG_SCHEMA]
    machine = catalog.get("machine")
    if not isinstance(machine, dict) or not machine.get("definition_id"):
        errors.append("machine.definition_id required")
    extruders = catalog.get("extruders")
    if not isinstance(extruders, dict) or not extruders:
        errors.append("extruders required")
        extruders = {}
    for pos, ext in extruders.items():
        if not isinstance(ext, dict):
            errors.append("extruder %s must be an object" % pos)
            continue
        if not isinstance(ext.get("available_materials"), list):
            errors.append("extruder %s: available_materials list required" % pos)
    for kind in ("qualities", "quality_changes"):
        if not isinstance(catalog.get(kind), list):
            errors.append(kind + " list required")
    for entry in _containers(catalog):
        if entry.get("digest") != container_digest(entry):
            errors.append("container digest mismatch: %s" % entry.get("id"))
    return errors


def _containers(catalog):
    out = []
    machine = catalog.get("machine") or {}
    for key in ("definition_changes", "variant"):
        if isinstance(machine.get(key), dict):
            out.append(machine[key])
    for ext in (catalog.get("extruders") or {}).values():
        if not isinstance(ext, dict):
            continue
        for key in ("definition_changes", "variant"):
            if isinstance(ext.get(key), dict):
                out.append(ext[key])
        out.extend(m for m in ext.get("available_materials") or [] if isinstance(m, dict))
        if isinstance(ext.get("current_material"), dict):
            out.append(ext["current_material"])
    out.extend(q for q in catalog.get("qualities") or [] if isinstance(q, dict))
    for group in catalog.get("quality_changes") or []:
        if isinstance(group, dict):
            parts = [group.get("global")] + list((group.get("extruders") or {}).values())
            out.extend(p for p in parts if isinstance(p, dict))
    return out


# ---------------------------------------------------------------- quality groups

class _Node(object):
    """QualityNode: quality_type -> metadata, plus named children (variant/buildplate/material)."""

    def __init__(self):
        self.types = []          # ordered (quality_type, entry), insertion order as in the registry
        self.children = {}

    def add(self, entry):
        if all(qt != entry["quality_type"] for qt, _ in self.types):
            self.types.append((entry["quality_type"], entry))

    def first_is_global(self):
        return bool(self.types) and bool(self.types[0][1].get("global_quality"))


def _quality_tree(catalog):
    tree = {}
    faulty = []
    for entry in catalog.get("qualities") or []:
        nozzle, buildplate, material = entry.get("variant"), entry.get("buildplate"), entry.get("material")
        is_global = bool(entry.get("global_quality")) or (material is None and nozzle is None and buildplate is None)
        if is_global and (material or nozzle):
            faulty.append(entry["id"])
            continue
        node = tree.setdefault(entry["definition"], _Node())
        if is_global:
            node.add(dict(entry, global_quality=True))
            continue
        for name in (nozzle, buildplate, material):
            if name is not None:
                node = node.children.setdefault(name, _Node())
        node.add(entry)
    return tree, faulty


def quality_groups(catalog, extruder_materials):
    """quality_type -> {name, global, extruders{pos: entry}, available} as QualityManager computes them.

    extruder_materials: {pos: material entry or None} for the configuration under test.
    """
    machine = catalog["machine"]
    tree, _ = _quality_tree(catalog)
    definition = machine.get("quality_definition_id") or DEFAULT_QUALITY_DEFINITION
    machine_node = tree.get(definition)
    default_node = tree.get(DEFAULT_QUALITY_DEFINITION)
    has_extruder_specific = bool(machine_node and machine_node.children)
    groups = {}
    for node in (machine_node, default_node):
        if node is not None and node.types:
            if not node.first_is_global():
                continue
            for quality_type, entry in node.types:
                groups[quality_type] = {"quality_type": quality_type, "name": entry.get("name", ""),
                                        "global": entry, "extruders": {}}
            break
    buildplate = machine.get("buildplate_name")
    for pos in sorted(catalog["extruders"]):
        ext = catalog["extruders"][pos]
        variant = ext.get("variant")
        nozzle = variant.get("name") if variant else None
        material = extruder_materials.get(pos)
        has_material = material is not None
        root_ids = []
        if has_material:
            for rid in [material.get("root_material_id")] + list(material.get("fallback_root_ids") or []):
                if rid and rid not in root_ids:
                    root_ids.append(rid)
        info = [nozzle, buildplate, material.get("root_material_id") if has_material else None]
        nodes = []

        def add_nodes(node, index):
            if node is None:
                return
            if index < len(info):
                name = info[index]
                if name is not None:
                    child = node.children.get(name)
                    if child is not None and has_material:
                        add_nodes(child, index + 1)
            if has_material:
                for rid in root_ids:
                    material_node = node.children.get(rid)
                    if material_node is not None:
                        nodes.append(material_node)
                        break
            nodes.append(node)

        add_nodes(machine_node, 0)
        if machine_node is not None and machine_node.types:
            nodes.append(machine_node)
        elif default_node is not None:
            nodes.append(default_node)
        for index, node in enumerate(nodes):
            if node is not None and node.types:
                if has_extruder_specific and node.first_is_global():
                    continue
                for quality_type, entry in node.types:
                    group = groups.setdefault(quality_type, {"quality_type": quality_type,
                                                             "name": entry.get("name", ""),
                                                             "global": None, "extruders": {}})
                    group["extruders"].setdefault(pos, entry)
            if has_extruder_specific and index == len(nodes) - 1:
                break
    used = [pos for pos, ext in catalog["extruders"].items() if ext.get("enabled", True)]
    for group in groups.values():
        group["available"] = group["global"] is not None and all(p in group["extruders"] for p in used)
    return groups


# ---------------------------------------------------------------- base selection

def _material_matches(ext, file_id, guid):
    """Installed materials of one extruder that are this exact material (id, then GUID); no name matching."""
    found, rejected = [], []
    for entry in ext.get("available_materials") or []:
        if entry.get("base_file") != file_id:
            continue
        if guid and entry.get("guid") and entry["guid"] != guid:
            rejected.append({"id": entry.get("id"), "reason": "material GUID differs from the planner catalog"})
            continue
        found.append(entry)
    return found, rejected


def _quality_conflicts(entries, mode):
    for entry in entries:
        value = (entry or {}).get("values", {}).get("printing_mode")
        if value is not None and not str(value).startswith("=") and value != mode:
            return "profile fixes printing_mode=%s, plan requires %s" % (value, mode)
    return None


def candidate_label(candidate):
    q = candidate["quality_group"]
    label = "%s (%s)" % (q.get("name") or q["quality_type"], q["quality_type"])
    if candidate.get("quality_changes"):
        label = "%s / %s" % (candidate["quality_changes"]["name"], label)
    return label


def build_layers(catalog, candidate):
    """Ordered instance layers (global, {pos: layers}) of a candidate, STE stack order below user."""
    machine = catalog["machine"]
    group, qc = candidate["quality_group"], candidate.get("quality_changes") or {}
    qc_global = qc.get("global") or {}
    global_layers = [("quality_changes", qc_global.get("values", {})),
                     ("quality", (group.get("global") or {}).get("values", {})),
                     ("material", {}),
                     ("variant", (machine.get("variant") or {}).get("values", {})),
                     ("definition_changes", (machine.get("definition_changes") or {}).get("values", {}))]
    extruder_layers = {}
    for pos, ext in catalog["extruders"].items():
        material = candidate["materials"].get(pos) or {}
        quality = group["extruders"].get(pos) or {}
        extruder_layers[pos] = [("quality_changes", ((qc.get("extruders") or {}).get(pos) or {}).get("values", {})),
                                ("quality", quality.get("values", {})),
                                ("material", material.get("values", {})),
                                ("variant", (ext.get("variant") or {}).get("values", {})),
                                ("definition_changes", (ext.get("definition_changes") or {}).get("values", {}))]
    return global_layers, extruder_layers


def resolver_for(schema, catalog, candidate=None):
    """Resolve(P); without a candidate only the hardware layers (variant, definition_changes)."""
    if candidate is None:
        machine = catalog["machine"]
        global_layers = [("variant", (machine.get("variant") or {}).get("values", {})),
                         ("definition_changes", (machine.get("definition_changes") or {}).get("values", {}))]
        extruder_layers = dict((pos, [("variant", (ext.get("variant") or {}).get("values", {})),
                                      ("definition_changes", (ext.get("definition_changes") or {}).get("values", {}))])
                               for pos, ext in catalog["extruders"].items())
    else:
        global_layers, extruder_layers = build_layers(catalog, candidate)
    return SettingResolver(schema, global_layers=global_layers, extruder_layers=extruder_layers)


def _ref(entry):
    if not entry:
        return None
    return {"id": entry.get("id"), "name": entry.get("name"), "digest": entry.get("digest"),
            "setting_version": entry.get("setting_version")}


def _material_ref(entry):
    ref = _ref(entry)
    if ref is not None:
        ref.update({"guid": entry.get("guid"), "base_file": entry.get("base_file")})
    return ref


def base_presets_record(catalog, candidate):
    """Provenance of P: ids, names, digests and the ordered stacks (index 0 = user, not part of P)."""
    machine = catalog["machine"]
    group, qc = candidate["quality_group"], candidate.get("quality_changes") or {}
    record = {"contract": CONTRACT, "catalog_sha256": candidate["catalog_sha256"],
              "ste_version": catalog.get("ste_version"), "setting_version": catalog.get("setting_version"),
              "source": catalog.get("source"), "machine": {"definition_id": machine["definition_id"],
                                                          "stack_id": machine.get("stack_id")},
              "quality_type": group["quality_type"], "quality_name": group.get("name"),
              "quality_changes_name": qc.get("name"), "label": candidate_label(candidate),
              "global": {"quality_changes": _ref(qc.get("global")), "quality": _ref(group.get("global")),
                         "variant": _ref(machine.get("variant")),
                         "definition_changes": _ref(machine.get("definition_changes"))},
              "extruders": {}}
    for pos, ext in sorted(catalog["extruders"].items()):
        record["extruders"][pos] = {
            "stack_id": ext.get("stack_id"), "definition_id": ext.get("definition_id"),
            "enabled": ext.get("enabled", True),
            "quality_changes": _ref((qc.get("extruders") or {}).get(pos)),
            "quality": _ref(group["extruders"].get(pos)),
            "material": _material_ref(candidate["materials"].get(pos)),
            "variant": _ref(ext.get("variant")),
            "definition_changes": _ref(ext.get("definition_changes"))}
    return record


def base_identity(record):
    """Calibration context of P: the exact container contents, not display names."""
    digests = []
    for layer in STACK_LAYERS:
        ref = record["global"].get(layer)
        if ref:
            digests.append("global/%s:%s" % (layer, ref["digest"]))
    for pos, part in sorted(record["extruders"].items()):
        for layer in STACK_LAYERS:
            ref = part.get(layer)
            if ref:
                digests.append("%s/%s:%s" % (pos, layer, ref["digest"]))
    return {"label": record["label"], "quality_type": record["quality_type"],
            "quality_changes_name": record["quality_changes_name"], "container_digests": sorted(digests)}


def same_base(a, b):
    return (isinstance(a, dict) and isinstance(b, dict) and
            sorted(a.get("container_digests") or []) == sorted(b.get("container_digests") or []) and
            bool(a.get("container_digests")))


def stack_refs(record, position=None):
    """Recorded references of a stack by STE index 1..5 (None means the empty container)."""
    part = record["global"] if position is None else record["extruders"][str(position)]
    return [part.get(layer) for layer in STACK_LAYERS]


def stack_ids(record, position=None):
    return [(ref or {}).get("id") for ref in stack_refs(record, position)]


def select_base(catalog, context, evaluate):
    """Deterministic base choice: compatibility, then mandatory requirements, then preference.

    context: machine_id, extruder, material_file_id, material_guid, printing_mode,
      required_nozzle_mm, allowed_quality_changes (names explicitly allowed by the application).
    evaluate(candidate) -> {"delta_count", "errors"}: minimal patch of the plan over that base.
    Preference: fewest overrides, then the machine's preferred_quality_type, then native quality
    before an allowed custom profile, then quality_type/name (stable tie-break). The GUI-current
    profile is not a criterion.
    """
    rejected, needs = [], []
    errors = validate_catalog(catalog)
    if errors:
        return _needs_input("invalid preset catalog", errors, rejected)
    machine = catalog["machine"]
    if machine["definition_id"] != context["machine_id"]:
        return _needs_input("catalog belongs to another machine",
                            ["select %s in STE or plan for %s" % (context["machine_id"], machine["definition_id"])],
                            rejected)
    pos = str(context.get("extruder", "0"))
    ext = catalog["extruders"].get(pos)
    if ext is None or not ext.get("enabled", True):
        return _needs_input("planned extruder %s is missing or disabled" % pos, ["enable the extruder in STE"], rejected)
    installed = ext.get("nozzle_size")
    required = context.get("required_nozzle_mm")
    if required is not None and (installed is None or required > float(installed) + 1e-9):
        return _needs_input("hardware change required: material needs nozzle >= %g mm, installed %s mm" %
                            (required, installed),
                            ["install the nozzle physically and select the matching nozzle in STE, then plan again",
                             "choose a material compatible with the installed nozzle"], rejected)
    matches, material_rejections = _material_matches(ext, context["material_file_id"], context.get("material_guid"))
    rejected.extend(material_rejections)
    if not matches:
        return _needs_input("material %s is not installed/compatible for extruder %s with nozzle %s" %
                            (context["material_file_id"], pos, (ext.get("variant") or {}).get("name") or "default"),
                            ["install the Stereotech material profile in STE", "choose another installed material"],
                            rejected)
    if len(matches) > 1:
        return _needs_input("ambiguous installed material containers for %s" % context["material_file_id"],
                            ["remove duplicate material profiles: " + ", ".join(sorted(m["id"] for m in matches))],
                            rejected)
    diameter = ext.get("material_diameter")
    approx = matches[0].get("approximate_diameter")
    if diameter is not None and approx is not None and int(round(float(diameter))) != int(round(float(approx))):
        return _needs_input("filament diameter %s mm differs from the extruder %s mm" % (approx, diameter),
                            ["select a material for the installed filament diameter"], rejected)
    materials = dict((p, e.get("current_material")) for p, e in catalog["extruders"].items())
    materials[pos] = matches[0]
    groups = quality_groups(catalog, materials)
    mode = context["printing_mode"]
    setting_version = catalog.get("setting_version")
    candidates = []
    for quality_type in sorted(groups):
        group = groups[quality_type]
        entries = [group["global"]] + list(group["extruders"].values())
        reason = None
        if not group["available"]:
            reason = "not available for the installed nozzle/material on every enabled extruder"
        elif setting_version is not None and any(e and e.get("setting_version") not in (None, setting_version)
                                                 for e in entries):
            reason = "setting_version differs from STE %s" % setting_version
        else:
            reason = _quality_conflicts(entries, mode)
        if reason:
            rejected.append({"quality_type": quality_type, "reason": reason})
            continue
        candidates.append({"quality_group": group, "quality_changes": None, "materials": materials})
    allowed = set(context.get("allowed_quality_changes") or [])
    for qc in catalog.get("quality_changes") or []:
        if qc["name"] not in allowed:
            continue
        base = next((c for c in candidates if c["quality_changes"] is None and
                     c["quality_group"]["quality_type"] == qc["quality_type"]), None)
        reason = None
        if base is None:
            reason = "its quality_type %s is not available here" % qc["quality_type"]
        elif qc.get("definition") != (machine.get("quality_definition_id") or DEFAULT_QUALITY_DEFINITION):
            reason = "defined for another machine"
        else:
            reason = _quality_conflicts([qc.get("global")] + list((qc.get("extruders") or {}).values()), mode)
        if reason:
            rejected.append({"quality_changes": qc["name"], "reason": reason})
            continue
        candidates.append({"quality_group": base["quality_group"], "quality_changes": qc, "materials": materials})
    missing_allowed = sorted(allowed - set(q["name"] for q in catalog.get("quality_changes") or []))
    for name in missing_allowed:
        rejected.append({"quality_changes": name, "reason": "allowed custom profile is not installed"})
    if not candidates:
        return _needs_input("no compatible native quality profile",
                            ["select a printer/nozzle/material combination that has quality profiles in STE"],
                            rejected)
    digest = catalog_digest(catalog)
    preferred = machine.get("preferred_quality_type")
    scored = []
    for c in candidates:
        c["catalog_sha256"] = digest
        result = evaluate(c)
        if result["errors"]:
            rejected.append({"candidate": candidate_label(c), "reason": "plan overrides fail validation",
                             "errors": result["errors"]})
            continue
        key = (result["delta_count"], 0 if c["quality_group"]["quality_type"] == preferred else 1,
               0 if c["quality_changes"] is None else 1, c["quality_group"]["quality_type"],
               (c["quality_changes"] or {}).get("name") or "")
        scored.append((key, c, result))
    if not scored:
        return _needs_input("every compatible profile conflicts with the validated plan",
                            ["review the rejected overrides", "allow another installed profile"], rejected)
    scored.sort(key=lambda s: s[0])
    best_key, best, best_result = scored[0]
    tie = [candidate_label(c) for k, c, _ in scored[1:] if k[:3] == best_key[:3]]
    return {"status": "selected", "candidate": best, "evaluation": best_result,
            "criterion": "compatibility -> validated plan -> fewest overrides -> preferred_quality_type (%s) -> "
                         "native before custom -> quality_type" % preferred,
            "selected": candidate_label(best), "ties_broken_by_name": tie,
            "alternatives": [{"label": candidate_label(c), "override_count": k[0]} for k, c, _ in scored[1:]],
            "rejected": rejected, "material": {"id": best["materials"][pos]["id"],
                                               "guid": best["materials"][pos].get("guid")}}


def _needs_input(reason, options, rejected):
    return {"status": "needs_input", "reason": reason, "options": options, "rejected": rejected,
            "candidate": None}


# ---------------------------------------------------------------- minimal override

def justification(item, load_class):
    """Why a compiled item may change the base: requirement / explicit / user priority / constraint /
    load class, else policy."""
    if item.get("reason_requirement_ids"):
        return "requirement"
    if item["key"] in EXPLICIT_KEYS:
        return "explicit"
    models = item.get("model_ids") or []
    if any(m.startswith("user_priority:") for m in models):
        return "user_priority"  # speed/quality priority stated by the user
    if any(m in CONSTRAINT_MODELS for m in models):
        return "constraint"
    policy = models and all(m.startswith("policy:") or m in POLICY_MODELS for m in models)
    if not policy:
        return "constraint"     # functional/calibrated/other explicit model: never dropped silently
    if item["key"] in CLASS_JUSTIFIED.get(load_class, ()):
        return "load_class:" + load_class
    return "policy"


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def _value(resolver, key, ext):
    try:
        return resolver.value(key, extruder=ext)
    except EvalError as e:
        return {"error": str(e)}


def minimal_patch(items, base_resolver, load_class):
    """Delta over B: drop equal values and unjustified policy seeds; validate like compile_patch."""
    kept, inherited, errors, warnings = [], [], [], []
    for source in items:
        item = dict(source)
        ext = item["target"].split("_")[1] if item["scope"] == "extruder" else None
        base_value = _value(base_resolver, item["key"], ext)
        why = justification(item, load_class)
        item.update({"base_value": base_value, "requested_value": item["value"], "justification": why})
        if why == "policy":
            inherited.append({"key": item["key"], "base_value": base_value, "policy_value": item["value"],
                              "reason": "generic policy seed; the verified preset value is kept"})
            continue
        if why != "explicit" and _equal(base_value, item["value"]):
            inherited.append({"key": item["key"], "base_value": base_value, "policy_value": item["value"],
                              "reason": "the base already has the planned value"})
            continue
        if why == "explicit" and _equal(base_value, item["value"]):
            item["note"] = (item.get("note") + "; " if item.get("note") else "") + EXPLICIT_KEYS[item["key"]]
        kept.append(item)
    patched = compiler.validate_items(kept, base_resolver, errors, warnings)
    for item in kept:
        ext = item["target"].split("_")[1] if item["scope"] == "extruder" else None
        item["effective_value"] = _value(patched, item["key"], ext)
        item["old_effective_value"] = item["base_value"]
    return {"items": kept, "errors": errors, "warnings": warnings, "ok": not errors, "resolver": patched,
            "inherited": inherited}


def provenance(base_resolver, patch, keys, extruder="0"):
    """Split effective values into inherited, overridden and dependent (computed) changes."""
    overridden = set(i["key"] for i in patch["items"])
    inherited, dependent = [], []
    for key in keys:
        if key in overridden:
            continue
        base_value = _value(base_resolver, key, extruder)
        effective = _value(patch["resolver"], key, extruder)
        if _equal(base_value, effective):
            inherited.append(key)
        else:
            dependent.append({"key": key, "base_value": base_value, "effective_value": effective,
                              "source": patch["resolver"].source(key, extruder)})
    return {"inherited": sorted(inherited), "overridden": sorted(overridden), "dependent": dependent}


def change_records(patch):
    out = []
    for i in patch["items"]:
        out.append({"key": i["key"], "scope": i["scope"],
                    "extruder": i["target"].split("_")[1] if i["scope"] == "extruder" else None,
                    "base_value": i.get("base_value"), "requested_value": i.get("requested_value", i["value"]),
                    "effective_value": i.get("effective_value"), "unit": i.get("unit"),
                    "reason": i.get("note"), "justification": i.get("justification"),
                    "requirement_ids": i.get("reason_requirement_ids") or [], "model_ids": i.get("model_ids") or [],
                    "validation": i.get("validation")})
    return out


def explain(selection, patch):
    """Short Russian explanation without internal container ids or stack indices."""
    if selection.get("status") != "selected":
        return ["Базовый профиль STE не выбран: %s. Варианты: %s." %
                (selection.get("reason"), "; ".join(selection.get("options") or []))]
    lines = ["Базовый профиль STE: %s; материал %s. Остальные настройки наследуются от него." %
             (selection["selected"], selection["material"]["id"])]
    changed = [i for i in patch["items"] if i.get("justification") != "explicit" or
               not _equal(i.get("base_value"), i["value"])]
    if changed:
        lines.append("Изменено поверх профиля (%d): %s." % (len(changed), "; ".join(
            "%s %s → %s (%s)" % (i["key"], i.get("base_value"), i["value"],
                                 i.get("note") or i.get("justification")) for i in changed)))
    else:
        lines.append("Изменения поверх профиля не потребовались.")
    return lines


# ---------------------------------------------------------------- offline catalog

def _read_cfg(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with open(path, encoding="utf-8") as f:
        parser.read_file(f)
    return parser


def _cfg_entry(path, kind):
    cfg = _read_cfg(path)
    meta = dict(cfg.items("metadata")) if cfg.has_section("metadata") else {}
    general = dict(cfg.items("general")) if cfg.has_section("general") else {}
    values = dict(cfg.items("values")) if cfg.has_section("values") else {}
    entry = {"id": os.path.basename(path).replace(".inst.cfg", ""), "type": kind, "name": general.get("name"),
             "definition": general.get("definition"), "values": values}
    for key in ("quality_type", "variant", "material", "buildplate"):
        if key in meta:
            entry[key] = meta[key]
    if "setting_version" in meta:
        entry["setting_version"] = int(meta["setting_version"])
    if "weight" in meta:
        entry["weight"] = int(meta["weight"])
    entry["global_quality"] = meta.get("global_quality", "False").strip().lower() in ("true", "1", "yes")
    return entry


def _fdm_material(path):
    ns = {"m": "http://www.ultimaker.com/material"}
    root = ET.parse(path).getroot()
    meta = root.find("m:metadata", ns)

    def text(parent, tag):
        node = parent.find(tag, ns) if parent is not None else None
        return node.text.strip() if node is not None and node.text else None

    name = meta.find("m:name", ns) if meta is not None else None
    diameter = text(root.find("m:properties", ns), "m:diameter")
    values = {}
    settings = root.find("m:settings", ns)
    for node in (settings.findall("m:setting", ns) if settings is not None else []):
        key = MATERIAL_SETTING_MAP.get(node.get("key"))
        if key and node.text:
            values[key] = node.text.strip()
    file_id = os.path.basename(path).replace(".xml.fdm_material", "").replace(".fdm_material", "")
    approx = None
    if diameter:
        approx = str(int(round(float(diameter))))
        values["material_diameter"] = diameter
    root_id = file_id
    for suffix in ("_175", "_285"):
        if root_id.endswith(suffix):
            root_id = root_id[:-len(suffix)]
    return {"id": file_id, "type": "material", "base_file": file_id, "root_material_id": root_id,
            "fallback_root_ids": [], "guid": text(meta, "m:GUID"), "brand": text(name, "m:brand"),
            "material_type": text(name, "m:material"), "name": text(name, "m:color") or file_id,
            "approximate_diameter": approx, "values": values}


def catalog_from_resources(resources_dir, machine_id, schema, nozzle_by_extruder=None, current_materials=None,
                           enabled_extruders=None, ste_version=None):
    """Offline catalog from a resources folder (bundled profiles only, no user containers).

    A planning preview for CLI/tests. In STE the catalog comes from the live
    registry, which also contains installed/user profiles and machine-specific
    material containers that this reader does not reproduce.
    """
    meta = schema.metadata
    has_mq = str(meta.get("has_machine_quality", False)).lower() in ("true", "1")
    quality_definition = (meta.get("quality_definition") or machine_id) if has_mq else DEFAULT_QUALITY_DEFINITION
    qualities = []
    quality_dir = os.path.join(resources_dir, "quality")
    for folder, _, files in sorted(os.walk(quality_dir)):
        for name in sorted(files):
            if name.endswith(".inst.cfg"):
                entry = _cfg_entry(os.path.join(folder, name), "quality")
                if entry.get("quality_type") and entry.get("definition") in (quality_definition, DEFAULT_QUALITY_DEFINITION):
                    qualities.append(_with_digest(entry))
    variants = {}
    variant_dir = os.path.join(resources_dir, "variants")
    for name in sorted(os.listdir(variant_dir)) if os.path.isdir(variant_dir) else []:
        if name.endswith(".inst.cfg"):
            entry = _cfg_entry(os.path.join(variant_dir, name), "variant")
            if entry.get("definition") == machine_id:
                variants[entry["name"]] = _with_digest(entry)
    materials = []
    material_dir = os.path.join(resources_dir, "materials")
    for name in sorted(os.listdir(material_dir)) if os.path.isdir(material_dir) else []:
        if name.endswith(".fdm_material"):
            try:
                materials.append(_with_digest(_fdm_material(os.path.join(material_dir, name))))
            except (ET.ParseError, ValueError):
                continue
    base = SettingResolver(schema)
    extruders = {}
    for pos in schema.extruder_positions():
        variant = variants.get((nozzle_by_extruder or {}).get(pos)) if nozzle_by_extruder else None
        nozzle = base.value("machine_nozzle_size", extruder=pos)
        if variant and "machine_nozzle_size" in variant["values"]:
            nozzle = float(variant["values"]["machine_nozzle_size"])
        current = (current_materials or {}).get(pos)
        extruders[pos] = {"definition_id": (meta.get("machine_extruder_trains") or {}).get(pos),
                          "enabled": pos in (enabled_extruders or ["0"]), "variant": variant,
                          "nozzle_size": nozzle, "material_diameter": base.value("material_diameter", extruder=pos),
                          "available_materials": materials,
                          "current_material": next((m for m in materials if m["id"] == current), None)}
    return {"schema": CATALOG_SCHEMA, "source": "resources_offline", "ste_version": ste_version,
            "setting_version": None,
            "machine": {"definition_id": machine_id, "quality_definition_id": quality_definition,
                        "has_machine_quality": has_mq, "has_variants": bool(meta.get("has_variants")),
                        "preferred_quality_type": meta.get("preferred_quality_type"), "buildplate_name": None,
                        "variant": None, "definition_changes": None},
            "extruders": extruders, "qualities": qualities, "quality_changes": []}
