# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Dialog loop: user message -> LLM -> MCP tool calls -> LLM ... -> answer.

Everything the model does is published to the chat window as it happens: reasoning
(collapsed in the UI), answer text, each tool call with arguments, result and changes.
"""

import base64
import json
import os
import re
import threading
import time
import uuid

from .llm import Cancelled, LLMClient, LLMError
from .orientation import summary_text as orientation_text, wants_own_pose
from .intent import clear_intent
from .execution import ExecutionConflict, ExecutionDeferred, PlanExecution

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
# The routing and proofreading calls are optional: a slow reply falls back instead of holding the turn.
ROUTER_TIMEOUT = 20
PROOFREAD_TIMEOUT = 45

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
- Material not fixed: when neither the specification nor the user names the material (and the user did not ask to
  keep the current one), call compare_materials with the same source as plan_print instead of asking about the
  spool. It plans the best feasible materials with their own presets and estimates print time and material
  before slicing (the open project does not change); the user picks one in a card; the chosen plan is applied and the choice is the consent to execute: then verify_plan, slice,
  export_gcode and save_project without asking again. If the user asks for another material without naming it, call
  compare_materials with exclude = the current material. A free answer from the card comes back as text: follow it.

Choose the workflow
- Planning and execution are separate. First calculate and explain the proposal. The APPLICATION always asks
  the user ONCE whether to move to execution before any changes after a core plan. That answer authorizes the
  whole current task: applying, verification, slicing, saving and recovery within the same task. Recalculating
  the same task or switching to settings-only application does NOT require another confirmation. Read an existing
  plan with get_plan and its plan_id; never plan_print just to read it. Do not ask this question yourself
  and never claim to have applied anything before a successful tool result. The application also includes the
  5D warning and a warning about global project settings in that confirmation when relevant. If the user stays
  in planning, the turn ends without the remaining actions; revise the plan only on a new request.
A. Empty scene and an attached STL (with or without a specification): list_model_sources -> plan_print with the
   STL attachment_id and, for an attached DOCX/TXT, its spec_attachment_id (otherwise the user's original text as
   task_text) -> tell in 1-3 sentences: preset, material, mode, what changes over the preset, missing inputs ->
   apply_plan -> verify_plan (must pass) -> slice -> export_gcode -> save_project. Do not load the model first.
   For a 5D plan (cylindrical/cylindrical_full), the application includes the unverified kinematics/collisions
   warning in its execution confirmation and sets confirm_unverified_5d only after the actual answer.
B. Objects already in the scene (an open project) and the user wants to improve or change it: work on THIS
   project; never ask for a new or empty project and never delete, move or retune objects on your own.
   - One model whose original STL is known (list_model_sources: can_plan_original_stl): plan_print with its
     object_id -> apply_plan. It reorients that same object in place to the planned orientation and position and
     puts the preset and the changes in one Undo step (5D: application confirmation as in A).
   - If apply_plan refuses because of other models, own modifiers/per-object settings or a changed mesh, or the
     user does not want the part moved: explain applying only the preset and settings to the whole project,
     then apply_plan_to_project; the application asks about this scope before execution (objects stay where
     they are; revert_plan_changes undoes it).
     If outside_build_volume lists objects afterwards, tell the user and offer arrange_all.
   - No original STL (OBJ/3MF/edited mesh) or small explicit edits such as "infill 30%": manual tools, preset
     first (list_profiles, select_profile, set_material), then as few set_settings as needed.
C. Explicit manual instructions: do exactly them with manual tools.
- An infeasible plan is never applied: explain which requirement fails and offer what the user can change.

Orientation and strength
- Orientation is a main lever for stiffness and strength: FDM parts are weakest and least stiff across the
  layers, so loads should run along the layers. The core weighs this together with supports, height and surface
  steps; its result has orientation (chosen up direction, interlayer_exposure 0..1 where 0 = loads along the
  layers, choice_note, auto_best for comparison).
- The user asks for a stronger/stiffer part or says the part is loaded: plan_print with orientation="strength",
  then apply_plan; this may turn the part on the plate, which is what the user asked for.
- The user wants their own pose or asks to rotate the part: rotate_object exactly as asked, then plan_print with
  that object_id and orientation="keep_current", then apply_plan (the part keeps that rotation and drops onto the
  plate; settings and supports follow the new pose). Compare its interlayer_exposure with auto_best and say in
  one line if the core's own orientation would carry the load better.
- In 3D, moving a part on the plate after apply_plan keeps the plan valid; rotating it needs a new plan_print
  (keep_current). In 5D the part axis follows the tooling axis and is not rotated by hand.
- The application checks the pose of every part before slicing and saving (optimize_orientation): stiffness from the
  core's FE model when planning inputs cover the part, otherwise from its load screen; print time and material
  estimated before slicing (the project is untouched while poses are compared). It turns a part by its rules and never changes a pose the user set. Do not rotate parts yourself
  to optimise them; report its result (orientation_check in the slice result) in one line, including whether
  stiffness came from FE or from the core's screen.
- Report the orientation in words ("деталь положена на бок: нагрузка идёт вдоль слоёв").

Ask when unsure
- Call ask_user when the answer changes the result and tools cannot tell you: 3D vs 5D unclear before any core
  calculation, priority strength vs speed vs quality, a preset that needs input, rotating an existing
  part the user did not ask to rotate or strengthen. The execution card includes the unverified 5D warning.
- A preset provides process defaults, not the physically available spool, service load/direction/fixture,
  operating temperature/environment or required critical tolerances. Read these from the specification and
  explicit user answers. When a missing condition is needed for the requested outcome, ask before deciding;
  never infer it from the part name or invent a number. A selected material profile alone does not prove
  that material is loaded. If the user already specified the material or asked to keep the current one,
  use it without asking the same question again. When the material is open, compare_materials replaces the question
  about the spool: the user picks a material he has from the prepared variants.
- Questions come at the START of the work, all at once, like a short interview: after the project is read and
  plan_print has chosen the preset, collect every condition that is still missing or ambiguous and ask them in ONE
  ask_user call with several questions (2-4 short options each, the recommended one first; the user can also
  type an own answer). Then use the answers: plan_print again with task_text = the original text + a line
  "Ответы пользователя: ..." when they change the task, then apply. Ask again later only if a new problem appears.
- Never ask what the specification, the attachments or the slicer state already answer, and never ask to
  confirm choices the core made from the specification (material, preset, settings): explain them instead.
  The separate application-owned question about moving from planning to execution is always required.
- Never ask about what plan_print has already decided: its printing mode (3D/5D) and every entry of
  setting_changes (justification explicit = required by the core, requirement = from the specification,
  constraint, load class). These follow from the part, the specification and the core rules; only the user's
  own new demand changes them. Ask only about the physical conditions the core cannot know (load, temperature,
  environment, critical sizes; the spool only when the material is already chosen) and about its needs_input items.
  Never offer to delete or move the user's other models; offer applying only the preset and settings to the
  project, or that the user clears the scene himself.

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
- For 5D, slice automatically returns trajectory_validation for the actual G-code (including travel).
  Report its individual checks and missing inputs. A failed check blocks export; fix the reported line/cause.
  Unknown checks allow an analysis export with a validation JSON report; call it unverified for those checks.
  validate_5d_trajectory can refresh the report after machine contract inputs change without re-slicing.
  Do not say no kinematic analysis was done when this result exists: distinguish completed checks from missing
  calibration, controller macros, full head/fixture/printed-volume collision coverage and physical qualification.
- Core results are proposals: never claim physical optimality, certified strength or verified 5D; keep
  needs_input/needs_calibration/infeasible visible; physical_print_approval stays false.

Talking to the user
- Questions asking why/how/what do not authorize changes or a clarification card. Answer them first.
  Only an explicit request for work authorizes tools; if the message asks both, explain before acting.
- Before each batch of tool calls write ONE short sentence (at most 15 words) saying what you are doing now,
  e.g. "Подбираю пресет и настройки на прочность". No reasoning, no tool or key names in that sentence.
- Final answer: short plain text with a few bullets: STE preset and why, material, mode, the changes over the
  preset with one-line reasons, orientation, then print time and material, then the saved files. Name settings
  in words ("стенки 2 → 4", "заполнение 20 → 100 %"), never by keys. No internal names (plan_print,
  attachment_id, canonical core): say "расчёт ядра" / "core calculation".
- Nothing is printed here: the result is a sliced G-code and a saved project. Say "подготовлена к печати" /
  "ready to print", never "напечатана" / "printed".
- Reply in {language}; do not mix languages."""


ROUTER_PROMPT = """You route messages in a chat inside a 3D-printing slicer. Decide what the user's LAST message needs.
answer: the user only asks for information or an explanation (what is set now, why, what something means, how long,
a comparison) and does not ask to change anything.
act: the user asks to do, change, set up, prepare, slice, export, rotate, apply, fix or continue something, gives
data for the task, or agrees to the assistant's proposal ("да", "давай", "делай").
The input is JSON: classify current_user_message; previous_messages are context only, not renewed instructions.
Attachments alone do not authorize work when the user asks a question about them.
"Почему так долго", "Можно ли ускорить?", "Как уменьшить время?", "Explain how to rotate" are answer.
"Ускорь печать", "Почему долго? Уменьши заполнение", "Can you rotate the part?" are act.
If there is no clear request or agreement to work, choose answer. Never turn an explanation into a task.
Reply with one word: answer or act."""

ANSWER_PROMPT = """You are the AI assistant inside STE Slicer (Stereotech). Answer the user's question now in {language},
briefly and concretely, using the conversation and the current slicer state below. You cannot change anything in this
reply: never say that you are changing or will now change something. Answer the actual question FIRST; never replace
the explanation with a question, an interview or a request to choose an option. If information is missing, say what
is known and what cannot be determined. Do not ask the user to decide on changes. You may describe a possible
improvement in one short declarative sentence at the end; do not begin work or ask for confirmation.
Explain likely causes with their limits; do not invent timings or claim measured strength from slicer settings.
Name settings in words, not by keys.
Current slicer state, read by the application just now:
{state}"""

PROOFREAD_PROMPT = """You check a chat reply written in {language} for SEVERE errors only:
- words or characters from another language or script (for example Chinese characters, or an English word inside a
  Russian sentence); technical names stay as they are: PETG, PLA, TPU, STL, 3MF, G-code, 3D/5D, file paths, preset,
  material and profile names, anything in backticks;
- wrong gender, number or case agreement that makes a phrase hard to read;
- garbled or glued words.
Style, punctuation and wording choices are NOT errors. If there is no severe error reply exactly: OK
Otherwise reply with the full corrected text only, keeping markdown, line breaks, numbers, units, paths and meaning."""

NAME_PROMPT = """Name the print variant the assistant has just prepared, so the user can tell it apart in a list of
variants: 1-3 words in {language}, for example "Быстрая печать", "Качество подольше", "Прочная PETG" (English: "Fast draft",
"Fine quality"). Base it on what the user asked and on the changes. Reply with the name only, without quotes."""
NAME_MAX_CHARS = 40
# The user asks for a stronger/stiffer part: the orientation check takes the stiffest pose.
STRENGTH_REQUEST = re.compile(r"прочн|жёстк|жестк|нагруж|stronger|strength|stiff", re.I)
# Tools after which the pose is checked (the result must not be sliced or saved in a worse pose).
POSE_CHECK_BEFORE = ("slice", "export_gcode", "save_project")
# "Поменяй материал на другой": the current material is not offered again.
CHANGE_MATERIAL = re.compile(r"(?:друг|ин)\w*\s+материал|(?:поменя|смени|замени|измени)\w*\s+(?:\w+\s+)?материал|"
                             r"(?:another|different|other)\s+material|change\s+(?:the\s+)?material", re.I)

FOREIGN_SCRIPT = re.compile(u"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
GLUED_WORDS = re.compile(u"[а-яё][a-z]|[a-z][а-яё]", re.IGNORECASE)
# A lowercase English word inside Russian text ("допустима slight подгонка"); names and units are kept.
LATIN_WORD = re.compile(r"(?<![\w./\\-])[a-z]{3,}(?![\w./\\-])")
CYRILLIC = re.compile(u"[а-яё]", re.IGNORECASE)
TECH_WORDS = frozenset("""pla petg abs asa tpu pa pc pp pva hips nylon carbon stl obj gcode mm cm sec min max brim skirt
raft none classic cylindrical full ste cura fdm usb wifi api mcp docx txt json""".split())


class Agent:
    on_turn_start = None  # set by the chat service: a turn begins (before its user message is recorded)
    on_turn_done = None  # set by the chat service: saves the chat after every turn
    before_work = None   # set by the chat service: called before the agent works on the project
    after_work = None    # set by the chat service: (namer, language, should_stop) after finished work

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
        self._kinds = []
        self._turn_text = None  # Original message, without appended attachment contents.
        self._final = None  # (event seq, text) of the last streamed answer
        self._note = None  # told to the model with the next message (e.g. another model chosen in the tree)
        self._pose = {"checked": False, "worked": False, "user_pose": False, "plan": None}  # this turn's pose check
        self._execution = PlanExecution(registry, settings)

    def busy(self):
        return self._running

    def submit(self, text, attachments):
        with self._lock:
            if self.busy():
                return False
            self._stop = False
            self._running = True
            self._kinds = [a["kind"] for a in attachments]
            self._turn_text = text or ""
            self._execution.begin_turn()
            self._pose = {"checked": False, "worked": False, "user_pose": wants_own_pose(text), "plan": None}
            if self.on_turn_start is not None:
                self.on_turn_start()
            self._bus.emit("user", text=text, attachments=[{"name": a["name"], "kind": a["kind"], "id": a.get("id")}
                                                           for a in attachments])
            content = self._user_content(text, attachments)
            if self._note:
                if isinstance(content, list):
                    content[0]["text"] += "\n\n" + self._note
                else:
                    content += "\n\n" + self._note
                self._note = None
            self._messages.append({"role": "user", "content": content})
            self._thread = threading.Thread(target=self._run, name="AIAssistantAgent", daemon=True)
            self._thread.start()
            return True

    def cancel(self):
        self._stop = True
        if self._registry.core is not None:
            self._registry.core.cancel()
        if self._registry.questions is not None:
            self._registry.questions.cancel_all()

    def messages(self):
        return list(self._messages)

    def note(self, text):
        """Context for the model, sent with the user's next message (the history is not rewritten)."""
        self._note = text

    def load(self, messages):
        """History of a saved chat; the chat feed itself is restored by the service.

        A chat saved while the slicer closed mid-turn may end with tool calls that have no result;
        they are closed so the next request is accepted."""
        self._messages = list(messages)
        self._execution = PlanExecution(self._registry, self._settings)
        self._close_dangling_calls()

    def reset(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(5)
        if self.busy():
            return False  # A late reply must not append to a newly reset conversation.
        self._messages = []
        self._execution = PlanExecution(self._registry, self._settings)
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
                spec_text = attachment.get("text") or ""
                notes.append("Specification file: %s (spec_attachment_id: %s, %d characters; plan_print uses its full "
                             "original text). Text:\n%s%s" % (attachment["name"], attachment["id"], len(spec_text),
                                                              spec_text[:SPEC_PREVIEW_CHARS],
                                                              "\n…" if len(spec_text) > SPEC_PREVIEW_CHARS else ""))
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
        return {"role": "system", "content": SYSTEM_PROMPT.replace("{language}", language) + self._execution.continuation()}

    # ------------------------------------------------------------------ loop

    def _run(self):
        settings = self._settings()
        client = LLMClient(settings.get("base_url"), settings.get("api_key"), settings.get("model"))
        status = "done"
        self._final = None
        try:
            if self._route(client) == "answer":
                self._answer(client, settings)
                self._proofread(client, settings)
                raise _Finished()
            available_tools = self._registry.openai_tools()
            if self.before_work is not None:
                try:
                    self.before_work()
                except Exception as e:
                    _log("before_work", e)
            self._read_context(settings)
            for _ in range(MAX_ROUNDS):
                reply = self._complete(client, self._execution.tools(available_tools), settings)
                message = {"role": "assistant", "content": reply["content"] or None}
                if reply["tool_calls"]:
                    message["tool_calls"] = reply["tool_calls"]
                self._messages.append(message)
                if not reply["tool_calls"]:
                    # Checked before a confirmation card too: that text stays in the chat.
                    self._proofread(client, settings)
                    if self._execution.offer_at_end():
                        self._execution.confirm("apply_plan", lambda: self._stop)
                        self._messages.append({"role": "user", "content":
                            "Application: the user explicitly chose to move from planning to execution in the confirmation card. "
                            "Use the approved plan_id=%s; apply, verify, slice and save without any further confirmation. "
                            "Read it with get_plan if needed; do not recalculate just to retrieve it." %
                            self._execution.plan["plan_id"]})
                        continue
                    break
                for call in reply["tool_calls"]:
                    if self._stop:
                        raise Cancelled()
                    self._messages.append(self._execute(call))
            else:
                self._bus.emit("error", code="too_many_steps", text="")
                status = "error"
            if status == "done" and self._pose["worked"] and not self._pose["checked"]:
                # Work without slicing (e.g. manual settings): the pose is still checked, and said in one line.
                text = orientation_text(self._check_pose(), settings.get("language", "ru"))
                if text:
                    self._messages.append({"role": "assistant", "content": text})
                    self._bus.emit("message", text=text, streaming=False)
            if status == "done" and self.after_work is not None:
                # The finished result becomes a node of the chat's model tree.
                try:
                    self.after_work(lambda rows: self._name_model(client, settings, rows),
                                    settings.get("language", "ru"), lambda: self._stop)
                except Exception as e:
                    _log("after_work", e)
                if self._stop:
                    raise Cancelled()
        except _Finished:
            pass
        except ExecutionDeferred:
            self._close_dangling_calls("Not executed: the user chose to stay in planning")
            text = ("Остаёмся в планировании. Дальнейшие действия по плану не запускаю."
                    if settings.get("language", "ru") == "ru" else
                    "Staying in planning. Further execution of the plan has been stopped.")
            self._messages.append({"role": "assistant", "content": text})
            self._bus.emit("message", text=text, streaming=False)
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
        if self.on_turn_done is not None:
            try:
                self.on_turn_done()
            except Exception:
                pass

    # ------------------------------------------------------------------ answer or act

    def _route(self, client):
        """answer: a question about the project, answered without tools; act: work on the project."""
        text = self._turn_text
        if text is None:
            text = next((m["content"] for m in reversed(_text_history(self._messages)) if m["role"] == "user"), "")
        intent = clear_intent(text)
        if intent is not None:
            return intent
        if not text.strip() and any(kind in ("model", "spec") for kind in self._kinds):
            return "act"
        # Attached specifications and old task requests are context, not current authorization.
        history = _text_history(self._messages[:-1])[-4:]
        payload = {"previous_messages": history, "current_user_message": text, "attachment_kinds": self._kinds}
        try:
            word = client.text([{"role": "system", "content": ROUTER_PROMPT},
                                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], max_tokens=8,
                               timeout=ROUTER_TIMEOUT)
        except LLMError:
            return "answer"
        # A timeout, empty, malformed or truncated classifier reply never grants tools.
        return "act" if (word or "").strip().lower().rstrip(".!") == "act" else "answer"

    def _answer(self, client, settings):
        state = {}
        for name in ("get_state", "list_profiles"):
            if self._registry.has(name):
                state[name] = self._registry.call(name, {}, None)["result"]
        # Explaining print time needs the existing estimate, not a new slice.
        if state.get("get_state", {}).get("slice_state") == "done" and self._registry.has("get_slice_result"):
            try:
                state["get_slice_result"] = self._registry.call("get_slice_result", {}, None)["result"]
            except Exception:
                state["get_slice_result"] = {"unavailable": "The finished slice changed while reading its estimate."}
        language = "Russian" if settings.get("language", "ru") == "ru" else "English"
        profiles = state.pop("list_profiles", {})
        snapshot = json.dumps(state, ensure_ascii=False)[:10000] + "\nNative profiles:\n" + json.dumps(profiles, ensure_ascii=False)[:2000]
        system = ANSWER_PROMPT.replace("{language}", language).replace(
            "{state}", snapshot)
        reply = self._complete(client, None, settings,
                               messages=[{"role": "system", "content": system}] + _text_history(self._messages))
        self._messages.append({"role": "assistant", "content": reply["content"] or ""})

    # ------------------------------------------------------------------ model tree

    def _name_model(self, client, settings, rows):
        """Short name of a finished variant; the first words of the request if the model gives none."""
        language = "Russian" if settings.get("language", "ru") == "ru" else "English"
        request = (self._turn_text or "").strip()
        summary = "; ".join("%s %s → %s" % (r["label"], r.get("old") or "", r["new"]) for r in rows) or "-"
        name = ""
        try:
            name = client.text([{"role": "system", "content": NAME_PROMPT.replace("{language}", language)},
                                {"role": "user", "content": "Request: %s\nChanges: %s" % (request[:600], summary)}],
                               max_tokens=16, timeout=ROUTER_TIMEOUT)
        except LLMError:
            pass
        name = " ".join((name or "").strip().strip("\"'«»“”.").split())
        if not name or len(name) > NAME_MAX_CHARS or FOREIGN_SCRIPT.search(name):
            words = re.findall(r"[^\W_]+", request, re.UNICODE)[:3]
            name = " ".join(words).capitalize() or ("Модель" if language == "Russian" else "Model")
        return name[:NAME_MAX_CHARS]

    # ------------------------------------------------------------------ proofreading

    def _fix_text(self, client, settings, text):
        """Corrected text when the reply has a severe language error, else None."""
        language = "Russian" if settings.get("language", "ru") == "ru" else "English"
        try:
            fixed = client.text([{"role": "system", "content": PROOFREAD_PROMPT.replace("{language}", language)},
                                 {"role": "user", "content": text}], max_tokens=min(4000, len(text) * 2 + 64),
                                timeout=PROOFREAD_TIMEOUT)
        except LLMError:
            return None
        fixed = (fixed or "").strip()
        if not fixed or fixed.upper().rstrip(".!") == "OK" or not _safe_fix(text, fixed):
            return None
        return fixed

    def _proofread(self, client, settings):
        """The answer is shown as it streams; a severe error is then replaced in place."""
        if not self._final or not self._final[1].strip():
            return
        seq, text = self._final
        fixed = self._fix_text(client, settings, text)
        if fixed is None:
            return
        self._bus.update(seq, text=fixed, corrected=True)
        for message in reversed(self._messages):
            if message.get("role") == "assistant" and (message.get("content") or "").strip() == text:
                message["content"] = fixed
                break

    def _clean_questions(self, client, settings, arguments):
        """All texts of a question card are proofread in one call before the card is shown.

        The texts go as numbered lines; when the reply keeps the numbering, each line is taken if it
        keeps its facts (same numbers, similar length). Foreign script is removed in any case.
        """
        items = [i for i in (arguments.get("questions") if isinstance(arguments.get("questions"), list) else [])
                 if isinstance(i, dict)]
        slots = []
        for item in items:
            if isinstance(item.get("question"), str):
                slots.append((item, "question", None))
            for index, option in enumerate(item.get("options") if isinstance(item.get("options"), list) else []):
                if isinstance(option, str):
                    slots.append((item, "options", index))
        if not slots:
            return

        def get(slot):
            item, key, index = slot
            return item[key] if index is None else item[key][index]

        def put(slot, text):
            item, key, index = slot
            if index is None:
                item[key] = text
            else:
                item[key][index] = text

        texts = [" ".join(get(slot).split()) for slot in slots]
        block = "\n".join("%d) %s" % (n + 1, text) for n, text in enumerate(texts))
        fixed = self._fix_text(client, settings, block)
        if fixed is not None:
            lines = [l for l in fixed.splitlines() if l.strip()]
            parsed = [re.sub(r"^\s*%d\)\s*" % (n + 1), "", l, count=1) for n, l in enumerate(lines)]
            if len(parsed) == len(texts) and all(re.match(r"^\s*%d\)" % (n + 1), l) for n, l in enumerate(lines)):
                texts = [new if _safe_line(old, new) else old for old, new in zip(texts, parsed)]
        for slot, text in zip(slots, texts):
            put(slot, FOREIGN_SCRIPT.sub("", text).strip() or get(slot))

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
        # Unique ids: the history of a branch is put together from turns of different times.
        calls = [{"id": "context_%s_%s" % (uuid.uuid4().hex[:8], name), "type": "function",
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

    def _complete(self, client, tools, settings, messages=None):
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

        messages = messages or self._request_messages(settings)
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
            elif text.strip() and not (calls and FOREIGN_SCRIPT.search(text)):
                # A step caption in another script ("Планирую最强的…") is not shown; the step has its own name.
                streams["message"] = self._bus.emit("message", text=text.strip(), streaming=False, note=calls)
            if reply is not None and not calls and streams["message"] is not None:
                self._final = (streams["message"], reply["content"].strip())
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
        blocked = None
        if isinstance(arguments, dict) and self._registry.has(name):
            try:
                self._execution.before(name, arguments, lambda: self._stop)
            except ExecutionConflict as e:
                blocked = {"ok": False, "result": {"error": str(e),
                                                  "execution_authorized": self._execution.executing}}
            if self._stop:
                raise Cancelled()
        if name == "ask_user" and isinstance(arguments, dict) and blocked is None:
            settings = self._settings()
            self._clean_questions(LLMClient(settings.get("base_url"), settings.get("api_key"), settings.get("model")),
                                  settings, arguments)
        if name == "compare_materials" and isinstance(arguments, dict) and CHANGE_MATERIAL.search(self._turn_text or ""):
            arguments["replace_current"] = True
        check = None
        if name in POSE_CHECK_BEFORE and blocked is None and not self._pose["checked"]:
            check = self._check_pose()
        seq = self._bus.emit("tool", call_id=call["id"], name=name, args=arguments if arguments is not None else raw,
                             status="running", source="agent")
        if blocked is not None:
            outcome = blocked
        elif arguments is None or not isinstance(arguments, dict):
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
        if check is not None and isinstance(outcome["result"], dict):
            outcome["result"]["orientation_check"] = check
        self._bus.update(seq, status="ok" if outcome["ok"] else "error", result=outcome["result"])
        if outcome["ok"]:
            self._execution.observe(name, outcome["result"])
            self._track(name, arguments, outcome["result"])
        content = json.dumps(outcome["result"], ensure_ascii=False)
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + "... (truncated)"
        return {"role": "tool", "tool_call_id": call["id"], "content": content}

    # ------------------------------------------------------------------ pose check and material choice

    def _track(self, name, arguments, result):
        from .execution import EXECUTION_TOOLS
        if name in EXECUTION_TOOLS or name == "compare_materials":
            self._pose["worked"] = True
        if name == "plan_print" and isinstance(arguments, dict):
            self._pose["plan"] = arguments
            if arguments.get("orientation") == "keep_current":
                self._pose["user_pose"] = True
        elif name == "rotate_object":
            self._pose["user_pose"] = True
        elif name == "optimize_orientation":
            self._pose["checked"] = True
        elif name == "compare_materials" and isinstance(result, dict) and result.get("status") == "applied":
            self._execution.approve(result.get("plan"))

    def _check_pose(self):
        """The application's pose check of this turn, shown as its own step (once per turn)."""
        self._pose["checked"] = True
        if not self._registry.has("optimize_orientation"):
            return None
        plan = self._pose["plan"] or {}
        arguments = {"task_text": plan.get("task_text") or self._turn_text or "",
                     "priority": "strength" if plan.get("orientation") == "strength" or
                     STRENGTH_REQUEST.search(self._turn_text or "") else "balanced",
                     "keep_pose": self._pose["user_pose"]}
        if plan.get("spec_attachment_id"):
            arguments["spec_attachment_id"] = plan["spec_attachment_id"]
        seq = self._bus.emit("tool", call_id="pose_%s" % uuid.uuid4().hex[:8], name="optimize_orientation",
                             args=arguments, status="running", source="app")
        outcome = self._registry.call("optimize_orientation", arguments,
                                      {"event_seq": seq, "cancelled": lambda: self._stop})
        result = outcome["result"]
        if not outcome["ok"]:
            # An application step, not the user's request: a failure only means the pose stays as it is.
            _log("optimize_orientation", RuntimeError((result or {}).get("error")))
            result = {"status": "skipped", "reason": (result or {}).get("error")}
        self._bus.update(seq, status="ok", result=result)
        if self._stop:
            raise Cancelled()
        return result

    def _close_dangling_calls(self, reason="cancelled by the user"):
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
                                   "content": json.dumps({"error": reason})})


def _log(where, error):
    try:
        from UM.Logger import Logger
        Logger.log("w", "AI Assistant: %s failed: %s: %s", where, type(error).__name__, error)
    except Exception:
        pass


class _Finished(Exception):
    """The turn ended without the tool loop (answer mode)."""


def _text_history(messages):
    """User and assistant text only: tool calls and results are dropped for tool-less requests."""
    out = []
    for message in messages:
        if message.get("role") not in ("user", "assistant") or not message.get("content"):
            continue
        content = message["content"]
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        out.append({"role": message["role"], "content": content[:4000]})
    return out


def _suspicious(text, language="ru"):
    if FOREIGN_SCRIPT.search(text) or GLUED_WORDS.search(text):
        return True
    return language == "ru" and bool(CYRILLIC.search(text)) and \
        any(word not in TECH_WORDS for word in LATIN_WORD.findall(text))


def _safe_fix(original, fixed):
    """A correction may fix words, not change facts: same numbers, similar length, no foreign script."""
    if FOREIGN_SCRIPT.search(fixed):
        return False
    if not 0.7 <= len(fixed) / float(max(1, len(original))) <= 1.4:
        return False
    numbers = lambda t: sorted(re.findall(r"\d+(?:[.,]\d+)?", t))
    return numbers(original) == numbers(fixed)


def _safe_line(original, fixed):
    """_safe_fix for one line of a card; a short option may shrink a lot (e.g. stray characters removed)."""
    if len(original) < 12:
        numbers = lambda t: sorted(re.findall(r"\d+(?:[.,]\d+)?", t))
        return bool(fixed.strip()) and not FOREIGN_SCRIPT.search(fixed) and numbers(original) == numbers(fixed)
    return _safe_fix(original, fixed)


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
