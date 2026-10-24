# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Chat panel inside the slicer window, with a docked window as fallback.

The SPA runs in the system browser: STE's PyQt has no QtWebEngine, and it could not be
initialised from a plugin anyway (it must be set up before the QGuiApplication exists).
When Edge or Chrome is available it is started in app mode (no tabs or address bar)
with a separate profile and the window is found by title.

EmbeddedChat (Windows) shows that window inside the slicer window: a strip on the right
edge is reserved in the slicer's QML layout (the UI gets a right margin, the 3D viewport
is narrowed to the rest, so nothing is covered) and the browser window is placed exactly
over it, cropped to its page (no title bar or frame). The window is owned by the slicer
window: it stays above it, minimizes and restores with it and has no taskbar button.
It is deliberately not a WS_CHILD window: Chromium drops all keyboard input in a child
window (verified with Edge: even posted WM_CHAR is ignored). DirectComposition is
disabled for this browser profile because its output ignores the window region, which
would leave the browser's own title bar visible over the slicer's title bar. The strip has a resize
handle; the width is kept in the preferences. If this is not possible the chat falls
back to DockedWindow: a separate window attached to the right edge of the slicer (the
slicer window is narrowed when there is no room). Without such a browser the SPA opens
as a normal tab.
"""

import os
import shutil
import subprocess
import sys
import webbrowser

WINDOW_TITLE = "STE AI Assistant"
PANEL_WIDTH = 400
PANEL_MIN_WIDTH = 300
PANEL_MAX_WIDTH = 900
MIN_SLICER_WIDTH = 600   # slicer area kept left of the panel, logical pixels
WIDTH_PREFERENCE = "ai_assistant/panel_width"
QML_PANEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qml", "ChatPanel.qml")
QML_CARD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qml", "ModelCard.qml")


def _candidates():
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        paths = []
        for root in [r for r in roots if r]:
            paths.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            paths.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        return paths
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return [p for p in (shutil.which(n) for n in ("google-chrome", "chromium", "chromium-browser",
                                                   "microsoft-edge")) if p]


def find_app_browser():
    for path in _candidates():
        if path and os.path.isfile(path):
            return path
    return None


class DockedWindow:
    def __init__(self, app, profile_dir):
        self._app = app
        self._profile_dir = profile_dir
        self._process = None
        self._win = _Win32() if sys.platform == "win32" else None
        self._hwnd = None
        self._saved_placement = None
        self._timer = None
        self._search_ticks = 0
        self._connected = False

    # ------------------------------------------------------------------ public

    def open(self, url):
        if self._hwnd and self._win and self._win.is_window(self._hwnd):
            self._win.show(self._hwnd)
            self._layout()
            return
        browser = find_app_browser()
        if browser is None:
            webbrowser.open(url)
            return
        x, y, width, height = self._initial_geometry()
        args = [browser, "--app=" + url, "--user-data-dir=" + self._profile_dir,
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                "--window-size=%d,%d" % (width, height), "--window-position=%d,%d" % (x, y)]
        try:
            self._process = subprocess.Popen(args, close_fds=True)
        except OSError:
            webbrowser.open(url)
            return
        if self._win is not None:
            self._hwnd = None
            self._search_ticks = 0
            self._start_timer()

    def close(self):
        if self._timer is not None:
            self._timer.stop()
        self._restore_main()
        if self._hwnd and self._win and self._win.is_window(self._hwnd):
            self._win.close(self._hwnd)
        self._hwnd = None
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        self._process = None

    # ------------------------------------------------------------------ geometry

    def _main_window(self):
        try:
            return self._app.getMainWindow()
        except Exception:
            return None

    def _initial_geometry(self):
        window = self._main_window()
        if window is None:
            return 100, 100, PANEL_WIDTH, 800
        ratio = window.devicePixelRatio() if hasattr(window, "devicePixelRatio") else 1.0
        x = int((window.x() + window.width()) * ratio)
        width = int(PANEL_WIDTH * ratio)
        screen = window.screen().availableGeometry() if window.screen() else None
        if screen is not None and x + width > (screen.x() + screen.width()) * ratio:
            x = int((screen.x() + screen.width()) * ratio) - width
        return x, int(window.y() * ratio), width, int(window.height() * ratio)

    def _start_timer(self):
        from PyQt5.QtCore import QTimer
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setInterval(250)
            self._timer.timeout.connect(self._tick)
        self._timer.start()
        window = self._main_window()
        if window is not None and not self._connected:
            for name in ("xChanged", "yChanged", "widthChanged", "heightChanged", "visibilityChanged"):
                signal = getattr(window, name, None)
                if signal is not None:
                    signal.connect(self._on_main_changed)
            if hasattr(window, "activeChanged"):
                window.activeChanged.connect(self._on_main_activated)
            self._connected = True

    def _tick(self):
        # The window is tracked by handle, not by process: a browser may hand the window
        # over to an already running instance of the same profile and exit.
        if self._hwnd is None:
            self._search_ticks += 1
            self._hwnd = self._win.find(WINDOW_TITLE)
            if self._hwnd:
                self._make_room()
                self._layout()
            elif self._search_ticks > 120:  # 30 s: give up docking, the window still works.
                self._timer.stop()
            return
        if not self._win.is_window(self._hwnd):
            self._hwnd = None
            self._restore_main()
            self._timer.stop()

    def _main_hwnd(self):
        window = self._main_window()
        return int(window.winId()) if window is not None else None

    def _make_room(self):
        main = self._main_hwnd()
        if not main:
            return
        width = self._win.scaled(main, PANEL_WIDTH)
        work = self._win.work_area(main)
        rect = self._win.visual_rect(main)
        if work[2] - rect[2] >= width:
            return
        self._saved_placement = self._win.get_placement(main)
        self._win.restore(main)
        self._win.place(main, work[0], work[1], work[2] - work[0] - width, work[3] - work[1])

    def _layout(self):
        main = self._main_hwnd()
        if not self._hwnd or not main:
            return
        if self._win.is_minimized(main):
            self._win.minimize(self._hwnd)
            return
        if self._win.is_minimized(self._hwnd):
            self._win.show_no_activate(self._hwnd)
        width = self._win.scaled(main, PANEL_WIDTH)
        work = self._win.work_area(main)
        left, top, right, bottom = self._win.visual_rect(main)
        x = min(right, work[2] - width)
        self._win.place(self._hwnd, x, top, width, bottom - top, no_activate=True)

    def _on_main_changed(self, *args):
        if self._hwnd and self._win:
            self._layout()

    def _on_main_activated(self, *args):
        window = self._main_window()
        if self._hwnd and self._win and window is not None and window.isActive():
            self._win.raise_no_activate(self._hwnd)

    def _restore_main(self):
        if self._saved_placement is not None and self._win is not None:
            main = self._main_hwnd()
            if main:
                self._win.set_placement(main, self._saved_placement)
            self._saved_placement = None


class HoverCard:
    """Card of a model-tree node in the slicer window, left of the chat (ModelCard.qml).

    The chat page cannot draw outside its window, so the card is shown by the slicer: the
    page reports the hovered node and its height, the card appears over the build plate
    view at that height, without covering the other nodes of the tree.
    """

    def __init__(self, app, chat):
        self._app = app
        self._chat = chat
        self._card = None
        try:
            window = app.getMainWindow()
            if window is not None:
                self._card = app.createQmlComponent(QML_CARD, {})
                if self._card is not None:
                    self._card.setParentItem(window.contentItem())
        except Exception:
            self._card = None

    def hide(self):
        if self._card is not None:
            self._app.callLater(self._update, None, 0)

    def show(self, view, y):
        """From any thread; False when the slicer window cannot show the card (the page shows its own)."""
        if self._card is None:
            return False
        self._app.callLater(self._update, view, y)
        return True

    def _update(self, view, y):
        card = self._card
        if not view:
            card.setProperty("visible", False)
            return
        rows = []
        for row in view.get("changes") or []:
            old = _html(row.get("old"))
            tag = _html(row.get("tag"))
            rows.append("%s&nbsp;&nbsp;%s<b>%s</b>%s" % (
                _html(row.get("label")), ('<span style="color:#80868b"><s>%s</s></span> → ' % old) if old else "→ ",
                _html(row.get("new")), ('&nbsp;&nbsp;<span style="color:#9aa0a6">%s</span>' % tag) if tag else ""))
        if view.get("more"):
            rows.append('<span style="color:#80868b">%s</span>' % _html(view["more"]))
        empty = "" if rows else view.get("empty") or ""
        card.setProperty("title", view.get("name") or "")
        card.setProperty("stats", view.get("stats") or "")
        card.setProperty("rows", rows)
        card.setProperty("emptyText", empty)
        card.setProperty("rightInset", self._chat.reserved_width() if self._chat is not None else 0)
        card.setProperty("cardY", max(0.0, min(1.0, float(y))))
        card.setProperty("visible", True)


def _html(text):
    return (str(text) if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class EmbeddedChat:
    """The chat as a panel inside the slicer window (see the module docstring)."""

    def __init__(self, app, profile_dir):
        self._app = app
        self._profile_dir = profile_dir
        self._fallback = DockedWindow(app, profile_dir)
        self._win = _Win32() if sys.platform == "win32" else None
        self._process = None
        self._hwnd = None
        self._placed = None        # last browser window rect we set, physical pixels
        self._crop = None          # visible page rectangle in window coordinates
        self._panel = None
        self._content = None
        self._visible = False
        self._embedded = None      # None: not tried yet; False: use the docked fallback
        self._timer = None
        self._search_ticks = 0
        self._connected = False
        self._insets = (0, 0)      # browser frame left/top before the page, physical pixels
        self._width = self._stored_width()

    # ------------------------------------------------------------------ public

    def open(self, url):
        if self._embedded is False:
            self._fallback.open(url)
            return
        if self._hwnd and self._win.is_window(self._hwnd):
            self._show()
            return
        if self._timer is not None and self._timer.isActive() and self._process is not None:
            self._show(status="Загрузка чата…")   # the browser is still starting
            return
        browser = find_app_browser()
        if self._win is None or browser is None or not self._prepare():
            self._use_fallback(url)
            return
        self._show(status="Загрузка чата…")
        x, y, width, height = self._fallback._initial_geometry()
        args = [browser, "--app=" + url, "--user-data-dir=" + self._profile_dir,
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate,CalculateNativeWinOcclusion",
                "--disable-direct-composition",
                "--window-size=%d,%d" % (width, height), "--window-position=%d,%d" % (x, y)]
        try:
            self._process = subprocess.Popen(args, close_fds=True)
        except OSError:
            self._use_fallback(url)
            return
        self._hwnd = None
        self._search_ticks = 0
        self._start_timer()

    def hide(self):
        if self._embedded is False:
            self._fallback.close()
            return
        self._visible = False
        if self._hwnd and self._win.is_window(self._hwnd):
            self._win.hide(self._hwnd)
        if self._panel is not None:
            self._panel.setProperty("visible", False)
        self._release_space()

    def close(self):
        if self._timer is not None:
            self._timer.stop()
        self._fallback.close()
        self._visible = False
        self._release_space()
        if self._hwnd and self._win and self._win.is_window(self._hwnd):
            self._win.close(self._hwnd)
        self._hwnd = None
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
            except OSError:
                pass
        self._process = None

    def reserved_width(self):
        """Width of the chat strip at the right edge of the slicer window (0 when the chat is outside it)."""
        if self._embedded and self._visible and self._panel is not None:
            return int(self._panel.property("width") or 0)
        return 0

    # ------------------------------------------------------------------ setup

    def _main_window(self):
        try:
            return self._app.getMainWindow()
        except Exception:
            return None

    def _prepare(self):
        """Find the slicer's root UI item and create the panel; False if the layout is unknown."""
        window = self._main_window()
        if window is None:
            return False
        if self._panel is not None:
            return True
        root = window.contentItem()
        self._content = self._root_item(root)
        if self._content is None:
            return False
        self._panel = self._app.createQmlComponent(QML_PANEL, {})
        if self._panel is None:
            self._content = None
            return False
        self._panel.setParentItem(root)
        self._panel.setProperty("visible", False)
        for name, value in (("minimumWidth", PANEL_MIN_WIDTH), ("maximumWidth", PANEL_MAX_WIDTH)):
            self._panel.setProperty(name, value)
        self._panel.resized.connect(self._on_resized)
        self._panel.resizeFinished.connect(self._on_resize_finished)
        if not self._connected:
            for name in ("xChanged", "yChanged", "widthChanged", "heightChanged"):
                getattr(window, name).connect(self._layout)
            if hasattr(window, "viewportRectChanged"):
                window.viewportRectChanged.connect(self._on_viewport_changed)
            self._connected = True
        return True

    @staticmethod
    def _root_item(root):
        # SteSlicer.qml: the whole UI is one Item with "anchors.fill: parent" under the window.
        # Grouped "anchors.*" properties are reachable through QQmlProperty only.
        from PyQt5.QtQml import QQmlProperty
        for item in root.childItems():
            fill = QQmlProperty(item, "anchors.fill").read()
            if fill is not None and abs(item.width() - root.width()) < 1 and abs(item.height() - root.height()) < 1:
                return item
        return None

    def _set_margin(self, value):
        if self._content is not None:
            from PyQt5.QtQml import QQmlProperty
            QQmlProperty(self._content, "anchors.rightMargin").write(value)

    def _use_fallback(self, url):
        self._embedded = False
        self._release_space()
        if self._panel is not None:
            self._panel.setProperty("visible", False)
        self._fallback.open(url)

    def _start_timer(self):
        from PyQt5.QtCore import QTimer
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setInterval(100)
            self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        # Tracked by handle: the browser may pass the window to a running instance and exit.
        if self._hwnd is None:
            self._search_ticks += 1
            hwnd = self._win.find(WINDOW_TITLE)
            if hwnd:
                self._embed(hwnd)
            elif self._search_ticks > 300:   # 30 s
                self._timer.stop()
                self._panel.setProperty("statusText", "Окно чата не открылось. Скройте панель и откройте "
                                                      "снова: Расширения → AI Assistant.")
            return
        if not self._win.is_window(self._hwnd):
            # The page closed itself or the browser exited.
            self._hwnd = None
            self._timer.stop()
            self.hide()
            return
        # Edge/Chrome draw the app title bar themselves: measure where the page starts.
        insets = self._win.content_insets(self._hwnd)
        if insets is not None and insets != self._insets:
            self._insets = insets
            self._layout()
        elif self._visible and self._placed and self._win.window_rect(self._hwnd) != self._placed:
            self._layout()   # moved by the system or the user (Alt+Space), or a missed move
        elif self._visible and self._crop and self._win.region_box(self._hwnd) != self._crop_box():
            # Chromium resets the window region when it repaints its frame; crop again.
            self._win.crop(self._hwnd, *self._crop)

    def _embed(self, hwnd):
        window = self._main_window()
        try:
            self._win.own(hwnd, int(window.winId()))
        except Exception:
            # Keep the chat usable as a docked window next to the slicer.
            self._embedded = False
            self._release_space()
            self._panel.setProperty("visible", False)
            self._fallback._hwnd = hwnd
            self._fallback._process = self._process
            self._fallback._make_room()
            self._fallback._layout()
            self._fallback._start_timer()
            self._timer.stop()
            return
        self._hwnd, self._embedded = hwnd, True
        if self._visible:
            self._show()
        else:
            self._win.hide(hwnd)   # hidden by the user while the browser was starting

    # ------------------------------------------------------------------ layout

    def _show(self, status=""):
        self._visible = True
        self._panel.setProperty("statusText", status)
        self._panel.setProperty("visible", True)
        self._layout()
        main = self._main_window()
        if self._hwnd and main is not None and not self._win.is_minimized(int(main.winId())):
            self._win.show_no_activate(self._hwnd)

    def _panel_width(self, window_width):
        room = max(PANEL_MIN_WIDTH, window_width - MIN_SLICER_WIDTH)
        return int(max(PANEL_MIN_WIDTH, min(self._width, PANEL_MAX_WIDTH, room)))

    def _layout(self, *args):
        window = self._main_window()
        if not self._visible or window is None or self._panel is None:
            return
        width, height = window.width(), window.height()
        panel = self._panel_width(width)
        handle = int(self._panel.property("handleWidth") or 0)
        self._panel.setProperty("panelWidth", panel)
        self._reserve(window, panel + handle)
        if self._hwnd:
            # Physical pixels: the page area of the browser window covers the strip right of
            # the handle; the title bar and frame around it are cut off by the window region.
            ratio = window.devicePixelRatio() or 1.0
            origin_x, origin_y = self._win.client_origin(int(window.winId()))
            page_w, page_h = int(round(panel * ratio)), int(round(height * ratio))
            left, top = self._insets
            x = origin_x + int(round((width - panel) * ratio))
            self._placed = (x - left, origin_y - top, x + page_w + left, origin_y + page_h + left)
            self._win.place_rect(self._hwnd, self._placed)
            self._crop = (left, top, page_w, page_h)
            self._win.crop(self._hwnd, *self._crop)

    def _crop_box(self):
        left, top, width, height = self._crop
        return left, top, left + width, top + height

    def _reserve(self, window, strip):
        self._set_margin(strip)
        self._set_viewport(window, strip)

    def _release_space(self):
        window = self._main_window()
        self._set_margin(0)
        if window is not None and self._content is not None:
            self._set_viewport(window, 0)

    @staticmethod
    def _set_viewport(window, strip):
        # MainWindow.viewportRect is the 3D view's fraction of the window (left-aligned).
        if not hasattr(window, "setViewportRect"):
            return
        from PyQt5.QtCore import QRectF
        width = window.width()
        fraction = 1.0 if not strip or width <= strip else (width - strip) / float(width)
        current = window.viewportRect   # pyqtProperty of UM MainWindow
        if abs(current.width() - fraction) > 1e-4 or current.x() or current.y() or current.height() != 1.0:
            window.setViewportRect(QRectF(0, 0, fraction, 1.0))

    def _on_viewport_changed(self):
        # SteSlicer.qml resets viewportRect when the settings sidebar collapses or expands.
        if self._visible:
            self._layout()

    def _on_resized(self, width):
        self._width = int(width)
        self._layout()

    def _on_resize_finished(self, width):
        self._width = int(width)
        try:
            self._app.getPreferences().setValue(WIDTH_PREFERENCE, self._width)
        except Exception:
            pass

    def _stored_width(self):
        try:
            preferences = self._app.getPreferences()
            preferences.addPreference(WIDTH_PREFERENCE, PANEL_WIDTH)
            return int(preferences.getValue(WIDTH_PREFERENCE) or PANEL_WIDTH)
        except Exception:
            return PANEL_WIDTH


class _Win32:
    """The few user32/dwmapi calls needed for docking; all coordinates in physical pixels."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.wt = wintypes
        self.user32 = ctypes.windll.user32
        try:
            self.dwm = ctypes.windll.dwmapi
        except OSError:
            self.dwm = None

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                        ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                        ("rcNormalPosition", wintypes.RECT)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        self.WINDOWPLACEMENT = WINDOWPLACEMENT
        self.MONITORINFO = MONITORINFO
        self.enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        ctypes.windll.gdi32.CreateRectRgn.restype = ctypes.c_void_p
        ctypes.windll.gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        # 64-bit user32 exports the *Ptr variants; 32-bit only the plain ones.
        self.get_long = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
        self.set_long = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
        self.get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_long.restype = ctypes.c_ssize_t
        self.set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self.set_long.restype = ctypes.c_ssize_t

    def own(self, hwnd, owner):
        """Make owner the window's owner (z-order, minimize) and drop its taskbar button."""
        self.set_long(hwnd, -8, owner)                                   # GWLP_HWNDPARENT
        ex = self.get_long(hwnd, -20) & 0xFFFFFFFF & ~0x00040000          # -WS_EX_APPWINDOW
        self.set_long(hwnd, -20, ex - (1 << 32) if ex & 0x80000000 else ex)
        self.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)  # FRAMECHANGED

    def client_origin(self, hwnd):
        point = self.wt.POINT(0, 0)
        self.user32.ClientToScreen(hwnd, self.ct.byref(point))
        return point.x, point.y

    def window_rect(self, hwnd):
        return self._rect(hwnd)

    def place_rect(self, hwnd, rect):
        left, top, right, bottom = rect
        self.user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, 0x0004 | 0x0010)  # NOZORDER|NOACTIVATE

    def hide(self, hwnd):
        self.user32.ShowWindow(hwnd, 0)  # SW_HIDE

    def region_box(self, hwnd):
        """Bounding box of the window region, or None when the window has no region."""
        rect = self.wt.RECT()
        kind = self.user32.GetWindowRgnBox(hwnd, self.ct.byref(rect))
        if kind not in (2, 3):  # SIMPLEREGION, COMPLEXREGION
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def content_insets(self, hwnd):
        """Offset of the page (Chromium's Chrome_RenderWidgetHostHWND) inside the browser window."""
        found = []

        def callback(child, lparam):
            buffer = self.ct.create_unicode_buffer(64)
            self.user32.GetClassNameW(child, buffer, 64)
            if buffer.value == "Chrome_RenderWidgetHostHWND" and self.user32.IsWindowVisible(child):
                found.append(child)
                return False
            return True

        self.user32.EnumChildWindows(hwnd, self.enum_proc(callback), 0)
        if not found:
            return None
        frame, page = self._rect(hwnd), self._rect(found[0])
        return page[0] - frame[0], page[1] - frame[1]

    def crop(self, hwnd, left, top, width, height):
        """Show only this rectangle of the window (window coordinates, physical pixels)."""
        region = self.ct.windll.gdi32.CreateRectRgn(left, top, left + width, top + height)
        if region and not self.user32.SetWindowRgn(hwnd, region, True):
            self.ct.windll.gdi32.DeleteObject(region)  # the system owns it only on success

    def find(self, title):
        found = []

        def callback(hwnd, lparam):
            if self.user32.IsWindowVisible(hwnd):
                length = self.user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = self.ct.create_unicode_buffer(length + 1)
                    self.user32.GetWindowTextW(hwnd, buffer, length + 1)
                    if buffer.value == title or buffer.value.startswith(title + " "):
                        found.append(hwnd)
                        return False
            return True

        self.user32.EnumWindows(self.enum_proc(callback), 0)
        return found[0] if found else None

    def is_window(self, hwnd):
        return bool(self.user32.IsWindow(hwnd))

    def is_minimized(self, hwnd):
        return bool(self.user32.IsIconic(hwnd))

    def scaled(self, hwnd, value):
        try:
            dpi = self.user32.GetDpiForWindow(hwnd)
        except AttributeError:
            dpi = 96
        return int(value * (dpi or 96) / 96.0)

    def _rect(self, hwnd):
        rect = self.wt.RECT()
        self.user32.GetWindowRect(hwnd, self.ct.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def visual_rect(self, hwnd):
        """Window bounds without the invisible resize borders of Windows 10/11."""
        if self.dwm is not None:
            rect = self.wt.RECT()
            if self.dwm.DwmGetWindowAttribute(hwnd, 9, self.ct.byref(rect), self.ct.sizeof(rect)) == 0:
                return rect.left, rect.top, rect.right, rect.bottom
        return self._rect(hwnd)

    def work_area(self, hwnd):
        monitor = self.user32.MonitorFromWindow(hwnd, 2)
        info = self.MONITORINFO()
        info.cbSize = self.ct.sizeof(info)
        self.user32.GetMonitorInfoW(monitor, self.ct.byref(info))
        return info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom

    def place(self, hwnd, x, y, width, height, no_activate=False):
        outer = self._rect(hwnd)
        inner = self.visual_rect(hwnd)
        dl, dt, dr, db = inner[0] - outer[0], inner[1] - outer[1], outer[2] - inner[2], outer[3] - inner[3]
        flags = 0x0004 | (0x0010 if no_activate else 0)  # SWP_NOZORDER | SWP_NOACTIVATE
        self.user32.SetWindowPos(hwnd, None, x - dl, y - dt, width + dl + dr, height + dt + db, flags)

    def raise_no_activate(self, hwnd):
        self.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOSIZE|NOMOVE|NOACTIVATE

    def show(self, hwnd):
        self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        self.user32.SetForegroundWindow(hwnd)

    def close(self, hwnd):
        self.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def show_no_activate(self, hwnd):
        self.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE

    def minimize(self, hwnd):
        self.user32.ShowWindow(hwnd, 7)  # SW_SHOWMINNOACTIVE

    def restore(self, hwnd):
        if self.user32.IsZoomed(hwnd):
            self.user32.ShowWindow(hwnd, 9)

    def get_placement(self, hwnd):
        placement = self.WINDOWPLACEMENT()
        placement.length = self.ct.sizeof(placement)
        self.user32.GetWindowPlacement(hwnd, self.ct.byref(placement))
        return placement

    def set_placement(self, hwnd, placement):
        self.user32.SetWindowPlacement(hwnd, self.ct.byref(placement))
