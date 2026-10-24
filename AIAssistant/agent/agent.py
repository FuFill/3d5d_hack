# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Dialog loop: user message -> LLM -> MCP tool calls -> LLM ... -> answer.

Everything the model does is published to the chat window as it happens: reasoning
(collapsed in the UI), answer text, each tool call with arguments, result and changes.
"""

import base64
import json
import os
import threading
import time

from .llm import Cancelled, LLMClient, LLMError

MAX_ROUNDS = 40
MAX_TOOL_RESULT_CHARS = 12000
IMAGE_MAX_SIDE = 1600

SYSTEM_PROMPT = """You are the AI assistant built into STE Slicer (Stereotech slicer, a fork of Cura 3.6).
You set up 3D and 5D FDM printing for the user through tools that change the real slicer state.

Rules:
- Start with get_state. Use only the printing modes classic (3D), cylindrical (5D Spiral) and cylindrical_full (5D).
  The main test printer is Stereotech Hybrid 530 V5.2.4.
- Order of work: load the model (load_model with the attached file path) -> inspect it (list_objects) ->
  set_print_mode -> select_profile -> set_material -> set_settings -> orient/scale/move/arrange ->
  slice -> export_gcode -> save_project. Switching mode, profile or material can change other values, so do these first.
- Choose 5D when the part is a body of revolution or must be printed around the rotary axis (tubes, rings,
  wheels, shafts, impellers built around a hub); choose 3D for prismatic and flat parts and figurines.
- Never invent setting keys: look them up with find_settings and use option keys exactly as returned.
  Prefer few, meaningful changes on top of a fitting profile. Respect warnings returned by tools.
- Coordinates are printer axes in mm, Z up, origin at the build plate centre.
- If a tool fails, read the error, fix the cause and retry once; if it still fails, tell the user.
- After a successful slice, export G-code and save the project unless the user asked otherwise.
- Final answer: short, plain text with a few bullet points: chosen mode, profile, material, key settings
  and orientation with one-line reasons, then print time and material, then the saved file paths.
- Reply in {language}."""


class Agent:
    def __init__(self, mcp, registry, bus, settings):
        self._mcp = mcp
        self._registry = registry
        self._bus = bus
        self._settings = settings  # callable returning dict(base_url, api_key, model, language)
        self._messages = []
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self._running = False
        self._rpc_id = 0

    def busy(self):
        return self._running

    def submit(self, text, attachments):
        with self._lock:
            if self.busy():
                return False
            self._stop = False
            self._running = True
            self._bus.emit("user", text=text, attachments=[{"name": a["name"], "kind": a["kind"]} for a in attachments])
            self._messages.append({"role": "user", "content": self._user_content(text, attachments)})
            self._thread = threading.Thread(target=self._run, name="AIAssistantAgent", daemon=True)
            self._thread.start()
            return True

    def cancel(self):
        self._stop = True

    def reset(self):
        self._stop = True
        if self._thread is not None:
            self._thread.join(5)
        self._messages = []
        self._bus.clear()

    # ------------------------------------------------------------------ message building

    def _user_content(self, text, attachments):
        parts = []
        notes = []
        for attachment in attachments:
            if attachment["kind"] == "image":
                url = _image_data_url(attachment["path"])
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                    notes.append("Image: %s" % attachment["name"])
            else:
                notes.append("3D model file: %s (path: %s)" % (attachment["name"], attachment["path"]))
        body = text or ""
        if notes:
            body = (body + "\n\n" if body else "") + "Attachments:\n" + "\n".join(notes)
        parts.insert(0, {"type": "text", "text": body or "-"})
        if len(parts) == 1:
            return parts[0]["text"]
        return parts

    def _system(self, settings):
        language = "Russian" if settings.get("language", "ru") == "ru" else "English"
        return {"role": "system", "content": SYSTEM_PROMPT.replace("{language}", language)}

    # ------------------------------------------------------------------ loop

    def _run(self):
        settings = self._settings()
        client = LLMClient(settings.get("base_url"), settings.get("api_key"), settings.get("model"))
        tools = self._registry.openai_tools()
        status = "done"
        try:
            for _ in range(MAX_ROUNDS):
                reply = self._complete(client, tools, settings)
                message = {"role": "assistant", "content": reply["content"] or None}
                if reply["tool_calls"]:
                    message["tool_calls"] = reply["tool_calls"]
                self._messages.append(message)
                if not reply["tool_calls"]:
                    break
                for call in reply["tool_calls"]:
                    if self._stop:
                        raise Cancelled()
                    self._messages.append(self._execute(call))
            else:
                self._bus.emit("error", code="too_many_steps", text="")
                status = "error"
        except Cancelled:
            status = "cancelled"
            self._close_dangling_calls()
        except LLMError as e:
            status = "error"
            code = "not_configured" if str(e) == "not_configured" else "llm"
            self._bus.emit("error", code=code, text="" if code == "not_configured" else str(e))
            self._close_dangling_calls()
        except Exception as e:
            status = "error"
            self._bus.emit("error", code="internal", text="%s: %s" % (type(e).__name__, e))
            self._close_dangling_calls()
        self._running = False
        self._bus.emit("done", status=status)

    def _complete(self, client, tools, settings):
        streams = {"thinking": None, "message": None}
        buffers = {"thinking": [], "message": []}
        last_flush = [time.time()]

        def flush(force=False):
            if not force and time.time() - last_flush[0] < 0.08:
                return
            last_flush[0] = time.time()
            for kind in ("thinking", "message"):
                if buffers[kind]:
                    text = "".join(buffers[kind])
                    buffers[kind] = []
                    if streams[kind] is None:
                        streams[kind] = self._bus.emit(kind, text=text, streaming=True)
                    else:
                        self._bus.append(streams[kind], "text", text)

        def on_reasoning(delta):
            buffers["thinking"].append(delta)
            flush()

        def on_content(delta):
            if buffers["thinking"] or streams["thinking"] is not None:
                flush(force=True)
            buffers["message"].append(delta)
            flush()

        messages = [self._system(settings)] + self._messages
        try:
            reply = client.chat(messages, tools, on_reasoning, on_content, lambda: self._stop)
        finally:
            flush(force=True)
            for kind in ("thinking", "message"):
                if streams[kind] is not None:
                    self._bus.update(streams[kind], streaming=False)
        if not reply["content"] and not reply["tool_calls"] and streams["message"] is None:
            raise LLMError("empty response")
        return reply

    def _execute(self, call):
        name = call["function"]["name"]
        raw = call["function"].get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except ValueError:
            arguments = None
        seq = self._bus.emit("tool", call_id=call["id"], name=name, args=arguments if arguments is not None else raw,
                             status="running", source="agent")
        if arguments is None or not isinstance(arguments, dict):
            outcome = {"ok": False, "result": {"error": "Arguments are not a valid JSON object"}}
        elif not self._registry.has(name):
            outcome = {"ok": False, "result": {"error": "Unknown tool %s" % name}}
        else:
            self._rpc_id += 1
            response = self._mcp.handle({"jsonrpc": "2.0", "id": self._rpc_id, "method": "tools/call",
                                         "params": {"name": name, "arguments": arguments}},
                                        context={"event_seq": seq})
            if "error" in response:
                outcome = {"ok": False, "result": {"error": response["error"]["message"]}}
            else:
                result = response["result"]
                outcome = {"ok": not result.get("isError"), "result": result.get("structuredContent")}
        self._bus.update(seq, status="ok" if outcome["ok"] else "error", result=outcome["result"])
        content = json.dumps(outcome["result"], ensure_ascii=False)
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + "... (truncated)"
        return {"role": "tool", "tool_call_id": call["id"], "content": content}

    def _close_dangling_calls(self):
        """Every tool call in the history needs a reply, otherwise the next request is rejected."""
        for index in range(len(self._messages) - 1, -1, -1):
            if self._messages[index].get("tool_calls"):
                break
        else:
            return
        answered = set(m.get("tool_call_id") for m in self._messages[index + 1:])
        for call in self._messages[index]["tool_calls"]:
            if call["id"] in answered:
                continue
            self._messages.append({"role": "tool", "tool_call_id": call["id"],
                                   "content": json.dumps({"error": "cancelled by the user"})})


def _image_data_url(path):
    """JPEG/PNG as base64 data URL; large photos are downscaled to keep requests small."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    try:
        from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
        from PyQt5.QtGui import QImage
        image = QImage()
        if image.loadFromData(data) and max(image.width(), image.height()) > IMAGE_MAX_SIDE:
            image = image.scaled(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            array = QByteArray()
            buffer = QBuffer(array)
            buffer.open(QIODevice.WriteOnly)
            image.save(buffer, "JPEG", 88)
            data = bytes(array)
            mime = "image/jpeg"
    except Exception:
        pass
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))
