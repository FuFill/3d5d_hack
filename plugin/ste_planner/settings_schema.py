# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Read-only model of the STE Slicer setting schema (specification sections 9.1 and 12).

Loads a machine definition chain (fdmprinter -> ... -> machine) with its
extruder definitions, evaluates setting expressions with a whitelisted AST
interpreter (expressions are data, never executed as code) and resolves
effective values over a stack of layers:

    user (global/extruder) -> material -> extruder definition -> machine definition chain -> schema

With a native preset base the instance layers follow STE's _ContainerIndexes
(SteSlicerContainerStack): user -> quality_changes -> quality -> material ->
variant -> definition_changes -> definition, for the global and every extruder stack.

This is an offline preview used for validation and planning. Inside the
running slicer the authoritative values are read back from Uranium after a
patch is applied (resolve_settings / read-after-write).
"""

import ast
import json
import math
import os

# Mapping used by STE's XmlMaterialProfile for .fdm_material <setting> keys.
MATERIAL_SETTING_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "shrinkage percentage": "material_shrinkage_percentage",
}

DEF_FIELDS = ("label", "description", "type", "unit", "default_value", "value", "minimum_value",
              "maximum_value", "minimum_value_warning", "maximum_value_warning", "enabled", "options",
              "settable_per_extruder", "settable_per_mesh", "settable_per_meshgroup", "settable_globally",
              "limit_to_extruder", "resolve")


class EvalError(Exception):
    pass


class SettingDefinition(object):
    def __init__(self, key, data, parent=None, category=None):
        self.key = key
        self.parent = parent
        self.category = category
        self.children = []
        self.fields = dict((f, data[f]) for f in DEF_FIELDS if f in data)

    def get(self, field, default=None):
        return self.fields.get(field, default)

    @property
    def type(self):
        return self.fields.get("type")

    @property
    def per_extruder(self):
        # STE registers settable_per_extruder with default True (SteSlicerApplication.__init__).
        return bool(self.fields.get("settable_per_extruder", True))


class MachineSchema(object):
    """Definitions of one machine as Uranium sees them.

    definitions: settings of the global definition container (fdmprinter + machine chain);
    extruder_definitions: settings of the extruder definition containers (fdmextruder);
    overrides are applied per container like Uranium does (only to settings it defines).
    """

    def __init__(self, definitions, global_overrides, extruder_overrides, machine_id, chain, metadata,
                 extruder_definitions=None):
        self.definitions = definitions
        self.extruder_definitions = extruder_definitions or {}
        self.global_overrides = global_overrides
        self.extruder_overrides = extruder_overrides
        self.machine_id = machine_id
        self.chain = chain
        self.metadata = metadata

    def has(self, key):
        return key in self.definitions or key in self.extruder_definitions

    def definition(self, key):
        return self.definitions.get(key) or self.extruder_definitions.get(key)

    def field(self, key, field, extruder=None):
        """Definition field after overrides, from the global or an extruder definition container."""
        if extruder is not None:
            return self.extruder_field(key, field, extruder)
        if key not in self.definitions:
            return None
        ov = self.global_overrides.get(key, {})
        if field in ov:
            return ov[field]
        return self.definitions[key].get(field)

    def extruder_field(self, key, field, position):
        """Field in the extruder definition container; None if the container lacks the setting."""
        if key not in self.extruder_definitions:
            return None
        ov = self.extruder_overrides.get(str(position), {}).get(key, {})
        if field in ov:
            return ov[field]
        return self.extruder_definitions[key].get(field)

    def any_field(self, key, field):
        v = self.field(key, field)
        if v is None and key not in self.definitions:
            v = self.extruder_field(key, field, "0")
        return v

    def extruder_positions(self):
        return sorted(self.extruder_overrides.keys())


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _flatten(tree, out, parent=None, category=None):
    for key, data in tree.items():
        sd = SettingDefinition(key, data, parent, category)
        out[key] = sd
        if parent is not None and parent in out:
            out[parent].children.append(key)
        if "children" in data:
            _flatten(data["children"], out, key, category if category else key)


def load_machine_schema(resources_dir, machine_id):
    """Load definitions/<machine_id>.def.json with its inheritance chain and extruders."""
    def_dir = os.path.join(resources_dir, "definitions")
    ext_dir = os.path.join(resources_dir, "extruders")
    chain = []
    cur = machine_id
    while cur:
        data = _read_json(os.path.join(def_dir, cur + ".def.json"))
        chain.append((cur, data))
        cur = data.get("inherits")
    chain.reverse()  # base first
    definitions = {}
    for _, data in chain:
        if "settings" in data:
            for cat_key, cat in data["settings"].items():
                definitions[cat_key] = SettingDefinition(cat_key, cat, None, None)
                _flatten(cat.get("children", {}), definitions, cat_key, cat_key)
    extruder_definitions = {}
    fdmextruder = os.path.join(def_dir, "fdmextruder.def.json")
    if os.path.exists(fdmextruder):
        ext = _read_json(fdmextruder)
        for cat_key, cat in ext.get("settings", {}).items():
            extruder_definitions[cat_key] = SettingDefinition(cat_key, cat, None, None)
            _flatten(cat.get("children", {}), extruder_definitions, cat_key, cat_key)
    global_overrides = {}
    metadata = {}
    for _, data in chain:
        metadata.update(data.get("metadata", {}))
        for key, ov in data.get("overrides", {}).items():
            if key in definitions:          # Uranium ignores overrides of undefined settings
                global_overrides.setdefault(key, {}).update(ov)
    extruder_overrides = {}
    trains = metadata.get("machine_extruder_trains") or {"0": None}
    for pos, ext_id in sorted(trains.items()):
        merged = {}
        if ext_id:
            ext_chain = []
            cur = ext_id
            while cur and cur != "fdmextruder":
                path = os.path.join(ext_dir, cur + ".def.json")
                if not os.path.exists(path):
                    path = os.path.join(def_dir, cur + ".def.json")
                data = _read_json(path)
                ext_chain.append(data)
                cur = data.get("inherits")
            for data in reversed(ext_chain):
                for key, ov in data.get("overrides", {}).items():
                    if key in extruder_definitions:
                        merged.setdefault(key, {}).update(ov)
        extruder_overrides[str(pos)] = merged
    return MachineSchema(definitions, global_overrides, extruder_overrides, machine_id,
                         [c[0] for c in chain], metadata, extruder_definitions)


# ---------------------------------------------------------------- safe evaluation

_MATH = {"sqrt": math.sqrt, "ceil": math.ceil, "floor": math.floor, "log": math.log, "tan": math.tan,
         "radians": math.radians, "degrees": math.degrees, "sin": math.sin, "cos": math.cos, "pi": math.pi,
         "atan": math.atan, "exp": math.exp, "pow": math.pow}
_BUILTINS = {"min": min, "max": max, "round": round, "int": int, "float": float, "str": str, "len": len,
             "sum": sum, "any": any, "all": all, "abs": abs, "bool": bool, "map": map, "list": list}
_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
           ast.FloorDiv: lambda a, b: a // b}
_CMPOPS = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
           ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


def evaluate(expr, lookup, functions=None, max_nodes=500):
    """Evaluate a setting expression; names resolve through lookup(name)."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise EvalError("syntax: %s" % e)
    count = [0]
    funcs = dict(_BUILTINS)
    funcs.update(functions or {})
    local_scopes = []

    def ev(node):
        count[0] += 1
        if count[0] > max_nodes * 20:
            raise EvalError("expression too complex")
        t = type(node)
        if t is ast.Expression:
            return ev(node.body)
        if t.__name__ in ("Constant", "NameConstant"):
            return node.value
        if t.__name__ == "Num":   # Python 3.5-3.7
            return node.n
        if t.__name__ in ("Str", "Bytes"):
            return node.s
        if t is ast.Name:
            for scope in reversed(local_scopes):
                if node.id in scope:
                    return scope[node.id]
            if node.id in ("True", "False", "None"):
                return {"True": True, "False": False, "None": None}[node.id]
            if node.id == "math":
                return _MATH
            return lookup(node.id)
        if t is ast.Attribute:
            base = ev(node.value)
            if base is _MATH and node.attr in _MATH:
                return _MATH[node.attr]
            raise EvalError("attribute access not allowed: %s" % node.attr)
        if t is ast.BinOp:
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise EvalError("operator not allowed")
            return op(ev(node.left), ev(node.right))
        if t is ast.UnaryOp:
            v = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return +v
            if isinstance(node.op, ast.Not):
                return not v
            raise EvalError("unary operator not allowed")
        if t is ast.BoolOp:
            if isinstance(node.op, ast.And):
                v = True
                for x in node.values:
                    v = ev(x)
                    if not v:
                        return v
                return v
            v = False
            for x in node.values:
                v = ev(x)
                if v:
                    return v
            return v
        if t is ast.Compare:
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                f = _CMPOPS.get(type(op))
                if f is None:
                    raise EvalError("comparison not allowed")
                if not f(left, right):
                    return False
                left = right
            return True
        if t is ast.IfExp:
            return ev(node.body) if ev(node.test) else ev(node.orelse)
        if t in (ast.List, ast.Tuple):
            vals = [ev(x) for x in node.elts]
            return vals if t is ast.List else tuple(vals)
        if t is ast.Subscript:
            base = ev(node.value)
            sl = node.slice
            if type(sl).__name__ == "Index":
                sl = sl.value
            return base[ev(sl)]
        if t is ast.Call:
            if isinstance(node.func, ast.Name):
                name = node.func.id
                if name not in funcs:
                    raise EvalError("function not allowed: %s" % name)
                f = funcs[name]
            elif isinstance(node.func, ast.Attribute):
                f = ev(node.func)
            else:
                raise EvalError("call not allowed")
            if getattr(node, "keywords", None):
                raise EvalError("keyword arguments not allowed")
            return f(*[ev(a) for a in node.args])
        if t in (ast.GeneratorExp, ast.ListComp):
            if len(node.generators) != 1:
                raise EvalError("nested comprehension not allowed")
            gen = node.generators[0]
            if not isinstance(gen.target, ast.Name):
                raise EvalError("comprehension target not allowed")
            out = []
            for item in ev(gen.iter):
                local_scopes.append({gen.target.id: item})
                try:
                    if all(ev(c) for c in gen.ifs):
                        out.append(ev(node.elt))
                finally:
                    local_scopes.pop()
            return out
        raise EvalError("syntax element not allowed: %s" % t.__name__)

    try:
        return ev(tree)
    except EvalError:
        raise
    except (ArithmeticError, TypeError, ValueError, KeyError, IndexError, AttributeError) as e:
        raise EvalError("%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------- resolver

class SettingResolver(object):
    """Effective values with the semantics of STE's GlobalStack/ExtruderStack over Uranium.

    Global stack containers: user_global -> global definition (machine chain).
    Extruder stack P: user_extruder[P] -> material[P] -> extruder definition, then the
    global stack's raw values. Rules reproduced from steslicer/Settings:
    - GlobalStack: a setting the user did not set returns its "resolve" if defined;
      "limit_to_extruder" redirects per-extruder settings to that extruder;
    - ExtruderStack: settings not settable per extruder are delegated to the global stack;
      "limit_to_extruder" redirects to another extruder;
    - setting functions are evaluated in the root stack of the query (Uranium
      PropertyEvaluationContext.rootStack), formula functions in their own stacks.
    All extruders of the definition are enabled (STE default); extruders without a
    material use definition defaults.

    global_layers / extruder_layers: optional ordered [(name, values)] instance
    containers below the user container (quality_changes ... definition_changes).
    Container values beginning with "=" are setting functions, as in Uranium.
    Any value stored in a global instance container has InstanceState.User, so
    the definition "resolve" is skipped for it (GlobalStack._shouldResolve).
    Without extruder_layers the material settings form the only extruder layer.
    """

    def __init__(self, schema, user_global=None, user_extruders=None, material_settings=None,
                 material_by_extruder=None, global_layers=None, extruder_layers=None):
        self.schema = schema
        self.user_global = dict(user_global or {})
        self.user_extruders = dict((str(k), dict(v)) for k, v in (user_extruders or {}).items())
        self.material_by_extruder = dict((str(k), dict(v)) for k, v in (material_by_extruder or {}).items())
        if material_settings is not None:
            self.material_by_extruder.setdefault("0", {}).update(material_settings)
        self.global_layers = [(str(n), dict(v)) for n, v in (global_layers or [])]
        if extruder_layers is None:
            self.extruder_layers = dict((pos, [("material", vals)]) for pos, vals in self.material_by_extruder.items())
        else:
            self.extruder_layers = dict((str(pos), [(str(n), dict(v)) for n, v in layers])
                                        for pos, layers in extruder_layers.items())
        self._cache = {}
        self._stack = []
        self._resolving = set()

    def copy_with(self, user_global=None, user_extruders=None):
        g = dict(self.user_global)
        g.update(user_global or {})
        e = dict((k, dict(v)) for k, v in self.user_extruders.items())
        for pos, vals in (user_extruders or {}).items():
            e.setdefault(str(pos), {}).update(vals)
        return SettingResolver(self.schema, g, e, None, self.material_by_extruder,
                               self.global_layers, self.extruder_layers)

    def without_user(self):
        """The same container layers with empty user containers: Resolve(P)."""
        return SettingResolver(self.schema, None, None, None, self.material_by_extruder,
                               self.global_layers, self.extruder_layers)

    # -- public API ----------------------------------------------------------------

    def value(self, key, extruder=None):
        """Effective value as seen from the global stack (extruder=None) or an extruder stack."""
        if not self.schema.has(key):
            raise EvalError("unknown setting %s" % key)
        stack = None if extruder is None else str(extruder)
        return self._get(key, "value", stack, stack)

    def source(self, key, extruder=None):
        stack = None if extruder is None else str(extruder)
        if stack is not None and key in self.user_extruders.get(stack, {}):
            return "user_extruder_" + stack
        if key in self.user_global:
            return "user_global"
        if stack is not None:
            for name, values in self.extruder_layers.get(stack, []):
                if key in values:
                    return name if name == "material" else name + "_extruder_" + stack
        for name, values in self.global_layers:
            if key in values:
                return name + "_global"
        return "definition"

    def enabled(self, key, extruder=None):
        stack = None if extruder is None else str(extruder)
        try:
            raw = self._get(key, "enabled", stack, stack)
        except EvalError:
            return None
        if raw is None:
            return True
        return bool(raw)

    def bounds(self, key, extruder=None):
        stack = None if extruder is None else str(extruder)
        out = {}
        for f in ("minimum_value", "maximum_value", "minimum_value_warning", "maximum_value_warning"):
            try:
                v = self._get(key, f, stack, stack)
            except EvalError:
                out[f] = None
                continue
            if v is None:
                continue
            try:
                out[f] = float(v)
            except (TypeError, ValueError):
                out[f] = None
        return out

    # -- stack semantics -----------------------------------------------------------

    def _per_extruder(self, key):
        d = self.schema.definition(key)
        return d.per_extruder if d is not None else False

    def _get(self, key, prop, stack, root):
        ck = (key, prop, stack, root)
        if ck in self._cache:
            return self._cache[ck]
        if ck in self._stack:
            raise EvalError("cyclic dependency at %s.%s" % (key, prop))
        self._stack.append(ck)
        try:
            if stack is None:
                v = self._global_get(key, prop, root)
            else:
                v = self._extruder_get(key, prop, stack, root)
        finally:
            self._stack.pop()
        if prop == "value":
            v = coerce(self.schema.definition(key), v)
        self._cache[ck] = v
        return v

    def _limit(self, key, stack, root):
        raw, holder = self._raw(key, "limit_to_extruder", stack)
        if raw is None:
            return None
        v = self._eval(raw, root if root is not None else holder)
        if v is None:
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return str(0 if v == -1 else v)

    def _global_get(self, key, prop, root):
        if key not in self.schema.definitions:
            return None
        if prop == "value" and not self._global_instance_value(key) and key not in self._resolving:
            res = self.schema.field(key, "resolve")
            if res is not None:
                self._resolving.add(key)
                try:
                    return self._eval(res, root)
                finally:
                    self._resolving.discard(key)
        if self._per_extruder(key):
            lim = self._limit(key, None, root)
            if lim is not None and lim in self.schema.extruder_positions():
                r = self._extruder_get(key, prop, lim, root)
                if r is not None:
                    return r
        raw, _ = self._raw(key, prop, None)
        return self._eval(raw, root)

    def _extruder_get(self, key, prop, stack, root):
        if not self._per_extruder(key):
            return self._global_get(key, prop, root)
        lim = self._limit(key, stack, root)
        if lim is not None and lim != stack and lim in self.schema.extruder_positions():
            r = self._extruder_get(key, prop, lim, root)
            if r is not None:
                return r
        raw, _ = self._raw(key, prop, stack)
        return self._eval(raw, root)

    def _global_instance_value(self, key):
        return key in self.user_global or any(key in values for _, values in self.global_layers)

    def _raw(self, key, prop, stack):
        """Raw property (literal or expression string) like ContainerStack.getRawProperty."""
        if prop == "value":
            if stack is not None:
                if key in self.user_extruders.get(stack, {}):
                    return _Literal(self.user_extruders[stack][key]), stack
                for _, values in self.extruder_layers.get(stack, []):
                    if key in values:
                        return _instance_raw(values[key], self.schema.definition(key)), stack
                if key in self.schema.extruder_definitions:
                    v = self.schema.extruder_field(key, "value", stack)
                    if v is None:
                        v = _Literal(self.schema.extruder_field(key, "default_value", stack))
                    return v, stack
            if key in self.user_global:
                return _Literal(self.user_global[key]), None
            for _, values in self.global_layers:
                if key in values:
                    return _instance_raw(values[key], self.schema.definition(key)), None
            if key in self.schema.definitions:
                v = self.schema.field(key, "value")
                if v is None:
                    v = _Literal(self.schema.field(key, "default_value"))
                return v, None
            return None, None
        if stack is not None and key in self.schema.extruder_definitions:
            return self.schema.extruder_field(key, prop, stack), stack
        if key in self.schema.definitions:
            return self.schema.field(key, prop), None
        return None, None

    def _eval(self, raw, root):
        if isinstance(raw, _Literal):
            return raw.value
        if not isinstance(raw, str):
            return raw

        def lookup(name):
            if not self.schema.has(name):
                raise EvalError("unknown setting %s" % name)
            return self._get(name, "value", root, root)

        return evaluate(raw, lookup, self._functions())

    def _functions(self):
        positions = self.schema.extruder_positions()

        def own(key, pos):
            raw, _ = self._raw(key, "value", pos)
            return self._eval(raw, pos)

        def extruder_value(pos, key):
            pos = 0 if pos is None or int(pos) == -1 else int(pos)
            return own(key, str(pos))

        def extruder_values(key):
            count = self._get("machine_extruder_count", "value", None, None)
            out = [own(key, p) for p in positions if int(p) < (count or 1)]
            out = [v for v in out if v is not None]
            return out or [self._get(key, "value", None, None)]

        return {"extruderValue": extruder_value, "extruderValues": extruder_values,
                "resolveOrValue": lambda key: self._get(key, "value", None, None),
                "defaultExtruderPosition": lambda: "0", "valueFromContainer": lambda i: None,
                "anyExtruderWithMaterial": lambda k: "0", "anyExtruderNrWithOrDefault": lambda k: "0"}

    # -- validation ----------------------------------------------------------------

    def validate(self, key, value, extruder=None):
        """Validator state like Uranium: 'valid', 'warning', 'error' plus messages."""
        d = self.schema.definition(key)
        if d is None:
            return "error", ["unknown setting"]
        msgs = []
        t = d.type
        if t == "enum":
            opts = self.schema.any_field(key, "options") or {}
            if value not in opts:
                return "error", ["%r not in options %s" % (value, sorted(opts))]
            return "valid", []
        if t == "bool":
            return ("valid", []) if isinstance(value, bool) else ("error", ["expected bool"])
        if t in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "error", ["expected number"]
            if t == "int" and int(value) != value:
                return "error", ["expected integer"]
            b = self.bounds(key, extruder)
            state = "valid"
            if b.get("minimum_value") is not None and value < b["minimum_value"]:
                return "error", ["below minimum %g" % b["minimum_value"]]
            if b.get("maximum_value") is not None and value > b["maximum_value"]:
                return "error", ["above maximum %g" % b["maximum_value"]]
            if b.get("minimum_value_warning") is not None and value < b["minimum_value_warning"]:
                state = "warning"
                msgs.append("below warning minimum %g" % b["minimum_value_warning"])
            if b.get("maximum_value_warning") is not None and value > b["maximum_value_warning"]:
                state = "warning"
                msgs.append("above warning maximum %g" % b["maximum_value_warning"])
            return state, msgs
        if t in ("[int]", "[float]"):
            try:
                vals = ast.literal_eval(value) if isinstance(value, str) else value
                if not isinstance(vals, (list, tuple)):
                    raise ValueError
            except (ValueError, SyntaxError):
                return "error", ["expected list"]
            return "valid", []
        return "valid", []


class _Literal(object):
    """A value stored in a container (not an expression to evaluate)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _instance_raw(value, definition):
    """Instance container value: "=expr" is a SettingFunction (Uranium SettingInstance.setProperty);
    a stored string literal is read with the definition type (SettingDefinition.settingValueFromString)."""
    if isinstance(value, str) and value.strip().startswith("="):
        return value.strip()[1:]
    return _Literal(coerce(definition, value))


def coerce(definition, value):
    t = definition.type if definition else None
    if value is None:
        return None
    try:
        if t == "int":
            # Uranium never rounds an int setting: a formula result is returned as is and
            # a stored string goes through ast.literal_eval ("3.7" -> 3.7). STE sends
            # str(value) and CuraEngine truncates it (stoul/atoi), so a fractional value
            # such as the 5D raft_surface_layers 29.9 must stay fractional here.
            number = float(value)
            return int(number) if number.is_integer() else number
        if t == "float":
            return float(value)
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "1", "yes")
            return bool(value)
    except (TypeError, ValueError):
        return value
    return value


def material_settings_from_file(material_record):
    """Map a slicer material (catalog 'slicer' entry) to setting keys of the material container."""
    out = {}
    for k, v in (material_record.get("settings") or {}).items():
        key = MATERIAL_SETTING_MAP.get(k)
        if key is None:
            continue
        try:
            out[key] = float(v)
        except ValueError:
            continue
    if material_record.get("diameter"):
        out["material_diameter"] = float(material_record["diameter"])
    return out
