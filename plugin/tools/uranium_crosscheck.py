# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Cross-check of ste_planner.settings_schema against the real Uranium of STE Slicer.

Runs inside the slicer's Python 3.5 (tools/run_py35_tests.py --script this_file). The
reference stack uses Uranium's DefinitionContainer/ContainerStack/SettingFunction from the
distribution and reproduces STE's GlobalStack/ExtruderStack.getProperty rules (resolve,
limit_to_extruder, delegation of global settings) and the formula functions of
SteSlicerFormulaFunctions. Every setting value is compared for several modes, materials
and user patches. Output: JSON report at argv[1].
"""

import collections
import json
import math
import os
import sys
import threading

RES = os.environ["STE_RESOURCES_DIR"]
OUT = sys.argv[1]

# Uranium creates storage folders under %APPDATA% on first use; keep them out of the user profile.
import tempfile  # noqa: E402
_tmp = tempfile.mkdtemp(prefix="uranium_check_")
os.environ["APPDATA"] = os.environ["LOCALAPPDATA"] = _tmp

if sys.version_info >= (3, 12):
    # Uranium imports `imp` only for plugin loading, which this check does not use.
    import types
    sys.modules.setdefault("imp", types.ModuleType("imp"))

from UM.Resources import Resources  # noqa: E402
Resources.addSearchPath(RES)
import UM.Settings.Interfaces  # noqa: E402
# Definition files of the installed version need no version upgrade; skip the upgrade manager.
UM.Settings.Interfaces.ContainerInterface.deserialize = lambda self, serialized, file_name=None: serialized
from UM.Settings.DefinitionContainer import DefinitionContainer  # noqa: E402
from UM.Settings.ContainerStack import ContainerStack  # noqa: E402
from UM.Settings.SettingFunction import SettingFunction  # noqa: E402
from UM.Settings.Interfaces import PropertyEvaluationContext  # noqa: E402
from UM.Settings.SettingInstance import InstanceState  # noqa: E402
from UM.Signal import Signal  # noqa: E402
from UM.Settings.SettingDefinition import SettingDefinition, DefinitionPropertyType  # noqa: E402
from UM.Settings.Validator import Validator  # noqa: E402

# Registrations done by SteSlicerApplication.__init__ (steslicer/SteSlicerApplication.py).
SettingDefinition.addSupportedProperty("settable_per_mesh", DefinitionPropertyType.Any, default=True, read_only=True)
SettingDefinition.addSupportedProperty("settable_per_extruder", DefinitionPropertyType.Any, default=True,
                                       read_only=True)
SettingDefinition.addSupportedProperty("settable_per_meshgroup", DefinitionPropertyType.Any, default=True,
                                       read_only=True)
SettingDefinition.addSupportedProperty("settable_globally", DefinitionPropertyType.Any, default=True, read_only=True)
SettingDefinition.addSupportedProperty("limit_to_extruder", DefinitionPropertyType.Function, default="-1",
                                       depends_on="value")
SettingDefinition.addSupportedProperty("resolve", DefinitionPropertyType.Function, default=None, depends_on="value")
SettingDefinition.addSettingType("extruder", None, str, Validator)
SettingDefinition.addSettingType("optional_extruder", None, str, None)
SettingDefinition.addSettingType("[int]", None, str, None)
SettingDefinition.addSettingType("[float]", None, str, None)

from ste_planner import settings_schema as S, materials as M  # noqa: E402


class DictContainer(object):
    """Instance-container stand-in: holds only "value" (and the "state" User) of set keys."""
    propertyChanged = Signal()

    def __init__(self, cid, values):
        self._id = cid
        self._values = dict(values)

    def getId(self):
        return self._id

    def getName(self):
        return self._id

    def getMetaDataEntry(self, key, default=None):
        return default

    def getMetaData(self):
        return {}

    def getProperty(self, key, property_name, context=None):
        if key not in self._values:
            return None
        if property_name == "value":
            return self._values[key]
        if property_name == "state":
            return InstanceState.User
        return None

    def hasProperty(self, key, property_name):
        return key in self._values and property_name in ("value", "state")


def load_definition(def_id, folder):
    d = DefinitionContainer(def_id)
    with open(os.path.join(RES, folder, def_id + ".def.json"), encoding="utf-8") as f:
        d.deserialize(f.read())
    return d


class RefGlobal(ContainerStack):
    def __init__(self, sid, definition):
        super().__init__(sid)
        self.definition = definition
        self.extruders = {}
        self._resolving = collections.defaultdict(set)

    def getProperty(self, key, property_name, context=None):
        if not self.definition.findDefinitions(key=key):
            return None
        if context is None:
            context = PropertyEvaluationContext()
        context.pushContainer(self)
        if property_name == "value":
            th = threading.current_thread().name
            state = ContainerStack.getProperty(self, key, "state", context)
            if key not in self._resolving[th] and (state is None or state == InstanceState.Default):
                self._resolving[th].add(key)
                resolve = ContainerStack.getProperty(self, key, "resolve", context)
                self._resolving[th].remove(key)
                if resolve is not None:
                    context.popContainer()
                    return resolve
        lim = ContainerStack.getProperty(self, key, "limit_to_extruder", context)
        if lim is not None:
            lim = str(0 if lim == -1 else lim)
        if lim is not None and lim != "-1" and lim in self.extruders:
            if ContainerStack.getProperty(self, key, "settable_per_extruder", context):
                result = self.extruders[lim].getProperty(key, property_name, context)
                if result is not None:
                    context.popContainer()
                    return result
        result = ContainerStack.getProperty(self, key, property_name, context)
        context.popContainer()
        return result


class RefExtruder(ContainerStack):
    def __init__(self, sid, position):
        super().__init__(sid)
        self.position = str(position)

    def getMetaDataEntry(self, key, default=None):
        if key == "position":
            return self.position
        return super().getMetaDataEntry(key, default)

    def getProperty(self, key, property_name, context=None):
        if context is None:
            context = PropertyEvaluationContext()
        context.pushContainer(self)
        if not ContainerStack.getProperty(self, key, "settable_per_extruder", context):
            result = self.getNextStack().getProperty(key, property_name, context)
            context.popContainer()
            return result
        lim = ContainerStack.getProperty(self, key, "limit_to_extruder", context)
        if lim is not None:
            lim = str(0 if lim == -1 else lim)
        if lim is not None and lim != "-1" and self.position != lim and lim in self.getNextStack().extruders:
            result = self.getNextStack().extruders[lim].getProperty(key, property_name, context)
            if result is not None:
                context.popContainer()
                return result
        result = ContainerStack.getProperty(self, key, property_name, context)
        context.popContainer()
        return result


def build_reference(machine_id, user_global, user_ext0, material_values):
    gdef = load_definition(machine_id, "definitions")
    g = RefGlobal("global", gdef)
    g.addContainer(gdef)
    g.addContainer(DictContainer("user_global", user_global))
    trains = gdef.getMetaDataEntry("machine_extruder_trains")
    for pos, ext_id in sorted(trains.items()):
        e = RefExtruder("ext%s" % pos, pos)
        edef = load_definition(ext_id, "extruders")
        e.addContainer(edef)
        e.addContainer(DictContainer("material%s" % pos, material_values if pos == "0" else {}))
        e.addContainer(DictContainer("user_ext%s" % pos, user_ext0 if pos == "0" else {}))
        e.setNextStack(g)
        g.extruders[pos] = e

    def extruder_value(pos, key, context=None):
        pos = 0 if pos == -1 else pos
        ext = g.extruders[str(pos)]
        value = ext.getRawProperty(key, "value", context=context)
        if isinstance(value, SettingFunction):
            value = value(ext, context=context)
        return value

    def extruder_values(key, context=None):
        out = []
        count = g.getProperty("machine_extruder_count", "value", context=context)
        for pos, ext in sorted(g.extruders.items()):
            if int(pos) >= count:
                continue
            value = ext.getRawProperty(key, "value", context=context)
            if value is None:
                continue
            if isinstance(value, SettingFunction):
                value = value(ext, context=context)
            out.append(value)
        if not out:
            out.append(g.getProperty(key, "value", context=context))
        return out

    SettingFunction.registerOperator("extruderValue", extruder_value)
    SettingFunction.registerOperator("extruderValues", extruder_values)
    SettingFunction.registerOperator("resolveOrValue", lambda key, context=None: g.getProperty(key, "value",
                                                                                             context=context))
    SettingFunction.registerOperator("defaultExtruderPosition", lambda: "0")
    return g


def same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) if (isinstance(a, (bool, int, float)) and isinstance(b, (bool, int, float))) \
            else a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return str(a) == str(b)


def main():
    machine = "hte530v5.2.4"
    schema = S.load_machine_schema(RES, machine)
    cat = M.load_catalog()
    scenarios = []
    for mode in ("classic", "cylindrical", "cylindrical_full"):
        for mid in ("enduse_petg", "fiberpart_pp_g30", "sealant_tpu_A95"):
            user = {"printing_mode": mode}
            ext = {}
            if mid == "fiberpart_pp_g30":
                user.update({"layer_height": 0.3, "wall_line_count": 3, "infill_sparse_density": 100.0,
                             "top_layers": 4, "bottom_layers": 4, "support_enable": False})
                ext.update({"machine_nozzle_size": 0.6, "speed_print": 40.0})
            elif mid == "sealant_tpu_A95":
                user.update({"layer_height": 0.2, "wall_line_count": 4, "infill_sparse_density": 100.0,
                             "support_enable": True})
                ext.update({"speed_print": 30.0})
            if mode != "classic":
                user.update({"cylindrical_mode_base_diameter": 12.0, "non_printing_base_diameter": 8.0})
            scenarios.append((mode, mid, user, ext))
    report = {"scenarios": [], "total_compared": 0, "total_mismatch": 0, "reference_errors": 0}
    for mode, mid, user, ext in scenarios:
        mat = S.material_settings_from_file(cat.get(mid).slicer)
        ref = build_reference(machine, user, ext, mat)
        mine = S.SettingResolver(schema, user, {"0": ext}, material_settings=mat)
        mismatches, compared, errors = [], 0, 0
        for key, d in sorted(schema.definitions.items()):
            if d.type in (None, "category"):
                continue
            try:
                if d.per_extruder:
                    rv = ref.extruders["0"].getProperty(key, "value")
                    mv = mine.value(key, extruder="0")
                else:
                    rv = ref.getProperty(key, "value")
                    mv = mine.value(key)
            except Exception as e:
                errors += 1
                mismatches.append({"key": key, "error": "%s: %s" % (type(e).__name__, e)})
                continue
            compared += 1
            if not same(rv, mv):
                mismatches.append({"key": key, "uranium": repr(rv), "planner": repr(mv)})
        report["scenarios"].append({"mode": mode, "material": mid, "compared": compared,
                                    "mismatches": len(mismatches), "errors": errors, "first": mismatches[:25]})
        report["total_compared"] += compared
        report["total_mismatch"] += len(mismatches)
        report["reference_errors"] += errors
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
