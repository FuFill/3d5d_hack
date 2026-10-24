"""The chat must get a real answer before moving from a core proposal to execution.

This guard belongs to the application, not the model prompt. Direct manual commands
without a core plan still use their ordinary tools and hardware checks.
"""
from .llm import Cancelled
from .questions import QuestionCancelled


EXECUTION_TOOLS = frozenset((
    "apply_plan", "apply_plan_to_project", "revert_plan_changes", "set_settings", "set_print_mode",
    "select_profile", "create_profile", "save_profile", "set_nozzle", "set_material", "load_model",
    "move_object", "rotate_object", "scale_object", "arrange_all", "delete_object", "slice",
    "export_gcode", "save_project"))


class ExecutionDeferred(Exception):
    """The user kept the proposal in planning; discard the remaining calls in this batch."""


class PlanExecution:
    def __init__(self, registry, settings):
        self._registry, self._settings = registry, settings
        self.plan = None
        self._approved = set()
        self.begin_turn()

    def begin_turn(self):
        self._produced = False

    def observe(self, name, result):
        if name in ("plan_print", "get_plan") and isinstance(result, dict) and result.get("plan_id"):
            if self._key(result) != self._key(self.plan):
                self._approved.clear()
            self.plan = result
            self._produced = self._produced or name == "plan_print"
        elif name in ("apply_plan", "apply_plan_to_project") and self.plan:
            self.plan = dict(self.plan, applied=True)

    @staticmethod
    def _key(plan):
        return (plan.get("plan_id"), plan.get("decision_sha256")) if plan else None

    def _ready(self, include_applied=False):
        return bool(self.plan and (include_applied or not self.plan.get("applied"))
                    and self.plan.get("status") not in ("infeasible", "needs_input")
                    and (self.plan.get("base_preset") or {}).get("status") != "needs_input")

    def offer_at_end(self):
        return self._produced and self._ready() and (self._key(self.plan), "plan") not in self._approved

    def before(self, name, arguments, should_stop):
        if name in ("apply_plan", "apply_plan_to_project"):
            # A reopened chat or a different plan id cannot reuse an earlier card's answer.
            outcome = self._registry.call("get_plan", {"plan_id": arguments.get("plan_id")}, None)
            if not outcome.get("ok"):
                return  # The real apply tool reports its existing invalid/stale-plan error.
            self.observe("get_plan", outcome["result"])
        elif name not in EXECUTION_TOOLS or not self._produced:
            return
        self.confirm(name, should_stop)
        if self._ready(include_applied=name == "apply_plan_to_project") and (self.plan.get("route") or {}).get("mode") in ("cylindrical", "cylindrical_full"):
            if name in ("apply_plan", "apply_plan_to_project"):
                # This flag follows the actual warning and answer below, never an LLM assertion.
                arguments["confirm_unverified_5d"] = True

    def confirm(self, name, should_stop):
        if not self._ready(include_applied=name == "apply_plan_to_project"):
            return
        scope = "project" if name == "apply_plan_to_project" else "plan"
        key = (self._key(self.plan), scope)
        if key in self._approved:
            return
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
        self._approved.add(key)
