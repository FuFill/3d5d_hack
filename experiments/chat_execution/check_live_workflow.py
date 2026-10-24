"""Live Qwen + real Agent/MCP/QuestionBox, with SOFTWARE-ONLY slicer operations.

An intentional stale-plan error forces recovery/recalculation after approval.
Only a generic program fixture is sent: no STL/TZ, saved chats or native project.
The local key is read in memory and never included in reports or prompts.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "AIAssistant"), str(ROOT / "plugin")]
from agent import agent as module
from agent.events import EventBus
from agent.llm import LLMClient
from agent.mcp import MCPServer
from agent.tools import ToolError, ToolRegistry
from ste_planner.qwen_client import load_env_file


class SoftwareSlicer:
    def __init__(self):
        self.plans, self.calls, self.completed = {}, [], []
        self.injected_stale_error = False
        self.applied = self.verified = self.sliced = False

    def invoke(self, name, args, context):
        self.calls.append(name)
        if name == "get_state":
            return {"printer": "Software-check Hybrid 530", "printing_mode": "cylindrical_full",
                    "preset": {"profile": "Fine (normal)"}, "slice_state": "done" if self.sliced else "not_started",
                    "extruders": [{"position": 0, "nozzle_mm": .6, "material": "Enduse PA6"}],
                    "objects": [{"id": 7, "name": "software-part.stl", "size": {"x": 30, "y": 10, "z": 30}}]}
        if name == "list_profiles":
            return {"profiles": [{"name": "Fine (normal)", "quality_type": "normal", "active": True}]}
        if name == "list_model_sources":
            return {"attachments": [], "scene_models": [{"object_id": 7, "name": "software-part.stl",
                    "source_path": "C:/software-check/part.stl", "can_plan_original_stl": True}]}
        if name == "plan_print":
            plan_id = "software-plan-%d" % (len(self.plans) + 1)
            plan = {"plan_id": plan_id, "decision_sha256": plan_id + "-decision", "geometry_sha256": "same-software-fixture",
                    "status": "needs_calibration", "applied": False, "physical_print_approval": False,
                    "base_preset": {"status": "selected", "profile": "Fine (normal)"},
                    "chosen_material": {"id": "enduse_pa6", "label": "Enduse PA6"},
                    "route": {"mode": "cylindrical_full"}, "missing_inputs": [], "setting_changes": [],
                    "apply_target": {"object_id": 7, "in_place": True},
                    "explanation": "Условия процесса заданы. Кинематика 5D и физическая прочность не подтверждены."}
            self.plans[plan_id] = plan
            return copy.deepcopy(plan)
        if name == "get_plan":
            if args.get("plan_id") not in self.plans:
                raise ToolError("Use a returned plan_id")
            return copy.deepcopy(self.plans[args["plan_id"]])
        if name == "apply_plan":
            if not self.injected_stale_error:
                self.injected_stale_error = True
                raise ToolError("Project changed since planning; calculate a new plan")
            if len(self.plans) < 2 or args.get("plan_id") != list(self.plans)[-1]:
                raise ToolError("Recalculate the same task after the stale-plan error, then apply its new plan_id")
            if args.get("confirm_unverified_5d") is not True:
                raise ToolError("Explicit 5D consent is missing")
            self.plans[args["plan_id"]]["applied"] = True
            self.applied = True
            result = {"applied": True, "physical_print_approval": False}
        elif name == "verify_plan":
            if not self.applied:
                raise ToolError("Apply the current plan first")
            self.verified = True
            result = {"status": "pass", "differences": []}
        elif name == "slice":
            if not self.verified:
                raise ToolError("Readback must pass before slicing")
            self.sliced = True
            result = {"print_time_s": 7200, "material_g": 25, "printing_mode": "cylindrical_full"}
        elif name in ("export_gcode", "save_project"):
            if not self.sliced:
                raise ToolError("Slice before exporting")
            result = {"path": "C:/software-check/" + ("result.gcode" if name == "export_gcode" else "project.3mf")}
        else:
            raise ToolError("Unexpected software check operation")
        self.completed.append(name)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError("Choose a new report path")
    env = load_env_file(str(ROOT / ".env"), dict(os.environ))
    settings = {"base_url": env.get("QWEN_BASE_URL") or env.get("LLM_BASE_URL") or "https://api.timeweb.ai/v1",
                "api_key": env.get("TIMEWEB_API_KEY") or env.get("LLM_API_KEY"),
                "model": env.get("QWEN_MODEL") or env.get("LLM_MODEL") or "dashscope/qwen3.8-flash", "language": "ru"}
    if not settings["api_key"]:
        raise ValueError("Local key is not configured")
    client = LLMClient(settings["base_url"], settings["api_key"], settings["model"], timeout=45)
    module.LLMClient = lambda *a, **kw: client
    fake = SoftwareSlicer()
    bus = EventBus()
    core = SimpleNamespace(plan=lambda *a: None, apply=lambda *a: None, verify=lambda *a: None,
                           summary=lambda *a: None, cancel=lambda: None)
    registry = ToolRegistry(None, None, lambda: "", core=core, bus=bus)
    allowed = {"get_state", "list_profiles", "list_model_sources", "plan_print", "get_plan", "apply_plan",
               "verify_plan", "slice", "export_gcode", "save_project", "ask_user"}
    registry._tools = [t for t in registry._tools if t.name in allowed]
    registry._by_name = {t.name: t for t in registry._tools}
    for tool in registry._tools:
        if tool.name != "ask_user":
            tool.func = lambda a, c, name=tool.name: fake.invoke(name, a, c)
            tool.on_main = False
    agent = module.Agent(MCPServer(registry), registry, bus, lambda: settings)
    task = ("Подготовь имеющуюся деталь к 5D-печати: примени план, проверь настройки, нарежь, сохрани G-code и проект. "
            "Материал Enduse PA6 и катушка подтверждены, сопло 0,6 мм физически установлено. "
            "Материал и режим менять не нужно. Я согласен на подготовку непроверенного 5D-процесса, но переход "
            "к выполнению подтверди отдельной карточкой один раз. Не заявляй подтверждённую физическую прочность.")
    agent.submit(task, [])
    deadline = time.monotonic() + args.timeout
    answered, duplicate = set(), False
    while agent.busy() and time.monotonic() < deadline:
        for event in bus.snapshot()[0]:
            if event["type"] != "question" or event["status"] != "pending" or event["seq"] in answered:
                continue
            if answered:
                duplicate = True
                agent.cancel()
                break
            questions = event["questions"]
            if len(questions) != 1 or "Перейти к выполнению" not in questions[0]["text"]:
                agent.cancel()
                break
            answer = [{"text": questions[0]["options"][0], "choice": 0}]
            if registry.questions.answer(event["seq"], answer):
                answered.add(event["seq"])
                print("Execution approved once", flush=True)
        time.sleep(.02)
    if agent.busy():
        agent.cancel()
    agent._thread.join(50)
    events = bus.snapshot()[0]
    cards = [e for e in events if e["type"] == "question"]
    required = ["apply_plan", "verify_plan", "slice", "export_gcode", "save_project"]
    passed = (not agent.busy() and not duplicate and len(cards) == len(answered) == 1
              and all(name in fake.completed for name in required) and len(fake.plans) >= 2
              and events[-1].get("status") == "done" and not any(e["type"] == "error" for e in events))
    report = {"status": "pass" if passed else "fail", "model": settings["model"],
              "scope": "Live Qwen + real Agent/MCP/QuestionBox; simulated slicer operations, not physical metrics",
              "question_cards": len(cards), "execution_approvals": len(answered), "duplicate_confirmation": duplicate,
              "calculations": len(fake.plans), "injected_stale_plan_error": fake.injected_stale_error,
              "completed_operations": fake.completed, "calls": fake.calls,
              "errors": [e.get("text") for e in events if e["type"] == "error"],
              "final_answer": "\n".join(e.get("text", "") for e in events if e["type"] == "message" and not e.get("note")),
              "private_documents_sent": False, "native_slicer_modified": False, "files_exported_by_real_slicer": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "question_cards", "execution_approvals", "calculations", "completed_operations", "errors")}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
