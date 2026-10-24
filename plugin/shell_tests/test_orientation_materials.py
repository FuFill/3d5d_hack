"""Pose check (stiffness from FE or the core's screen, time/material from slicing) and material comparison."""

from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "AIAssistant"))

import pytest

from agent import orientation as O
from agent.execution import PlanExecution
from agent.tools import ToolError
from agent.variants import MaterialComparison, _excluded

IDENTITY = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
SIDE = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]   # the part's +Y ends up along Z (part on its side)


class Stack:
    def __init__(self):
        self.done = []

    def undo(self):
        self.done.append("undo")


class Registry:
    """The parts of ToolRegistry the pose check and the material comparison use."""

    def __init__(self, decision, stats, mode="classic"):
        self.decision, self.stats, self.mode = decision, stats, mode
        self.pose = "current"
        self.stack = Stack()
        self._app = SimpleNamespace(getOperationStack=lambda: self.stack)
        self._slices = SimpleNamespace(status=lambda: (1, 0))
        self.core = SimpleNamespace(decision_record=lambda plan_id: {"decision": self.decision})
        self.calls = []
        self.questions = None
        self.language = lambda: "ru"

    def _mode(self):
        return {"mode": self.mode}

    def _scene_nodes(self):
        return [SimpleNamespace(getName=lambda: "gear")]

    def _node_id(self, node):
        return 1

    def _node_source(self, node):
        return {"can_plan_original_stl": True, "source_path": "/m/gear.stl"}

    def _printer_rotation(self, node):
        return IDENTITY

    def _node(self, object_id):
        return "gear"

    def call(self, name, args, ctx=None):
        self.calls.append((name, args))
        if name == "plan_print":
            return {"ok": True, "result": {"plan_id": "p1"}}
        if name in ("slice", "get_slice_result"):
            return {"ok": True, "result": dict(self.stats[self.pose])}
        return {"ok": True, "result": {}}


def check(registry, **args):
    run = O.OrientationCheck(registry)

    def turn(object_id, rotation):
        registry.pose = "side" if rotation[2][1] > 0.5 else "other"
    run._turn = turn
    original_undo = registry.stack.undo

    def undo():
        original_undo()
        registry.pose = "current"
    registry.stack.undo = undo
    return run.run(args, {})


def screen(current_exposure, best_exposure, best_up=(0, 1, 0)):
    """A plan of the current pose with the core's load screen only (no FE inputs)."""
    return {"orientation": {"up": [0, 0, 1], "interlayer_exposure": current_exposure,
                            "auto_best": {"up": list(best_up), "interlayer_exposure": best_exposure}},
            "trace": [{"step": "orientation", "candidates": [
                {"up": list(best_up), "interlayer_exposure": best_exposure, "score": 1.0},
                {"up": [0, 0, 1], "interlayer_exposure": current_exposure, "score": 2.0}]}],
            "functional_analysis": {"status": "needs_input", "value": None,
                                    "trace": [{"reason": "purpose, service loads and scoped printed properties required"}]}}


def fe(current_compliance, side_compliance):
    """A plan with the core's FE model: compliance of the current pose and of the part on its side."""
    case = lambda c: {"mechanics": {"cases": [{"compliance_Nmm": c}]}}
    baseline = dict(case(current_compliance), profile_id="base", rotation=IDENTITY)
    side = dict(case(side_compliance), profile_id="base", rotation=SIDE, rejected_reasons=[])
    decision = screen(0.6, 0.6)
    decision["functional_analysis"] = {"status": "computed", "value": {"baseline": baseline, "candidates": [side]}}
    return decision


def test_a_better_pose_by_time_or_material_is_taken():
    r = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3000, "material_g": 30}})
    result = check(r)
    part = result["parts"][0]
    assert part["status"] == "rotated" and round(part["time_gain"], 3) == 0.167 and r.pose == "side"
    assert r.calls[0] == ("plan_print", {"object_id": 1, "mode": "classic", "orientation": "keep_current",
                                         "task_text": "Print preparation of this part."})


def test_a_small_gain_keeps_the_pose():
    r = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3500, "material_g": 29.5}})
    assert check(r)["parts"][0]["status"] == "kept_small_gain" and r.pose == "current"


def test_a_much_softer_pose_is_never_taken_even_if_faster():
    r = Registry(screen(0.1, 0.5), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 2000, "material_g": 20}})
    part = check(r)["parts"][0]
    assert part["status"] == "optimal" and r.pose == "current"      # not even sliced


def test_a_stiffer_pose_is_taken_when_time_and_material_barely_change():
    r = Registry(fe(10.0, 6.0), {"current": {"print_time_s": 3600, "material_g": 30},
                                 "side": {"print_time_s": 3700, "material_g": 30.5}})
    r.decision["orientation"]["auto_best"]["up"] = [0, 0, 1]       # the geometric best is the current pose
    result = check(r)
    part = result["parts"][0]
    assert part["status"] == "rotated" and part["why"] == "stiffer" and part["stiffness_basis"] == "fe"
    assert round(part["stiffness_gain"], 2) == 0.4
    assert "жёсткость +40 % (FE)" in O.summary_text(result)


def test_strength_takes_the_stiffest_pose_even_if_slower():
    r = Registry(fe(10.0, 6.0), {"current": {"print_time_s": 3600, "material_g": 30},
                                 "side": {"print_time_s": 5400, "material_g": 40}})
    part = check(r, priority="strength")["parts"][0]
    assert part["status"] == "rotated_for_strength" and r.pose == "side"


def test_a_pose_the_user_set_is_kept_and_the_gain_reported():
    r = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3000, "material_g": 30}})
    result = check(r, keep_pose=True)
    assert result["parts"][0]["status"] == "kept_user_pose" and r.pose == "current"
    assert O.summary_text(result) == "Положение «gear» оставил как задано; лучшая поза дала бы: время −17 %."


def test_the_screen_is_named_when_fe_has_no_inputs():
    r = Registry(screen(0.6, 0.1), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3600, "material_g": 30}})
    result = check(r)
    part = result["parts"][0]
    assert part["stiffness_basis"] == "screen" and "service loads" in part["stiffness_note"]
    assert "оценка ядра, FE не считался" in O.summary_text(result)


def test_5d_and_user_words_for_the_pose():
    assert check(Registry(screen(0.3, 0.3), {}, mode="cylindrical_full"))["status"] == "skipped"
    assert O.wants_own_pose("Сделай прочнее, но не поворачивай деталь")
    assert O.wants_own_pose("оставь положение как есть") and not O.wants_own_pose("сделай быстрее")


# ---------------------------------------------------------------- materials

class Questions:
    def __init__(self, choice):
        self.choice, self.asked = choice, []

    def ask(self, questions, should_stop=None):
        self.asked.append(questions)
        option = questions[0]["options"][self.choice] if self.choice is not None else "TPU"
        return [{"choice": self.choice, "text": option}], 7


class Materials(Registry):
    STATS = {"enduse_petg": (7200, 31), "generic_pla": (6000, 28), "enduse_abs": (7800, 30), "enduse_pp": (9000, 26)}

    def __init__(self, choice=0):
        Registry.__init__(self, {}, {})
        self.questions = Questions(choice)
        self.plans, self.applied = {}, []
        self.current = None

    def call(self, name, args, ctx=None):
        self.calls.append((name, args))
        if name == "plan_print":
            material = args.get("preferred_material") or "enduse_petg"
            plan_id = "p%d" % (len(self.plans) + 1)
            self.plans[plan_id] = material
            options = [{"id": m} for m in ("enduse_petg", "generic_pla", "enduse_abs", "enduse_pp")]
            return {"ok": True, "result": {"plan_id": plan_id, "status": "candidate", "route": {"mode": "classic"},
                                           "chosen_material": {"id": material, "label": material.replace("_", " ").title()},
                                           "base_preset": {"profile": "Fine", "status": "selected"},
                                           "material_options": options}}
        if name == "apply_plan":
            self.current = self.plans[args["plan_id"]]
            self.applied.append(args["plan_id"])
            return {"ok": True, "result": {"status": "applied"}}
        if name == "slice":
            seconds, grams = self.STATS[self.current]
            return {"ok": True, "result": {"print_time_s": seconds, "material_g": grams}}
        if name == "get_plan":
            return {"ok": True, "result": {"plan_id": args["plan_id"], "route": {"mode": "classic"},
                                           "chosen_material": {"id": self.plans[args["plan_id"]]}}}
        return {"ok": True, "result": {}}


def test_three_materials_are_prepared_and_the_chosen_one_applied():
    r = Materials(choice=1)
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "Шестерня"}, {})
    labels = r.questions.asked[0][0]["options"]
    assert labels == ["Enduse Petg · Fine — 2 ч · 31 г (рекомендуется)", "Generic Pla · Fine — 1 ч 40 мин · 28 г",
                      "Enduse Abs · Fine — 2 ч 10 мин · 30 г"]
    assert result["status"] == "applied" and result["chosen"]["material_id"] == "generic_pla"
    assert r.stack.done == ["undo", "undo", "undo"]                   # every measured variant was undone
    final = [a for n, a in r.calls if n == "apply_plan"][-1]["plan_id"]
    assert r.plans[final] == "generic_pla" and final not in r.applied[:-1]   # planned again, applied once


def test_another_material_leaves_out_the_current_one():
    r = Materials(choice=0)
    result = MaterialComparison(r).run({"object_id": 1, "task_text": "x", "exclude": ["Enduse PETG"]}, {})
    assert [v["material_id"] for v in result["variants"]] == ["generic_pla", "enduse_abs", "enduse_pp"]
    assert _excluded({"id": "generic_pla"}, ["PLA"]) and not _excluded({"id": "enduse_petg"}, ["PLA"])


def test_a_free_answer_goes_back_to_the_model():
    r = Materials(choice=None)
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "x"}, {})
    assert result["status"] == "user_answer" and result["answer"] == "TPU"
    assert [n for n, _a in r.calls].count("apply_plan") == 3            # only the measurements


def test_the_choice_is_the_consent_to_execute():
    gate = PlanExecution(SimpleNamespace(questions=None), lambda: {"language": "ru"})
    gate.approve({"plan_id": "p4", "route": {"mode": "classic"}, "chosen_material": {"id": "generic_pla"}})
    assert gate.executing and gate.plan["applied"]
    gate.before("slice", {}, lambda: False)                          # no confirmation card any more


def test_another_material_without_a_name_leaves_out_the_one_in_the_printer():
    r = Materials(choice=0)
    original = r.call

    def call(name, args, ctx=None):
        if name == "get_state":
            return {"ok": True, "result": {"extruders": [{"material_id": "generic_pla", "material": "Generic PLA"}]}}
        return original(name, args, ctx)
    r.call = call
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "x", "replace_current": True}, {})
    assert "generic_pla" not in [v["material_id"] for v in result["variants"]]
    assert "ALREADY APPLIED" in result["next_step"]
