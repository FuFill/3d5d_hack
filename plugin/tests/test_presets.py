# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Native preset base: container stack order, quality groups, deterministic selection, minimal Delta.

The tiny machine below is a synthetic program fixture for isolated contracts
(stack order, resolve, availability, provenance). It proves nothing about
print quality; the integration tests at the end use the real STE resources.
"""

import copy
import json
import os
import tempfile

from testutil import raises, skip, resources_dir
from test_qwen_integration import box_model

from ste_planner import presets as P, compiler as C, calibration, planner
from ste_planner import runtime_adapter as A
from ste_planner.settings_schema import SettingResolver, load_machine_schema

FDMPRINTER = {
    "name": "FDM", "metadata": {"preferred_quality_type": "normal"},
    "settings": {
        "machine_settings": {"type": "category", "children": {
            "machine_extruder_count": {"type": "int", "default_value": 2, "settable_per_extruder": False},
            "machine_nozzle_size": {"type": "float", "default_value": 0.4},
            "material_diameter": {"type": "float", "default_value": 1.75}}},
        "resolution": {"type": "category", "children": {
            "layer_height": {"type": "float", "default_value": 0.1, "minimum_value": "0.01",
                             "maximum_value": "machine_nozzle_size", "settable_per_extruder": False},
            "line_width": {"type": "float", "default_value": 0.4, "value": "machine_nozzle_size"},
            "wall_line_count": {"type": "int", "default_value": 2},
            "wall_thickness": {"type": "float", "default_value": 0.8, "value": "wall_line_count * line_width"},
            "top_layers": {"type": "int", "default_value": 4, "value": "math.ceil(0.8 / layer_height)"},
            "infill_sparse_density": {"type": "float", "default_value": 20, "minimum_value": "0",
                                      "maximum_value": "100"},
            "infill_pattern": {"type": "enum", "options": {"grid": "Grid", "lines": "Lines"},
                               "default_value": "grid"}}},
        "material": {"type": "category", "children": {
            "default_material_print_temperature": {"type": "float", "default_value": 210},
            "material_print_temperature": {"type": "float", "default_value": 210,
                                           "value": "default_material_print_temperature"},
            "material_bed_temperature": {"type": "float", "default_value": 60,
                                         "resolve": "max(extruderValues('material_bed_temperature'))"},
            "speed_print": {"type": "float", "default_value": 50}}},
        "mode": {"type": "category", "children": {
            "printing_mode": {"type": "enum", "default_value": "classic", "settable_per_extruder": False,
                              "options": {"classic": "3D", "cylindrical": "5D Spiral",
                                          "cylindrical_full": "5D"}}}}}}

_TINY = {}


def tiny_resources():
    """Resources folder of a two-extruder machine 'tiny' with machine-specific qualities."""
    if "dir" in _TINY:
        return _TINY["dir"]
    root = tempfile.mkdtemp(prefix="ste_presets_")
    for sub in ("definitions", "extruders", "quality", "variants", "materials"):
        os.mkdir(os.path.join(root, sub))

    def write(path, data):
        with open(os.path.join(root, path), "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    write("definitions/fdmprinter.def.json", FDMPRINTER)
    write("definitions/fdmextruder.def.json", {"name": "E", "settings": {"machine_settings": {
        "type": "category", "children": {"extruder_nr": {"type": "int", "default_value": 0}}}}})
    write("definitions/tiny.def.json", {"name": "Tiny", "inherits": "fdmprinter", "metadata": {
        "has_machine_quality": True, "has_variants": True, "preferred_quality_type": "normal",
        "machine_extruder_trains": {"0": "tiny_e0", "1": "tiny_e1"}}})
    for pos in ("0", "1"):
        write("extruders/tiny_e%s.def.json" % pos, {"name": "E" + pos, "inherits": "fdmextruder",
                                                   "overrides": {"extruder_nr": {"default_value": int(pos)}}})
    write("quality/tiny_global_normal.inst.cfg", "[general]\nversion = 4\nname = Normal\ndefinition = tiny\n\n"
          "[metadata]\nsetting_version = 5\ntype = quality\nquality_type = normal\nglobal_quality = True\n\n"
          "[values]\nlayer_height = 0.2\n")
    write("quality/tiny_04_pla_normal.inst.cfg", "[general]\nversion = 4\nname = Normal\ndefinition = tiny\n\n"
          "[metadata]\nsetting_version = 5\ntype = quality\nquality_type = normal\nvariant = 0.4 mm\n"
          "material = generic_pla\n\n[values]\nspeed_print = 45\nwall_line_count = =2 + 1\n")
    write("variants/tiny_04.inst.cfg", "[general]\nversion = 4\nname = 0.4 mm\ndefinition = tiny\n\n"
          "[metadata]\nsetting_version = 5\ntype = variant\nhardware_type = nozzle\n\n"
          "[values]\nmachine_nozzle_size = 0.4\n")
    write("materials/generic_pla_175.xml.fdm_material",
          '<?xml version="1.0"?><fdmmaterial xmlns="http://www.ultimaker.com/material" version="1.3">'
          '<metadata><name><brand>Generic</brand><material>PLA</material><color>Generic</color></name>'
          '<GUID>guid-pla</GUID></metadata><properties><diameter>1.75</diameter></properties>'
          '<settings><setting key="print temperature">205</setting>'
          '<setting key="heated bed temperature">60</setting></settings></fdmmaterial>')
    _TINY["dir"] = root
    return root


def schema():
    return load_machine_schema(tiny_resources(), "tiny")


def quality(qid, qt, variant=None, material=None, glob=False, values=None, setting_version=5):
    entry = {"id": qid, "type": "quality", "name": qt.title(), "definition": "tiny", "quality_type": qt,
             "global_quality": glob, "values": values or {}, "setting_version": setting_version}
    if variant:
        entry["variant"] = variant
    if material:
        entry["material"] = material
    return P._with_digest(entry)


def material(mid, base, root, guid, values=None, diameter="2"):
    return P._with_digest({"id": mid, "type": "material", "name": mid, "base_file": base,
                           "root_material_id": root, "fallback_root_ids": [], "guid": guid,
                           "approximate_diameter": diameter, "values": values or {}})


PLA = material("generic_pla_175", "generic_pla_175", "generic_pla", "guid-pla",
               {"default_material_print_temperature": "205", "material_bed_temperature": "60"})
ABS = material("generic_abs_175", "generic_abs_175", "generic_abs", "guid-abs",
               {"material_bed_temperature": "100"})


def catalog(variant_name="0.4 mm", qualities=None, enabled1=False, materials0=None, current1=None,
            quality_changes=None, setting_version=5):
    variant = P._with_digest({"id": "tiny_" + variant_name.replace(" ", ""), "type": "variant",
                              "name": variant_name, "definition": "tiny",
                              "values": {"machine_nozzle_size": float(variant_name.split()[0])}})
    if qualities is None:
        qualities = [quality("tiny_global_normal", "normal", glob=True, values={"layer_height": "0.2"}),
                     quality("tiny_global_fine", "fine", glob=True, values={"layer_height": "0.1"}),
                     quality("tiny_04_pla_normal", "normal", "0.4 mm", "generic_pla",
                             values={"speed_print": "45", "wall_line_count": "3"}),
                     quality("tiny_04_pla_fine", "fine", "0.4 mm", "generic_pla",
                             values={"speed_print": "35", "infill_sparse_density": "=15 if layer_height < 0.15 else 20"})]
    ext = {"definition_id": "tiny_e0", "enabled": True, "variant": variant,
           "nozzle_size": float(variant_name.split()[0]), "material_diameter": 1.75,
           "available_materials": list(materials0 or [PLA, ABS]), "current_material": PLA}
    ext1 = dict(ext, definition_id="tiny_e1", enabled=enabled1, current_material=current1)
    return {"schema": P.CATALOG_SCHEMA, "source": "test", "ste_version": "test", "setting_version": setting_version,
            "machine": {"definition_id": "tiny", "quality_definition_id": "tiny", "has_machine_quality": True,
                        "preferred_quality_type": "normal", "variant": None, "definition_changes": None},
            "extruders": {"0": ext, "1": ext1}, "qualities": qualities, "quality_changes": quality_changes or []}


def context(**kw):
    out = {"machine_id": "tiny", "extruder": "0", "material_file_id": "generic_pla_175", "material_guid": "guid-pla",
           "printing_mode": "classic", "required_nozzle_mm": None, "allowed_quality_changes": []}
    out.update(kw)
    return out


def count_by_type(counts):
    return lambda c: {"delta_count": counts.get(c["quality_group"]["quality_type"], 0), "errors": []}


# ---------------------------------------------------------------- resolver

def test_layer_order_follows_ste_container_indexes():
    sch = schema()
    ext0 = [("quality_changes", {"speed_print": 30}), ("quality", {"speed_print": "45", "wall_line_count": "3"}),
            ("material", {"speed_print": 25, "default_material_print_temperature": "215"}),
            ("variant", {"machine_nozzle_size": "0.6"}), ("definition_changes", {})]
    r = SettingResolver(sch, global_layers=[("quality", {"layer_height": "0.2"})], extruder_layers={"0": ext0})
    assert r.value("speed_print", extruder="0") == 30.0          # quality_changes > quality > material
    assert r.source("speed_print", extruder="0") == "quality_changes_extruder_0"
    assert r.value("layer_height") == 0.2 and isinstance(r.value("layer_height"), float)
    assert r.value("line_width", extruder="0") == 0.6            # formula sees the nozzle variant
    assert abs(r.value("wall_thickness", extruder="0") - 1.8) < 1e-12   # computed, never written
    assert r.value("material_print_temperature", extruder="0") == 215.0
    without_qc = SettingResolver(sch, extruder_layers={"0": ext0[1:]})
    assert without_qc.value("speed_print", extruder="0") == 45.0
    user = r.copy_with(user_extruders={"0": {"speed_print": 20.0}})
    assert user.value("speed_print", extruder="0") == 20.0
    assert user.without_user().value("speed_print", extruder="0") == 30.0
    assert user.global_layers == r.global_layers


def test_global_instance_value_disables_resolve_like_uranium_state():
    sch = schema()
    layers = {"0": [("material", {"material_bed_temperature": "70"})],
              "1": [("material", {"material_bed_temperature": "50"})]}
    r = SettingResolver(sch, extruder_layers=layers)
    assert r.value("material_bed_temperature") == 70.0           # resolve: max over extruders
    pinned = SettingResolver(sch, global_layers=[("quality", {"material_bed_temperature": "55"})],
                             extruder_layers=layers)
    assert pinned.value("material_bed_temperature") == 55.0      # InstanceState.User -> no resolve
    assert pinned.value("material_bed_temperature", extruder="0") == 70.0


def test_legacy_material_settings_keep_previous_semantics():
    sch = schema()
    r = SettingResolver(sch, {"printing_mode": "classic"}, material_settings={"default_material_print_temperature": 220.0})
    assert r.value("material_print_temperature", extruder="0") == 220.0
    assert r.source("default_material_print_temperature", extruder="0") == "material"
    assert r.copy_with({"layer_height": 0.3}).value("material_print_temperature", extruder="0") == 220.0


# ---------------------------------------------------------------- quality groups and selection

def test_quality_groups_follow_installed_nozzle_and_material():
    groups = P.quality_groups(catalog(), {"0": PLA, "1": None})
    assert sorted(groups) == ["fine", "normal"]
    assert groups["normal"]["available"] and groups["normal"]["extruders"]["0"]["id"] == "tiny_04_pla_normal"
    assert groups["normal"]["global"]["id"] == "tiny_global_normal"
    other_nozzle = P.quality_groups(catalog("0.6 mm"), {"0": PLA, "1": None})
    assert not any(g["available"] for g in other_nozzle.values())
    result = P.select_base(catalog("0.6 mm"), context(), count_by_type({}))
    assert result["status"] == "needs_input" and "no compatible native quality" in result["reason"]
    assert all(r["reason"].startswith("not available") for r in result["rejected"])


def test_selection_is_deterministic_fewest_overrides_then_preferred_then_name():
    result = P.select_base(catalog(), context(), count_by_type({"normal": 3, "fine": 1}))
    assert result["status"] == "selected" and result["candidate"]["quality_group"]["quality_type"] == "fine"
    assert result["alternatives"] == [{"label": "Normal (normal)", "override_count": 3}]
    tie = P.select_base(catalog(), context(), count_by_type({"normal": 2, "fine": 2}))
    assert tie["candidate"]["quality_group"]["quality_type"] == "normal"   # machine preferred_quality_type
    again = P.select_base(copy.deepcopy(catalog()), context(), count_by_type({"normal": 2, "fine": 2}))
    assert again["selected"] == tie["selected"]
    failing = P.select_base(catalog(), context(), lambda c: {"delta_count": 0, "errors": [{"key": "x"}]})
    assert failing["status"] == "needs_input"


def test_material_identity_by_id_and_guid_never_by_name():
    lookalike = material("pla_by_name", "other_pla", "generic_pla", "guid-pla")
    lookalike["name"] = "generic_pla_175"
    lookalike = P._with_digest(dict((k, v) for k, v in lookalike.items() if k != "digest"))
    result = P.select_base(catalog(materials0=[lookalike, ABS]), context(), count_by_type({}))
    assert result["status"] == "needs_input" and "not installed" in result["reason"]
    wrong_guid = P.select_base(catalog(), context(material_guid="guid-other"), count_by_type({}))
    assert wrong_guid["status"] == "needs_input"
    assert any("GUID" in r["reason"] for r in wrong_guid["rejected"])
    duplicate = material("generic_pla_175_copy", "generic_pla_175", "generic_pla", "guid-pla")
    ambiguous = P.select_base(catalog(materials0=[PLA, duplicate]), context(), count_by_type({}))
    assert ambiguous["status"] == "needs_input" and "ambiguous" in ambiguous["reason"]


def test_hardware_context_is_not_changed_silently():
    result = P.select_base(catalog(), context(required_nozzle_mm=0.6), count_by_type({}))
    assert result["status"] == "needs_input" and "hardware change required" in result["reason"]
    other_machine = P.select_base(catalog(), context(machine_id="hte530v5.2.4"), count_by_type({}))
    assert other_machine["status"] == "needs_input" and "another machine" in other_machine["reason"]
    wide = material("generic_pla_285", "generic_pla_285", "generic_pla", None, diameter="3")
    diameter = P.select_base(catalog(materials0=[wide]), context(material_file_id="generic_pla_285",
                                                                 material_guid=None), count_by_type({}))
    assert diameter["status"] == "needs_input" and "diameter" in diameter["reason"]


def test_every_enabled_extruder_needs_a_quality_node():
    assert P.select_base(catalog(enabled1=True, current1=ABS), context(), count_by_type({}))["status"] == "needs_input"
    assert P.select_base(catalog(enabled1=True, current1=PLA), context(), count_by_type({}))["status"] == "selected"
    assert P.select_base(catalog(enabled1=False, current1=ABS), context(), count_by_type({}))["status"] == "selected"


def test_custom_profile_only_when_explicitly_allowed_and_compatible():
    qc_global = P._with_digest({"id": "my_qc_global", "type": "quality_changes", "name": "Mine",
                                "definition": "tiny", "quality_type": "normal", "values": {"layer_height": "0.15"}})
    qc_ext = P._with_digest({"id": "my_qc_e0", "type": "quality_changes", "name": "Mine", "definition": "tiny",
                             "quality_type": "normal", "position": "0", "values": {"speed_print": "41"}})
    group = {"name": "Mine", "quality_type": "normal", "definition": "tiny", "origin": "user",
             "global": qc_global, "extruders": {"0": qc_ext}}
    cat = catalog(quality_changes=[group])
    counts = lambda c: {"delta_count": 0 if c["quality_changes"] else 2, "errors": []}
    assert P.select_base(cat, context(), counts)["candidate"]["quality_changes"] is None
    chosen = P.select_base(cat, context(allowed_quality_changes=["Mine"]), counts)
    assert chosen["selected"] == "Mine / Normal (normal)"
    r = P.resolver_for(schema(), cat, chosen["candidate"])
    assert r.value("layer_height") == 0.15 and r.value("speed_print", extruder="0") == 41.0
    missing = P.select_base(cat, context(allowed_quality_changes=["Absent"]), counts)
    assert any(x.get("quality_changes") == "Absent" for x in missing["rejected"])


def test_profile_fixing_another_mode_or_setting_version_is_rejected():
    qs = catalog()["qualities"]
    qs[2] = quality("tiny_04_pla_normal", "normal", "0.4 mm", "generic_pla", values={"printing_mode": "cylindrical"})
    result = P.select_base(catalog(qualities=qs), context(), count_by_type({}))
    assert result["candidate"]["quality_group"]["quality_type"] == "fine"
    assert any("printing_mode" in r["reason"] for r in result["rejected"])
    stale = P.select_base(catalog(setting_version=6), context(), count_by_type({}))
    assert stale["status"] == "needs_input"


# ---------------------------------------------------------------- minimal Delta and provenance

def _decisions(load_class_reason=None):
    D = C.Decision
    return [D("printing_mode", "classic", model_ids=["choose_route"]),
            D("wall_count", 3, model_ids=["wall_count", "policy:shell"]),
            D("infill_fraction", 0.35, model_ids=["policy:infill"]),
            D("layer_height", 0.1, reason_requirement_ids=["R1"], model_ids=["choose_layer_height"]),
            D("bed_temperature", 60.0, model_ids=["thermal_window"]),
            D("speed_print", 45.0, model_ids=["role_speed", "material_datasheet"])]


def _base():
    cat = catalog()
    selected = P.select_base(cat, context(), count_by_type({"fine": 9}))
    return cat, selected, P.resolver_for(schema(), cat, selected["candidate"])


def test_minimal_patch_inherits_unchanged_fields_and_keeps_justified_ones():
    cat, selected, base = _base()
    assert selected["selected"] == "Normal (normal)"
    full = C.compile_patch(_decisions(), base)
    assert not full["errors"]
    patch = P.minimal_patch(full["items"], base, "general")
    keys = dict((i["key"], i) for i in patch["items"])
    assert sorted(keys) == ["layer_height", "material_bed_temperature", "printing_mode"]
    assert keys["layer_height"]["base_value"] == 0.2 and keys["layer_height"]["effective_value"] == 0.1
    assert keys["printing_mode"]["justification"] == "explicit"   # kept although equal to the base
    inherited = dict((i["key"], i) for i in patch["inherited"])
    assert inherited["infill_sparse_density"]["reason"].startswith("generic policy")
    assert inherited["wall_line_count"]["base_value"] == 3 and inherited["speed_print"]["base_value"] == 45.0
    assert "wall_thickness" not in keys
    prov = P.provenance(base, patch, ["wall_line_count", "wall_thickness", "top_layers", "infill_sparse_density"])
    assert "wall_line_count" in prov["inherited"] and "infill_sparse_density" in prov["inherited"]
    assert [d["key"] for d in prov["dependent"]] == ["top_layers"]   # ceil(0.8 / h) follows the new h
    payload = C.patch_to_slicer_payload(patch)
    assert payload == {"global": {"layer_height": 0.1, "printing_mode": "classic"},
                       "extruders": {"0": {"material_bed_temperature": 60.0}}}
    records = P.change_records(patch)
    assert all(set(("key", "scope", "extruder", "base_value", "requested_value", "effective_value", "unit",
                    "reason", "validation")) <= set(r) for r in records)
    text = " ".join(P.explain(selected, patch))
    assert "Normal (normal)" in text and "tiny_04_pla" not in text


def test_load_class_justifies_policy_seeds_and_bounds_still_validate():
    _, _, base = _base()
    full = C.compile_patch(_decisions(), base)
    structural = P.minimal_patch(full["items"], base, "structural")
    item = next(i for i in structural["items"] if i["key"] == "infill_sparse_density")
    assert item["justification"] == "load_class:structural" and item["effective_value"] == 35.0
    bad = C.compile_patch([C.Decision("layer_height", 0.9, reason_requirement_ids=["R1"])], base)
    assert bad["errors"]                                            # above machine_nozzle_size


def test_catalog_tampering_and_changed_containers_are_detected():
    cat = catalog()
    cat["qualities"][0]["values"]["layer_height"] = "0.3"          # content changed, digest not
    assert any("digest" in e for e in P.validate_catalog(cat))
    _, selected, _ = _base()
    record = P.base_presets_record(catalog(), selected["candidate"])
    changed = catalog()
    changed["qualities"][2] = quality("tiny_04_pla_normal", "normal", "0.4 mm", "generic_pla",
                                      values={"speed_print": "44", "wall_line_count": "3"})
    other = P.select_base(changed, context(), count_by_type({"fine": 9}))
    record2 = P.base_presets_record(changed, other["candidate"])
    assert record["catalog_sha256"] != record2["catalog_sha256"]
    assert not P.same_base(P.base_identity(record), P.base_identity(record2))
    assert P.same_base(P.base_identity(record), P.base_identity(copy.deepcopy(record)))
    assert P.stack_ids(record, "0") == [None, "tiny_04_pla_normal", "generic_pla_175", "tiny_0.4mm", None]


def test_offline_catalog_reads_bundled_resource_files():
    sch = schema()
    cat = P.catalog_from_resources(tiny_resources(), "tiny", sch, nozzle_by_extruder={"0": "0.4 mm"},
                                   current_materials={"0": "generic_pla_175"})
    assert P.validate_catalog(cat) == []
    assert sorted(q["id"] for q in cat["qualities"]) == ["tiny_04_pla_normal", "tiny_global_normal"]
    result = P.select_base(cat, context(), count_by_type({}))
    assert result["selected"] == "Normal (normal)"
    r = P.resolver_for(sch, cat, result["candidate"])
    assert r.value("wall_line_count", extruder="0") == 3            # "=2 + 1" is a setting function
    assert r.value("material_print_temperature", extruder="0") == 205.0
    assert r.value("material_bed_temperature") == 60.0


# ---------------------------------------------------------------- calibration and runtime contracts

def test_dml_requires_the_same_native_base():
    _, selected, base = _base()
    identity = P.base_identity(P.base_presets_record(catalog(), selected["candidate"]))
    ctx = {"machine_id": "tiny", "material_id": "pla", "printing_mode": "classic", "geometry_sha256": "a",
           "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "preset_base": identity}
    data = {"dataset_id": "d", "source": "physical_test", "design": "randomised",
            "context": dict((k, v) for k, v in ctx.items() if k != "preset_base")}
    assert calibration._assess_one(data, ctx, base, None).status == "needs_input"
    other = dict(identity, container_digests=["global/quality:other"])
    data["context"]["preset_base"] = other
    assert calibration._assess_one(data, ctx, base, None).status == "out_of_domain"


def test_runtime_refuses_missing_or_foreign_base_before_any_write():
    with box_model() as path:
        decision = {"route": {"mode": "classic"}, "chosen_material": {"id": "m"}, "setting_patch": {},
                    "calibration_context": {"machine_id": "tiny", "printing_mode": "classic", "material_id": "m",
                                            "geometry_sha256": A.file_sha256(path)},
                    "base_presets": None, "preset_selection": {"status": "needs_input"}}
        raises(A.ApplyError, A.preflight, decision, path, "tiny")
        _, selected, _ = _base()
        record = P.base_presets_record(catalog(), selected["candidate"])
        decision.update(base_presets=dict(record, contract="other/0"), preset_selection={"status": "selected"})
        raises(A.ApplyError, A.preflight, decision, path, "tiny")
        decision["base_presets"] = record
        raises(A.ApplyError, A.preflight, decision, path, "tiny")      # calibration context lacks the base


class _Container(object):
    def __init__(self, cid):
        self._id = cid

    def getId(self):
        return self._id


class _Stack(object):
    def __init__(self, ids, extruders=None):
        self._containers = [_Container(i) for i in ids]
        self.extruders = extruders or {}

    def getContainers(self):
        return self._containers


def test_readback_compares_actual_stacks_with_the_recorded_base():
    _, selected, _ = _base()
    record = P.base_presets_record(catalog(), selected["candidate"])
    def stack_for(position):
        ids = P.stack_ids(record, position)
        return ["user"] + [i or A.EMPTY_IDS[n] for n, i in enumerate(ids)] + ["definition"]
    extruders = {"0": _Stack(stack_for("0")), "1": _Stack(stack_for("1"))}
    assert A.check_native_stacks(_Stack(stack_for(None), extruders), record) == []
    swapped = stack_for("0")
    swapped[2] = "ste_planner_quality_x"
    extruders["0"] = _Stack(swapped)
    differences = A.check_native_stacks(_Stack(stack_for(None), extruders), record)
    assert differences == [{"stack": "extruder_0", "layer": "quality", "expected": "tiny_04_pla_normal",
                            "actual": "ste_planner_quality_x"}]


# ---------------------------------------------------------------- integration with STE resources

def _ste_machine():
    """The delivered machine when its resources are present; the public STE-Slicer one otherwise."""
    res = resources_dir()
    for machine in (planner.DEFAULT_MACHINE, "hte530v5.2"):
        if os.path.isfile(os.path.join(res, "definitions", machine + ".def.json")):
            return res, machine
    skip("no Stereotech Hybrid 530 definition in the STE resources")


def test_planner_inherits_native_base_and_writes_minimal_patch():
    res, machine = _ste_machine()
    sch = load_machine_schema(res, machine)
    cat = P.catalog_from_resources(res, machine, sch)
    with box_model() as path:
        legacy = planner.plan_case("Тестовая деталь", path, resources_dir=res, machine_id=machine,
                                   forced_mode="classic")
        decision = planner.plan_case("Тестовая деталь", path, resources_dir=res, machine_id=machine,
                                     forced_mode="classic", preset_catalog=cat)
        again = planner.plan_case("Тестовая деталь", path, resources_dir=res, machine_id=machine,
                                  forced_mode="classic", preset_catalog=copy.deepcopy(cat))
    assert legacy["base_presets"] is None and legacy["preset_selection"]["status"] == "not_requested"
    assert decision["preset_selection"]["status"] == "selected"
    assert planner.decision_json(decision) == planner.decision_json(again)
    base = decision["baseline_effective_settings"]
    keys = set(i["key"] for i in decision["setting_patch"]["items"])
    assert len(keys) <= len(legacy["setting_patch"]["items"])
    for key in decision["setting_provenance"]["inherited"]:
        assert decision["effective_settings"].get(key, base.get(key)) == base.get(key)
    for change in decision["setting_patch"]["changes"]:
        assert change["justification"] in ("requirement", "explicit", "constraint") or \
            change["justification"].startswith("load_class:")
    assert decision["calibration_context"]["preset_base"]["container_digests"]
    assert decision["status"] == legacy["status"]


def test_organizer_gear_stays_infeasible_with_native_base():
    from testutil import example_cases
    res, machine = _ste_machine()
    cases = [c for c in example_cases() if c[0].startswith("1.") and os.path.isfile(c[1])]
    if not cases:
        skip("organizer gear example not available")
    _, docx, stl = cases[0]
    sch = load_machine_schema(res, machine)
    decision = planner.plan_case_files(docx, stl, resources_dir=res, machine_id=machine,
                                       preset_catalog=P.catalog_from_resources(res, machine, sch))
    assert decision["status"] == "infeasible"
