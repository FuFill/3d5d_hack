# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Chat window docked to the right edge of the slicer window.

The SPA runs in the system browser as required. When Edge or Chrome is available it is
started in app mode (no tabs or address bar) with a separate profile; on Windows the
window is then found by title and kept attached to the slicer: it follows moves and
resizes, minimizes and restores with it. If there is no room on the right, the slicer
window is narrowed and restored when the chat closes. Without such a browser the SPA
opens as a normal tab.
"""

import os
import shutil
import subprocess
import sys
import webbrowser

WINDOW_TITLE = "STE AI Assistant"
PANEL_WIDTH = 400


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
