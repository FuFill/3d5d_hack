"""Live Timeweb check with generic SOFTWARE context; never modifies a slicer.

Only generic chat strings and fictional snapshot values are sent, no STL/TZ or
saved user chats. Reads the existing local API key in memory; never writes it.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "AIAssistant"), str(ROOT / "plugin")]
from agent import agent as module
from agent.events import EventBus
from agent.llm import LLMClient
from ste_planner.qwen_client import load_env_file


class Snapshot:
    core = questions = None

    def __init__(self):
        self.reads = []

    def openai_tools(self):
        raise AssertionError("A question entered the action branch")

    def has(self, name):
        return name in ("get_state", "list_profiles", "get_slice_result")

    def call(self, name, args, context):
        self.reads.append(name)
        return {"result": {
            "get_state": {"slice_state": "done", "preset": {"profile": "Fine"},
                          "key_settings": {"layer_height": .1, "wall_line_count": 4, "speed_print": 40.}},
            "list_profiles": [{"name": "Fine"}],
            "get_slice_result": {"print_time_s": 7200, "print_time": "2 hours"}
        }[name]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError("Choose a new output path")
    env = load_env_file(str(ROOT / ".env"), dict(os.environ))
    settings = {"base_url": env.get("QWEN_BASE_URL") or env.get("LLM_BASE_URL") or "https://api.timeweb.ai/v1",
                "api_key": env.get("TIMEWEB_API_KEY") or env.get("LLM_API_KEY"),
                "model": env.get("QWEN_MODEL") or env.get("LLM_MODEL") or "dashscope/qwen3.8-flash",
                "language": "ru"}
    if not settings["api_key"]:
        raise ValueError("Local API key is not configured")
    client = LLMClient(settings["base_url"], settings["api_key"], settings["model"], timeout=45)
    requests = []
    original_request = client._request

    def recorded(body, timeout=None):
        requests.append({"stream": bool(body.get("stream")), "has_tools": bool(body.get("tools")),
                         "tool_choice": body.get("tool_choice")})
        return original_request(body, timeout)

    client._request = recorded
    module.LLMClient = lambda *a, **kw: client
    router = module.Agent(None, None, None, None)
    routes = []
    for text, expected in (("Печать слишком долгая", "answer"), ("Мне интересен смысл этого значения", "answer")):
        router._messages = [{"role": "user", "content": text}]
        actual = router._route(client)
        routes.append({"text": text, "expected": expected, "actual": actual})
    router._messages = [{"role": "assistant", "content": "Могу изменить заполнение. Продолжить?"},
                        {"role": "user", "content": "да"}]
    routes.append({"text": "да (after an offer)", "expected": "act", "actual": router._route(client)})
    bus, registry = EventBus(), Snapshot()
    value = module.Agent(None, registry, bus, lambda: settings)
    value._messages = [{"role": "user", "content": "Сделай прочнее"},
                       {"role": "assistant", "content": "Могу изменить заполнение. Продолжить?"}]
    value._turn_text = "почему так долго"
    value._messages.append({"role": "user", "content": value._turn_text})
    value._run()
    events = bus.snapshot()[0]
    answer = "\n".join(e.get("text", "") for e in events if e["type"] == "message" and not e.get("note"))
    passed = (all(r["expected"] == r["actual"] for r in routes) and bool(answer.strip()) and
              not any(e["type"] in ("question", "tool", "error") for e in events) and
              events[-1]["type"] == "done" and events[-1]["status"] == "done" and
              all(not r["has_tools"] and r["tool_choice"] == "none" for r in requests if r["stream"]))
    report = {"status": "pass" if passed else "fail", "model": settings["model"],
              "scope": "live API + software-only project snapshot; no native scene changes",
              "routes": routes, "question": "почему так долго", "answer": answer,
              "question_cards": sum(e["type"] == "question" for e in events), "read_only_snapshot_calls": registry.reads,
              "requests": requests, "private_documents_sent": False, "native_slicer_mutated": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    # Frozen/Windows consoles may use cp1252. The saved UTF-8 report keeps the
    # original text; ASCII escaping avoids turning a passing check into an I/O error.
    print(json.dumps({"status": report["status"], "routes": routes, "answer": answer, "question_cards": report["question_cards"]}))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
