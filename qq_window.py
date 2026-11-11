"""从已打开的 QQ 窗口把消息「粘贴 + 回车」发出去（不需要任何机器人权限）。

用法：
    python qq_window.py "要发的文字"          # 发文字
    python qq_window.py --image 截图.png "说明"  # 发图片（QQ 会把图片粘进输入框）

前提：QQ 已登录，并且**目标群/会话已经打开在当前窗口里**（本工具不切换会话，
只往当前聊天框里粘贴，避免把消息发错地方）。
"""
from __future__ import annotations

import ctypes
import io
import sys
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002


class QQWindowError(RuntimeError):
    pass


def find_qq_window() -> int:
    """找类名 Chrome_WidgetWin_* 且标题是 QQ 的窗口（QQNT 主窗口）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, cls, 128)
        if cls.value.startswith("Chrome_WidgetWin") and buf.value.strip() in ("QQ", "QQ NT"):
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if rect.right - rect.left > 300:      # 排除弹出的小窗
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        raise QQWindowError("没找到已登录的 QQ 窗口（请先登录 QQ）")
    return found[0]


def _focus(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    # 绕过前台窗口限制
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_me = kernel32.GetCurrentThreadId()
    if tid_fg != tid_me:
        user32.AttachThreadInput(tid_me, tid_fg, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg != tid_me:
        user32.AttachThreadInput(tid_me, tid_fg, False)
    time.sleep(0.4)


def _click(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # left down
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # left up
    time.sleep(0.2)


def _set_clipboard_text(text: str) -> None:
    if not user32.OpenClipboard(None):
        raise QQWindowError("打开剪贴板失败")
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            raise QQWindowError("写剪贴板失败（文字）")
    finally:
        user32.CloseClipboard()


def _set_clipboard_image(path: Path) -> None:
    """把 PNG 压成 CF_DIB（24 位 BMP 去掉 14 字节文件头）。"""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    dib = buf.getvalue()[14:]
    if not user32.OpenClipboard(None):
        raise QQWindowError("打开剪贴板失败")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, dib, len(dib))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_DIB, h):
            raise QQWindowError("写剪贴板失败（图片）")
    finally:
        user32.CloseClipboard()


def _press_ctrl_v() -> None:
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.4)


def _press_enter() -> None:
    user32.keybd_event(VK_RETURN, 0, 0, 0)
    user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.3)


def input_box_point(hwnd: int) -> tuple[int, int]:
    """聊天输入框的大致位置：窗口右下角往上 70px、往左 160px。"""
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - 160, rect.bottom - 70


def send_text(text: str, *, click_input: bool = True) -> None:
    hwnd = find_qq_window()
    _focus(hwnd)
    if click_input:
        x, y = input_box_point(hwnd)
        _click(x, y)
    _set_clipboard_text(text)
    _press_ctrl_v()
    _press_enter()


def send_image(path: str | Path, caption: str = "") -> None:
    p = Path(path)
    if not p.exists():
        raise QQWindowError(f"图片不存在：{p}")
    hwnd = find_qq_window()
    _focus(hwnd)
    x, y = input_box_point(hwnd)
    _click(x, y)
    _set_clipboard_image(p)
    _press_ctrl_v()
    if caption:
        _set_clipboard_text(caption)
        _press_ctrl_v()
    _press_enter()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--image":
        if len(argv) < 2:
            print("需要图片路径")
            return 1
        send_image(argv[1], argv[2] if len(argv) > 2 else "")
        print("已发送图片")
        return 0
    send_text(" ".join(argv))
    print("已发送文字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
