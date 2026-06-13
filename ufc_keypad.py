# -*- coding: utf-8 -*-
"""
UFC 风格键盘面板 - PyQt6 v4.0
分辨率: 1024x600 (固定)
字体: FA-18C Hornet UFC
每个方块可点击，输出对应键盘信号
主窗口 = 设置窗口 (始终在主屏) + UFC面板窗口 (可选显示器全屏)

=== v4 新增：原生触控隔离（方案四保底手段）===
=== v3 新增：按键映射自动保存 ===
=== DCS-BIOS 数据接收 + 亮度同步 ===
"""
import sys
import os
import json
import traceback
import ctypes
import ctypes.wintypes
import socket
import struct
import threading
import time
from collections import defaultdict
from PyQt6 import QtCore, QtGui
from PyQt6.QtWidgets import (QApplication, QWidget, QHBoxLayout, QVBoxLayout,
                             QLabel, QPushButton, QSizePolicy, QComboBox,
                             QLineEdit, QTextEdit,
                             QGroupBox, QScrollArea, QGridLayout,
                             QCheckBox, QMessageBox, QFrame)
from PyQt6.QtCore import Qt, QRect, pyqtSignal, QTimer

# ============ 按键映射自动保存路径 ============
_CONF_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, 'frozen', False):
    _CONF_DIR = os.path.dirname(sys.executable)
CONFIG_FILE = os.path.join(_CONF_DIR, "ufc_config.json")
CRASH_LOG_FILE = os.path.join(_CONF_DIR, "ufc_crash.log")

# ============ 崩溃日志系统 ============
def _crash_log(msg):
    """写入崩溃日志（带时间戳）"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with open(CRASH_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass  # 写日志本身不能再崩溃

def _setup_excepthook():
    """替换全局异常钩子，将所有未捕获异常写入崩溃日志"""
    _original_excepthook = sys.excepthook

    def _global_excepthook(exc_type, exc_value, exc_tb):
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        _crash_log("=" * 60)
        _crash_log(f"UNHANDLED EXCEPTION: {exc_type.__name__}: {exc_value}")
        _crash_log("Traceback:")
        for line in tb_lines:
            _crash_log(line.rstrip())
        _crash_log("=" * 60)
        # 仍然调用原始 excepthook（打印到 stderr）
        _original_excepthook(exc_type, exc_value, exc_tb)

    # 线程内的未捕获异常
    _original_thread_excepthook = threading.excepthook

    def _thread_excepthook(args):
        tb_lines = traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        _crash_log("=" * 60)
        _crash_log(f"THREAD EXCEPTION in '{args.thread.name}': {args.exc_type.__name__}: {args.exc_value}")
        _crash_log("Traceback:")
        for line in tb_lines:
            _crash_log(line.rstrip())
        _crash_log("=" * 60)
        if _original_thread_excepthook is not None:
            _original_thread_excepthook(args)

    sys.excepthook = _global_excepthook
    threading.excepthook = _thread_excepthook

def setup_crash_log():
    """初始化崩溃日志系统"""
    # 写入启动标记
    _crash_log("=" * 40)
    _crash_log("UFC Keypad STARTED")
    _crash_log(f"Python: {sys.version}")
    _setup_excepthook()

# ============ Windows 原生触控 + WH_MOUSE_LL 光标锁定 ============
# RegisterTouchWindow: 阻止 Windows 将触摸合成为鼠标点击
# WH_MOUSE_LL: 在 OS 层面拦截触摸驱动注入的鼠标移动事件
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
TWF_FINETOUCH    = 0x00000001
WM_TOUCH         = 0x0240

# --- 结构体 ---
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt",          _POINT),
        ("mouseData",   ctypes.wintypes.DWORD),
        ("flags",       ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _RECT(ctypes.Structure):
    _fields_ = [
        ("left",   ctypes.wintypes.LONG),
        ("top",    ctypes.wintypes.LONG),
        ("right",  ctypes.wintypes.LONG),
        ("bottom", ctypes.wintypes.LONG),
    ]

class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    ctypes.wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork",    _RECT),
        ("dwFlags",   ctypes.wintypes.DWORD),
    ]

# --- WH_MOUSE_LL 常量 ---
WH_MOUSE_LL      = 14
LLMHF_INJECTED   = 0x00000001    # 事件由触摸/笔驱动注入（非真实鼠标）
WM_MOUSEMOVE     = 0x0200
WM_QUIT_HOOK     = 0x0012        # WM_QUIT
WM_MOUSEACTIVATE = 0x0021        # 鼠标点击激活窗口前发送
MA_NOACTIVATE    = 3              # 不激活、不设置鼠标在窗口内
WM_ACTIVATE      = 0x0006         # 窗口激活/失活通知
GWL_EXSTYLE      = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW  = 0x00000080
SWP_NOMOVE       = 0x0002
SWP_NOSIZE       = 0x0001
SWP_NOZORDER     = 0x0004
SWP_FRAMECHANGED  = 0x0020
MONITOR_DEFAULTTONEAREST = 2

# --- 钩子状态 ---
_hook_handle       = None
_hook_thread       = None
_hook_running      = False
_hook_thread_id    = 0
_dcs_monitor_rect  = None         # (left, top, right, bottom) DCS 所在显示器的屏幕坐标

# --- Win32 API 声明 ---
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong,
                               ctypes.c_int,
                               ctypes.wintypes.WPARAM,
                               ctypes.wintypes.LPARAM)

_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                       ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD]
_user32.SetWindowsHookExW.restype  = ctypes.wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [ctypes.wintypes.HHOOK]
_user32.UnhookWindowsHookEx.restype  = ctypes.wintypes.BOOL
_user32.CallNextHookEx.argtypes = [ctypes.wintypes.HHOOK, ctypes.c_int,
                                    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.CallNextHookEx.restype  = ctypes.c_longlong
_user32.GetMessageW.argtypes = [ctypes.wintypes.LPMSG, ctypes.wintypes.HWND,
                                 ctypes.wintypes.UINT, ctypes.wintypes.UINT]
_user32.GetMessageW.restype  = ctypes.wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.UINT,
                                        ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostThreadMessageW.restype  = ctypes.wintypes.BOOL
_user32.GetWindowRect.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowRect.restype  = ctypes.wintypes.BOOL
_user32.MonitorFromPoint.argtypes = [_POINT, ctypes.wintypes.DWORD]
_user32.MonitorFromPoint.restype  = ctypes.wintypes.HMONITOR
_user32.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
_user32.GetMonitorInfoW.restype  = ctypes.wintypes.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype  = ctypes.wintypes.DWORD
_user32.GetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype  = ctypes.c_longlong
_user32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
_user32.SetWindowLongPtrW.restype  = ctypes.c_longlong
_user32.SetLayeredWindowAttributes.argtypes = [ctypes.wintypes.HWND,
                                             ctypes.wintypes.COLORREF,
                                             ctypes.wintypes.BYTE,
                                             ctypes.wintypes.DWORD]
_user32.SetLayeredWindowAttributes.restype  = ctypes.wintypes.BOOL
_user32.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.wintypes.UINT]
_user32.SetWindowPos.restype  = ctypes.wintypes.BOOL


# --- WH_MOUSE_LL 钩子回调（模块级函数，由 Windows 在独立线程中调用） ---
def _point_in_rect(pt, rect):
    """判断 POINT 是否在矩形 (left, top, right, bottom) 内"""
    return rect[0] <= pt.x <= rect[2] and rect[1] <= pt.y <= rect[3]


@HOOKPROC
def _mouse_hook_proc(nCode, wParam, lParam):
    """每一条鼠标消息到达窗口前都会经过此回调。
    拦截策略：如果移动事件来自触摸驱动(LLMHF_INJECTED) 且目标不在 DCS 显示器 → 返回 1 吞掉"""
    if nCode >= 0 and wParam == WM_MOUSEMOVE:
        ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if ms.flags & LLMHF_INJECTED:
            safe = _dcs_monitor_rect
            if safe and not _point_in_rect(ms.pt, safe):
                return 1   # 拦截：光标要逃离 DCS 显示器
    return _user32.CallNextHookEx(_hook_handle, nCode, wParam, lParam)


# --- 查找 DCS 窗口所在显示器 ---
def _find_dcs_monitor():
    """更新 _dcs_monitor_rect 为 DCS 窗口所在的显示器屏幕坐标。
    DCS 未运行时不做任何事，钩子此时为完全透明（所有事件放行）。"""
    global _dcs_monitor_rect
    hwnd = _find_dcs_window()
    if not hwnd:
        return False
    wr = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        return False
    # 用窗口中心找显示器
    cx, cy = (wr.left + wr.right) // 2, (wr.top + wr.bottom) // 2
    hmon = _user32.MonitorFromPoint(_POINT(cx, cy), MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return False
    _dcs_monitor_rect = (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom)
    return True


# --- 钩子线程：安装钩子 + Windows 消息泵 ---
def _hook_thread_proc():
    """WH_MOUSE_LL 要求所在线程运行消息泵。此函数在 daemon 线程中运行。"""
    global _hook_handle, _hook_thread_id
    _hook_thread_id = _kernel32.GetCurrentThreadId()
    _hook_handle = _user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_hook_proc, None, 0)
    if not _hook_handle:
        return
    msg = ctypes.wintypes.MSG()
    while _hook_running:
        ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret in (0, -1):
            break
    # 退出：卸载钩子
    if _hook_handle:
        _user32.UnhookWindowsHookEx(_hook_handle)
        _hook_handle = None


def _start_mouse_hook():
    """启动 WH_MOUSE_LL 钩子线程"""
    global _hook_thread, _hook_running
    if _hook_running:
        return
    _find_dcs_monitor()
    _hook_running = True
    _hook_thread = threading.Thread(target=_hook_thread_proc, daemon=True, name="MouseHook")
    _hook_thread.start()


def _stop_mouse_hook():
    """停止 WH_MOUSE_LL 钩子"""
    global _hook_running
    _hook_running = False
    if _hook_thread_id:
        _user32.PostThreadMessageW(_hook_thread_id, WM_QUIT_HOOK, 0, 0)


# --- RegisterTouchWindow：阻止触摸→鼠标点击合成 ---

def _register_native_touch(hwnd_int):
    """注册原生触控：Windows 不再合成鼠标点击/滚轮事件"""
    return bool(_user32.RegisterTouchWindow(ctypes.wintypes.HWND(hwnd_int), TWF_FINETOUCH))

def _unregister_native_touch(hwnd_int):
    """注销原生触控"""
    return bool(_user32.UnregisterTouchWindow(ctypes.wintypes.HWND(hwnd_int)))

# ============ Windows SendInput API — 系统级按键注入 ============
# 用 SendInput 替代 QApplication.sendEvent，按键直达 Windows 输入队列，
# 由系统投递给前台窗口（配合 MonMouse 确保 DCS 保持前台）
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP   = 0x0002

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.wintypes.DWORD),
        ("ki",   _KEYBDINPUT),
    ]

def _send_input_key(vk_code, key_up=False):
    """通过 SendInput 发送单个按键事件（按下或释放），使用虚拟键码"""
    flags = KEYEVENTF_KEYUP if key_up else 0
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        ki=_KEYBDINPUT(
            wVk=vk_code,
            wScan=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=None,
        ),
    )
    _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

# 按键名 → (VK码, 扫描码) 映射
# 字符串键 → Windows VK
_VK_MAP = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "A": 0x41, "B": 0x42, "C": 0x43, "D": 0x44, "E": 0x45,
    "F": 0x46, "G": 0x47, "H": 0x48, "I": 0x49, "J": 0x4A,
    "K": 0x4B, "L": 0x4C, "M": 0x4D, "N": 0x4E, "O": 0x4F,
    "P": 0x50, "Q": 0x51, "R": 0x52, "S": 0x53, "T": 0x54,
    "U": 0x55, "V": 0x56, "W": 0x57, "X": 0x58, "Y": 0x59, "Z": 0x5A,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "Left": 0x25, "Right": 0x27, "Up": 0x26, "Down": 0x28,
    "Home": 0x24, "End": 0x23, "Insert": 0x2D, "Delete": 0x2E,
    "PageUp": 0x21, "PageDown": 0x22,
    "Return": 0x0D, "Enter": 0x0D, "Backspace": 0x08,
    "Tab": 0x09, "Escape": 0x1B, "Esc": 0x1B, "Space": 0x20,
    "minus": 0xBD, "Minus": 0xBD,
    "plus": 0xBB, "Plus": 0xBB, "equal": 0xBB, "Equal": 0xBB,
    "period": 0xBE, "Period": 0xBE, "comma": 0xBC, "Comma": 0xBC,
    "slash": 0xBF, "Slash": 0xBF, "backslash": 0xDC, "Backslash": 0xDC,
    "semicolon": 0xBA, "Semicolon": 0xBA,
    "apostrophe": 0xDE, "Apostrophe": 0xDE,
    "bracketLeft": 0xDB, "bracketRight": 0xDD,
    "grave": 0xC0, "Grave": 0xC0,
    "CapsLock": 0x14, "NumLock": 0x90, "ScrollLock": 0x91,
    "Print": 0x2C, "Pause": 0x13,
    "Ctrl": 0x11, "Control": 0x11, "LCtrl": 0xA2, "RCtrl": 0xA3,
    "Shift": 0x10, "LShift": 0xA0, "RShift": 0xA1,
    "Alt": 0x12, "LAlt": 0xA4, "RAlt": 0xA5,
    "Meta": 0x5B, "Win": 0x5B, "LWin": 0x5B, "RWin": 0x5C,
    # DCS 特殊
    "JOY_BTN1": 0x01, "JOY_BTN2": 0x02, "JOY_BTN3": 0x03,
}

def inject_key_combo(key_str: str):
    """
    发送组合键到 DCS。
    
    优先方案: SendInput 系统注入 —— 驱动层按键，DCS 的 DirectInput
    /Raw Input 能正确捕获。需要 DCS 在前台（鼠标钩子+防激活保
    证了这一点）。

    回退方案: PostMessage 直投 DCS 窗口 —— 无需焦点，但 DCS 用
    DirectInput 读取键盘时会忽略 WM_KEYDOWN/WM_KEYUP 消息。
    """
    if not key_str:
        return

    parts = [p.strip() for p in key_str.split("+")]
    if not parts:
        return

    # 分类：修饰键 vs 主键
    modifier_names = {"Ctrl", "Control", "LCtrl", "RCtrl",
                      "Shift", "LShift", "RShift",
                      "Alt", "LAlt", "RAlt",
                      "Meta", "Win", "LWin", "RWin"}
    mod_vks = []
    main_vk = None
    main_name = None

    for p in parts:
        vk = _VK_MAP.get(p)
        if vk is None:
            continue
        if p in modifier_names:
            mod_vks.append(vk)
        else:
            main_vk = vk
            main_name = p

    if main_vk is None:
        return  # 全是修饰键，无主键

    # 优先 SendInput（驱动层注入，DCS DirectInput 能捕获）
    # PostMessage 投递 WM_KEYDOWN 虽然"成功"，但 DCS 用 DirectInput/Raw Input
    # 不读消息队列，所以消息被忽略。SendInput 走驱动层，DirectInput 能抓到。
    # 当 DCS 不在前台或 SendInput 失败时，回退到 PostMessage。
    _inject_via_sendinput(mod_vks, main_vk) or _inject_via_postmessage(mod_vks, main_vk)


# ========== 方案A: PostMessage 直投 DCS 窗口（无需焦点）==========
WM_KEYDOWN = 0x0100
WM_KEYUP   = 0x0101

# 已缓存 DCS 窗口句柄 (首次查找后缓存，效率高)
_dcs_hwnd_cache = None
_dcs_hwnd_cache_time = 0
_DCS_CACHE_TTL = 3.0  # 缓存 3 秒后重新查找

# 声明 Win32 API
_user32.FindWindowW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPCWSTR]
_user32.FindWindowW.restype  = ctypes.wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype  = ctypes.wintypes.DWORD
_user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
_user32.IsWindowVisible.restype  = ctypes.wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = [ctypes.wintypes.HWND]
_user32.GetWindowTextLengthW.restype  = ctypes.c_int
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype  = ctypes.c_int
_user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                                  ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
_user32.PostMessageW.restype  = ctypes.wintypes.BOOL
_user32.MapVirtualKeyW.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.UINT]
_user32.MapVirtualKeyW.restype  = ctypes.wintypes.UINT

_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, ctypes.wintypes.LPARAM]
_user32.EnumWindows.restype  = ctypes.wintypes.BOOL


def _vk_to_scan(vk):
    """VK 码 → 扫描码 (MAPVK_VK_TO_VSC = 0)"""
    return _user32.MapVirtualKeyW(vk, 0)


def _find_dcs_window():
    """查找 DCS World 主窗口 HWND。支持多种匹配策略，结果缓存 3 秒。"""
    global _dcs_hwnd_cache, _dcs_hwnd_cache_time
    now = time.time()
    if _dcs_hwnd_cache and (now - _dcs_hwnd_cache_time) < _DCS_CACHE_TTL:
        # 验证缓存的窗口是否仍然有效
        if _user32.IsWindowVisible(_dcs_hwnd_cache):
            return _dcs_hwnd_cache
        _dcs_hwnd_cache = None

    found = []

    # 策略1: 精确匹配 "Digital Combat Simulator"
    def enum_callback(hwnd, lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if 'Digital Combat Simulator' in buf.value:
            found.append(hwnd)
            return False  # 找到，停止枚举
        return True

    _user32.EnumWindows(_WNDENUMPROC(enum_callback), 0)

    if found:
        _dcs_hwnd_cache = found[0]
        _dcs_hwnd_cache_time = now
        return found[0]

    # 策略2: 宽泛匹配 "DCS"
    found2 = []

    def enum_callback2(hwnd, lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        if 'DCS' in buf.value:
            found2.append(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(enum_callback2), 0)
    if found2:
        _dcs_hwnd_cache = found2[0]
        _dcs_hwnd_cache_time = now
        return found2[0]

    return None


def _post_key(vk_code, key_up=False):
    """通过 PostMessage 发送单个按键到 DCS 窗口。返回 True 表示投递成功。"""
    hwnd = _find_dcs_window()
    if not hwnd:
        return False

    scan = _vk_to_scan(vk_code)
    msg = WM_KEYUP if key_up else WM_KEYDOWN

    # lParam 编码 (标准 Windows 键盘消息格式):
    # bits 0-15:  repeat count (1)
    # bits 16-23: scan code
    # bit 24:     extended key (0 for most keys)
    # bit 29:     context code (0 = key was pressed, not Alt+SysRq)
    # bit 30:     previous key state (0 for press, 1 for release)
    # bit 31:     transition (0 = press, 1 = release)
    if key_up:
        lparam = (scan << 16) | 0xC0000001
    else:
        lparam = (scan << 16) | 0x00000001

    return bool(_user32.PostMessageW(hwnd, msg, vk_code, lparam))


def _inject_via_postmessage(mod_vks, main_vk):
    """PostMessage 直投方案。成功返回 True，失败返回 False。"""
    hwnd = _find_dcs_window()
    if not hwnd:
        return False

    # 1) 按下所有修饰键
    for vk in mod_vks:
        _post_key(vk, key_up=False)

    # 2) 按下 + 释放主键
    _post_key(main_vk, key_up=False)
    _post_key(main_vk, key_up=True)

    # 3) 释放修饰键（逆序）
    for vk in reversed(mod_vks):
        _post_key(vk, key_up=True)

    return True


# ========== 方案B: SendInput 系统注入 ==========
def _inject_via_sendinput(mod_vks, main_vk):
    """SendInput 系统级按键注入。返回 True 表示已发送（不保证 DCS 收到）。"""
    for vk in mod_vks:
        _send_input_key(vk, key_up=False)
    _send_input_key(main_vk, key_up=False)
    _send_input_key(main_vk, key_up=True)
    for vk in reversed(mod_vks):
        _send_input_key(vk, key_up=True)
    return True  # SendInput 已调用，但不保证 DCS 收到（DCS 用 Raw Input 会忽略合成事件）


# ========== DCS-BIOS 控制命令发送（正解！）==========
# DCS-BIOS 控制命令端口（客户端 → DCS）
_DCS_BIOS_CMD_ADDR = ("127.0.0.1", 7778)
_dcs_bios_cmd_sock = None

def _get_bios_cmd_sock():
    """获取 DCS-BIOS 命令发送 socket（单例）"""
    global _dcs_bios_cmd_sock
    if _dcs_bios_cmd_sock is None:
        _dcs_bios_cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return _dcs_bios_cmd_sock

def send_dcs_bios(identifier: str, value):
    """发送 DCS-BIOS 控制命令。
    value: int (1/0 模拟按钮按下/释放) 或 str (INC/DEC 步进, TOGGLE 切换)"""
    try:
        sock = _get_bios_cmd_sock()
        msg = f"{identifier} {value}\n".encode("utf-8")
        sock.sendto(msg, _DCS_BIOS_CMD_ADDR)
        return True
    except Exception as e:
        print(f"[DCS-BIOS] 发送失败: {e}")
        return False

def dcs_bios_click(identifier: str):
    """模拟一次完整点击（按下+释放）。DCS-BIOS 处理时序，无需 delay。"""
    send_dcs_bios(identifier, 1)
    send_dcs_bios(identifier, 0)


def _send_release(identifier: str):
    """发送 release(0) 命令（由 QTimer.singleShot 延迟调用）"""
    send_dcs_bios(identifier, 0)


# 最小按压时间（毫秒），确保 press/release 两个 UDP 包落在不同 DCS 帧
_MIN_PRESS_MS = 50


# UFC 格子位置 → DCS-BIOS 按钮标识符映射
# 值类型:
#   str           → PushButton: press 发 "1", release 发 "0"（UFCCell 内部处理）
#   (str, str)    → 单次命令: 如 ("UFC_COMM1_PULL", "TOGGLE") 或 ("...", "INC")
UFC_BIOS_MAP = {
    # 数字键（PushButton → press/release 1/0，UFCCell 内部处理）
    (1, 1): "UFC_1",
    (1, 2): "UFC_2",
    (1, 3): "UFC_3",
    (2, 1): "UFC_4",
    (2, 2): "UFC_5",
    (2, 3): "UFC_6",
    (3, 1): "UFC_7",
    (3, 2): "UFC_8",
    (3, 3): "UFC_9",
    (4, 2): "UFC_0",
    # 功能键（PushButton → press/release）
    (4, 1): "UFC_CLR",
    (4, 3): "UFC_ENT",
    # 顶部功能键（PushButton → press/release）
    (0, 0): "UFC_IP",
    # 底部功能键（PushButton → press/release）
    (5, 2): "UFC_AP",
    (5, 3): "UFC_IFF",
    (5, 4): "UFC_TCN",
    (5, 5): "UFC_ILS",
    (5, 6): "UFC_DL",
    (5, 7): "UFC_BCN",
    (5, 8): "UFC_ONOFF",
    # 选项选择按钮（OSB 1-5，PushButton → press/release）
    (0, 4): "UFC_OS1",
    (1, 4): "UFC_OS2",
    (2, 4): "UFC_OS3",
    (3, 4): "UFC_OS4",
    (4, 4): "UFC_OS5",
    # EM CON（无线电静默，PushButton → press/release）
    (1, 5): "UFC_EMCON",
    # COMM 频道旋钮（左右转，FixedStepInput → INC/DEC）
    (5, 0): ("UFC_COMM1_CHANNEL_SELECT", "DEC"),
    (5, 1): ("UFC_COMM1_CHANNEL_SELECT", "INC"),
    (5, 9): ("UFC_COMM2_CHANNEL_SELECT", "DEC"),
    (5, 10): ("UFC_COMM2_CHANNEL_SELECT", "INC"),
    # COMM Pull（拉起/按下 → press/release：按住拉出，松开放下）
    (3, 0): "UFC_COMM1_PULL",
    (3, 5): "UFC_COMM2_PULL",
}

def save_config(config_dict):
    """保存通用配置到 ufc_config.json"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config_dict, f, ensure_ascii=False, indent=2)
        print(f"[配置] 已保存: {CONFIG_FILE}")
    except Exception as e:
        print(f"[配置] 保存失败: {e}")

def load_config():
    """加载通用配置"""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[配置] 加载失败: {e}")
        return {}
from PyQt6.QtGui import QFont, QFontDatabase, QKeyEvent, QMouseEvent,QCursor

# ============ Hornet UFC 字体路径 ============
# 优先查找脚本同目录下的字体文件（便于移植）
# PyInstaller 打包后 sys._MEIPASS 中查找
if getattr(sys, 'frozen', False):
    _SCRIPT_DIR = sys._MEIPASS
else:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_HORNET_FONT_FILENAME = "FA-18C_Hornet_Up_Front_Controller.ttf"
HORNET_UFC_FONT_PATH = os.path.join(_SCRIPT_DIR, _HORNET_FONT_FILENAME)
_HORNET_UFC_FALLBACK = r"D:\Helios-DCS-Fonts-master\Helios-DCS-Fonts-master\Hornet Harrier Hawg Fonts\Output\FA-18C_Hornet_Up_Front_Controller.ttf"
_hornet_font_loaded = False
HORNET_UFC_FAMILY = None

def _load_hornet_font():
    """加载 Hornet UFC 字体，返回字体族名称
    查找顺序：脚本目录 → 原 D 盘路径 → B612 回退"""
    global _hornet_font_loaded, HORNET_UFC_FAMILY
    if _hornet_font_loaded:
        return HORNET_UFC_FAMILY
    _hornet_font_loaded = True
    
    # 1) 脚本同目录（便携）
    if os.path.exists(HORNET_UFC_FONT_PATH):
        font_id = QFontDatabase.addApplicationFont(HORNET_UFC_FONT_PATH)
        if font_id != -1:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                HORNET_UFC_FAMILY = families[0]
                print(f"[字体] 已加载 (脚本目录): {HORNET_UFC_FAMILY}")
                return HORNET_UFC_FAMILY
    
    # 2) 原始安装路径
    if os.path.exists(_HORNET_UFC_FALLBACK):
        font_id = QFontDatabase.addApplicationFont(_HORNET_UFC_FALLBACK)
        if font_id != -1:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                HORNET_UFC_FAMILY = families[0]
                print(f"[字体] 已加载 (备用路径): {HORNET_UFC_FAMILY}")
                return HORNET_UFC_FAMILY
    
    # 3) 全部失败，回退 B612
    print("[字体] ⚠️ Hornet UFC 字体未找到，回退到 B612")
    HORNET_UFC_FAMILY = None
    return None

def get_hornet_font(font_size):
    """获取 Hornet UFC 字体对象"""
    family = _load_hornet_font()
    if family:
        return QFont(family, font_size)
    return QFont("B612", font_size)

# ============ 分辨率 ============
WIN_W = 1024
WIN_H = 600

# ============ 按钮文本配置 ============
BUTTON_TEXTS = {
    (0, 0): "I/P",
    (0, 4): "可变显示\n(原RTTH)",
    (0, 5): "SETTINGS",
    (1, 0): "SYSTEMS",
    (1, 1): "1",
    (1, 2): "N\n2",
    (1, 3): "3",
    (1, 4): "可变显示\n(原HSEL)",
    (1, 5): "EM\nCON",
    (2, 1): "W\n4",
    (2, 2): "5",
    (2, 3): "E\n6",
    (2, 4): "可变显示\n(原BALT)",
    (3, 0): "COMM 1",
    (3, 1): "7",
    (3, 2): "S\n8",
    (3, 3): "9",
    (3, 4): "可变显示\n(原RALT)",
    (3, 5): "COMM 2",
    (4, 0): "可变显示\n(原1)",
    (4, 1): "CLR",
    (4, 2): "0",
    (4, 3): "ENT",
    (4, 4): "可变显示\n(原空白)",
    (4, 5): "可变显示\n(原2)",
    (5, 0): "<",
    (5, 1): ">",
    (5, 2): "A/P",
    (5, 3): "IFF",
    (5, 4): "TCN",
    (5, 5): "ILS",
    (5, 6): "D/L",
    (5, 7): "BCN",
    (5, 8): "ON\nOFF",
    (5, 9): "<",
    (5, 10): ">",
}

# ============ 样式配置（亮度动态版） ============
BG_COLOR = "#000000"  # 背景始终纯黑
_CURRENT_BRIGHTNESS = 0.0  # 全局亮度（DCS 未连接时最低，连上后由 UFC_BRT 更新）

def _dim(brightness):
    """亮度 → 强度因子：DCS断连~0.05（微弱可见），正常运行 25%~100%"""
    if brightness <= 0:
        return 0.05  # DCS 离线：微弱可见
    return max(0.25, 0.2 + brightness * 0.8)

def text_color_br(br=None):
    """文字颜色：绿色，亮度跟随"""
    b = br if br is not None else _CURRENT_BRIGHTNESS
    f = _dim(b)
    return f"rgb({int(0*f)}, {int(255*f)}, {int(0*f)})"

def border_color_br(br=None):
    """边框颜色"""
    b = br if br is not None else _CURRENT_BRIGHTNESS
    f = _dim(b)
    return f"rgb({int(0*f)}, {int(255*f)}, {int(0*f)})"

def hover_bg_br(br=None):
    """hover 背景色"""
    b = br if br is not None else _CURRENT_BRIGHTNESS
    f = _dim(b)
    return f"rgb({int(0*f)}, {int(51*f)}, {int(0*f)})"

def pressed_bg_br(br=None):
    """按下背景色"""
    b = br if br is not None else _CURRENT_BRIGHTNESS
    f = _dim(b)
    return f"rgb({int(0*f)}, {int(85*f)}, {int(0*f)})"

# 保持向后兼容（初始化阶段用）
BORDER_COLOR = text_color_br()
TEXT_COLOR = text_color_br()
HOVER_BG = hover_bg_br()
PRESSED_BG = pressed_bg_br()

# 按钮行号标签
ROW_LABELS = {
    0: "第0行: I/P, 连体空白(可变), 可变显示, SETTINGS",
    1: "第1行: SYSTEMS, 1, N2, 3, 可变显示, EM CON",
    2: "第2行: W4, 5, E6, 可变显示",
    3: "第3行: COMM1, 7, S8, 9, 可变显示, COMM2",
    4: "第4行: 可变显示, CLR, 0, ENT, 可变显示, 可变显示",
    5: "第5行: <, >, A/P, IFF, TCN, ILS, D/L, BCN, ON OFF, <, >",
}

# 所有可选的按键名称 (单个键)
KEY_OPTIONS_SINGLE = [
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "A", "B", "C", "D", "E", "F", "G", "H", "I", "J",
    "K", "L", "M", "N", "O", "P", "Q", "R", "S", "T",
    "U", "V", "W", "X", "Y", "Z",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "Left", "Right", "Up", "Down",
    "Home", "End", "Insert", "Delete", "PageUp", "PageDown",
    "Return", "Backspace", "Tab", "Escape", "Space",
    "minus", "equal", "period", "comma", "slash", "backslash",
    "semicolon", "apostrophe", "bracketLeft", "bracketRight",
    "grave", "CapsLock", "NumLock", "ScrollLock", "Print",
    "Alt", "Control", "Shift", "Meta",
]

# 组合键预设 (Ctrl/Shift/Alt/Meta/Win + 键)
KEY_OPTIONS_COMBO = [
    "Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+A",
    "Ctrl+S", "Ctrl+F", "Ctrl+N", "Ctrl+O", "Ctrl+W",
    "Shift+Tab", "Alt+Tab", "Ctrl+Shift+Tab",
    "Alt+F4", "Ctrl+Shift+Escape",
    "Ctrl+Left", "Ctrl+Right", "Ctrl+Up", "Ctrl+Down",
    "Shift+Left", "Shift+Right", "Shift+Up", "Shift+Down",
    "Ctrl+Home", "Ctrl+End", "Shift+Home", "Shift+End",
    "Ctrl+Shift+Left", "Ctrl+Shift+Right",
    "Ctrl+plus", "Ctrl+minus", "Ctrl+0",
    "Win+D", "Win+E", "Win+R", "Win+L",
    "Ctrl+PageUp", "Ctrl+PageDown",
]

KEY_OPTIONS = KEY_OPTIONS_SINGLE + KEY_OPTIONS_COMBO

# ============================================
# DCS-BIOS 数据接收与解析
# ============================================

class DCSBIOSParser:
    """
    DCS-BIOS Skunkworks 协议解析器
    
    帧结构（BIOSStateMachine:step + MemoryMap:flushData）：
      - 帧同步：4字节 0x55 0x55 0x55 0x55
      - 数据记录：[Address:2LE][Length:2LE][Data:N_bytes]
        - Address/Data 都是 2-byte word 编码（encodeInt: lowbyte, highbyte）
        - Length 是字节数（2的倍数）
        - Data 中每个 word 的低字节=ASCII字符，高字节=0x00（字符串）
    
    多个模块的 flushData 输出拼接在一起，每个模块可能产生多个数据记录
    """
    
    # Skunkworks 版帧同步：4个 0x55 连续字节
    FRAME_SYNC = b'\x55\x55\x55\x55'
    
    def __init__(self):
        self.buffer = bytearray()
        # 状态内存：address -> word value（每个地址2字节）
        self.state = bytearray(65536)
        # 地址→字段名 映射
        self.address_to_field = {}  # {address: (field_name, length)} — 字符串字段
        self.analog_addresses = {}  # {address: internal_name} — 模拟量字段 (uint16 → 0.0~1.0)
        self.synced = False
        
    def inject_address_map(self, addr_map: dict):
        """注入地址→字段名映射，格式: {address: (field_name, length)}"""
        self.address_to_field = addr_map
        
    def _find_sync(self) -> int:
        """在 buffer 中搜索帧同步字节序列，返回其起始位置"""
        try:
            return self.buffer.index(self.FRAME_SYNC)
        except ValueError:
            return -1

    def _extract_record(self, offset: int):
        """
        从 buffer[offset:] 提取一条数据记录
        返回 (address, consumed_bytes, written_addresses) 或 None
        """
        if offset + 4 > len(self.buffer):
            return None
        
        addr_lo = self.buffer[offset]
        addr_hi = self.buffer[offset + 1]
        address = addr_lo | (addr_hi << 8)
        
        len_lo = self.buffer[offset + 2]
        len_hi = self.buffer[offset + 3]
        length = len_lo | (len_hi << 8)
        
        if offset + 4 + length > len(self.buffer):
            return None  # 数据不完整
        
        data = self.buffer[offset + 4 : offset + 4 + length]
        written = []  # 记录写入的地址
        
        # Length 是字节数，每次写 2 字节（一个 word）
        for i in range(0, length, 2):
            if i + 2 <= len(data):
                word_addr = address + i
                self.state[word_addr] = data[i]
                self.state[word_addr + 1] = data[i + 1]
                written.append(word_addr)
        
        return (4 + length, written)  # consumed, written addresses
        
    def parse(self, data: bytes, debug=False):
        """
        解析一个 UDP 包，返回本次更新的字段列表
        返回: [(field_name, value_str), ...]
        """
        self.buffer.extend(data)
        updated_fields = []  # [(field_name, value_str)]
        updated_addrs = set()
        _frame_count = 0
        _record_count = 0
        
        while True:
            # 搜索帧同步
            sync_pos = self._find_sync()
            if sync_pos < 0:
                if not self.synced:
                    # 还没同步，保留最后3字节（可能是部分同步字）
                    if len(self.buffer) > 3:
                        del self.buffer[:-3]
                    break
                else:
                    break
            
            # 找到帧同步
            self.synced = True
            _frame_count += 1
            # 丢弃帧同步之前的字节 + 同步字本身
            del self.buffer[:sync_pos + 4]
            
            # 帧同步后是一系列数据记录
            while len(self.buffer) >= 4:
                # 检查是否遇到下一个帧同步
                if self.buffer[:4] == self.FRAME_SYNC:
                    break
                
                result = self._extract_record(0)
                if result is None:
                    break
                
                consumed, written = result
                del self.buffer[:consumed]
                for addr in written:
                    updated_addrs.add(addr)
                _record_count += 1
            
            # 如果缓冲区不完整，break 外层等待更多数据
            if len(self.buffer) < 4 or self.buffer[:4] != self.FRAME_SYNC:
                break
        
        # 批量提取字符串值（只提取可打印 ASCII，去重）
        # DCS-BIOS Skunkworks: 每个16-bit word含2个8-bit字符，连续字节存储
        _reported = set()
        for field_addr, (field_name, field_len) in self.address_to_field.items():
            field_touched = False
            # 检查 field_addr ~ field_addr+field_len-1 之间的字节是否有更新
            for i in range(field_len):
                # 判断该字节所在的 word 地址是否被更新过
                word_addr = field_addr + (i & ~1)  # i=0,1→word0, i=2,3→word2, ...
                if word_addr in updated_addrs:
                    field_touched = True
                    break
            
            if field_touched and field_name not in _reported:
                _reported.add(field_name)
                chars = []
                # DCS-BIOS Skunkworks 位打包: 每个16-bit word 含2个8-bit字符（低字节=char[0], 高字节=char[1]）
                # 内存是连续字节布局，直接 field_addr + i 读取
                for i in range(field_len):
                    ch = self.state[field_addr + i]  # 连续字节读取（非跳字节）
                    if 0x20 <= ch <= 0x7E:  # 仅可打印 ASCII
                        chars.append(chr(ch))
                    elif ch != 0:
                        chars.append(' ')  # 控制字符 → 空格
                val_str = ''.join(chars)
                updated_fields.append((field_name, val_str))
                    
        return updated_fields


class DCSBIOSReceiver(threading.Thread):
    """
    DCS-BIOS UDP 数据接收线程
    
    工作流程：
      1. 监听 UDP 组播 239.255.50.10:5010
      2. 第一次收到完整数据包时，扫描 MetadataStart 帧获取字段地址
      3. 将地址映射注入解析器
      4. 持续解析数据并回调
    """
    
    DCS_BIOS_IP   = "239.255.50.10"
    DCS_BIOS_PORT = 5010
    
    # F/A-18C UFC 感兴趣的字段（从 DCS-BIOS 源码提取的实际字段名）
    # 格式: { 'DCS-BIOS字段名': ('内部变量名', UI_pos) }
    UFC_FIELDS = {
        'UFC_SCRATCHPAD_NUMBER_DISPLAY': ('scratchpad_number', (0, "blank"), 8),
        'UFC_SCRATCHPAD_STRING_1_DISPLAY': ('scratchpad_str1',  (0, "blank"), 2),
        'UFC_SCRATCHPAD_STRING_2_DISPLAY': ('scratchpad_str2',  (0, "blank"), 2),
        'UFC_COMM1_DISPLAY':              ('comm1',             (4, 0),       2),
        'UFC_COMM2_DISPLAY':              ('comm2',             (4, 5),       2),
        'UFC_OPTION_DISPLAY_1':           ('option_1',          (0, 4),       4),
        'UFC_OPTION_DISPLAY_2':           ('option_2',          (1, 4),       4),
        'UFC_OPTION_DISPLAY_3':           ('option_3',          (2, 4),       4),
        'UFC_OPTION_DISPLAY_4':           ('option_4',          (3, 4),       4),
        'UFC_OPTION_DISPLAY_5':           ('option_5',          (4, 4),       4),
        'UFC_OPTION_CUEING_1':            ('cueing_1',          (0, 4),       1),
        'UFC_OPTION_CUEING_2':            ('cueing_2',          (1, 4),       1),
        'UFC_OPTION_CUEING_3':            ('cueing_3',          (2, 4),       1),
        'UFC_OPTION_CUEING_4':            ('cueing_4',          (3, 4),       1),
        'UFC_OPTION_CUEING_5':            ('cueing_5',          (4, 4),       1),
        'UFC_BRT':                        ('ufc_brightness',    None,         0),  # UFC 亮度旋钮 0.0~1.0
    }

    # 需要拼接成组合字符串的位置 → 需要合并的内部字段名列表
    # (内部名 → BIOS字段名的反向映射在 __init__ 时构建)
    COMBINED_DISPLAYS = {
        (0, "blank"): ('scratchpad_str1', 'scratchpad_str2', 'scratchpad_number'),  # 长条
        (0, 4):       ('cueing_1', 'option_1'),  # 第0行右侧: cuing + 选项
        (1, 4):       ('cueing_2', 'option_2'),
        (2, 4):       ('cueing_3', 'option_3'),
        (3, 4):       ('cueing_4', 'option_4'),
        (4, 4):       ('cueing_5', 'option_5'),
    }

    # 每个位置的期望字符数（用于单字居中补齐）
    SLOT_WIDTHS = {
        (4, 0): 2,
        (4, 5): 2,
    }

    @staticmethod
    def pad_text(value, pos):
        """按位置期望宽度居中。单字用 HTML div 绕过 Hornet 字体空格问题"""
        w = DCSBIOSReceiver.SLOT_WIDTHS.get(pos, 0)
        if w <= 1:
            return value
        s = str(value).strip()
        if len(s) >= w:
            return s
        # 补齐空格后用 HTML div + text-align:center 强制居中
        padded = s.center(w)
        return f'<div style="text-align:center;">{padded}</div>'
    
    # 字段名 → UI 位置映射（供外部查询）
    DISPLAY_POS_MAP = {
        info[0]: info[1]
        for info in UFC_FIELDS.values()
        if info[1] is not None
    }

    # 内部名 → BIOS字段名 反向映射（用于从 latest 获取值）
    _INTERNAL_TO_BIOS = {}
    @classmethod
    def _build_maps(cls):
        """构建反向映射（首次用）"""
        if not cls._INTERNAL_TO_BIOS:
            for bios_name, (internal, _pos, _len) in cls.UFC_FIELDS.items():
                cls._INTERNAL_TO_BIOS[internal] = bios_name

    def __init__(self, callback=None):
        super().__init__(daemon=True)
        self.callback = callback
        self.parser   = DCSBIOSParser()
        self.sock     = None
        self.running  = False
        self.latest   = {}
        self._addr_map_built = False
        self._last_packet_time = 0.0   # 任何数据包到达都更新（不受值去重影响）
        
    def _build_address_map_from_metadata(self, raw_udp: bytes):
        """
        从 MetadataStart 帧的 JSON 中提取字段地址
        MetadataStart 帧包含一个 JSON 字符串，描述所有字段的地址和长度
        
        DCS-BIOS 新版使用 TCP(42674) 提供完整 JSON，UDP 不含元数据
        所以我们改用"地址学习"方式：监听一段时间，从帧中推断字段地址
        """
        pass  # 见 _learn_addresses
        
    # DCS-BIOS 生成的地址文件路径
    ADDRESS_H_PATH = r"C:\Users\Administrator\Saved Games\DCS\Scripts\DCS-BIOS\doc\Addresses.h"
    JSON_PATH      = r"C:\Users\Administrator\Saved Games\DCS\Scripts\DCS-BIOS\doc\json\FA-18C_hornet.json"

    # 直接从 Lua 模块提取的字段名 → 长度（不需要地址）
    KNOWN_FIELDS = {
        'UFC_SCRATCHPAD_NUMBER_DISPLAY':  8,
        'UFC_SCRATCHPAD_STRING_1_DISPLAY': 2,
        'UFC_SCRATCHPAD_STRING_2_DISPLAY': 2,
        'UFC_COMM1_DISPLAY':               2,
        'UFC_COMM2_DISPLAY':               2,
        'UFC_OPTION_DISPLAY_1':            4,
        'UFC_OPTION_DISPLAY_2':            4,
        'UFC_OPTION_DISPLAY_3':            4,
        'UFC_OPTION_DISPLAY_4':            4,
        'UFC_OPTION_DISPLAY_5':            4,
        'UFC_OPTION_CUEING_1':             1,
        'UFC_OPTION_CUEING_2':             1,
        'UFC_OPTION_CUEING_3':             1,
        'UFC_OPTION_CUEING_4':             1,
        'UFC_OPTION_CUEING_5':             1,
    }

    @classmethod
    def _parse_addresses_h(cls, path: str):
        """从 Addresses.h 解析 FA_18C_hornet_ 开头的 string 字段地址"""
        import re
        addr_map = {}
        if not os.path.exists(path):
            return addr_map
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                # 匹配: #define FA_18C_hornet_UFC_SCRATCHPAD_NUMBER_DISPLAY_A 0x7446
                m = re.match(
                    r'#define\s+FA_18C_hornet_(UFC_\w+)_A\s+(0x[0-9A-Fa-f]+)',
                    line.strip()
                )
                if m:
                    field_name = m.group(1)
                    addr_str   = m.group(2)
                    if field_name in cls.KNOWN_FIELDS:
                        length = cls.KNOWN_FIELDS[field_name]
                        addr_map[int(addr_str, 16)] = (field_name, length)
        return addr_map

    def _learn_addresses(self):
        """
        从 DCS-BIOS 生成的文件读取字段地址映射。
        优先读取 Addresses.h（dev_mode=true 时始终生成），
        其次尝试 JSON。
        """
        import json

        # 方法1: 读 Addresses.h（最可靠，始终生成）
        addr_map = self._parse_addresses_h(self.ADDRESS_H_PATH)
        if addr_map:
            self.parser.inject_address_map(addr_map)
            print(f"[DCS-BIOS] Loaded {len(addr_map)} field addresses from Addresses.h")
            self._addr_map_built = True
            self._inject_analog_addresses()
            return True

        # 方法2: 读 JSON（可选）
        if os.path.exists(self.JSON_PATH):
            try:
                with open(self.JSON_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                addr_map = {}
                for category_name, controls in data.items():
                    if not isinstance(controls, dict):
                        continue
                    for control_id, control in controls.items():
                        outputs = control.get('outputs', [])
                        for output in outputs:
                            if output.get('type') == 'string':
                                addr = output.get('address')
                                length = output.get('max_length', 2)
                                if addr is not None and control_id in self.UFC_FIELDS:
                                    addr_map[addr] = (control_id, length)
                if addr_map:
                    self.parser.inject_address_map(addr_map)
                    print(f"[DCS-BIOS] Loaded {len(addr_map)} field addresses from JSON")
                    self._addr_map_built = True
                    self._inject_analog_addresses()
                    return True
            except Exception as e:
                print(f"[DCS-BIOS] Failed to read JSON: {e}")

        # 方法3: 使用内嵌的真实地址（从 Addresses.h 硬编码备份）
        print("[DCS-BIOS] No external source found, using embedded addresses (synced from Addresses.h)")
        self._use_fallback_addresses()
        return False
        
    def _use_fallback_addresses(self):
        """
        嵌入式地址备份 — 与 Addresses.h 同步。
        当 DCS-BIOS 生成 Addresses.h 后，会被 _parse_addresses_h() 自动读取并覆盖。
        """
        # ⚠️ 这些地址必须与 DCS-BIOS 生成的 Addresses.h 保持一致！
        # 最后同步: 2025-06-10 (DCS-BIOS skunkworks, F/A-18C hornet)
        fallback = {
            0x7424: ('UFC_COMM1_DISPLAY',              2),
            0x7426: ('UFC_COMM2_DISPLAY',              2),
            0x7428: ('UFC_OPTION_CUEING_1',            1),
            0x742a: ('UFC_OPTION_CUEING_2',            1),
            0x742c: ('UFC_OPTION_CUEING_3',            1),
            0x742e: ('UFC_OPTION_CUEING_4',            1),
            0x7430: ('UFC_OPTION_CUEING_5',            1),
            0x7432: ('UFC_OPTION_DISPLAY_1',           4),
            0x7436: ('UFC_OPTION_DISPLAY_2',           4),
            0x743a: ('UFC_OPTION_DISPLAY_3',           4),
            0x743e: ('UFC_OPTION_DISPLAY_4',           4),
            0x7442: ('UFC_OPTION_DISPLAY_5',           4),
            0x7446: ('UFC_SCRATCHPAD_NUMBER_DISPLAY',  8),
            0x744e: ('UFC_SCRATCHPAD_STRING_1_DISPLAY',2),
            0x7450: ('UFC_SCRATCHPAD_STRING_2_DISPLAY',2),
        }

        addr_map = {}
        for addr, (field_name, length) in fallback.items():
            if field_name in self.UFC_FIELDS:
                addr_map[addr] = (field_name, length)

        self.parser.inject_address_map(addr_map)
        print(f"[DCS-BIOS] Using fallback address map ({len(addr_map)} fields)")
        self._addr_map_built = True
        self._inject_analog_addresses()
    
    def _inject_analog_addresses(self):
        """注入模拟量字段地址（Potentiometer/Float 类，uint16 编码 0~65535 → 0.0~1.0）"""
        # 来源: Addresses.h #define FA_18C_hornet_UFC_BRT_A 0x741E
        # 实际值是 uint16 at 0x741E (little-endian)
        self.parser.analog_addresses[0x741E] = 'ufc_brightness'
        print(f"[DCS-BIOS] Injected 1 analog field (UFC_BRT @ 0x741E)")
        
    def run(self):
        """线程主循环"""
        self.running = True
        
        # 先尝试从 JSON 文件获取地址映射
        self._learn_addresses()
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("", self.DCS_BIOS_PORT))
            
            mreq = struct.pack("4sL", socket.inet_aton(self.DCS_BIOS_IP), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.sock.settimeout(0.5)
            
            print(f"[DCS-BIOS] Listening on {self.DCS_BIOS_IP}:{self.DCS_BIOS_PORT}")
            
            _packet_count = 0
            _last_retry = time.time()
            _last_value = {}  # 记录每个字段上次的值，只在变化时打印
            _nonblank_count = 0  # 累计非空白更新次数
            _data_active = False  # 是否收到过非空格数据
            _last_summary = time.time()
            _addr_file_loaded = self._addr_map_built and (
                os.path.exists(self.ADDRESS_H_PATH) or os.path.exists(self.JSON_PATH)
            )
            
            while self.running:
                try:
                    data, _ = self.sock.recvfrom(65535)
                    _packet_count += 1
                    self._last_packet_time = time.time()  # 包级心跳，不受值去重影响
                    
                    # 如果还在用 fallback 地址，定期重试读外部文件（DCS 可能已经生成了）
                    if not _addr_file_loaded and time.time() - _last_retry > 10.0:
                        _last_retry = time.time()
                        if os.path.exists(self.ADDRESS_H_PATH) or os.path.exists(self.JSON_PATH):
                            print("[DCS-BIOS] External address file now available, reloading...")
                            self._learn_addresses()
                            _addr_file_loaded = True
                    
                    updated = self.parser.parse(data)
                    
                    for field_name, value in updated:
                        self.latest[field_name] = value
                        # 统计非空格数据
                        stripped = value.strip()
                        if stripped:
                            _nonblank_count += 1
                            _data_active = True
                        # 只在值变化时回调（避免刷屏）
                        prev = _last_value.get(field_name)
                        if value != prev:
                            _last_value[field_name] = value
                            if self.callback and field_name in self.UFC_FIELDS:
                                internal_name = self.UFC_FIELDS[field_name][0]
                                self.callback(internal_name, value)
                    
                    # ==== 模拟量字段（如 UFC_BRT）====
                    for addr, internal_name in self.parser.analog_addresses.items():
                        # 读 2 字节 uint16 little-endian
                        lo = self.parser.state[addr]
                        hi = self.parser.state[addr + 1]
                        raw = lo | (hi << 8)
                        val = raw / 65535.0  # 0.0 ~ 1.0
                        val = round(val, 3)
                        self.latest[internal_name] = str(val)  # 存入 latest 供查询
                        prev_a = _last_value.get(internal_name)
                        if val != prev_a:
                            _last_value[internal_name] = val
                            if self.callback:
                                self.callback(internal_name, str(val))
                    
                    # 每30秒输出数据活跃度摘要
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print(f"[DCS-BIOS] Receive error: {e}")
                        
        except Exception as e:
            print(f"[DCS-BIOS] Failed to start: {e}")
        finally:
            self.stop()
            
    def _process_data(self, data: bytes):
        """处理接收到的数据"""
        results = self.parser.parse(data)
        
        for result in results:
            address = result['address']
            value = result['value']
            
            if address in self.ADDRESS_MAP:
                field_name, field_type = self.ADDRESS_MAP[address]
                
                # 类型转换
                if field_type == int:
                    try:
                        value = int(value)
                    except:
                        value = 0
                        
                self.latest_data[field_name] = value
                
                # 触发回调
    def stop(self):
        """停止接收"""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
        print("[DCS-BIOS] Stopped")


# ============ 精确布局参数 (基于用户提供的尺寸数据) ============
# 左右留白8px, 上下留白7px
# 第0行: y=7,  h=90
# 第1行: y=114, h=90  (7+90+17)
# 第2行: y=221, h=90
# 第3行: y=328, h=90
# 第4行: y=435, h=90
# 第5行: y=542, h=50

Y0, H0 = 7, 90
Y1, H1 = 114, 90
Y2, H2 = 221, 90
Y3, H3 = 328, 90
Y4, H4 = 435, 90
Y5, H5 = 542, 50

# 第5行小按钮专用高度常量
ROW5_H = H5  # 50

# 水平坐标
# 第0行特殊: I/P(140) | gap(16) | 连体空白(425) | gap(16) | RTTH(255) | gap(16) | SETTINGS(140)
# 第1-4行: col0=140 | gap16 | col1=121 | gap31 | col2=121 | gap31 | col3=121 | gap16 | col4=255 | gap16 | col5=140

# 第0行坐标
R0_X = [8, 164, 605, 876]           # I/P, 连体空白, RTTH, SETTINGS
R0_W = [140, 425, 255, 140]

# 第1-4行标准6列坐标
COL_X = [8, 164, 316, 468, 605, 876]
COL_W = [140, 121, 121, 121, 255, 140]

# 第5行小按钮坐标 (11个按钮, 两端各2个方向键各62px)
# (5,0): <,  (5,1): >,  (5,2): A/P ... (5,8): ON,  (5,9): <,  (5,10): >
R5_BTN_X = [8, 70, 157, 262, 367, 472, 577, 682, 787, 892, 954]
R5_BTN_W = [62, 62, 80, 80, 80, 80, 80, 80, 80, 62, 62]

ROW_Y = [Y0, Y1, Y2, Y3, Y4, Y5]
ROW_H = [H0, H1, H2, H3, H4, H5]

MARGIN_L = 8
MARGIN_R = 8

# ============ 布局辅助函数 ============
def col_x(n):
    """返回第n列(0-5)的x坐标"""
    if 0 <= n < len(COL_X):
        return COL_X[n]
    return 8 + n * (140 + 16)  # fallback

def col_w(n):
    """返回第n列(0-5)的宽度"""
    if 0 <= n < len(COL_W):
        return COL_W[n]
    return 140  # fallback

def row_y(n):
    """返回第n行(0-5)的y坐标"""
    if 0 <= n < len(ROW_Y):
        return ROW_Y[n]
    return 7 + n * (90 + 17)  # fallback

def row_h(n):
    """返回第n行(0-5)的高度"""
    if 0 <= n < len(ROW_H):
        return ROW_H[n]
    return 90 if n < 5 else 50  # fallback

def wide_w():
    """返回宽方块的宽度 (第4列, 605~860)"""
    return 255


class UFCCell(QFrame):
    """UFC 按钮单元格 - 有边框可点击"""
    clicked = pyqtSignal(tuple)

    def __init__(self, text, pos, font_size=16, is_variable=False, bold=False, parent=None, no_feedback=False, var_align=None):
        super().__init__(parent)
        self.pos = pos
        self._no_feedback = no_feedback  # True → 不显示按压/触摸视觉反馈
        self._is_variable = is_variable  # 必须在 _refresh_stylesheet() 之前！Qt 事件可能提前触发
        # 可变显示文本对齐（None=默认居中, 如 AlignRight|TextDontClip）
        self._var_align = var_align
        self._bios_pressed = False  # 当前是否已发送 press(1) 等待 release(0)
        
        # 存储当前颜色（由 _apply_brightness 更新）
        self._tc = TEXT_COLOR
        self._bc = BORDER_COLOR
        self._hb = HOVER_BG
        self._pb = PRESSED_BG
        
        self._refresh_stylesheet()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 触摸屏支持
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self._touch_active = False

        if is_variable:
            # 可变显示单元格：用 QPainter 直接绘制文本
            self.label = None
            self._var_text = text
            self._var_font = get_hornet_font(font_size)
            self._var_font.setBold(bold)
            self._label_font = None
            self._label_font_size = font_size
            self._label_bold = bold
        else:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(2, 2, 2, 2)
            layout.setSpacing(0)
            self.label = QLabel(text)
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.label.setWordWrap(True)
            self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            font = QFont("B612", font_size)
            font.setBold(bold)
            self.label.setFont(font)
            self.label.setStyleSheet(f"color: {self._tc}; background: transparent;")
            layout.addWidget(self.label)
            self._var_text = None
            self._var_font = None
            self._label_font = font
            self._label_font_size = font_size
            self._label_bold = bold

    def event(self, e):
        """触摸事件处理 + 视觉反馈。
        注意：return True 接受触摸会阻止 Qt 自动合成 mousePressEvent，
        所以 TouchBegin/TouchEnd 中直接应用/恢复样式并 emit clicked。"""
        t = e.type()
        # 诊断：OSB 相关格子（column 4 的可变显示区）打印触控事件
        if self._is_variable and self.pos is not None and self.pos[1] == 4:
            pass  # 第4行可变显示：跳过 TouchBegin/TouchEnd 日志（已移除调试打印）
        # ── 触控按下 → DCS-BIOS press ──
        if t == QtCore.QEvent.Type.TouchBegin:
            self._touch_active = True
            if not self._no_feedback:
                self.setStyleSheet(f"""
                    UFCCell {{ background-color: {self._pb}; border: 2px solid {self._bc}; border-radius: 0px; }}
                """)
            # 发送 DCS-BIOS press (1)
            if self.pos is not None:
                bios_entry = UFC_BIOS_MAP.get(self.pos)
                if isinstance(bios_entry, str):
                    send_dcs_bios(bios_entry, 1)
                    self._bios_pressed = True
            e.accept()
            return True
        # ── 触控移动 ──
        elif t == QtCore.QEvent.Type.TouchUpdate:
            e.accept()
            return True
        # ── 触控松开 → DCS-BIOS release (延迟保证分帧) → emit clicked ──
        elif t == QtCore.QEvent.Type.TouchEnd:
            self._touch_active = False
            if not self._no_feedback:
                self._refresh_stylesheet()
            if self.pos is not None:
                bios_entry = UFC_BIOS_MAP.get(self.pos)
                if isinstance(bios_entry, str) and self._bios_pressed:
                    identifier = bios_entry
                    QTimer.singleShot(_MIN_PRESS_MS, lambda id=identifier: _send_release(id))
                    self._bios_pressed = False
                elif isinstance(bios_entry, tuple):
                    identifier, value = bios_entry
                    send_dcs_bios(identifier, value)
                self.clicked.emit(self.pos)
            e.accept()
            return True
        return super().event(e)

    def contextMenuEvent(self, event):
        """触摸屏长按禁止右键菜单 / 右键菜单"""
        if self._touch_active:
            event.accept()  # 吃掉触摸产生的右键事件
        else:
            super().contextMenuEvent(event)

    def setText(self, text):
        """设置可变显示文本（仅 is_variable=True 时有效）"""
        if self._is_variable:
            self._var_text = text
            self.repaint()  # 强制立即重绘（亮度变化后 setText 立即生效）
        elif self.label:
            self.label.setText(text)

    def paintEvent(self, event):
        """可变显示单元格：QPainter 手动绘制。"""
        super().paintEvent(event)
        if not self._is_variable or self._var_font is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setFont(self._var_font)
        brightness = _CURRENT_BRIGHTNESS
        f = _dim(brightness)
        green = int(255 * f)
        painter.setPen(QtGui.QColor(0, green, 0))

        # ── scratchpad 长条：三字段独立渲染，不用拼接 ──
        if self._var_text == "__SCRATCHPAD__" and hasattr(self, '_scratchpad_parts') and self._scratchpad_parts:
            # 三字段宽度比例（按 UFC 实际布局）：str1=2ch, str2=2ch, number=8ch → 共12ch
            # 用字体等宽假设估算每字符宽度，实际用 fontMetrics 量
            metrics = QtGui.QFontMetrics(self._var_font)
            ch_w = metrics.horizontalAdvance('0')   # 数字0的宽度作为参考等宽宽度
            if ch_w == 0:
                ch_w = 22   # fallback
            h = self.height()
            baseline = (h + metrics.ascent() - metrics.descent()) // 2

            # str1: 左侧起始，左对齐，取2字符
            str1 = self._scratchpad_parts[0]
            if len(str1) < 2:
                str1 = str1.ljust(2)
            else:
                str1 = str1[:2]
            painter.drawText(6, baseline, str1)

            # str2: str1 后留一字符间距，左对齐，取2字符
            str2 = self._scratchpad_parts[1]
            if len(str2) < 2:
                str2 = str2.ljust(2)
            else:
                str2 = str2[:2]
            x2 = 6 + ch_w * 3   # str1 2ch + 1ch 间距
            painter.drawText(x2, baseline, str2)

            # number: 右对齐，最后一个数字固定在右侧边距
            num = self._scratchpad_parts[2]
            num = num.rjust(8)[-8:]
            num_rect = QRect(0, 0, self.width() - 4, h)
            painter.drawText(num_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, num)
        else:
            # 普通可变显示：按 var_align 渲染
            align = self._var_align if self._var_align is not None else Qt.AlignmentFlag.AlignCenter
            draw_rect = self.contentsRect()
            painter.drawText(draw_rect, align, self._var_text)
        painter.end()

    def enterEvent(self, event):
        """触摸模式下跳过 hover 效果"""
        if not self._touch_active:
            super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._touch_active:
            super().leaveEvent(event)

    def mousePressEvent(self, event):
        """鼠标/触控板点击反馈 → DCS-BIOS press(1)。
        注意：原生触控开启时触摸走 event(TouchEnd) 路径，不会经过这里。
        原生触控关闭时 Windows 合成鼠标事件走这个路径。"""
        if self._touch_active or self._no_feedback:
            super().mousePressEvent(event)
            return
        self.setStyleSheet(f"""
            UFCCell {{ background-color: {self._pb}; border: 2px solid {self._bc}; border-radius: 0px; }}
        """)
        # 发送 DCS-BIOS press (1)
        if self.pos is not None:
            bios_entry = UFC_BIOS_MAP.get(self.pos)
            if isinstance(bios_entry, str):
                send_dcs_bios(bios_entry, 1)
                self._bios_pressed = True
        # 不再 emit clicked on press — 等 release
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        """鼠标松开 → DCS-BIOS release(0) 延迟 → emit clicked"""
        if not self._no_feedback:
            self._refresh_stylesheet()
        if self.pos is not None:
            bios_entry = UFC_BIOS_MAP.get(self.pos)
            if isinstance(bios_entry, str) and self._bios_pressed:
                identifier = bios_entry
                QTimer.singleShot(_MIN_PRESS_MS, lambda id=identifier: _send_release(id))
                self._bios_pressed = False
            elif isinstance(bios_entry, tuple):
                identifier, value = bios_entry
                send_dcs_bios(identifier, value)
            self.clicked.emit(self.pos)
        super().mouseReleaseEvent(event)

    def _refresh_stylesheet(self):
        """重建 stylesheet（用于亮度变化或重置样式）"""
        self.setStyleSheet(f"""
            UFCCell {{
                background-color: {BG_COLOR};
                border: 2px solid {self._bc};
                border-radius: 0px;
            }}
            UFCCell:hover {{
                background-color: {self._hb};
            }}
        """)
        # 刷新 QLabel 文字颜色（可变显示单元格无 QLabel，走 QPainter）
        if hasattr(self, 'label') and self.label:
            self.label.setStyleSheet(f"color: {self._tc}; background: transparent;")

    def _apply_brightness(self, brightness, tc, bc, hb, pb):
        """亮度变化时更新颜色并强制重绘"""
        self._tc = tc
        self._bc = bc
        self._hb = hb
        self._pb = pb
        self._refresh_stylesheet()
        self.repaint()  # 强制立即同步重绘（update 可能被 setStyleSheet 的内部重绘覆盖）


class UFCBlank(QFrame):
    """空白占位方块 - 无边框不可点击"""
    def __init__(self, parent=None, bordered=False):
        super().__init__(parent)
        self._bordered = bordered
        if bordered:
            self.setStyleSheet(f"background-color: {BG_COLOR}; border: 2px solid {border_color_br()};")
        else:
            self.setStyleSheet(f"background-color: {BG_COLOR}; border: none;")


class UFCKeypadWindow(QWidget):
    """UFC键盘面板 - 固定1024x600"""
    keyPressed = pyqtSignal(str)
    keyLogUpdated = pyqtSignal(str, str)  # (时间, 按键名) 按键日志
    _dcs_signal = pyqtSignal(str, str)  # 线程安全：DCS-BIOS 数据更新信号
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UFC Keypad - Panel")
        self.setFixedSize(WIN_W, WIN_H)
        self.setStyleSheet(f"background-color: {BG_COLOR};")
        # 触摸屏优化
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        
        self.cells = {}
        self.display_cells = {}  # 可变显示方块: {pos: UFCCell}
        self._brightness = 0.0   # 当前亮度（DCS 未连接时最低）
        self._blanks = []        # 有边框空白方块引用
        self._key_press_log = [] # 最近按键记录 (最多 5 条)
        self._last_dcs_data_time = 0.0   # 最后一次收到 DCS-BIOS 数据的时间
        self._dcs_disconnected = True   # DCS 是否已断开（启动时默认断连）
        
        # ==== DCS-BIOS 接收器 ====
        self._dcs_signal.connect(self._update_display)
        self.dcs_bios = DCSBIOSReceiver(callback=self.on_dcsbios_data)
        self.dcs_bios.start()
        
        # ==== DCS 连接看门狗（退出游戏后自动清空显示） ====
        self._dcs_watchdog = QTimer(self)
        self._dcs_watchdog.timeout.connect(self._check_dcs_timeout)
        self._dcs_watchdog.start(2000)  # 每2秒检查一次
        
        self.setStyleSheet(f"background-color: {BG_COLOR};")
        self.init_ui()

        # ==== WH_MOUSE_LL 光标锁定（启动钩子线程） ====
        self._native_touch_enabled = False
        self._noactivate_applied = False   # showEvent 里只应用一次
        _start_mouse_hook()

        # 每 2 秒刷新 DCS 显示器范围（应对 DCS 窗口移动/切换显示器）
        self._monitor_refresh_timer = QTimer(self)
        self._monitor_refresh_timer.timeout.connect(_find_dcs_monitor)
        self._monitor_refresh_timer.start(2000)

        config = load_config()
        if config.get("native_touch", False):
            self.enable_native_touch(True)

        # _apply_noactivate_style() 已移到 showEvent 中调用（确保窗口句柄已创建）

    def _apply_noactivate_style(self):
        """用 ctypes 设置 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW，
        确保 Windows 不会因触摸/点击激活本窗口。
        同时调用 SetWindowPos(SWP_FRAMECHANGED) 强制样式生效。"""
        hwnd = int(self.winId())
        cur = _user32.GetWindowLongPtrW(ctypes.wintypes.HWND(hwnd), GWL_EXSTYLE)
        if cur == 0:
            err = ctypes.get_last_error()
            if err != 0:
                print(f"[窗口] ⚠️ GetWindowLongPtrW 失败: {err}")
                return
        new_style = cur
        changed = False
        if not (cur & WS_EX_NOACTIVATE):
            new_style |= WS_EX_NOACTIVATE
            changed = True
        if not (cur & WS_EX_TOOLWINDOW):
            new_style |= WS_EX_TOOLWINDOW
            changed = True
        if changed:
            _user32.SetWindowLongPtrW(
                ctypes.wintypes.HWND(hwnd), GWL_EXSTYLE,
                ctypes.c_longlong(new_style)
            )
            # 强制 Windows 重新应用窗口样式
            _user32.SetWindowPos(
                ctypes.wintypes.HWND(hwnd), None,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED
            )
            print("[窗口] ✓ WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW 已设置")



    def showEvent(self, event):
        """窗口首次显示后应用 WS_EX_NOACTIVATE，确保句柄已创建"""
        super().showEvent(event)
        if not self._noactivate_applied:
            self._apply_noactivate_style()
            self._noactivate_applied = True

    def _on_activate(self, hwnd, msg_obj):
        """WM_ACTIVATE 兜底：如果窗口被意外激活，立即把前台交还给 DCS"""
        # wParam: 0=INACTIVE, 1=ACTIVE, 2=CLICKACTIVE
        wParam = msg_obj.wParam if hasattr(msg_obj, 'wParam') else 0
        if wParam in (1, 2):
            hwnd_dcs = _find_dcs_window()
            if hwnd_dcs:
                # 用 AttachThreadInput 避免跨线程前台切换被 Windows 阻止
                self._safe_activate_dcs(hwnd_dcs)

    def _safe_activate_dcs(self, hwnd_dcs):
        """尝试把前台切换回 DCS（降低权限强制切换）"""
        try:
            _user32.SetForegroundWindow(ctypes.wintypes.HWND(hwnd_dcs))
        except Exception:
            pass

    def nativeEvent(self, eventType, message):
        """Windows 原生消息处理"""
        if eventType == b"windows_generic_MSG":
            msg = ctypes.wintypes.MSG.from_address(int(message))
            # WM_ACTIVATE 兜底：如果窗口被意外激活，把前台交还 DCS
            if msg.message == WM_ACTIVATE:
                self._on_activate(None, msg)
                return True, 0
            # WM_MOUSEACTIVATE：在 Windows 激活窗口之前拦截
            if msg.message == WM_MOUSEACTIVATE:
                return True, MA_NOACTIVATE
            if self._native_touch_enabled and msg.message == WM_TOUCH:
                return False, 0
        return False, 0

    def enable_native_touch(self, enable):
        """启用/禁用原生触控隔离（阻止触摸→鼠标转换）"""
        hwnd = int(self.winId())
        if enable:
            if _register_native_touch(hwnd):
                self._native_touch_enabled = True
                print("[触控] ✅ 原生触控隔离已启用（触摸不再模拟鼠标）")
                config = load_config()
                config["native_touch"] = True
                save_config(config)
            else:
                print("[触控] ❌ 原生触控注册失败（系统可能不支持）")
                self._native_touch_enabled = False
        else:
            if _unregister_native_touch(hwnd):
                self._native_touch_enabled = False
                print("[触控] 原生触控隔离已关闭")
                config = load_config()
                config["native_touch"] = False
                save_config(config)

    def on_dcsbios_data(self, field_name, value):
        """接收到 DCS-BIOS 数据，更新UI (线程安全)"""
        # 使用 signal.emit 确保线程安全（QTimer+lambda 存在跨线程捕获问题）
        self._dcs_signal.emit(field_name, value)
        
    def _update_display(self, field_name, value):
        """更新显示单元格 (主线程执行)"""
        # 每次收到数据都刷新时间戳
        self._last_dcs_data_time = time.time()
        if self._dcs_disconnected:
            self._dcs_disconnected = False
            print("[DCS-BIOS] 🟢 DCS 重新连接，恢复数据显示")
            # 立即从缓存恢复亮度（不依赖 ufc_brightness 回调，因为模拟量值未变时不触发）
            cached = self.dcs_bios.latest.get('ufc_brightness')
            if cached:
                try:
                    b = float(cached)
                    b = max(0.0, min(1.0, b))
                    self._brightness = b
                    self._refresh_brightness()
                except (ValueError, TypeError):
                    pass
        
        # ==== 亮度同步 ====
        if field_name == 'ufc_brightness':
            try:
                b = float(value)
                b = max(0.0, min(1.0, b))
                if abs(b - self._brightness) > 0.01:  # 去掉微小抖动
                    self._brightness = b
                    self._refresh_brightness()
            except (ValueError, TypeError):
                pass
            return
        
        # 确保反向映射已构建
        DCSBIOSReceiver._build_maps()
        inv = DCSBIOSReceiver._INTERNAL_TO_BIOS
        
        # 1) 处理组合显示
        for pos, internal_list in DCSBIOSReceiver.COMBINED_DISPLAYS.items():
            if field_name in internal_list and pos in self.display_cells:
                cell = self.display_cells[pos]
                if pos == (0, "blank"):
                    # scratchpad 长条：三字段独立存储，不拼接
                    parts = []
                    for iname in internal_list:
                        bname = inv.get(iname)
                        val = self.dcs_bios.latest.get(bname, '') if bname else ''
                        # 清理：只保留可打印字符，去掉尾随空字符/空格后的垃圾
                        val = ''.join(c for c in str(val) if c in ' 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,/-+')
                        parts.append(val)
                    cell._scratchpad_parts = parts
                    cell._var_text = "__SCRATCHPAD__"   # 标记，触发新渲染路径
                    cell.update()   # 触发 paintEvent
                else:
                    # 其他组合显示（OSB 旁的 cueing+option）：正常拼接
                    parts = []
                    for iname in internal_list:
                        bname = inv.get(iname)
                        parts.append(self.dcs_bios.latest.get(bname, '') if bname else '')
                    combined = ''.join(parts)
                    cell.setText(combined)
        
        # 2) 处理单独映射的字段
        if field_name in DCSBIOSReceiver.DISPLAY_POS_MAP:
            pos = DCSBIOSReceiver.DISPLAY_POS_MAP[field_name]
            # 跳过已在 COMBINED_DISPLAYS 中处理的位置
            if any(field_name in fl for fl in DCSBIOSReceiver.COMBINED_DISPLAYS.values()):
                pass
            elif pos in self.display_cells:
                self.display_cells[pos].setText(str(value))
        else:
            _uk = f"_uk_{field_name}"
            if not hasattr(self, _uk):
                setattr(self, _uk, True)
                print(f"[UI] ⚠️ 字段 {field_name} 不在 DISPLAY_POS_MAP 中")
    
    def _check_dcs_timeout(self):
        """看门狗：断连检测 + 自动恢复。使用 _last_packet_time（每次收包都更新，不受值去重影响）"""
        pkt_t = self.dcs_bios._last_packet_time
        # ── 恢复检测：断连状态下，UDP 包重新到达 ──
        if self._dcs_disconnected:
            if pkt_t > self._last_dcs_data_time:
                self._dcs_disconnected = False
                self._last_dcs_data_time = pkt_t
                print("[DCS-BIOS] 🟢 DCS 重新连接，恢复数据显示")
                # 恢复亮度
                cached = self.dcs_bios.latest.get('ufc_brightness')
                if cached:
                    try:
                        b = float(cached)
                        self._brightness = max(0.0, min(1.0, b))
                        self._refresh_brightness()
                    except (ValueError, TypeError):
                        pass
                # 恢复所有可变显示单元格（从 latest 缓存读取，值未变时回调不触发）
                DCSBIOSReceiver._build_maps()
                inv = DCSBIOSReceiver._INTERNAL_TO_BIOS
                # 1) 组合显示（scratchpad + OSB）
                for pos, internal_list in DCSBIOSReceiver.COMBINED_DISPLAYS.items():
                    if pos not in self.display_cells:
                        continue
                    cell = self.display_cells[pos]
                    if pos == (0, "blank"):
                        parts = []
                        for iname in internal_list:
                            bname = inv.get(iname)
                            val = self.dcs_bios.latest.get(bname, '') if bname else ''
                            val = ''.join(c for c in str(val) if c in ' 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.,/-+')
                            parts.append(val)
                        if any(p.strip() for p in parts):
                            cell._scratchpad_parts = parts
                            cell._var_text = "__SCRATCHPAD__"
                            cell.update()
                    else:
                        parts = [self.dcs_bios.latest.get(inv.get(iname), '') if inv.get(iname) else '' for iname in internal_list]
                        combined = ''.join(parts)
                        cell.setText(combined)
                # 2) 独立字段
                for internal_name, bios_name in inv.items():
                    val = self.dcs_bios.latest.get(bios_name, '')
                    if val and internal_name in DCSBIOSReceiver.DISPLAY_POS_MAP:
                        pos = DCSBIOSReceiver.DISPLAY_POS_MAP[internal_name]
                        if pos in self.display_cells:
                            self.display_cells[pos].setText(str(val))
                return
            return  # 断连且无新包，静默等待
        
        # ── 断连检测：超过10秒无包 ──
        if pkt_t == 0.0:
            return  # 尚未收到任何数据
        elapsed = time.time() - pkt_t
        if elapsed > 10.0:
            self._dcs_disconnected = True
            self._last_dcs_data_time = pkt_t  # 记录断开时的包时间，用于恢复比较
            print(f"[DCS-BIOS] 🔴 DCS 信号丢失 (已{elapsed:.0f}秒无包)，清空显示并熄灭亮度")
            self._brightness = 0.0
            self._refresh_brightness()
            for pos, cell in self.display_cells.items():
                if pos == (0, "blank"):
                    cell._scratchpad_parts = []
                cell._var_text = ""
                cell.update()
    
    def _refresh_brightness(self):
        """亮度变化 → 刷新所有元素颜色"""
        global _CURRENT_BRIGHTNESS
        _CURRENT_BRIGHTNESS = self._brightness
        b = self._brightness
        
        # 重新计算颜色
        tc = text_color_br(b)
        bc = border_color_br(b)
        hb = hover_bg_br(b)
        pb = pressed_bg_br(b)
        
        # 刷新所有按钮 cell
        for cell in self.cells.values():
            cell._apply_brightness(b, tc, bc, hb, pb)
        
        # 刷新所有可变显示 cell
        for cell in self.display_cells.values():
            if cell not in self.cells.values():  # 避免重复刷新
                cell._apply_brightness(b, tc, bc, hb, pb)
        
        # 刷新有边框空白方块
        for blank in self._blanks:
            blank.setStyleSheet(f"background-color: {BG_COLOR}; border: 2px solid {bc};")
            
    def set_display(self, pos, text):
        """设置可变显示方块的文本"""
        if pos in self.display_cells:
            self.display_cells[pos].setText(text)

    def init_ui(self):
        """精确布局 - 基于用户提供的尺寸数据"""

        # ============ 第0行: I/P(140) | 连体空白(425, 可变) | RTTH(255, 可变) | SETTINGS(140) ============
        self.place_cell("I/P", (0, 0), 8, 7, 140, 90, font_size=22)
        c = self.place_cell("", None, 164, 7, 425, 90, font_size=32, is_variable=True, register=False, no_feedback=True, var_align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.display_cells[(0, "blank")] = c
        c = self.place_cell("", (0, 4), 605, 7, 255, 90, font_size=28, is_variable=True)
        self.display_cells[(0, 4)] = c
        self.place_cell("SETTINGS", (0, 5), 876, 7, 140, 90, font_size=20)

        # ============ 第1行: SYSTEMS(140) | 1(121) | N2(121) | 3(121) | HSEL(255, 可变) | EM CON(140) ============
        self.place_cell("SYSTEMS", (1, 0), 8, 114, 140, 90, font_size=20)
        self.place_cell("1", (1, 1), 164, 114, 121, 90, font_size=22)
        self.place_cell("N\n2", (1, 2), 316, 114, 121, 90, font_size=18)
        self.place_cell("3", (1, 3), 468, 114, 121, 90, font_size=22)
        c = self.place_cell("", (1, 4), 605, 114, 255, 90, font_size=28, is_variable=True)
        self.display_cells[(1, 4)] = c
        self.place_cell("EM\nCON", (1, 5), 876, 114, 140, 90, font_size=20)

        # ============ 第2行: 空白(140) | W4(121) | 5(121) | E6(121) | BALT(255, 可变) | 空白(140) ============
        self.place_blank(8, 221, 140, 90)
        self.place_cell("W\n4", (2, 1), 164, 221, 121, 90, font_size=18)
        self.place_cell("5", (2, 2), 316, 221, 121, 90, font_size=22)
        self.place_cell("E\n6", (2, 3), 468, 221, 121, 90, font_size=18)
        c = self.place_cell("", (2, 4), 605, 221, 255, 90, font_size=28, is_variable=True)
        self.display_cells[(2, 4)] = c
        self.place_blank(876, 221, 140, 90)

        # ============ 第3行: COMM1(140) | 7(121) | S8(121) | 9(121) | RALT(255, 可变) | COMM2(140) ============
        self.place_cell("COMM 1", (3, 0), 8, 328, 140, 90, font_size=20)
        self.place_cell("7", (3, 1), 164, 328, 121, 90, font_size=22)
        self.place_cell("S\n8", (3, 2), 316, 328, 121, 90, font_size=18)
        self.place_cell("9", (3, 3), 468, 328, 121, 90, font_size=22)
        c = self.place_cell("", (3, 4), 605, 328, 255, 90, font_size=28, is_variable=True)
        self.display_cells[(3, 4)] = c
        self.place_cell("COMM 2", (3, 5), 876, 328, 140, 90, font_size=20)

        # ============ 第4行: COMM1(140,动态) | CLR(121) | 0(121) | ENT(121) | 空白(255, 可变) | COMM2(140,动态) ============
        c = self.place_cell("", (4, 0), 8, 435, 140, 90, font_size=32, is_variable=True, no_feedback=True)
        self.display_cells[(4, 0)] = c
        self.place_cell("CLR", (4, 1), 164, 435, 121, 90, font_size=18)
        self.place_cell("0", (4, 2), 316, 435, 121, 90, font_size=22)
        self.place_cell("ENT", (4, 3), 468, 435, 121, 90, font_size=18)
        c = self.place_cell("", (4, 4), 605, 435, 255, 90, font_size=28, is_variable=True)
        self.display_cells[(4, 4)] = c
        c = self.place_cell("", (4, 5), 876, 435, 140, 90, font_size=32, is_variable=True, no_feedback=True)
        self.display_cells[(4, 5)] = c

        # ============ 第5行: <(62) >(62) | A/P(80) | IFF(80) | TCN(80) | ILS(80) | D/L(80) | BCN(80) | ON(80) | <(62) >(62) ============
        y5 = 542
        h5 = 50
        self.place_cell("<", (5, 0), 8, y5, 62, h5, font_size=16)
        self.place_cell(">", (5, 1), 70, y5, 62, h5, font_size=16, bold=True)
        self.place_cell("A/P", (5, 2), 157, y5, 80, h5, font_size=13, bold=True)
        self.place_cell("IFF", (5, 3), 262, y5, 80, h5, font_size=13)
        self.place_cell("TCN", (5, 4), 367, y5, 80, h5, font_size=13)
        self.place_cell("ILS", (5, 5), 472, y5, 80, h5, font_size=13)
        self.place_cell("D/L", (5, 6), 577, y5, 80, h5, font_size=13)
        self.place_cell("BCN", (5, 7), 682, y5, 80, h5, font_size=13)
        self.place_cell("ON\nOFF", (5, 8), 787, y5, 80, h5, font_size=13)
        self.place_cell("<", (5, 9), 892, y5, 62, h5, font_size=16, bold=True)
        self.place_cell(">", (5, 10), 954, y5, 62, h5, font_size=16, bold=True)

    def place_cell(self, text, pos, x, y, w, h, font_size=16, is_variable=False, bold=False, register=True, no_feedback=False, var_align=None):
        cell = UFCCell(text, pos, font_size=font_size, is_variable=is_variable, bold=bold, parent=self, no_feedback=no_feedback, var_align=var_align)
        cell.setGeometry(x, y, w, h)
        if pos is not None:
            cell.clicked.connect(self.on_cell_click)
            if register:
                self.cells[pos] = cell
        return cell

    def place_blank(self, x, y, w, h):
        blank = UFCBlank(parent=self, bordered=False)
        blank.setGeometry(x, y, w, h)
        return blank

    def place_bordered_blank(self, x, y, w, h):
        """放置一个有边框的空白连体方块"""
        blank = UFCBlank(parent=self, bordered=True)
        blank.setGeometry(x, y, w, h)
        self._blanks.append(blank)
        return blank

    def on_cell_click(self, pos):
        """UFCCell 点击回调。DCS-BIOS press/release 已由 UFCCell 内部处理，
        这里只管日志。"""
        bios_entry = UFC_BIOS_MAP.get(pos)

        # 记录按键日志
        ts = time.strftime("%H:%M:%S", time.localtime())
        if isinstance(bios_entry, tuple):
            log_key = f"{bios_entry[0]} {bios_entry[1]}"
        else:
            log_key = bios_entry or "—"
        self._key_press_log.append((ts, log_key))
        if len(self._key_press_log) > 50:
            self._key_press_log.pop(0)
        self.keyLogUpdated.emit(ts, log_key)

    def simulate_keypress(self, key):
        """使用 Windows SendInput API 发送系统级按键到前台窗口（DCS）。
        支持组合键: Ctrl+C, LAlt+C, Shift+Tab 等。"""
        inject_key_combo(key)

    def keyPressEvent(self, event: QKeyEvent):
        super().keyPressEvent(event)

    def closeEvent(self, event):
        """窗口关闭时停止所有后台线程"""
        print("[UFC] Stopping DCS-BIOS receiver...")
        if hasattr(self, 'dcs_bios'):
            self.dcs_bios.stop()
        print("[UFC] Stopping WH_MOUSE_LL hook...")
        _stop_mouse_hook()
        event.accept()


class SettingsWindow(QWidget):
    """设置窗口 - 始终在主屏显示"""
    def __init__(self, key_panel):
        super().__init__()
        self.setWindowTitle("UFC Keypad - Settings")
        self.setMinimumSize(850, 600)
        self.resize(750, 650)

        self.key_panel = key_panel
        self._key_log_lines = []  # 按键日志行缓存

        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a2e;
                color: #e0e0e0;
                font-family: 'Microsoft YaHei', 'Segoe UI';
                font-size: 12px;
            }
            QGroupBox {
                border: 1px solid #3a3a6a;
                border-radius: 6px;
                margin-top: 10px;
                font-weight: bold;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #a78bfa;
            }
            QComboBox {
                background: #16162a;
                border: 1px solid #3a3a6a;
                border-radius: 3px;
                padding: 3px 6px;
                color: #e0e0e0;
            }
            QComboBox QAbstractItemView {
                background: #16162a;
                border: 1px solid #3a3a6a;
                color: #e0e0e0;
                selection-background-color: #3d3d6a;
            }
            QPushButton {
                background: #2d2d4a;
                border: 1px solid #4a4a7a;
                border-radius: 4px;
                padding: 6px 14px;
                min-width: 80px;
            }
            QPushButton:hover { background: #3d3d6a; }
            QPushButton#primary {
                background: #5a3daa;
                color: white;
                font-weight: bold;
            }
            QPushButton#primary:hover { background: #7a5dca; }
            QPushButton#danger {
                background: #aa3d3d;
                color: white;
            }
            QPushButton#danger:hover { background: #cc5555; }
        """)

        self.init_ui()
        self.refresh_screen_list()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        title = QLabel("UFC Keypad 设置面板")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #a78bfa;")
        layout.addWidget(title)

        # 显示器选择
        screen_group = QGroupBox("显示器选择")
        screen_lo = QHBoxLayout(screen_group)

        screen_lo.addWidget(QLabel("输出到显示器:"))
        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(200)
        screen_lo.addWidget(self.screen_combo)

        self.fullscreen_cb = QCheckBox("全屏")
        self.fullscreen_cb.setChecked(True)
        screen_lo.addWidget(self.fullscreen_cb)

        self.always_top_cb = QCheckBox("置顶")
        self.always_top_cb.setChecked(False)
        screen_lo.addWidget(self.always_top_cb)

        screen_lo.addStretch()

        self.apply_screen_btn = QPushButton("应用显示器")
        self.apply_screen_btn.clicked.connect(self.apply_screen)
        screen_lo.addWidget(self.apply_screen_btn)

        self.refresh_screen_btn = QPushButton("刷新")
        self.refresh_screen_btn.clicked.connect(self.refresh_screen_list)
        screen_lo.addWidget(self.refresh_screen_btn)

        layout.addWidget(screen_group)

        # ==== 原生触控隔离（方案四：保底手段） ====
        touch_group = QGroupBox("触控隔离 (副屏触摸不抢主屏鼠标)")
        touch_lo = QHBoxLayout(touch_group)
        self.native_touch_cb = QCheckBox("启用原生触控隔离 (RegisterTouchWindow + FINETOUCH)")
        self.native_touch_cb.setToolTip(
            "阻止 Windows 将副屏触摸转换为鼠标事件，避免光标跳到副屏导致 DCS 失焦。\n"
            "启用后副屏触摸仅操作 UFC 面板，不会影响主屏游戏。\n"
            "⚠ 需要管理员权限运行才能生效。"
        )
        # 加载已保存的偏好
        config = load_config()
        self.native_touch_cb.setChecked(config.get("native_touch", False))
        self.native_touch_cb.toggled.connect(self._on_native_touch_toggled)
        touch_lo.addWidget(self.native_touch_cb)
        touch_lo.addStretch()
        layout.addWidget(touch_group)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8888aa; font-size: 11px;")
        layout.addWidget(self.status_label)

        # ==== 按键输入日志 ====
        log_group = QGroupBox("按键输入记录 (最近 50 条)")
        log_lo = QVBoxLayout(log_group)
        self.key_log_text = QTextEdit()
        self.key_log_text.setReadOnly(True)
        self.key_log_text.setMaximumHeight(150)
        self.key_log_text.setStyleSheet("""
            QTextEdit {
                background: #0d0d1a;
                color: #a0a0c0;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                border: 1px solid #2a2a4a;
                border-radius: 4px;
                padding: 4px;
            }
        """)
        log_lo.addWidget(self.key_log_text)
        layout.addWidget(log_group)

        # 连接 UFC 面板的按键日志信号
        self.key_panel.keyLogUpdated.connect(self._on_key_log)
        self._flush_log()  # 显示已有的日志（如果有）

    def _on_key_log(self, ts, key):
        """接收 UFC 面板按键日志"""
        self._key_log_lines.append(f"[{ts}] {key}")
        if len(self._key_log_lines) > 50:
            self._key_log_lines.pop(0)
        self._flush_log()

    def _flush_log(self):
        """刷新日志显示（最新在最上面）"""
        if self._key_log_lines:
            # _key_log_lines 存的是已格式化的字符串
            text = "\n".join(reversed(self._key_log_lines[-50:]))
        else:
            # 回退：_key_press_log 存的是 (ts, key) 元组
            lines = self.key_panel._key_press_log
            text = "\n".join(f"[{t}] {k}" for t, k in reversed(lines[-50:]))
        self.key_log_text.setPlainText(text)

    def refresh_screen_list(self):
        self.screen_combo.clear()
        screens = QApplication.screens()
        for i, screen in enumerate(screens):
            geo = screen.geometry()
            name = screen.name()
            self.screen_combo.addItem(
                f"显示器 {i}: {name} ({geo.width()}x{geo.height()})",
                userData=i
            )

    def _on_native_touch_toggled(self, checked):
        """原生触控隔离开关"""
        self.key_panel.enable_native_touch(checked)
        if checked:
            self.status_label.setText("原生触控隔离已启用 — 触摸不再抢鼠标")
        else:
            self.status_label.setText("原生触控隔离已关闭")

    def apply_screen(self):
        idx = self.screen_combo.currentData()
        if idx is None:
            return

        screens = QApplication.screens()
        if idx >= len(screens):
            self.status_label.setText("所选显示器不存在!")
            return

        screen = screens[idx]
        geo = screen.geometry()

        self.key_panel.showNormal()
        self.key_panel.move(geo.x(), geo.y())
        self.key_panel.setFixedSize(WIN_W, WIN_H)

        if self.fullscreen_cb.isChecked():
            self.key_panel.showFullScreen()

        if self.always_top_cb.isChecked():
            self.key_panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        else:
            self.key_panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)

        self.key_panel.show()

        self.status_label.setText(f"已输出到显示器 {idx}: {screen.name()} "
                                  f"{'全屏' if self.fullscreen_cb.isChecked() else '窗口'}")

    def closeEvent(self, event):
        """关闭设置窗口 → 退出整个程序"""
        QApplication.quit()


# ============ 入口 ============
if __name__ == '__main__':
    setup_crash_log()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    key_panel = UFCKeypadWindow()
    key_panel.hide()

    settings = SettingsWindow(key_panel)
    settings.show()

    sys.exit(app.exec())
