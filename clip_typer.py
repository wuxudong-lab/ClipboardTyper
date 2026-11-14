# -*- coding: utf-8 -*-
"""
剪贴板打字机 (Clipboard Typer)  v1.0.0
=====================================
复制一段文字，按一个快捷键，让它像真人打字一样输入到任意窗口。

两种输出方式:
  * 剪贴板直出 —— 不用粘贴到任何地方，按快捷键时直接输出剪贴板当前内容
  * 编辑内容   —— 在预览框里修改后再输出
可选: 剪贴板一变化就自动输出（复制即输入）。

特性:
  * 全局快捷键开始/停止（界面里可录制任意组合键）
  * 输入速度可调（每字间隔 / 换行额外停顿 / 随机抖动 / 速度预设）
  * 倒计时 + 自动最小化，方便切换到目标窗口
  * 输出前自动校验前台窗口、等待 Ctrl/Alt 松开，避免字符被当成快捷键
  * 支持中文等 Unicode 字符（SendInput + KEYEVENTF_UNICODE）
  * 自动模式：多行内容走粘贴，避免被编辑器的自动缩进/自动补全括号打乱格式
  * 在线判题适配（头歌等禁用粘贴的平台）：补偿式逐字，抵消自动缩进与括号补全
  * 剪贴板保护：粘贴被平台拦截/清空时自动还原，并改用判题逐字重输
  * 头歌专用模式：复制即自动填入编辑框（抢在平台清空剪贴板之前固化内容）
  * 抗补全：换行/删除前先按 Esc 关掉代码提示，避免候选框吃掉按键
  * 直投模式：字符直接投递给浏览器渲染窗口，完全不触发代码提示与自动缩进
  * 极速粘贴模式（适合超长文本）
  * 纯标准库实现，无需 pip 安装任何依赖

仅支持 Windows。
"""

import ctypes
import json
import os
import queue
import random
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox, ttk

try:
    import winsound
except ImportError:  # pragma: no cover - 非 Windows
    winsound = None

APP_NAME = "剪贴板打字机"
APP_VERSION = "1.0.0"
if getattr(sys, "frozen", False):          # PyInstaller 打包后
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _user_config_dir():
    """配置放到用户目录：分发包里就不会夹带别人的设置。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "ClipboardTyper")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return BASE_DIR


CONFIG_PATH = os.path.join(_user_config_dir(), "config.json")

# --------------------------------------------------------------------------- #
# Win32 基础
# --------------------------------------------------------------------------- #
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    winmm = ctypes.WinDLL("winmm")
except OSError:  # pragma: no cover
    winmm = None

ULONG_PTR = ctypes.c_size_t

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_V = 0x56

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = ()
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = ()
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SystemParametersInfoW.argtypes = (wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT)
user32.SystemParametersInfoW.restype = wintypes.BOOL

user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetDesktopWindow.argtypes = ()
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetShellWindow.argtypes = ()
user32.GetShellWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = (wintypes.HWND,)
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetFocus.argtypes = (wintypes.HWND,)
user32.SetFocus.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.EnumChildWindows.argtypes = (wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM)
user32.EnumChildWindows.restype = wintypes.BOOL
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL
user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT

user32.GetClipboardOwner.argtypes = ()
user32.GetClipboardOwner.restype = wintypes.HWND
user32.EnumClipboardFormats.argtypes = (wintypes.UINT,)
user32.EnumClipboardFormats.restype = wintypes.UINT

kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GetCurrentThreadId.argtypes = ()
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# --------------------------------------------------------------------------- #
# 键盘模拟
# --------------------------------------------------------------------------- #
def _key_input(vk=0, scan=0, flags=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = scan
    inp.u.ki.dwFlags = flags
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def _send(events):
    n = len(events)
    if not n:
        return 0
    arr = (INPUT * n)(*events)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def press_vk(vk):
    """按下并释放一个虚拟键。"""
    _send([_key_input(vk=vk), _key_input(vk=vk, flags=KEYEVENTF_KEYUP)])


def type_char(ch):
    """输入单个字符（支持任意 Unicode，含中文/emoji）。"""
    if ch in ("\n", "\r"):
        press_vk(VK_RETURN)
        return
    if ch == "\t":
        press_vk(VK_TAB)
        return
    # 逐 16 位码元发送，兼容 BMP 之外的字符（如 emoji 的代理对）
    data = ch.encode("utf-16-le")
    events = []
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        events.append(_key_input(scan=code, flags=KEYEVENTF_UNICODE))
        events.append(_key_input(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    _send(events)


VK_BACKSPACE = 0x08
VK_DELETE = 0x2E
VK_SHIFT = 0x10
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_HOME = 0x24
VK_END = 0x23
VK_HOME = 0x24
VK_END = 0x23

# 会触发编辑器“自动补全配对符”的开符号
OJ_OPENERS = "([{'\""


def type_char_real(ch):
    """用真实虚拟键码输入字符（ASCII 部分），事件与真人敲键盘一致。

    KEYEVENTF_UNICODE 在浏览器里表现为 keyCode=231(VK_PACKET)，部分带
    输入监控的平台会拦截它。这里把 ASCII 可打印字符转换成
    VkKeyScanW 得到的虚拟键 + Shift，中文等非 ASCII 才回退到 Unicode。
    """
    if ch == "\n":
        press_vk(VK_RETURN)
        return
    if ch == "\r":
        return
    if ch == "\t":
        press_vk(VK_TAB)
        return
    if ord(ch) < 128:
        res = int(user32.VkKeyScanW(ch))
        if res != -1 and res != 0xFFFF:
            vk = res & 0xFF
            mods = (res >> 8) & 0xFF
            if not (mods & ~1):            # 只需要 Shift 才安全，Ctrl/Alt 组合回退
                scan = user32.MapVirtualKeyW(vk, 0)
                events = []
                if mods & 1:
                    events.append(_key_input(vk=VK_SHIFT, scan=0x2A))
                events.append(_key_input(vk=vk, scan=scan))
                events.append(_key_input(vk=vk, scan=scan, flags=KEYEVENTF_KEYUP))
                if mods & 1:
                    events.append(_key_input(vk=VK_SHIFT, scan=0x2A,
                                             flags=KEYEVENTF_KEYUP))
                _send(events)
                return
    type_char(ch)


WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101


def find_render_widget(hwnd):
    """找到 Chromium 内核的渲染窗口（WM_CHAR 投给它就能直接插入字符）。"""
    if not is_window(hwnd):
        return 0
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(h, _l):
        if get_window_class(h).startswith("Chrome_RenderWidgetHost"):
            found.append(h)
        return True

    try:
        user32.EnumChildWindows(hwnd, _cb, 0)
    except Exception:  # noqa: BLE001
        return 0
    return found[0] if found else 0


def post_char(render, ch):
    """把字符直接投递到渲染窗口：不产生 keydown，编辑器的补全/括号补全都不会触发。"""
    data = ch.encode("utf-16-le")
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        user32.PostMessageW(render, WM_CHAR, code, 1)


def post_enter(render):
    user32.PostMessageW(render, WM_KEYDOWN, VK_RETURN, 0)
    user32.PostMessageW(render, WM_KEYUP, VK_RETURN, 0)


def oj_dismiss_hint(strong=True):
    """关掉编辑器的自动补全候选框。

    候选框打开时，Enter 会被当成“接受候选”（插进一个 SELECT 之类），
    Tab/Delete 也会被吞掉，导致输出错乱。

    只按 Esc 对很多编辑器无效（头歌的提示框就是这种），所以再补一招：
    输入一个空格（非字母字符通常会关闭候选框）再退格删掉它，净效果为零。
    """
    press_vk(VK_ESCAPE)
    if strong:
        press_vk(VK_SPACE)
        press_vk(VK_BACKSPACE)


def oj_emit_char(ch, real_keys=False, dismiss=True):
    """在线判题适配：输入一个字符，并删掉编辑器自动补出的配对符。"""
    if ch == "\t":
        if dismiss:
            oj_dismiss_hint()
            time.sleep(0.012)
        press_vk(VK_TAB)
        return
    (type_char_real if real_keys else type_char)(ch)
    if ch in OJ_OPENERS:
        time.sleep(0.015)           # 等编辑器把配对符补出来
        if dismiss:
            oj_dismiss_hint()
            time.sleep(0.012)
        press_vk(VK_DELETE)


def send_paste():
    """发送 Ctrl+V。"""
    _send([
        _key_input(vk=VK_CONTROL),
        _key_input(vk=VK_V),
        _key_input(vk=VK_V, flags=KEYEVENTF_KEYUP),
        _key_input(vk=VK_CONTROL, flags=KEYEVENTF_KEYUP),
    ])


# --------------------------------------------------------------------------- #
# 剪贴板
# --------------------------------------------------------------------------- #
CF_TEXT = 1

FORMAT_NAMES = {
    1: "CF_TEXT", 2: "CF_BITMAP", 3: "CF_METAFILEPICT", 8: "CF_DIB",
    13: "CF_UNICODETEXT", 14: "CF_ENHMETAFILE", 15: "CF_HDROP", 16: "CF_LOCALE",
    17: "CF_DIBV5",
}


def read_clipboard(retries=8):
    """读取剪贴板文本，失败返回 None。

    先取 CF_UNICODETEXT；没有则退回 CF_TEXT（ANSI）。
    """
    for _ in range(retries):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            ptr = kernel32.GlobalLock(handle)
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                    if text:
                        return text
                finally:
                    kernel32.GlobalUnlock(handle)
        # 兜底：某些程序只放了 ANSI 文本
        handle = user32.GetClipboardData(CF_TEXT)
        if handle:
            ptr = kernel32.GlobalLock(handle)
            if ptr:
                try:
                    raw = ctypes.string_at(ptr)
                    for enc in ("mbcs", "utf-8", "gbk"):
                        try:
                            text = raw.decode(enc).strip("\x00")
                            if text:
                                return text
                        except (UnicodeDecodeError, LookupError):
                            continue
                finally:
                    kernel32.GlobalUnlock(handle)
        return None
    finally:
        user32.CloseClipboard()


def clipboard_report():
    """剪贴板诊断信息（供“诊断”面板使用）。"""
    lines = []
    opened = False
    for _ in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.03)
    if not opened:
        return ["能否打开: 否（被其它程序占用）"]
    try:
        lines.append("能否打开: 是")
        fmts = []
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            name = FORMAT_NAMES.get(fmt, "")
            if fmt == 49161:
                name = "HTML Format"
            elif fmt == 49158:
                name = "Rich Text Format"
            fmts.append("%s(%d)" % (name, fmt) if name else str(fmt))
            fmt = user32.EnumClipboardFormats(fmt)
        lines.append("格式: " + ("、".join(fmts) if fmts else "空"))

        h = user32.GetClipboardData(CF_UNICODETEXT)
        if h:
            size = kernel32.GlobalSize(h)
            ptr = kernel32.GlobalLock(h)
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(h)
                lines.append("CF_UNICODETEXT: %d 字符（%d 字节）" % (len(text), size))
                preview = text.replace("\n", "⏎")[:60]
                lines.append("内容预览: " + (preview if preview else "（空）"))
            else:
                lines.append("CF_UNICODETEXT: 有数据但无法读取")
        else:
            lines.append("CF_UNICODETEXT: 无")

        owner = user32.GetClipboardOwner()
        if owner:
            lines.append("剪贴板所属: %s [%s]"
                         % (get_window_title(owner)[:40] or "?", get_window_class(owner)))
        else:
            lines.append("剪贴板所属: 未知（可能是延迟渲染）")
    finally:
        user32.CloseClipboard()
    return lines


def write_clipboard(text, retries=8):
    """写入剪贴板文本，成功返回 True。"""
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(retries):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # 成功后内存归系统所有，不能再释放
        return True
    finally:
        user32.CloseClipboard()


class ClipboardKeeper(threading.Thread):
    """输出期间盯着剪贴板：一旦被平台清空就立刻写回。

    头歌之类的防作弊脚本会在 paste/键盘事件里清空剪贴板，甚至延迟清空，
    补一次不够。这个守护线程在输出期间持续巡检，结束后再多守一会儿。
    """

    def __init__(self, snapshot, interval=0.22, linger=2.5):
        super().__init__(daemon=True)
        self.snapshot = snapshot
        self.interval = interval
        self.linger = linger
        self.restored = 0
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        deadline = None
        while True:
            if self._stop_evt.is_set() and deadline is None:
                deadline = time.monotonic() + self.linger
            if deadline is not None and time.monotonic() > deadline:
                return
            try:
                cur = read_clipboard(retries=1)
                if cur is None or cur == "":
                    if write_clipboard(self.snapshot, retries=3):
                        self.restored += 1
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.interval)


def clipboard_restore(snapshot, written=None):
    """把剪贴板恢复成 snapshot。

    written 是本次程序写入的内容。只有当前剪贴板仍是它、或已被平台清空时
    才恢复；用户期间自己复制了新内容则不动。
    """
    if not snapshot:
        return False
    cur = read_clipboard(retries=5)
    if cur == snapshot:
        return True
    if cur is None or cur == "" or (written is not None and cur == written):
        return write_clipboard(snapshot, retries=6)
    return False


def normalize_text(raw):
    return raw.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- #
# 快捷键解析
# --------------------------------------------------------------------------- #
VK_SPECIAL = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "prior": 0x21, "next": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "pause": 0x13, "printscreen": 0x2C, "numlock": 0x90, "scrolllock": 0x91,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}

_FKEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")


def is_function_key(key):
    return bool(_FKEY_RE.match(key.lower()))


def key_to_vk(key):
    key = key.lower()
    if key in VK_SPECIAL:
        return VK_SPECIAL[key]
    if is_function_key(key):
        return 0x70 + int(key[1:]) - 1
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return ord(key.upper())
    return None


def parse_hotkey(text):
    """'ctrl+alt+f9' -> (mods, vk)，非法时抛 ValueError。"""
    parts = [p for p in re.split(r"[+\s]+", str(text).strip().lower()) if p]
    if not parts:
        raise ValueError("快捷键为空")
    key = parts[-1]
    mods = 0
    for m in parts[:-1]:
        if m in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif m == "alt":
            mods |= MOD_ALT
        elif m == "shift":
            mods |= MOD_SHIFT
        elif m in ("win", "super", "meta"):
            mods |= MOD_WIN
        else:
            raise ValueError("未知修饰键：%s" % m)
    vk = key_to_vk(key)
    if vk is None:
        raise ValueError("不支持的按键：%s" % key)
    if mods == 0 and not is_function_key(key):
        raise ValueError("字母/数字等按键需要搭配 Ctrl / Alt / Shift")
    return mods | MOD_NOREPEAT, vk


MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Shift_L", "Shift_R", "Alt_L", "Alt_R",
    "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R", "Caps_Lock",
}

KEYSYM_MAP = {
    "return": "enter", "kp_enter": "enter", "prior": "pageup", "next": "pagedown",
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "backslash": "\\", "semicolon": ";", "apostrophe": "'", "comma": ",",
    "period": ".", "slash": "/", "grave": "`", "space": "space",
    "backspace": "backspace", "insert": "insert", "delete": "delete",
    "home": "home", "end": "end", "left": "left", "right": "right",
    "up": "up", "down": "down", "tab": "tab",
}


def keysym_to_hotkey(keysym, state):
    """把 tk 的按键事件转成 'ctrl+alt+f9' 形式，返回 (字符串, 错误信息)。"""
    if keysym in MODIFIER_KEYSYMS:
        return None, None
    name = KEYSYM_MAP.get(keysym.lower(), keysym.lower())
    if not (is_function_key(name) or len(name) == 1):
        return None, "暂不支持这个按键"
    mods = []
    if state & 0x4:
        mods.append("ctrl")
    if state & 0x1:
        mods.append("shift")
    if state & 0x20000 or state & 0x8:
        mods.append("alt")
    if not mods and not is_function_key(name):
        return None, "请至少加一个 Ctrl / Alt / Shift"
    combo = "+".join(mods + [name])
    if key_to_vk(name) is None:
        return None, "暂不支持这个按键"
    return combo, None


def pretty_hotkey(combo):
    """f9 -> F9；ctrl+alt+t -> Ctrl + Alt + T"""
    parts = [p for p in re.split(r"[+\s]+", str(combo).strip()) if p]
    names = []
    for p in parts:
        low = p.lower()
        if low in ("ctrl", "control"):
            names.append("Ctrl")
        elif low == "alt":
            names.append("Alt")
        elif low == "shift":
            names.append("Shift")
        elif low in ("win", "super", "meta"):
            names.append("Win")
        else:
            names.append(p.upper() if len(p) <= 3 else p.capitalize())
    return " + ".join(names)


# --------------------------------------------------------------------------- #
# 全局快捷键监听线程
# --------------------------------------------------------------------------- #
HK_TOGGLE = 1
HK_STOP = 2


# 默认热键被其它程序占用时，按顺序尝试的备选
FALLBACK_TOGGLE = ["ctrl+alt+v", "ctrl+shift+v", "alt+v", "ctrl+alt+b", "f9"]
FALLBACK_STOP = ["alt+t", "alt+s", "alt+f10", "f10"]


class GlobalHotkeys:
    def __init__(self, out_queue):
        self.q = out_queue
        self._thread = None
        self._tid = None
        self._lock = threading.Lock()

    def start(self, bindings):
        """bindings: [(id, mods, vk), ...]"""
        self.stop()
        with self._lock:
            self._thread = threading.Thread(target=self._run, args=(list(bindings),), daemon=True)
            self._thread.start()

    def _run(self, bindings):
        self._tid = kernel32.GetCurrentThreadId()
        registered = []
        for hid, mods, vk in bindings:
            if user32.RegisterHotKey(None, hid, mods, vk):
                registered.append(hid)
            else:
                self.q.put(("hotkey_fail", hid))
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY:
                self.q.put(("hotkey", int(msg.wParam)))
        for hid in registered:
            user32.UnregisterHotKey(None, hid)
        self._tid = None

    def stop(self):
        with self._lock:
            thread, tid = self._thread, self._tid
            self._thread = None
        if thread and thread.is_alive():
            if tid:
                user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            thread.join(timeout=1.0)


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def flash_taskbar(window):
    """任务栏图标闪烁（不抢焦点）。"""
    try:
        hwnd = int(window.wm_frame(), 16)
        info = FLASHWINFO()
        info.cbSize = ctypes.sizeof(FLASHWINFO)
        info.hwnd = hwnd
        info.dwFlags = 0x00000002 | 0x0000000C  # FLASHW_TRAY | FLASHW_TIMERNOFG
        info.uCount = 3
        info.dwTimeout = 0
        user32.FlashWindowEx(ctypes.byref(info))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 焦点与修饰键
# --------------------------------------------------------------------------- #
# 修饰键的通用 + 左右分开的虚拟键码
MODIFIER_VKS = (0x11, 0x10, 0x12, 0x5B, 0x5C,      # Ctrl Shift Alt LWin RWin
                0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)  # LShift RShift LCtrl RCtrl LAlt RAlt


def get_foreground_window():
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def get_window_title(hwnd):
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def is_window(hwnd):
    try:
        return bool(hwnd) and bool(user32.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def is_shell_window(hwnd):
    """桌面 / 任务栏等外壳窗口（此时没有真正的前台应用）。"""
    if not hwnd:
        return True
    try:
        return hwnd in (int(user32.GetDesktopWindow() or 0),
                        int(user32.GetShellWindow() or 0))
    except Exception:  # noqa: BLE001
        return False


def get_window_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


# 提示气泡、输入法候选框、右键菜单等不是“输出目标”
SKIP_WINDOW_CLASSES = {
    "tooltips_class32", "IME", "MSCTFIME UI", "SysShadow", "#32768",
    "TaskListThumbnailWnd", "ForegroundStaging",
}


def is_typing_target(hwnd):
    """判断窗口是否像是一个可以接收输入的应用窗口。"""
    if not hwnd or is_shell_window(hwnd):
        return False
    if get_window_class(hwnd) in SKIP_WINDOW_CLASSES:
        return False
    return is_window(hwnd)


def activate_window(hwnd):
    """把窗口切到前台，成功返回 True。"""
    if not is_window(hwnd):
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        if get_foreground_window() == hwnd:
            return True
        user32.SetForegroundWindow(hwnd)
        if get_foreground_window() == hwnd:
            return True
        # 前台锁定时借用前台线程的输入队列再试一次
        fg = get_foreground_window()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = kernel32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid_me, tid_fg, False)
        return get_foreground_window() == hwnd
    except Exception:  # noqa: BLE001
        return False


def modifiers_down():
    """是否有 Ctrl / Shift / Alt / Win 正被按住。"""
    for vk in MODIFIER_VKS:
        try:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def wait_modifiers_released(timeout=1.2):
    """等修饰键松开；超时返回 False。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not modifiers_down():
            return True
        time.sleep(0.02)
    return not modifiers_down()


def force_release_modifiers():
    """强制发送修饰键抬起，避免 Ctrl/Alt 被目标程序当成组合键。"""
    events = [_key_input(vk=vk, flags=KEYEVENTF_KEYUP) for vk in MODIFIER_VKS]
    _send(events)


# --------------------------------------------------------------------------- #
# 配色
# --------------------------------------------------------------------------- #
BG = "#101216"
PANEL = "#171a21"
PANEL2 = "#1f232c"
PANEL3 = "#2a2f3a"
BORDER = "#232833"
FG = "#eef1f6"
FG_DIM = "#c5cbd5"
MUTED = "#7d8592"
ACCENT = "#5b8cff"
ACCENT_DARK = "#3f6fe0"
ACCENT_SOFT = "#1e2a44"
GREEN = "#3ddc97"
RED = "#ff6b6b"
AMBER = "#ffb84d"
FONT = "Microsoft YaHei UI"

SOURCE_CLIP = "clip"
SOURCE_EDIT = "edit"
MODE_AUTO = "自动"
MODE_TYPE = "逐字模拟"
MODE_PASTE = "极速粘贴"
MODE_OJ = "在线判题"

# 头歌 / 在线判题平台窗口标题特征（大小写不敏感）
OJ_TITLE_KEYWORDS = (
    "头歌", "educoder", "实践教学平台", "实训平台", "实践教学",
    "在线判题", "online judge", "代码编辑器", "编程实训", "实验平台",
)
# 浏览器窗口的类名，用于弱特征辅助判断
BROWSER_CLASSES = {"Chrome_WidgetWin_1", "MozillaWindowClass",
                   "ApplicationFrameWindow", "Chrome_WidgetWin_0"}


def is_oj_window(title, window_class=""):
    t = (title or "").lower()
    if any(k.lower() in t for k in OJ_TITLE_KEYWORDS):
        return True
    # 浏览器里的关卡/实验页，标题常常只有关卡名
    if window_class in BROWSER_CLASSES and any(
            k in t for k in ("关卡", "实训", "实验", "作业", "编程题", "在线评测")):
        return True
    return False


def resolve_mode(text, mode, oj_detected=False, force_oj=False, is_browser=False):
    """把“自动”解析成具体方式，返回 (方式, 说明)。

    逐字模拟等于真实按键：网页/代码编辑器会当成键盘输入，触发
    “回车自动缩进”“括号自动补全”，多行内容的缩进会越打越歪、
    结尾还会多出右括号。所以多行或含制表符的内容改用粘贴，其余仍然逐字输入。
    """
    if force_oj:
        return MODE_OJ, "已锁定在线判题适配（专用逐字，不依赖粘贴）"
    if mode != MODE_AUTO:
        return mode, ""
    if oj_detected:
        # 头歌等平台禁用粘贴，只能逐字；用补偿算法抵消编辑器的自动缩进/补全括号
        return MODE_OJ, "检测到在线判题平台，已启用专用逐字适配（不依赖粘贴）"
    if "\n" in text or "\t" in text:
        if is_browser:
            # 浏览器里的多行内容一律走逐字：粘贴可能被平台拦截并清空剪贴板，
            # 那样既没输出、还会弄丢剪贴板，代价太大
            return MODE_OJ, "浏览器中的多行内容，已改用判题逐字（不触碰剪贴板）"
        return MODE_PASTE, "多行内容，已自动改用粘贴以保持原格式"
    return MODE_TYPE, ""

DEFAULT_CFG = {
    "hotkey": "ctrl+alt+v",
    "stop_hotkey": "alt+t",
    "source": SOURCE_EDIT,
    "mode": MODE_AUTO,
    "cfg_version": 1,
    "interval": 10,
    "line_delay": 120,
    "jitter": 0,
    "countdown": 1,
    "repeat": 1,
    "minimize": True,
    "beep": True,
    "auto_output": False,
    "oj_mode": True,
    "oj_dismiss": True,
    "oj_direct": True,
    "real_keys": False,
}


def load_config():
    """读取用户配置；不存在或损坏时使用默认值。"""
    cfg = dict(DEFAULT_CFG)
    src = CONFIG_PATH
    if not os.path.exists(src) and os.path.exists(LEGACY_CONFIG_PATH):
        src = LEGACY_CONFIG_PATH      # 便携用法：配置也可以放在程序目录
    try:
        with open(src, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k in cfg:
                if k in data:
                    cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


# --------------------------------------------------------------------------- #
# 自绘控件
# --------------------------------------------------------------------------- #
class Switch(tk.Canvas):
    """明确的开关：绿色=开，灰色=关。"""

    W = 46
    H = 24

    def __init__(self, parent, variable, bg=PANEL, command=None):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _toggle(self, _event=None):
        self.var.set(not bool(self.var.get()))
        if self.command:
            self.command(self.var.get())

    def _redraw(self):
        self.delete("all")
        on = bool(self.var.get())
        track = GREEN if on else "#3d434c"
        r = self.H / 2.0
        self.create_oval(0, 0, self.H, self.H, fill=track, outline=track)
        self.create_oval(self.W - self.H, 0, self.W, self.H, fill=track, outline=track)
        self.create_rectangle(r, 0, self.W - r, self.H, fill=track, outline=track)
        pad = 3
        d = self.H - pad * 2
        x = self.W - self.H + pad if on else pad
        self.create_oval(x, pad, x + d, pad + d,
                         fill="#ffffff" if on else "#9aa0a6", outline="")


class ProgressBar(tk.Canvas):
    """自绘进度条：深色轨道 + 蓝色进度。"""

    def __init__(self, parent, bg=BG, height=8):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0)
        self.h = height
        self.value = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, value):
        try:
            self.value = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            self.value = 0.0
        self._draw()

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            return
        cy = self.h / 2.0
        r = self.h / 2.0
        self._round(0, 0, w, self.h, r, "#3a3f48")
        if self.value > 0:
            x2 = max(2 * r, w * self.value / 100.0)
            self._round(0, 0, x2, self.h, r, ACCENT)

    def _round(self, x1, y1, x2, y2, r, color):
        if x2 - x1 < 2 * r:
            x2 = x1 + 2 * r
        self.create_oval(x1, y1, x1 + 2 * r, y2, fill=color, outline=color)
        self.create_oval(x2 - 2 * r, y1, x2, y2, fill=color, outline=color)
        self.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline=color)


class Slider(tk.Canvas):
    """自绘滑杆：圆角轨道 + 蓝色已选段 + 圆形滑块。"""

    def __init__(self, parent, variable, from_=0, to=300, width=190, height=24,
                 bg=PANEL, command=None):
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.lo = from_
        self.hi = to
        self.w = width
        self.h = height
        self.command = command
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<MouseWheel>", self._on_wheel)
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    # ---- 数值换算 ---- #
    def _pad(self):
        return 9

    def _value(self):
        try:
            v = float(self.var.get())
        except (TypeError, ValueError):
            return self.lo
        return max(self.lo, min(self.hi, v))

    def _x_of(self, value):
        pad = self._pad()
        span = self.hi - self.lo or 1
        return pad + (value - self.lo) / span * (self.w - 2 * pad)

    def _value_of(self, x):
        pad = self._pad()
        span = self.hi - self.lo or 1
        ratio = (x - pad) / float(self.w - 2 * pad)
        return self.lo + max(0.0, min(1.0, ratio)) * span

    def set(self, value):
        self.var.set(str(int(round(float(value)))))

    # ---- 交互 ---- #
    def _apply(self, event):
        self.set(self._value_of(event.x))
        if self.command:
            self.command(str(self._value()))

    def _on_press(self, event):
        self._apply(event)

    def _on_drag(self, event):
        self._apply(event)

    def _on_wheel(self, event):
        step = 5 if event.delta > 0 else -5
        self.set(max(self.lo, min(self.hi, self._value() + step)))
        if self.command:
            self.command(str(self._value()))

    # ---- 绘制 ---- #
    def _draw(self):
        self.delete("all")
        pad = self._pad()
        cy = self.h / 2.0
        bar_h = 6
        x = self._x_of(self._value())
        # 轨道
        self._round_rect(pad, cy - bar_h / 2, self.w - pad, cy + bar_h / 2,
                         bar_h / 2, "#3a3f48")
        # 已选段
        if x > pad:
            self._round_rect(pad, cy - bar_h / 2, x, cy + bar_h / 2, bar_h / 2, ACCENT)
        # 滑块
        r = 7
        self.create_oval(x - r, cy - r, x + r, cy + r, fill="#ffffff", outline=ACCENT, width=2)

    def _round_rect(self, x1, y1, x2, y2, r, color):
        if x2 - x1 < 2 * r:
            x2 = x1 + 2 * r
        self.create_oval(x1, y1, x1 + 2 * r, y2, fill=color, outline=color)
        self.create_oval(x2 - 2 * r, y1, x2, y2, fill=color, outline=color)
        self.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline=color)


def draw_round_rect(canvas, x1, y1, x2, y2, r, fill):
    """在 Canvas 上画一个圆角矩形（4 个圆 + 2 个矩形拼接）。"""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    canvas.create_oval(x1, y1, x1 + 2 * r, y2, fill=fill, outline=fill)
    canvas.create_oval(x2 - 2 * r, y1, x2, y2, fill=fill, outline=fill)
    canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill)


class RoundButton(tk.Canvas):
    """自绘圆角按钮：primary / ghost / danger 三种风格，带 hover。"""

    PAD_X = 16

    def __init__(self, parent, text, command, kind="ghost", height=34,
                 bg=BG, font_size=9, min_width=62):
        self._text = text
        self._cmd = command
        self._kind = kind
        self._h = height
        self._fs = font_size
        self._min_w = min_width
        self._state = "normal"
        self._hover = False
        super().__init__(parent, width=self._measure(text), height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    def _measure(self, text):
        width = 0.0
        for ch in text:
            width += self._fs * (1.05 if ord(ch) > 127 else 0.62)
        return max(self._min_w, int(width + self.PAD_X * 2))

    def configure(self, **kw):
        redraw = False
        if "text" in kw:
            self._text = kw.pop("text")
            redraw = True
        if "state" in kw:
            self._state = kw.pop("state")
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
            redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()

    config = configure

    def _on_click(self, _e=None):
        if self._state == "disabled":
            return
        if self._cmd:
            self._cmd()

    def _on_enter(self, _e=None):
        if self._state != "disabled":
            self._hover = True
            self._draw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._draw()

    def _palette(self):
        if self._state == "disabled":
            return PANEL2, "#555c68"
        if self._kind == "primary":
            return (ACCENT_DARK if self._hover else ACCENT), "#ffffff"
        if self._kind == "danger":
            return (PANEL2 if not self._hover else "#35262b"), (RED if not self._hover else "#ff9d9d")
        return (PANEL2 if not self._hover else PANEL3), (FG_DIM if not self._hover else FG)

    def _draw(self):
        self.delete("all")
        w = self._measure(self._text)
        try:
            cur = int(float(self["width"]))
        except (TypeError, ValueError):
            cur = -1
        if cur != w:
            super().configure(width=w)
        fill, fg = self._palette()
        draw_round_rect(self, 0.5, 0.5, w - 0.5, self._h - 0.5, self._h / 2 - 0.5, fill)
        self.create_text(w / 2, self._h / 2 + 0.5, text=self._text, fill=fg,
                         font=(FONT, self._fs, "bold" if self._kind == "primary" else "normal"))


class Segmented(tk.Frame):
    """分段选择器：选中项高亮为蓝色，一眼可见。"""

    def __init__(self, parent, options, variable, bg=PANEL, command=None):
        super().__init__(parent, bg=bg)
        self.var = variable
        self.command = command
        self.disabled = set()
        self.items = {}
        for i, (value, label) in enumerate(options):
            lb = tk.Label(self, text=label, font=(FONT, 9), padx=14, pady=5,
                          cursor="hand2", bd=0)
            lb.grid(row=0, column=i, padx=(0 if i == 0 else 3, 0))
            lb.bind("<Button-1>", lambda _e, v=value: self.select(v))
            self.items[value] = lb
        self.var.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def set_disabled(self, values, fallback=None):
        """禁用若干选项（例如头歌模式下不允许「剪贴板直出」）。"""
        self.disabled = set(values or ())
        if self.var.get() in self.disabled and fallback is not None:
            self.var.set(fallback)
        self.refresh()

    def select(self, value):
        if value in self.disabled:
            return
        if self.var.get() != value:
            self.var.set(value)
            if self.command:
                self.command(value)

    def refresh(self):
        cur = self.var.get()
        for value, lb in self.items.items():
            if value in self.disabled:
                lb.configure(bg=PANEL2, fg="#464c56", cursor="arrow",
                             font=(FONT, 9))
            elif value == cur:
                lb.configure(bg=ACCENT, fg="#ffffff", cursor="hand2",
                             font=(FONT, 9, "bold"))
            else:
                lb.configure(bg=PANEL2, fg=MUTED, cursor="hand2",
                             font=(FONT, 9))


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.q = queue.Queue()
        self.hk = GlobalHotkeys(self.q)

        self.state = "idle"          # idle / countdown / typing
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self._typing_thread = None
        self._cd_token = None
        self._last_clip = None
        self._last_output = None
        self._clip_guard = 0.0
        self._target_hwnd = 0
        self._target_title = ""
        self._last_foreign_hwnd = 0
        self._force_target = False
        self._oj_detected = False
        self._target_is_browser = False
        self._filling = False
        self._paste_blocked = False
        self._saw_paste_block = False
        self._clip_snapshot = None
        self._current_text = ""
        self._chips = {}

        root.title(APP_NAME)
        root.minsize(760, 600)
        root.configure(bg=BG)

        self._init_style()
        self._build_ui()
        self._apply_hotkeys()
        self._reload_clipboard(silent=True)

        self._poll()
        self._watch_clipboard()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 样式 ---------------- #
    def _init_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=FG, font=(FONT, 9))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=(FONT, 9))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("Card.TLabel", background=PANEL, foreground=FG, font=(FONT, 9))
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 16, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                        focusthickness=0, padding=(12, 6), font=(FONT, 9))
        style.map("TButton",
                  background=[("pressed", PANEL3), ("active", "#3a3e46"), ("disabled", "#26282c")],
                  foreground=[("disabled", "#5c6066")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        borderwidth=0, padding=(18, 8), font=(FONT, 10, "bold"))
        style.map("Accent.TButton",
                  background=[("pressed", "#3b7ae8"), ("active", "#5c98ff"), ("disabled", "#31415c")],
                  foreground=[("disabled", "#8fa3bf")])
        style.configure("Danger.TButton", background="#3a2b2d", foreground=RED, borderwidth=0,
                        padding=(12, 7), font=(FONT, 9))
        style.map("Danger.TButton",
                  background=[("pressed", "#4a3335"), ("active", "#4a3335"), ("disabled", "#26282c")],
                  foreground=[("disabled", "#6a5a5c")])
        style.configure("TSpinbox", fieldbackground=PANEL2, foreground=FG, insertcolor=FG,
                        arrowcolor=FG, bordercolor=PANEL2, lightcolor=PANEL2,
                        darkcolor=PANEL2, padding=3, arrowsize=11)
        style.configure("TScale", background=PANEL, troughcolor=PANEL3, borderwidth=0,
                        sliderlength=16, lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#3a3f48",
                        borderwidth=0, thickness=6, bordercolor="#3a3f48",
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Horizontal.TProgressbar",
                  background=[("disabled", "#3a3f48")])
        for orient in ("Vertical", "Horizontal"):
            style.configure(orient + ".TScrollbar", background="#3d434c", troughcolor=PANEL,
                            bordercolor=PANEL, lightcolor="#3d434c", darkcolor="#3d434c",
                            arrowcolor=MUTED, borderwidth=0, arrowsize=12, gripcount=0)
            style.map(orient + ".TScrollbar",
                      background=[("pressed", "#4a515b"), ("active", "#4a515b")],
                      arrowcolor=[("pressed", FG), ("active", FG)])

    # ---------------- 界面 ---------------- #
    def _build_ui(self):
        root = self.root
        outer = ttk.Frame(root, padding=(18, 14, 18, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self._build_header(outer)
        self._build_source_row(outer)
        self._build_text_area(outer)
        self._build_settings(outer)
        self._build_switches(outer)
        self._build_actions(outer)

        self._on_source_change(self.source_var.get())
        self._apply_oj_lock()
        self.set_status("就绪")
        self._update_hotkey_labels()
        self._update_count()
        self._place_window()

    def _build_header(self, parent):
        head = ttk.Frame(parent)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        head.columnconfigure(1, weight=1)

        brand = ttk.Frame(head)
        brand.grid(row=0, column=0, sticky="w")

        icon = tk.Canvas(brand, width=40, height=40, bg=BG,
                         highlightthickness=0, bd=0)
        icon.pack(side="left", padx=(0, 11))
        draw_round_rect(icon, 0, 0, 40, 40, 12, ACCENT)
        icon.create_text(20, 21, text="剪", fill="#ffffff", font=(FONT, 15, "bold"))

        box = ttk.Frame(brand)
        box.pack(side="left")
        ttk.Label(box, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, text="复制即输入 · 一键直出", style="Muted.TLabel").pack(anchor="w")

        right = ttk.Frame(head)
        right.grid(row=0, column=1, sticky="e")
        tk.Label(right, text="v" + APP_VERSION, bg=PANEL2, fg=MUTED, font=(FONT, 8),
                 padx=8, pady=2).pack(side="left", padx=(0, 8))
        self.pill = tk.Label(right, text="● 就绪", bg=PANEL, fg=MUTED,
                             font=(FONT, 9), padx=13, pady=6)
        self.pill.pack(side="left")

    def _build_source_row(self, parent):
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(2, weight=1)

        ttk.Label(bar, text="输出内容", style="Muted.TLabel").grid(row=0, column=0, sticky="w")

        self.source_var = tk.StringVar(value=self.cfg.get("source", SOURCE_CLIP))
        self.seg_source = Segmented(
            bar,
            [(SOURCE_CLIP, "剪贴板直出"), (SOURCE_EDIT, "编辑内容")],
            self.source_var,
            bg=BG,
        )
        self.seg_source.grid(row=0, column=1, sticky="w", padx=(10, 12))
        self.source_var.trace_add("write", lambda *_: self._on_source_change(self.source_var.get()))

        self.source_hint = ttk.Label(bar, text="", style="Muted.TLabel")
        self.source_hint.grid(row=0, column=2, sticky="w")

        right = ttk.Frame(bar)
        right.grid(row=0, column=3, sticky="e")
        self.count_var = tk.StringVar(value="0 字符")
        ttk.Label(right, textvariable=self.count_var, style="Muted.TLabel").pack(side="left", padx=(0, 10))
        self.btn_read = RoundButton(right, "读取剪贴板", self.read_clipboard,
                                    bg=BG, height=30, font_size=9)
        self.btn_read.pack(side="left")
        self.btn_clear = RoundButton(right, "清空", self.clear_text,
                                     bg=BG, height=30, font_size=9, min_width=56)
        self.btn_clear.pack(side="left", padx=(6, 0))
        self.btn_diag = RoundButton(right, "诊断", self.show_diagnostics,
                                    bg=BG, height=30, font_size=9, min_width=56)
        self.btn_diag.pack(side="left", padx=(6, 0))

    def _build_text_area(self, parent):
        wrap = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                        highlightthickness=1, bd=0)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.text = tk.Text(wrap, wrap="char", bg=PANEL, fg=FG_DIM, insertbackground=FG,
                            relief="flat", bd=0, padx=14, pady=12, font=(FONT, 10),
                            selectbackground="#3a4a68", selectforeground="#ffffff",
                            undo=True, spacing1=2, spacing3=3, height=6, width=20)
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)
        self.text.bind("<<Modified>>", self._on_text_modified)

    def _build_settings(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12))
        card.grid(row=3, column=0, sticky="ew", pady=(10, 0))

        # 第 1 行：输入方式 + 换行停顿 + 抖动 + 倒计时 + 重复
        r1 = ttk.Frame(card, style="Card.TFrame")
        r1.grid(row=0, column=0, sticky="w", pady=(0, 10))
        ttk.Label(r1, text="输入方式", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.mode_var = tk.StringVar(value=self.cfg.get("mode", MODE_AUTO))
        Segmented(r1, [(MODE_AUTO, "自动"), (MODE_TYPE, "逐字"),
                       (MODE_PASTE, "粘贴"), (MODE_OJ, "判题")],
                  self.mode_var, bg=PANEL).pack(side="left", padx=(0, 14))

        def num_field(parent, label, value, frm, to, inc, unit, width=5):
            ttk.Label(parent, text=label, style="CardMuted.TLabel").pack(side="left", padx=(0, 6))
            var = self._int_var(value)
            sp = ttk.Spinbox(parent, from_=frm, to=to, increment=inc, width=width,
                             textvariable=var, justify="center")
            sp.pack(side="left")
            sp.bind("<FocusOut>", lambda _e, w=sp, v=var: self._refresh_num(w, v))
            sp.bind("<Return>", lambda _e, w=sp, v=var: self._refresh_num(w, v))
            ttk.Label(parent, text=unit, style="CardMuted.TLabel").pack(side="left", padx=(4, 20))
            return var

        self.line_delay_var = num_field(r1, "换行停顿", self.cfg.get("line_delay", 120), 0, 5000, 20, "ms")
        self.jitter_var = num_field(r1, "随机抖动", self.cfg.get("jitter", 0), 0, 2000, 5, "ms")
        self.countdown_var = num_field(r1, "倒计时", self.cfg.get("countdown", 3), 0, 30, 1, "s", 3)
        self.repeat_var = num_field(r1, "重复", self.cfg.get("repeat", 1), 1, 999, 1, "次", 3)

        # 第 2 行：每字间隔滑杆 + 数字框 + 速度预设
        r2 = ttk.Frame(card, style="Card.TFrame")
        r2.grid(row=1, column=0, sticky="w", pady=(0, 10))
        ttk.Label(r2, text="每字间隔", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.interval_var = self._int_var(self.cfg.get("interval", 25))
        self.scale = Slider(r2, self.interval_var, from_=0, to=300, width=190,
                            bg=PANEL, command=self._on_scale)
        self.scale.pack(side="left")
        self.spin_interval = ttk.Spinbox(r2, from_=0, to=2000, increment=5, width=5,
                                         textvariable=self.interval_var, justify="center")
        self.spin_interval.pack(side="left", padx=(8, 4))
        ttk.Label(r2, text="ms", style="CardMuted.TLabel").pack(side="left", padx=(0, 18))

        for text, value in (("很慢", 120), ("慢", 60), ("正常", 25), ("快", 10), ("极速", 0)):
            chip = tk.Label(r2, text=text, bg=PANEL2, fg=MUTED, font=(FONT, 8),
                            padx=9, pady=3, cursor="hand2")
            chip.pack(side="left", padx=(0, 4))
            chip.bind("<Button-1>", lambda _e, v=value: self._apply_preset(v))
            chip.bind("<Enter>", lambda _e, w=chip: w.configure(bg=PANEL3))
            chip.bind("<Leave>", lambda _e, w=chip: self._refresh_chips())
            self._chips[value] = chip

        # 第 3 行：快捷键
        r3 = ttk.Frame(card, style="Card.TFrame")
        r3.grid(row=2, column=0, sticky="ew")
        r3.columnconfigure(0, weight=1)
        ttk.Label(r3, text="开始 / 停止", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.kcap_start = tk.Label(r3, text="F9", bg=PANEL2, fg=FG, font=(FONT, 10, "bold"),
                                   padx=16, pady=3)
        self.kcap_start.pack(side="left")
        RoundButton(r3, "录制", lambda: self._record_hotkey("hotkey"),
                    bg=PANEL, height=28, font_size=8, min_width=52).pack(side="left", padx=(6, 24))
        ttk.Label(r3, text="单独停止", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        self.kcap_stop = tk.Label(r3, text="F10", bg=PANEL2, fg=FG, font=(FONT, 10, "bold"),
                                  padx=16, pady=3)
        self.kcap_stop.pack(side="left")
        RoundButton(r3, "录制", lambda: self._record_hotkey("stop_hotkey"),
                    bg=PANEL, height=28, font_size=8, min_width=52).pack(side="left", padx=(6, 0))
        ttk.Label(r3, text="快捷键在任意窗口都生效", style="CardMuted.TLabel").pack(side="right")

        self._refresh_chips()

    def _build_switches(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(16, 12))
        card.grid(row=4, column=0, sticky="ew", pady=(10, 0))

        self.minimize_var = tk.BooleanVar(value=bool(self.cfg.get("minimize", True)))
        self.beep_var = tk.BooleanVar(value=bool(self.cfg.get("beep", True)))
        self.auto_output_var = tk.BooleanVar(value=bool(self.cfg.get("auto_output", False)))
        self.oj_mode_var = tk.BooleanVar(value=bool(self.cfg.get("oj_mode", True)))
        self.real_keys_var = tk.BooleanVar(value=bool(self.cfg.get("real_keys", False)))
        self.oj_dismiss_var = tk.BooleanVar(value=bool(self.cfg.get("oj_dismiss", True)))
        self.oj_direct_var = tk.BooleanVar(value=bool(self.cfg.get("oj_direct", True)))

        items = [
            ("头歌专用模式", "复制即填入编辑框，自动用判题逐字输出", self.oj_mode_var),
            ("开始后最小化窗口", "腾出时间切到目标窗口", self.minimize_var),
            ("剪贴板变化自动输出", "复制即输入，不用按快捷键", self.auto_output_var),
            ("输出结束提示音", "打完后响一声提醒你", self.beep_var),
        ]
        for i, (title, desc, var) in enumerate(items):
            col = ttk.Frame(card, style="Card.TFrame")
            col.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 26), pady=(0, 8))
            top = ttk.Frame(col, style="Card.TFrame")
            top.pack(anchor="w")
            sw = Switch(top, var, bg=PANEL)
            sw.pack(side="left", padx=(0, 8))
            lbl = ttk.Label(top, text=title, style="Card.TLabel")
            lbl.pack(side="left")
            ttk.Label(col, text=desc, style="CardMuted.TLabel").pack(anchor="w", padx=(54, 0))
            # 点文字也能切换
            lbl.bind("<Button-1>", lambda _e, s=sw: s._toggle())
            lbl.configure(cursor="hand2")

        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)
        self.oj_mode_var.trace_add("write", lambda *_: self._apply_oj_lock())

    def _apply_oj_lock(self):
        """头歌专用模式下禁止「剪贴板直出」：内容统一走编辑框。"""
        on = bool(self.oj_mode_var.get())
        if on:
            self.seg_source.set_disabled([SOURCE_CLIP], fallback=SOURCE_EDIT)
            self.btn_read.configure(state="disabled")
            self.source_hint.configure(text="头歌模式：复制即填入，按快捷键输出")
            if self.auto_output_var.get():
                self.auto_output_var.set(False)
        else:
            self.seg_source.set_disabled([])
            self.btn_read.configure(state="normal")
            self.source_hint.configure(
                text="按快捷键直接输出剪贴板当前内容，无需粘贴"
                if self.source_var.get() == SOURCE_CLIP
                else "输出下方文本框里的内容，可自由修改")

    def _build_actions(self, parent):
        bar = ttk.Frame(parent)
        bar.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        bar.columnconfigure(3, weight=1)

        self.btn_start = RoundButton(bar, "开始输出  F9", self.toggle_typing,
                                     kind="primary", height=40, font_size=10,
                                     min_width=168)
        self.btn_start.grid(row=0, column=0, sticky="w")
        self.btn_pause = RoundButton(bar, "暂停", self.toggle_pause,
                                     height=40, font_size=9, min_width=72)
        self.btn_pause.configure(state="disabled")
        self.btn_pause.grid(row=0, column=1, padx=(8, 0))
        self.btn_stop = RoundButton(bar, "停止", self.stop_typing, kind="danger",
                                    height=40, font_size=9, min_width=72)
        self.btn_stop.configure(state="disabled")
        self.btn_stop.grid(row=0, column=2, padx=(8, 0))

        self.progress = ProgressBar(bar, bg=BG, height=8)
        self.progress.grid(row=0, column=3, sticky="ew", padx=(26, 10))
        self.pct_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.pct_var, style="Muted.TLabel", width=5,
                  anchor="e").grid(row=0, column=5, sticky="e")

        self.status_var = tk.StringVar(value="就绪")
        self.lbl_status = ttk.Label(parent, textvariable=self.status_var, style="Status.TLabel")
        self.lbl_status.grid(row=6, column=0, sticky="w", pady=(8, 0))

    # ---------------- 小工具 ---------------- #
    def _int_var(self, value):
        """整数字符串变量：滑杆/箭头都只显示整数。"""
        var = tk.StringVar(value=str(int(value)))

        def _normalize(*_args):
            raw = var.get()
            try:
                num = float(raw)
            except (TypeError, ValueError):
                return
            fixed = str(int(round(num)))
            if fixed != raw:
                var.set(fixed)

        var.trace_add("write", _normalize)
        return var

    def _refresh_num(self, widget, var):
        try:
            widget.set(str(int(round(float(var.get())))))
        except (TypeError, ValueError, tk.TclError):
            pass

    def _on_scale(self, val):
        try:
            v = int(round(float(val)))
        except (TypeError, ValueError):
            return
        v = max(0, min(2000, v))
        s = str(v)
        try:
            same = self.interval_var.get() == s
        except (ValueError, tk.TclError):
            same = False
        if not same:
            self.interval_var.set(s)
        # ttk.Spinbox 不会因外部改变变量而重绘，必须显式 set 一次
        try:
            if self.spin_interval.get() != s:
                self.spin_interval.set(s)
        except (tk.TclError, AttributeError):
            pass
        self._refresh_chips()

    def _apply_preset(self, value):
        self.interval_var.set(str(value))
        self.scale.set(value)
        self._refresh_chips()
        self.set_status("每字间隔已设为 %d ms" % value)

    def _refresh_chips(self):
        try:
            current = int(round(float(self.interval_var.get())))
        except (TypeError, ValueError, tk.TclError):
            current = -1
        for value, chip in self._chips.items():
            if value == current:
                chip.configure(bg=ACCENT_SOFT, fg="#cfe0ff", font=(FONT, 8, "bold"))
            else:
                chip.configure(bg=PANEL2, fg=MUTED, font=(FONT, 8))

    def _on_text_modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._update_count()

    def _update_count(self):
        text = self.text.get("1.0", "end-1c")
        lines = text.count("\n") + (1 if text else 0)
        self.count_var.set("%d 字符 · %d 行" % (len(text), lines))

    def set_status(self, msg, color=None):
        self.status_var.set(msg)
        self.lbl_status.configure(foreground=color or MUTED)

    def _set_pill(self, text, color):
        self.pill.configure(text="● " + text, fg=color)

    def _update_hotkey_labels(self):
        self.kcap_start.configure(text=pretty_hotkey(self.cfg["hotkey"]))
        self.kcap_stop.configure(text=pretty_hotkey(self.cfg["stop_hotkey"]))
        self._update_buttons()

    # ---------------- 内容来源 ---------------- #
    def _on_source_change(self, value):
        clip_mode = (value == SOURCE_CLIP)
        if clip_mode:
            self.text.configure(state="disabled", bg=PANEL)
            self.btn_read.configure(state="normal")
            self.btn_clear.configure(state="disabled")
            self.source_hint.configure(text="按快捷键直接输出剪贴板当前内容，无需粘贴")
            self._reload_clipboard(silent=True)
        else:
            self.text.configure(state="normal", bg=PANEL)
            self.btn_read.configure(state="normal")
            self.btn_clear.configure(state="normal")
            self.source_hint.configure(text="输出下方文本框里的内容，可自由修改")
            self.set_status("已切换到编辑模式，可修改文本后再输出")

    def _set_preview(self, text):
        state = str(self.text.cget("state"))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_modified(False)
        if state == "disabled":
            self.text.configure(state="disabled")
        self._update_count()

    def _fill_editor(self, text):
        """头歌专用模式：把剪贴板内容自动填入编辑框，并切到「编辑内容」来源。"""
        if self._filling:
            return
        self._filling = True
        try:
            if self.source_var.get() == SOURCE_CLIP:
                self.source_var.set(SOURCE_EDIT)   # 触发 _on_source_change，文本框变为可编辑
                self.root.update_idletasks()
            self._set_preview(text)
            lines = text.count("\n") + 1
            self.set_status(
                "头歌模式：已自动填入编辑框（%d 行 %d 字），按 %s 输出"
                % (lines, len(text), pretty_hotkey(self.cfg["hotkey"])), ACCENT)
            self._set_pill("已填入", ACCENT)
            self.root.after(1500, self._reset_pill_if_idle)
        except tk.TclError:
            pass
        finally:
            self._filling = False

    def _reset_pill_if_idle(self):
        if self.state == "idle" and not getattr(self, "_closing", False):
            try:
                self._set_pill("就绪", MUTED)
            except tk.TclError:
                pass

    def _reload_clipboard(self, silent=False):
        raw = read_clipboard()
        if raw is None:
            if not silent:
                messagebox.showinfo(APP_NAME, "剪贴板中没有可读取的文本。", parent=self.root)
            return False
        text = normalize_text(raw)
        self._last_clip = text
        self._set_preview(text)
        if not silent:
            self.set_status("已读取剪贴板 %d 个字符" % len(text), GREEN)
        return True

    def read_clipboard(self):
        self._reload_clipboard(silent=False)

    def clear_text(self):
        if self.source_var.get() == SOURCE_CLIP:
            return
        self._set_preview("")
        self.set_status("已清空")

    # ---------------- 诊断 ---------------- #
    def _diagnostic_text(self, target):
        self_hwnd = self._self_hwnd()
        fg = get_foreground_window()
        lines = []
        lines.append("【剪贴板】")
        lines.extend("  " + x for x in clipboard_report())
        try:
            text = read_clipboard(retries=3)
            lines.append("  当前读取结果: %s" % ("%d 字符" % len(text) if text else "读不到文本"))
        except Exception as exc:  # noqa: BLE001
            lines.append("  当前读取结果: 读取异常 %s" % exc)

        lines.append("")
        lines.append("【窗口】")
        lines.append("  本工具: %s" % (get_window_title(self_hwnd) or "?"))
        lines.append("  当前前台: %s [%s]" % (get_window_title(fg) or "?", get_window_class(fg)))
        lines.append("  记住的目标: %s" % (
            "%s [%s]" % (get_window_title(target), get_window_class(target))
            if target and is_window(target) else "无"))
        lines.append("  修饰键: %s" % ("有按键被按住" if modifiers_down() else "未按下"))
        lines.append("  输出内容来源: %s" % ("剪贴板直出" if self.source_var.get() == SOURCE_CLIP else "编辑内容"))
        lines.append("  输入方式: %s" % self.mode_var.get())
        lines.append("  在线判题适配: %s" % (
            "已开启（复制即填入 + 强制判题）" if self.oj_mode_var.get()
            else "已识别到目标平台" if self._oj_detected
            else "未识别（可勾选『头歌专用模式』强制启用）"))
        lines.append("  上次输出策略: %s" % getattr(self, "_last_mode", "（尚未输出）"))
        lines.append("  粘贴拦截检测: %s" % ("检测到平台清空剪贴板，已自动改用判题"
                                          if getattr(self, "_saw_paste_block", False)
                                          else "未检测到"))
        lines.append("  真实按键通道: %s" % ("开启（需英文输入法）" if self.real_keys_var.get() else "关闭（Unicode，推荐）"))
        lines.append("  抗补全（Esc）: %s" % ("开启" if self.oj_dismiss_var.get() else "关闭"))
        lines.append("  直投模式: %s" % ("开启" if self.oj_direct_var.get() else "关闭"))
        lines.append("  每字间隔: %s ms / 倒计时 %s s / 开始后最小化: %s" % (
            self.interval_var.get(), self.countdown_var.get(),
            "是" if self.minimize_var.get() else "否"))
        lines.append("")
        lines.append("【程序】")
        lines.append("  版本: v%s" % APP_VERSION)
        lines.append("  配置文件: %s" % CONFIG_PATH)
        lines.append("")
        lines.append("【建议】")
        lines.append("  · 头歌等禁用粘贴的平台：用「判题」或保持「自动」让程序自动识别")
        lines.append("  · 普通网页编辑器：多行内容用「粘贴」可保住缩进和括号")
        lines.append("  · 先点一下目标输入框（让光标真的在里面），再按快捷键")
        lines.append("  · 判题模式会试打一段多行探针，用它确认缩进是否对齐")
        return "\n".join(lines)

    def show_diagnostics(self):
        target = get_foreground_window()
        if target == self._self_hwnd():
            target = self._last_foreign_hwnd

        top = tk.Toplevel(self.root)
        top.title("诊断")
        top.configure(bg=BG)
        top.transient(self.root)
        top.geometry("640x700")

        frame = ttk.Frame(top, padding=(16, 12))
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="当前状态", background=BG, foreground=FG,
                  font=(FONT, 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))

        box = tk.Text(frame, bg=PANEL, fg=FG_DIM, relief="flat", bd=0, padx=12, pady=10,
                      font=(FONT, 9), wrap="word", height=15)
        box.grid(row=1, column=0, sticky="nsew")
        box.insert("1.0", self._diagnostic_text(target))
        box.configure(state="disabled")

        btns = ttk.Frame(frame)
        btns.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        def refresh():
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", self._diagnostic_text(target))
            box.configure(state="disabled")

        def test_type():
            top.destroy()
            self.set_status("2 秒后向目标窗口试打探针…", ACCENT)
            self.root.after(200, lambda: self._test_typing(target))

        def reset_cfg():
            if not messagebox.askyesno(
                    APP_NAME,
                    "把所有设置恢复为默认值？\n\n"
                    "（只影响开关/快捷键/速度等设置，不动剪贴板和文本框内容）",
                    parent=top):
                return
            try:
                self.cfg = dict(DEFAULT_CFG)
                self._save_config()
            except OSError:
                pass
            messagebox.showinfo(APP_NAME, "已恢复默认设置，重新打开程序后完全生效。", parent=top)
            top.destroy()

        def retry_oj():
            top.destroy()
            self.mode_var.set(MODE_OJ)
            self.oj_mode_var.set(True)
            self._last_output = None
            self.set_status("已切换到判题适配，正在重新输出…", ACCENT)
            self.root.after(200, lambda: self.start_typing(target=target))

        ttk.Button(btns, text="刷新", command=refresh).pack(side="left")
        ttk.Button(btns, text="试打", command=test_type).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="判题重试", command=retry_oj).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="恢复默认", command=reset_cfg).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

        adv = ttk.Frame(frame)
        adv.grid(row=3, column=0, sticky="ew", pady=(10, 2))
        tk.Checkbutton(adv, text="真实按键通道（高级）", variable=self.real_keys_var,
                       bg=BG, fg=FG_DIM, selectcolor=PANEL3, activebackground=BG,
                       activeforeground=FG, bd=0, highlightthickness=0,
                       font=(FONT, 9), anchor="w").pack(anchor="w")
        ttk.Label(adv, text="仅当平台拦截模拟输入时开启；使用前必须把输入法切到英文，否则字母会变成拼音",
                  background=BG, foreground=FG_DIM, font=(FONT, 8),
                  wraplength=560, justify="left").pack(anchor="w", padx=(20, 0))
        tk.Checkbutton(adv, text="抗补全：空格/换行/删除前先关掉代码提示（建议保持开启）",
                       variable=self.oj_dismiss_var, bg=BG, fg=FG_DIM, selectcolor=PANEL3,
                       activebackground=BG, activeforeground=FG, bd=0, highlightthickness=0,
                       font=(FONT, 9), anchor="w").pack(anchor="w", pady=(6, 0))
        tk.Checkbutton(adv, text="直投模式：字符直接投递给浏览器，完全不触发代码提示（推荐）",
                       variable=self.oj_direct_var, bg=BG, fg=FG_DIM, selectcolor=PANEL3,
                       activebackground=BG, activeforeground=FG, bd=0, highlightthickness=0,
                       font=(FONT, 9), anchor="w").pack(anchor="w", pady=(6, 0))

    def _test_typing(self, target):
        probe = ("PROBE A\n"
                 "  PROBE B\n"
                 "    PROBE (C)\n"
                 "  PROBE D") if self.mode_var.get() == MODE_OJ else "abc123"
        self.start_typing(text=probe, target=target)

    # ---------------- 快捷键 ---------------- #
    @staticmethod
    def _hotkey_available(combo):
        """试注册一次，判断这个组合键当前是否可用。"""
        try:
            mods, vk = parse_hotkey(combo)
        except ValueError:
            return False
        probe_id = 0x9000
        if user32.RegisterHotKey(None, probe_id, mods, vk):
            user32.UnregisterHotKey(None, probe_id)
            return True
        return False

    def _apply_hotkeys(self):
        # 默认键可能被输入法 / 其它软件占用，被占就自动换一个能用的
        switched = []
        for key, cands in (("hotkey", FALLBACK_TOGGLE), ("stop_hotkey", FALLBACK_STOP)):
            cur = str(self.cfg.get(key, "")).strip()
            if cur and self._hotkey_available(cur):
                continue
            for cand in cands:
                if cand.lower() != cur.lower() and self._hotkey_available(cand):
                    if cur:
                        switched.append("%s → %s" % (pretty_hotkey(cur), pretty_hotkey(cand)))
                    self.cfg[key] = cand
                    break
        if switched:
            self.kcap_start.configure(text=pretty_hotkey(self.cfg["hotkey"]))
            self.kcap_stop.configure(text=pretty_hotkey(self.cfg["stop_hotkey"]))
            self.set_status("快捷键被占用，已自动改用 " + "，".join(switched), AMBER)
            self._save_config()

        bindings = []
        failed = []
        for key, hid in (("hotkey", HK_TOGGLE), ("stop_hotkey", HK_STOP)):
            try:
                mods, vk = parse_hotkey(self.cfg[key])
                bindings.append((hid, mods, vk))
            except ValueError as exc:
                failed.append("%s：%s" % (self.cfg[key], exc))
        if failed:
            self.set_status("快捷键无效 → " + "；".join(failed), RED)
        self.hk.start(bindings)

    def _record_hotkey(self, which):
        if self.state != "idle":
            return
        self.hk.stop()  # 录制期间先停掉全局热键，避免按到自己的组合键

        top = tk.Toplevel(self.root)
        top.title("录制快捷键")
        top.configure(bg=BG)
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()

        frame = ttk.Frame(top, padding=(22, 18))
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="请按下想要的组合键", font=(FONT, 12, "bold"),
                  background=BG, foreground=FG).pack(anchor="w")
        ttk.Label(frame, text="例如 F9、Ctrl+Alt+T（字母/数字需搭配 Ctrl/Alt/Shift）",
                  background=BG, foreground=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(6, 14))
        preview = tk.Label(frame, text="等待按键…", bg=PANEL2, fg=ACCENT,
                           font=(FONT, 13, "bold"), height=2, relief="flat")
        preview.pack(fill="x")
        ttk.Button(frame, text="取消", command=lambda: finish(None)).pack(anchor="e", pady=(14, 0))

        closed = {"v": False}

        def finish(combo):
            if closed["v"]:
                return
            closed["v"] = True
            try:
                top.grab_release()
            except tk.TclError:
                pass
            top.destroy()
            if combo:
                other = "stop_hotkey" if which == "hotkey" else "hotkey"
                if combo.lower() == str(self.cfg[other]).lower():
                    messagebox.showwarning(APP_NAME, "两个快捷键不能相同。", parent=self.root)
                    self._apply_hotkeys()
                    return
                self.cfg[which] = combo
                self._update_hotkey_labels()
                self._save_config()
                self.set_status("快捷键已设为 %s" % pretty_hotkey(combo), GREEN)
            self._apply_hotkeys()

        def on_key(event):
            if event.keysym == "Escape":
                finish(None)
                return
            combo, err = keysym_to_hotkey(event.keysym, event.state)
            if combo is None and err is None:
                preview.configure(text="继续按主键…")
                return
            if err:
                preview.configure(text=err, fg=RED)
                return
            preview.configure(text=pretty_hotkey(combo), fg=GREEN)
            top.after(220, lambda: finish(combo))

        top.bind("<KeyPress>", on_key)
        top.protocol("WM_DELETE_WINDOW", lambda: finish(None))
        top.update_idletasks()
        w, h = 430, 215
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 3
        top.geometry("%dx%d+%d+%d" % (w, h, max(x, 0), max(y, 0)))
        top.focus_force()

    # ---------------- 配置 ---------------- #
    def _save_config(self):
        cfg = dict(self.cfg)
        cfg.update(self._read_widgets())
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _read_widgets(self):
        def num(var, default, lo=0, hi=99999):
            try:
                v = int(round(float(var.get())))
            except (tk.TclError, ValueError):
                return default
            return max(lo, min(hi, v))

        return {
            "source": self.source_var.get(),
            "mode": self.mode_var.get(),
            "interval": num(self.interval_var, 25, 0, 2000),
            "line_delay": num(self.line_delay_var, 120),
            "jitter": num(self.jitter_var, 0),
            "countdown": num(self.countdown_var, 3, 0, 30),
            "repeat": num(self.repeat_var, 1, 1, 999),
            "minimize": bool(self.minimize_var.get()),
            "beep": bool(self.beep_var.get()),
            "auto_output": bool(self.auto_output_var.get()),
            "oj_mode": bool(self.oj_mode_var.get()),
            "real_keys": bool(self.real_keys_var.get()),
            "oj_dismiss": bool(self.oj_dismiss_var.get()),
            "oj_direct": bool(self.oj_direct_var.get()),
        }

    # ---------------- 开始 / 停止 ---------------- #
    def toggle_typing(self):
        if self.state == "typing":
            self.stop_typing()
        elif self.state == "countdown":
            self._cancel_countdown()
        else:
            self.start_typing()

    def start_typing(self, text=None, target=None):
        if self.state != "idle":
            return
        cfg = self._read_widgets()
        self.cfg.update(cfg)
        self._save_config()

        if text is None:
            if cfg["source"] == SOURCE_CLIP:
                raw = read_clipboard()
                if raw is None or not raw.strip():
                    self.set_status("剪贴板里没有可输出的文本（点“诊断”查看原因）", RED)
                    self._set_pill("无内容", RED)
                    self.root.bell()
                    if self.root.state() == "iconic":
                        self.root.deiconify()
                    flash_taskbar(self.root)
                    messagebox.showwarning(
                        APP_NAME,
                        "没有从剪贴板读到文本。\n\n"
                        "可能的原因：\n"
                        "· 复制的内容不是纯文本（例如图片、文件）\n"
                        "· 网页的“复制”按钮没有真正写入系统剪贴板\n"
                        "· 剪贴板正被其它程序占用\n\n"
                        "可以点主界面的“诊断”按钮查看剪贴板里实际有什么。",
                        parent=self.root)
                    return
                text = normalize_text(raw)
                self._last_clip = text
                self._set_preview(text)
            else:
                text = self.text.get("1.0", "end-1c")
                if not text.strip():
                    # 兜底：编辑框空但剪贴板有内容时直接用剪贴板，
                    # 避免“复制了却按 F9 没反应”
                    raw = read_clipboard()
                    if raw and raw.strip():
                        text = normalize_text(raw)
                        self._last_clip = text
                        self._set_preview(text)
                        self.set_status("编辑框为空，已自动读取剪贴板内容", AMBER)
                    else:
                        self.set_status("文本框为空：请先复制要输出的内容，或手动输入", RED)
                        self._set_pill("无内容", RED)
                        self.root.bell()
                        flash_taskbar(self.root)
                        if self.root.state() == "iconic":
                            self.root.deiconify()
                        return

        # 记录“按快捷键那一刻”的前台窗口，作为真正的输出目标
        self_hwnd = self._self_hwnd()
        pinned = target if (target and is_window(target)) else 0
        if pinned:
            self._target_hwnd = pinned
            self._target_title = get_window_title(pinned)
        else:
            fg = get_foreground_window()
            if fg and fg != self_hwnd and is_typing_target(fg):
                pinned = fg
            elif is_window(self._last_foreign_hwnd):
                # 从本工具窗口里点按钮/按快捷键时，回退到刚才用过的那窗口
                pinned = self._last_foreign_hwnd
            else:
                pinned = 0
            self._target_hwnd = pinned
            self._target_title = get_window_title(pinned) if pinned else ""
        target_class = get_window_class(pinned) if pinned else ""
        self._oj_detected = is_oj_window(self._target_title, target_class)
        self._target_is_browser = target_class in BROWSER_CLASSES

        self._last_output = text
        self.state = "countdown"
        self._update_buttons()
        self._set_pill("倒计时", AMBER)
        # 只有本工具在前台时才最小化，避免无谓地搅动窗口焦点
        self._force_target = bool(target and is_window(target))
        if cfg["minimize"] and get_foreground_window() == self_hwnd:
            self._force_target = True
            self.root.iconify()

        remaining = cfg["countdown"]
        if remaining <= 0:
            self._begin_typing(text, cfg)
            return
        token = object()
        self._cd_token = token
        self._countdown_left = remaining
        self._pending_text = text
        self._pending_cfg = cfg
        self._tick_countdown(token)

    def _tick_countdown(self, token):
        if token is not self._cd_token:
            return
        if self._countdown_left <= 0:
            self._cd_token = None
            self._begin_typing(self._pending_text, self._pending_cfg)
            return
        self.set_status("%d 秒后开始输出…（按 %s 取消）"
                        % (self._countdown_left, pretty_hotkey(self.cfg["hotkey"])), ACCENT)
        self._countdown_left -= 1
        self.root.after(1000, lambda: self._tick_countdown(token))

    def _cancel_countdown(self):
        self._cd_token = None
        self.state = "idle"
        self._update_buttons()
        self._set_pill("就绪", MUTED)
        self.set_status("已取消")
        self.root.deiconify()

    def _self_hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)
        except Exception:  # noqa: BLE001
            return 0

    def _ensure_target_ready(self):
        """输出前确认：前台窗口是目标窗口，且没有 Ctrl/Alt 被按住。

        返回 (ok, 提示文字)。
        """
        self_hwnd = self._self_hwnd()
        target = self._target_hwnd
        cur = get_foreground_window()

        if target and is_window(target) and cur != target:
            # 自己刚最小化过、或前台是桌面/任务栏/本工具时，把目标窗口拉回前台；
            # 用户倒计时期间主动切到别的窗口时尊重用户的选择，不硬抢。
            if self._force_target or cur in (0, self_hwnd) or is_shell_window(cur):
                activate_window(target)
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if get_foreground_window() == target:
                        break
                    time.sleep(0.03)
                cur = get_foreground_window()
                if self._force_target and cur != target:
                    return False, "没能把目标窗口切到前台，已取消输出（请手动点一下目标窗口再试）"

        if cur == self_hwnd:
            return False, "焦点还停在本工具窗口，请先点一下目标输入框再按快捷键"

        # 关键：Ctrl / Alt 若仍被按住，字符会被目标程序当成组合键（WPS 里就是工具区乱跳）
        if not wait_modifiers_released(1.2):
            force_release_modifiers()
            time.sleep(0.08)
        else:
            time.sleep(0.10)
        return True, ""

    def _begin_typing(self, text, cfg):
        ok, msg = self._ensure_target_ready()
        if not ok:
            self.state = "idle"
            self._cd_token = None
            self._update_buttons()
            self._set_pill("就绪", MUTED)
            self.set_status(msg, RED)
            if self.root.state() == "iconic":
                self.root.deiconify()
            flash_taskbar(self.root)
            self.root.bell()
            return

        # 固化一份剪贴板快照：平台随时可能清空它，之后就没得读了。
        # 只有“剪贴板直出”才允许用文本兜底（那时文本本来就来自剪贴板），
        # 否则剪贴板原本为空时不该被写入内容。
        snap = read_clipboard(retries=3)
        if not snap and cfg["source"] == SOURCE_CLIP:
            snap = text
        if not snap:
            snap = getattr(self, "_clip_snapshot", None)
        self._clip_snapshot = snap
        cfg["clip_snapshot"] = snap

        mode, why = resolve_mode(text, cfg["mode"], self._oj_detected,
                                bool(cfg.get("oj_mode")), self._target_is_browser)
        if self._oj_detected and mode == MODE_PASTE:
            why = "检测到在线判题平台：这类平台常禁用粘贴，建议用「判题」或「自动」"
        cfg["mode"] = mode
        self._last_mode = mode
        self._current_text = text

        self.state = "typing"
        self._stop_evt.clear()
        self._pause_evt.clear()
        self.progress.set(0)
        self.pct_var.set("0%")
        self._update_buttons()
        self._set_pill("粘贴中" if mode == MODE_PASTE else
                       "判题适配" if mode == MODE_OJ else "输出中", ACCENT)
        title = self._target_title.strip()
        if len(title) > 26:
            title = title[:26] + "…"
        where = "「%s」" % title if title else ""
        strategy = {MODE_PASTE: "粘贴", MODE_OJ: "判题逐字",
                    MODE_TYPE: "逐字"}.get(mode, "")
        stop_txt = "%s 停止" % pretty_hotkey(self.cfg["stop_hotkey"])
        if why:
            self.set_status("正在输出到%s · 策略：%s · %s · %s"
                            % (where, strategy, why, stop_txt), ACCENT)
        else:
            self.set_status("正在输出到%s · %d 字符 · 策略：%s · %s"
                            % (where, len(text), strategy, stop_txt), ACCENT)
        self._typing_thread = threading.Thread(target=self._worker, args=(text, dict(cfg)), daemon=True)
        self._typing_thread.start()

    def stop_typing(self):
        if self.state == "countdown":
            self._cancel_countdown()
            return
        if self.state == "typing":
            self._stop_evt.set()
            self._pause_evt.clear()
            self.set_status("正在停止…")

    def toggle_pause(self):
        if self.state != "typing":
            return
        if self._pause_evt.is_set():
            self._pause_evt.clear()
            self.btn_pause.configure(text="暂停")
            self._set_pill("输出中", ACCENT)
            self.set_status("继续输出…", ACCENT)
        else:
            self._pause_evt.set()
            self.btn_pause.configure(text="继续")
            self._set_pill("已暂停", AMBER)
            self.set_status("已暂停（按停止键结束）", AMBER)

    def _update_buttons(self):
        typing = self.state == "typing"
        idle = self.state == "idle"
        key = pretty_hotkey(self.cfg["hotkey"])
        self.btn_stop.configure(state="disabled" if idle else "normal")
        self.btn_pause.configure(state="normal" if typing else "disabled", text="暂停")
        if typing:
            self.btn_start.configure(text="停止输出  %s" % key)
        elif self.state == "countdown":
            self.btn_start.configure(text="取消倒计时  %s" % key)
        else:
            self.btn_start.configure(text="开始输出  %s" % key)

    # ---------------- 输入线程 ---------------- #
    def _select_indent(self):
        """选中「光标 → 行首」这一段（也就是编辑器回车后自动加的缩进）。

        只选到行首为止，**不碰行内已有内容**——否则在文档中间插入时，
        会把换行后跟着的后半段内容一起删掉。
        """
        _send([
            _key_input(vk=VK_SHIFT),
            _key_input(vk=VK_HOME),
            _key_input(vk=VK_HOME, flags=KEYEVENTF_KEYUP),
            _key_input(vk=VK_SHIFT, flags=KEYEVENTF_KEYUP),
        ])
        time.sleep(0.012)

    def _oj_type_direct(self, text, cfg, report, start_done, render):
        """直投后端：字符用 WM_CHAR 直接投给渲染窗口。

        不产生 keydown，所以编辑器的自动补全、括号补全、输入法组词全都不触发；
        换行仍要发真实 Enter（会触发自动缩进），因此换行后先把该行清空，
        再输出整行原文，保证缩进与原文一致。
        """
        interval = min(max(0, cfg["interval"]) / 1000.0, 0.008)
        line_delay = max(0, cfg["line_delay"]) / 1000.0
        done = start_done
        lines = text.split("\n")

        for i, line in enumerate(lines):
            if self._stop_evt.is_set():
                break
            if i > 0:
                post_enter(render)
                self._sleep_interruptible(0.09 + line_delay)   # 等换行与自动缩进完成
                self._select_indent()
                if not line:
                    # 空行：输入一个空格覆盖选中的缩进，再退格删掉这个空格，
                    # 净效果为零，且不会误删任何原有内容
                    post_char(render, " ")
                    self._sleep_interruptible(0.015)
                    press_vk(VK_BACKSPACE)
                    self._sleep_interruptible(0.02)
            for ch in line:
                if self._stop_evt.is_set():
                    break
                while self._pause_evt.is_set() and not self._stop_evt.is_set():
                    time.sleep(0.05)
                if self._stop_evt.is_set():
                    break
                post_char(render, ch)
                done += 1
                report(done)
                if interval > 0:
                    self._sleep_interruptible(interval)
        return done

    def _oj_type_once(self, text, cfg, report, start_done=0):
        """选后端：能找到 Chromium 渲染窗口就走直投，否则回退到逐字模拟。"""
        if cfg.get("oj_direct", True):
            render = find_render_widget(self._target_hwnd)
            if render:
                return self._oj_type_direct(text, cfg, report, start_done, render)
        return self._oj_type_legacy(text, cfg, report, start_done)

    def _oj_type_legacy(self, text, cfg, report, start_done=0):
        """补偿式逐字输出（在线判题 / CodeMirror 专用）。

        这类编辑器禁用粘贴，且会在换行时自动复制上一行缩进、在输入
        括号引号时自动补配对符。策略：
          * 换行后按“目标缩进 - 上一行缩进”只补差值（多则补字符、少则 Backspace 回退）
          * 输入开括号/引号后删掉编辑器自动补出的那一半
        这样既不依赖粘贴，又能让多行代码与原文本逐字一致。
        """
        interval = max(0, cfg["interval"]) / 1000.0
        line_delay = max(0, cfg["line_delay"]) / 1000.0
        jitter = max(0, cfg["jitter"]) / 1000.0
        dismiss = bool(cfg.get("oj_dismiss", True))
        real_keys = bool(cfg.get("real_keys"))
        done = start_done

        def nap(sec):
            if sec > 0:
                self._sleep_interruptible(sec)

        def emit(ch):
            nonlocal done
            if self._stop_evt.is_set():
                return False
            while self._pause_evt.is_set() and not self._stop_evt.is_set():
                time.sleep(0.05)
            if self._stop_evt.is_set():
                return False
            if dismiss and ch == " ":
                # 空格最容易被候选框当成“接受候选”吃掉，导致变量.属性 后内容错位
                oj_dismiss_hint()
                nap(0.012)
            oj_emit_char(ch, real_keys=real_keys, dismiss=dismiss)
            done += 1
            report(done)
            nap(interval + (random.uniform(0, jitter) if jitter else 0))
            return True

        lines = text.split("\n")
        cur_indent = 0
        for i, line in enumerate(lines):
            if self._stop_evt.is_set():
                break
            target = len(line) - len(line.lstrip(" \t"))
            if i > 0:
                # 候选框打开时 Enter 会被当成“接受候选”，先按 Esc 关掉
                if dismiss:
                    oj_dismiss_hint()
                    nap(0.012)
                press_vk(VK_RETURN)
                done += 1
                report(done)
                nap(0.02 + line_delay)
                if target > cur_indent:
                    for ch in line[cur_indent:target]:
                        if not emit(ch):
                            break
                        nap(0.01)
                elif target < cur_indent:
                    if dismiss:
                        oj_dismiss_hint()
                        nap(0.012)
                    for _ in range(cur_indent - target):
                        if self._stop_evt.is_set():
                            break
                        press_vk(VK_BACKSPACE)
                        nap(0.01)
            for ch in line[target:]:
                if not emit(ch):
                    break
            cur_indent = target
        return done

    def _worker(self, text, cfg):
        try:
            repeat = max(1, int(cfg["repeat"]))
            interval = max(0, cfg["interval"]) / 1000.0
            line_delay = max(0, cfg["line_delay"]) / 1000.0
            jitter = max(0, cfg["jitter"]) / 1000.0
            total = len(text) * repeat
            done = 0
            last_push = 0.0

            if cfg["mode"] == MODE_OJ:
                oj_snapshot = cfg.get("clip_snapshot") or read_clipboard(retries=3)
                # 逐字模式本身不写剪贴板，但平台可能持续/延迟清空剪贴板，
                # 用守护线程在输出期间持续补回
                keeper = None
                if oj_snapshot:
                    keeper = ClipboardKeeper(oj_snapshot)
                    keeper.start()
                try:
                    total = max(1, len(text) * repeat)
                    done_all = 0
                    for r in range(repeat):
                        if self._stop_evt.is_set():
                            break
                        if r > 0:
                            self._sleep_interruptible(0.3)
                        base = done_all
                        done_all = self._oj_type_once(
                            text, cfg,
                            lambda d, b=base: self.q.put(
                                ("progress", min(100.0, d * 100.0 / total))),
                            start_done=base)
                finally:
                    if keeper:
                        keeper.stop()
                        if keeper.restored:
                            self.q.put(("clip_restored", oj_snapshot))
                    elif oj_snapshot and read_clipboard(retries=3) in (None, ""):
                        if write_clipboard(oj_snapshot):
                            self.q.put(("clip_restored", oj_snapshot))
                return

            if cfg["mode"] == MODE_PASTE:
                # 始终备份：有些平台（如头歌）会在 paste 事件里清空剪贴板，
                # 备份晚了就再也拿不回来了
                original = cfg.get("clip_snapshot") or read_clipboard(retries=4)
                same = (read_clipboard(retries=2) == text)
                if not same:
                    if not write_clipboard(text):
                        clipboard_restore(original)
                        self.q.put(("error", "写入剪贴板失败，已恢复原剪贴板"))
                        return
                    self.q.put(("clip_written", text))

                blocked = False
                for _ in range(repeat):
                    if self._stop_evt.is_set():
                        break
                    while self._pause_evt.is_set() and not self._stop_evt.is_set():
                        time.sleep(0.05)
                    if self._stop_evt.is_set():
                        break
                    send_paste()
                    done += len(text)
                    self.q.put(("progress", done * 100.0 / total))
                    self._sleep_interruptible(0.45)
                    # 粘贴后剪贴板变空 = 平台拦截了 paste 并清空了剪贴板
                    if read_clipboard(retries=4) in (None, ""):
                        blocked = True
                        break

                # 无论成功与否，都尽量把用户原来的剪贴板还回去；
                # 平台可能延迟二次清空，所以补回后继续守一段时间
                if original:
                    write_clipboard(original, retries=6)
                    self.q.put(("clip_restored", original))
                    keeper = ClipboardKeeper(original, linger=3.0)
                    keeper.start()
                    keeper.stop()
                if blocked:
                    self.q.put(("paste_blocked", original))
                return

            for r in range(repeat):
                if self._stop_evt.is_set():
                    break
                if r > 0:
                    self._sleep_interruptible(0.3)
                for ch in text:
                    if self._stop_evt.is_set():
                        break
                    while self._pause_evt.is_set() and not self._stop_evt.is_set():
                        time.sleep(0.05)
                    if self._stop_evt.is_set():
                        break
                    type_char(ch)
                    done += 1
                    now = time.monotonic()
                    if now - last_push > 0.08:
                        last_push = now
                        self.q.put(("progress", done * 100.0 / total))
                    delay = interval
                    if ch == "\n":
                        delay += line_delay
                    if jitter:
                        delay += random.uniform(0, jitter)
                    if delay > 0:
                        self._sleep_interruptible(delay)
        except Exception as exc:  # noqa: BLE001
            self.q.put(("error", "输出出错：%s" % exc))
        finally:
            self.q.put(("finished",))

    def _sleep_interruptible(self, seconds):
        end = time.monotonic() + seconds
        while True:
            remain = end - time.monotonic()
            if remain <= 0 or self._stop_evt.is_set():
                return
            time.sleep(min(remain, 0.02))

    # ---------------- 事件循环 ---------------- #
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        if not getattr(self, "_closing", False):
            try:
                self.root.after(40, self._poll)
            except tk.TclError:
                pass

    def _handle(self, msg):
        kind = msg[0]
        if kind == "hotkey":
            if msg[1] == HK_TOGGLE:
                self.toggle_typing()
            elif msg[1] == HK_STOP:
                self.stop_typing()
        elif kind == "hotkey_fail":
            name = self.cfg["hotkey"] if msg[1] == HK_TOGGLE else self.cfg["stop_hotkey"]
            self.set_status("快捷键 %s 注册失败，可能已被其它程序占用"
                            % pretty_hotkey(name), RED)
        elif kind == "clip_written":
            # 粘贴模式会写剪贴板，避免被“自动输出”误触发
            self._last_clip = msg[1]
            self._clip_guard = time.monotonic() + 1.5
        elif kind == "clip_restored":
            # 恢复剪贴板也可能被“自动输出”当成新内容，压一下守卫
            self._last_clip = msg[1] or ""
            self._clip_guard = time.monotonic() + 2.0
        elif kind == "paste_blocked":
            # 平台拦截了粘贴并清空剪贴板：恢复已由 worker 完成，这里切到判题模式
            self._paste_blocked = True
            self._saw_paste_block = True
            self.oj_mode_var.set(True)
            self.mode_var.set(MODE_OJ)
            self.set_status("检测到该平台拦截粘贴（并清空了剪贴板）：剪贴板已还原，"
                            "正自动改用判题逐字重新输出…", AMBER)
        elif kind == "progress":
            self.progress.set(msg[1])
            self.pct_var.set("%d%%" % int(msg[1]))
        elif kind == "error":
            self.set_status(msg[1], RED)
            self._set_pill("出错", RED)
        elif kind == "finished":
            self.state = "idle"
            self._cd_token = None
            self.progress.set(0)
            self.pct_var.set("")
            self._update_buttons()
            self._set_pill("就绪", MUTED)
            if self.root.state() == "iconic":
                flash_taskbar(self.root)
            if self._paste_blocked:
                self._paste_blocked = False
                text = self._current_text
                if text and not self._stop_evt.is_set():
                    self._set_pill("判题适配", ACCENT)
                    self.root.after(600, lambda t=text: self.start_typing(text=t))
                return
            if self.status_var.get().startswith("正在停止"):
                self.set_status("已停止")
            elif not self.status_var.get().startswith(("输出出错", "写入剪贴板失败")):
                mode = getattr(self, "_last_mode", "")
                if mode == MODE_PASTE:
                    self.set_status(
                        "输出完成 · 策略：粘贴。若没出现内容，说明页面禁用了粘贴 →"
                        " 点「诊断」→「判题重试」", AMBER)
                elif mode == MODE_OJ:
                    self.set_status("输出完成 · 策略：判题逐字。格式不对可点「诊断」试打", GREEN)
                else:
                    self.set_status("输出完成 · 策略：逐字", GREEN)
                if self.beep_var.get() and winsound is not None:
                    try:
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except RuntimeError:
                        pass

    # ---------------- 剪贴板监听 ---------------- #
    def _watch_clipboard(self):
        try:
            fg = get_foreground_window()
            self_hwnd = self._self_hwnd()
            if fg and fg != self_hwnd and is_typing_target(fg):
                self._last_foreign_hwnd = fg
            if self.state == "idle":
                oj_mode = bool(self.oj_mode_var.get())
                need_preview = self.source_var.get() == SOURCE_CLIP
                need_auto = bool(self.auto_output_var.get())
                if oj_mode or need_preview or need_auto:
                    raw = read_clipboard(retries=1)
                    if raw is not None:
                        text = normalize_text(raw)
                        if text != self._last_clip:
                            self._last_clip = text
                            if oj_mode:
                                # 头歌专用模式：复制即填入编辑框，之后输出只读编辑框，
                                # 平台再清空剪贴板也不影响
                                if text.strip() and time.monotonic() > self._clip_guard:
                                    self._fill_editor(text)
                                return
                            if need_preview:
                                self._set_preview(text)
                            if (need_auto and text.strip()
                                    and text != self._last_output
                                    and time.monotonic() > self._clip_guard
                                    and self.state == "idle"):
                                self.set_status("检测到新的剪贴板内容，即将自动输出…", ACCENT)
                                self.start_typing(text=text)
        finally:
            if not getattr(self, "_closing", False):
                try:
                    self.root.after(500, self._watch_clipboard)
                except tk.TclError:
                    pass

    # ---------------- 窗口位置 ---------------- #
    def _place_window(self):
        """按屏幕工作区（扣除任务栏）居中，小屏幕自动缩小。"""
        left, top, right, bottom = 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        try:
            rect = RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except Exception:  # noqa: BLE001
            pass
        avail_w, avail_h = right - left, bottom - top
        w = max(820, min(1000, avail_w - 60))
        h = max(640, min(790, avail_h - 40))
        x = left + max(0, (avail_w - w) // 2)
        y = top + max(0, (avail_h - h) // 2)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ---------------- 关闭 ---------------- #
    def _on_close(self):
        try:
            self.cfg.update(self._read_widgets())
            self._save_config()
        except Exception:  # noqa: BLE001
            pass
        self._closing = True
        self._stop_evt.set()
        self.hk.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    if winmm is not None:
        try:
            winmm.timeBeginPeriod(1)  # 提升 sleep 精度，速度控制更准
        except OSError:
            pass
    root = tk.Tk()
    app = App(root)
    try:
        root.mainloop()
    finally:
        if winmm is not None:
            try:
                winmm.timeEndPeriod(1)
            except OSError:
                pass


if __name__ == "__main__":
    main()
