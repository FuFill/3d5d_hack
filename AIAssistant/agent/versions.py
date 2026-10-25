# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Model tree of a chat: every finished piece of the agent's work is a node.

A node ("model") is a snapshot of the print setup: the containers of every stack (quality
changes, quality, material, variant, definition changes) with the user's values on top,
the printing mode and the pose and mesh of every part on the plate. The tree belongs to one chat;
its selected node is restored when the chat is opened again. Nodes are made only when a
turn of the agent ends with a result, never for intermediate states.

A plan applied in place may replace the mesh of a part (5D: the raw STL indexed for the
5D backend instead of the centred one) and add parts (batch copies, modifier meshes);
the saved matrix belongs to the mesh it was saved with. A node therefore also brings back
the mesh of each part and the set of parts: parts this tree knows that the node has not
are taken off the plate (kept for the nodes that have them), parts it has come back.
"""

from collections import OrderedDict
import re
import time
import uuid

from .mainthread import run_on_main
from .tools import MODE_NAMES, ToolError, _plain

# What a node card shows first: the mode, preset, material, nozzle and placement, then the settings
# changed explicitly (user values), then what moved with the preset, most significant first.
PRIORITY = ("printing_mode", "preset", "material", "nozzle", "objects")
IMPORTANT = ("layer_height", "infill_sparse_density", "wall_line_count", "speed_print", "support_enable",
             "support_type", "adhesion_type", "top_layers", "bottom_layers", "infill_pattern",
             "material_print_temperature", "material_bed_temperature", "line_width", "cool_fan_speed",
             "retraction_enable", "layer_height_0", "wall_thickness", "top_bottom_thickness", "speed_wall_0",
             "speed_wall_x", "speed_infill", "speed_topbottom", "speed_travel", "support_angle", "support_infill_rate",
             "brim_width", "retraction_amount", "retraction_speed", "cool_fan_enabled", "infill_overlap",
             "material_flow", "magic_spiralize")
EFFECTIVE_KEYS = IMPORTANT[:15]  # read even by snapshots of older versions
MAX_CHANGES = 10
MESH_CACHE = 32  # meshes of saved nodes kept in this session; older ones are matched by their bounding box
VALUE_CHARS = 40
NAMES = {"printing_mode": ("Режим печати", "Printing mode"), "preset": ("Пресет", "Preset"),
         "material": ("Материал", "Material"), "nozzle": ("Сопло", "Nozzle"),
         "objects": ("Положение деталей", "Part placement"), "count": ("Деталей на столе", "Parts on the plate")}
TAGS = {True: ("явно", "set"), False: ("пресет", "preset")}
MORE = ("и ещё %d", "and %d more")
START_NAME = ("Исходная", "Start")
START_NOTE = ("Исходное состояние проекта", "The project before the first change")
SAME_NOTE = ("Без изменений настроек", "No setting changes")


def abbreviation(name):
    """Two letters for the node circle: initials of two words or the start of one word."""
    words = re.findall(r"[^\W_]+", name or "", re.UNICODE)
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].capitalize()
    return "•"


def duration_text(seconds, language="ru"):
    if not seconds:
        return ""
    hours, minutes = int(seconds) // 3600, int(round((int(seconds) % 3600) / 60.0))
    if minutes == 60:
        hours, minutes = hours + 1, 0
    h, m = ("ч", "мин") if language == "ru" else ("h", "min")
    if hours:
        return "%d %s %d %s" % (hours, h, minutes, m) if minutes else "%d %s" % (hours, h)
    return "%d %s" % (max(1, minutes), m)


def _number(value):
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    text = str(value)
    return text if len(text) <= VALUE_CHARS else text[:VALUE_CHARS - 1] + "…"


def _value_text(value, info, language):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return ("да" if value else "нет") if language == "ru" else ("yes" if value else "no")
    options = (info or {}).get("options") or {}
    if isinstance(value, str) and value in options:
        return options[value]
    unit = (info or {}).get("unit") or ""
    text = _number(value)
    return text + ("%" if unit == "%" else " " + unit if unit else "")


def _pose(item):
    """World pose of the part's mesh centre: the same for the centred and the raw STL of one placement."""
    matrix = [[float(v) for v in row] for row in item.get("world_matrix", item.get("matrix")) or []]
    box = item.get("mesh_box")
    if box and len(matrix) == 4:
        centre = [(float(a) + float(b)) / 2.0 for a, b in zip(box[0], box[1])]
        for row in matrix[:3]:
            row[3] += sum(row[i] * centre[i] for i in range(3))
    return [[round(v, 3) for v in row] for row in matrix]


def _poses(snapshot):
    """Poses by part: the order of the scene changes when parts come back to the plate."""
    objects = sorted(snapshot.get("objects") or [], key=lambda o: (str(o.get("id") or ""), str(o.get("name") or ""),
                                                                   str(o.get("source") or "")))
    return [(o.get("name"), o.get("source"), _pose(o), o.get("z_offset")) for o in objects]


def _user_keys(snapshot):
    return set(k for x in snapshot.get("stacks") or [] for k in (x.get("user") or {}))


def same_state(a, b):
    """True when two snapshots describe the same setup (containers, values, mode, poses)."""
    if not a or not b:
        return False
    # A restored node always has printing_mode among the user values; the mode is compared on its own.
    key = lambda s: sorted(((str(x.get("position")), x.get("containers"),
                             sorted(((k, v) for k, v in (x.get("user") or {}).items() if k != "printing_mode"),
                                    key=lambda i: i[0]))
                            for x in s.get("stacks") or []), key=lambda i: i[0])
    return (key(a) == key(b) and a.get("mode") == b.get("mode") and _poses(a) == _poses(b)
            and a.get("physics") == b.get("physics"))


def diff(old, new):
    """Every difference of `new` against `old`, most significant first (raw values, no labels).

    Settings are compared on their effective values; a setting read on one side only was
    switched on or off by another one and is shown through that one.
    """
    if old is None:
        return []
    rows = []
    if old.get("mode") != new.get("mode"):
        rows.append({"kind": "printing_mode", "old": old.get("mode"), "new": new.get("mode")})
    if old.get("preset") != new.get("preset"):
        rows.append({"kind": "preset", "old": old.get("preset"), "new": new.get("preset")})
    for kind in ("material", "nozzle"):
        before, after = old.get(kind + "s") or {}, new.get(kind + "s") or {}
        for position in sorted(set(before) | set(after), key=str):
            if before.get(position) != after.get(position):
                rows.append({"kind": kind, "old": before.get(position), "new": after.get(position)})
    poses_old, poses_new = _poses(old), _poses(new)
    if len(poses_old) != len(poses_new):
        rows.append({"kind": "count", "old": len(poses_old), "new": len(poses_new)})
    elif poses_old != poses_new:
        rotated = any([r[:3] for r in a[2][:3]] != [r[:3] for r in b[2][:3]] for a, b in zip(poses_old, poses_new))
        rows.append({"kind": "objects", "old": None, "new": "rotated" if rotated else "moved"})
    before, after = old.get("effective") or {}, new.get("effective") or {}
    explicit = _user_keys(old) | _user_keys(new)
    rank = dict((k, i) for i, k in enumerate(IMPORTANT))
    changed = [k for k in set(before) & set(after) if before[k] != after[k] and k != "printing_mode"]
    changed.sort(key=lambda k: (k not in explicit, rank.get(k, len(rank)), k))
    rows.extend({"kind": "setting", "key": k, "old": before[k], "new": after[k], "explicit": k in explicit}
                for k in changed)
    return rows


def describe(rows, labels, language="ru", limit=MAX_CHANGES):
    """Card lines of a diff in the window language: at most `limit`, then how many more there are."""
    index = 0 if language == "ru" else 1
    out = []
    for row in rows[:limit]:
        kind = row["kind"]
        if kind == "setting":
            info = (labels or {}).get(row["key"]) or {}
            label = (info.get("label_local") if index == 0 else info.get("label")) or info.get("label") or row["key"]
            out.append({"key": row["key"], "label": label, "old": _value_text(row["old"], info, language),
                        "new": _value_text(row["new"], info, language), "tag": TAGS[bool(row.get("explicit"))][index]})
        elif kind == "printing_mode":
            out.append({"key": kind, "label": NAMES[kind][index], "old": MODE_NAMES.get(row["old"], row["old"] or "—"),
                        "new": MODE_NAMES.get(row["new"], row["new"] or "—"), "tag": ""})
        elif kind == "objects":
            word = {"rotated": ("повёрнута", "rotated"), "moved": ("сдвинута", "moved")}[row["new"]][index]
            out.append({"key": kind, "label": NAMES[kind][index], "old": "", "new": word, "tag": ""})
        else:
            out.append({"key": kind, "label": NAMES[kind][index], "old": _number(row["old"]) if row["old"] is not None
                        else "—", "new": _number(row["new"]) if row["new"] is not None else "—", "tag": ""})
    more = max(0, len(rows) - limit)
    return out, (MORE[index] % more if more else "")


def changes(old, new, language="ru"):
    """Card lines of `new` against `old` from the snapshots alone (labels saved in them, if any)."""
    if old is None:
        return []
    labels = dict(old.get("labels") or {}, **(new.get("labels") or {}))
    return describe(diff(old, new), labels, language)[0]


class ModelTree:
    """Nodes of one chat, oldest first; `selected` is the node the slicer is on."""

    def __init__(self, data=None):
        data = data or {}
        self.nodes = list(data.get("nodes") or [])
        self.selected = data.get("selected")
        self.start = data.get("start")  # snapshot taken before the first work, kept until the first node

    def to_dict(self):
        return {"nodes": self.nodes, "selected": self.selected, "start": self.start}

    def node(self, node_id):
        return next((n for n in self.nodes if n["id"] == node_id), None)

    def object_ids(self):
        """IDs of the parts of every model: a part outside them was added by the user later and stays."""
        snapshots = [n.get("snapshot") or {} for n in self.nodes] + [(self.start or {}).get("snapshot") or {}]
        return set(o["id"] for x in snapshots for o in x.get("objects") or [] if o.get("id"))

    def current(self):
        return self.node(self.selected)

    def add(self, snapshot, name, stats=None, kind="model", rows=None, labels=None):
        """A new node under the selected one; it becomes the selected node.

        rows/labels: its differences to the parent with setting labels, read when it is made."""
        node = {"id": uuid.uuid4().hex[:10], "parent": self.selected, "kind": kind, "name": name,
                "short": abbreviation(name), "created": time.time(), "snapshot": snapshot, "stats": stats or {},
                "diff": rows if rows is not None else [], "labels": labels or {}}
        self.nodes.append(node)
        self.selected = node["id"]
        return node

    def view(self, language="ru"):
        """What the tree sheet and the node card show; snapshots stay on the server."""
        out = []
        for node in self.nodes:
            parent = self.node(node.get("parent"))
            stats = node.get("stats") or {}
            snapshot = node.get("snapshot") or {}
            effective = snapshot.get("effective") or {}
            infill = stats.get("infill", effective.get("infill_sparse_density"))
            printing_format = {"classic": "3D", "cylindrical": "5D", "cylindrical_full": "5D"}.get(
                snapshot.get("mode") or effective.get("printing_mode"))
            parts = [printing_format, duration_text(stats.get("print_time_s"), language)]
            if stats.get("material_g"):
                parts.append(("%s г" if language == "ru" else "%s g") % _number(stats["material_g"]))
            if infill is not None:
                parts.append(("заполнение %s %%" if language == "ru" else "infill %s %%") % _number(infill))
            empty = (START_NOTE if node.get("kind") == "start" else SAME_NOTE)[0 if language == "ru" else 1]
            if "diff" in node:
                rows, more = describe(node["diff"], node.get("labels"), language)
            elif parent is not None:  # a node of an earlier version: compared from the snapshots
                old, new = parent.get("snapshot") or {}, node.get("snapshot") or {}
                rows, more = describe(diff(old, new), dict(old.get("labels") or {}, **(new.get("labels") or {})),
                                      language)
            else:
                rows, more = [], ""
            out.append({"id": node["id"], "parent": node.get("parent"), "kind": node.get("kind", "model"), "empty": empty,
                        "name": node.get("name") or "", "short": node.get("short") or abbreviation(node.get("name")),
                        "created": node.get("created", 0), "stats": " · ".join(p for p in parts if p),
                        "changes": rows, "more": more, "selected": node["id"] == self.selected})
        return {"nodes": out, "selected": self.selected}


# ---------------------------------------------------------------------- the slicer side

def _stored(value):
    """User values as JSON: setting functions keep their expression ("=...")."""
    try:
        from UM.Settings.SettingFunction import SettingFunction
        if isinstance(value, SettingFunction):
            return str(value)
    except ImportError:
        pass
    return _plain(value)


def _loaded(value):
    if isinstance(value, str) and value.startswith("="):
        from UM.Settings.SettingFunction import SettingFunction
        return SettingFunction(value[1:])
    return value


class StateIO:
    """Reads and restores the print setup of the open project on the Qt thread."""

    def __init__(self, app, registry):
        self._app = app
        self._registry = registry
        self._meshes = OrderedDict()  # mesh id -> MeshData of a saved node
        self._boxes = {}              # mesh id -> local bounding box [min, max]
        self._detached = {}           # part id -> part taken off the plate by a node without it

    def capture(self):
        return run_on_main(self._app, self._capture)

    def restore(self, snapshot, known=None):
        """known: part IDs of the whole tree (ModelTree.object_ids)."""
        return run_on_main(self._app, lambda: self._restore(snapshot, known))

    def _sliceable(self):
        return bool(run_on_main(self._app, self._registry._scene_nodes))

    def labels(self, keys):
        """Names, units and options of settings, for the card lines of a new node."""
        def read():
            out = {}
            for key in keys:
                try:
                    target, _scope = self._registry._stack_for(key, 0)
                    described = self._registry._describe(key, target)
                except Exception:
                    continue
                out[key] = {"label": described["label"], "label_local": described["label_local"],
                            "unit": described["unit"], "options": described.get("options")}
            return out
        return run_on_main(self._app, read)

    def needs_slice(self):
        slices = self._registry._slices
        return (slices is None or slices.status()[0] != 3) and self._sliceable()

    def stats(self, should_stop=None):
        """Print time and material of the current slice, slicing first when the estimate is not current."""
        if not self._sliceable():
            return {}
        if self.needs_slice():
            result = self._registry.call("slice", {}, {"cancelled": should_stop} if should_stop else None)
        else:
            result = self._registry.call("get_slice_result", {}, None)
        if not result.get("ok"):
            return {}
        return dict((k, result["result"].get(k)) for k in ("print_time_s", "material_g", "material_m"))

    def _stacks(self):
        stack = self._registry._global()
        return stack, [stack] + [stack.extruders[p] for p in sorted(stack.extruders)]

    def _physics_preferences(self):
        preferences = self._app.getPreferences()
        values = dict((key, preferences.getValue("physics/" + key))
                      for key in ("automatic_drop_down", "automatic_push_free"))
        return values

    @staticmethod
    def _object_id(node):
        # A source path/name is not unique when a model has several copies.
        if not getattr(node, "_ai_assistant_tree_id", None):
            node._ai_assistant_tree_id = uuid.uuid4().hex
        return node._ai_assistant_tree_id

    def _mesh(self, mesh):
        """(id, local bounding box) of a mesh; the mesh is kept to restore it."""
        token = getattr(mesh, "_ai_assistant_tree_mesh", None)
        if token is None:
            token = uuid.uuid4().hex
            try:
                mesh._ai_assistant_tree_mesh = token
            except AttributeError:
                return None, None
        self._meshes[token] = mesh
        self._meshes.move_to_end(token)
        while len(self._meshes) > MESH_CACHE:
            self._boxes.pop(self._meshes.popitem(last=False)[0], None)
        if token not in self._boxes:
            import numpy
            vertices = mesh.getVertices()
            vertices = numpy.asarray(vertices, dtype=float) if vertices is not None else numpy.zeros((0, 3))
            self._boxes[token] = [vertices.min(axis=0).tolist(), vertices.max(axis=0).tolist()] \
                if vertices.size else None
        return token, self._boxes[token]

    def _saved_mesh(self, node, item):
        """(mesh to set or None, local shift or None) so the part takes the pose saved with its mesh.

        Only a mesh of the same size counts as the same part (the centred and the raw STL);
        a part reloaded with other geometry keeps it. The saved mesh comes back while this
        session keeps it; otherwise the matrix is shifted by the offset of the two meshes."""
        import numpy
        saved, current = item.get("mesh"), node.getMeshData()
        if not saved or current is None or getattr(current, "_ai_assistant_tree_mesh", None) == saved:
            return None, None
        box, now = item.get("mesh_box"), self._mesh(current)[1]
        if not box or not now:
            return None, None
        (lo, hi), (lo_now, hi_now) = numpy.asarray(box, dtype=float), numpy.asarray(now, dtype=float)
        if not numpy.allclose(hi - lo, hi_now - lo_now, atol=1e-3 * max(1.0, float((hi - lo).max()))):
            return None, None
        if saved in self._meshes:
            return self._meshes[saved], None
        return None, (lo + hi) / 2.0 - (lo_now + hi_now) / 2.0

    @staticmethod
    def _z_offset(node):
        return node.callDecoration("getZOffset")

    @staticmethod
    def _set_z_offset(node, value):
        from steslicer.Scene.ZOffsetDecorator import ZOffsetDecorator
        decorator = node.getDecorator(ZOffsetDecorator)
        if value is None:
            if decorator is not None:
                node.removeDecorator(ZOffsetDecorator)
        else:
            if decorator is None:
                decorator = ZOffsetDecorator()
                node.addDecorator(decorator)
            decorator.setZOffset(float(value))

    def _capture(self):
        registry = self._registry
        stack, stacks = self._stacks()
        saved = []
        for s in stacks:
            user = {}
            for key in s.userChanges.getAllKeys():
                user[key] = _stored(s.userChanges.getProperty(key, "value"))
            saved.append({"position": None if s is stack else s.getMetaDataEntry("position"),
                          "containers": [c.getId() for c in s.getContainers()[1:6]], "user": user})
        effective = {}
        for key in registry._all_keys():
            if key.startswith("machine_") or key == "printing_mode":
                continue
            try:
                target, _scope = registry._stack_for(key, 0)
                if not target.getProperty(key, "enabled"):
                    continue
                effective[key] = _plain(target.getProperty(key, "value"))
            except Exception:
                continue
        manager = self._app.getMachineManager()
        objects = []
        for node in registry._scene_nodes():
            source = registry._node_source(node)
            item = {"id": self._object_id(node), "name": node.getName(), "source": source.get("source_path"),
                    "parent": getattr(node.getParent(), "_ai_assistant_tree_id", None),
                    "matrix": node.getLocalTransformation().getData().tolist(),
                    "world_matrix": node.getWorldTransformation().getData().tolist(),
                    "z_offset": self._z_offset(node)}
            if node.getMeshData() is not None:
                item["mesh"], item["mesh_box"] = self._mesh(node.getMeshData())
            objects.append(item)
        mode = stack.getProperty("printing_mode", "value")
        physics = self._physics_preferences()
        if mode in ("cylindrical", "cylindrical_full"):
            # A 5D part is centred on the rotary axis; y=0 is not a floor.
            physics["automatic_drop_down"] = False
        return {"machine": stack.getId(), "machine_definition": stack.definition.getId(),
                "stacks": saved, "mode": mode,
                "preset": manager.activeQualityOrQualityChangesName,
                "materials": dict((s.getMetaDataEntry("position"), s.material.getName()) for s in stacks[1:]),
                "nozzles": dict((s.getMetaDataEntry("position"), s.variant.getName()) for s in stacks[1:]),
                "effective": effective, "objects": objects, "physics": physics}

    def _restore(self, snapshot, known=None):
        """Containers, values, parts, meshes and poses of a node as one Undo step.

        Returns how many saved parts were not found and how many were moved, brought back
        and taken off the plate."""
        app, registry = self._app, self._registry
        stack, stacks = self._stacks()
        if snapshot.get("machine") and snapshot["machine"] != stack.getId():
            raise ToolError("This model was made for another printer; select that printer in STE first")
        from UM.Math.Matrix import Matrix
        from UM.Math.Vector import Vector
        from UM.Operations.Operation import Operation
        from UM.Settings.InstanceContainer import InstanceContainer

        containers = app.getContainerRegistry()
        root = app.getController().getScene().getRoot()
        free = list(registry._scene_nodes())
        matched, missing = [], 0  # (item, node, None or the parent it is brought back to)
        placed = {}  # saved part id -> its node
        for item in snapshot.get("objects") or []:
            node = next((n for n in free if item.get("id") and
                         getattr(n, "_ai_assistant_tree_id", None) == item["id"]), None)
            if node is None and item.get("id") in self._detached:
                node = self._detached[item["id"]]  # taken off by another node of the tree
                if node.getParent() is not None or node in placed.values():
                    node = None
            if node is None:
                # Old snapshots/reopened scenes have no live IDs. Never guess between copies.
                candidates = [n for n in free if n.getName() == item.get("name") and
                              registry._node_source(n).get("source_path") == item.get("source")]
                if len(candidates) > 1:
                    raise ToolError("Cannot identify a saved part uniquely; several parts have the same source and name")
                node = candidates[0] if candidates else None
            if node is None:
                missing += 1
                continue
            back = None
            if node in free:
                free.remove(node)
                if item.get("id") and not getattr(node, "_ai_assistant_tree_id", None):
                    node._ai_assistant_tree_id = item["id"]  # found by name after a restart: the tree knows it again
            else:
                back = placed.get(item.get("parent")) or root
            if item.get("id"):
                placed[item["id"]] = node
            matched.append((item, node, back))
        if all(back is not None for _item, _node, back in matched):
            # Nothing of the node is on the plate (a new project): no copies come back without their part.
            missing += len(matched)
            matched = []

        def ancestors(node):
            parent = node.getParent()
            while parent is not None:
                yield parent
                parent = parent.getParent()

        # Parts of this tree the node has not (batch copies, modifiers of a later plan) leave the plate;
        # a modifier goes with its part. Parts added by the user after the last model stay.
        known = set(known or ())
        leaving = [n for n in free if getattr(n, "_ai_assistant_tree_id", None) in known]
        leaving = [(n, n.getParent()) for n in leaving if not any(p in leaving for p in ancestors(n))]
        moves, comings, shifts = [], [], {}
        for item, node, back in matched:
            parent = back or node.getParent()
            if "world_matrix" not in item or parent in placed.values():
                after = Matrix(item["matrix"])  # its parent takes its saved pose too
                if parent in shifts:  # the parent keeps another mesh: its frame moved by the offset
                    after = shifts[parent].getInverse().multiply(after, copy=True)
            else:
                after = Matrix(item["world_matrix"])
                if parent is not None:
                    after = parent.getWorldTransformation().getInverse().multiply(after, copy=True)
            mesh, shift = self._saved_mesh(node, item)
            if shift is not None:
                shifts[node] = Matrix()
                shifts[node].setByTranslation(Vector(*shift.tolist()))
                after = after.multiply(shifts[node], copy=True)
            # Legacy snapshots did not store ZOffset. Clear any offset from a different node.
            moves.append((node, (node.getLocalTransformation(), self._z_offset(node), node.getMeshData()),
                          (after, item.get("z_offset"), mesh)))
            if back is not None:
                comings.append((node, back))

        before_physics = self._physics_preferences()
        after_physics = dict(before_physics, **(snapshot.get("physics") or {}))
        if snapshot.get("mode") in ("cylindrical", "cylindrical_full"):
            after_physics["automatic_drop_down"] = False
        elif "physics" not in snapshot and snapshot.get("mode") == "classic":
            after_physics["automatic_drop_down"] = True
        by_position = dict((str(x.get("position")), x) for x in snapshot.get("stacks") or [])
        plans = []
        for s in stacks:
            saved = by_position.get(str(None if s is stack else s.getMetaDataEntry("position")))
            if saved is None:
                continue
            before = list(s.getContainers())
            after = list(before)
            for index, container_id in enumerate(saved.get("containers") or [], 1):
                found = containers.findInstanceContainers(id=container_id)
                if not found:
                    raise ToolError("The preset of this model is no longer installed (%s)" % container_id)
                after[index] = found[0]
            user = InstanceContainer("ai_assistant_user_" + uuid.uuid4().hex)
            user.setMetaDataEntry("type", "user")
            user.setMetaDataEntry("setting_version", app.SettingVersion)
            user.setDefinition(stack.definition.getId())
            containers.addContainer(user)
            for key, value in (saved.get("user") or {}).items():
                user.setProperty(key, "value", _loaded(value))
            if s is stack and snapshot.get("mode") is not None:
                user.setProperty("printing_mode", "value", snapshot["mode"])
            after[0] = user
            plans.append((s, before, after))

        state_io = self
        class RestoreModel(Operation):
            def __init__(self):
                Operation.__init__(self)
                self.active = False

            def _apply(self, index):
                physics = getattr(app, "_physics", None)
                enabled = physics._enabled if physics is not None else None
                timer = getattr(physics, "_change_timer", None)
                if physics is not None:
                    physics._enabled = False
                if timer is not None:
                    timer.stop()
                try:
                    for s, before, after in plans:
                        for position, container in enumerate((before, after)[index][:-1]):
                            s.replaceContainer(position, container)
                    # Complete the mode switch before applying coordinates/physics of that mode.
                    stack.propertyChanged.emit("printing_mode", "value")
                    handler = getattr(app.getBackendManager(), "_onSettingChanged", None)
                    if callable(handler):
                        handler("printing_mode", "value")
                    for key, value in (before_physics, after_physics)[index].items():
                        app.getPreferences().setValue("physics/" + key, value)
                    state_io._move_parts(leaving, comings, index)
                    for node, before, after in moves:
                        matrix, z_offset, mesh = (before, after)[index]
                        if mesh is not None and mesh is not node.getMeshData():
                            node.setMeshData(mesh)
                        state_io._set_z_offset(node, z_offset)
                        node.setTransformation(matrix)
                    scene = app.getController().getScene()
                    scene.sceneChanged.emit(scene.getRoot())
                finally:
                    # Discard ticks scheduled for the intermediate scene, including an old pending drop.
                    if timer is not None:
                        timer.stop()
                    if physics is not None:
                        physics._enabled = enabled

            def redo(self):
                if not self.active:
                    try:
                        self._apply(1)
                    except Exception:
                        self._apply(0)  # never leave the project half switched
                        raise
                    self.active = True

            def undo(self):
                if self.active:
                    self._apply(0)
                    self.active = False

        operation = RestoreModel()
        operation.redo()  # Failed restoration must not enter the undo stack.
        app.getOperationStack().push(operation)
        # Reverting a plan now would undo it over another model's setup; the tree is the way back.
        applied = getattr(getattr(registry, "_in_place", None), "_applied", None)
        if isinstance(applied, dict):
            applied.clear()
        return {"missing_objects": missing, "moved": len(moves), "returned": len(comings), "removed": len(leaving)}

    def _move_parts(self, leaving, comings, index):
        """index 1: parts leave and come back for the node; 0: undone."""
        try:
            from UM.Scene.Selection import Selection
        except ImportError:
            Selection = None
        for node, parent in (leaving if index else comings):
            if Selection is not None and Selection.isSelected(node):
                Selection.remove(node)
            node.setParent(None)
            if getattr(node, "_ai_assistant_tree_id", None):
                self._detached[node._ai_assistant_tree_id] = node
        for node, parent in (comings if index else leaving):
            node.setParent(parent)
