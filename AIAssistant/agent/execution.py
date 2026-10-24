"""One real answer authorizes the current task through its execution workflow.

This guard belongs to the application, not the model prompt. Direct manual commands
without a core plan still use their ordinary tools and hardware checks.
"""
import json

from .llm import Cancelled
from .questions import QuestionCancelled


EXECUTION_TOOLS = frozenset((
    "apply_plan", "apply_plan_to_project", "revert_plan_changes", "set_settings", "set_print_mode",
    "select_profile", "create_profile", "save_profile", "set_nozzle", "set_material", "load_model",
    "move_object", "rotate_object", "scale_object", "arrange_all", "delete_object", "slice",
    "export_gcode", "save_project"))


class ExecutionDeferred(Exception):
    """The user kept the proposal in planning; discard the remaining calls in this batch."""


class ExecutionConflict(Exception):
    """A tool tried to reopen consent or change the approved task's physical context."""


class PlanExecution:
    def __init__(self, registry, settings):
        self._registry, self._settings = registry, settings
        self.plan = None
        self.begin_turn()

    def begin_turn(self):
        self._produced = False
        self.executing = False
        self._approved_context = None

    def observe(self, name, result):
        if name in ("plan_print", "get_plan") and isinstance(result, dict) and result.get("plan_id"):
            self.plan = result
            self._produced = self._produced or name == "plan_print"
        elif name in ("apply_plan", "apply_plan_to_project") and self.plan:
            self.plan = dict(self.plan, applied=True)

    @staticmethod
    def _context(plan):
        material = plan.get("chosen_material") or {}
        return {"mode": (plan.get("route") or {}).get("mode"),
                "material": material.get("id") or material.get("label"),
                "geometry_sha256": plan.get("geometry_sha256")}

    def tools(self, tools):
        """After consent the model executes; it cannot reopen a question card."""
        if not self.executing:
            return tools
        return [tool for tool in tools if tool.get("function", {}).get("name") != "ask_user"]

    def continuation(self):
        if not self.executing:
            return ""
        return ("\nAPPLICATION EXECUTION STATE (authoritative):\n" + json.dumps({
            "phase": "executing", "user_approved_current_task": True,
            "current_plan_id": self.plan.get("plan_id"), "plan_applied": bool(self.plan.get("applied")),
            "approved_context": self._approved_context}, ensure_ascii=False) +
            "\nFinish this task without further confirmations: apply the plan if needed, verify, slice, "
            "export G-code and save the project, respecting the original user request. "
            "Use get_plan with current_plan_id to READ the current plan. Do not call plan_print just to read it; "
            "recalculate only to fix an actual error or refine this same task. Recalculation does not revoke consent. "
            "Do not ask whether to continue, apply, slice or save. Do not change the approved material, mode or model. "
            "If a real missing physical fact prevents execution, report the blocker instead of inventing an answer.")

    def _ready(self, include_applied=False):
        return bool(self.plan and (include_applied or not self.plan.get("applied"))
                    and self.plan.get("status") not in ("infeasible", "needs_input")
                    and (self.plan.get("base_preset") or {}).get("status") != "needs_input")

    def approve(self, plan):
        """The user chose this applied plan in a choice card (compare_materials): that answer is the consent."""
        self.plan = dict(plan or {}, applied=True)
        self.executing = True
        self._approved_context = self._context(self.plan)

    def offer_at_end(self):
        return self._produced and self._ready() and not self.executing

    def before(self, name, arguments, should_stop):
        if self.executing and name == "ask_user":
            raise ExecutionConflict("Execution is already authorized for this task. Do not ask for confirmation again. "
                                    "Continue applying, verifying, slicing and saving within the approved task. "
                                    "No new user answer was collected; report an actual blocker if a required physical fact is missing.")
        if name in ("apply_plan", "apply_plan_to_project"):
            # Read the current application-owned proposal; a new calculation ID is not a new task.
            outcome = self._registry.call("get_plan", {"plan_id": arguments.get("plan_id")}, None)
            if not outcome.get("ok"):
                return  # The real apply tool reports its existing invalid/stale-plan error.
            self.observe("get_plan", outcome["result"])
        elif name not in EXECUTION_TOOLS or not self._produced:
            return
        if self.executing and self.plan and self._context(self.plan) != self._approved_context:
            raise ExecutionConflict("The current proposal changes the approved material, mode or source model. "
                                    "Keep the original task context: %s. Recalculate within it and continue; "
                                    "do not ask for another execution confirmation." %
                                    json.dumps(self._approved_context, ensure_ascii=False))
        self.confirm(name, should_stop)
        if self._ready(include_applied=name == "apply_plan_to_project") and (self.plan.get("route") or {}).get("mode") in ("cylindrical", "cylindrical_full"):
            if name in ("apply_plan", "apply_plan_to_project"):
                # This flag follows the actual warning and answer below, never an LLM assertion.
                arguments["confirm_unverified_5d"] = True

    def confirm(self, name, should_stop):
        if self.executing:
            return
        if not self._ready(include_applied=name == "apply_plan_to_project"):
            return
        scope = "project" if name == "apply_plan_to_project" else "plan"
        questions = self._registry.questions
        if questions is None:
            raise ExecutionDeferred()
        ru = self._settings().get("language", "ru") == "ru"
        base = self.plan.get("base_preset") or {}
        material = self.plan.get("chosen_material") or {}
        mode = (self.plan.get("route") or {}).get("mode")
        labels = [base.get("profile"), material.get("label") or material.get("id"),
                  {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}.get(mode)]
        summary = " · ".join(str(x) for x in labels if x)
        text = ("План готов" if ru else "The plan is ready") + (": " + summary if summary else "") + ". "
        text += ("Перейти к выполнению: применить план и продолжить подготовку печати?" if ru else
                 "Move to execution: apply the plan and continue print preparation?")
        text += (" После согласия выполню слайсинг и сохраню файлы без повторных подтверждений. "
                 "Настройки печати общие для всех моделей проекта." if ru else
                 " After approval I will slice and save the files without further confirmations. "
                 "Print settings are shared by all models in the project.")
        if scope == "project":
            text += (" Настройки изменятся для всех моделей проекта; их положение сохранится." if ru else
                     " Settings will change for every model in the project; their positions will stay unchanged.")
        if mode in ("cylindrical", "cylindrical_full"):
            text += (" Для 5D кинематика и столкновения не проверены; применение плана не подтверждает готовность к печати." if ru else
                     " For 5D, kinematics and collisions are unverified; applying the plan does not qualify it for printing.")
        options = (["Перейти к выполнению", "Остаться в планировании"] if ru else
                   ["Move to execution", "Stay in planning"])
        try:
            answers, _seq = questions.ask([{"text": text, "options": options, "allow_custom": False}], should_stop)
        except QuestionCancelled:
            raise Cancelled()
        if should_stop():
            raise Cancelled()
        answer = answers[0] if answers else {}
        choice = answer.get("choice")
        text = str(answer.get("text") or "").strip()
        explicit = text.lower().rstrip(".! ") in (options[0].lower(), "да", "давай", "применяй", "приступай",
                                                   "начинай", "выполняй", "yes", "go ahead", "apply", "proceed")
        if not ((choice == 0 and text == options[0]) or (choice is None and explicit)):
            raise ExecutionDeferred()
        self.executing = True
        self._approved_context = self._context(self.plan)
