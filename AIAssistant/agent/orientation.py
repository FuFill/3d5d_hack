# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Orientation check: every part on the plate is put into the pose that prints best.

One core plan of the part in its current pose (plan_print keep_current) gives the current
pose's assessment, the core's best pose and its top candidates. Stiffness of a pose comes
from the core's FE model when planning inputs (loads, fixtures, constants) cover the part
(compliance, lower is stiffer); otherwise from the core's analytical screen (share of the
load across the layers). Nothing is invented: without loads stiffness is not judged.

Print time and material of each pose come from the pre-slice estimate (estimate.py): the
open project is not touched while poses are compared, only the chosen pose is applied.

Rules (GAIN = 5 %):
- a pose more than GAIN less stiff than the current one is never taken;
- the core's best pose is taken when it saves at least GAIN of print time or material and
  costs at most GAIN of the other;
- a pose at least GAIN stiffer is taken when time and material get at most GAIN worse;
- when the user asked for strength the stiffest pose is taken;
- a pose the user set is kept; then only the possible gain is reported.
Several parts are checked one by one and arranged when they collide or leave the plate.
5D parts follow the tooling axis. Only the pose changes (one Undo step per part).
"""

import math
import re
import sys

from . import estimate as E
from .mainthread import run_on_main

GAIN = 0.05
SAME_UP_DEG = 2.0
# The user keeps the pose: "не поворачивай", "оставь положение", "keep the orientation".
KEEP_POSE = re.compile(r"не\s+(?:поворачивай|вращай|переворачивай|трогай\s+(?:положение|позу|ориентацию))|"
                       r"оставь\w*\s+(?:положение|позу|ориентацию|как\s+(?:есть|стоит|лежит))|"
                       r"keep\s+(?:the\s+)?(?:pose|orientation|rotation)|do\s*n[o']t\s+rotate", re.I)
PRINTER_TO_SCENE = ((1., 0., 0.), (0., 0., 1.), (0., -1., 0.))


def wants_own_pose(text):
    return bool(KEEP_POSE.search(text or ""))


def _geometry():
    from .core_worker import core_path
    if core_path() not in sys.path:
        sys.path.insert(0, core_path())
    from ste_planner import geometry
    return geometry


def _angle(a, b):
    dot = sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _gain(before, after, key):
    old, new = before.get(key), after.get(key)
    if not old or new is None:
        return None
    return (float(old) - float(new)) / float(old)


def slicing_ok(before, after):
    """Time or material saves GAIN while the other gets at most GAIN worse."""
    gains = [g for g in (_gain(before, after, "print_time_s"), _gain(before, after, "material_g")) if g is not None]
    return bool(gains) and max(gains) >= GAIN and min(gains) > -GAIN


def not_worse(before, after):
    gains = [g for g in (_gain(before, after, "print_time_s"), _gain(before, after, "material_g")) if g is not None]
    return all(g > -GAIN for g in gains)


def _sum_compliance(candidate):
    cases = ((candidate or {}).get("mechanics") or {}).get("cases") or []
    return sum(float(c["compliance_Nmm"]) for c in cases) if cases else None


def stiffness_options(decision):
    """Current pose and its alternatives with their stiffness gain (positive = stiffer), from FE or the screen.

    Returns (basis, note, current_up, [{"name", "rotation" or "up", "stiffness_gain"}]).
    """
    orientation = decision.get("orientation") or {}
    current_up = orientation.get("up")
    functional = decision.get("functional_analysis") or {}
    value = functional.get("value") or {}
    baseline = value.get("baseline") or {}
    base_compliance = _sum_compliance(baseline)
    options = []
    if base_compliance:
        same = [c for c in value.get("candidates") or [] if c.get("profile_id") == baseline.get("profile_id")
                and not c.get("rejected_reasons") and _sum_compliance(c)]
        for candidate in same:
            options.append({"rotation": candidate["rotation"], "up": candidate["rotation"][2],
                            "stiffness_gain": (base_compliance - _sum_compliance(candidate)) / base_compliance})
        return "fe", "", current_up, options
    trace = functional.get("trace") or [{}]
    note = trace[0].get("reason") or "planning inputs with loads, fixtures and constants are not set for this part"
    step = next((t for t in decision.get("trace") or [] if t.get("step") == "orientation"), {})
    current = orientation.get("interlayer_exposure")
    for candidate in step.get("candidates") or []:
        exposure = candidate.get("interlayer_exposure")
        gain = (current - exposure) if current is not None and exposure is not None else None
        options.append({"up": candidate["up"], "stiffness_gain": gain, "score": candidate.get("score")})
    basis = "screen" if current is not None else "none"
    return basis, note, current_up, options


def _match(options, up):
    return next((o for o in options if o.get("up") is not None and _angle(o["up"], up) < SAME_UP_DEG), None)


class OrientationCheck:
    def __init__(self, registry):
        self._r = registry

    def run(self, args, ctx):
        r = self._r
        app = r._app
        if run_on_main(app, r._mode)["mode"] != "classic":
            return {"status": "skipped", "reason": "5D: the part axis follows the tooling axis"}
        parts = run_on_main(app, lambda: [(r._node_id(n), n.getName(), r._node_source(n)) for n in r._scene_nodes()])
        if not parts:
            return {"status": "skipped", "reason": "no parts on the plate"}
        keep = bool(args.get("keep_pose"))
        priority = "strength" if args.get("priority") == "strength" else "balanced"
        state = r.call("get_state", {}, ctx).get("result") or {}
        settings = dict(state.get("key_settings") or {})
        material = ((state.get("extruders") or [{}])[0] or {}).get("material")
        results, turned = [], 0
        for object_id, name, source in parts:
            if (ctx or {}).get("cancelled", lambda: False)():
                break
            item = {"object_id": object_id, "object": name}
            results.append(item)
            if not source.get("can_plan_original_stl"):
                item.update(status="skipped", reason="no original STL (OBJ/3MF or an edited mesh)")
                continue
            try:
                current = run_on_main(app, lambda: r._printer_rotation(r._node(object_id)))
            except Exception as e:  # scaled/mirrored or an unexpected scene object: the part keeps its pose
                item.update(status="skipped", reason=str(e))
                continue
            decision = self._plan(object_id, args, ctx)
            if decision is None:
                item.update(status="skipped", reason="the core could not evaluate this part")
                continue
            basis, note, _up, options = stiffness_options(decision)
            item.update(stiffness_basis=basis, stiffness_note=note)
            proposals = self._proposals(decision, options, basis, priority, current)
            if not proposals:
                item.update(status="optimal")
                continue
            path = source["source_path"]
            before = E.estimate(path, settings, E.pose(path, current, settings), material)
            chosen = None
            for proposal in proposals:
                after = E.estimate(path, settings, E.pose(path, proposal["rotation"], settings), material)
                proposal.update(time_gain=_gain(before, after, "print_time_s"),
                                material_gain=_gain(before, after, "material_g"))
                gain = proposal.get("stiffness_gain")
                if proposal["why"] == "strength":
                    accept = gain is None or gain > 0
                elif proposal["why"] == "best":
                    accept = slicing_ok(before, after) or (gain is not None and gain >= GAIN and not_worse(before, after))
                else:  # a stiffer pose
                    accept = not_worse(before, after)
                if accept:
                    chosen = proposal
                    break
            if chosen is None:
                item.update(status="kept_small_gain", tried=[_public(p) for p in proposals])
                continue
            item.update(_public(chosen), time_before_s=before.get("print_time_s"),
                        material_before_g=before.get("material_g"))
            if keep:
                item["status"] = "kept_user_pose"
            else:
                run_on_main(app, lambda: self._turn(object_id, chosen["rotation"]))
                item["status"] = "rotated_for_strength" if chosen["why"] == "strength" else "rotated"
                turned += 1
        if turned and len(parts) > 1 and run_on_main(app, self._crowded):
            arranged = r.call("arrange_all", {}, ctx)
            results.append({"arranged": bool(arranged.get("ok"))})
        return {"status": "done", "parts": results,
                "estimate": "time and material are pre-slice estimates (about +-20 %); the slicer's numbers come "
                            "after slicing",
                "rule": "a pose >5 % less stiff is never taken; the core's best pose is taken when it saves >= 5 % time "
                        "or material (other <= 5 % worse); a pose >= 5 % stiffer when time and material get <= 5 % "
                        "worse; strength requests take the stiffest pose; a pose set by the user is kept"}

    def _plan(self, object_id, args, ctx):
        """Core plan of the part in its current pose: its assessment, best pose, candidates, FE if inputs exist."""
        request = {"object_id": object_id, "mode": "classic", "orientation": "keep_current",
                   "task_text": args.get("task_text") or "Print preparation of this part."}
        if args.get("spec_attachment_id"):
            request["spec_attachment_id"] = args["spec_attachment_id"]
        with self._r.core.side_plans():  # the model's current plan stays valid
            outcome = self._r.call("plan_print", request, ctx)
            if not outcome.get("ok") or not outcome["result"].get("plan_id"):
                return None
            return self._r.core.decision_record(outcome["result"]["plan_id"])["decision"]

    def _proposals(self, decision, options, basis, priority, current):
        """Poses worth slicing, in order: the strength pose, or the core's best then a stiffer one."""
        current_up = list(current[2])
        out = []

        def rotation_of(option):
            return option.get("rotation") or _geometry().rotation_to_z(option["up"]).tolist()

        def new(option):
            return option.get("up") is not None and _angle(option["up"], current_up) >= SAME_UP_DEG

        if priority == "strength":
            known = [o for o in options if o.get("stiffness_gain") is not None and new(o)]
            if known:
                best = max(known, key=lambda o: o["stiffness_gain"])
                if best["stiffness_gain"] > 0:
                    out.append(dict(best, rotation=rotation_of(best), why="strength"))
            return out
        auto = (decision.get("orientation") or {}).get("auto_best") or {}
        if auto.get("up") is not None and _angle(auto["up"], current_up) >= SAME_UP_DEG:
            matched = _match(options, auto["up"]) or {}
            gain = matched.get("stiffness_gain")
            if gain is None and basis == "screen" and auto.get("interlayer_exposure") is not None:
                current_exposure = (decision.get("orientation") or {}).get("interlayer_exposure")
                gain = current_exposure - auto["interlayer_exposure"]
            if gain is None or gain >= -GAIN:  # never a pose much less stiff
                out.append({"up": auto["up"], "rotation": rotation_of(matched or {"up": auto["up"]}),
                            "stiffness_gain": gain, "why": "best"})
        stiffer = [o for o in options if o.get("stiffness_gain") is not None and o["stiffness_gain"] >= GAIN and new(o)
                   and not (out and _angle(o["up"], out[0]["up"]) < SAME_UP_DEG)]
        if stiffer:
            best = max(stiffer, key=lambda o: o["stiffness_gain"])
            out.append(dict(best, rotation=rotation_of(best), why="stiffer"))
        return out

    def _turn(self, object_id, rotation):
        """Put the part into `rotation` (printer frame) on its place on the plate, as one Undo step."""
        import numpy
        from UM.Math.Matrix import Matrix
        from UM.Operations.Operation import Operation

        node = self._r._node(object_id)
        p = numpy.array(PRINTER_TO_SCENE)
        scene_rotation = p.dot(numpy.asarray(rotation, dtype=float)).dot(p.T)
        vertices = numpy.asarray(node.getMeshData().getVertices(), dtype=float).dot(scene_rotation.T)
        box = node.getBoundingBox()
        low, high = vertices.min(axis=0), vertices.max(axis=0)
        matrix = numpy.eye(4)
        matrix[:3, :3] = scene_rotation
        matrix[:3, 3] = [box.center.x - (low[0] + high[0]) / 2.0, -low[1], box.center.z - (low[2] + high[2]) / 2.0]
        old, new = node.getLocalTransformation(), Matrix(matrix)

        class TurnPart(Operation):
            def redo(self):
                node.setTransformation(new)

            def undo(self):
                node.setTransformation(old)

        self._r._app.getOperationStack().push(TurnPart())

    def _crowded(self):
        nodes = self._r._scene_nodes()
        if any(getattr(n, "isOutsideBuildArea", None) and n.isOutsideBuildArea() for n in nodes):
            return True
        hulls = [n.callDecoration("getConvexHull") for n in nodes]
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                if hulls[i] is not None and hulls[j] is not None and hulls[i].intersectsPolygon(hulls[j]) is not None:
                    return True
        return False


def _public(proposal):
    return dict((k, proposal.get(k)) for k in ("why", "stiffness_gain", "time_gain", "material_gain", "up"))


def summary_text(result, language="ru"):
    """One line for the chat about what the check did, or "" when nothing is worth saying."""
    ru = language == "ru"
    lines = []
    for item in (result or {}).get("parts") or []:
        status = item.get("status")
        if status not in ("rotated", "rotated_for_strength", "kept_user_pose"):
            continue
        gains = []
        for key, word in (("time_gain", "время" if ru else "time"), ("material_gain", "пластик" if ru else "material")):
            value = item.get(key)
            if value is not None and abs(value) >= 0.005:
                gains.append("%s %s%d %%" % (word, "−" if value > 0 else "+", round(abs(value) * 100)))
        stiffness = item.get("stiffness_gain")
        if stiffness is not None and abs(stiffness) >= 0.005:
            if item.get("stiffness_basis") == "fe":
                gains.append(("жёсткость %s%d %% (FE)" if ru else "stiffness %s%d %% (FE)") %
                             ("+" if stiffness > 0 else "−", round(abs(stiffness) * 100)))
            else:
                gains.append(("нагрузка поперёк слоёв %s%d п.п. (оценка ядра, FE не считался)" if ru else
                              "load across the layers %s%d pp (core screen, no FE)") %
                             ("−" if stiffness > 0 else "+", round(abs(stiffness) * 100)))
        detail = ", ".join(gains)
        name = item.get("object") or ""
        if status == "kept_user_pose":
            if detail:
                lines.append(("Положение «%s» оставил как задано; лучшая поза дала бы: %s." if ru else
                              "Kept the pose of \"%s\" as set; the best pose would give: %s.") % (name, detail))
        elif status == "rotated_for_strength":
            lines.append(("Деталь «%s» повёрнута для прочности: нагрузка вдоль слоёв%s." if ru else
                          "Turned \"%s\" for strength: loads along the layers%s.") % (name, (" (" + detail + ")") if detail else ""))
        else:
            lines.append(("Деталь «%s» повёрнута в лучшую позу: %s." if ru else
                          "Turned \"%s\" into the best pose: %s.") % (name, detail or ("заметно выгоднее" if ru else "better")))
    return " ".join(lines)
