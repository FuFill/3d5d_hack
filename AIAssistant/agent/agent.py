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
SPEC_PREVIEW_CHARS = 8000
# Text that comes with tool calls is a short step caption; longer text is shown as reasoning.
CAPTION_MAX_CHARS = 280
# Read by the application at the start of every turn.
CONTEXT_TOOLS = ("get_state", "list_model_sources", "list_profiles")
OLD_TOOL_RESULT_CHARS = 3000
RETRY_DELAYS = (2, 6)

SYSTEM_PROMPT = """You are the AI assistant built into STE Slicer (Stereotech slicer, a fork of Cura 3.6).
You prepare 3D and 5D FDM prints through tools that change the real slicer state.

Basics
- Printing modes: only classic (3D), cylindrical (5D Spiral), cylindrical_full (5D).
- Main printer: Stereotech Hybrid 530 V5.2.4; extruder 0 prints plastic, extruder 1 is the fiber extruder.
- Coordinates: printer axes in mm, Z up, origin at the build plate centre.
- At the start of every turn the application has already read the project for you: get_state (printer,
  mode, active preset, extruders and nozzles, key settings, objects), list_model_sources (attached and scene
  files) and list_profiles (installed presets). Use these results; call them again only after a change.
- Then read everything the user attached: specification text (DOCX/TXT), images, models.

Presets come first
- Every print is built on a native STE preset (installed quality profile + material + nozzle). The core
  calculation (plan_print) picks the compatible preset itself and changes only what the task justifies on top of
  it. Its result has base_preset (the chosen STE preset), setting_changes (the few values changed over the preset,
  each with base_value -> effective_value and a reason) and material_options.
- Never rebuild a preset by hand with many set_settings calls. Manual setting changes are for explicit user
  requests or when the core cannot be used; even then pick a fitting STE profile first (list_profiles,
  select_profile, set_material) and change as little as possible on top.
- If base_preset.status is needs_input (e.g. the material needs another nozzle, no compatible profile): do not
  apply. ask_user with its options in plain words. If the user says the nozzle is physically installed: set_nozzle,
  then plan_print again. If the user chooses another material: plan_print again with preferred_material from
  material_options. Custom user profiles are used only when allowed by the application settings.

Choose the workflow
A. Empty scene and an attached STL (with or without a specification): list_model_sources -> plan_print with the
   STL attachment_id and, for an attached DOCX/TXT, its spec_attachment_id (otherwise the user's original text as
   task_text) -> tell in 1-3 sentences: preset, material, mode, what changes over the preset, missing inputs ->
   apply_plan -> verify_plan (must pass) -> slice -> export_gcode -> save_project. Do not load the model first.
   For a 5D plan (cylindrical/cylindrical_full) ask_user first whether to apply it although 5D kinematics and
   collisions are not verified by the core; only if yes call apply_plan with confirm_unverified_5d=true.
B. Objects already in the scene (an open project) and the user wants to improve or change it: work on THIS
   project; never ask for a new or empty project and never delete, move or retune objects on your own.
   - One model whose original STL is known (list_model_sources: can_plan_original_stl): plan_print with its
     object_id -> apply_plan. It reorients that same object in place to the planned orientation and position and
     puts the preset and the changes in one Undo step (5D: ask_user and confirm_unverified_5d as in A).
   - If apply_plan refuses because of other models, own modifiers/per-object settings or a changed mesh, or the
     user does not want the part moved: ask_user whether to apply only the preset and settings to the whole
     project; if yes apply_plan_to_project (objects stay where they are; revert_plan_changes undoes it).
     If outside_build_volume lists objects afterwards, tell the user and offer arrange_all.
   - No original STL (OBJ/3MF/edited mesh) or small explicit edits such as "infill 30%": manual tools, preset
     first (list_profiles, select_profile, set_material), then as few set_settings as needed.
C. Explicit manual instructions: do exactly them with manual tools.
- An infeasible plan is never applied: explain which requirement fails and offer what the user can change.

Ask when unsure
- Call ask_user when the answer changes the result and tools cannot tell you: material not given and several
  fit, 3D vs 5D unclear, priority strength vs speed vs quality, a preset that needs input, rotating an existing
  part, applying unverified 5D.
- Short question, 2-4 short options with the recommended one first. Combine what you need into ONE question
  when possible; at most 2 per task. Never ask what the specification, the attachments or the slicer state
  already answer, and never ask to confirm choices the core made from the specification (material, preset,
  settings): explain them instead. Never offer to delete or move the user's other models; offer applying only
  the preset and settings to the project, or that the user clears the scene himself.

Settings
- Known keys, use them directly: layer_height, line_width, wall_line_count, top_layers, bottom_layers,
  top_bottom_thickness, infill_sparse_density, infill_pattern, infill_wall_line_count, infill_overlap,
  material_print_temperature, material_bed_temperature, speed_print, speed_wall_0, speed_infill, cool_fan_speed,
  support_enable, support_type, adhesion_type, brim_width, retraction_enable.
- For any other setting call find_settings ONCE with all topics in queries. Never guess keys; use enum option
  keys exactly as returned. Respect tool warnings.
- Do not create or save profiles unless the user asks for it.

Execution
- Call each tool once per need: no repeated reads, no second export.
- If a tool fails, read the error, fix the cause and retry once; if it still fails, tell the user plainly.
- After a successful slice: export_gcode and save_project once each, unless the user asked otherwise.
- Core results are proposals: never claim physical optimality, certified strength or verified 5D; keep
  needs_input/needs_calibration/infeasible visible; physical_print_approval stays false.

Talking to the user
- Before each batch of tool calls write ONE short sentence (at most 15 words) saying what you are doing now,
  e.g. "Подбираю пресет и настройки на прочность". No reasoning, no tool or key names in that sentence.
- Final answer: short plain text with a few bullets: STE preset and why, material, mode, the changes over the
  preset with one-line reasons, orientation, then print time and material, then the saved files. Name settings
  in words ("стенки 2 → 4", "заполнение 20 → 100 %"), never by keys. No internal names (plan_print,
  attachment_id, canonical core): say "расчёт ядра" / "core calculation".
- Reply in {language}; do not mix languages."""


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
        self._turn = 0

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
        if self._registry.core is not None:
            self._registry.core.cancel()
        if self._registry.questions is not None:
            self._registry.questions.cancel_all()

    def reset(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(5)
        if self.busy():
            return False  # A late reply must not append to a newly reset conversation.
        self._messages = []
        self._bus.clear()
        return True

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
            elif attachment["kind"] == "spec":
                text = attachment.get("text") or ""
                notes.append("Specification file: %s (spec_attachment_id: %s, %d characters; plan_print uses its full "
                             "original text). Text:\n%s%s" % (attachment["name"], attachment["id"], len(text),
                                                              text[:SPEC_PREVIEW_CHARS],
                                                              "\n…" if len(text) > SPEC_PREVIEW_CHARS else ""))
            else:
                notes.append("3D model file: %s (attachment_id: %s; actual path: %s)" %
                             (attachment["name"], attachment["id"], attachment["path"]))
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
            self._read_context(settings)
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

    def _read_context(self, settings):
        """First step of every turn, done by the application: the open project, presets and attachments.

        The calls go through MCP like the model's own, are shown in the chat as one step and stay in the
        history as tool results, so the model starts from the real state instead of guessing it.
        """
        names = [n for n in CONTEXT_TOOLS if self._registry.has(n)]
        if not names:
            return
        self._turn += 1
        caption = "Читаю проект, пресеты и вложения" if settings.get("language", "ru") == "ru" else \
            "Reading the project, presets and attachments"
        self._bus.emit("message", text=caption, streaming=False, note=True, auto=True)
        calls = [{"id": "context_%d_%s" % (self._turn, name), "type": "function",
                  "function": {"name": name, "arguments": "{}"}} for name in names]
        self._messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        for call in calls:
            if self._stop:
                raise Cancelled()
            self._messages.append(self._execute(call))

    def _request_messages(self, settings):
        """History for the next request: earlier turns keep their text, not their images and long tool output."""
        last_user = max([i for i, m in enumerate(self._messages) if m.get("role") == "user"] or [0])
        messages = [self._system(settings)]
        for index, message in enumerate(self._messages):
            if index < last_user:
                if isinstance(message.get("content"), list):
                    message = dict(message, content=[p if p.get("type") != "image_url" else
                                                     {"type": "text", "text": "[image shown earlier]"}
                                                     for p in message["content"]])
                elif message.get("role") == "tool" and len(message.get("content") or "") > OLD_TOOL_RESULT_CHARS:
                    message = dict(message, content=message["content"][:OLD_TOOL_RESULT_CHARS] + "... (earlier turn)")
            messages.append(message)
        return messages

    def _complete(self, client, tools, settings):
        """Stream one completion. Answer text is held back until it is clearly longer than a step caption,
        so a short sentence that comes with tool calls becomes the caption of the step (a "note")."""
        streams = {"thinking": None, "message": None}
        buffers = {"thinking": [], "message": []}
        state = {"flushed": 0.0, "content": 0}

        def flush(force=False):
            if not force and time.time() - state["flushed"] < 0.08:
                return
            state["flushed"] = time.time()
            for kind in ("thinking", "message"):
                if not buffers[kind]:
                    continue
                if kind == "message" and streams["message"] is None and state["content"] <= CAPTION_MAX_CHARS:
                    continue  # Still possibly a caption: wait for more text or the end of the stream.
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
            if buffers["thinking"]:
                flush(force=True)
            buffers["message"].append(delta)
            state["content"] += len(delta)
            flush()

        messages = self._request_messages(settings)
        reply = None
        try:
            for attempt in range(len(RETRY_DELAYS) + 1):
                try:
                    reply = client.chat(messages, tools, on_reasoning, on_content, lambda: self._stop)
                    break
                except LLMError as e:
                    streamed = streams["message"] is not None or streams["thinking"] is not None or \
                        buffers["message"] or buffers["thinking"]
                    if streamed or not _transient(e) or attempt == len(RETRY_DELAYS):
                        raise
                    deadline = time.time() + RETRY_DELAYS[attempt]
                    while time.time() < deadline:
                        if self._stop:
                            raise Cancelled()
                        time.sleep(0.2)
        finally:
            text = "".join(buffers["message"])
            buffers["message"] = []
            flush(force=True)
            if streams["thinking"] is not None:
                self._bus.update(streams["thinking"], streaming=False)
            calls = bool(reply and reply["tool_calls"])
            if streams["message"] is not None:
                if text:
                    self._bus.append(streams["message"], "text", text)
                self._bus.update(streams["message"], streaming=False, note=calls)
            elif text.strip():
                streams["message"] = self._bus.emit("message", text=text.strip(), streaming=False, note=calls)
        if not reply["content"] and not reply["tool_calls"]:
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
                                        context={"event_seq": seq, "cancelled": lambda: self._stop})
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


def _transient(error):
    """Network failures, rate limits and server errors are retried; bad requests and auth are not."""
    text = str(error)
    if text in ("not_configured", "empty response"):
        return False
    if text.startswith("HTTP "):
        return text[5:8] in ("408", "409", "425", "429") or text[5:6] == "5"
    return True


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
