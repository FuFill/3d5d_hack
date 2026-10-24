# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Model tree of a chat: every finished piece of the agent's work is a node.

A node ("model") is a snapshot of the print setup: the containers of every stack (quality
changes, quality, material, variant, definition changes) with the user's values on top,
the printing mode and the pose of every part on the plate. The tree belongs to one chat;
its selected node is restored when the chat is opened again. Nodes are made only when a
turn of the agent ends with a result, never for intermediate states.
"""

import re
import time
import uuid

from .mainthread import run_on_main
from .tools import MODE_NAMES, ToolError, _plain

# What a node card compares first; the rest of the user's changes follow in key order.
PRIORITY = ("printing_mode", "preset", "material", "nozzle", "objects")
EFFECTIVE_KEYS = ("layer_height", "infill_sparse_density", "wall_line_count", "speed_print", "support_enable",
                  "support_type", "adhesion_type", "top_layers", "bottom_layers", "infill_pattern",
                  "material_print_temperature", "material_bed_temperature", "line_width", "cool_fan_speed",
                  "retraction_enable")
MAX_CHANGES = 5
NAMES = {"printing_mode": ("Режим печати", "Printing mode"), "preset": ("Пресет", "Preset"),
         "material": ("Материал", "Material"), "nozzle": ("Сопло", "Nozzle"),
         "objects": ("Положение деталей", "Part placement"), "count": ("Деталей на столе", "Parts on the plate")}
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
    return str(value)


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


def _poses(snapshot):
    return [(o.get("name"), o.get("source"), [[round(float(v), 3) for v in row] for row in o.get("matrix") or []])
            for o in snapshot.get("objects") or []]


def same_state(a, b):
    """True when two snapshots describe the same setup (containers, values, mode, poses)."""
    if not a or not b:
        return False
    key = lambda s: sorted(((str(x.get("position")), x.get("containers"), sorted((x.get("user") or {}).items(),
                                                                              key=lambda i: i[0]))
                            for x in s.get("stacks") or []), key=lambda i: i[0])
    return key(a) == key(b) and a.get("mode") == b.get("mode") and _poses(a) == _poses(b)


def changes(old, new, language="ru"):
    """The most significant differences of `new` against `old`, at most MAX_CHANGES, for the node card."""
    index = 0 if language == "ru" else 1
    rows = []

    def add(key, before, after, label=None):
        rows.append({"key": key, "label": label or NAMES[key][index], "old": before, "new": after})

    if old is None:
        return []
    if old.get("mode") != new.get("mode"):
        add("printing_mode", MODE_NAMES.get(old.get("mode"), old.get("mode") or "—"),
            MODE_NAMES.get(new.get("mode"), new.get("mode") or "—"))
    if old.get("preset") != new.get("preset"):
        add("preset", old.get("preset") or "—", new.get("preset") or "—")
    for key in ("material", "nozzle"):
        before, after = old.get(key + "s") or {}, new.get(key + "s") or {}
        for position in sorted(set(before) | set(after)):
            if before.get(position) != after.get(position):
                add(key, before.get(position) or "—", after.get(position) or "—")
    if len(_poses(old)) != len(_poses(new)):
        add("count", str(len(_poses(old))), str(len(_poses(new))))
    elif _poses(old) != _poses(new):
        rotated = any(a[2][:3] and b[2][:3] and [r[:3] for r in a[2][:3]] != [r[:3] for r in b[2][:3]]
                      for a, b in zip(_poses(old), _poses(new)))
        word = ("повёрнута" if rotated else "сдвинута") if index == 0 else ("rotated" if rotated else "moved")
        add("objects", "", word)
    labels = dict(old.get("labels") or {}, **(new.get("labels") or {}))
    before, after = old.get("effective") or {}, new.get("effective") or {}
    keys = [k for k in EFFECTIVE_KEYS if k in before or k in after]
    keys += sorted(k for k in set(before) | set(after) if k not in keys)
    for key in keys:
        # A key read for one side only (a user value of that node) is not a change by itself.
        if key in before and key in after and before[key] != after[key]:
            info = labels.get(key) or {}
            label = (info.get("label_local") if index == 0 else info.get("label")) or info.get("label") or key
            rows.append({"key": key, "label": label, "old": _value_text(before.get(key), info, language),
                         "new": _value_text(after.get(key), info, language)})
    return rows[:MAX_CHANGES]


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

    def current(self):
        return self.node(self.selected)

    def add(self, snapshot, name, stats=None, kind="model"):
        """A new node under the selected one; it becomes the selected node."""
        node = {"id": uuid.uuid4().hex[:10], "parent": self.selected, "kind": kind, "name": name,
                "short": abbreviation(name), "created": time.time(), "snapshot": snapshot, "stats": stats or {}}
        self.nodes.append(node)
        self.selected = node["id"]
        return node

    def view(self, language="ru"):
        """What the tree sheet and the node card show; snapshots stay on the server."""
        out = []
        for node in self.nodes:
            parent = self.node(node.get("parent"))
            stats = node.get("stats") or {}
            effective = (node.get("snapshot") or {}).get("effective") or {}
            infill = stats.get("infill", effective.get("infill_sparse_density"))
            parts = [duration_text(stats.get("print_time_s"), language)]
            if stats.get("material_g"):
                parts.append(("%s г" if language == "ru" else "%s g") % _number(stats["material_g"]))
            if infill is not None:
                parts.append(("заполнение %s %%" if language == "ru" else "infill %s %%") % _number(infill))
            empty = (START_NOTE if node.get("kind") == "start" else SAME_NOTE)[0 if language == "ru" else 1]
            out.append({"id": node["id"], "parent": node.get("parent"), "kind": node.get("kind", "model"), "empty": empty,
                        "name": node.get("name") or "", "short": node.get("short") or abbreviation(node.get("name")),
                        "created": node.get("created", 0), "stats": " · ".join(p for p in parts if p),
                        "changes": changes(parent.get("snapshot") if parent else None, node.get("snapshot") or {},
                                           language),
                        "selected": node["id"] == self.selected})
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

    def capture(self):
        return run_on_main(self._app, self._capture)

    def restore(self, snapshot):
        return run_on_main(self._app, lambda: self._restore(snapshot))

    def _sliceable(self):
        return bool(run_on_main(self._app, self._registry._scene_nodes))

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
        effective, labels = {}, {}
        keys = list(EFFECTIVE_KEYS) + sorted(set(k for x in saved for k in x["user"]) - set(EFFECTIVE_KEYS)
                                             - {"printing_mode"})
        for key in keys:
            try:
                target, _scope = registry._stack_for(key, 0)
                described = registry._describe(key, target)
            except Exception:
                continue
            effective[key] = described["value"]
            labels[key] = {"label": described["label"], "label_local": described["label_local"],
                           "unit": described["unit"], "options": described.get("options")}
        manager = self._app.getMachineManager()
        objects = []
        for node in registry._scene_nodes():
            source = registry._node_source(node)
            objects.append({"name": node.getName(), "source": source.get("source_path"),
                            "matrix": node.getLocalTransformation().getData().tolist()})
        return {"machine": stack.getId(), "machine_definition": stack.definition.getId(),
                "stacks": saved, "mode": stack.getProperty("printing_mode", "value"),
                "preset": manager.activeQualityOrQualityChangesName,
                "materials": dict((s.getMetaDataEntry("position"), s.material.getName()) for s in stacks[1:]),
                "nozzles": dict((s.getMetaDataEntry("position"), s.variant.getName()) for s in stacks[1:]),
                "effective": effective, "labels": labels, "objects": objects}

    def _restore(self, snapshot):
        """Containers, values and poses of a node as one Undo step; returns how many parts were not found."""
        app, registry = self._app, self._registry
        stack, stacks = self._stacks()
        if snapshot.get("machine") and snapshot["machine"] != stack.getId():
            raise ToolError("This model was made for another printer; select that printer in STE first")
        from UM.Math.Matrix import Matrix
        from UM.Operations.Operation import Operation
        from UM.Settings.InstanceContainer import InstanceContainer

        containers = app.getContainerRegistry()
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
            after[0] = user
            plans.append((s, before, after))

        free = list(registry._scene_nodes())
        moves, missing = [], 0
        for item in snapshot.get("objects") or []:
            def same(node, by_source):
                return node.getName() == item.get("name") and (
                    not by_source or registry._node_source(node).get("source_path") == item.get("source"))
            node = next((n for n in free if same(n, True)), None) or next((n for n in free if same(n, False)), None)
            if node is None:
                missing += 1
                continue
            free.remove(node)
            moves.append((node, node.getLocalTransformation(), Matrix(item["matrix"])))

        class RestoreModel(Operation):
            def __init__(self):
                Operation.__init__(self)
                self.active = False

            def _apply(self, index):
                for s, before, after in plans:
                    for position, container in enumerate((before, after)[index][:-1]):
                        s.replaceContainer(position, container)
                for node, before, after in moves:
                    node.setTransformation((before, after)[index])
                stack.propertyChanged.emit("printing_mode", "value")
                handler = getattr(app.getBackendManager(), "_onSettingChanged", None)
                if callable(handler):
                    handler("printing_mode", "value")
                scene = app.getController().getScene()
                scene.sceneChanged.emit(scene.getRoot())

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

        app.getOperationStack().push(RestoreModel())
        return {"missing_objects": missing, "moved": len(moves)}
