# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""AI Assistant extension: menu item, settings, local server and the docked chat window."""

import os
import json
import sys
import threading

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Resources import Resources

from .agent.agent import Agent
from .agent.events import EventBus
from .agent.llm import LLMClient, LLMError
from .agent.mainthread import run_on_main
from .agent.mcp import MCPServer
from .agent.server import ChatService
from .agent.tools import SliceMonitor, ToolRegistry
from .agent.window import EmbeddedChat
from .agent.core_bridge import CoreBridge, default_python
from .agent.core_worker import core_path


def local_defaults():
    """Local installer writes paths only. Secret values never enter a bundle."""
    path = os.path.join(os.path.dirname(__file__), "local_config.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        local = json.load(handle)
    values = {}
    if local.get("worker_python"):
        values["worker_python"] = local["worker_python"]
    # Native preset policy is application-owned: not editable from the page or by the model.
    if isinstance(local.get("native_presets"), bool):
        values["native_presets"] = local["native_presets"]
    names = local.get("allowed_quality_changes")
    if isinstance(names, list) and all(isinstance(name, str) for name in names):
        values["allowed_quality_changes"] = list(names)
    env_file = local.get("env_file")
    if env_file and os.path.isfile(env_file):
        if core_path() not in sys.path:
            sys.path.insert(0, core_path())
        from ste_planner.qwen_client import load_env_file
        env = load_env_file(env_file, dict(os.environ))
        for target, keys in (("api_key", ("TIMEWEB_API_KEY", "LLM_API_KEY")),
                             ("base_url", ("QWEN_BASE_URL", "LLM_BASE_URL")),
                             ("model", ("QWEN_MODEL", "LLM_MODEL"))):
            values[target] = next((env[k] for k in keys if env.get(k)), "")
    return values

PREFIX = "ai_assistant/"
DEFAULTS = {
    "base_url": "https://api.timeweb.ai/v1",
    "api_key": "",
    "model": "dashscope/qwen3.8-flash",
    "language": "ru",
    "theme": "light",
    "worker_python": default_python(),
    "planning_inputs_file": "",
    "output_dir": os.path.join(os.path.expanduser("~"), "Documents", "STE AI Assistant"),
}


class Settings:
    """Plugin settings stored in the slicer preferences (the API key is never sent to the page)."""

    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()
        preferences = app.getPreferences()
        for key, default in DEFAULTS.items():
            preferences.addPreference(PREFIX + key, default)
        self._values = dict((key, preferences.getValue(PREFIX + key)) for key in DEFAULTS)
        local = local_defaults()
        for key, value in local.items():
            if key in DEFAULTS and value and (not self._values.get(key) or self._values[key] == DEFAULTS[key]):
                self._values[key] = value
        for key in ("native_presets", "allowed_quality_changes"):
            if key in local:
                self._values[key] = local[key]
        # Local environment is an optional fallback; never return it to the page.
        if not self._values.get("api_key"):
            self._values["api_key"] = os.environ.get("TIMEWEB_API_KEY") or os.environ.get("LLM_API_KEY") or ""

    def get(self, key):
        with self._lock:
            return self._values.get(key) or DEFAULTS[key]

    def values(self):
        with self._lock:
            return dict(self._values)

    def public(self):
        values = self.values()
        key = values.get("api_key") or ""
        return {"base_url": values["base_url"], "model": values["model"], "language": values["language"],
                "theme": values["theme"], "output_dir": values["output_dir"] or DEFAULTS["output_dir"],
                "worker_python": values["worker_python"],
                "planning_inputs_file": values["planning_inputs_file"],
                "has_key": bool(key), "key_hint": ("…" + key[-4:]) if len(key) > 8 else ""}

    def update(self, data):
        changed = {}
        for key in ("base_url", "model", "output_dir", "worker_python", "planning_inputs_file"):
            if isinstance(data.get(key), str):
                changed[key] = data[key].strip()
        if isinstance(data.get("api_key"), str) and data["api_key"].strip():
            changed["api_key"] = data["api_key"].strip()
        if data.get("clear_key"):
            changed["api_key"] = ""
        if data.get("language") in ("ru", "en"):
            changed["language"] = data["language"]
        if data.get("theme") in ("light", "dark"):
            changed["theme"] = data["theme"]
        if changed.get("base_url") and not changed["base_url"].startswith(("http://", "https://")):
            raise ValueError("base_url")
        with self._lock:
            self._values.update(changed)
        run_on_main(self._app, self._store, changed)

    def _store(self, changed):
        preferences = self._app.getPreferences()
        for key, value in changed.items():
            preferences.setValue(PREFIX + key, value)
        self._app.savePreferences()

    def test(self):
        values = self.values()
        try:
            LLMClient(values["base_url"], values["api_key"], values["model"], timeout=30).ping()
            return True, ""
        except LLMError as e:
            return False, str(e)


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()
        self._app = Application.getInstance()
        self._service = None
        self._window = None
        self.setMenuName("AI Assistant")
        self.addMenuItem("Open chat", self.showChat)
        self.addMenuItem("Hide chat", self.hideChat)
        self._app.applicationShuttingDown.connect(self._shutdown)

    def _start(self):
        settings = Settings(self._app)
        bus = EventBus()
        monitor = SliceMonitor(self._app, bus)
        monitor.connect()
        core = CoreBridge(self._app, settings.values)
        registry = ToolRegistry(self._app, monitor, lambda: settings.get("output_dir"), core=core, bus=bus)
        core.resolve_node = registry._node
        mcp = MCPServer(registry)
        agent = Agent(mcp, registry, bus, settings.values)
        self._service = ChatService(bus, agent, mcp, registry, settings)
        registry.upload_sources = self._service.model_uploads
        registry.spec_sources = self._service.spec_uploads
        port = self._service.start()
        profile = os.path.join(Resources.getDataStoragePath(), "ai_assistant_browser")
        self._window = EmbeddedChat(self._app, profile)
        Logger.log("i", "AI Assistant: chat on http://127.0.0.1:%d/, MCP endpoint %s (token in the chat settings)",
                   port, self._service.mcp_url())

    def showChat(self):
        try:
            if self._service is None:
                self._start()
            self._window.open(self._service.url())
        except Exception:
            Logger.logException("e", "AI Assistant: could not open the chat")

    def hideChat(self):
        try:
            if self._window is not None:
                self._window.hide()
        except Exception:
            Logger.logException("e", "AI Assistant: could not hide the chat")

    def _shutdown(self):
        try:
            if self._window is not None:
                self._window.close()
            if self._service is not None:
                self._service.agent.cancel()
                self._service.stop()
        except Exception:
            Logger.logException("w", "AI Assistant: error on shutdown")
