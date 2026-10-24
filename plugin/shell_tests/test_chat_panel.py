"""Chat panel inside the slicer window: fallback and width rules without Qt.

The real embedding (owned browser window over the reserved strip, crop, keyboard)
is checked on Windows with Edge/Chrome by plugin/tools/check_embedded_chat.py.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "AIAssistant"))
from agent import window as W


class Preferences:
    def __init__(self, stored=None):
        self.values = {} if stored is None else {W.WIDTH_PREFERENCE: stored}

    def addPreference(self, key, default):
        self.values.setdefault(key, default)

    def getValue(self, key):
        return self.values.get(key)

    def setValue(self, key, value):
        self.values[key] = value


class App:
    def __init__(self, stored=None):
        self.preferences = Preferences(stored)

    def getPreferences(self):
        return self.preferences

    def getMainWindow(self):
        return None


def test_without_app_browser_the_chat_uses_the_previous_behaviour(monkeypatch):
    opened = []
    monkeypatch.setattr(W, "find_app_browser", lambda: None)
    chat = W.EmbeddedChat(App(), "profile")
    monkeypatch.setattr(chat._fallback, "open", opened.append)
    chat.open("http://127.0.0.1:1/")
    assert opened == ["http://127.0.0.1:1/"] and chat._embedded is False
    chat.open("http://127.0.0.1:1/")                 # stays on the fallback afterwards
    assert len(opened) == 2


def test_unknown_slicer_layout_falls_back_instead_of_covering_the_ui(monkeypatch):
    opened = []
    monkeypatch.setattr(W, "find_app_browser", lambda: "browser.exe")
    chat = W.EmbeddedChat(App(), "profile")          # getMainWindow() is None: layout unknown
    monkeypatch.setattr(chat._fallback, "open", opened.append)
    chat.open("http://127.0.0.1:1/")
    assert opened and chat._embedded is False and chat._panel is None


def test_panel_width_keeps_the_slicer_usable():
    chat = W.EmbeddedChat(App(), "profile")
    assert chat._width == W.PANEL_WIDTH
    assert chat._panel_width(1600) == 400
    assert chat._panel_width(800) == W.PANEL_MIN_WIDTH          # 800 - 600 < minimum
    chat._width = 2000
    assert chat._panel_width(3000) == W.PANEL_MAX_WIDTH
    assert chat._panel_width(1200) == 1200 - W.MIN_SLICER_WIDTH


def test_resized_width_is_remembered():
    app = App(stored=520)
    chat = W.EmbeddedChat(app, "profile")
    assert chat._width == 520
    chat._on_resize_finished(610)
    assert app.preferences.values[W.WIDTH_PREFERENCE] == 610
    assert W.EmbeddedChat(app, "profile")._width == 610


def test_qml_panel_ships_with_the_plugin():
    assert Path(W.QML_PANEL).is_file()
    text = Path(W.QML_PANEL).read_text(encoding="utf-8")
    assert "signal resized" in text and "signal resizeFinished" in text
