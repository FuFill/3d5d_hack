# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Timeweb connectivity check or TZ/STL planning with Qwen; Python 3.5+."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ste_planner.qwen_client import QwenClient, QwenError
from ste_planner.qwen_assistant import PlanningAssistant
from ste_planner.causal_bridge import PLREffect, CausalRegistry
from ste_planner.requirements import read_docx_text
from ste_planner.result import to_json


def check_api(client):
    tools = [{"type": "function", "function": {"name": "ping", "description": "Check connection",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
    messages = [{"role": "user", "content": "Вызови инструмент ping с пустыми аргументами. После результата ответь OK."}]
    reply = client.complete(messages, tools=tools, max_tokens=512,
                            tool_choice={"type": "function", "function": {"name": "ping"}})
    calls = reply.get("tool_calls") or []
    valid = (len(calls) == 1 and calls[0].get("id") and calls[0].get("function", {}).get("name") == "ping")
    if not valid or json.loads(calls[0]["function"].get("arguments", "{}")) != {}:
        return {"api_connected": True, "tool_calling": False, "model": client.model}
    messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
    messages.append({"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"status":"ok"}'})
    client.complete(messages, tools=tools, max_tokens=128)
    return {"api_connected": True, "tool_calling": True, "tool_result_roundtrip": True, "model": client.model}


def main():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=os.path.join(root, ".env"))
    parser.add_argument("--check-api", action="store_true")
    parser.add_argument("--tz", help="User-selected DOCX or UTF-8 text file")
    parser.add_argument("--stl", help="User-selected STL")
    parser.add_argument("--resources")
    parser.add_argument("--out", help="JSON result; omitted means stdout")
    parser.add_argument("--causal-models", help="Reviewed scalar PLR exports: JSON list of PLREffect constructor records")
    args = parser.parse_args()
    try:
        client = QwenClient.from_env(args.env_file if os.path.isfile(args.env_file) else None)
        if args.check_api:
            result = check_api(client)
        else:
            if not args.tz or not args.stl:
                parser.error("--tz and --stl are required unless --check-api is used")
            if args.tz.lower().endswith(".docx"):
                text = read_docx_text(args.tz)
            else:
                with open(args.tz, encoding="utf-8-sig") as handle:
                    text = handle.read()
            registry = CausalRegistry()
            if args.causal_models:
                with open(args.causal_models, encoding="utf-8-sig") as handle:
                    records = json.load(handle)
                registry = CausalRegistry([PLREffect(**record) for record in records])
            options = {"source": os.path.basename(args.tz)}
            if args.resources:
                options["resources_dir"] = args.resources
            result = PlanningAssistant(client, registry).run(text, os.path.abspath(args.stl), options)
        output = to_json(result, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(output + "\n")
        else:
            print(output)
        return 0 if result.get("tool_calling", True) and not result.get("llm_error") else 1
    except QwenError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
