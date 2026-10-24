"""Check the chat panel inside the slicer window on Windows with a real Edge/Chrome.

Runs AIAssistant/agent/window.EmbeddedChat against a stand-in of STE's main window:
a PyQt5 QQuickWindow with SteSlicer.qml's structure (one root Item with
"anchors.fill: parent", sidebar on the left) and UM MainWindow's viewportRect. Checks
the reserved strip, the 3D viewport fraction, that the browser page covers exactly the
strip after resize/move/minimize, the owner window, the crop of the browser title bar,
hide/reopen, and real keyboard input: one mouse click into the chat input and three
key presses. The click is made only when the point is verifiably on the chat window,
the keys only when the chat or the test window is in front. Needs modern Python with
PyQt5 (QtQuick) and Pillow for screenshots; writes them to the output folder.

    python plugin/tools/check_embedded_chat.py [--out folder]

This is not STE itself (PyQt 5.15 here, 5.10 in STE 7.10): confirm in the slicer.
"""
import argparse
import ctypes
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from ctypes import wintypes as WT

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "AIAssistant"))

STAND_QML = """import QtQuick 2.7
import Stand 1.0
MainWindow {
    x: 40; y: 40; width: 1000; height: 640; visible: true; title: "STE window stand-in"
    Item {
        anchors.fill: parent
        Rectangle { anchors.fill: parent; color: "#2b6cb0" }
        Rectangle { width: 280; height: parent.height; color: "#e2e8f0"
            Text { anchors.centerIn: parent; text: "STE sidebar" } }
        Rectangle { anchors.right: parent.right; width: 260; height: 50; color: "orange"
            Text { anchors.centerIn: parent; text: "right edge of the slicer UI" } }
    }
}
"""
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>STE AI Assistant</title>
<style>body{margin:0;font:16px sans-serif}header{background:#10b981;color:#fff;padding:12px}
input{margin:16px;width:80%;font-size:18px}</style></head><body><header>Chat page</header>
<input id="q" autofocus><script>
const q = document.getElementById('q');
q.addEventListener('input', () => fetch('/log?text=' + encodeURIComponent(q.value)));
</script></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=tempfile.mkdtemp(prefix="embedded_chat_"))
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Windows only"); return 2
    from PyQt5.QtCore import pyqtProperty, pyqtSignal, QRectF, QUrl
    from PyQt5.QtGui import QGuiApplication
    from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext, QQmlProperty, qmlRegisterType
    from PyQt5.QtQuick import QQuickWindow
    from agent import window as W

    if W.find_app_browser() is None:
        print("Edge or Chrome is required"); return 2
    os.makedirs(args.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="embedded_chat_work_")
    for name, text in (("stand.qml", STAND_QML), ("page.html", PAGE)):
        with open(os.path.join(work, name), "w", encoding="utf-8") as f:
            f.write(text)
    typed = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=work, **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/log":
                typed.extend(v for k, v in urllib.parse.parse_qsl(url.query) if k == "text")
                self.send_response(204); self.end_headers(); return
            super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_url = "http://127.0.0.1:%d/page.html" % server.server_address[1]

    class MainWindow(QQuickWindow):  # UM.Qt.Bindings.MainWindow.viewportRect
        viewportRectChanged = pyqtSignal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._rect = QRectF(0, 0, 1, 1)

        def setViewportRect(self, rect):
            if rect != self._rect:
                self._rect = rect
                self.viewportRectChanged.emit()

        @pyqtProperty(QRectF, fset=setViewportRect, notify=viewportRectChanged)
        def viewportRect(self):
            return self._rect

    qmlRegisterType(MainWindow, "Stand", 1, 0, "MainWindow")
    app = QGuiApplication(sys.argv)
    engine = QQmlApplicationEngine()
    engine.load(QUrl.fromLocalFile(os.path.join(work, "stand.qml")))
    window = engine.rootObjects()[0]

    class Preferences:
        values = {}

        def addPreference(self, key, default):
            self.values.setdefault(key, default)

        def getValue(self, key):
            return self.values.get(key)

        def setValue(self, key, value):
            self.values[key] = value

    class Application:
        def getMainWindow(self):
            return window

        def getPreferences(self):
            return Preferences()

        def createQmlComponent(self, path, context):
            component = QQmlComponent(engine, QUrl.fromLocalFile(path))
            return component.create(QQmlContext(engine.rootContext()))

    user32 = ctypes.windll.user32

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)

    def rect(hwnd):
        r = WT.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def client(hwnd):
        p = WT.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(p))
        r = WT.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        return p.x, p.y, p.x + r.right, p.y + r.bottom

    def page_rect(hwnd):
        found = []
        callback = ctypes.WINFUNCTYPE(WT.BOOL, WT.HWND, WT.LPARAM)(lambda child, l: found.append(child) or True)
        user32.EnumChildWindows(hwnd, callback, 0)
        for child in found:
            buffer = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, buffer, 64)
            if buffer.value == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(child):
                return rect(child)
        return None

    def screenshot(name):
        try:
            from PIL import ImageGrab
        except ImportError:
            return
        pump(0.8)
        c = client(int(window.winId()))
        ImageGrab.grab(bbox=(c[0] - 8, c[1] - 40, c[2] + 8, c[3] + 8), all_screens=True).save(
            os.path.join(args.out, name))

    chat = W.EmbeddedChat(Application(), os.path.join(work, "profile"))
    checks, details = {}, {}
    pump(0.5)
    chat.open(page_url)
    for _ in range(300):
        pump(0.1)
        if chat._hwnd and chat._insets != (0, 0):
            break
    pump(1.0)
    hwnd, main_hwnd = chat._hwnd, int(window.winId())
    checks["embedded"] = bool(chat._embedded and hwnd)
    if not checks["embedded"]:
        print(json.dumps(checks)); return 1
    checks["owned_by_slicer"] = user32.GetWindow(hwnd, 4) == main_hwnd

    def strip_check(tag):
        # Chromium resets the window region after its own resize; the plugin re-crops on the
        # next tick (100 ms), so wait for a stable state instead of taking one sample.
        for _ in range(15):
            pump(0.1)
            c = client(main_hwnd)
            strip = (c[2] - int(chat._panel.property("panelWidth")), c[1], c[2], c[3])
            page = page_rect(hwnd)
            fraction = (window.width() - QQmlProperty(chat._content, "anchors.rightMargin").read()) / float(window.width())
            checks[tag] = bool(page and all(abs(a - b) <= 2 for a, b in zip(page, strip))
                               and abs(window.viewportRect.width() - fraction) < 1e-3
                               and chat._win.region_box(hwnd) == chat._crop_box())
            if checks[tag]:
                break
        if not checks[tag]:
            details[tag] = {"page": page, "strip": strip, "viewport": window.viewportRect.width(),
                            "fraction": fraction, "region": chat._win.region_box(hwnd), "crop": chat._crop_box()}

    strip_check("strip_after_open")
    screenshot("1_open.png")
    window.setWidth(1080)
    strip_check("strip_after_resize")
    window.setX(window.x() + 40)
    strip_check("strip_after_move")
    window.setViewportRect(QRectF(0, 0, 1, 1))           # SteSlicer.qml sidebar toggle
    strip_check("viewport_after_sidebar_toggle")
    chat.hide()
    pump(0.6)
    checks["hide_releases_space"] = (QQmlProperty(chat._content, "anchors.rightMargin").read() == 0
                                     and window.viewportRect.width() == 1.0 and not user32.IsWindowVisible(hwnd))
    chat.open(page_url)
    strip_check("strip_after_reopen")
    window.showMinimized()
    pump(0.8)
    checks["hidden_with_minimized_slicer"] = not user32.IsWindowVisible(hwnd)
    window.showNormal()
    strip_check("strip_after_restore")

    user32.SetWindowPos(main_hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # topmost while typing
    # A fresh Edge profile shows a sign-in notice owned by the chat window; close such popups.
    popups = []
    callback = ctypes.WINFUNCTYPE(WT.BOOL, WT.HWND, WT.LPARAM)(
        lambda w, l: (popups.append(w) if user32.GetWindow(w, 4) == hwnd and user32.IsWindowVisible(w) else None) or True)
    user32.EnumWindows(callback, 0)
    for popup in popups:
        user32.PostMessageW(popup, 0x0010, 0, 0)  # WM_CLOSE
    details["closed_browser_popups"] = len(popups)
    pump(0.8)
    # Bring the stand-in to the front without key presses (Alt would open the browser's app menu).
    front = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    own = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(front, own, True)
    user32.SetForegroundWindow(main_hwnd)
    user32.AttachThreadInput(front, own, False)
    pump(0.6)
    c = client(main_hwnd)
    x, y = c[2] - 150, c[1] + 92                          # inside the page's input field
    under = user32.WindowFromPoint(WT.POINT(x, y))
    details["click_on_chat"] = under == hwnd or user32.GetAncestor(under, 2) == hwnd
    if details["click_on_chat"]:
        user32.SetCursorPos(x, y)
        user32.mouse_event(2, 0, 0, 0, 0); user32.mouse_event(4, 0, 0, 0, 0)
        pump(0.6)
        details["foreground"] = {hwnd: "chat", main_hwnd: "stand-in"}.get(user32.GetForegroundWindow(), "other")
        if user32.GetForegroundWindow() in (hwnd, main_hwnd):
            for key in (0x41, 0x42, 0x43):
                user32.keybd_event(key, 0, 0, 0); user32.keybd_event(key, 0, 2, 0); pump(0.05)
        pump(1.0)
    checks["keyboard_reaches_chat"] = bool(typed) and len(typed[-1]) == 3
    screenshot("2_typed.png")
    user32.SetWindowPos(main_hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    chat.close()
    pump(1.0)
    checks["close_restores_layout"] = (QQmlProperty(chat._content, "anchors.rightMargin").read() == 0
                                       and window.viewportRect.width() == 1.0)
    details["typed"] = typed[-1:] if typed else []
    print(json.dumps({"checks": checks, "details": details, "screenshots": args.out}, indent=1, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
