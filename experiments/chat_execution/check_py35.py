"""Small native Python 3.5 smoke check; no Qt writes, network calls or print-quality claims."""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "AIAssistant"))
from agent.agent import Agent
from agent.execution import ExecutionDeferred, PlanExecution


class Questions:
    def __init__(self):
        self.calls, self.choice = [], 0

    def ask(self, questions, should_stop):
        self.calls.append(questions)
        return [{"choice": self.choice, "text": questions[0]["options"][self.choice]}], len(self.calls)


class Registry:
    def __init__(self, plan):
        self.plan, self.questions = plan, Questions()

    def call(self, name, args, context):
        assert name == "get_plan"
        return {"ok": True, "result": copy.deepcopy(self.plan)}


plan = {"plan_id": "p1", "decision_sha256": "decision1", "status": "candidate", "applied": False,
        "route": {"mode": "cylindrical"}, "base_preset": {"status": "selected", "profile": "Fine"}}
registry = Registry(plan)
gate = PlanExecution(registry, lambda: {"language": "ru"})
gate.observe("plan_print", copy.deepcopy(plan))
assert gate.offer_at_end()
args = {"plan_id": "p1", "confirm_unverified_5d": False}
gate.before("apply_plan", args, lambda: False)
assert args["confirm_unverified_5d"] is True
assert "не проверены" in registry.questions.calls[0][0]["text"]
gate.before("slice", {}, lambda: False)
assert len(registry.questions.calls) == 1
registry.plan["decision_sha256"] = "decision2"
registry.questions.choice = 1
try:
    gate.before("apply_plan", {"plan_id": "p1"}, lambda: False)
except ExecutionDeferred:
    pass
else:
    raise AssertionError("A changed plan executed without a new affirmative answer")
assert len(registry.questions.calls) == 2
report = {"status": "pass", "python": sys.version.split()[0], "agent_import": True,
          "approval_required": True, "one_card_per_plan": True, "five_d_warning_before_flag": True,
          "changed_decision_requires_new_consent": True, "refusal_defers_execution": True}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(report, stream, indent=2)
