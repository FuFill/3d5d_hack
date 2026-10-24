# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Cross-check of the native preset layers (presets.resolver_for) against the real Uranium.

Reference stacks use Uranium's ContainerStack with the GlobalStack/ExtruderStack rules of
tools/uranium_crosscheck.py and real InstanceContainer objects: bundled quality profiles
are deserialized from their .inst.cfg files; material, nozzle variant, definition_changes,
quality_changes and user containers are filled through InstanceContainer.setProperty, so
SettingInstance type conversion and SettingFunction ("=expr") apply as in STE. Every
setting of the global stack and extruder 0 is compared for each available quality group,
material and printing mode, with and without synthetic variant/quality_changes/
definition_changes layers and a planner-like user Delta.

    STE_RESOURCES_DIR=<resources> PYTHONPATH=plugin;plugin/tools;<Uranium> \
        python plugin/tools/uranium_preset_crosscheck.py report.json [machine_id]

The machine defaults to hte530v5.2.4. Material containers here are built from the
global settings of the .fdm_material files (as the planner's offline catalog); STE's
machine-specific material sections are not reproduced.
"""

import json
import os
import sys

import uranium_crosscheck as X  # Uranium setup and STE stack rules  # noqa: E402

from UM.Settings.InstanceContainer import InstanceContainer, setContainerRegistry  # noqa: E402

from ste_planner import presets as P, settings_schema as S  # noqa: E402

OUT = sys.argv[1]
MACHINE = sys.argv[2] if len(sys.argv) > 2 else "hte530v5.2.4"
MATERIALS = ("enduse_petg", "fiberpart_pp_g30", "sealant_tpu_A95")
EXTRA = {"variant": {"machine_nozzle_size": "0.6"},
         "definition_changes": {"machine_width": "300"},
         "quality_changes_global": {"layer_height": "=0.2 if machine_nozzle_size < 0.5 else 0.3"},
         "quality_changes_extruder": {"speed_print": "37", "wall_line_count": "=3"}}


class Registry(object):
    """Only what InstanceContainer needs: the definition used for new setting instances."""

    def __init__(self, definition):
        self.definition = definition

    def findDefinitionContainers(self, **kwargs):
        return [self.definition]

    def isReadOnly(self, container_id):
        return False


def filled(cid, values):
    container = InstanceContainer(cid)
    container.setDefinition(MACHINE)
    for key, value in values.items():
        container.setProperty(key, "value", value)
    return container


def quality_file(entry):
    for folder, _, files in os.walk(os.path.join(X.RES, "quality")):
        if entry["id"] + ".inst.cfg" in files:
            path = os.path.join(folder, entry["id"] + ".inst.cfg")
            container = InstanceContainer(entry["id"])
            with open(path, encoding="utf-8") as f:
                container.deserialize(f.read(), path)
            return container
    raise IOError("quality file not found: " + entry["id"])


def reference(group, material, mode_user, extra):
    g = X.build_reference(MACHINE, {}, {}, {})      # registers STE formula functions for this g
    g.removeContainer(0)                            # drop the empty user stand-in
    layers = []
    if extra:
        layers.append(filled("dc_global", EXTRA["definition_changes"]))
    layers.append(quality_file(group["global"]))
    if extra:
        layers.append(filled("qc_global", EXTRA["quality_changes_global"]))
    layers.append(filled("user_global", mode_user["global"]))
    for container in layers:                        # bottom -> top
        g.addContainer(container)
    for pos, e in sorted(g.extruders.items()):
        e.removeContainer(0)
        e.removeContainer(0)
        stack = []
        if extra and pos == "0":
            stack.append(filled("variant0", EXTRA["variant"]))
        if pos == "0":
            stack.append(filled("material0", material["values"]))
        if pos in group["extruders"]:
            stack.append(quality_file(group["extruders"][pos]))
        if extra and pos == "0":
            stack.append(filled("qc0", EXTRA["quality_changes_extruder"]))
        stack.append(filled("user%s" % pos, mode_user["extruders"].get(pos, {})))
        for container in stack:
            e.addContainer(container)
    return g


def planner_side(schema, catalog, group, material, mode_user, extra):
    catalog = json.loads(json.dumps(catalog))
    qc = None
    if extra:
        ext = catalog["extruders"]["0"]
        ext["variant"] = P._with_digest({"id": "variant0", "type": "variant", "name": "0.6 mm",
                                         "values": EXTRA["variant"]})
        catalog["machine"]["definition_changes"] = P._with_digest({"id": "dc_global", "type": "definition_changes",
                                                                   "values": EXTRA["definition_changes"]})
        qc = {"name": "check", "quality_type": group["quality_type"],
              "global": P._with_digest({"id": "qc_global", "values": EXTRA["quality_changes_global"]}),
              "extruders": {"0": P._with_digest({"id": "qc0", "values": EXTRA["quality_changes_extruder"]})}}
    materials = {"0": material, "1": None}
    candidate = {"quality_group": group, "quality_changes": qc, "materials": materials}
    base = P.resolver_for(schema, catalog, candidate)
    return base.copy_with(mode_user["global"], mode_user["extruders"])


def main():
    schema = S.load_machine_schema(X.RES, MACHINE)
    setContainerRegistry(Registry(X.load_definition(MACHINE, "definitions")))
    catalog = P.catalog_from_resources(X.RES, MACHINE, schema)
    by_id = dict((m["id"], m) for m in catalog["extruders"]["0"]["available_materials"])
    report = {"machine": MACHINE, "scenarios": [], "total_compared": 0, "total_mismatch": 0, "reference_errors": 0}
    for mid in MATERIALS:
        material = by_id[mid]
        groups = P.quality_groups(catalog, {"0": material, "1": None})
        for quality_type, group in sorted(groups.items()):
            if not group["available"]:
                continue
            for mode in ("classic", "cylindrical", "cylindrical_full"):
                for extra in (False, True):
                    user = {"global": {"printing_mode": mode, "infill_sparse_density": 100.0},
                            "extruders": {"0": {"wall_line_count": 4} if not extra else {}}}
                    if mode != "classic":
                        user["global"].update({"cylindrical_mode_base_diameter": 12.0,
                                               "non_printing_base_diameter": 8.0})
                    ref = reference(group, material, user, extra)
                    mine = planner_side(schema, catalog, group, material, user, extra)
                    mismatches, compared, errors = [], 0, 0
                    for key, d in sorted(schema.definitions.items()):
                        if d.type in (None, "category"):
                            continue
                        try:
                            if d.per_extruder:
                                rv, mv = ref.extruders["0"].getProperty(key, "value"), mine.value(key, extruder="0")
                            else:
                                rv, mv = ref.getProperty(key, "value"), mine.value(key)
                        except Exception as e:
                            errors += 1
                            mismatches.append({"key": key, "error": "%s: %s" % (type(e).__name__, e)})
                            continue
                        compared += 1
                        if not X.same(rv, mv):
                            mismatches.append({"key": key, "uranium": repr(rv), "planner": repr(mv)})
                    report["scenarios"].append({"material": mid, "quality_type": quality_type, "mode": mode,
                                                "extra_layers": extra, "compared": compared,
                                                "mismatches": len(mismatches), "errors": errors,
                                                "first": mismatches[:25]})
                    report["total_compared"] += compared
                    report["total_mismatch"] += len(mismatches)
                    report["reference_errors"] += errors
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)
    print("scenarios %d, compared %d, mismatches %d, errors %d" % (
        len(report["scenarios"]), report["total_compared"], report["total_mismatch"], report["reference_errors"]))


if __name__ == "__main__":
    main()
