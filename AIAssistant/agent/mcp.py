# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""MCP server core: JSON-RPC 2.0 with initialize, tools/list and tools/call.

The official MCP SDK needs Python 3.10+, so the protocol subset is implemented here.
The same handler serves external MCP clients over HTTP (POST /mcp) and the built-in
agent, which calls it in-process.
"""

import json

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class MCPServer:
    def __init__(self, registry, name="ste-slicer-ai-assistant", version="1.0.0"):
        self._registry = registry
        self._info = {"name": name, "version": version}

    def handle_text(self, text):
        """Handle a raw JSON-RPC body; returns the response text or None for notifications."""
        try:
            message = json.loads(text)
        except ValueError:
            return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
        if isinstance(message, list):
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return json.dumps(replies, ensure_ascii=False) if replies else None
        reply = self.handle(message)
        return json.dumps(reply, ensure_ascii=False) if reply is not None else None

    def handle(self, message, context=None):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
        request_id = message.get("id")
        is_notification = "id" not in message
        method = message["method"]
        params = message.get("params") or {}
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                result = {
                    "protocolVersion": requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": self._info,
                }
            elif method == "ping":
                result = {}
            elif method.startswith("notifications/"):
                return None
            elif method == "tools/list":
                result = {"tools": self._registry.mcp_tools()}
            elif method == "tools/call":
                name = params.get("name")
                if not self._registry.has(name):
                    return _error(request_id, INVALID_PARAMS, "Unknown tool: %s" % name)
                outcome = self._registry.call(name, params.get("arguments") or {}, context)
                result = {
                    "content": [{"type": "text", "text": json.dumps(outcome["result"], ensure_ascii=False)}],
                    "structuredContent": outcome["result"],
                    "isError": not outcome["ok"],
                }
            else:
                return None if is_notification else _error(request_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        except Exception as e:
            return None if is_notification else _error(request_id, INTERNAL_ERROR, str(e))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
