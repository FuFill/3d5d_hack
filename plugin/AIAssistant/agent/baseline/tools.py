# -*- coding: utf-8 -*-
"""MCP-инструменты бэйзлайна (tools/list, tools/call) — чистые расчёты без изменения сцены.

Эти же описания передаются в LLM как tools (Chat Completions). Применение решения
к слайсеру (настройки, сцена, слайсинг) делают инструменты плагина в главном Qt-потоке;
здесь только анализ и рекомендация, поэтому вызывать их можно из потока сервера.
"""

import json

from . import knowledge
from .decision import recommend
from .documents import read_text
from .explain import explain_decision
from .geometry import analyze_mesh, load_stl
from .requirements import parse_requirements

TOOLS = [
    {
        "name": "baseline_analyze_model",
        "description": "Геометрия STL: габариты, объём, ось вращения, сквозное отверстие, нависания по ориентациям.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "description": "путь к STL"}},
                        "required": ["path"]},
    },
    {
        "name": "baseline_parse_requirements",
        "description": "Структурирует ТЗ: числовые пределы, среда, технологические требования с цитатами-источниками.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "текст задачи пользователя"},
            "documents": {"type": "array", "items": {"type": "string"}, "description": "пути к DOCX/TXT ТЗ"},
            "hint": {"type": "string", "description": "подсказка режима, например «5D»"}}},
    },
    {
        "name": "baseline_recommend_setup",
        "description": "Рекомендация бэйзлайна: режим 3D/5D, материал, профиль, ориентация, патч настроек с обоснованием.",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string"},
            "documents": {"type": "array", "items": {"type": "string"}},
            "model_path": {"type": "string"},
            "hint": {"type": "string"},
            "material_id": {"type": "string", "description": "принудительный материал из baseline_list_materials"},
            "mode": {"type": "string", "enum": ["classic", "cylindrical", "cylindrical_full"]}},
            "required": ["model_path"]},
    },
    {
        "name": "baseline_list_materials",
        "description": "Каталог материалов Stereotech для Hybrid 530 с профилями и оценками свойств.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(args):
    parts = [args.get("text") or ""]
    for path in args.get("documents") or []:
        parts.append(read_text(path))
    return "\n".join(p for p in parts if p)


def call_tool(name, args):
    """Возвращает результат в формате MCP tools/call: {content: [{type: text, text}], isError}."""
    try:
        if name == "baseline_analyze_model":
            geo = analyze_mesh(load_stl(args["path"]))
            geo["orientations"] = sorted(geo["orientations"], key=lambda o: o["overhang_area"])[:6]
            result = geo
        elif name == "baseline_parse_requirements":
            result = parse_requirements(_text(args), args.get("hint"))
        elif name == "baseline_recommend_setup":
            decision = recommend(_text(args), model_path=args["model_path"], hint=args.get("hint"),
                                 material_id=args.get("material_id"), mode=args.get("mode"))
            result = {"decision": decision, "explanation_md": explain_decision(decision)}
        elif name == "baseline_list_materials":
            result = [{"id": m["id"], "label": m["label"], "polymer": m["polymer"], "filler": m["filler"],
                       "flexible": m["flexible"], "density_g_cm3": m["density_g_cm3"], "profile": m["profile"],
                       "claims": m["claims"]} for m in knowledge.materials()]
        else:
            return {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True}
    except Exception as e:  # ошибка инструмента возвращается агенту, а не роняет сервер
        return {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}


def openai_tools():
    """Те же инструменты в формате tools для OpenAI-совместимого Chat Completions."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["inputSchema"]}} for t in TOOLS]
