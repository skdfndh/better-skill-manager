"""Skill Shelf 桌面窗口启动器。"""

from __future__ import annotations

import atexit
import ctypes
import os
import threading
import time
import tkinter.messagebox
from ctypes import wintypes

import server


WINDOW_TITLE = "Skill Shelf · 技能管理"
MUTEX_NAME = "Local\\SkillShelfDesktopSingleInstance"
_INSTANCE_MUTEX_HANDLE = None


def focus_existing_window(timeout_seconds: float = 0) -> bool:
    if os.name != "nt":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.FlashWindow.argtypes = (wintypes.HWND, wintypes.BOOL)

    deadline = time.monotonic() + timeout_seconds
    while True:
        window = user32.FindWindowW(None, WINDOW_TITLE)
        if window:
            if user32.IsIconic(window):
                user32.ShowWindowAsync(window, 9)
            user32.BringWindowToTop(window)
            if not user32.SetForegroundWindow(window):
                user32.FlashWindow(window, True)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def acquire_single_instance() -> bool:
    global _INSTANCE_MUTEX_HANDLE
    if os.name != "nt":
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    if focus_existing_window():
        return False

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(handle)
        focus_existing_window(timeout_seconds=10)
        return False

    _INSTANCE_MUTEX_HANDLE = handle
    atexit.register(kernel32.CloseHandle, handle)
    return True


def main() -> None:
    try:
        is_primary_instance = acquire_single_instance()
    except OSError as exc:
        tkinter.messagebox.showerror("Skill Shelf 启动失败", f"无法检查应用实例：{exc}")
        raise SystemExit(1)
    if not is_primary_instance:
        return

    try:
        import webview
    except ImportError:
        tkinter.messagebox.showerror(
            "Skill Shelf 启动失败",
            "缺少桌面窗口组件。请运行 start-app.ps1 安装依赖，或使用 start.ps1 启动本地端口版本。",
        )
        raise SystemExit(1)

    http_server = server.create_server(port=0, start_watcher=False)
    api_thread = threading.Thread(target=http_server.serve_forever, daemon=True, name="skill-shelf-api")
    api_thread.start()
    url = f"http://{server.HOST}:{http_server.server_port}/"
    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=1440,
            height=960,
            min_size=(1024, 680),
        )
        def start_watcher() -> None:
            threading.Thread(target=server.watch_library, daemon=True, name="skill-shelf-watcher").start()

        webview.start(func=start_watcher, gui="edgechromium", debug=os.environ.get("SKILL_SHELF_DEBUG") == "1")
    except Exception as exc:
        tkinter.messagebox.showerror(
            "Skill Shelf 无法打开窗口",
            f"桌面窗口启动失败：{exc}\n\n请确认已安装 Microsoft Edge WebView2 Runtime。也可以运行 start.ps1 使用本地端口版本。",
        )
        raise SystemExit(1)
    finally:
        http_server.shutdown()
        http_server.server_close()
        server.cleanup_source_previews()


if __name__ == "__main__":
    main()
