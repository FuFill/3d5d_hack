"""Pose check (stiffness from FE or the core's screen, time/material from slicing) and material comparison."""

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
sys.path.insert(0, str(ROOT / "plugin"))
sys.path.insert(0, str(ROOT / "plugin/tests"))

import contextlib

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
        self.side = []

        @contextlib.contextmanager
        def side_plans():
            self.side.append("in")
            yield
            self.side.append("out")
        self.core = SimpleNamespace(decision_record=lambda plan_id: {"decision": self.decision}, side_plans=side_plans)
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
            raise AssertionError("the pose check must not slice")
        if name == "get_state":
            return {"ok": True, "result": {"key_settings": {"layer_height": 0.2}, "extruders": [{"material": "PLA"}]}}
        return {"ok": True, "result": {}}


def check(registry, **args):
    """The pose check with estimates per pose (no slicing); turns are counted, the project is not touched otherwise."""
    run = O.OrientationCheck(registry)
    registry.turns = []

    def turn(object_id, rotation):
        registry.turns.append(rotation)
        registry.pose = "side"
    run._turn = turn
    name = lambda rotation: "side" if rotation[2][1] > 0.5 else "current" if rotation[2][2] > 0.5 else "other"
    original = (O.E.pose, O.E.estimate)
    O.E.pose = lambda path, rotation, settings: {"pose": name(rotation)}
    O.E.estimate = lambda path, settings, placement, material=None: dict(registry.stats[placement["pose"]])
    try:
        return run.run(args, {})
    finally:
        O.E.pose, O.E.estimate = original


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
    assert part["status"] == "rotated" and round(part["time_gain"], 3) == 0.167 and len(r.turns) == 1
    assert ("plan_print", {"object_id": 1, "mode": "classic", "orientation": "keep_current",
                           "task_text": "Print preparation of this part."}) in r.calls


def test_a_small_gain_keeps_the_pose():
    r = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3500, "material_g": 29.5}})
    assert check(r)["parts"][0]["status"] == "kept_small_gain" and r.turns == []


def test_a_much_softer_pose_is_never_taken_even_if_faster():
    r = Registry(screen(0.1, 0.5), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 2000, "material_g": 20}})
    part = check(r)["parts"][0]
    assert part["status"] == "optimal" and r.turns == []


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
    assert part["status"] == "rotated_for_strength" and len(r.turns) == 1


def test_a_pose_the_user_set_is_kept_and_the_gain_reported():
    r = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                    "side": {"print_time_s": 3000, "material_g": 30}})
    result = check(r, keep_pose=True)
    assert result["parts"][0]["status"] == "kept_user_pose" and r.turns == []
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
    """plan_print records per material; the estimate reads them (no apply/slice before the choice)."""
    STATS = {"enduse_petg": (7200, 31), "generic_pla": (6000, 28), "enduse_abs": (7800, 30), "enduse_pp": (9000, 26)}

    def __init__(self, choice=0):
        Registry.__init__(self, {}, {})
        self.questions = Questions(choice)
        self.plans, self.applied = {}, []
        self.current = None
        self.core = SimpleNamespace(side_plans=self.core.side_plans, decision_record=lambda plan_id: {
            "request": {"mesh_path": "/m/gear.stl"},
            "decision": {"chosen_material": {"id": self.plans[plan_id]}, "effective_settings": {}, "orientation": {}}})

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


def estimates(monkeypatch, failing=()):
    from agent import variants
    monkeypatch.setattr(variants.E, "estimate", lambda path, settings, placement, material=None:
                        {} if material in failing else
                        dict(zip(("print_time_s", "material_g"), Materials.STATS[material]), estimate=True))


def test_three_materials_are_prepared_and_the_chosen_one_applied(monkeypatch):
    estimates(monkeypatch)
    r = Materials(choice=1)
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "Шестерня"}, {})
    labels = r.questions.asked[0][0]["options"]
    assert labels == ["Enduse Petg · Fine — ≈2 ч · ≈31 г (рекомендуется)", "Generic Pla · Fine — ≈1 ч 40 мин · ≈28 г",
                      "Enduse Abs · Fine — ≈2 ч 10 мин · ≈30 г"]
    assert result["status"] == "applied" and result["chosen"]["material_id"] == "generic_pla"
    assert [n for n, _a in r.calls if n in ("apply_plan", "slice")] == ["apply_plan"]   # only the chosen one
    assert r.plans[r.applied[0]] == "generic_pla" and r.stack.done == []
    assert r.applied[0] != result["variants"][1]["plan_id"]          # planned again outside the trial plans
    assert r.side == ["in", "out"]


def test_another_material_leaves_out_the_current_one(monkeypatch):
    estimates(monkeypatch)
    r = Materials(choice=0)
    result = MaterialComparison(r).run({"object_id": 1, "task_text": "x", "exclude": ["Enduse PETG"]}, {})
    assert [v["material_id"] for v in result["variants"]] == ["generic_pla", "enduse_abs", "enduse_pp"]
    assert _excluded({"id": "generic_pla"}, ["PLA"]) and not _excluded({"id": "enduse_petg"}, ["PLA"])


def test_a_free_answer_goes_back_to_the_model(monkeypatch):
    estimates(monkeypatch)
    r = Materials(choice=None)
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "x"}, {})
    assert result["status"] == "user_answer" and result["answer"] == "TPU"
    assert [n for n, _a in r.calls].count("apply_plan") == 0            # nothing was applied


def test_a_single_material_is_applied_only_after_the_user_agrees(monkeypatch):
    estimates(monkeypatch)
    args = {"attachment_id": "a1", "task_text": "x", "exclude": ["PETG", "ABS", "PP"]}
    r = Materials(choice=1)
    result = MaterialComparison(r).run(dict(args), {})
    assert r.questions.asked[0][0]["options"][-1] == "Не применять"
    assert result["status"] == "declined" and [n for n, _a in r.calls].count("apply_plan") == 0
    r = Materials(choice=0)
    result = MaterialComparison(r).run(dict(args), {})
    assert result["status"] == "applied" and result["chosen"]["material_id"] == "generic_pla"


def test_the_choice_is_the_consent_to_execute():
    gate = PlanExecution(SimpleNamespace(questions=None), lambda: {"language": "ru"})
    gate.approve({"plan_id": "p4", "route": {"mode": "classic"}, "chosen_material": {"id": "generic_pla"}})
    assert gate.executing and gate.plan["applied"]
    gate.before("slice", {}, lambda: False)                          # no confirmation card any more


def test_another_material_without_a_name_leaves_out_the_one_in_the_printer(monkeypatch):
    estimates(monkeypatch)
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


def test_a_slice_cancelled_by_late_setting_updates_is_started_again(monkeypatch):
    from agent import tools as T
    monkeypatch.setattr(T, "SETTLE_SECONDS", 0)
    states = [1, 3]                      # STE cancels the first slice ("not started"), the second one runs
    forced = []
    checks = [True, False, False, False]  # the settings check is still running at first

    class Monitor:
        def generation(self):
            return len(forced)

        def wait_finished(self, generation, timeout, should_stop=None):
            return states.pop(0)

        def watch(self, seq):
            pass

        def status(self):
            return (3, 1.0)

    class Checker:
        @property
        def needToWaitForResult(self):
            return checks.pop(0) if checks else False
    checker = Checker()
    app = SimpleNamespace(getBackend=lambda: SimpleNamespace(forceSlice=lambda: forced.append(1)),
                          getMachineErrorChecker=lambda: checker)
    registry = T.ToolRegistry(app, Monitor(), lambda: "")
    registry._scene_nodes = lambda: [SimpleNamespace(getName=lambda: "gear", isOutsideBuildArea=lambda: False)]
    registry._print_info = lambda: {"print_time_s": 3600, "material_g": 20}
    registry._mode = lambda: {"mode": "classic"}
    result = registry.call("slice", {}, {})
    assert result["ok"] and result["result"]["print_time_s"] == 3600 and len(forced) == 2


def test_a_variant_without_an_estimate_is_still_offered_and_unfit_ones_are_replaced(monkeypatch):
    estimates(monkeypatch, failing=("generic_pla",))
    r = Materials(choice=0)
    original = r.call

    def call(name, args, ctx=None):
        if name == "plan_print" and args.get("preferred_material") == "enduse_abs":
            result = original(name, args, ctx)
            result["result"]["status"] = "needs_input"       # e.g. needs another nozzle
            return result
        return original(name, args, ctx)
    r.call = call
    result = MaterialComparison(r).run({"attachment_id": "a1", "task_text": "x"}, {})
    assert [v["material_id"] for v in result["variants"]] == ["enduse_petg", "generic_pla", "enduse_pp"]
    assert "Generic Pla · Fine — время не посчитано" in r.questions.asked[0][0]["options"]
    assert result["skipped"][0]["material"] == "enduse_abs"


def test_the_estimate_of_a_real_gear_is_plausible_and_follows_the_settings():
    from agent import estimate as E
    from testutil import example_cases
    path = next(c[2] for c in example_cases() if str(c[2]).lower().endswith(".stl"))
    flat = E.pose(path, IDENTITY, {"layer_height": 0.2})
    base = {"layer_height": 0.2, "line_width": 0.4, "wall_line_count": 2, "top_layers": 4, "bottom_layers": 4,
            "infill_sparse_density": 20, "speed_print": 50}
    pla = E.estimate(path, base, flat, "generic_pla")
    assert pla["estimate"] and 60 < pla["print_time_s"] < 48 * 3600 and 0.5 < pla["material_g"] < 5000
    finer = E.estimate(path, dict(base, layer_height=0.1), flat, "generic_pla")
    denser = E.estimate(path, dict(base, infill_sparse_density=80), flat, "generic_pla")
    assert finer["print_time_s"] > 1.6 * pla["print_time_s"] and denser["material_g"] > pla["material_g"]
    assert E.density("enduse_petg") == 1.3 and E.density("Enduse PETG") == 1.3 and E.density("unknown") == 1.24


def test_the_estimate_stays_close_to_native_ste_slices(tmp_path):
    """Calibration guard: experiments/ste_runtime/final holds native STE slices with their projects."""
    import json, math, re, struct, zipfile
    import numpy as np
    from agent import estimate as E
    for case in ("baseline", "live_demo"):
        folder = ROOT / "experiments" / "ste_runtime" / "final" / case
        xml = zipfile.ZipFile(str(folder / "part.3mf")).read("3D/3dmodel.model").decode("utf-8")
        vertices = np.array(re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', xml), dtype=float)
        triangles = np.array(re.findall(r'<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"', xml), dtype=int)
        stl = tmp_path / (case + ".stl")
        with open(str(stl), "wb") as handle:
            handle.write(b"\0" * 80 + struct.pack("<I", len(triangles)))
            for a, b, c in vertices[triangles]:
                handle.write(struct.pack("<12fH", 0, 0, 0, *a, *b, *c, 0))
        record = json.load(open(str(folder / "004_slice.json")))["readback"]
        settings = record["actual"]
        estimate = E.estimate(str(stl), settings, E.pose(str(stl), IDENTITY, settings), record["material_id"])
        header = open(str(folder / "part.gcode"), errors="replace").read(2000)
        seconds = int(re.search(r";TIME:(\d+)", header).group(1))
        grams = math.pi * 0.875 ** 2 * float(re.search(r";Filament used: ([\d.]+)m", header).group(1)) * 1000 * \
            E.density(record["material_id"]) / 1000.0
        assert abs(estimate["print_time_s"] - seconds) / seconds < 0.3, (case, estimate, seconds)
        assert abs(estimate["material_g"] - grams) / grams < 0.3, (case, estimate, grams)


def test_one_source_is_used_and_the_pose_check_keeps_the_models_plan(monkeypatch):
    estimates(monkeypatch)
    r = Materials(choice=0)
    MaterialComparison(r).run({"attachment_id": "a1", "object_id": 1, "task_text": "x"}, {})
    assert all("attachment_id" not in a for n, a in r.calls if n == "plan_print")   # the part on the plate wins
    pose = Registry(screen(0.3, 0.3), {"current": {"print_time_s": 3600, "material_g": 30},
                                       "side": {"print_time_s": 3000, "material_g": 30}})
    check(pose)
    assert pose.side == ["in", "out"]                                  # its plan was a side plan
