#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面待办清单挂件（Deepin / X11 + tkinter）。

一个常驻桌面、无边框的小窗口：回车添加、点勾选完成、右键条目标记「等待中」，
点 × 删除，所有改动立即写入 JSON，重开自动恢复待办与窗口位置。

每件事有三种状态：未完成 / 等待中（已着手，但在等别人反馈，自己现在动不了）/ 已完成。

删掉、归档的条目都不会丢：右键标题栏 →「查看归档…」能翻出来，并挑一条恢复回列表。

默认**不置顶**：它就是普通窗口，会被别的窗口盖住，不会一直挡在前面；
需要时在右键菜单里勾「始终置顶」（勾选状态会记住）。

它也**不在任务栏 / dock 里显示图标**（EWMH 的 `SKIP_TASKBAR`/`SKIP_PAGER`）。
被别的窗口盖住后，按全局快捷键（默认 `Super+T`，可用 `--hotkey` 改）、用 `Alt+Tab`，
或者**再运行一次本程序**，都能把它叫到最前面。

用法::

    python3 desktop_todo.py                    # 启动挂件；已在运行则把窗口叫到前面
    python3 desktop_todo.py --check            # 自检（数据读写 + 界面构造）后退出
    python3 desktop_todo.py --doctor           # 打印环境/窗口能力诊断报告后退出
    python3 desktop_todo.py --hotkey "Ctrl+Alt+K"    # 改全局唤回快捷键
    python3 desktop_todo.py --install-autostart     # 开机自启
    python3 desktop_todo.py --uninstall-autostart   # 取消开机自启

交互::

    回车 / 「+」      添加待办
    点文字或方框      标记完成 / 取消完成
    右键条目          等待中 / 今天要办的 / 优先级 / 子步骤 / 删除
    按住条目上下拖     调整顺序（松手即保存）
    双击条目          改文字
    点 ×             删除该条
    拖标题栏          移动窗口
    双击标题栏        折叠 / 展开（右键标题栏还有菜单，含「查看归档…」）
    Esc              清空输入框

数据文件: ``~/.local/share/desktop-todo/todos.json``
"""

import argparse
import json
import re
import ctypes
import os
import shutil
import socket
import sys
import tempfile
import time

try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:            # 没装 python3-tk 时，--doctor / --data-file 仍要能跑出信息
    tk = None
    tkfont = None

APP_ID = "desktop-todo"
# 商店规范要求倒置域名做包名；窗口类名/数据目录仍用 APP_ID，避免老用户数据搬家
APP_STORE_ID = "com.github.xinming-chenniang.desktoptodo"
APP_VERSION = "0.2.2"      # 发布版本；--version 打印它，--doctor 报告头部也带着它


class _XClientMessageEvent(ctypes.Structure):
    """Xlib 的 XClientMessageEvent（用来给 WM 发 _NET_WM_STATE 消息）。"""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


# ---------- 外观 ----------
PANEL_W = 300          # 面板宽度（像素）
PAD = 12               # 面板内边距
LIST_MAX_H = 268       # 列表区最大高度，超出后滚轮滚动
LIST_W = PANEL_W - 2 * PAD - 2          # 列表区宽度
TEXT_X = 36            # 条目文字起始 x（给勾选框留位置）
DEL_W = 30             # 条目右侧删除按钮的点击区宽度
ROW_PAD_Y = 6          # 条目上下内边距
ROW_WRAP = LIST_W - TEXT_X - DEL_W - 4  # 条目文字换行宽度
UNDO_SECONDS = 8       # 删除后「撤销」保留的秒数
ARCHIVE_MAX = 500      # 归档最多留多少条（超了丢最老的）
TAG_MAX = 3            # 一条最多几个情境标签
TAG_LEN = 8            # 单个标签最长几个字
TAG_RE = re.compile(r"(?:^|\s)@([\w\u4e00-\u9fff]{1,%d})(?=\s|$)" % TAG_LEN)
DOUBLE_CLICK_REVERT_S = 0.8   # 双击进入编辑时，把第一次单击的勾选撤回的时间窗
REMIND_POLL_MS = 15000        # 到点提醒的检查间隔（15 秒；只在主窗口里跑）
REMIND_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")

# 条目状态：未完成 / 等待中 / 已完成
# 「等待中」= 已经着手了，但在等别人反馈（现在自己动不了），所以既不算完成、也不该被当成待办催自己。
STATE_TODO = "todo"
STATE_WAITING = "waiting"
STATE_DONE = "done"
# 列表默认排序：未完成 → 等待中 → 已完成（同组内保持手动拖出来的顺序）
STATE_ORDER = (STATE_TODO, STATE_WAITING, STATE_DONE)
WAIT_W = 30             # 条目右侧「等待」按钮的点击区宽度
TODAY_W = 26            # 条目右侧「今天」按钮的点击区宽度
PRIO_W = 26             # 条目右侧「优先级」按钮的点击区宽度
STEPS_W = 30            # 条目右侧「子步骤」按钮的点击区宽度
STEP_MAX = 20           # 一条待办最多几个子步骤（防止数据被灌爆）
STEP_BOX = 11           # 子步骤小方框边长
STEP_INDENT = 20        # 子步骤文字相对条目文字的缩进

# 优先级：0 无 / 1 中 / 2 高（值越大越靠前）；点一下按这个顺序循环
PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH = 0, 1, 2
PRIORITY_CYCLE = (PRIORITY_NONE, PRIORITY_HIGH, PRIORITY_MID)

C_CARD = "#24272c"     # 卡片底色
C_LIST = "#1d2024"     # 输入框 / 列表底色
C_BORDER = "#3a3f47"   # 边框
C_TEXT = "#e9ebee"     # 主文字
C_DIM = "#98a0aa"      # 次要文字
C_MUTED = "#6d7480"    # 更淡的文字 / 图标
C_ACCENT = "#4c9aff"   # 强调色
C_DANGER = "#e5695f"   # 删除悬停
C_HOVER = "#2e333a"    # 拖动中的条目底色
C_DONE = "#7b828c"     # 已完成文字
C_WAIT = "#e0a33e"     # 等待中文字 / 图标
C_HIGH = "#ff7a59"     # 高优先级旗子

PLACEHOLDER = "添加一条待办，回车确认"
FONT_STACK = (
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Droid Sans Fallback",
    "DejaVu Sans",
)


# --------------------------------------------------------------------------- #
# 界面缩放（高 DPI / 系统缩放）
#
# Tk 的字号单位是「点」，会随显示器 DPI 自动放大；而本程序的窗口尺寸是像素，
# 不会跟着变。于是 125% / 150% 缩放下会出现「字变大、面板不变」：文字挤、
# 列表变窄、标题栏发紧。这里用统一的缩放系数把像素尺寸按同样比例放大。
#
# 系数取自 Tk 自己算的 tk scaling（X11 依据 Xft.dpi），
# 与字号用的是同一套依据，不会出现两套比例打架。缩放为 1.0 时，
# 下面所有 _scaled() 的返回值都与基准常量逐像素相同——即默认观感一点不变。
# --------------------------------------------------------------------------- #
TK_SCALING_AT_96DPI = 96.0 / 72.0     # Tk 的 scaling 就是「每点多少像素」


def ui_scale(root):
    """返回界面缩放系数（1.0 = 96dpi / 100%）。"""
    try:
        scaling = float(root.tk.call("tk", "scaling"))
    except (tk.TclError, ValueError, TypeError, AttributeError):
        return 1.0
    if scaling <= 0:
        return 1.0
    return max(1.0, min(3.0, scaling / TK_SCALING_AT_96DPI))


def _scaled(value, scale):
    """基准像素值 → 当前缩放下的像素值。

    scale=1.0 时**原样返回**（不改动、也不取整）：像勾选框的 6.5 / 21.5 这种半像素
    值一旦走 round() 会被银行家舍入（6.5→6、21.5→22），在 100% 缩放下反而比原来
    偏 0.5~1px，那就违反了「默认观感一点不变」。
    """
    if scale == 1.0:
        return value
    return int(round(value * scale))


# --------------------------------------------------------------------------- #
# 窗口管理器交互
# --------------------------------------------------------------------------- #
# 装着的那一个 X 错误处理器。装上期间必须有活引用：这个 ctypes 回调的 thunk 一旦被 GC，
# 而 Xlib 那边还留着它的地址（比如还原失败），后续 X 错误就会调到一个已释放的 trampoline。
# 2026-10-19 之前正是这样：还原那句传的是 int 地址、抛 ArgumentError 被 except 吞掉，
# 处理器从未还原，于是每次 --check 都刷一串
# `TypeError: <lambda>() takes 1 positional argument but 2 were given`（HANDOFF §3.11）。
_X_ERROR_HANDLER = [None]


def _on_wm_window(root, action):
    """打开 X 连接，找到 Tk toplevel 的 WM 窗口，交给 action(x11, display, target)。

    找不到就返回 False；任何 X 错误只当这次没做成，绝不把程序带崩。
    """
    try:
        import ctypes
        x11 = ctypes.CDLL("libX11.so.6")
    except (ImportError, OSError):
        return False

    handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    handler = handler_type(lambda display, event: 0)     # 忽略 X 错误，别让默认处理器退出进程
    previous = None
    display = None
    try:
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        # 形参按 c_void_p 声明，而不是 [handler_type]：XSetErrorHandler 的返回值也是一个
        # 函数指针（restype=c_void_p ⇒ Python 拿到的是 int 地址），finally 里要原样传回去。
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]

        display = x11.XOpenDisplay(None)
        if not display:
            return False
        _X_ERROR_HANDLER[0] = handler
        previous = x11.XSetErrorHandler(ctypes.cast(handler, ctypes.c_void_p))

        window = ctypes.c_ulong(root.winfo_id())
        root_ret, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        x11.XQueryTree(ctypes.c_void_p(display), window, ctypes.byref(root_ret),
                       ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count))
        root_window = x11.XDefaultRootWindow(ctypes.c_void_p(display))
        if parent.value and parent.value != root_window:
            target = parent.value                      # Tk 为 toplevel 建的 WM 窗口
        elif window.value and window.value != root_window:
            target = window.value
        else:
            return False                               # 窗口还没建出来
        action(x11, display, target, root_window)
        x11.XFlush(ctypes.c_void_p(display))
        return True
    except Exception:
        return False
    finally:
        if display:
            try:
                if previous:
                    x11.XSetErrorHandler(ctypes.c_void_p(previous))
                x11.XCloseDisplay(ctypes.c_void_p(display))
            except Exception:
                pass
        _X_ERROR_HANDLER[0] = None


def strip_window_decorations(root):
    """请窗口管理器去掉标题栏，但保留 WM 托管。

    ``overrideredirect(True)`` 的无边框窗口不受 WM 管理，Deepin 下点它拿不到
    键盘焦点（输入框没法打字），所以这里走另一条路：窗口交给 WM 托管，
    再用 ``_MOTIF_WM_HINTS`` 的 decorations=0 把标题栏去掉。
    必须在窗口第一次映射前、且 Tk 已经建出该 toplevel 的 WM 窗口之后调用
    （先做一次 ``wm attributes`` 调用、再 ``update_idletasks()`` 即可）。
    """

    def apply(x11, display, target, root_window):
        atom = x11.XInternAtom(ctypes.c_void_p(display), b"_MOTIF_WM_HINTS", 0)
        values = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)     # flags=DECORATIONS, decorations=0
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(atom), ctypes.c_ulong(atom), 32, 0,
                            ctypes.byref(values), 5)

    return _on_wm_window(root, apply)


def set_skip_taskbar(root):
    """告诉 WM：这个窗口不要出现在任务栏 / dock / 分页器里（EWMH 标准做法）。

    用 ``_NET_WM_STATE`` 的 ClientMessage 发 ``SKIP_TASKBAR`` + ``SKIP_PAGER``；
    故意不发 KDE 的 ``SKIP_SWITCHER``——那样连 Alt+Tab 都找不到它了，
    而这个窗口默认不置顶，总得留个能把它叫回来的入口。
    """

    def apply(x11, display, target, root_window):
        state_atom = x11.XInternAtom(ctypes.c_void_p(display), b"_NET_WM_STATE", 0)
        skip_taskbar = x11.XInternAtom(ctypes.c_void_p(display),
                                       b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pager = x11.XInternAtom(ctypes.c_void_p(display),
                                     b"_NET_WM_STATE_SKIP_PAGER", 0)

        # 1) 还没映射时：直接写 _NET_WM_STATE 属性，WM 建图时会读到（XA_ATOM = 4）
        atoms = (ctypes.c_ulong * 2)(skip_taskbar, skip_pager)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(state_atom), ctypes.c_ulong(4), 32, 0,
                            ctypes.byref(atoms), 2)

        # 2) 已经映射时：再按 EWMH 发的标准 ClientMessage（_NET_WM_STATE_ADD）
        buf = ctypes.create_string_buffer(192)           # 足够放一个 XEvent
        event = ctypes.cast(buf, ctypes.POINTER(_XClientMessageEvent)).contents
        event.type = 33
        event.serial = 0
        event.send_event = 1
        event.display = display
        event.window = target
        event.message_type = state_atom
        event.format = 32
        event.data[0] = 1                                # _NET_WM_STATE_ADD
        event.data[1] = skip_taskbar
        event.data[2] = skip_pager
        event.data[3] = 1                                # source indication: application
        event.data[4] = 0
        mask = 0x00180000                                # SubstructureRedirect | SubstructureNotify
        x11.XSendEvent(ctypes.c_void_p(display), ctypes.c_ulong(root_window), 0, mask,
                       ctypes.byref(event))

    return _on_wm_window(root, apply)


# --------------------------------------------------------------------------- #
# 全局快捷键（X11：把键抓在根窗口上，不需要 WM 配合）
#
# 为什么需要它：这个窗口刻意不进任务栏、也没有托盘图标（Deepin 23 的 dde-dock
# 只画它自己「持有列表」里的托盘项，第三方注册得再规范也不显示），
# 于是「把窗口叫回来」就只剩「再运行一次本程序」。全局快捷键把这个入口补上：
# 无论当前焦点在哪个窗口，按一下就能把它提到最前。
#
# XGrabKey 抓在根窗口上、owner_events=False：按键事件直接进我们这个 X 连接的队列，
# 所以用一个 Tk 定时器轮询就够了，不需要线程，也不需要动 Tk 自己的事件循环。
# --------------------------------------------------------------------------- #
HOTKEY_DEFAULT = "Super+T"

_HOTKEY_MODS = {
    "shift": 0x0001, "lock": 0x0002, "ctrl": 0x0004, "control": 0x0004,
    "mod1": 0x0008, "alt": 0x0008, "meta": 0x0008, "mod2": 0x0010,
    "mod3": 0x0020, "mod4": 0x0040, "super": 0x0040, "win": 0x0040,
    "cmd": 0x0040, "mod5": 0x0080,
}
_HOTKEY_KEYS = {
    "space": "space", "esc": "Escape", "escape": "Escape", "enter": "Return",
    "return": "Return", "tab": "Tab", "backspace": "BackSpace", "insert": "Insert",
    "delete": "Delete", "home": "Home", "end": "End", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "pageup": "Prior", "pagedown": "Next",
    "comma": "comma", "period": "period", "slash": "slash", "semicolon": "semicolon",
    "quote": "apostrophe", "backslash": "backslash", "minus": "minus", "equal": "equal",
    "grave": "grave", "bracketleft": "bracketleft", "bracketright": "bracketright",
}
_HOTKEY_LOCK_MASKS = (0x0000, 0x0002, 0x0010, 0x0002 | 0x0010)   # 无 / 大小写锁 / 数字锁 / 两者


def parse_hotkey(spec):
    """把 ``"Super+T"`` / ``"Ctrl+Alt+K"`` / ``"F5"`` 解析成 (修饰位掩码, keysym 名)。

    认不出来就返回 None（调用方据此退回默认值），不抛异常。
    """
    text = str(spec or "").replace(" ", "")
    if not text:
        return None
    mods, key = 0, None
    for piece in text.replace("-", "+").split("+"):
        piece = piece.strip().lower()
        if not piece:
            continue
        if piece in _HOTKEY_MODS:
            mods |= _HOTKEY_MODS[piece]
            continue
        if key is not None:
            return None                       # 出现了第二个主键，认不出来
        if piece in _HOTKEY_KEYS:
            key = _HOTKEY_KEYS[piece]
        elif len(piece) == 1 and ord(piece) < 128:
            key = piece
        elif len(piece) <= 3 and piece[0] == "f" and piece[1:].isdigit() \
                and 1 <= int(piece[1:]) <= 24:
            key = piece.upper()
        else:
            return None
    if key is None:
        return None
    return mods, key


def format_hotkey(spec):
    """把快捷键写成统一好看的样子（认不出来就原样返回）。"""
    parsed = parse_hotkey(spec)
    if parsed is None:
        return str(spec or "")
    mods, key = parsed
    names = []
    for name, mask in (("Ctrl", 0x0004), ("Alt", 0x0008), ("Shift", 0x0001),
                       ("Super", 0x0040)):
        if mods & mask:
            names.append(name)
    if key.startswith("F") and key[1:].isdigit():
        shown = key
    elif len(key) == 1:
        shown = key.upper()
    else:
        shown = {"space": "Space", "Return": "Enter", "Escape": "Esc", "Prior": "PageUp",
                 "Next": "PageDown", "BackSpace": "Backspace"}.get(key, key)
    return "+".join(names + [shown])


def parse_remind_time(text, now=None):
    """把 `HH:MM` 解析成「下一个该时刻」的时间戳；认不出返回 None。

    已经过了今天的这个点，就落到明天 —— 设提醒的人说「15:30」时，心里想的
    是最近的那个 15:30，而不是要求他先想清楚今天是几号。
    """
    match = REMIND_RE.match(str(text or ""))
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    now = time.time() if now is None else now
    local = time.localtime(now)
    target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                          hour, minute, 0, 0, 0, -1))
    if target <= now:
        target += 24 * 3600
    return target


def format_remind_time(when, now=None):
    """把提醒时间写成「15:30 / 明天 15:30 / 10-25 15:30」。"""
    now = time.time() if now is None else now
    target = time.localtime(when)
    today = time.localtime(now)
    tomorrow = time.localtime(now + 24 * 3600)
    clock = "%02d:%02d" % (target.tm_hour, target.tm_min)
    if (target.tm_year, target.tm_yday) == (today.tm_year, today.tm_yday):
        return clock
    if (target.tm_year, target.tm_yday) == (tomorrow.tm_year, tomorrow.tm_yday):
        return "明天 " + clock
    return "%02d-%02d " % (target.tm_mon, target.tm_mday) + clock


class _XKeyEvent(ctypes.Structure):
    """XKeyEvent 的前半段（XEvent 是联合体，这里只按 KeyPress 解释）。"""

    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class X11Hotkey(object):
    """X11 全局快捷键：抓根窗口 + Tk 定时器轮询。"""

    POLL_MS = 120
    KEY_PRESS = 2
    GRAB_MODE_ASYNC = 1

    def __init__(self):
        self.display = None
        self.root_window = 0
        self.keycode = 0
        self.masks = []
        self.callback = None
        self.job = None
        self.spec = None

    # ---- 注册 / 注销 ----
    def install(self, root, spec, callback):
        parsed = parse_hotkey(spec)
        if parsed is None:
            return False
        mods, key = parsed
        x11 = self._open()
        if x11 is None:
            return False
        keysym = x11.XStringToKeysym(key.encode("ascii"))
        if not keysym:
            self._close(x11)
            return False
        keycode = x11.XKeysymToKeycode(ctypes.c_void_p(self.display), ctypes.c_ulong(keysym))
        if not keycode:
            self._close(x11)
            return False

        self.root_window = x11.XDefaultRootWindow(ctypes.c_void_p(self.display))
        self.keycode = int(keycode)
        self.masks = [mods | extra for extra in _HOTKEY_LOCK_MASKS]
        # 抓的时候把 X 的默认错误处理器换掉：键被别的程序占了是 BadAccess，
        # 默认处理器会直接把进程打掉，必须自己收下这次错误。
        failed = []
        handler = self._handler_type(lambda display, event: failed.append(True) or 0)
        self._handler = handler                    # 别让回调被回收
        previous = x11.XSetErrorHandler(ctypes.cast(handler, ctypes.c_void_p))
        try:
            for mask in self.masks:
                x11.XGrabKey(ctypes.c_void_p(self.display), ctypes.c_int(self.keycode),
                             ctypes.c_uint(mask), ctypes.c_ulong(self.root_window),
                             ctypes.c_int(0), ctypes.c_int(self.GRAB_MODE_ASYNC),
                             ctypes.c_int(self.GRAB_MODE_ASYNC))
            x11.XSync(ctypes.c_void_p(self.display), ctypes.c_int(0))
        finally:
            x11.XSetErrorHandler(previous)
        if failed:
            self.remove()
            self._close(x11)
            return False
        self.callback = callback
        self.spec = spec
        self.job = (root, root.after(self.POLL_MS, self._poll, root))
        return True

    def remove(self):
        """注销（幂等）：先撤 after 任务，再解抓、关连接。"""
        if self.job is not None:
            root, job_id = self.job
            self.job = None
            try:
                root.after_cancel(job_id)
            except Exception:
                pass
        if self.display is None:
            return
        x11 = self._x11
        if x11 is not None and self.keycode:
            for mask in self.masks:
                try:
                    x11.XUngrabKey(ctypes.c_void_p(self.display), ctypes.c_int(self.keycode),
                                   ctypes.c_uint(mask), ctypes.c_ulong(self.root_window))
                except Exception:
                    pass
            try:
                x11.XSync(ctypes.c_void_p(self.display), ctypes.c_int(0))
            except Exception:
                pass
        self._close(x11)

    # ---- 内部 ----
    _x11 = None
    _handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _open(self):
        """开一条自己的 X 连接（必须活着，抓的键才有效）。"""
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XStringToKeysym.restype = ctypes.c_ulong
            x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
            x11.XKeysymToKeycode.restype = ctypes.c_ubyte
            x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x11.XSetErrorHandler.restype = ctypes.c_void_p
            x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            x11.XPending.restype = ctypes.c_int
            x11.XPending.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError):
            return None
        display = x11.XOpenDisplay(None)
        if not display:
            return None
        self.display = display
        self._x11 = x11
        return x11

    def _close(self, x11):
        if self.display is not None and x11 is not None:
            try:
                x11.XCloseDisplay(ctypes.c_void_p(self.display))
            except Exception:
                pass
        self.display = None
        self.keycode = 0
        self.masks = []

    def _poll(self, root):
        """定时器回调：把队列里的事件读掉，命中快捷键就回调。"""
        self.job = None
        x11 = self._x11
        if x11 is not None and self.display is not None:
            try:
                buffer = ctypes.create_string_buffer(192)
                while x11.XPending(ctypes.c_void_p(self.display)):
                    x11.XNextEvent(ctypes.c_void_p(self.display), buffer)
                    event = ctypes.cast(buffer, ctypes.POINTER(_XKeyEvent)).contents
                    if event.type == self.KEY_PRESS and self.callback is not None:
                        # 把按键事件的时间戳交给回调：EWMH 的 _NET_ACTIVE_WINDOW 要带
                        # 「触发这次唤回的那个事件」的时间，KWin 的防抢焦点才认（见 activate_window）。
                        try:
                            self.callback(int(event.time))
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            self.job = (root, root.after(self.POLL_MS, self._poll, root))
        except tk.TclError:
            self.job = None


# --------------------------------------------------------------------------- #
# 窗口能力与系统集成（X11 / EWMH，统信 UOS / Deepin）
#
# 本项目只服务统信 UOS / Deepin(X11)：2026-10-19 起 Windows 支持已整体移除，原来那层
# PlatformBackend / X11Backend / WindowsBackend 接口也随之删掉 —— 这一节就是唯一那份实现：
#   * 窗口外观：_MOTIF_WM_HINTS 去标题栏（strip_window_decorations）；
#   * 不进任务栏：EWMH 的 SKIP_TASKBAR + SKIP_PAGER（set_skip_taskbar）；
#   * 单实例唤回：unix 套接字 + Tk 文件事件（single_instance_start / _stop）；
#   * 全局快捷键：XGrabKey 抓根窗口（X11Hotkey，见上）；
#   * 开机自启：~/.config/autostart + 同名应用条目（Deepin/UOS 的 startdde 要能解析出应用 id）。
# --------------------------------------------------------------------------- #
_DIAG_PATH = None      # 唤回诊断日志的落点，由界面层 set_diag_path() 设成「数据文件旁边」


def set_diag_path(directory):
    """把唤回诊断日志放在这个目录里（自检跑临时目录时就不会碰用户真实数据）。"""
    global _DIAG_PATH
    _DIAG_PATH = os.path.join(directory, "hotkey.log")


def write_diag(text):
    """写一行唤回诊断日志；失败就算了，不影响使用。"""
    try:
        target = _DIAG_PATH
        if not target:
            directory = data_dir()
            if not os.path.isdir(directory):
                os.makedirs(directory)
            target = os.path.join(directory, "hotkey.log")
        # 日志只用于排障：超过 64 KB 就把旧的挪成 .1，免得在用户机器上无限长大
        try:
            if os.path.getsize(target) > 64 * 1024:
                os.replace(target, target + ".1")
        except (IOError, OSError):
            pass
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except (IOError, OSError):
        pass


# 「唤回」的最后一步：自己发 EWMH 的 _NET_ACTIVE_WINDOW。
#
# 为什么非要这一条：**KWin 忽略后台应用的 XRaiseWindow**。按 Super+D（显示桌面）之后，
# 窗口是 mapped 的、Tk 也认为一切正常（hotkey.log 里是 `state=normal viewable=1`），
# 但它被压在桌面层之下 —— deiconify / lift / 临时置顶都不够。2026-10-16 的取证：
# `xdotool windowactivate`（发的就是这条消息）能把它 100% 拉回来（逐像素比对 0% → 100%）。
_ACTIVE_WINDOW_SOURCE = 2      # 2 = paging tool。实测 KWin 认这个（与 xdotool 一致）；
                               # 1 = application 会被它的防抢焦点逻辑压住。


def _send_active_window(x11, display, target, root_window, timestamp=0):
    """请 WM 把 target 激活到最前（EWMH `_NET_ACTIVE_WINDOW` 的 ClientMessage）。

    `timestamp` 必须是**触发这次唤回的那个输入事件**的时间（全局快捷键的 KeyPress
    事件里就有）；拿不到时传 0（CurrentTime）。
    """
    atom = x11.XInternAtom(ctypes.c_void_p(display), b"_NET_ACTIVE_WINDOW", 0)
    buf = ctypes.create_string_buffer(192)           # 足够放一个 XEvent
    event = ctypes.cast(buf, ctypes.POINTER(_XClientMessageEvent)).contents
    event.type = 33
    event.serial = 0
    event.send_event = 1
    event.display = display
    event.window = target
    event.message_type = atom
    event.format = 32
    event.data[0] = _ACTIVE_WINDOW_SOURCE
    event.data[1] = int(timestamp or 0)
    event.data[2] = 0                                # 请求方当前的活动窗口：没有
    event.data[3] = 0
    event.data[4] = 0
    mask = 0x00180000                                # SubstructureRedirect | SubstructureNotify
    x11.XSendEvent(ctypes.c_void_p(display), ctypes.c_ulong(root_window), 0, mask,
                   ctypes.byref(event))


def activate_window(root, topmost, notify_wm=True, timestamp=0):
    """把窗口叫到最前面，且不改变用户设置的「始终置顶」状态。

    这里也是**所有唤回路径的总闸门**（全局快捷键 / 再跑一次 / 隐藏后唤回）——
    所以诊断日志写在这里，而不是某个具体的入口。

    `timestamp` = 触发唤回的那个输入事件的时间（快捷键能拿到，socket「再跑一次」拿不到、
    传 0）。**这个值很关键**：KWin 的防抢焦点会拿它跟「最后一次用户操作」比 ——
    传 0（CurrentTime）时，一个从没被激活过的窗口会被拒；实测同一台机器上
    「新实例第一次按 Super+T 不上来、第二次才上来」就是这个原因。
    `notify_wm=False` 只给自检用：那一步会真的请 WM 激活窗口，而 `--check`
    不该打扰桌面（自检里另有对这段接线的核对）。
    """
    before, remap = "", False
    try:
        before = "state=%s viewable=%s" % (root.state(), root.winfo_viewable())
        remap = root.state() != "normal"
    except tk.TclError:
        pass
    write_diag("activate 前: %s remap=%s ts=%s" % (before, remap, timestamp or 0))
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)      # 短暂置顶，确保能从别的窗口后面冒出来
        root.focus_force()
        if remap:
            # 有的 WM（例如 DDE 的"显示桌面"）把窗口留在 Iconic/Withdrawn 状态，
            # 只 deiconify 拉不回来 —— 做一次 withdraw→deiconify 强制重新映射。
            root.withdraw()
            root.deiconify()
            root.lift()
            root.focus_force()
        if notify_wm:
            # Tk 这几下对 KWin 不够：它忽略后台应用的 XRaiseWindow，得自己发 EWMH
            # （Super+D 之后按快捷键唤不回来，缺的就是这一条，见本文件顶部注释与 HANDOFF §3.9）
            def send(x11, display, target, root_window):
                _send_active_window(x11, display, target, root_window, timestamp)

            sent = _on_wm_window(root, send)
            write_diag("ewmh 激活请求=%s ts=%s" % (sent, timestamp or 0))
            # 冷启动后的**第一次**激活，KWin 有时会先拒一次（拒的同时记下时间戳，
            # 下一次就放行）：实测新实例上出现过「第一下 Super+T 不上来、第二下才行」。
            # 隔 120ms 补发一次把这个不确定性抹掉；窗口已经在前时补发是无害的
            # （实测连续按不会跳到别的窗口）。
            try:
                root.after(120, lambda: _on_wm_window(root, send))
            except tk.TclError:
                pass
        root.after(400, lambda: root.attributes("-topmost", bool(topmost)))
        write_diag("activate 后: state=%s viewable=%s"
                   % (root.state(), root.winfo_viewable()))
    except tk.TclError as error:
        write_diag("activate 失败: %r" % (error,))


# ---- 单实例：unix 套接字 + Tk 文件事件（第二个进程靠它唤回已有窗口）----
_SERVER = None


def single_instance_start(root, on_raise):
    """监听套接字：第二个进程连上来发一句 raise 就唤回窗口。"""
    global _SERVER
    path = ipc_socket_path()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if os.path.exists(path):
            os.unlink(path)
        server.bind(path)
        server.listen(2)
        server.setblocking(False)
    except OSError:
        server.close()
        return
    _SERVER = server

    def on_readable(_server, _mask):
        try:
            connection, _ = server.accept()
        except OSError:
            return
        try:
            connection.recv(64)
            connection.sendall(b"ok\n")
        except OSError:
            pass
        finally:
            connection.close()
        on_raise()

    try:
        root.tk.createfilehandler(server, tk.READABLE, on_readable)
    except (tk.TclError, AttributeError):
        _SERVER = None


def single_instance_stop(root):
    """释放套接字（退出前调用）。"""
    global _SERVER
    if _SERVER is None:
        return
    path = ipc_socket_path()
    try:
        root.tk.deletefilehandler(_SERVER)
    except (tk.TclError, AttributeError):
        pass
    try:
        _SERVER.close()
    except OSError:
        pass
    _SERVER = None
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


# ---- 全局快捷键：XGrabKey 抓根窗口（窗口不进任务栏，这是最可靠的唤回入口）----
_HOTKEY = None


def hotkey_install(root, spec, callback):
    """注册全局快捷键（按下时调用 callback()）；成功返回 True。"""
    global _HOTKEY
    hotkey = X11Hotkey()
    if hotkey.install(root, spec, callback):
        _HOTKEY = hotkey
        return True
    return False


def hotkey_remove():
    """注销全局快捷键（退出前调用，不然那个键会一直被空占着）。"""
    global _HOTKEY
    if _HOTKEY is not None:
        _HOTKEY.remove()
        _HOTKEY = None


# ---- 开机自启：~/.config/autostart/*.desktop ----
#
# Deepin / UOS 上只写 autostart 那条是不够的：startdde 会盯着这个目录，
# 新建的条目如果解析不出「应用 id」就会被它删掉（journal 里是
# `startmanager.go: RemoveAutostart failed: failed to get app id`），
# 实测创建后约 30 秒消失。所以这里同时写一条同名的应用条目到
# ~/.local/share/applications/，让 app id（= 文件名）能对上；顺带也给了
# 用户一个「应用菜单里点一下就唤回」的入口。
def autostart_path():
    """开机自启的 .desktop 落点。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "%s.desktop" % APP_ID)


def app_entry_target():
    """应用菜单条目的路径（Deepin 靠它解析自启项的应用 id）。"""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "applications", "%s.desktop" % APP_ID)


def _system_entry_paths():
    """系统级（.deb 安装的）应用菜单条目可能出现的两个位置。

    普通 deb 装到 /usr/share/applications；商店规范包只允许装在 /opt/apps/<appid>/ 下，
    桌面入口在 entries/applications 里。两处都要认，否则会重复写一份用户级条目。
    """
    return ["/usr/share/applications/%s.desktop" % APP_ID,
            "/opt/apps/%s/entries/applications/%s.desktop" % (APP_STORE_ID, APP_STORE_ID)]


def _has_system_entry():
    return any(os.path.exists(path) for path in _system_entry_paths())


def _desktop_entry(autostart):
    """autostart=True 时多带自启相关的键。"""
    script = os.path.abspath(__file__)
    # .deb 装过就在系统里自带图标与菜单条目；只有从源码目录跑时才退化成系统自带图标
    icon_installed = os.path.exists(
        "/usr/share/icons/hicolor/scalable/apps/%s.svg" % APP_ID) or os.path.exists(
        "/opt/apps/%s/entries/icons/hicolor/scalable/apps/%s.svg" % (APP_STORE_ID, APP_STORE_ID))
    icon = APP_ID if icon_installed else "checkbox-checked"
    entry = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=桌面待办清单",
        "Name[en]=Desktop To-do",
        "Comment=常驻桌面的待办清单挂件",
        "Comment[en]=Desktop to-do widget",
        "Exec=%s %s" % (sys.executable, script),
        "Icon=%s" % icon,
        "Terminal=false",
        "Categories=Utility;",
        "StartupWMClass=%s" % APP_ID,
        "X-Deepin-AppID=%s" % APP_ID,
        "X-Deepin-Vendor=user-custom",
    ]
    if autostart:
        entry += ["StartupNotify=false", "X-GNOME-Autostart-enabled=true"]
    return "\n".join(entry) + "\n"


def _write_entry(path, text):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def autostart_install():
    """写入开机自启（同时写应用条目），返回退出码。"""
    app_entry = app_entry_target()
    # .deb 已经装了系统级菜单条目；再写一份用户级的会出现两个「桌面待办」
    if not _has_system_entry():
        _write_entry(app_entry, _desktop_entry(autostart=False))
        print("已写入应用菜单条目: %s（Deepin/UOS 用它解析自启项的应用 id）" % app_entry)
    target = autostart_path()
    _write_entry(target, _desktop_entry(autostart=True))
    print("已写入开机自启: %s" % target)
    return 0


def autostart_uninstall():
    """移除开机自启与应用条目，返回退出码。"""
    target = autostart_path()
    if os.path.exists(target):
        os.remove(target)
        print("已移除开机自启: %s" % target)
    else:
        print("未找到开机自启项: %s" % target)
    app_entry = app_entry_target()
    if os.path.exists(app_entry):
        os.remove(app_entry)
        print("已移除应用菜单条目: %s" % app_entry)
    return 0



# --------------------------------------------------------------------------- #
# 数据层
# --------------------------------------------------------------------------- #
def data_dir():
    """返回数据目录（遵循 XDG_DATA_HOME）。"""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_ID)


def data_file():
    """返回待办数据文件路径。"""
    return os.path.join(data_dir(), "todos.json")


def ipc_socket_path():
    """单实例套接字路径（放在 XDG_RUNTIME_DIR 或临时目录）。"""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "%s.sock" % APP_ID)


def raise_running_instance():
    """已有实例在跑时让它把窗口叫到最前面；成功返回 True。

    任务栏里看不到这个窗口，所以「再运行一次」就是最顺手的唤回方式。
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(ipc_socket_path())
    except OSError:
        return False
    try:
        client.sendall(b"raise\n")
        client.recv(16)
    except OSError:
        return False
    finally:
        client.close()
    return True


class Store(object):
    """待办条目 + 窗口状态的持久化，单文件 JSON，改动即落盘。"""

    def __init__(self, path):
        self.path = path
        self.items = []
        # topmost 默认关闭：不置顶，就不会一直挡在其他窗口前面。
        # 需要时在右键菜单里勾「始终置顶」，勾选状态会被记住。
        self.ui = {"x": None, "y": None, "collapsed": False, "alpha": 0.94,
                   "topmost": False, "hotkey": HOTKEY_DEFAULT,
                   "width": None, "height": None}
        self.archive = []                 # 归档：完成的（过 00:00）/ 清掉的 / 删掉的都进这里
        self._next_id = 1
        self.load()

    # ---- 读写 ----
    def load(self):
        """读取数据；文件缺失按空清单处理，损坏则备份后从空清单开始。"""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except IOError:
            return
        except ValueError:
            self._backup_broken()
            return
        if not isinstance(raw, dict):
            return

        archived = raw.get("archive")
        if isinstance(archived, list):
            for record in archived:
                if not isinstance(record, dict) or not isinstance(record.get("item"), dict):
                    continue
                self.archive.append({"item": record["item"],
                                     "archived_at": record.get("archived_at") or time.time(),
                                     "reason": record.get("reason") or "done",
                                     "index": record.get("index")})
        entries = raw.get("items")
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                text = " ".join(str(entry.get("text", "")).split())
                if not text:
                    continue
                try:
                    item_id = int(entry.get("id"))
                except (TypeError, ValueError):
                    item_id = index + 1
                # 新格式有 state（todo / waiting / done）；旧格式只有 done（bool）。
                # 两者都留着并保持一致：只认 done 的老数据 / 老脚本读它也不会看错。
                done = bool(entry.get("done"))
                state = str(entry.get("state") or "")
                if state not in (STATE_TODO, STATE_WAITING, STATE_DONE):
                    state = STATE_DONE if done else STATE_TODO
                try:
                    priority = int(entry.get("priority") or 0)
                except (TypeError, ValueError):
                    priority = PRIORITY_NONE
                self.items.append({
                    "id": item_id,
                    "text": text,
                    "steps": self._clean_steps(entry.get("steps")),
                    "tags": self.tags_of(entry),
                    "done_at": self._load_done_at(entry, state),
                    "done": state == STATE_DONE,
                    "state": state,
                    "priority": priority if priority in (PRIORITY_NONE, PRIORITY_MID,
                                                        PRIORITY_HIGH) else PRIORITY_NONE,
                    "today": bool(entry.get("today")),
                    "created": float(entry.get("created") or time.time()),
                    "remind_at": self._load_remind_at(entry),
                })
        if self.items:
            self._next_id = max(item["id"] for item in self.items) + 1
            self.resort()                     # 默认排序：未完成 → 等待中 → 已完成

        ui = raw.get("ui")
        if isinstance(ui, dict):
            for key in ("x", "y"):
                value = ui.get(key)
                if isinstance(value, (int, float)):
                    self.ui[key] = int(value)
            if isinstance(ui.get("collapsed"), bool):
                self.ui["collapsed"] = ui["collapsed"]
            if isinstance(ui.get("topmost"), bool):
                self.ui["topmost"] = ui["topmost"]
            hotkey = ui.get("hotkey")
            if isinstance(hotkey, str) and parse_hotkey(hotkey) is not None:
                self.ui["hotkey"] = hotkey
            for key in ("width", "height"):
                value = ui.get(key)
                if isinstance(value, (int, float)) and 80 <= float(value) <= 10000:
                    self.ui[key] = int(value)
            alpha = ui.get("alpha")
            if isinstance(alpha, (int, float)) and 0.3 <= float(alpha) <= 1.0:
                self.ui["alpha"] = float(alpha)

    def _backup_broken(self):
        try:
            shutil.copy2(self.path, self.path + ".broken")
        except IOError:
            pass

    def save(self):
        """原子写入；磁盘不可写时静默跳过，不影响使用。"""
        directory = os.path.dirname(self.path)
        try:
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            temp = self.path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": self.items,
                           "archive": self.archive, "ui": self.ui},
                          handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        except (IOError, OSError):
            pass

    # ---- 条目操作 ----
    def add(self, text):
        """新增一条（插到最前）；空文本返回 None。"""
        text, tags = self.parse_tags(text)
        if not text:
            return None
        item = {"id": self._next_id, "text": text, "steps": [], "tags": tags,
                "done": False,
                "state": STATE_TODO, "priority": PRIORITY_NONE, "today": False,
                "done_at": None, "created": time.time(), "remind_at": None}
        self._next_id += 1
        self.items.insert(0, item)
        self.save()
        return item

    def _set_state(self, item, state):
        """统一改状态：state 为准，done 跟着同步，然后落盘。"""
        item["state"] = state
        item["done"] = state == STATE_DONE
        if state == STATE_DONE:
            if not item.get("done_at"):
                item["done_at"] = time.time()      # 记下"什么时候完成的"
            item["remind_at"] = None               # 做完了就别再提醒了
        else:
            item["done_at"] = None                 # 又变回没完成了
        self.save()
        return item

    def toggle(self, item_id):
        """勾选 / 取消勾选：等待中或未完成 → 已完成；已完成 → 未完成。"""
        for item in self.items:
            if item["id"] == item_id:
                state = item.get("state", STATE_DONE if item["done"] else STATE_TODO)
                return self._set_state(
                    item, STATE_TODO if state == STATE_DONE else STATE_DONE)
        return None

    def toggle_waiting(self, item_id):
        """「等待中」开关：未完成/已完成 → 等待中，等待中 → 未完成。"""
        for item in self.items:
            if item["id"] == item_id:
                state = item.get("state", STATE_DONE if item["done"] else STATE_TODO)
                return self._set_state(
                    item, STATE_TODO if state == STATE_WAITING else STATE_WAITING)
        return None

    # ---- 到点提醒 ----
    # 只在本机算时间、只在本机弹：不调用 notify-send、不起子进程（命令被劫持 / 无桌面通知守护时
    # 都会静默失败），因此「无 subprocess」这条安全声明不用改。
    def set_reminder(self, item_id, when):
        """给一条设提醒时间（epoch 秒）；返回条目，找不到返回 None。"""
        for item in self.items:
            if item["id"] == item_id:
                item["remind_at"] = float(when)
                self.save()
                return item
        return None

    def clear_reminder(self, item_id):
        for item in self.items:
            if item["id"] == item_id:
                item["remind_at"] = None
                self.save()
                return item
        return None

    def due_reminders(self, now=None):
        """已经到点、且还没完成的条目（包括程序没开时错过的）。"""
        now = time.time() if now is None else now
        return [item for item in self.items
                if item.get("remind_at") and item["remind_at"] <= now
                and self.state_of(item) != STATE_DONE]

    def reminder_count(self, items=None):
        pool = self.items if items is None else items
        return sum(1 for item in pool
                   if item.get("remind_at") and self.state_of(item) != STATE_DONE)

    @classmethod
    def parse_tags(cls, text):
        """从文字里摘出 `@情境`，返回（去掉标签的正文, 标签列表）。

        规矩（都是为了不误伤普通文字）：`@` 前面必须是空白或开头；后面 1–8 个中英文/数字；
        再后面必须是空白或结尾。所以 `邮箱@qq.com`、`@qq.com` 这种都不会被当标签。
        重复的标签去掉；超过 TAG_MAX 个的留在正文里，不悄悄吃掉。
        """
        tags = []

        def take(match):
            tag = match.group(1)
            if tag in tags:
                return ""
            if len(tags) >= TAG_MAX:
                return match.group(0)
            tags.append(tag)
            return ""

        cleaned = " ".join(TAG_RE.sub(take, " " + str(text) + " ").split())
        return cleaned, tags

    @staticmethod
    def _load_remind_at(entry):
        """读到提醒时间（epoch 秒）；坏数据 / 老数据一律当「没设提醒」。"""
        try:
            value = float(entry.get("remind_at"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def tags_of(item):
        """取标签（兼容老数据）：去重、限个数、限长度。"""
        raw = item.get("tags")
        tags = []
        if isinstance(raw, list):
            for entry in raw:
                tag = " ".join(str(entry).split())[:TAG_LEN]
                if tag and tag not in tags:
                    tags.append(tag)
                if len(tags) >= TAG_MAX:
                    break
        return tags

    def all_tags(self):
        """出现过的全部标签（按出现次数多的在前）。"""
        counts = {}
        for item in self.items:
            for tag in self.tags_of(item):
                counts[tag] = counts.get(tag, 0) + 1
        return [tag for tag, _count in sorted(counts.items(),
                                             key=lambda pair: (-pair[1], pair[0]))]

    @staticmethod
    def _clean_steps(raw):
        """把任意输入收拾成 [{text, done}]：去空行、限条数、text 限长。"""
        steps = []
        if not isinstance(raw, list):
            return steps
        for entry in raw:
            if isinstance(entry, dict):
                text = " ".join(str(entry.get("text", "")).split())
                done = bool(entry.get("done"))
            elif isinstance(entry, str):
                text = " ".join(entry.split())
                done = False
            else:
                continue                      # None / 数字之类一律丢掉，别变成 "None"
            if not text:
                continue
            steps.append({"text": text[:200], "done": done})
            if len(steps) >= STEP_MAX:
                break
        return steps

    def steps_of(self, item):
        """取子步骤（兼容老数据/坏数据）：总是返回一份新的干净列表。"""
        return self._clean_steps(item.get("steps"))

    def steps_progress(self, item):
        """(已完成数, 总数)。"""
        steps = self.steps_of(item)
        return len([step for step in steps if step["done"]]), len(steps)

    def set_steps(self, item_id, steps):
        """整体替换某条的子步骤（编辑器保存时用）。"""
        cleaned = self._clean_steps(steps)
        for item in self.items:
            if item["id"] == item_id:
                item["steps"] = cleaned
                self.save()
                return item
        return None

    def add_step(self, item_id, text="新步骤"):
        """在末尾追加一条子步骤（右键菜单用）；受 STEP_MAX 约束。"""
        for item in self.items:
            if item["id"] == item_id:
                steps = self.steps_of(item)
                if len(steps) >= STEP_MAX:
                    return None
                steps.append({"text": " ".join(str(text).split()) or "新步骤", "done": False})
                item["steps"] = steps
                self.save()
                return item
        return None

    def toggle_step(self, item_id, index):
        """勾选第 index 个子步骤（不动主条目的状态）。"""
        for item in self.items:
            if item["id"] == item_id:
                steps = self.steps_of(item)
                if not isinstance(index, int) or not 0 <= index < len(steps):
                    return None
                steps[index]["done"] = not steps[index]["done"]
                item["steps"] = steps
                self.save()
                return item
        return None

    def priority_of(self, item):
        """取优先级（兼容没有这个字段的老数据）。"""
        value = item.get("priority")
        return value if value in (PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH) \
            else PRIORITY_NONE

    def set_priority(self, item_id, value):
        """直接设优先级（右键菜单用；cycle_priority 保留给自检与将来用）。"""
        if value not in (PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH):
            return None
        for item in self.items:
            if item["id"] == item_id:
                item["priority"] = value
                self.save()
                return item
        return None

    def cycle_priority(self, item_id):
        """点旗子：无 → 高 → 中 → 无。"""
        for item in self.items:
            if item["id"] == item_id:
                current = self.priority_of(item)
                try:
                    index = PRIORITY_CYCLE.index(current)
                except ValueError:
                    index = 0
                item["priority"] = PRIORITY_CYCLE[(index + 1) % len(PRIORITY_CYCLE)]
                self.save()
                return item
        return None

    def toggle_today(self, item_id):
        """标记 / 取消「今天要办的」。"""
        for item in self.items:
            if item["id"] == item_id:
                item["today"] = not bool(item.get("today"))
                self.save()
                return item
        return None

    def today_stats(self):
        """(今天已做完数, 今天标记总数)。"""
        marked = [item for item in self.items if item.get("today")]
        done = len([item for item in marked if self.state_of(item) == STATE_DONE])
        return done, len(marked)

    def state_of(self, item):
        """取条目状态（兼容老数据：只认 done 的条目）。"""
        state = item.get("state")
        if state in (STATE_TODO, STATE_WAITING, STATE_DONE):
            return state
        return STATE_DONE if item.get("done") else STATE_TODO

    def resort(self, save=False):
        """按状态分组排序（未完成 → 等待中 → 已完成），返回顺序是否真的变了。

        用稳定排序：同一组里的相对顺序原样保留，所以「拖拽排出来的优先级」不会被它打乱，
        它只负责把三段归位。列表顺序变了才会写盘。
        """
        rank = dict((state, index) for index, state in enumerate(STATE_ORDER))

        def sort_key(pair):
            index, item = pair
            state = self.state_of(item)
            state_rank = rank.get(state, len(STATE_ORDER))
            # 段内：先「今天要办的」（已完成的不参与置顶），再优先级，最后是原来的手动顺序
            today_rank = 0 if (item.get("today") and state != STATE_DONE) else 1
            priority_rank = -self.priority_of(item)
            return (state_rank, today_rank, priority_rank, index)

        before = [item["id"] for item in self.items]
        order = sorted(enumerate(self.items), key=sort_key)
        self.items = [item for _, item in order]
        changed = [item["id"] for item in self.items] != before
        if changed and save:
            self.save()
        return changed

    def move(self, item_id, index, save=True):
        """把某条挪到列表的第 index 个位置（index 按移动前的列表计算）。

        拖拽过程中先 ``save=False`` 只改内存顺序，松手时再落盘。
        """
        item = None
        for candidate in self.items:
            if candidate["id"] == item_id:
                item = candidate
                break
        if item is None:
            return False
        current = self.items.index(item)
        index = max(0, min(int(index), len(self.items) - 1))
        if index == current:
            return False
        self.items.pop(current)
        self.items.insert(index, item)
        if save:
            self.save()
        return True

    def remove(self, item_id):
        before = len(self.items)
        self.items = [item for item in self.items if item["id"] != item_id]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def rename(self, item_id, text):
        """改条目文字（压掉首尾与多余空白）；空文本不生效。返回是否真的改了。"""
        cleaned, tags = self.parse_tags(text)
        if not cleaned:
            return False
        for item in self.items:
            if item["id"] == item_id:
                if item["text"] == cleaned and self.tags_of(item) == tags:
                    return False
                item["text"] = cleaned
                item["tags"] = tags
                self.save()
                return True
        return False

    def pop(self, item_id):
        """移除条目，并把 (条目, 原位置) 交出来供撤销用；没找到返回 None。"""
        for index, item in enumerate(self.items):
            if item["id"] == item_id:
                self.items.pop(index)
                self._archive_item(item, "deleted", index=index)   # 先归档，撤销=从归档里拿回来
                self.save()
                return {"item": item, "index": index}
        return None

    def restore(self, record):
        """把 pop() 出来的条目放回原位置（位置越界就夹到合法范围）。"""
        if not record or not record.get("item"):
            return False
        item = record["item"]
        for existing in self.items:
            if existing["id"] == item["id"]:
                return False              # 已经在列表里，别插重复
        index = max(0, min(int(record.get("index") or 0), len(self.items)))
        self.items.insert(index, item)
        self._unarchive(item["id"])
        self.save()
        return True

    @staticmethod
    def day_start(now=None):
        """今天 00:00 的时间戳（本地时区）。"""
        now = time.time() if now is None else now
        parts = time.localtime(now)[:3]
        # mktime 要 9 元组：(年,月,日,时,分,秒,周几,一年第几天,是否夏令时)
        return time.mktime(tuple(parts) + (0, 0, 0, 0, 0, -1))

    @staticmethod
    def _load_done_at(entry, state):
        """完成时间：老数据没这个字段就按"现在"算 —— 免得一升级就被自动归档掉。"""
        if state != STATE_DONE:
            return None
        try:
            value = float(entry.get("done_at") or 0)
        except (TypeError, ValueError):
            value = 0.0
        return value if value > 0 else time.time()

    def _archive_item(self, item, reason, now=None, index=None):
        """把一条从活动列表挪进归档（不丢数据）。

        `index` = 它被移走时在列表里的位置（只有删除拿得到）。归档浏览器恢复这一条时
        会尽量放回原位；其它归档原因没有位置，就回到列表开头（随后按状态归位）。
        """
        self.archive.append({"item": item,
                             "archived_at": time.time() if now is None else now,
                             "reason": reason,
                             "index": index})
        self._trim_archive()

    def _trim_archive(self):
        """归档只留最近 ARCHIVE_MAX 条（按归档时间）。"""
        if len(self.archive) > ARCHIVE_MAX:
            self.archive.sort(key=lambda record: record.get("archived_at") or 0)
            self.archive = self.archive[-ARCHIVE_MAX:]

    def _unarchive(self, item_id):
        """把某条从归档里摘掉（撤销删除 / 恢复时用）。"""
        before = len(self.archive)
        self.archive = [record for record in self.archive
                        if record.get("item", {}).get("id") != item_id]
        return len(self.archive) != before

    def archive_completed(self, now=None):
        """把「今天 00:00 之前完成」的条目挪进归档；返回挪了几条。

        今天完成的还留着（当天能看到），过了 00:00 再看就不显示了。
        """
        now = time.time() if now is None else now
        boundary = self.day_start(now)
        keep, moved = [], []
        for item in self.items:
            done_at = item.get("done_at")
            if item.get("state") == STATE_DONE and done_at and done_at < boundary:
                moved.append(item)
            else:
                keep.append(item)
        if not moved:
            return 0
        for item in moved:
            self._archive_item(item, "done", now)
        self.items = keep
        self.save()
        return len(moved)

    def archive_stats(self):
        """归档条数（按原因分开数）。"""
        counts = {"done": 0, "cleared": 0, "deleted": 0}
        for record in self.archive:
            counts[record.get("reason") or "done"] = \
                counts.get(record.get("reason") or "done", 0) + 1
        return len(self.archive), counts

    def clear_done(self):
        before = len(self.items)
        moved = [item for item in self.items if item["done"]]
        self.items = [item for item in self.items if not item["done"]]
        for item in moved:
            self._archive_item(item, "cleared")      # 归档，不再真删
        if len(self.items) != before:
            self.save()
            return before - len(self.items)
        return 0

    def remaining(self, items=None):
        """还没完成的条数（等待中算没完成 —— 事情确实还没了）。items 给子集时就只数子集。"""
        items = self.items if items is None else items
        return len([item for item in items if self.state_of(item) != STATE_DONE])

    def waiting_count(self, items=None):
        items = self.items if items is None else items
        return len([item for item in items if self.state_of(item) == STATE_WAITING])

    def has_done(self):
        return any(item["done"] for item in self.items)


# --------------------------------------------------------------------------- #
# 界面层
# --------------------------------------------------------------------------- #
class TodoWidget(object):
    """桌面待办面板（无边框、默认不置顶；条目可拖拽排序）。"""

    REGROUP_DELAY_MS = 1200       # 状态变了多久之后把它归位（留出双击改名的窗口）

    def _apply_metrics(self, width=None):
        """按给定宽度（缺省用设计宽度）重算所有跟尺寸有关的量。

        正常启动算出来的数与老实现逐像素一致（这些值都能被自检卡住）；
        窗口被拉大之后才跟着变宽 —— 换行宽度、右侧图标位置都会跟着变。
        """
        scale = self.s
        panel_w = _scaled(PANEL_W, scale)
        if width:
            panel_w = max(panel_w, int(width))
        self.panel_w = panel_w
        self.pad = _scaled(PAD, scale)
        self.list_w = max(1, self.panel_w - 2 * self.pad - 2)
        self.list_max_h = _scaled(LIST_MAX_H, scale)
        self.text_x = _scaled(TEXT_X, scale)
        self.del_w = _scaled(DEL_W, scale)
        self.wait_w = _scaled(WAIT_W, scale)
        self.today_w = _scaled(TODAY_W, scale)
        self.prio_w = _scaled(PRIO_W, scale)
        self.steps_w = _scaled(STEPS_W, scale)
        self.step_box = _scaled(STEP_BOX, scale)
        self.step_indent = _scaled(STEP_INDENT, scale)
        # 右侧按钮：从右往左排（区名, 宽度）。绘制、点击判定、折行宽度共用这一张表
        # 右侧只留两个：子步骤（≡ / 2-3）与删除。等待中/今天/优先级搬进右键菜单
        self.markers = (("delete", self.del_w), ("steps", self.steps_w))
        self.marker_w = sum(width for _, width in self.markers)
        self.row_pad_y = _scaled(ROW_PAD_Y, scale)
        self.row_wrap = max(_scaled(40, scale),
                            self.list_w - self.text_x - self.marker_w - _scaled(4, scale))
        self.header_h = _scaled(34, scale)
        self.icon_box = _scaled(20, scale)
        self.check_box = _scaled(16, scale)

    def _list_space(self, height=None):
        """当前窗口高度里最多能给列表多少像素（多出来的高度就多显示几条）。"""
        try:
            if height is None:
                height = self.root.winfo_height()
            # 卡片高度减去列表高度 = 标题 + 输入框 + 底部 + 间距
            chrome = self.card.winfo_reqheight() - self.list_area.winfo_reqheight()
        except (tk.TclError, AttributeError):
            return _scaled(LIST_MAX_H, self.s)
        return max(0, int(height) - int(chrome) - self.px(4))

    def _on_root_configure(self, event):
        """窗口被拉大 / 拉高（WM 边缘拖拽）：防抖之后按新尺寸重排。"""
        if event.widget is not self.root:
            return
        if self._size_job is not None:
            try:
                self.root.after_cancel(self._size_job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._size_job = self.root.after(160, self._apply_resize)
        except tk.TclError:
            self._size_job = None

    def _apply_resize(self):
        """按窗口现在的尺寸重排列宽与列表高度；没变化就不重画（避免来回震荡）。"""
        self._size_job = None
        if self._steps_edit is not None:
            # 子步骤编辑器会临时把这一行（和窗口）撑高；那是它自己的动作，
            # 不是用户改了窗口大小。这里若照常处理会触发 _refresh()，把编辑框连内容一起销毁。
            return
        try:
            width, height = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        if width <= 1 or height <= 1:
            return
        before = (self.panel_w, self.list_max_h)
        self._apply_metrics(width)
        space = self._list_space(height)
        if space > self.list_max_h:
            self.list_max_h = space
        if (self.panel_w, self.list_max_h) == before:
            return
        self._user_sized = True
        self.store.ui["width"] = self.panel_w
        self.store.ui["height"] = height
        self.store.save()
        self._refresh()

    def _apply_saved_size(self):
        """启动时把上次的窗口尺寸装回来（宽度影响排版，高度决定列表能显示几条）。"""
        width = self.store.ui.get("width")
        height = self.store.ui.get("height")
        if not width and not height:
            return
        self._apply_metrics(width)
        if height:
            self.list_max_h = max(self.list_max_h, self._list_space(height))
        self._user_sized = True
        try:                              # 把上次的尺寸真正装回窗口（宽 + 高）
            self.root.geometry("%dx%d" % (self.panel_w, int(height or self.root.winfo_height())))
        except tk.TclError:
            pass

    def __init__(self, root, store, visible=True):
        self.root = root
        self.store = store
        # 高 DPI：字号由 Tk 按「点」自动放大，窗口尺寸得按同一比例自己放大
        self.s = ui_scale(root)
        # 尺寸相关的量都走 _apply_metrics()：窗口被拉大/拉高之后要能跟着重排
        self.panel_w = self.pad = self.list_w = self.list_max_h = 0
        self.text_x = self.del_w = self.wait_w = self.row_pad_y = self.row_wrap = 0
        self.header_h = self.icon_box = self.check_box = 0
        self._size_job = None             # 尺寸变化的重排任务（防抖）
        self._user_sized = False          # 用户手动改过大小：之后不再把它缩回去
        self._apply_metrics(store.ui.get("width"))
        self.fonts = self._make_fonts()
        self._drag_offset = None          # 拖窗口的偏移
        self._placeholder_on = False
        # 条目拖拽排序状态
        self.row_widgets = []             # [(item_id, row_canvas)]，按显示顺序
        self._press = None                # 按下时记录 {id, y_root, zone}
        self._drag_id = None              # 正在被拖动的条目 id
        self._pointer_y = None            # 拖动中指针的屏幕 y（用于贴边自动滚动）
        self._autoscroll_job = None
        # 撤销与编辑状态
        self._undo = None                 # 刚删掉的 {item, index}，供撤销放回原位
        self._undo_job = None             # 撤销时限的 after 句柄
        self._edit_id = None              # 正在编辑的条目 id
        self._edit_entry = None           # 正在编辑的输入框（盖在那一行上）
        self._steps_edit = None           # 正在编辑子步骤的 ([每步一个小输入框], 条目 id)
        self._steps_edit_done = []        # 编辑中的完成标记（跟小输入框一一对应）
        self._steps_edit_geom = []        # 编辑中每个输入框的 (y, 高度, 小方框中心 y)
        self._steps_edit_base_h = 0       # 打开编辑器前那一行的行高
        self._steps_edit_active = None    # 当前聚焦第几个子步骤输入框
        self._steps_edit_win = None       # 打开编辑器前的窗口尺寸（编辑时临时撑高，收掉要还原）
        self._steps_edit_grew = False     # 这次编辑有没有把这一行 / 窗口撑高
        self._last_toggle = (None, 0.0)   # 最近一次勾选（条目 id, 时间），供双击回退
        self._regroup_job = None          # 归位（按状态分组排序）的待执行任务
        self._archive_window = None       # 归档浏览器（开着就复用，不重复开窗）
        self._alert = None                # 正在显示的到点提醒（条目列表）
        self._hint_job = None             # 临时提示（设/取消提醒后 3 秒恢复常规提示）
        self._remind_job = None           # 到点提醒的周期检查句柄
        self._build()
        self._restore_position()
        self._apply_collapsed()
        self._refresh(keep_scroll=False)
        if visible:
            self.root.deiconify()
            self.root.update_idletasks()
            self._restore_position()      # 映射后再落一次，避免 WM 摆放时挪位
            single_instance_start(self.root, self.raise_window)
            set_skip_taskbar(self.root)   # 映射后再补一次（KWin 只对已托管的窗口处理）
            # 全局快捷键：窗口不进任务栏，这是最可靠的唤回入口
            self._hotkey_spec = self.store.ui.get("hotkey") or HOTKEY_DEFAULT
            set_diag_path(os.path.dirname(self.store.path))
            self._hotkey_on = hotkey_install(
                self.root, self._hotkey_spec, self.raise_window)
            # 先补报「关机/web 没开期间错过」的提醒，再挂上 15 秒一次的周期检查
            self._remind_tick(raise_it=False)

    # ---- 单实例 / 唤回 ----
    def raise_window(self, timestamp=None):
        """把窗口提到最前面并拿到焦点（不改变用户设置的「始终置顶」状态）。

        `timestamp` 由全局快捷键那一路传进来（KeyPress 事件的时间），EWMH 激活要靠它
        过 KWin 的防抢焦点；「再跑一次」那一路没有事件，留空即可。

        单实例监听是 unix 套接字 + Tk 文件事件（见 single_instance_start），
        唤回动作就是下面的 activate_window()。
        """
        activate_window(self.root, self.store.ui["topmost"], timestamp=timestamp)

    def toggle_hidden(self):
        """隐藏 / 显示窗口：隐藏之后按全局快捷键唤回（免得和「退出」混在一起）。"""
        try:
            viewable = bool(self.root.winfo_viewable())
            state = ""
            try:
                state = self.root.state()
            except tk.TclError:
                pass
            write_diag("hotkey viewable=%s wm_state=%s -> %s"
                           % (viewable, state, "withdraw" if viewable else "activate"))
            if viewable:
                self.root.withdraw()
            else:
                self.raise_window()
        except tk.TclError as error:
            write_diag("hotkey 失败: %r" % (error,))

    def open_archive(self):
        """页头右键 → 查看归档…：打开归档浏览器；已经开着就提到最前，不重复开窗。"""
        existing = self._archive_window
        if existing is not None:
            try:
                if existing.win.winfo_exists():
                    existing.show()
                    return existing
            except tk.TclError:
                pass
        self._archive_window = ArchiveWindow(self, visible=True)
        return self._archive_window

    # ---- 字体 ----
    def _make_fonts(self):
        available = set(tkfont.families())
        family = None
        for name in FONT_STACK:
            if name in available:
                family = name
                break
        base = tkfont.Font(family=family, size=10) if family else tkfont.Font(size=10)
        actual = base.cget("family")
        return {
            "base": base,
            "bold": tkfont.Font(family=actual, size=10, weight="bold"),
            "small": tkfont.Font(family=actual, size=9),
            "done": tkfont.Font(family=actual, size=10, overstrike=1),
            "marker": tkfont.Font(family=actual, size=8),
            "small_done": tkfont.Font(family=actual, size=9, overstrike=1),
        }

    # ---- 组装 ----
    def _build(self):
        root = self.root
        root.title("桌面待办清单")
        # 窗口交给系统托管（这样点击输入框才能拿到键盘焦点），去标题栏用 Motif 提示、
        # 「不进任务栏」用 EWMH 的 SKIP_TASKBAR + SKIP_PAGER（见 _on_wm_window 一节）。
        # 被别的窗口盖住后，按全局快捷键、Alt+Tab 或「再运行一次本程序」都能叫回最前面。
        root.withdraw()                          # 先不映射，提示才能在首次建图前生效
        root.attributes("-topmost", bool(self.store.ui["topmost"]))
        root.attributes("-alpha", float(self.store.ui["alpha"]))
        root.update_idletasks()                  # 促使 Tk 先建出 toplevel 的窗口
        strip_window_decorations(root)
        set_skip_taskbar(root)                   # 任务栏 / dock 里不出现这个图标
        root.configure(bg=C_BORDER)              # 卡片外层留出 1px 边框色
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.card = tk.Frame(root, bg=C_CARD, bd=0, highlightthickness=0)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()
        self.filter_tag = None            # 顶部情境筛选：None = 看全部
        # 唤回诊断日志写在"当前数据文件旁边"（自检跑临时目录时就不会碰用户真实数据）
        set_diag_path(os.path.dirname(self.store.path))
        self._build_input()
        self._build_tags()
        self._build_list()
        self._build_footer()
        self.menu = self._build_menu()
        self._row_menu = None                  # 当前弹出的条目右键菜单
        # 只挂左键：右键那一下正是"要弹菜单"的动作，若也挂上全局收起，
        # 就是"先弹出来、紧接着被同一事件的全局处理收掉"——实测菜单根本弹不出来。
        # （右键点别的条目由 _popup_row_menu 自己先收起上一个。）
        self.root.bind_all("<Button-1>", self._dismiss_row_menu, add="+")
        self._archived_day = None
        self._archive_on_start()

    def _build_header(self):
        self.header = head = tk.Frame(self.card, bg=C_CARD, height=self.header_h)
        head.pack(fill="x", side="top")
        head.pack_propagate(False)

        dot_size = self.px(10)
        dot = tk.Canvas(head, width=dot_size, height=dot_size, bg=C_CARD,
                        highlightthickness=0)
        dot.create_oval(self.px(2), self.px(2), self.px(8), self.px(8),
                        fill=C_ACCENT, outline="")
        dot.pack(side="left", padx=(self.pad, self.px(6)))

        self.title = tk.Label(head, text="待办事项", bg=C_CARD, fg=C_TEXT, font=self.fonts["bold"])
        self.title.pack(side="left")

        self.count = tk.Label(head, text="", bg=C_CARD, fg=C_DIM, font=self.fonts["small"])
        self.count.pack(side="left", padx=(self.px(8), 0))

        close_btn = self._icon_button(head, "close", self.quit)
        close_btn.pack(side="right", padx=(0, max(0, self.pad - self.px(6))))
        self.collapse_btn = self._icon_button(head, "collapse", self.toggle_collapsed)
        self.collapse_btn.pack(side="right")

        for widget in (head, dot, self.title, self.count):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._popup_menu)
        head.bind("<Double-Button-1>", lambda event: self.toggle_collapsed())

    def _build_tags(self):
        """顶部情境筛选条：只在有标签时才长出内容（没标签时是个空框，高度≈0）。"""
        self.tags_row = tk.Frame(self.card, bg=C_CARD)
        self.tags_row.pack(fill="x", padx=self.pad, pady=(0, self.px(2)))

    def _refresh_tags(self):
        for child in list(self.tags_row.winfo_children()):
            child.destroy()
        tags = self.store.all_tags()
        if not tags:
            self.filter_tag = None
            return
        self._tag_chip("全部", None)
        for tag in tags:
            self._tag_chip("@" + tag, tag)

    def _tag_chip(self, label, tag):
        active = (self.filter_tag == tag)
        chip = tk.Label(self.tags_row, text=label, bg=C_LIST,
                        fg=C_ACCENT if active else C_DIM,
                        font=self.fonts["small"], padx=self.px(6), pady=self.px(1))
        chip.pack(side="left", padx=(0, self.px(4)))
        if active:
            chip.configure(highlightbackground=C_ACCENT, highlightthickness=1)
        chip.bind("<Button-1>", lambda event, t=tag: self.set_filter(t))
        chip.bind("<Enter>", lambda event: chip.configure(fg=C_ACCENT))
        chip.bind("<Leave>", lambda event: chip.configure(
            fg=C_ACCENT if active else C_DIM))
        return chip

    def set_filter(self, tag):
        """切换「只看某个情境」；tag=None 表示看全部。"""
        self.filter_tag = tag
        self._refresh_tags()
        self._refresh(keep_scroll=False)
        self._update_counts()
        self._update_footer()

    def _visible_items(self):
        if not self.filter_tag:
            return list(self.store.items)
        return [item for item in self.store.items
                if self.filter_tag in self.store.tags_of(item)]

    def _build_input(self):
        self.input_row = row = tk.Frame(self.card, bg=C_CARD)

        box = tk.Frame(row, bg=C_LIST, highlightbackground=C_BORDER, highlightthickness=1)
        box.pack(side="left", fill="x", expand=True)

        self.entry = tk.Entry(box, bg=C_LIST, fg=C_TEXT, insertbackground=C_TEXT,
                              relief="flat", bd=0, font=self.fonts["base"], highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=self.px(8), pady=self.px(5))
        self.entry.bind("<Return>", lambda event: self.add_from_entry())
        self.entry.bind("<Escape>", lambda event: self.entry.delete(0, "end"))
        self.entry.bind("<FocusIn>", lambda event: self._clear_placeholder())
        self.entry.bind("<FocusOut>", lambda event: self._set_placeholder())
        self.entry.bind("<Button-1>", self._focus_entry)
        self._set_placeholder()

        add_btn = tk.Label(row, text="+", bg=C_ACCENT, fg="#ffffff", font=self.fonts["bold"],
                           width=2, cursor="hand2")
        add_btn.pack(side="left", padx=(self.px(6), 0), ipady=self.px(5))
        add_btn.bind("<Button-1>", lambda event: self.add_from_entry())
        add_btn.bind("<Enter>", lambda event: add_btn.configure(bg="#5aa6ff"))
        add_btn.bind("<Leave>", lambda event: add_btn.configure(bg=C_ACCENT))

    def _build_list(self):
        self.list_area = tk.Frame(self.card, bg=C_LIST)
        self.canvas = tk.Canvas(self.list_area, bg=C_LIST, highlightthickness=0, bd=0,
                                width=self.list_w)
        self.canvas.pack(fill="x", expand=True)
        self.inner = tk.Frame(self.canvas, bg=C_LIST)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.root.bind("<Configure>", self._on_root_configure)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def _build_footer(self):
        self.footer = foot = tk.Frame(self.card, bg=C_CARD)
        self.hint = tk.Label(foot, text="双击标题可折叠", bg=C_CARD, fg=C_MUTED,
                             font=self.fonts["small"], anchor="w")
        self.hint.pack(side="left")
        # 「撤销」只在刚删掉一条时出现（见 _show_undo / _clear_undo），平时不占位置
        self.undo_btn = tk.Label(foot, text="撤销删除", bg=C_CARD, fg=C_ACCENT,
                                 font=self.fonts["small"], cursor="hand2")
        self.undo_btn.bind("<Button-1>", lambda event: self.undo_delete())
        self.undo_btn.bind("<Enter>", lambda event: self.undo_btn.configure(fg=C_TEXT))
        self.undo_btn.bind("<Leave>", lambda event: self.undo_btn.configure(fg=C_ACCENT))
        # 「知道了」只在到点提醒弹出时出现（见 _show_alert / _dismiss_alert）
        self.alert_btn = tk.Label(foot, text="知道了", bg=C_CARD, fg=C_WAIT,
                                  font=self.fonts["small"], cursor="hand2")
        self.alert_btn.bind("<Button-1>", lambda event: self._dismiss_alert())
        self.alert_btn.bind("<Enter>", lambda event: self.alert_btn.configure(fg=C_TEXT))
        self.alert_btn.bind("<Leave>", lambda event: self.alert_btn.configure(fg=C_WAIT))
        self.clear_btn = tk.Label(foot, text="归档已完成", bg=C_CARD, fg=C_DIM,
                                  font=self.fonts["small"], cursor="hand2")
        self.clear_btn.bind("<Button-1>", lambda event: self.clear_done())
        self.clear_btn.bind("<Enter>", lambda event: self.clear_btn.configure(fg=C_TEXT))
        self.clear_btn.bind("<Leave>", lambda event: self.clear_btn.configure(fg=C_DIM))

    def _build_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="折叠 / 展开", command=self.toggle_collapsed)
        menu.add_command(label="隐藏窗口（快捷键唤回）", command=self.toggle_hidden)
        self._topmost_var = tk.BooleanVar(value=bool(self.store.ui["topmost"]))
        menu.add_checkbutton(label="始终置顶", variable=self._topmost_var, command=self._apply_topmost)
        # 装的是 .deb 时，用户根本不会去终端敲 --install-autostart；这里给它一个开关
        self._autostart_var = tk.BooleanVar(value=os.path.exists(autostart_path()))
        menu.add_checkbutton(label="开机自启", variable=self._autostart_var,
                             command=self._toggle_autostart)
        self._alpha_var = tk.DoubleVar(value=float(self.store.ui["alpha"]))
        alpha_menu = tk.Menu(menu, tearoff=0)
        for label, value in (("完全不透明", 1.0), ("半透明 92%", 0.92),
                             ("更透明 82%", 0.82), ("最透明 70%", 0.70)):
            alpha_menu.add_radiobutton(label=label, variable=self._alpha_var,
                                       value=value, command=self._apply_alpha)
        menu.add_cascade(label="窗口透明度", menu=alpha_menu)
        menu.add_separator()
        menu.add_command(label="查看归档…", command=self.open_archive)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        return menu

    # ---- 小控件 ----
    def px(self, value):
        """基准像素值 → 当前缩放下的像素值（缩放 1.0 时原样返回）。"""
        return _scaled(value, self.s)

    def _icon_button(self, parent, kind, command):
        canvas = tk.Canvas(parent, width=self.icon_box, height=self.icon_box, bg=C_CARD,
                           highlightthickness=0, cursor="hand2")
        canvas._kind = kind
        self._draw_icon(canvas, kind, C_DIM)
        canvas.bind("<Button-1>", lambda event: command())
        canvas.bind("<Enter>", lambda event: self._draw_icon(canvas, kind, C_TEXT))
        canvas.bind("<Leave>", lambda event: self._draw_icon(canvas, kind, C_DIM))
        return canvas

    def _draw_icon(self, canvas, kind, color):
        canvas.delete("all")
        line_w = 1.6 * self.s
        if kind == "close":
            canvas.create_line(self.px(6), self.px(6), self.px(14), self.px(14),
                               fill=color, width=line_w, capstyle="round")
            canvas.create_line(self.px(14), self.px(6), self.px(6), self.px(14),
                               fill=color, width=line_w, capstyle="round")
        else:  # 折叠箭头：展开时朝下，折叠时朝右
            if self.store.ui["collapsed"]:
                canvas.create_line(self.px(8), self.px(5), self.px(13), self.px(10),
                                   self.px(8), self.px(15), fill=color, width=line_w,
                                   capstyle="round", joinstyle="round")
            else:
                canvas.create_line(self.px(5), self.px(8), self.px(10), self.px(13),
                                   self.px(15), self.px(8), fill=color, width=line_w,
                                   capstyle="round", joinstyle="round")

    def _draw_check(self, canvas, done, top=0):
        """在 canvas 上画勾选框，top 为垂直偏移（尺寸按界面缩放）。"""
        canvas.delete("check")
        y = top
        box = self.check_box
        if done:
            canvas.create_rectangle(self.px(6), y, self.px(22), y + box, fill=C_ACCENT,
                                    outline="", tags="check")
            canvas.create_line(self.px(10), y + self.px(8), self.px(13), y + self.px(11.5),
                               self.px(18.5), y + self.px(4.5), fill="#ffffff",
                               width=2 * self.s, capstyle="round", joinstyle="round",
                               tags="check")
        else:
            canvas.create_rectangle(self.px(6.5), y + 0.5, self.px(21.5), y + box - 0.5,
                                    outline=C_BORDER, width=1.4 * self.s, tags="check")

    def _bind_wheel(self, widget):
        widget.bind("<Button-4>", self._on_wheel)
        widget.bind("<Button-5>", self._on_wheel)
        widget.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        if self.inner.winfo_reqheight() <= self.list_max_h:
            return "break"
        up = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
        self.canvas.yview_scroll(-2 if up else 2, "units")
        return "break"

    # ---- 输入框占位文字 ----
    def _set_placeholder(self):
        if not self.entry.get():
            self._placeholder_on = True
            self.entry.insert(0, PLACEHOLDER)
            self.entry.configure(fg=C_MUTED)

    def _clear_placeholder(self):
        """清掉占位文字，只删占位本身——用户已经敲进去的字一个都不能丢。"""
        if not self._placeholder_on:
            return
        self._placeholder_on = False
        text = self.entry.get()
        if text != PLACEHOLDER and PLACEHOLDER in text:
            self.entry.delete(0, "end")
            self.entry.insert(0, text.replace(PLACEHOLDER, ""))
        elif text == PLACEHOLDER:
            self.entry.delete(0, "end")
        self.entry.configure(fg=C_TEXT)

    def _focus_entry(self, event=None):
        if self._placeholder_on:
            # 第一次点进来：先删掉提示文字并吞掉这次点击，光标落到文字末尾，
            # 否则默认行为会把插入点放进提示文字中间，打字就打进提示里了。
            self._clear_placeholder()
            self.entry.icursor("end")
            self.entry.focus_force()      # 同步拿焦点，紧接着的第一次按键不能丢
            return "break"
        self.entry.focus_force()

    # ---- 列表刷新 ----
    def _refresh(self, keep_scroll=True):
        if self._steps_edit is not None:
            # 子步骤编辑器还开着就整表重建，会把正在输入的内容一起吞掉（它会重建行控件）。
            # 先按"保存"把它收掉；_end_steps_edit 自己会再调一次 _refresh。
            self._end_steps_edit(save=True)
            return
        try:
            position = self.canvas.yview()[0] if keep_scroll else 0.0
        except tk.TclError:
            position = 0.0

        for child in list(self.inner.winfo_children()):
            child.destroy()
        # 行会被重建，正在编辑的输入框随之销毁，别留悬空引用
        self._edit_id = None
        self._edit_entry = None
        self._steps_edit = None
        self._steps_edit_done = []
        self._steps_edit_geom = []
        self._steps_edit_base_h = 0
        self._steps_edit_active = None
        self._steps_edit_win = None
        self._steps_edit_grew = False
        self.row_widgets = []
        self._press = None
        self._drag_id = None
        self._pointer_y = None
        self._stop_autoscroll()

        visible = self._visible_items()
        if not visible:
            empty = tk.Label(self.inner,
                             text=("没有 @%s 的条目" % self.filter_tag) if self.filter_tag
                             else "暂无待办，在上面输入框添加第一条",
                             bg=C_LIST, fg=C_MUTED, font=self.fonts["small"])
            empty.pack(fill="x", pady=self.px(20))
            self._bind_wheel(empty)
        else:
            for item in visible:
                self._make_row(item)

        self._update_counts()
        self._update_footer()
        self.root.update_idletasks()
        self._resize()
        if self.inner.winfo_reqheight() > self.list_max_h:
            self.canvas.yview_moveto(position)
        else:
            self.canvas.yview_moveto(0.0)

    def _wrap_text(self, text, font, limit):
        """按像素宽度折行（Tk 的 Label 折行在本机渲染不齐，这里自己算）。"""
        lines = []
        for paragraph in str(text).split("\n"):
            current = ""
            for char in paragraph:
                if current and font.measure(current + char) > limit:
                    lines.append(current.rstrip())
                    current = "" if char == " " else char
                else:
                    current += char
            lines.append(current.rstrip())
        return [line for line in lines if line] or [""]

    def _draw_row(self, canvas, item):
        """把一条待办画进行画布（新建与原地重画共用）。

        视觉上的几个维度：
          状态：未完成＝白字空方框；等待中＝琥珀字 + 琥珀小钟；已完成＝灰字删除线 + 蓝勾；
          优先级：右侧旗子（实心橙＝高、空心橙＝中、淡灰＝无）；
          今天：右侧「今」（蓝色＝今天要办的）；
          删除：最右边那个 ×。

        返回 (删除按钮的图元 id, 行高)；行高变化时会同步 canvas 的 height。
        """
        canvas.delete("all")
        state = self.store.state_of(item)
        done = state == STATE_DONE
        canvas._waiting = state == STATE_WAITING
        canvas._prio = self.store.priority_of(item)
        canvas._today = bool(item.get("today"))
        font = self.fonts["done"] if done else self.fonts["base"]
        line_h = font.metrics("linespace")
        steps = self.store.steps_of(item)
        done_steps, total_steps = self.store.steps_progress(item)
        tags = self.store.tags_of(item)
        tag_text = " ".join("@" + tag for tag in tags)
        tag_font = self.fonts["marker"]
        tag_w = (tag_font.measure(tag_text) + self.px(6)) if tag_text else 0
        lines = self._wrap_text(item["text"], font,
                               max(self.px(40), self.row_wrap - tag_w))
        step_h = self.fonts["small"].metrics("linespace") + self.px(2)
        step_wrap = max(self.px(40), self.row_wrap - self.step_indent)
        step_rows = 0
        for step in steps:
            step_font = self.fonts["small_done"] if step["done"] else self.fonts["small"]
            step_rows += len(self._wrap_text(step["text"], step_font, step_wrap))
        main_h = line_h * len(lines)
        row_h = main_h + step_h * step_rows + 2 * self.row_pad_y
        if int(canvas.cget("height")) != row_h:
            canvas.configure(height=row_h)

        # 优先级：行最左边一条竖条（高＝整条、中＝中间半条）。不占正文宽度
        if canvas._prio in (PRIORITY_HIGH, PRIORITY_MID):
            bar_w = max(2.0, self.px(3))
            if canvas._prio == PRIORITY_HIGH:
                bar_top, bar_bottom = 0.0, float(row_h)
            else:
                bar_top, bar_bottom = row_h * 0.25, row_h * 0.75
            canvas.create_rectangle(0, bar_top, bar_w, bar_bottom, fill=C_HIGH,
                                    outline="", tags="prio")
        # 今天：正文前面的小蓝点（在勾选框与文字之间的空档里，也不占宽度）
        if canvas._today:
            dot = max(2.0, self.px(3))
            dot_x, dot_y = self.text_x - self.px(8), self.row_pad_y + line_h / 2.0
            canvas.create_oval(dot_x - dot, dot_y - dot, dot_x + dot, dot_y + dot,
                               fill=C_ACCENT, outline="", tags="today")

        # 主勾选框对齐「主文字块」而不是整行：有子步骤时不会跑到中间去
        self._draw_check(canvas, done,
                         top=self.row_pad_y + (main_h - self.check_box) // 2)
        text_color = C_DONE if done else (C_WAIT if canvas._waiting else C_TEXT)
        for index, line in enumerate(lines):
            canvas.create_text(self.text_x, self.row_pad_y + index * line_h, text=line,
                               anchor="nw", fill=text_color, font=font, tags="text")
        canvas._tag_item = None
        if tag_text and lines:
            canvas._tag_item = canvas.create_text(
                self.text_x + font.measure(lines[-1]) + self.px(6),
                self.row_pad_y + (len(lines) - 1) * line_h + line_h / 2.0,
                text=tag_text, anchor="w", fill=C_ACCENT, font=tag_font, tags="tag")
        canvas._step_boxes = self._draw_steps(canvas, steps, self.row_pad_y + main_h, step_h)
        canvas._steps_done, canvas._steps_total = done_steps, total_steps
        canvas._markers = {
            "delete": self._draw_delete_marker(canvas, row_h),
            "steps": self._draw_steps_marker(canvas, row_h, done_steps, total_steps),
        }
        canvas._delete_item = canvas._markers["delete"][0][0]   # 兼容旧引用
        self._paint_markers(canvas, None)
        return canvas._delete_item, row_h

    def _draw_step_check(self, canvas, left, cy, done, tags="step"):
        """在 (left, cy) 画一个子步骤小方框（勾选/未勾选），返回图元 id 列表。

        列表排版和子步骤编辑器共用这一份，两边的小方框长得一模一样。
        """
        box = self.step_box
        bx0, by0 = left, cy - box / 2.0
        bx1, by1 = left + box, cy + box / 2.0
        if done:
            ids = [canvas.create_rectangle(bx0, by0, bx1, by1, fill=C_ACCENT,
                                           outline="", tags=tags)]
            ids.append(canvas.create_line(bx0 + box * 0.22, by0 + box * 0.52,
                                          bx0 + box * 0.44, by0 + box * 0.74,
                                          bx0 + box * 0.80, by0 + box * 0.26,
                                          fill="#ffffff", width=max(1.0, 1.6 * self.s),
                                          capstyle="round", joinstyle="round",
                                          tags=tags))
            return ids
        return [canvas.create_rectangle(bx0, by0, bx1, by1, outline=C_BORDER,
                                        width=max(1.0, 1.2 * self.s), tags=tags)]

    def _draw_steps(self, canvas, steps, top, step_h):
        """在主文字下面逐行画子步骤，返回每行小方框的命中区。

        返回 [(index, y0, y1, x0, x1)]（canvas 坐标）。点小方框 = 勾这个步骤；
        点这一行的其它地方什么也不做 —— 免得误把整条待办标成完成。
        """
        boxes = []
        step_font = self.fonts["small"]
        step_wrap = max(self.px(40), self.row_wrap - self.step_indent)
        step_h = step_h * 1.0
        y = top
        for index, step in enumerate(steps):
            font = self.fonts["small_done"] if step["done"] else step_font
            text_lines = self._wrap_text(step["text"], font, step_wrap)
            block_h = step_h * len(text_lines)
            y0 = y
            cy = y0 + step_h / 2.0
            self._draw_step_check(canvas, self.text_x, cy, step["done"], tags="step")
            for line_index, line in enumerate(text_lines):
                canvas.create_text(self.text_x + self.step_indent,
                                   y0 + line_index * step_h,
                                   text=line, anchor="nw",
                                   fill=C_DONE if step["done"] else C_DIM,
                                   font=font, tags="step")
            bx0 = self.text_x
            bx1 = self.text_x + self.step_box
            boxes.append((index, y0, y0 + block_h,
                          bx0 - self.px(3), bx1 + self.px(4)))
            y += block_h
        canvas._step_top = top
        return boxes

    # ---- 右侧按钮：位置、绘制、上色 ----
    def _marker_center(self, zone, row_h):
        """某个右侧按钮的中心坐标（按钮从右往左排）。"""
        right = self.list_w
        for name, width in self.markers:
            if name == zone:
                return right - width / 2.0, row_h // 2.0 + self.px(1)
            right -= width
        return right, row_h // 2.0 + self.px(1)

    def _marker_zone_start(self):
        """右侧按钮区的左边界（点击落在这右边就不算"点条目本身"）。"""
        return self.list_w - self.marker_w

    def _zone_at(self, x):
        """x 落在哪个右侧按钮上；不在按钮区返回 None。"""
        right = self.list_w
        for name, width in self.markers:
            if x >= right - width:
                return name
            right -= width
        return None

    def _draw_steps_marker(self, canvas, row_h, done, total):
        """「子步骤」按钮：没有子步骤时画 ≡，有子步骤时画 已完成/总数。"""
        cx, cy = self._marker_center("steps", row_h)
        text = ("%d/%d" % (done, total)) if total else "≡"
        text_id = canvas.create_text(cx, cy, text=text, fill=C_MUTED,
                                     font=self.fonts["marker"], tags="marker")
        return ((text_id, "text"),)

    def _draw_delete_marker(self, canvas, row_h):
        """删除按钮：×（原来那个位置和判定都没变）。"""
        cx, cy = self._marker_center("delete", row_h)
        text_id = canvas.create_text(cx, cy, text="×", fill=C_MUTED,
                                     font=self.fonts["bold"], tags="marker")
        return ((text_id, "text"),)

    def _colorize(self, canvas, items, color, filled=False):
        """给一组图元上色：圆环改 outline、三角旗改 outline+fill、其余改 fill。"""
        for item_id, kind in items:
            try:
                if kind == "oval":
                    canvas.itemconfigure(item_id, outline=color)
                elif kind == "poly":
                    canvas.itemconfigure(item_id, outline=color,
                                         fill=color if filled else "")
                else:
                    canvas.itemconfigure(item_id, fill=color)
            except tk.TclError:
                return

    def _paint_markers(self, canvas, hover_zone):
        """给右侧两个按钮上色（子步骤格：无步骤＝≡灰、部分＝暗、全完成＝强调色）。"""
        markers = getattr(canvas, "_markers", None)
        if not markers:
            return
        total = getattr(canvas, "_steps_total", 0)
        done = getattr(canvas, "_steps_done", 0)
        if not total:
            color = C_MUTED
        elif done >= total:
            color = C_ACCENT
        else:
            color = C_DIM
        if hover_zone == "steps":
            color = C_ACCENT
        self._colorize(canvas, markers["steps"], color)
        self._colorize(canvas, markers["delete"],
                       C_DANGER if hover_zone == "delete" else C_MUTED)

    def _redraw_row(self, item_id):
        """只重画这一行（原地更新），不重建控件。

        勾选以前是 store.toggle() 后 _refresh() 整表重建；而 Tk 判定双击要求两次点击
        落在同一个控件上——重建后第二次点击只能算「新控件上的单击」，于是
        <Double-Button-1> 永远不触发，双击改文字在真机上永远进不去。
        改成原地重画之后，行控件活下来，双击才能按文档那样工作。
        """
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return False
        if self._edit_entry is not None and self._edit_id == item_id:
            return False                     # 正在编辑这一行，别把它盖掉
        if self._steps_edit is not None and self._steps_edit[1] == item_id:
            return False                     # 正在编辑子步骤，同理
        before = int(widget.cget("height"))
        self._draw_row(widget, item)
        if int(widget.cget("height")) != before:
            self.root.update_idletasks()
            self._resize()                   # 行高变了才需要重排窗口高度
        return True

    def _make_row(self, item):
        row = tk.Canvas(self.inner, bg=C_LIST, highlightthickness=0, bd=0,
                        width=self.list_w, height=self.px(20), cursor="hand2")
        row.pack(fill="x")
        self._draw_row(row, item)

        item_id = item["id"]

        def on_motion(event):
            self._paint_markers(row, self._zone_at(event.x))

        def on_leave(event):
            self._paint_markers(row, None)

        row.bind("<Button-1>", lambda event, i=item_id: self._row_press(event, i))
        row.bind("<B1-Motion>", self._row_motion)
        row.bind("<ButtonRelease-1>", self._row_release)
        row.bind("<Double-Button-1>", lambda event, i=item_id: self._begin_edit(i, event))
        row.bind("<Button-3>", lambda event, i=item_id: self._popup_row_menu(event, i))
        row.bind("<Motion>", on_motion)
        row.bind("<Leave>", on_leave)
        self._bind_wheel(row)

        self.row_widgets.append((item_id, row))

    # ---- 条目拖拽排序 ----
    def _row_press(self, event, item_id):
        """记住按下的位置：右侧按钮各自成区，子步骤小方框单独成区，其余当"点条目本身"。"""
        zone, step = self._zone_at(event.x), None
        event_y = getattr(event, "y", 0)
        widget = self._row_widget(item_id)
        editor = self._steps_edit
        if editor is not None and editor[1] == item_id and widget is not None:
            # 子步骤编辑中：小方框单独成区（点它＝勾这一步），输入框之间的缝隙 / 左边
            # 小方框列算"编辑区"，点它只是把焦点挪到最近的输入框，不会误把主条目标完成。
            index = self._steps_edit_check_at(event.x, event_y)
            if index is not None:
                self._press = {"id": item_id, "y": event.y_root,
                               "zone": "steps-edit-check", "step": index}
            elif event_y >= getattr(widget, "_step_top", 1e9):
                self._press = {"id": item_id, "y": event.y_root,
                               "zone": "steps-edit-area",
                               "step": self._steps_edit_nearest(event_y)}
            else:
                self._press = {"id": item_id, "y": event.y_root, "zone": "body"}
            return
        if zone is None and widget is not None:
            for index, y0, y1, x0, x1 in getattr(widget, "_step_boxes", None) or []:
                if x0 <= event.x <= x1 and y0 <= event_y <= y1:
                    zone, step = "step", index
                    break
            if zone is None and event_y >= getattr(widget, "_step_top", 1e9):
                zone = "step-area"        # 子步骤那一行（但不是小方框）：什么也不做
        self._press = {"id": item_id, "y": event.y_root, "zone": zone or "body",
                       "step": step}

    def _row_motion(self, event):
        if self._press is None:
            return
        self._pointer_y = event.y_root
        if self._drag_id is None:
            if self._press["zone"] != "body":
                return
            if abs(event.y_root - self._press["y"]) < self.px(5):
                return                       # 还在阈值内，先不进入拖动，避免影响点击
            self._begin_drag(self._press["id"])
        self._drag_to(event.y_root)

    def _row_release(self, event):
        press, self._press = self._press, None
        if self._drag_id is not None:
            self._end_drag()
            return
        if press is None:
            return
        zone = press["zone"]
        if zone == "steps-edit-check":
            self._steps_edit_toggle(press.get("step"))
            return
        if zone == "steps-edit-area":
            self._steps_edit_focus(press.get("step"), cursor_to_end=False)
            return
        if zone == "step-area":
            # 点子步骤那一行的文字（不是小方框）：直接进子步骤编辑，光标落在这一行。
            # 原来这里是空的 —— 用户点步骤文字毫无反应，看起来就像"子步骤编不了"。
            widget_now = self._row_widget(press["id"])
            if widget_now is not None:
                self._begin_steps_edit(press["id"],
                                       step_index=self._step_index_at(widget_now, event))
            return
        if zone == "step":
            widget = self._row_widget(press["id"])
            hit = None
            for index, y0, y1, x0, x1 in getattr(widget, "_step_boxes", None) or []:
                if x0 <= event.x <= x1 and y0 <= getattr(event, "y", 0) <= y1:
                    hit = index
                    break
            if hit is not None and hit == press.get("step"):
                self.toggle_step_item(press["id"], hit)
            return
        if zone == "body":
            self.toggle_item(press["id"])
            return
        if self._zone_at(event.x) != zone:
            return                       # 松手时指针得还在同一个按钮上才算数
        if zone == "delete":
            self.remove_item(press["id"])
        elif zone == "steps":
            self._begin_steps_edit(press["id"])

    def _begin_drag(self, item_id):
        self._cancel_regroup()               # 拖动期间别归位（归位会重建行，拖到一半会乱）
        self._drag_id = item_id
        widget = self._row_widget(item_id)
        if widget is not None:
            # 用 Canvas 图元画高亮底 + 左侧强调条：改 Canvas 的 bg 在这里不会重绘
            height = int(widget.cget("height"))
            widget.configure(cursor="fleur")
            widget.create_rectangle(0, 0, self.list_w, height, fill=C_HOVER, outline="",
                                    tags="dragbg")
            widget.create_rectangle(0, 0, self.px(3), height, fill=C_ACCENT, outline="",
                                    tags="dragbg")
            widget.tag_lower("dragbg")
        self._start_autoscroll()

    def _end_drag(self):
        item_id, self._drag_id = self._drag_id, None
        self._pointer_y = None
        self._stop_autoscroll()
        widget = self._row_widget(item_id)
        if widget is not None:
            widget.configure(cursor="hand2")
            widget.delete("dragbg")
        self.store.save()                    # 新顺序落盘
        self._refresh()                      # 重画，保持行高/滚动位置一致
        self._schedule_regroup()             # 万一之前有改过状态还没归位，顺手补上

    def _row_widget(self, item_id):
        for row_id, widget in self.row_widgets:
            if row_id == item_id:
                return widget
        return None

    def _drag_to(self, y_root):
        """把被拖动的条目挪到指针所在的那一行（实时换位，松手才落盘）。

        只在自己那一「段」里换位：默认排序是未完成 → 等待中 → 已完成，拖过界也只贴到
        本段的头/尾，免得一次拖动就把分组打乱（想换段就点勾选框或小钟）。
        """
        if not self.row_widgets:
            return
        target = None
        for index, (_, widget) in enumerate(self.row_widgets):
            top = widget.winfo_rooty()
            if top <= y_root < top + widget.winfo_height():
                target = index
                break
        if target is None:                   # 拖到列表外：贴到首/尾
            first_top = self.row_widgets[0][1].winfo_rooty()
            target = 0 if y_root < first_top else len(self.row_widgets) - 1
        target = self._clamp_to_group(self._drag_id, target)
        if target == self._index_of(self._drag_id):
            return
        if self.store.move(self._drag_id, target, save=False):
            self._repack_rows()

    def _clamp_to_group(self, item_id, target):
        """把落点夹到该条目所在状态段内（默认排序是分段的，拖拽不跨段）。"""
        low, high = self._group_span(item_id)
        return max(low, min(int(target), high))

    def _bucket_of(self, item):
        """排序桶：状态 → 是否今天 → 优先级。排序键就是这三样，拖拽也只在桶内换位。"""
        state = self.store.state_of(item)
        today_rank = 0 if (item.get("today") and state != STATE_DONE) else 1
        return (state, today_rank, -self.store.priority_of(item))

    def _group_span(self, item_id):
        """被拖条目所在「桶」在 row_widgets 里的下标范围（含两端）。

        桶 = 状态 + 是否今天 + 优先级。拖拽只在这个范围里换位，免得松手一归位就被弹回去
        （想换桶就去点勾选框 / 小钟 / 今 / 旗子）。
        """
        last = max(0, len(self.row_widgets) - 1)
        item = self._find_item(item_id)
        if item is None:
            return 0, last
        key = self._bucket_of(item)
        indexes = []
        for index, (row_id, _) in enumerate(self.row_widgets):
            row_item = self._find_item(row_id)
            if row_item is not None and self._bucket_of(row_item) == key:
                indexes.append(index)
        if not indexes:
            return 0, last
        return min(indexes), max(indexes)

    def _index_of(self, item_id):
        for index, (row_id, _) in enumerate(self.row_widgets):
            if row_id == item_id:
                return index
        return -1

    def _repack_rows(self):
        """按 store.items 的新顺序重新摆放行（只动顺序，不重建内容）。"""
        by_id = dict(self.row_widgets)
        ordered = [(item["id"], by_id[item["id"]])
                   for item in self.store.items if item["id"] in by_id]
        if len(ordered) != len(self.row_widgets):
            return
        for _, widget in ordered:
            widget.pack_forget()
        for _, widget in ordered:
            widget.pack(fill="x")
        self.row_widgets = ordered

    def _start_autoscroll(self):
        if self._autoscroll_job is None:
            self._autoscroll_job = self.root.after(70, self._autoscroll_tick)

    def _stop_autoscroll(self):
        if self._autoscroll_job is not None:
            try:
                self.root.after_cancel(self._autoscroll_job)
            except (tk.TclError, ValueError):
                pass
            self._autoscroll_job = None

    def _autoscroll_tick(self):
        """拖到列表上下边缘时持续滚动，方便把条目挪到看不见的位置。"""
        self._autoscroll_job = None
        if self._drag_id is None or self._pointer_y is None:
            return
        if self.inner.winfo_reqheight() > self.list_max_h:
            top = self.canvas.winfo_rooty()
            bottom = top + self.canvas.winfo_height()
            if self._pointer_y < top + self.px(14):
                self.canvas.yview_scroll(-1, "units")
                self._drag_to(self._pointer_y)
            elif self._pointer_y > bottom - self.px(14):
                self.canvas.yview_scroll(1, "units")
                self._drag_to(self._pointer_y)
        self._start_autoscroll()

    def _update_counts(self):
        self._refresh_tags()
        visible = self._visible_items()
        remaining = self.store.remaining(visible)
        waiting = self.store.waiting_count(visible)
        reminders = self.store.reminder_count(visible)
        if visible and remaining == 0:
            text = "全部完成"
        elif waiting:
            text = "%d 项未完成 · %d 等待" % (remaining, waiting)
        else:
            text = "%d 项未完成" % remaining
        if reminders:
            text += " · %d 提醒" % reminders
        self.count.configure(text=text)

    def _update_footer(self):
        self._update_hint()
        if self.store.has_done():
            self.clear_btn.pack(side="right")
        else:
            self.clear_btn.pack_forget()

    def _archive_on_start(self):
        """启动时看一眼：有没有「今天 00:00 之前完成」的，有就收进归档。

        刻意**不做**周期性检查 —— 免得东西在你眼前 00:00 那一下消失；
        只在打开它的时候收（一天最多一次，`_archived_day` 记着今天已经看过了）。
        """
        self._archive_tick()

    def _archive_tick(self, now=None):
        """跨天就把「今天 00:00 之前完成」的条目收进归档；返回收了几条（同一天再叫返回 0）。"""
        now = time.time() if now is None else now
        day = self.store.day_start(now)
        if self._archived_day == day:
            return 0
        moved = self.store.archive_completed(now)
        self._archived_day = day
        if moved:
            self._refresh(keep_scroll=False)
        return moved

    def _update_hint(self):
        """底部提示：折叠时提示展开，条目够多时提示可拖动排序 / 双击可改。"""
        if self._alert is not None:
            return                     # 到点提醒优先，别被常规提示盖掉
        if self._undo is not None:
            return                     # 正在显示「已删除…＋撤销」，别把它覆盖掉
        if self._steps_edit is not None:
            self.hint.configure(text="回车=保存 · Ctrl+回车=加一步 · Esc=取消",
                                fg=C_ACCENT)
            return
        if self.filter_tag:
            self.hint.configure(text="只看 @%s（%d 条）· 点上面的「全部」取消"
                                     % (self.filter_tag, len(self._visible_items())),
                                fg=C_ACCENT)
            return
        if self.store.ui["collapsed"]:
            text = "双击标题展开"
        elif self.store.today_stats()[1]:
            done, total = self.store.today_stats()
            self.hint.configure(text="今天 %d/%d" % (done, total), fg=C_ACCENT)
            return
        elif len(self.store.items) >= 2:
            text = "拖动可排序，双击可改"
        else:
            text = "双击标题可折叠"
        self.hint.configure(text=text, fg=C_MUTED)

    def _show_undo(self, record):
        """底部临时显示「已删除「…」 + 撤销」，过一会儿自动收起。"""
        if self._alert is not None:        # 提醒横幅让位给撤销（撤销只有 8 秒）
            self._dismiss_alert()
        text = record["item"]["text"]
        limit = 14
        if len(text) > limit:
            text = text[:limit] + "…"
        self.hint.configure(text="已删除「%s」" % text, fg=C_TEXT)
        self.undo_btn.pack(side="left", padx=(self.px(6), 0))
        if self._undo_job is not None:
            try:
                self.root.after_cancel(self._undo_job)
            except (tk.TclError, ValueError):
                pass
        self._undo_job = self.root.after(int(UNDO_SECONDS * 1000), self._clear_undo)

    def _clear_undo(self):
        """撤销时限到了：收起按钮、恢复普通提示。"""
        self._undo_job = None
        self._undo = None
        self.undo_btn.pack_forget()
        self._update_hint()

    # ---- 到点提醒 ----
    def _remind_tick(self, raise_it=True):
        """检查有没有到点的提醒；到点就弹横幅、把窗口提到前面，然后挂下一次检查。

        `raise_it=False` 用在两处：启动时（窗口本来就在前面）和自检里（不打扰桌面）。
        """
        if self._remind_job is not None:
            try:
                self.root.after_cancel(self._remind_job)
            except (tk.TclError, ValueError):
                pass
            self._remind_job = None
        due = self.store.due_reminders()
        if due:
            for item in due:
                item["remind_at"] = None        # 只响一次；改时间再设
            self.store.save()
            self._show_alert(due)
            self._update_counts()
            if raise_it:
                try:
                    self.raise_window()
                except tk.TclError:
                    pass
            try:
                self.root.bell()                # 一声铃；系统静音/无声卡就算了
            except tk.TclError:
                pass
        try:
            self._remind_job = self.root.after(REMIND_POLL_MS, self._remind_tick)
        except tk.TclError:
            self._remind_job = None

    def _show_alert(self, items):
        """底部横幅：⏰ 到点什么 + 「知道了」。多条时只列前三条。"""
        self._alert = list(items)
        names = "；".join(item["text"][:16] for item in items[:3])
        if len(items) > 3:
            names += " 等 %d 条" % len(items)
        self.hint.configure(text="⏰ %s" % names, fg=C_WAIT)
        self.undo_btn.pack_forget()
        self.alert_btn.pack(side="left", padx=(self.px(6), 0))

    def _dismiss_alert(self):
        self._alert = None
        self.alert_btn.pack_forget()
        self._update_hint()

    def _flash_hint(self, text):
        """临时提示 3 秒（设/取消提醒后的反馈），然后回到常规提示。"""
        if self._alert is not None:
            return
        self.hint.configure(text=text, fg=C_ACCENT)
        if self._hint_job is not None:
            try:
                self.root.after_cancel(self._hint_job)
            except (tk.TclError, ValueError):
                pass
        self._hint_job = self.root.after(3000, self._restore_hint)

    def _restore_hint(self):
        self._hint_job = None
        if self._alert is None:
            self._update_hint()

    def set_reminder_item(self, item_id, when):
        item = self.store.set_reminder(item_id, when)
        if item is None:
            return None
        self._update_counts()
        self._flash_hint("已设提醒：%s" % format_remind_time(when))
        return item

    def clear_reminder_item(self, item_id):
        item = self.store.clear_reminder(item_id)
        if item is None:
            return None
        self._update_counts()
        self._flash_hint("已取消提醒")
        return item

    def _ask_reminder(self, item_id):
        """右键 → 设提醒：一个小对话框，只认 HH:MM（过了今天就落到明天）。"""
        item = self._find_item(item_id)
        if item is None:
            return None
        current = item.get("remind_at")
        default = time.strftime("%H:%M", time.localtime(
            current if current else time.time() + 3600))

        win = tk.Toplevel(self.root)
        win.title("设提醒")
        win.configure(bg=C_CARD)
        win.transient(self.root)
        win.resizable(False, False)
        tk.Label(win, text="几点提醒？（HH:MM，过了点就明天）", bg=C_CARD, fg=C_DIM,
                 font=self.fonts["small"]).pack(padx=self.px(14), pady=(self.px(12), self.px(6)))
        entry = tk.Entry(win, font=self.fonts["base"], width=8, bg=C_LIST, fg=C_TEXT,
                         insertbackground=C_TEXT, relief="flat", justify="center")
        entry.insert(0, default)
        entry.pack(padx=self.px(14), ipady=self.px(4))
        error = tk.Label(win, text="", bg=C_CARD, fg=C_DANGER, font=self.fonts["small"])
        error.pack(padx=self.px(14))
        buttons = tk.Frame(win, bg=C_CARD)
        buttons.pack(padx=self.px(14), pady=self.px(10))

        def close():
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()

        def submit(event=None):
            when = parse_remind_time(entry.get())
            if when is None:
                error.configure(text="写成 15:30 这样")
                return
            close()
            self.set_reminder_item(item_id, when)

        ok = tk.Label(buttons, text="确定", bg=C_CARD, fg=C_ACCENT,
                      font=self.fonts["small"], cursor="hand2", padx=self.px(8))
        ok.pack(side="left")
        cancel = tk.Label(buttons, text="取消", bg=C_CARD, fg=C_MUTED,
                          font=self.fonts["small"], cursor="hand2", padx=self.px(8))
        cancel.pack(side="left")
        ok.bind("<Button-1>", submit)
        cancel.bind("<Button-1>", lambda event: close())
        win.bind("<Return>", submit)
        win.bind("<Escape>", lambda event: close())
        win.protocol("WM_DELETE_WINDOW", close)
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_rootx() + self.px(40),
                                 self.root.winfo_rooty() + self.px(70)))
        entry.focus_set()
        entry.select_range(0, "end")
        try:
            win.grab_set()                 # 窗口还没 viewable 时抓不到；抓不到不影响输入
        except tk.TclError:
            pass
        return win

    def _on_inner_configure(self, event=None):
        height = min(self.inner.winfo_reqheight(), self.list_max_h)
        if int(self.canvas.cget("height")) != height:
            self.canvas.configure(height=height)
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                            self.inner.winfo_reqheight()))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.canvas.find_all()[0], width=event.width)

    # ---- 尺寸 / 位置 ----
    def _resize(self):
        self.card.update_idletasks()
        if self.store.ui["collapsed"]:
            height = self.header.winfo_reqheight() + self.px(2)
        else:
            height = self.card.winfo_reqheight() + self.px(2)
        width = self.panel_w
        if self._user_sized:
            # 用户自己拉过大小：只增不减，别把他的窗口缩回去
            try:
                width = max(width, self.root.winfo_width())
                height = max(height, self.root.winfo_height())
            except tk.TclError:
                pass
        self.root.geometry("%dx%d" % (width, height))

    def _restore_position(self):
        self.root.update_idletasks()
        self._apply_saved_size()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = self.store.ui["x"]
        y = self.store.ui["y"]
        if x is None or y is None:
            x = screen_w - self.panel_w - self.px(48)
            y = self.px(96)
        x = max(self.px(80) - self.panel_w, min(int(x), screen_w - self.px(80)))
        y = max(0, min(int(y), screen_h - self.px(40)))
        self.root.geometry("+%d+%d" % (x, y))

    def _apply_collapsed(self):
        if self.store.ui["collapsed"]:
            self.input_row.pack_forget()
            self.list_area.pack_forget()
            self.footer.pack_forget()
        else:
            self.input_row.pack(fill="x", padx=self.pad, pady=(self.px(2), self.px(8)))
            self.list_area.pack(fill="x", padx=self.pad, pady=(0, self.px(8)))
            self.footer.pack(fill="x", padx=self.pad,
                             pady=(0, max(0, self.pad - self.px(4))))
        self._update_hint()
        self._draw_icon(self.collapse_btn, "collapse", C_DIM)
        self._resize()
    # ---- 拖动 ----
    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag_offset is None:
            return
        offset_x, offset_y = self._drag_offset
        self.root.geometry("+%d+%d" % (event.x_root - offset_x, event.y_root - offset_y))

    def _drag_end(self, event):
        if self._drag_offset is None:
            return
        self._drag_offset = None
        self.store.ui["x"] = self.root.winfo_x()
        self.store.ui["y"] = self.root.winfo_y()
        if self._user_sized:
            self.store.ui["width"] = self.root.winfo_width()
            self.store.ui["height"] = self.root.winfo_height()
        self.store.save()

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---- 动作 ----
    def add_from_entry(self):
        self._clear_placeholder()          # 只清占位文字，用户输入原样保留
        created = self.store.add(self.entry.get())
        self.entry.delete(0, "end")
        if created is not None:
            self._refresh(keep_scroll=False)
        self.entry.focus_force()      # 输入完留在输入框里，提示文字等失焦后再显示
        return created

    def toggle_item(self, item_id):
        """勾选 / 取消：只重画这一行，不整表重建（否则双击永远到不了编辑）。"""
        item = self.store.toggle(item_id)
        if item is None:
            return None
        self._last_toggle = (item_id, time.time())
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_counts()
        self._update_footer()
        return item

    def toggle_waiting_item(self, item_id):
        """标记 / 取消「等待中」：同样只重画那一行。

        等待中不是完成：事情已经着手，但在等别人反馈，所以从「未完成」里分出来单列，
        计数里也单独显示，免得天天看到它以为自己在拖。
        """
        item = self.store.toggle_waiting(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_counts()
        self._update_footer()
        return item

    def add_step_item(self, item_id):
        """右键菜单：加一条子步骤，然后重画那一行。"""
        item = self.store.add_step(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        return item

    def set_priority_item(self, item_id, value):
        """右键菜单里选了优先级：重画那一行。"""
        item = self.store.set_priority(item_id, value)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        return item

    def toggle_step_item(self, item_id, index):
        """勾选一个子步骤：只重画那一行；不动主条目的状态，也不参与排序。

        子步骤编辑器开着的时候走编辑器自己的路径：只改编辑中的勾选状态，
        保存时一起落盘（不重建行控件，输入框也不会被销毁）。
        """
        editor = self._steps_edit
        if editor is not None and editor[1] == item_id:
            self._steps_edit_toggle(index)
            return self._find_item(item_id)
        item = self.store.toggle_step(item_id, index)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._update_footer()
        return item

    def cycle_priority_item(self, item_id):
        """点旗子：无 → 高 → 中 → 无。排序会变，所以照例延迟归位。"""
        item = self.store.cycle_priority(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        return item

    def toggle_today_item(self, item_id):
        """标记 / 取消「今天要办的」：会浮到本段最前，所以也延迟归位。"""
        item = self.store.toggle_today(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_footer()             # 底部「今天 x/y」要跟着变
        return item

    # ---- 归位（默认排序：未完成 → 等待中 → 已完成）----
    def _schedule_regroup(self):
        """状态变了先别急着把它挪到别的段去，等用户停手再归位。

        原因和「双击改名」直接相关：双击的第一下按单击处理会改状态，如果这一下就让整行
        飞到列表底部，第二下就落到别的行上了 —— Tk 判定双击要求两次点击在同一个控件上，
        编辑框再也打不开。所以归位推迟到 REGROUP_DELAY_MS 之后，且在拖动/编辑/按住时顺延。
        """
        self._cancel_regroup()
        try:
            self._regroup_job = self.root.after(self.REGROUP_DELAY_MS, self._regroup_now)
        except tk.TclError:
            self._regroup_job = None

    def _cancel_regroup(self):
        if self._regroup_job is not None:
            job, self._regroup_job = self._regroup_job, None
            try:
                self.root.after_cancel(job)
            except (tk.TclError, ValueError):
                pass

    def _regroup_now(self):
        """把列表按状态归位；用户正在操作时顺延，别抢他的鼠标。"""
        self._regroup_job = None
        if self._drag_id is not None or self._press is not None \
                or self._edit_entry is not None:
            self._schedule_regroup()
            return
        if self.store.resort(save=True):
            self._refresh()

    def remove_item(self, item_id):
        """删除一条：先记下它和原位置，底部留 8 秒撤销窗口。"""
        if self._edit_entry is not None:
            self._end_edit(save=True)      # 正在编辑就先收好，别改到已经删掉的行
        record = self.store.pop(item_id)
        if record is None:
            return
        self._undo = record
        self._refresh()
        self._show_undo(record)

    def undo_delete(self):
        """把刚删掉的那条放回它原来的位置。"""
        record, self._undo = self._undo, None
        if self._undo_job is not None:
            try:
                self.root.after_cancel(self._undo_job)
            except (tk.TclError, ValueError):
                pass
            self._undo_job = None
        self.undo_btn.pack_forget()
        if record is not None and self.store.restore(record):
            self._refresh()
        else:
            self._update_hint()
        return record

    # ---- 双击编辑 ----
    def _find_item(self, item_id):
        for item in self.store.items:
            if item["id"] == item_id:
                return item
        return None

    def _begin_edit(self, item_id, event=None):
        """双击条目：在那一行上盖一个输入框；回车保存、Esc 取消、点到别处也算保存。"""
        if event is not None and event.x >= self._marker_zone_start():
            return                         # 双击落在右侧按钮上，不当编辑
        editor = self._steps_edit
        if editor is not None and editor[1] == item_id:
            # 已经在编子步骤了：双击只是换个输入框，别保存→重开
            self._steps_edit_focus(self._steps_edit_nearest(getattr(event, "y", 0)))
            return
        if event is not None:
            widget_now = self._row_widget(item_id)
            if widget_now is not None and getattr(event, "y", 0) >= \
                    getattr(widget_now, "_step_top", 1e9):
                # 双击在子步骤那一行上：进"子步骤编辑"，光标落在双击的那一行，
                # 而不是什么都不做、也不是去改主文字（这两点都踩过）
                self._begin_steps_edit(item_id,
                                       step_index=self._step_index_at(widget_now, event))
                return
        self._cancel_regroup()             # 编辑期间不归位：_refresh() 会把输入框一起销毁
        self._end_steps_edit(save=True)    # 子步骤编辑器也先收掉
        if self._edit_entry is not None:
            self._end_edit(save=True)      # 先收掉上一个
        # 双击的第一下已经按「单击 = 勾选」处理过了；双击的语义是改文字，
        # 不该顺手改完成状态，所以这里把那次勾选撤回来。
        last_id, last_at = self._last_toggle
        if (event is not None and last_id == item_id
                and (time.time() - last_at) < DOUBLE_CLICK_REVERT_S):
            self._last_toggle = (None, 0.0)
            self.store.toggle(item_id)
            self._redraw_row(item_id)
            self._update_counts()
            self._update_footer()
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return
        entry = tk.Entry(widget, bg=C_BORDER, fg=C_TEXT, insertbackground=C_TEXT,
                         relief="flat", bd=0, font=self.fonts["base"],
                         highlightthickness=0)
        tags = self.store.tags_of(item)
        entry.insert(0, item["text"] + (" " + " ".join("@" + t for t in tags) if tags else ""))
        entry.select_range(0, "end")
        height = int(widget.cget("height"))
        entry.place(x=self.text_x - self.px(2), y=self.px(2),
                    width=max(self.px(40),
                              self.list_w - self.text_x - self.del_w - self.px(2)),
                    height=max(self.px(16), height - 2 * self.px(2)))
        entry.bind("<Return>", lambda event: self._end_edit(save=True))
        entry.bind("<KP_Enter>", lambda event: self._end_edit(save=True))   # 小键盘回车
        entry.bind("<Escape>", lambda event: self._end_edit(save=False))
        entry.bind("<FocusOut>", lambda event: self._end_edit(save=True))
        self._edit_id = item_id
        self._edit_entry = entry
        try:
            entry.focus_force()
        except tk.TclError:
            pass

    def _end_edit(self, save=True):
        """结束编辑：先把状态摘掉再销毁控件，免得回调打在被销毁的控件上。"""
        entry, item_id = self._edit_entry, self._edit_id
        if entry is None:
            return False
        self._edit_entry = None
        self._edit_id = None
        try:
            text = entry.get()
        except tk.TclError:
            return False
        try:
            entry.destroy()
        except tk.TclError:
            pass
        changed = self.store.rename(item_id, text) if save else False
        self._refresh()
        self._schedule_regroup()             # 编辑结束再归位（编辑中归位会把输入框销毁）
        return changed

    def _build_row_menu(self, item_id):
        """右键条目的菜单。右侧图标区删掉的动作都搬到这里（拆出来是为了自检能查它）。"""
        item = self._find_item(item_id)
        if item is None:
            return None
        menu = tk.Menu(self.root, tearoff=0)
        waiting = self.store.state_of(item) == STATE_WAITING
        menu.add_command(label="取消「等待中」" if waiting else "标记为「等待中」",
                         command=lambda: self.toggle_waiting_item(item_id))
        marked = bool(item.get("today"))
        menu.add_command(label="取消「今天要办的」" if marked else "标记为「今天要办的」",
                         command=lambda: self.toggle_today_item(item_id))
        priority = self.store.priority_of(item)
        prio_names = {PRIORITY_NONE: "无", PRIORITY_MID: "中", PRIORITY_HIGH: "高"}
        prio_menu = tk.Menu(menu, tearoff=0)
        prio_var = tk.IntVar(master=self.root, value=priority)
        menu._prio_var = prio_var                 # 别让 IntVar 被回收
        for label, value in (("高", PRIORITY_HIGH), ("中", PRIORITY_MID),
                             ("无", PRIORITY_NONE)):
            prio_menu.add_radiobutton(label=label, value=value, variable=prio_var,
                                      command=lambda v=value: self.set_priority_item(item_id, v))
        menu.add_cascade(label="优先级（现在是%s）" % prio_names.get(priority, "无"),
                         menu=prio_menu)
        when = item.get("remind_at")
        menu.add_separator()
        if when:
            menu.add_command(label="改提醒（%s）…" % format_remind_time(when),
                             command=lambda: self._ask_reminder(item_id))
            menu.add_command(label="取消提醒",
                             command=lambda: self.clear_reminder_item(item_id))
        else:
            menu.add_command(label="设提醒…", command=lambda: self._ask_reminder(item_id))
        done, total = self.store.steps_progress(item)
        menu.add_separator()
        menu.add_command(label=("编辑子步骤…" if not total
                                else "编辑子步骤（%d/%d 已完成）…" % (done, total)),
                         command=lambda: self._begin_steps_edit(item_id))
        menu.add_command(label="＋ 加一条子步骤",
                         command=lambda: self.add_step_item(item_id))
        if total:
            menu.add_command(label="清空子步骤",
                             command=lambda: self._clear_steps(item_id))
        menu.add_separator()
        menu.add_command(label="删除这条", command=lambda: self.remove_item(item_id))
        return menu

    def _popup_row_menu(self, event, item_id):
        menu = self._build_row_menu(item_id)
        if menu is None:
            return
        self._dismiss_row_menu()               # 先收起上一个，别堆着
        self._row_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _dismiss_row_menu(self, *args, **kwargs):
        """收起右键菜单：点别处、再右键、或菜单被选过之后都走这里。

        签名为什么收 *args：Tk 走 bind_all 时实测会**传两个参数**进来
        （TypeError: <lambda>() takes 1 positional argument but 2 were given）——
        收窄成 event 一个参数的话，回调会抛异常、菜单就收不掉了。
        Tk 自带的"点外部自动收起"在这套无边框窗口上也不可靠（实测点空白处菜单不走），
        所以额外挂全局绑定兜底，并且**销毁**菜单控件，免得每次右键都堆一个。
        """
        menu = self._row_menu
        if menu is None:
            return
        event = args[0] if args else None
        hit = getattr(event, "widget", None)
        if hit is not None and str(hit).startswith(str(menu)):
            return                         # 点在菜单自己身上：交给菜单处理，别销毁
        self._row_menu = None
        try:
            menu.unpost()
        except tk.TclError:
            pass
        try:
            menu.destroy()
        except tk.TclError:
            pass

    def _clear_steps(self, item_id):
        if self.store.set_steps(item_id, []) is not None:
            if not self._redraw_row(item_id):
                self._refresh()

    # ---- 子步骤编辑（右键条目 → 编辑子步骤…）----
    def _step_index_at(self, canvas, event):
        """双击落在第几条子步骤上（靠行内已记下的小方框命中区算）。"""
        y = getattr(event, "y", 0)
        for index, y0, y1, _x0, _x1 in getattr(canvas, "_step_boxes", None) or []:
            if y0 <= y < y1:
                return index
        return None

    def _steps_editor_metrics(self, widget):
        """子步骤编辑区在行内的顶端和每个输入框的行高。

        顶端取自 _draw_row 记下的 canvas._step_top：它在**标题文字块**下面，
        所以每个输入框都从子步骤那一行开始，不会盖住条目正文。
        """
        top = getattr(widget, "_step_top", None)
        if top is None:
            top = int(widget.cget("height")) - self.row_pad_y
        line_h = self.fonts["small"].metrics("linespace") + self.px(2)
        return int(top), int(line_h)

    def _steps_edit_texts(self, boxes=None):
        """把编辑中各输入框的文字读出来（不销毁输入框）。"""
        if boxes is None:
            record = self._steps_edit
            boxes = record[0] if record is not None else []
        texts = []
        for box in boxes:
            try:
                texts.append(box.get("1.0", "end-1c"))
            except tk.TclError:
                texts.append("")
        return texts

    def _steps_edit_lines(self, text):
        """一段子步骤文字在当前行宽下会占几个显示行（折行规则和列表里一致）。"""
        limit = max(self.px(40), self.row_wrap - int(self.step_indent))
        return max(1, len(self._wrap_text(text, self.fonts["small"], limit)))

    def _steps_edit_check_at(self, x, y):
        """编辑时：点在第几行的小方框上（横竖都算），返回行号；否则 None。"""
        box = self.step_box
        left = self.text_x
        for index, (_y0, _height, cy) in enumerate(self._steps_edit_geom):
            if (left - self.px(4) <= x <= left + box + self.px(4)
                    and cy - box / 2.0 - self.px(2) <= y <= cy + box / 2.0 + self.px(2)):
                return index
        return None

    def _steps_edit_nearest(self, y):
        """编辑时：y 落在（或最靠近）第几个输入框。"""
        best, best_dist = 0, None
        for index, (y0, height, _cy) in enumerate(self._steps_edit_geom):
            if y0 <= y <= y0 + height:
                return index
            dist = min(abs(y - y0), abs(y - (y0 + height)))
            if best_dist is None or dist < best_dist:
                best, best_dist = index, dist
        return best

    def _steps_edit_make_box(self, widget, text, done, index):
        """子步骤编辑器里"一条子步骤一个输入框"（不是一个装下所有子步骤的大框）。"""
        box = tk.Text(widget, bg=C_BORDER, fg=C_TEXT, insertbackground=C_TEXT,
                      relief="flat", bd=0, highlightthickness=0,
                      font=self.fonts["small"], wrap="char",
                      padx=0, pady=0, spacing1=0, spacing2=0, spacing3=0)
        box.insert("1.0", text)
        box.tag_configure("done", foreground=C_DONE, overstrike=1)
        if done:
            box.tag_add("done", "1.0", "1.end")
        box.bind("<Escape>", self._steps_cancel_key)
        box.bind("<Return>", self._steps_save_key)              # 回车＝保存退出
        box.bind("<KP_Enter>", self._steps_save_key)            # 小键盘回车同样＝保存
        box.bind("<Control-Return>", lambda event, i=index: self._steps_add_key(i))
        box.bind("<FocusOut>", self._steps_focus_out)
        box.bind("<<Modified>>", self._steps_modified)
        box.bind("<Tab>", lambda event: self._steps_move_focus(1))
        box.bind("<Shift-Tab>", lambda event: self._steps_move_focus(-1))
        box.bind("<Down>", lambda event: self._steps_arrow_key(event, 1))
        box.bind("<Up>", lambda event: self._steps_arrow_key(event, -1))
        box.bind("<FocusIn>", lambda event, i=index: self._steps_edit_note_focus(i))
        box.edit_modified(False)
        return box

    def _steps_edit_make_boxes(self, widget, texts, done, focus_index=None):
        """按 texts / done 建一排输入框并摆好（加一步、删空行之后也走这里）。"""
        record = self._steps_edit
        if record is None:
            return
        for old in record[0]:
            try:
                old.destroy()
            except tk.TclError:
                pass
        boxes = []
        for index, text in enumerate(texts):
            boxes.append(self._steps_edit_make_box(
                widget, text, bool(done[index]) if index < len(done) else False, index))
        self._steps_edit = (boxes, record[1])
        self._steps_edit_done = [bool(value) for value in done]
        self._steps_edit_layout()
        if boxes:
            self._steps_edit_focus(len(boxes) - 1 if focus_index is None else focus_index,
                                   cursor_to_end=True)

    def _steps_edit_layout(self):
        """重新摆这一行：输入框一个接一个（中间留缝），小方框按新位置重画。"""
        record = self._steps_edit
        if record is None:
            return
        boxes, item_id = record
        widget = self._row_widget(item_id)
        if widget is None or not boxes:
            return
        top, line_h = self._steps_editor_metrics(widget)
        linespace = self.fonts["small"].metrics("linespace")
        gap = self.px(6)
        left = self.text_x + int(self.step_indent)
        width = max(self.px(40), self.row_wrap - int(self.step_indent))
        y = top
        geom = []
        for box in boxes:
            try:
                text = box.get("1.0", "end-1c")
            except tk.TclError:
                text = ""
            height = self._steps_edit_lines(text) * linespace + self.px(2)
            try:
                box.place(x=left, y=y, width=width, height=height)
            except tk.TclError:
                return
            geom.append((y, height, y + linespace / 2.0))
            y += height + gap
        total = (y - gap - top) if boxes else 0
        base_h = max(int(self._steps_edit_base_h), top + line_h + self.row_pad_y)
        need = top + total + self.row_pad_y
        new_h = max(base_h, need)
        if int(widget.cget("height")) != new_h:
            widget.configure(height=new_h)
            self._steps_edit_grew = self._steps_edit_grew or new_h > base_h
            try:
                self.root.update_idletasks()
                self._on_inner_configure()
                self._resize()
                self._reveal_steps_editor(widget)
            except tk.TclError:
                pass
        self._steps_edit_geom = geom
        widget.delete("steps-edit-check")
        for index, (_y0, _height, cy) in enumerate(geom):
            done = self._steps_edit_done[index] if index < len(self._steps_edit_done) else False
            self._draw_step_check(widget, self.text_x, cy, done, tags="steps-edit-check")

    def _steps_edit_focus(self, index, cursor_to_end=False):
        """把焦点挪到第 index 个子步骤输入框（越界就夹到两端）。"""
        record = self._steps_edit
        if record is None or not record[0]:
            return
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(index, len(record[0]) - 1))
        try:
            box = record[0][index]
            box.focus_force()
            if cursor_to_end:
                box.mark_set("insert", "end-1c")
            self._steps_edit_active = index
        except tk.TclError:
            pass

    def _steps_edit_focus_index(self):
        """当前聚焦第几个子步骤输入框（自检 / 驱动用）。"""
        return self._steps_edit_active

    def _steps_edit_toggle(self, index):
        """编辑时点小方框：只改"编辑中"的勾选状态（保存时一起落盘），同步删除线。"""
        record = self._steps_edit
        if record is None:
            return
        try:
            index = int(index)
        except (TypeError, ValueError):
            return
        if not 0 <= index < len(record[0]):
            return
        done = list(self._steps_edit_done)
        if index >= len(done):
            done.extend([False] * (index + 1 - len(done)))
        done[index] = not bool(done[index])
        self._steps_edit_done = done
        box = record[0][index]
        try:
            if done[index]:
                box.tag_add("done", "1.0", "1.end")
            else:
                box.tag_remove("done", "1.0", "1.end")
        except tk.TclError:
            pass
        self._steps_edit_focus(index)
        self._steps_edit_layout()

    def _begin_steps_edit(self, item_id, step_index=None):
        """进入子步骤编辑：**每一条子步骤一个独立的输入框**，不再是装下所有子步骤的大框。

        每条输入框都挨着自己那一行的小方框，框与框之间留缝，谁是谁一眼分得清；
        条目正文和右侧按钮照常可见。回车＝保存退出、Ctrl+回车＝在下面加一步、
        Esc＝取消、点别处也算保存。
        """
        self._cancel_regroup()
        self._end_steps_edit(save=True)
        if self._edit_entry is not None:
            self._end_edit(save=True)
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return None
        steps = self.store.steps_of(item)
        texts = [step["text"] for step in steps] or [""]
        done = [bool(step["done"]) for step in steps] or [False]
        self._steps_edit_win = (self.root.winfo_width(), self.root.winfo_height())
        self._steps_edit_grew = False
        self._steps_edit_base_h = int(widget.cget("height"))
        self._steps_edit_geom = []
        self._steps_edit = ([], item_id)
        widget.delete("step")       # 正常排版画的那排子步骤先撤掉，编辑时按新排版重画
        focus_index = step_index if step_index is not None else len(texts) - 1
        self._steps_edit_make_boxes(widget, texts, done, focus_index=focus_index)
        self._update_hint()
        try:
            self.root.update_idletasks()
            if widget.winfo_exists():
                self._reveal_steps_editor(widget)
        except tk.TclError:
            pass
        boxes = self._steps_edit[0] if self._steps_edit else []
        return boxes[0] if boxes else None

    def _steps_modified(self, event=None):
        """某个子步骤框的内容变了：重排（文字变长折行 / 删字变短）并让光标露出来。"""
        record = self._steps_edit
        if record is None:
            return
        box = getattr(event, "widget", None) if event is not None else None
        if box is None or box not in record[0]:
            return
        try:
            if not box.edit_modified():
                return                 # 复位标记本身会再触发一次 <<Modified>>，这里挡掉
            box.edit_modified(False)
        except tk.TclError:
            return
        self._steps_edit_layout()
        try:
            box.see("insert")
        except tk.TclError:
            pass

    def _reveal_steps_editor(self, widget):
        """这一行被撑高之后，保证它的底部在可视区域里（窗口加不高时靠列表滚动）。"""
        try:
            self.root.update_idletasks()
            inner_h = max(1, self.inner.winfo_reqheight())
            visible = self.canvas.winfo_height()
            top = widget.winfo_y()
            bottom = top + widget.winfo_height()
            view_top = int(self.canvas.canvasy(0))
        except tk.TclError:
            return
        if bottom > view_top + visible:
            self.canvas.yview_moveto(max(0.0, min(1.0, (bottom - visible) / float(inner_h))))
        elif top < view_top:
            self.canvas.yview_moveto(max(0.0, top / float(inner_h)))

    def _steps_edit_note_focus(self, index):
        """用户用鼠标点到 / Tab 到某个框：记住它，Ctrl+回车加一步时以它为准。"""
        try:
            if 0 <= int(index) < len(self._steps_edit[0]):
                self._steps_edit_active = int(index)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _steps_arrow_key(self, event, delta):
        """上下方向键：光标还在长文本中间时照常移动光标，到边界才换到上/下一个框。"""
        box = getattr(event, "widget", None)
        try:
            cursor = int(box.index("insert").split(".")[0])
            last = int(box.index("end-1c").split(".")[0])
        except (tk.TclError, AttributeError, ValueError):
            return None
        if (delta > 0 and cursor >= last) or (delta < 0 and cursor <= 1):
            self._steps_move_focus(delta)
            return "break"
        return None

    def _steps_move_focus(self, delta):
        """Tab / 上下方向键：在子步骤输入框之间挪。"""
        record = self._steps_edit
        if record is None:
            return "break"
        current = self._steps_edit_active
        self._steps_edit_focus((0 if current is None else current) + delta,
                               cursor_to_end=True)
        return "break"

    def _steps_add_key(self, index=None, event=None):
        """Ctrl+回车：在当前这条子步骤后面加一个空输入框。"""
        record = self._steps_edit
        if record is None:
            return "break"
        texts = self._steps_edit_texts()
        done = list(self._steps_edit_done)
        if len(texts) >= STEP_MAX:
            self._flash_hint("一条待办最多 %d 个子步骤" % STEP_MAX)
            return "break"
        if index is None:
            index = self._steps_edit_active
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = len(texts) - 1
        index = max(-1, min(index, len(texts) - 1))
        texts.insert(index + 1, "")
        done.insert(index + 1, False)
        widget = self._row_widget(record[1])
        if widget is not None:
            self._steps_edit_make_boxes(widget, texts, done, focus_index=index + 1)
        return "break"

    def _steps_save_key(self, event=None):
        """子步骤输入框里按回车：保存退出。"""
        self._end_steps_edit(save=True)
        return "break"                     # 别再往已销毁的框里插换行

    def _steps_cancel_key(self, event=None):
        """子步骤输入框里按 Esc：丢弃这次修改。"""
        self._end_steps_edit(save=False)
        return "break"

    def _steps_focus_out(self, event=None):
        """某个子步骤框失焦：可能只是换到另一个框，稍后再判断要不要收。"""
        if self._steps_edit is None:
            return
        try:
            self.root.after(1, self._steps_edit_check_focus)
        except tk.TclError:
            pass

    def _steps_edit_check_focus(self):
        """焦点确实离开整组输入框（点别处 / 窗口失焦）才算"点别处保存"。"""
        record = self._steps_edit
        if record is None:
            return
        try:
            focus = self.root.focus_get()
        except (tk.TclError, KeyError):
            focus = None
        if focus is not None and focus in record[0]:
            return
        self._end_steps_edit(save=True)

    def _end_steps_edit(self, save=True):
        """收掉子步骤编辑器：save=False 直接丢弃；save=True 把每个框的文字写回（勾选照旧）。"""
        record, self._steps_edit = self._steps_edit, None
        if record is None:
            return False
        boxes, item_id = record
        grew, window = self._steps_edit_grew, self._steps_edit_win
        self._steps_edit_grew = False
        self._steps_edit_win = None
        texts = self._steps_edit_texts(boxes)
        done = list(self._steps_edit_done)
        self._steps_edit_done = []
        self._steps_edit_geom = []
        self._steps_edit_active = None
        for box in boxes:
            try:
                box.destroy()
            except tk.TclError:
                pass
        widget = self._row_widget(item_id)
        if widget is not None:
            try:
                widget.delete("steps-edit-check")
            except tk.TclError:
                pass
        changed = False
        if save:
            steps = []
            for index, text in enumerate(texts):
                text = " ".join(str(text).split())
                if not text:
                    continue
                steps.append({"text": text,
                              "done": bool(done[index]) if index < len(done) else False})
            changed = self.store.set_steps(item_id, steps) is not None
        self._refresh()
        # 用户自己拉过窗口大小时，_resize() 只增不减 —— 编辑时撑高的那点要还回去，
        # 否则每编辑一次窗口就长高一行。
        if grew and window is not None and self._user_sized \
                and int(window[0]) > 1 and int(window[1]) > 1:
            try:
                self.root.update_idletasks()
                self.root.geometry("%dx%d" % (int(window[0]), int(window[1])))
            except (tk.TclError, TypeError, ValueError):
                pass
        self._update_hint()
        self._schedule_regroup()
        return changed

    def clear_done(self):
        if self.store.clear_done():
            self._refresh(keep_scroll=False)

    def toggle_collapsed(self):
        self.store.ui["collapsed"] = not self.store.ui["collapsed"]
        self.store.save()
        self._apply_collapsed()

    def _apply_topmost(self):
        value = bool(self._topmost_var.get())
        self.store.ui["topmost"] = value
        self.root.attributes("-topmost", value)
        self.store.save()

    def _apply_alpha(self):
        value = float(self._alpha_var.get())
        self.store.ui["alpha"] = value
        self.root.attributes("-alpha", value)
        self.store.save()

    def _toggle_autostart(self):
        """右键菜单勾「开机自启」：装/拆自启项，不用再让用户去终端敲参数。"""
        want = bool(self._autostart_var.get())
        if want:
            autostart_install()
        else:
            autostart_uninstall()
        self._autostart_var.set(os.path.exists(autostart_path()))
        self._flash_hint("已开启开机自启" if want else "已关闭开机自启")

    def quit(self):
        try:
            self.store.ui["x"] = self.root.winfo_x()
            self.store.ui["y"] = self.root.winfo_y()
        except tk.TclError:
            pass
        self._stop_autoscroll()
        self._cancel_regroup()
        hotkey_remove()                           # 解掉全局快捷键（不然退出后那个键还空占着）
        single_instance_stop(self.root)
        self.store.resort()                       # 退出前把顺序归位，文件里也是分好组的
        self.store.save()
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 归档浏览器
# --------------------------------------------------------------------------- #
_ARCHIVE_REASON = {          # archive.reason → (给用户看的词, 颜色)
    "done": ("完成", C_DIM),        # 过了 00:00 自动收走的
    "cleared": ("清掉", C_DIM),     # 点「归档已完成」收走的
    "deleted": ("删除", C_DANGER),  # 点 × 删掉的
}


class ArchiveWindow(object):
    """只读的归档浏览器：列出 `archive` 段，点「恢复」把一条放回列表。

    为什么单开一个窗口（而不是嵌进挂件里）：归档最多能存 500 条，塞进 300px 宽的挂件
    会把待办列表挤没；而且它只是"翻旧账"，翻完就该关掉 —— 所以用**非模态** Toplevel，
    主窗口照常能用。观感沿用主面板那套缩放系数与配色，窗口同样不进任务栏。
    """

    WIDTH = 380          # 设计宽度（会按界面缩放放大）
    MAX_LIST_H = 420     # 列表区最高；超出就滚轮

    def __init__(self, owner, visible=True):
        self.owner = owner
        self.store = owner.store
        self.s = owner.s
        self._drag_offset = None
        self._rows = []                  # 每行一个 Frame（自检按它数行）
        self._window_item = None
        self.win = tk.Toplevel(owner.root)
        self.win.withdraw()              # 先别映射：去标题栏的提示要在建图前生效
        self.win.title("归档 · 桌面待办清单")
        self.win.configure(bg=C_BORDER)
        self.win.transient(owner.root)   # 跟主窗口绑定：总在它上面，也不单独占一个任务栏项
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda event: self.close())
        self.width = owner.px(self.WIDTH)
        self._build()
        if visible:
            self.show()

    # ---- 组装 ----
    def _build(self):
        px = self.owner.px
        self.card = tk.Frame(self.win, bg=C_CARD)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(self.card, bg=C_CARD)
        head.pack(fill="x", padx=px(10), pady=(px(8), px(4)))
        title = tk.Label(head, text="归档", bg=C_CARD, fg=C_TEXT,
                         font=self.owner.fonts["bold"])
        title.pack(side="left")
        self.count = tk.Label(head, text="", bg=C_CARD, fg=C_DIM,
                              font=self.owner.fonts["small"])
        self.count.pack(side="left", padx=(px(8), 0))
        close_btn = tk.Label(head, text="×", bg=C_CARD, fg=C_MUTED,
                             font=self.owner.fonts["bold"], cursor="hand2")
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda event: self.close())
        close_btn.bind("<Enter>", lambda event: close_btn.configure(fg=C_TEXT))
        close_btn.bind("<Leave>", lambda event: close_btn.configure(fg=C_MUTED))
        for widget in (head, title, self.count):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        self.canvas = tk.Canvas(self.card, bg=C_LIST, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=1)
        self.inner = tk.Frame(self.canvas, bg=C_LIST)
        self._window_item = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.inner):
            widget.bind("<Button-4>", self._on_wheel)
            widget.bind("<Button-5>", self._on_wheel)
            widget.bind("<MouseWheel>", self._on_wheel)

        self.hint = tk.Label(self.card, text="", bg=C_CARD, fg=C_MUTED,
                             font=self.owner.fonts["small"], anchor="w")
        self.hint.pack(fill="x", padx=px(10), pady=(px(4), px(8)))
        self.refresh()

    def refresh(self):
        """重建列表（归档变了之后调用）。"""
        for child in list(self.inner.winfo_children()):
            child.destroy()
        self._rows = []
        records = sorted(self.store.archive,
                         key=lambda record: record.get("archived_at") or 0, reverse=True)
        if not records:
            tk.Label(self.inner,
                     text="归档是空的。\n过了 00:00 收走的已完成、点「归档已完成」收走的、\n"
                          "点 × 删掉的，都会留在这里（最多 %d 条）。" % ARCHIVE_MAX,
                     bg=C_LIST, fg=C_MUTED, font=self.owner.fonts["small"],
                     justify="left", anchor="w").pack(
                fill="x", padx=self.owner.px(10), pady=self.owner.px(16))
        for record in records:
            self._rows.append(self._make_row(record))
        self.count.configure(text="%d 条" % len(records))
        self.hint.configure(text="点「恢复」把一条放回列表 · Esc 或 × 关闭", fg=C_MUTED)
        self._fit()

    def _make_row(self, record):
        px = self.owner.px
        item = record.get("item") or {}
        reason = record.get("reason") or "done"
        label, color = _ARCHIVE_REASON.get(reason, ("完成", C_DIM))
        row = tk.Frame(self.inner, bg=C_LIST)
        row.pack(fill="x", padx=px(8), pady=(px(6), px(4)))

        meta = tk.Frame(row, bg=C_LIST)
        meta.pack(fill="x")
        tk.Label(meta, text=label, bg=C_LIST, fg=color,
                 font=self.owner.fonts["small"]).pack(side="left")
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(record.get("archived_at") or 0))
        tk.Label(meta, text=when, bg=C_LIST, fg=C_MUTED,
                 font=self.owner.fonts["small"]).pack(side="left", padx=(px(6), 0))
        restore = tk.Label(meta, text="恢复", bg=C_LIST, fg=C_ACCENT,
                           font=self.owner.fonts["small"], cursor="hand2")
        restore.pack(side="right")
        restore.bind("<Button-1>", lambda event, rec=record: self.restore(rec))
        restore.bind("<Enter>", lambda event: restore.configure(fg=C_TEXT))
        restore.bind("<Leave>", lambda event: restore.configure(fg=C_ACCENT))

        tk.Label(row, text=item.get("text") or "(无正文)", bg=C_LIST, fg=C_TEXT,
                 font=self.owner.fonts["base"], wraplength=self.width - px(40),
                 justify="left", anchor="w").pack(fill="x", pady=(px(2), 0))
        tk.Frame(self.inner, bg=C_BORDER, height=1).pack(fill="x", padx=px(8))
        return row

    # ---- 动作 ----
    def restore(self, record):
        """把归档里的一条放回列表；成功返回 True（失败只改底部提示）。"""
        index = record.get("index") or 0
        if not self.store.restore({"item": record.get("item"), "index": index}):
            self.hint.configure(text="这一条已经在列表里了", fg=C_DANGER)
            return False
        self.store.resort(save=True)          # 放回去就按状态归位（不必等延迟归位）
        self.owner._refresh(keep_scroll=False)
        self.refresh()
        return True

    # ---- 显示 / 关闭 / 拖动 ----
    def show(self):
        """显示并提到最前（已开着时走这里，不重建窗口）。"""
        self.refresh()
        self.win.update_idletasks()
        strip_window_decorations(self.win)
        set_skip_taskbar(self.win)            # 建图前先写属性
        x, y = self._place()
        self.win.geometry("+%d+%d" % (x, y))
        self.win.deiconify()
        # 映射后再补一次：KWin 只对**已托管**的窗口处理那条 ClientMessage。
        # 少了这一句，归档窗口会在 dock 里多出一个 Tk 默认图标的图标（实测过）。
        self.win.update_idletasks()
        set_skip_taskbar(self.win)
        self.win.lift()
        try:
            self.win.focus_force()
        except tk.TclError:
            pass

    def close(self):
        """关掉窗口（幂等），顺手清掉主窗口留的引用。"""
        if getattr(self.owner, "_archive_window", None) is self:
            self.owner._archive_window = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _place(self):
        """贴着主窗口的右下角放（超出屏幕就往回收）。"""
        px = self.owner.px
        try:
            rx, ry = self.owner.root.winfo_rootx(), self.owner.root.winfo_rooty()
            rw = self.owner.root.winfo_width()
        except tk.TclError:
            rx, ry, rw = 0, 0, self.width
        x = rx + rw - self.width
        y = ry + px(40)
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        x = max(px(4), min(x, screen_w - self.width - px(4)))
        y = max(px(4), min(y, screen_h - px(200)))
        return x, y

    def _fit(self):
        """按内容定窗口高度（列表区最多 MAX_LIST_H）。"""
        px = self.owner.px
        self.canvas.configure(height=px(60))
        self.win.update_idletasks()
        chrome = self.card.winfo_reqheight() - self.canvas.winfo_reqheight()
        content = max(px(60), min(self.inner.winfo_reqheight(), px(self.MAX_LIST_H)))
        self.canvas.configure(height=content)
        self.win.geometry("%dx%d" % (self.width, chrome + content))

    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.win.winfo_x(),
                             event.y_root - self.win.winfo_y())

    def _drag_move(self, event):
        if self._drag_offset is None:
            return
        offset_x, offset_y = self._drag_offset
        self.win.geometry("+%d+%d" % (event.x_root - offset_x, event.y_root - offset_y))

    def _drag_end(self, event):
        self._drag_offset = None

    # ---- 滚动 ----
    def _on_inner_configure(self, event=None):
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                            self.inner.winfo_reqheight()))

    def _on_canvas_configure(self, event):
        if self._window_item is not None:
            self.canvas.itemconfigure(self._window_item, width=event.width)

    def _on_wheel(self, event):
        if self.inner.winfo_reqheight() <= int(self.canvas.cget("height")):
            return "break"
        up = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
        self.canvas.yview_scroll(-2 if up else 2, "units")
        return "break"


# --------------------------------------------------------------------------- #
# 自检 / 自启动
# --------------------------------------------------------------------------- #
def run_check():
    """数据读写与界面构造自检，成功打印 SELFTEST OK 并返回 0。"""
    import tempfile

    if tk is None:
        sys.stderr.write("自检失败: 缺少 tkinter（统信 UOS / Debian: "
                         "sudo apt install python3-tk）\n")
        return 1

    problems = []
    temp_dir = tempfile.mkdtemp(prefix="desktop-todo-check-")
    try:
        path = os.path.join(temp_dir, "todos.json")

        store = Store(path)
        if store.items:
            problems.append("新数据文件应为空清单")
        first = store.add("  写周报  ")
        second = store.add("买牛奶")
        if not first or first["text"] != "写周报":
            problems.append("新增条目未去除首尾空白")
        if [item["text"] for item in store.items] != ["买牛奶", "写周报"]:
            problems.append("新增条目未插入到最前")

        reloaded = Store(path)
        if len(reloaded.items) != 2 or reloaded.remaining() != 2:
            problems.append("重新读取后条目不一致")

        reloaded.toggle(second["id"])
        if reloaded.remaining() != 1 or not reloaded.has_done():
            problems.append("切换完成状态失败")
        if Store(path).remaining() != 1:
            problems.append("完成状态未落盘")

        # ---- 三态：未完成 / 等待中 / 已完成 ----
        # 「等待中」= 已经着手，但在等别人反馈，自己现在动不了：既不算完成，也不该混在待办里催自己。
        tri = Store(path)
        tri.clear_done()
        todo_item = tri.add("还没开始的一条")
        wait_item = tri.add("等同事回字段的一条")
        tri.toggle_waiting(wait_item["id"])
        if tri.state_of(wait_item) != STATE_WAITING:
            problems.append("toggle_waiting 没有把状态改成等待中")
        if wait_item["done"]:
            problems.append("等待中不该被算成已完成")
        if tri.waiting_count() != 1:
            problems.append("waiting_count 计数不对")
        if tri.remaining() != len(tri.items):
            problems.append("等待中的条目仍应算作「未完成」")
        again = Store(path)
        reading = [item for item in again.items if item["id"] == wait_item["id"]]
        if not reading or reading[0].get("state") != STATE_WAITING or reading[0]["done"]:
            problems.append("等待中状态没有落盘或被读错")
        for item in again.items:            # done 与 state 必须始终一致（老版本只读 done）
            if bool(item["done"]) != (again.state_of(item) == STATE_DONE):
                problems.append("done 与 state 不同步: %r" % (item,))
        again.toggle_waiting(wait_item["id"])          # 再点一次 → 回到未完成
        if again.state_of([i for i in again.items if i["id"] == wait_item["id"]][0]) \
                != STATE_TODO:
            problems.append("取消「等待中」失败")
        again.toggle_waiting(wait_item["id"])          # 等待中 → 勾选应为已完成
        again.toggle(wait_item["id"])
        if again.state_of([i for i in again.items if i["id"] == wait_item["id"]][0]) \
                != STATE_DONE:
            problems.append("等待中的条目勾选后应为已完成")
        again.toggle_waiting(wait_item["id"])          # 回到未完成
        again.toggle_waiting(wait_item["id"])          # 再标记等待中
        again.toggle(todo_item["id"])                  # 另一条标记完成
        if again.clear_done() != 1 or not any(i["id"] == wait_item["id"] for i in again.items):
            problems.append("清除已完成时把「等待中」的条目也删了")

        # 旧数据（只有 done、没有 state）要能读：缺 state 时按 done 推断
        legacy_path = os.path.join(temp_dir, "legacy.json")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump({"items": [{"id": 1, "text": "老格式已完成", "done": True},
                                 {"id": 2, "text": "老格式未完成", "done": False}]}, handle)
        legacy = Store(legacy_path)
        legacy_states = dict((item["text"], legacy.state_of(item)) for item in legacy.items)
        if legacy_states.get("老格式已完成") != STATE_DONE \
                or legacy_states.get("老格式未完成") != STATE_TODO:
            problems.append("旧格式数据没有正确推断状态: %r" % (legacy_states,))
        if [item["text"] for item in legacy.items] != ["老格式未完成", "老格式已完成"]:
            problems.append("旧格式数据读进来没有按状态归位: %r"
                            % ([item["text"] for item in legacy.items],))

        # ---- 全局快捷键的解析（纯逻辑，两平台共用；真机注册在各自后端里）----
        hotkey_cases = {"Super+T": (0x40, "t"), "ctrl+alt+k": (0x0C, "k"),
                        "F5": (0x00, "F5"), "Shift+Space": (0x01, "space")}
        for spec, expect in hotkey_cases.items():
            if parse_hotkey(spec) != expect:
                problems.append("快捷键解析不对: %r → %r（期望 %r）"
                                % (spec, parse_hotkey(spec), expect))
        for bad in ("", "Ctrl", "Ctrl++", "Ctrl+Alt", "Foo+Bar",
                    "Ctrl+Alt+Shift+Meta+1+2", "中"):
            if parse_hotkey(bad) is not None:
                problems.append("认不出的快捷键应当返回 None: %r → %r"
                                % (bad, parse_hotkey(bad)))
        if format_hotkey("super+t") != "Super+T" or format_hotkey("ctrl+alt+k") != "Ctrl+Alt+K":
            problems.append("快捷键格式化不对: %r / %r"
                            % (format_hotkey("super+t"), format_hotkey("ctrl+alt+k")))
        if parse_hotkey("Super+T") != parse_hotkey("super-t"):
            problems.append("快捷键对大小写和连字符应当等价")

        # ---- 子步骤 ----
        subs = Store(os.path.join(temp_dir, "steps.json"))
        st1 = subs.add("带子步骤的一条")
        if subs.steps_of(st1) != [] or subs.steps_progress(st1) != (0, 0):
            problems.append("新条目应当没有子步骤")
        subs.set_steps(st1["id"], ["第一", "", "  第二  ",
                                   {"text": "第三", "done": True}])
        got_steps = subs.steps_of(st1)
        if [x["text"] for x in got_steps] != ["第一", "第二", "第三"] \
                or [x["done"] for x in got_steps] != [False, False, True]:
            problems.append("set_steps 没把数据收拾干净: %r" % (got_steps,))
        if subs.steps_progress(st1) != (1, 3):
            problems.append("子步骤进度统计不对: %r" % (subs.steps_progress(st1),))
        subs.toggle_step(st1["id"], 0)
        if subs.steps_of(st1)[0]["done"] is not True or subs.steps_progress(st1) != (2, 3):
            problems.append("toggle_step 没生效: %r" % (subs.steps_of(st1),))
        if subs.toggle_step(st1["id"], 99) is not None or subs.toggle_step(999999, 0) is not None:
            problems.append("越界的子步骤索引应当返回 None")
        subs.set_steps(st1["id"], ["步骤%02d" % i for i in range(STEP_MAX + 5)])
        if len(subs.steps_of(st1)) != STEP_MAX:
            problems.append("子步骤条数没有被限制在 %d: %r"
                            % (STEP_MAX, len(subs.steps_of(st1))))
        # 子步骤既不改主条目状态，也不参与排序
        state_before = subs.state_of(st1)
        order_before = [item["id"] for item in subs.items]
        subs.toggle_step(st1["id"], 0)
        subs.resort(save=True)
        if subs.state_of(st1) != state_before or st1["done"]:
            problems.append("勾选子步骤不该改动主条目的状态")
        if [item["id"] for item in subs.items] != order_before:
            problems.append("子步骤不该影响排序: %r → %r"
                            % (order_before, [item["id"] for item in subs.items]))
        reloaded_subs = Store(os.path.join(temp_dir, "steps.json"))
        if reloaded_subs.steps_of(reloaded_subs.items[0])[0]["done"] is not True:
            problems.append("子步骤的勾选没有落盘")
        bad_steps = Store(os.path.join(temp_dir, "steps-bad.json"))
        bad_item = bad_steps.add("坏数据")
        bad_item["steps"] = "不是列表"
        if bad_steps.steps_of(bad_item) != []:
            problems.append("steps 不是列表时应当按空处理")
        bad_item["steps"] = [None, 12, {"text": "  "}, {"text": "好", "done": 1}]
        if [x["text"] for x in bad_steps.steps_of(bad_item)] != ["好"]:
            problems.append("混杂的 steps 数据没有被清理: %r" % (bad_steps.steps_of(bad_item),))

        # ---- 优先级 / 「今天要办的」----
        marks = Store(os.path.join(temp_dir, "marks.json"))
        m1 = marks.add("第一条")          # 新增插到最前，所以列表顺序是 [第二条, 第一条]
        m2 = marks.add("第二条")
        if marks.priority_of(m1) != PRIORITY_NONE or m1.get("today"):
            problems.append("新条目的优先级/今天标记默认值不对")
        marks.cycle_priority(m1["id"])     # 无 → 高
        if marks.priority_of(m1) != PRIORITY_HIGH:
            problems.append("点一下旗子应该是高: %r" % (marks.priority_of(m1),))
        marks.cycle_priority(m1["id"])     # 高 → 中
        if marks.priority_of(m1) != PRIORITY_MID:
            problems.append("再点一下应该是中: %r" % (marks.priority_of(m1),))
        marks.cycle_priority(m1["id"])     # 中 → 无
        if marks.priority_of(m1) != PRIORITY_NONE:
            problems.append("第三下应该回到无: %r" % (marks.priority_of(m1),))
        if marks.cycle_priority(999999) is not None:
            problems.append("给不存在的条目设优先级应返回 None")
        # 「今天」要把老的那条顶到本段最前
        marks.toggle_today(m1["id"])
        if not m1.get("today") or marks.today_stats() != (0, 1):
            problems.append("「今天」标记或统计不对: %r %r"
                            % (m1.get("today"), marks.today_stats()))
        marks.resort()
        if [i["text"] for i in marks.items] != ["第一条", "第二条"]:
            problems.append("「今天」没有排在段内最前: %r"
                            % ([i["text"] for i in marks.items],))
        # 同为今天时先看优先级：给第二条高，它应该回到最前
        marks.cycle_priority(m2["id"])
        marks.toggle_today(m2["id"])
        marks.resort()
        if [i["text"] for i in marks.items] != ["第二条", "第一条"]:
            problems.append("同是今天时应先按优先级: %r"
                            % ([(i["text"], i.get("priority")) for i in marks.items],))
        # 已完成的条目不参与「今天」置顶，但仍计入今天的进度
        marks.toggle(m2["id"])
        marks.resort(save=True)
        if marks.items[-1]["text"] != "第二条":
            problems.append("已完成的条目不该被「今天」顶到最前: %r"
                            % ([i["text"] for i in marks.items],))
        if marks.today_stats() != (1, 2):
            problems.append("今天的进度统计不对: %r" % (marks.today_stats(),))
        saved = Store(os.path.join(temp_dir, "marks.json"))
        by_text = dict((i["text"], i) for i in saved.items)
        if (saved.priority_of(by_text.get("第二条") or {}) != PRIORITY_HIGH
                or not (by_text.get("第一条") or {}).get("today")):
            problems.append("优先级/今天没有落盘: %r"
                            % ([(i["text"], i.get("priority"), i.get("today"))
                                for i in saved.items],))
        if [i["text"] for i in saved.items] != [i["text"] for i in marks.items]:
            problems.append("归位后的顺序没有落盘: %r" % ([i["text"] for i in saved.items],))

        # ---- 到点提醒 ----
        # 基准用写死的「2026-10-22 09:00」，其余都按相对量算，不依赖运行时刻
        base = time.mktime((2026, 10, 22, 9, 0, 0, 0, 0, -1))
        if parse_remind_time("09:30", now=base) != base + 30 * 60:
            problems.append("parse_remind_time 没算出今天 09:30")
        if parse_remind_time("08:00", now=base) != base + 23 * 3600:
            problems.append("已经过去的时刻应该落到明天: %r"
                            % (parse_remind_time("08:00", now=base),))
        for bad in ("", "25:00", "9点", "930", "09:60"):
            if parse_remind_time(bad, now=base) is not None:
                problems.append("认不出的提醒时间应当返回 None: %r" % (bad,))
        if format_remind_time(base + 30 * 60, now=base) != "09:30":
            problems.append("format_remind_time 当天格式不对")
        if not format_remind_time(base + 23 * 3600, now=base).startswith("明天"):
            problems.append("format_remind_time 明天格式不对")

        remind = Store(os.path.join(temp_dir, "remind.json"))
        r1 = remind.add("九点半交材料")
        r2 = remind.add("已完成的不该再提醒")
        if r1.get("remind_at") is not None:
            problems.append("新条目默认不该有提醒")
        remind.set_reminder(r1["id"], base - 60)          # 已经到点
        remind.set_reminder(r2["id"], base - 60)
        remind.toggle(r2["id"])                           # 完成 → 提醒应被清掉
        if r2.get("remind_at"):
            problems.append("完成后提醒没有清掉（取消完成时会立刻补响）")
        due = remind.due_reminders(now=base)
        if [item["text"] for item in due] != ["九点半交材料"]:
            problems.append("到点提醒筛选不对（已完成的不该算）: %r"
                            % ([item["text"] for item in due],))
        if remind.reminder_count() != 1:
            problems.append("提醒计数不对: %r" % (remind.reminder_count(),))
        reread = Store(os.path.join(temp_dir, "remind.json"))
        kept = [item for item in reread.items if item["text"] == "九点半交材料"]
        if not kept or not kept[0].get("remind_at"):
            problems.append("提醒时间没有落盘")
        if not reread.clear_reminder(r1["id"]) or reread.due_reminders(now=base):
            problems.append("取消提醒后仍会到点")

        # ---- 默认排序：未完成 → 等待中 → 已完成（同组内保持手动顺序）----
        order = Store(os.path.join(temp_dir, "order.json"))
        a = order.add("A")            # 顺序：A B C D（新增插到最前）
        b = order.add("B")
        c = order.add("C")
        d = order.add("D")
        order.toggle(d["id"])                    # D 已完成
        order.toggle_waiting(c["id"])            # C 等待中
        order.resort()
        got_order = [item["text"] for item in order.items]
        if got_order != ["B", "A", "C", "D"]:
            problems.append("归位后顺序不对（期望 未完成B,A → 等待中C → 已完成D）: %r" % (got_order,))
        if order.resort():
            problems.append("已经排好序的列表再排一次不该有变化")
        # 同组内手动顺序要保住：把 A 拖到 B 前面
        order.move(a["id"], 0)
        order.resort()
        if [item["text"] for item in order.items] != ["A", "B", "C", "D"]:
            problems.append("归位把同组内拖出来的顺序打乱了: %r"
                            % ([item["text"] for item in order.items],))
        order.save()
        if [item["text"] for item in Store(os.path.join(temp_dir, "order.json")).items] \
                != ["A", "B", "C", "D"]:
            problems.append("归位后的顺序没有落盘")
        messy_path = os.path.join(temp_dir, "messy.json")
        with open(messy_path, "w", encoding="utf-8") as handle:
            json.dump({"items": [
                {"id": 1, "text": "已完成", "done": True},
                {"id": 2, "text": "未完成1", "done": False},
                {"id": 3, "text": "已完成2", "done": True},
                {"id": 4, "text": "未完成2", "done": False},
            ]}, handle)
        messy = Store(messy_path)
        if [item["text"] for item in messy.items] != \
                ["未完成1", "未完成2", "已完成", "已完成2"]:
            problems.append("乱序的老数据读进来没有归位: %r"
                            % ([item["text"] for item in messy.items],))

        reloaded.ui.update({"x": 123, "y": 45, "collapsed": True, "alpha": 0.82})
        reloaded.save()
        restored = Store(path)
        if (restored.ui["x"], restored.ui["y"], restored.ui["collapsed"]) != (123, 45, True):
            problems.append("窗口状态未正确恢复")
        if abs(restored.ui["alpha"] - 0.82) > 1e-6:
            problems.append("透明度设置未正确恢复")

        if restored.clear_done() != 1 or len(Store(path).items) != 1:
            problems.append("清除已完成失败")
        if not restored.remove(first["id"]) or len(Store(path).items) != 0:
            problems.append("删除条目失败")

        # 拖拽排序：move() 让条目占据目标位置，其余顺序保持
        reorder = Store(path)
        for text in ("A", "B", "C", "D"):
            reorder.add(text)                       # 新增插到最前 → D C B A
        if [item["text"] for item in reorder.items] != ["D", "C", "B", "A"]:
            problems.append("排序前置顺序不对")
        reorder.move(reorder.items[0]["id"], 2)     # 把 D 放到第 3 个位置
        if [item["text"] for item in reorder.items] != ["C", "B", "D", "A"]:
            problems.append("向后拖动排序结果不对")
        reorder.move(reorder.items[2]["id"], 0)     # 把 D 提到最前
        if [item["text"] for item in reorder.items] != ["D", "C", "B", "A"]:
            problems.append("向前拖动排序结果不对")
        if [item["text"] for item in Store(path).items] != ["D", "C", "B", "A"]:
            problems.append("排序结果未落盘")

        # 改名 + 删除后撤销：pop() 要交出原位置，restore() 要放回原位
        editing = Store(path)
        before_texts = [item["text"] for item in editing.items]
        keep = editing.add("会被删掉的一条")
        editing.add("后面的")
        record = editing.pop(keep["id"])
        if record is None:
            problems.append("pop() 没找到条目")
        elif [item["text"] for item in Store(path).items] != ["后面的"] + before_texts:
            problems.append("pop() 没有正确移除条目")
        elif not Store(path).restore(record):
            problems.append("restore() 没能把条目放回去")
        elif [item["text"] for item in Store(path).items] != \
                ["后面的", "会被删掉的一条"] + before_texts:
            problems.append("撤销删除后位置不对：%r"
                            % ([item["text"] for item in Store(path).items],))
        renamed = Store(path)
        first_item = renamed.items[0]
        if not renamed.rename(first_item["id"], "  改过的文字  "):
            problems.append("改名没有生效")
        elif Store(path).items[0]["text"] != "改过的文字":
            problems.append("改名未去空白或未落盘")
        if renamed.rename(first_item["id"], "   "):
            problems.append("改名不该接受空文本")
        if Store(path).restore(record):
            problems.append("restore() 不该把已经在列表里的条目再插一遍")

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        broken = Store(path)
        if broken.items:
            problems.append("损坏的数据文件应回退为空清单")
        if not os.path.exists(path + ".broken"):
            problems.append("损坏的数据文件未备份")

        # 界面构造（窗口保持隐藏，跑一轮事件循环）
        try:
            root = tk.Tk()
            widget = TodoWidget(root, broken, visible=False)   # 自检不打扰桌面
            for text in ("界面自检一", "界面自检二", "界面自检三"):
                broken.add(text)
            widget._refresh()
            widget.toggle_collapsed()
            widget.toggle_collapsed()
            widget.clear_done()
            # 拖拽排序的代码路径（隐藏窗口下不校验落点几何，只验证能跑通、状态能收尾）
            widget._begin_drag(broken.items[0]["id"])
            widget._drag_to(widget.row_widgets[-1][1].winfo_rooty() + 1)
            if widget._drag_id is None:
                problems.append("拖动状态未建立")
            widget._end_drag()
            if widget._drag_id is not None:
                problems.append("拖动状态未清理")
            widget._refresh()
            # 勾选必须原地重画：行控件对象要活下来，否则 Tk 永远配不出 <Double-Button-1>
            # （这就是「双击改文字真机上没反应」的根因，用对象同一性在这里卡住）
            target = broken.items[0]
            row_before = widget._row_widget(target["id"])
            widget.toggle_item(target["id"])
            if widget._row_widget(target["id"]) is not row_before:
                problems.append("勾选后行控件被重建了（双击会因此失效）")
            # 原地重画还得真把内容改掉：勾选框在「已完成」时是 2 个图元（底 + 勾），
            # 未完成时 1 个（只有边框）。数量不对说明这一行没按新状态重画。
            expect_marks = 2 if target["done"] else 1
            if len(row_before.find_withtag("check")) != expect_marks:
                problems.append("勾选后行内容没有重画（原地更新失效）")
            if getattr(row_before, "_delete_item", None) is None:
                problems.append("重画后删除按钮的图元引用丢了")
            if widget._edit_entry is not None:
                problems.append("勾选不该进入编辑状态")
            # 双击手势：第一下按「单击＝勾选」处理，进编辑时要把那一下撤回来，
            # 且必须真的进入编辑状态（隐藏窗口下用桩事件走 _row_press/_row_release）
            class _StubEvent(object):
                def __init__(self, x, y_root=0, y=0):
                    self.x = x
                    self.y_root = y_root
                    self.y = y

            done_before = target["done"]
            body_x = widget.text_x
            widget._row_press(_StubEvent(x=body_x), target["id"])
            widget._row_release(_StubEvent(x=body_x))
            if target["done"] == done_before:
                problems.append("单击没有切换完成状态")
            widget._begin_edit(target["id"], _StubEvent(x=body_x))
            if target["done"] != done_before:
                problems.append("双击进编辑时没有撤回第一下的勾选")
            if widget._edit_entry is None:
                problems.append("双击编辑没有进入编辑状态")
            else:
                widget._edit_entry.delete(0, "end")
                widget._edit_entry.insert(0, "界面改名测试")
                widget._end_edit(save=True)
                edited = widget._find_item(target["id"])
                if edited is None or edited["text"] != "界面改名测试":
                    problems.append("界面改名没有生效")
            # 删除 + 撤销：要放回原位置
            order_before = [item["text"] for item in broken.items]
            victim = broken.items[0]
            widget.remove_item(victim["id"])
            if widget._undo is None or any(item["id"] == victim["id"]
                                           for item in broken.items):
                problems.append("删除后没有留下撤销记录")
            widget.undo_delete()
            if [item["text"] for item in broken.items] != order_before:
                problems.append("撤销删除后内容或顺序不一致")
            widget._refresh()
            # 「等待中」：格子已删，改从右键菜单切；切完那一行必须原地重画成琥珀字
            row_wait = widget._row_widget(target["id"])

            def menu_label(menu_now, index):
                """取菜单项标签；分隔线没有 -label 选项，返回空串。"""
                try:
                    return str(menu_now.entrycget(index, "label"))
                except tk.TclError:
                    return ""

            def menu_find(menu_now, needle):
                if menu_now is None:
                    return None
                for index in range(menu_now.index("end") + 1):
                    if needle in menu_label(menu_now, index):
                        return index
                return None

            def wait_menu_item(item_id):
                menu_now = widget._build_row_menu(item_id)
                return menu_now, menu_find(menu_now, "等待中")

            menu, wait_index = wait_menu_item(target["id"])
            if wait_index is None:
                problems.append("右键菜单里没有「等待中」这一项")
            else:
                menu.invoke(wait_index)
                if widget.store.state_of(target) != STATE_WAITING:
                    problems.append("从菜单标记「等待中」没有生效")
                if widget._row_widget(target["id"]) is not row_wait:
                    problems.append("标记等待中时行控件被重建了（双击会因此失效）")
                if target["done"]:
                    problems.append("等待中不该同时是已完成")
                fills = {str(row_wait.itemcget(i, "fill"))
                         for i in row_wait.find_withtag("text")}
                if fills != {C_WAIT}:
                    problems.append("等待中的那一行正文没有变成琥珀色: %r" % (fills,))
                menu, wait_index = wait_menu_item(target["id"])
                if wait_index is not None:
                    menu.invoke(wait_index)
                if widget.store.state_of(target) != STATE_TODO:
                    problems.append("从菜单再点一次「等待中」没有回到未完成")
                if Store(path).state_of(
                        [i for i in Store(path).items if i["id"] == target["id"]][0]) != STATE_TODO:
                    problems.append("等待状态没有落盘")

            # ---- 子步骤：渲染 + 小方框勾选 + 步骤行保护 + 右键编辑器 ----
            step_item = widget.store.add("带三个子步骤的界面自检条")
            widget.store.set_steps(step_item["id"], ["步骤甲", "步骤乙", "步骤丙"])
            widget._refresh()
            row_steps = widget._row_widget(step_item["id"])
            boxes = getattr(row_steps, "_step_boxes", None) or []
            if len(boxes) != 3:
                problems.append("子步骤没画出三行小方框: %r" % (boxes,))
            else:
                box_h_before = int(row_steps.cget("height"))

                def steps_cell(row_now):
                    items = (getattr(row_now, "_markers", None) or {}).get("steps") or ()
                    return items[0][0] if items else None

                cell = steps_cell(row_steps)
                if cell is None or str(row_steps.itemcget(cell, "text")) != "0/3":
                    problems.append("子步骤格子上没有显示 0/3: %r"
                                    % (row_steps.itemcget(cell, "text") if cell else None,))
                index, y0, y1, x0, x1 = boxes[1]          # 勾第二个
                cx = (x0 + x1) // 2
                cy = (y0 + y1) // 2
                widget._row_press(_StubEvent(x=cx, y=cy), step_item["id"])
                widget._row_release(_StubEvent(x=cx, y=cy))
                steps_now = widget.store.steps_of(step_item)
                if [s["done"] for s in steps_now] != [False, True, False]:
                    problems.append("点小方框没有勾上第二个子步骤: %r" % (steps_now,))
                if widget._row_widget(step_item["id"]) is not row_steps:
                    problems.append("勾子步骤时行控件被重建了（双击会因此失效）")
                if widget.store.state_of(step_item) == STATE_DONE:
                    problems.append("勾子步骤不该把主条目标成已完成")
                if int(row_steps.cget("height")) != box_h_before:
                    problems.append("勾子步骤不该改变行高")
                cell = steps_cell(row_steps)
                if cell is None or str(row_steps.itemcget(cell, "text")) != "1/3":
                    problems.append("勾完一个后格子上应是 1/3: %r"
                                    % (row_steps.itemcget(cell, "text") if cell else None,))
                # 点步骤文字（不是小方框）：打开子步骤编辑，焦点落在点的那一条
                step_text_x = widget.text_x + widget.step_indent + 12
                widget._row_press(_StubEvent(x=step_text_x, y=cy), step_item["id"])
                widget._row_release(_StubEvent(x=step_text_x, y=cy))
                if [s["done"] for s in widget.store.steps_of(step_item)] != [False, True, False]:
                    problems.append("点子步骤文字不该改动勾选")
                if widget.store.state_of(step_item) == STATE_DONE:
                    problems.append("点子步骤文字不该把主条目标成已完成")
                if widget._steps_edit is None:
                    problems.append("点子步骤文字应当打开子步骤编辑器（点了没反应＝编不了）")
                else:
                    if widget._steps_edit_focus_index() != 1:
                        problems.append("点第 2 条子步骤文字时，焦点应当在第 2 个输入框: %r"
                                        % (widget._steps_edit_focus_index(),))
                    if len(widget._steps_edit[0]) != 3:
                        problems.append("编辑器应当一条子步骤一个输入框，实际 %d 个: %r"
                                        % (len(widget._steps_edit[0]),
                                           widget._steps_edit[0]))
                    if row_steps.find_withtag("step"):
                        problems.append("编辑器开着时旧排版画的子步骤没撤掉（会重影）")
                    widget._end_steps_edit(save=False)     # 收掉，别影响后面的用例
                # 双击落在子步骤那一行 → 进子步骤编辑（不是主文字编辑、也不是没反应）
                widget._begin_edit(step_item["id"], _StubEvent(x=step_text_x, y=cy))
                if widget._edit_entry is not None:
                    problems.append("双击子步骤那一行不该打开主文字编辑框")
                if widget._steps_edit is None:
                    problems.append("双击子步骤那一行应当打开子步骤编辑器（否则双击没反应）")
                else:
                    if widget._steps_edit_focus_index() != 1:
                        problems.append("双击第 2 条子步骤时，焦点应当在第 2 个输入框: %r"
                                        % (widget._steps_edit_focus_index(),))
                    widget._end_steps_edit(save=False)     # 收掉，别影响后面的用例
                # 右键条目的绑定必须在（子步骤编辑器的入口）
                fresh_row = widget._row_widget(step_item["id"])   # 上面收编辑器时重画过，旧引用已销毁
                if fresh_row is None or not fresh_row.bind("<Button-3>"):
                    problems.append("条目没有绑定右键菜单，子步骤编辑器就没有入口")
            # 编辑器：一条子步骤一个独立输入框；加一步 / 勾选 / 保存 / Esc 取消
            widget._begin_steps_edit(step_item["id"])
            record = widget._steps_edit
            if not record or not record[0]:
                problems.append("没有打开子步骤编辑器")
            else:
                edit_boxes = record[0]

                def edit_texts():
                    return [b.get("1.0", "end-1c") for b in widget._steps_edit[0]]

                def box_tag_has_done(idx):
                    try:
                        return "done" in widget._steps_edit[0][idx].tag_names("1.0")
                    except (tk.TclError, IndexError, TypeError):
                        return False

                if len(edit_boxes) != 3:
                    problems.append("编辑器应当有 3 个独立输入框，实际 %d" % (len(edit_boxes),))
                if edit_texts() != ["步骤甲", "步骤乙", "步骤丙"]:
                    problems.append("编辑器内容不对: %r" % (edit_texts(),))
                if len(set(id(b) for b in edit_boxes)) != len(edit_boxes):
                    problems.append("编辑器的输入框不是各自独立的控件")
                geom = list(widget._steps_edit_geom)
                if len(geom) != len(edit_boxes) or any(
                        geom[i + 1][0] <= geom[i][0] + geom[i][1]
                        for i in range(len(geom) - 1)):
                    problems.append("子步骤输入框之间没有留出空隙（会连成一块）: %r" % (geom,))
                if widget._redraw_row(step_item["id"]):
                    problems.append("子步骤编辑器开着时不该重画那一行")
                if "回车=保存" not in str(widget.hint.cget("text")):
                    problems.append("子步骤编辑器开着时，底部应当写成「回车=保存」: %r"
                                    % (widget.hint.cget("text"),))
                src = open(__file__, encoding="utf-8").read()
                # 要搜的串拼出来 —— 直接写字面量的话，检查自己的源码里就有一份，
                # 于是"永远找得到"、反例抓不到（这个坑实测过：swapped 绑定仍报 SELFTEST OK）。
                want_return = 'box.bind("<Return>", self.' + '_steps_save_key)'
                want_ctrl = ('box.bind("<Control-Return>", lambda event, i=index: self.'
                             + '_steps_add_key(i))')
                if want_return not in src or want_ctrl not in src:
                    problems.append("按键接线不对：回车要接 _steps_save_key、"
                                    "Ctrl+回车要接 _steps_add_key")
                count_before = len(edit_boxes)
                widget._steps_add_key(0)                     # 等价于在第 1 条上按 Ctrl+回车
                if widget._steps_edit is None:
                    problems.append("Ctrl+回车 不该退出编辑（它的活是加一步）")
                elif len(widget._steps_edit[0]) != count_before + 1:
                    problems.append("Ctrl+回车 没有加出一个输入框: %d -> %d"
                                    % (count_before, len(widget._steps_edit[0])))
                elif widget._steps_edit_focus_index() != 1:
                    problems.append("加一步之后焦点应当落在新框上: %r"
                                    % (widget._steps_edit_focus_index(),))
                # 编辑中点小方框：只改编辑中的勾选状态（保存时一起落盘）
                widget._steps_edit_toggle(3)
                if not box_tag_has_done(3):
                    problems.append("编辑中点小方框后，那个输入框没有删除线")
                widget._steps_edit_toggle(3)
                boxes_now = widget._steps_edit[0]
                boxes_now[0].delete("1.0", "end")
                boxes_now[0].insert("1.0", "步骤甲改了")
                boxes_now[1].delete("1.0", "end")            # 新加的空框：保存时应当丢掉
                boxes_now[2].delete("1.0", "end")
                boxes_now[2].insert("1.0", "步骤乙改了")
                boxes_now[3].delete("1.0", "end")            # 删掉「步骤丙」
                widget._steps_save_key()                     # 等价于按回车（保存退出）
                steps_saved = widget.store.steps_of(step_item)
                if [s["text"] for s in steps_saved] != ["步骤甲改了", "步骤乙改了"]:
                    problems.append("保存的文本不对（空框应当丢掉）: %r" % (steps_saved,))
                if [s["done"] for s in steps_saved] != [False, True]:
                    problems.append("按位置保留勾选这条不对: %r" % (steps_saved,))
                if widget._steps_edit is not None:
                    problems.append("保存后编辑器引用没清掉")
                if "回车=保存" in str(widget.hint.cget("text")):
                    problems.append("编辑器收掉后，底部提示应当恢复: %r"
                                    % (widget.hint.cget("text"),))
            widget._begin_steps_edit(step_item["id"])
            esc_boxes = (widget._steps_edit or ([], None))[0]
            if esc_boxes:
                esc_boxes[0].delete("1.0", "end")
                esc_boxes[0].insert("1.0", "不该被保存")
                widget._end_steps_edit(save=False)
                if [s["text"] for s in widget.store.steps_of(step_item)] != \
                        ["步骤甲改了", "步骤乙改了"]:
                    problems.append("Esc 取消不该改动子步骤")
            else:
                problems.append("第二次打开子步骤编辑器失败")
            # 回车＝保存（真按键）：改一条再按回车，编辑器收掉且改动落盘
            widget._begin_steps_edit(step_item["id"])
            enter_boxes = (widget._steps_edit or ([], None))[0]
            if not enter_boxes:
                problems.append("回车用例打不开子步骤编辑器")
            else:
                enter_boxes[0].delete("1.0", "end")
                enter_boxes[0].insert("1.0", "回车保存的第一行")
                if len(enter_boxes) > 1:
                    enter_boxes[1].delete("1.0", "end")
                    enter_boxes[1].insert("1.0", "回车保存的第二行")
                widget._steps_save_key()                    # 等价于按回车
                if widget._steps_edit is not None:
                    problems.append("子步骤编辑器里按回车应当保存并退出")
                saved_now = [s["text"] for s in widget.store.steps_of(step_item)]
                if saved_now != ["回车保存的第一行", "回车保存的第二行"]:
                    problems.append("按回车保存的内容不对: %r" % (saved_now,))

            # 小键盘回车 / 点别处收菜单 / 右键加子步骤
            src2 = open(__file__, encoding="utf-8").read()
            kp_steps = 'box.bind("<KP_Enter>", self.' + '_steps_save_key)'
            kp_entry = 'entry.bind("<KP_Enter>"'
            if kp_steps not in src2 or kp_entry not in src2:
                problems.append("小键盘回车没接上（子步骤框和主文字框都要接 KP_Enter）")
            src3 = open(__file__, encoding="utf-8").read()
            if not widget.root.bind_all("<Button-1>"):
                problems.append("没有装「点别处收起右键菜单」的全局绑定")
            # 拼串查，免得检查自己的字面量把自己喂饱（这个坑栽过一次）
            bad_global = 'bind_all("<Button-' + '3>"'
            if bad_global in src3:
                problems.append("不许全局绑 <Button-3>：右键那一下会把刚弹出的菜单立刻收掉")
            probe_menu = widget._build_row_menu(step_item["id"])
            widget._row_menu = probe_menu
            blank = _StubEvent(x=0)
            blank.widget = widget.inner                 # 模拟"点在空白处"
            widget._dismiss_row_menu(blank)
            if widget._row_menu is not None:
                problems.append("点空白处没有收起右键菜单（引用没清）")
            elif probe_menu.winfo_exists():
                problems.append("收起右键菜单后控件没销毁（会越堆越多）")
            probe_menu2 = widget._build_row_menu(step_item["id"])
            widget._row_menu = probe_menu2
            inside = _StubEvent(x=0)
            inside.widget = probe_menu2                 # 模拟"点在菜单自己身上"
            widget._dismiss_row_menu(inside)
            if widget._row_menu is None or not probe_menu2.winfo_exists():
                problems.append("点在菜单自己身上不该把它销毁（否则菜单项点不动）")
            widget._dismiss_row_menu()                  # 清干净
            if widget._row_menu is not None:
                problems.append("收起菜单后引用没清掉")
            steps_before_add = len(widget.store.steps_of(step_item))
            widget.add_step_item(step_item["id"])
            if len(widget.store.steps_of(step_item)) != steps_before_add + 1:
                problems.append("「＋ 加一条子步骤」没有加出一条")
            widget.store.set_steps(step_item["id"],
                                   [{"text": "步骤%d" % i, "done": False}
                                    for i in range(STEP_MAX)])
            widget.add_step_item(step_item["id"])
            if len(widget.store.steps_of(step_item)) != STEP_MAX:
                problems.append("加子步骤也要受 %d 条上限约束" % STEP_MAX)

            # 全勾上（显式设成完成，别用 toggle —— 有的本来就是勾着的）：进度变强调色
            widget.store.set_steps(step_item["id"],
                                   [{"text": st["text"], "done": True}
                                    for st in widget.store.steps_of(step_item)])
            widget._refresh()
            row_all = widget._row_widget(step_item["id"])
            cell_all = steps_cell(row_all)
            if cell_all is None or str(row_all.itemcget(cell_all, "fill")) != C_ACCENT:
                problems.append("子步骤全完成时格子应是强调色（text=%r fill=%r 进度=%r）"
                                % (row_all.itemcget(cell_all, "text") if cell_all else None,
                                   row_all.itemcget(cell_all, "fill") if cell_all else None,
                                   widget.store.steps_progress(step_item)))
            # 收尾：删掉这条测试条目，后面用例面对的还是原来的列表
            widget.store.pop(step_item["id"])
            widget._refresh()

            # ---- 右侧图标区只许有「子步骤」与「删除」（钟/今/旗已删）----
            mark_item = broken.items[0]
            row_mark = widget._row_widget(mark_item["id"])
            zones = set((getattr(row_mark, "_markers", None) or {}).keys())
            if zones != {"delete", "steps"}:
                problems.append("右侧图标区应当只有 子步骤+删除，实际画了: %r" % (zones,))
            table = [name for name, _width in widget.markers]
            if table != ["delete", "steps"]:
                problems.append("尺寸表/点击判定里的格子应当只有 子步骤+删除，实际: %r"
                                % (table,))
            # 行为面：老格子所在的位置不该再被判成 wait/today/prio 区
            probes = [widget.list_w - widget.del_w - widget.wait_w // 2,
                      widget.list_w - widget.del_w - widget.wait_w - widget.today_w // 2,
                      widget.list_w - widget.del_w - widget.wait_w - widget.today_w \
                      - widget.prio_w // 2]
            for probe_x in probes:
                got_zone = widget._zone_at(probe_x)
                if got_zone in ("wait", "today", "prio"):
                    problems.append("x=%s 还被判成已删除的按钮区: %r" % (probe_x, got_zone))
            # 优先级：菜单里选 → 行左侧竖条（高＝整行、中＝半行、无＝不画）
            menu = widget._build_row_menu(mark_item["id"])
            cascade = [i for i in range(menu.index("end") + 1)
                       if "优先级" in menu_label(menu, i)]
            if not cascade:
                problems.append("右键菜单里没有「优先级」子菜单")
            else:
                sub = menu.nametowidget(menu.entrycget(cascade[0], "menu"))
                sub.invoke(0)                                  # 第一项 ＝ 高
                if widget.store.priority_of(mark_item) != PRIORITY_HIGH:
                    problems.append("菜单里选「高」没有生效: %r"
                                    % (widget.store.priority_of(mark_item),))
                if widget._row_widget(mark_item["id"]) is not row_mark:
                    problems.append("改优先级时行控件被重建了（双击会因此失效）")
                bars = row_mark.find_withtag("prio")
                row_h_now = int(row_mark.cget("height"))
                if len(bars) != 1 or str(row_mark.itemcget(bars[0], "fill")) != C_HIGH:
                    problems.append("高优先级应当在行左侧画一条实心橙竖条: %r" % (bars,))
                elif row_mark.bbox(bars[0])[3] - row_mark.bbox(bars[0])[1] < row_h_now * 0.8:
                    problems.append("高优先级的竖条应当是整行高: %r"
                                    % (row_mark.bbox(bars[0]),))
                sub.invoke(1)                                  # 中
                if widget.store.priority_of(mark_item) != PRIORITY_MID:
                    problems.append("菜单里选「中」没有生效")
                bars = row_mark.find_withtag("prio")
                if len(bars) != 1:
                    problems.append("中优先级也该有一条竖条: %r" % (bars,))
                elif row_mark.bbox(bars[0])[3] - row_mark.bbox(bars[0])[1] > row_h_now * 0.7:
                    problems.append("中优先级的竖条应当只有半行高: %r"
                                    % (row_mark.bbox(bars[0]),))
                sub.invoke(2)                                  # 无
                if widget.store.priority_of(mark_item) != PRIORITY_NONE \
                        or row_mark.find_withtag("prio"):
                    problems.append("选「无」之后竖条应当消失")
            # 今天：菜单里标记 → 正文前一个蓝点；底部显示今天进度
            menu = widget._build_row_menu(mark_item["id"])

            today_index = menu_find(menu, "今天")
            if today_index is None:
                problems.append("右键菜单里没有「今天要办的」")
            else:
                menu.invoke(today_index)
                if not mark_item.get("today"):
                    problems.append("从菜单标记「今天要办的」没有生效")
                dots = row_mark.find_withtag("today")
                if len(dots) != 1 or str(row_mark.itemcget(dots[0], "fill")) != C_ACCENT:
                    problems.append("今天的条目前面应当有一个蓝点: %r" % (dots,))
                if "今天" not in str(widget.hint.cget("text")):
                    problems.append("底部没有显示今天进度: %r" % (widget.hint.cget("text"),))
                menu = widget._build_row_menu(mark_item["id"])
                today_index = menu_find(menu, "今天")
                if today_index is not None:
                    menu.invoke(today_index)
                if mark_item.get("today"):
                    problems.append("再从菜单点一次「今天要办的」应取消标记")
            # 菜单里该有的动作都在（删除不真的点，免得弹出撤销条影响后面的提示断言）
            menu = widget._build_row_menu(mark_item["id"])
            labels = " ".join(menu_label(menu, i)
                              for i in range(menu.index("end") + 1))
            for wanted in ("等待中", "今天要办的", "优先级", "编辑子步骤",
                           "＋ 加一条子步骤", "删除这条"):
                if wanted not in labels:
                    problems.append("右键菜单缺少「%s」: %r" % (wanted, labels))
            probe = {"id": -1, "text": "x", "done": False, "state": STATE_TODO,
                     "priority": PRIORITY_NONE, "today": False}
            if widget._bucket_of(probe) == widget._bucket_of(dict(probe, priority=PRIORITY_HIGH)):
                problems.append("优先级没有参与排序桶")
            if widget._bucket_of(probe) == widget._bucket_of(dict(probe, today=True)):
                problems.append("「今天」没有参与排序桶")

            # ---- 情境标签：@ 解析 / 行尾显示 / 顶部筛选 ----
            tg = Store(os.path.join(temp_dir, "tags.json"))
            if Store.parse_tags("写周报") != ("写周报", []):
                problems.append("没标签的文字不该被改动")
            if Store.parse_tags("给张老师打电话 @电话 @外出") != ("给张老师打电话",
                                                              ["电话", "外出"]):
                problems.append("@ 解析不对: %r"
                                % (Store.parse_tags("给张老师打电话 @电话 @外出"),))
            t2_text, t2_tags = Store.parse_tags("@电话 交资料 @电话 @家 @单位 @超出的")
            if t2_tags != ["电话", "家", "单位"] or "@超出的" not in t2_text:
                problems.append("标签去重/上限不对: %r" % ((t2_text, t2_tags),))
            t3_text, t3_tags = Store.parse_tags("邮箱@qq.com 和 a.com/x@y")
            if t3_tags or "@qq.com" not in t3_text:
                problems.append("邮箱/网址不该被当成标签: %r" % ((t3_text, t3_tags),))
            ti = tg.add("整理会议纪要 @电话")
            if ti["text"] != "整理会议纪要" or Store.tags_of(ti) != ["电话"]:
                problems.append("add 没有把 @标签 摘进 tags: %r" % (ti,))
            tg.rename(ti["id"], "整理会议纪要 @电话 @外出")
            if Store.tags_of(ti) != ["电话", "外出"]:
                problems.append("rename 没有更新标签: %r" % (Store.tags_of(ti),))
            tg.rename(ti["id"], "整理会议纪要")
            if Store.tags_of(ti) != []:
                problems.append("删掉 @标签 后 tags 应当为空: %r" % (Store.tags_of(ti),))
            tg.rename(ti["id"], "整理会议纪要 @电话")
            tg.add("买菜 @外出")
            if sorted(tg.all_tags()) != ["外出", "电话"]:
                problems.append("all_tags 不对: %r" % (tg.all_tags(),))
            reload_tags = Store(os.path.join(temp_dir, "tags.json"))
            got_tags = sorted(tag for item in reload_tags.items
                              for tag in Store.tags_of(item))
            if got_tags != ["外出", "电话"]:
                problems.append("标签没有落盘: %r" % (got_tags,))
            # 界面：正文顶满一行时，行尾标签也不许压到右侧图标区
            base_char = widget.fonts["base"].measure("长") or 1
            long_text = "长" * max(4, int(widget.row_wrap // base_char))
            tagged = widget.store.add(long_text + " @电话")
            no_tag_item = widget.store.add("界面无标签条")
            widget._refresh()
            row_tag = widget._row_widget(tagged["id"])
            tag_item = getattr(row_tag, "_tag_item", None)
            if tag_item is None or str(row_tag.itemcget(tag_item, "text")) != "@电话":
                problems.append("行尾没有画出 @电话 标签: %r" % (tag_item,))
            elif row_tag.bbox(tag_item)[2] > widget._marker_zone_start():
                problems.append("行尾标签压到了右侧图标区")
            if not widget.tags_row.winfo_children():
                problems.append("有标签时顶部筛选条没有生成筛选项")
            widget.set_filter("电话")
            if [i["id"] for i in widget._visible_items()] != [tagged["id"]]:
                problems.append("筛选后可见条目不对: %r"
                                % ([i["text"][:6] for i in widget._visible_items()],))
            if "只看 @电话" not in str(widget.hint.cget("text")):
                problems.append("筛选时底部没有提示: %r" % (widget.hint.cget("text"),))
            if widget.store.remaining(widget._visible_items()) != 1:
                problems.append("筛选后的计数不对")
            widget.set_filter(None)
            if len(widget._visible_items()) != len(widget.store.items):
                problems.append("取消筛选后应当看到全部")
            widget._begin_edit(tagged["id"])
            shown = widget._edit_entry.get() if widget._edit_entry is not None else ""
            if "@电话" not in shown:
                problems.append("编辑框没有回填 @电话: %r" % (shown,))
            widget._end_edit(save=True)
            if Store.tags_of(tagged) != ["电话"]:
                problems.append("编辑保存后标签丢了: %r" % (Store.tags_of(tagged),))
            for extra_id in (tagged["id"], no_tag_item["id"]):
                widget.store.pop(extra_id)
            widget._refresh()

            # 唤回的最后一招：自己发 EWMH 的 _NET_ACTIVE_WINDOW（KWin 忽略后台应用的
            # XRaiseWindow —— Super+D 之后按快捷键拉不回来，缺的就是这一条）。
            # 放在 activate_window 之前真发一次：此刻自检窗口从没映射过、KWin 的客户列表里
            # 没有它，所以这条消息会被忽略，桌面照旧不被打扰；但整段代码（atom、XEvent
            # 结构体、XSendEvent 的签名、XFlush）都是真跑的 —— 写错了自检就会失败。
            if not _on_wm_window(widget.root, _send_active_window):
                problems.append("发不出 EWMH _NET_ACTIVE_WINDOW（Super+D 之后唤不回来就是这条断了）")

            # 唤回总闸门：写日志 + 必要时强制重映射（真调一次，看日志里有没有前后状态）。
            # notify_wm=False：上一条已经单独验过发送，这里不让它再打扰桌面一次。
            activate_window(widget.root, False, notify_wm=False)
            widget.root.withdraw()                    # 别把自检窗口真的弹出来
            try:
                with open(os.path.join(os.path.dirname(widget.store.path), "hotkey.log"),
                          encoding="utf-8") as handle:
                    log_text = handle.read()
            except IOError:
                log_text = ""
            if "activate 前:" not in log_text:
                problems.append("唤回函数没有写诊断日志（下次唤不回来还是没法定位）")
            if "remap=" not in log_text:
                problems.append("诊断日志里没有 remap 判定")
            if "activate 后:" not in log_text and "activate 失败" not in log_text:
                problems.append("唤回日志内容不对（既没有成功也没有失败记录）")

            # 诊断日志：能真写出文件（下次"唤不回来"靠它定位）
            write_diag("selftest-probe")
            log_path = os.path.join(os.path.dirname(widget.store.path), "hotkey.log")
            try:
                with open(log_path, encoding="utf-8") as handle:
                    if "selftest-probe" not in handle.read():
                        problems.append("诊断日志没有真的写出来（下次没法定位唤回问题）")
            except IOError:
                problems.append("诊断日志文件不存在")

            # ---- 归档：过 00:00 自动收走已完成的；删除进归档、可撤销 ----
            arc_path = os.path.join(temp_dir, "archive.json")
            arc = Store(arc_path)
            midnight = Store.day_start()
            old_done = arc.add("昨天就完成了的")
            arc.toggle(old_done["id"])
            old_done["done_at"] = midnight - 60           # 昨天 23:59 完成
            today_done = arc.add("今天刚完成的")
            arc.toggle(today_done["id"])
            plain = arc.add("还没做的")
            moved_count = arc.archive_completed()
            if moved_count != 1:
                problems.append("自动归档应当只收走昨天完成的那一条，实际 %r" % (moved_count,))
            left_ids = [item["id"] for item in arc.items]
            if old_done["id"] in left_ids or today_done["id"] not in left_ids \
                    or plain["id"] not in left_ids:
                problems.append("自动归档收错了: %r" % (left_ids,))
            total, counts = arc.archive_stats()
            if total != 1 or counts.get("done") != 1:
                problems.append("归档统计不对: %r" % ((total, counts),))
            reload_arc = Store(arc_path)
            if len(reload_arc.archive) != 1 \
                    or reload_arc.archive[0]["item"]["id"] != old_done["id"]:
                problems.append("归档没有落盘")
            if [item["id"] for item in reload_arc.items] != left_ids:
                problems.append("归档后的活动列表没有落盘")
            # 删除 → 进归档；撤销 → 从归档里拿回来
            victim = arc.items[0]
            record = arc.pop(victim["id"])
            if record is None or arc.archive_stats()[0] != 2:
                problems.append("删除没有把条目送进归档（数据会丢）")
            if not arc.restore(record):
                problems.append("撤销删除失败")
            if arc.archive_stats()[0] != 1 or victim["id"] not in [i["id"] for i in arc.items]:
                problems.append("撤销删除后归档里还留着那条")
            # 「归档已完成」= 归档，不是真删
            arc.clear_done()
            total, counts = arc.archive_stats()
            if counts.get("cleared") != 1:
                problems.append("「归档已完成」没有按 cleared 记进归档: %r" % (counts,))
            if any(item["state"] == STATE_DONE for item in arc.items):
                problems.append("「归档已完成」没有把已完成的挪走")
            for index in range(ARCHIVE_MAX + 20):
                arc._archive_item({"id": 900000 + index, "text": "x", "done": True,
                                   "state": STATE_DONE}, "done")
            if len(arc.archive) != ARCHIVE_MAX:
                problems.append("归档没有限制在 %d 条: %r" % (ARCHIVE_MAX, len(arc.archive)))
            # done_at：勾选时记、取消时清；老数据读取时补
            dat = Store(os.path.join(temp_dir, "done-at.json"))
            dat_item = dat.add("看 done_at")
            if dat_item.get("done_at") is not None:
                problems.append("新条目的 done_at 应当是空的")
            dat.toggle(dat_item["id"])
            if not dat_item.get("done_at"):
                problems.append("勾成已完成时没有记 done_at")
            dat.toggle(dat_item["id"])
            if dat_item.get("done_at") is not None:
                problems.append("取消完成后没有清掉 done_at")
            legacy_path = os.path.join(temp_dir, "legacy-done.json")
            legacy = Store(legacy_path)
            legacy.items.append({"id": 1, "text": "老数据已完成", "done": True,
                                 "state": STATE_DONE, "priority": PRIORITY_NONE,
                                 "today": False, "created": time.time()})
            legacy.save()
            if not Store(legacy_path).items[0].get("done_at"):
                problems.append("老数据里已完成的条目读取时要补 done_at，否则一升级就被归档")
            # 界面：定时器在跑；跨天那一眼真的把行收走；删除可撤销
            if hasattr(widget, "_archive_job") or hasattr(widget, "_archive_timer_tick"):
                problems.append("不该再有周期性的归档定时器（只在启动时看一次）")
            if widget._archive_tick() != 0:
                problems.append("同一天里再叫一次归档应当什么都不做（一天最多一次）")
            widget.store.items.insert(0, {"id": 424242, "text": "昨天完成的界面条",
                                          "steps": [], "done": True, "state": STATE_DONE,
                                          "priority": PRIORITY_NONE, "today": False,
                                          "done_at": Store.day_start() - 3600,
                                          "created": time.time()})
            widget._archived_day = None
            moved_ui = widget._archive_tick()
            if moved_ui != 1 or widget._row_widget(424242) is not None:
                problems.append("跨天那一眼没有把已完成的行收走: %r" % (moved_ui,))
            if not any(rec["item"]["id"] == 424242 for rec in widget.store.archive):
                problems.append("界面归档的那条没有进 archive")
            undo_victim = widget.store.items[0]
            widget.remove_item(undo_victim["id"])
            if widget._undo is None or "已删除" not in str(widget.hint.cget("text")):
                problems.append("删掉之后没有出现撤销提示")
            if str(widget.undo_btn.cget("text")) != "撤销删除":
                problems.append("撤销按钮文案不对: %r" % (widget.undo_btn.cget("text"),))
            widget.undo_delete()
            if undo_victim["id"] not in [i["id"] for i in widget.store.items]:
                problems.append("点撤销没有把删除的条目放回来")
            if any(rec["item"]["id"] == undo_victim["id"] for rec in widget.store.archive):
                problems.append("撤销之后归档里还留着那条")

            # ---- 归档浏览器：右键标题栏 →「查看归档…」（只读列表 + 逐条恢复）----
            saved_archive = list(widget.store.archive)
            now_ts = time.time()

            def _arch_record(item_id, text, reason, state, age):
                return {"item": {"id": item_id, "text": text, "steps": [], "tags": [],
                                 "done": state == STATE_DONE, "state": state,
                                 "priority": PRIORITY_NONE, "today": False,
                                 "done_at": None, "created": now_ts},
                        "archived_at": now_ts - age, "reason": reason, "index": 0}

            widget.store.archive = [
                _arch_record(700001, "昨天完成的", "done", STATE_DONE, 300),
                _arch_record(700002, "手动归档的", "cleared", STATE_DONE, 200),
                _arch_record(700003, "删掉的那条", "deleted", STATE_TODO, 100),
            ]
            viewer = ArchiveWindow(widget, visible=False)
            if len(viewer._rows) != 3:
                problems.append("归档窗口没把 3 条归档都列出来: %r" % (len(viewer._rows),))
            if viewer.win.winfo_ismapped():
                problems.append("visible=False 的归档窗口不该被映射出来（会打扰桌面）")

            def _all_labels(parent):
                found = []
                for child in parent.winfo_children():
                    if isinstance(child, tk.Label):
                        found.append(str(child.cget("text")))
                    found.extend(_all_labels(child))
                return found

            texts = _all_labels(viewer.inner)
            for wanted in ("完成", "清掉", "删除"):
                if wanted not in texts:
                    problems.append("归档窗口里没有「%s」这个原因标签: %r" % (wanted, texts))
            if "删掉的那条" not in texts:
                problems.append("归档窗口里没有列出条目正文: %r" % (texts,))
            # 恢复「删除」的那一条：回到列表、从归档里消失、窗口少一行、按状态归位
            deleted_rec = [r for r in widget.store.archive if r["reason"] == "deleted"][0]
            if not viewer.restore(deleted_rec):
                problems.append("从归档窗口恢复一条失败")
            elif not any(item["id"] == 700003 for item in widget.store.items):
                problems.append("恢复后条目没有回到列表")
            elif any(r["item"]["id"] == 700003 for r in widget.store.archive):
                problems.append("恢复后归档里还留着这一条")
            elif len(viewer._rows) != 2:
                problems.append("恢复后归档窗口没有少一行: %r" % (len(viewer._rows),))
            if Store(path).items and not any(i["id"] == 700003 for i in Store(path).items):
                problems.append("恢复的那一条没有落盘")
            # 同一条再恢复一次：应当失败，且不能插两遍
            if viewer.restore(deleted_rec):
                problems.append("已经恢复过的条目，再恢复一次应当失败")
            if len([i for i in widget.store.items if i["id"] == 700003]) != 1:
                problems.append("重复恢复把条目插了两遍")
            viewer.close()
            if viewer.win.winfo_exists():
                problems.append("归档窗口关闭后控件没销毁")
            if widget._archive_window is not None:
                problems.append("归档窗口关闭后主窗口还留着引用")
            widget.store.remove(700003)           # 收尾：别把恢复进来的测试条留在列表里
            widget.store.archive = saved_archive
            widget._refresh()
            # 页头右键菜单里必须有入口
            head_labels = " ".join(menu_label(widget.menu, i)
                                   for i in range(widget.menu.index("end") + 1))
            if "查看归档" not in head_labels:
                problems.append("页头右键菜单里没有「查看归档…」入口: %r" % (head_labels,))

            # ---- 尺寸：默认排版不能变，拉大后要跟着重排 ----
            default_w = _scaled(PANEL_W, widget.s)
            default_h = _scaled(LIST_MAX_H, widget.s)
            if widget.panel_w != default_w:
                problems.append("默认面板宽度不对: %r（期望 %r）" % (widget.panel_w, default_w))
            if widget.list_max_h != default_h:
                problems.append("默认列表高度上限不对: %r（期望 %r）"
                                % (widget.list_max_h, default_h))
            narrow_wrap = widget.row_wrap
            widget._apply_metrics(default_w + 200)
            if widget.panel_w != default_w + 200:
                problems.append("拉宽后没有按新宽度重算: %r" % (widget.panel_w,))
            if widget.row_wrap <= narrow_wrap:
                problems.append("拉宽后换行宽度没有跟着变大: %r → %r"
                                % (narrow_wrap, widget.row_wrap))
            widget._apply_metrics()
            if widget.panel_w != default_w or widget.row_wrap != narrow_wrap:
                problems.append("还原默认尺寸失败: %r / %r"
                                % (widget.panel_w, widget.row_wrap))
            if widget._list_space(default_h * 3) <= default_h:
                problems.append("窗口拉高后没有把多出来的高度给列表")

            # ---- 默认排序：未完成 → 等待中 → 已完成 ----
            for entry in list(broken.items):          # 先把状态归一，免得受前面用例影响
                if broken.state_of(entry) == STATE_WAITING:
                    broken.toggle_waiting(entry["id"])
                elif broken.state_of(entry) == STATE_DONE:
                    broken.toggle(entry["id"])
            widget._refresh()
            widget._cancel_regroup()                  # 先清干净，免得受前面用例遗留的任务影响
            first_todo = broken.items[0]
            order_before = [entry["id"] for entry in broken.items]
            widget.toggle_item(first_todo["id"])
            if [entry["id"] for entry in broken.items] != order_before:
                problems.append("勾选后立刻就把行挪走了（双击改名会因此落空）")
            if widget._regroup_job is None:
                problems.append("勾选后没有安排归位")
            widget._regroup_now()
            if broken.state_of(broken.items[-1]) != STATE_DONE:
                problems.append("归位后已完成条目不在最后: %r"
                                % ([broken.state_of(e) for e in broken.items],))
            if widget._regroup_job is not None:
                problems.append("归位之后还留着待执行任务")
            # 拖拽不跨段：落点被夹在本状态段内
            widget._refresh()
            todo_items = [e for e in broken.items if broken.state_of(e) == STATE_TODO]
            done_indexes = [i for i, e in enumerate(broken.items)
                            if broken.state_of(e) == STATE_DONE]
            if todo_items and done_indexes:
                if widget._clamp_to_group(todo_items[0]["id"], 999) >= done_indexes[0]:
                    problems.append("拖拽落点没有被限制在本状态段内")
                if widget._clamp_to_group(todo_items[0]["id"], -5) != 0:
                    problems.append("拖拽落点下界没有被夹住")
                # 真拖一次：往「已完成」那一段里拖，不能跨过去（跨段只能靠勾选/小钟）
                dragged = todo_items[0]["id"]
                done_row = widget._row_widget(broken.items[done_indexes[0]]["id"])
                widget._begin_drag(dragged)
                widget._drag_to(done_row.winfo_rooty() + 1)
                widget._end_drag()
                after_items = list(broken.items)
                dragged_at = [i for i, e in enumerate(after_items) if e["id"] == dragged]
                done_first = [i for i, e in enumerate(after_items)
                              if broken.state_of(e) == STATE_DONE]
                if dragged_at and done_first and dragged_at[0] >= done_first[0]:
                    problems.append("拖拽把条目拖进了别的状态段: %r"
                                    % ([e["text"] for e in after_items],))
            # 编辑期间不归位（归位会重建行、把输入框一起销毁）
            edit_target = [e for e in broken.items if broken.state_of(e) == STATE_TODO]
            if edit_target:
                edit_id = edit_target[0]["id"]
                widget.toggle_item(edit_id)           # 变成已完成，安排归位
                widget._begin_edit(edit_id, _StubEvent(x=body_x))
                if widget._regroup_job is not None:
                    problems.append("进入编辑时没有取消失效的归位任务")
                widget._regroup_now()                 # 编辑中触发：应顺延，不该重画
                if widget._edit_entry is None:
                    problems.append("编辑中触发归位把输入框销毁了")
                if widget._regroup_job is None:
                    problems.append("编辑中触发归位后没有顺延")
                widget._end_edit(save=False)
            # 到点提醒：横幅出现 + 清掉提醒（否则每 15 秒响一次）+ 「知道了」收起
            alert_target = [e for e in broken.items if broken.state_of(e) == STATE_TODO]
            if alert_target:
                broken.set_reminder(alert_target[0]["id"], time.time() - 1)
                widget._remind_tick(raise_it=False)
                if "⏰" not in str(widget.hint.cget("text")):
                    problems.append("到点后没有弹出提醒横幅: %r"
                                    % (widget.hint.cget("text"),))
                if widget.alert_btn.winfo_manager() != "pack":
                    problems.append("到点后没有显示「知道了」")
                if broken.due_reminders():
                    problems.append("到点提醒没有被清掉，会重复响")
                widget._dismiss_alert()
                if widget.alert_btn.winfo_manager() == "pack":
                    problems.append("点「知道了」后横幅没有收起")
            root.update()
            root.withdraw()
            root.update()
            root.destroy()
        except Exception as error:      # 不限于 TclError：代码里的笔误也要在这里暴露
            import traceback
            problems.append("界面/拖拽自检失败: %r\n%s"
                            % (error, traceback.format_exc()))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if problems:
        for problem in problems:
            sys.stderr.write("自检失败: %s\n" % problem)
        return 1
    print("SELFTEST OK")
    return 0


# --------------------------------------------------------------------------- #
# 诊断（--doctor）
#
# 用途：把「必须盯着桌面看一眼」才能确认的事，压成一段可以复制粘贴回来的文本
# （排障时贴给别人 / 贴进 issue 都行）。每个能力都尽量「写进去、再读回来」验证，
# 而不是「调用没报错就算成功」。
# 只读：不改数据、不写配置；只额外建一个隐藏窗口做探测。
# --------------------------------------------------------------------------- #
def _x11_read_property(x11, display, window, atom, length=8):
    """读回一个 32 位 X 属性，返回整数列表；读不到返回 []。"""
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.POINTER(ctypes.c_ulong)()
    try:
        status = x11.XGetWindowProperty(
            ctypes.c_void_p(display), ctypes.c_ulong(window), ctypes.c_ulong(atom),
            0, length, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(prop))
    except Exception:
        return []
    if status != 0 or not prop:
        return []
    values = []
    if nitems.value:
        values = [int(prop[index]) for index in range(int(nitems.value))]
    try:
        x11.XFree(prop)
    except Exception:
        pass
    return values


def _doctor_x11(root, rows, caps):
    """X11 侧探测：把 Motif / EWMH 属性真的写进去，再读回来确认。"""
    seen = {}

    def probe_motif(x11, display, target, root_window):
        atom = x11.XInternAtom(ctypes.c_void_p(display), b"_MOTIF_WM_HINTS", 0)
        values = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(atom), ctypes.c_ulong(atom), 32, 0,
                            ctypes.byref(values), 5)
        seen["target"] = int(target)
        seen["motif"] = _x11_read_property(x11, display, target, atom)

    def probe_state(x11, display, target, root_window):
        state_atom = x11.XInternAtom(ctypes.c_void_p(display), b"_NET_WM_STATE", 0)
        skip_taskbar = x11.XInternAtom(ctypes.c_void_p(display),
                                       b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pager = x11.XInternAtom(ctypes.c_void_p(display),
                                     b"_NET_WM_STATE_SKIP_PAGER", 0)
        atoms = (ctypes.c_ulong * 2)(skip_taskbar, skip_pager)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(state_atom), ctypes.c_ulong(4), 32, 0,
                            ctypes.byref(atoms), 2)
        seen["skip_wanted"] = [int(skip_taskbar), int(skip_pager)]
        seen["skip_read"] = _x11_read_property(x11, display, target, state_atom)

    if not _on_wm_window(root, probe_motif):
        rows.append("[x11]   连接不可用：libX11.so.6 缺失 / DISPLAY 无效 / 窗口还没建出来")
        caps.append(("去标题栏（Motif）", "N/A", "没有可用的 X 连接"))
        caps.append(("不进任务栏（EWMH）", "N/A", "没有可用的 X 连接"))
        return

    rows.append("[x11]   WM 顶层窗口 = 0x%x" % seen.get("target", 0))
    motif = seen.get("motif") or []
    if len(motif) >= 5 and motif[0] == 2 and motif[2] == 0:
        rows.append("[x11]   _MOTIF_WM_HINTS 回读 = %r（decorations=0）" % (motif,))
        caps.append(("去标题栏（Motif）", "OK", "回读确认 decorations=0"))
    else:
        rows.append("[x11]   _MOTIF_WM_HINTS 回读 = %r（空或不符合预期）" % (motif,))
        caps.append(("去标题栏（Motif）", "WARN", "回读 %r" % (motif,)))

    _on_wm_window(root, probe_state)
    wanted = seen.get("skip_wanted") or []
    got = seen.get("skip_read") or []
    if wanted and all(atom in got for atom in wanted):
        rows.append("[x11]   _NET_WM_STATE 回读含 SKIP_TASKBAR + SKIP_PAGER")
        caps.append(("不进任务栏（EWMH）", "OK", "SKIP_TASKBAR/SKIP_PAGER 已写入"))
    else:
        rows.append("[x11]   _NET_WM_STATE 回读 = %r（期望包含 %r）" % (got, wanted))
        caps.append(("不进任务栏（EWMH）", "WARN", "回读 %r" % (got,)))

    rows.append("[x11]   映射后 ClientMessage 补发 = %s" % set_skip_taskbar(root))
    filehandler = hasattr(root.tk, "createfilehandler")
    rows.append("[x11]   tk.createfilehandler 可用 = %s" % filehandler)
    caps.append(("单实例唤回（filehandler）", "OK" if filehandler else "WARN",
                 "unix 套接字 + Tk 文件事件"))


def _doctor_instance(rows, caps):
    """单实例与唤回机制的探测：AF_UNIX 套接字能不能绑定。"""
    path = ipc_socket_path()
    rows.append("[ipc]   套接字路径 = %s（已存在=%s）" % (path, os.path.exists(path)))
    probe = path + ".doctor"
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(probe):
            os.unlink(probe)
        server.bind(probe)
        server.listen(1)
        server.close()
        os.unlink(probe)
        caps.append(("单实例", "OK", "AF_UNIX 可绑定"))
    except Exception as error:
        caps.append(("单实例", "FAIL", repr(error)))


def run_doctor():
    """打印一份可粘贴的诊断报告（只读，不改数据/配置）。"""
    rows, caps = [], []

    def row(section, key, value):
        rows.append("[%s] %s = %s" % (section, key, value))

    # ---- 环境与路径 ----
    row("env", "os / sys.platform", "posix / %s" % sys.platform)
    row("env", "窗口外观", "X11（Motif 去标题栏 + EWMH SKIP_TASKBAR/SKIP_PAGER）")
    row("env", "唤回方式", "全局快捷键 / 再运行一次本程序 / 应用菜单")
    row("env", "python", sys.version.split()[0])
    row("env", "脚本路径", os.path.abspath(__file__))
    row("env", "tk", "Tk %s" % tk.TkVersion if tk else "缺失（统信 UOS: apt install python3-tk）")
    row("env", "DISPLAY", os.environ.get("DISPLAY") or "(未设置)")
    row("env", "WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY") or "(未设置)")
    row("env", "XDG_DATA_HOME", os.environ.get("XDG_DATA_HOME") or "(未设置)")
    row("env", "XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR") or "(未设置)")
    row("path", "数据文件", data_file())
    row("path", "单实例套接字", ipc_socket_path())
    row("path", "开机自启", autostart_path())
    app_entry = app_entry_target()
    row("path", "应用菜单条目", "%s（%s）" % (
        app_entry, "已存在" if os.path.exists(app_entry) else "不存在"))

    # ---- 数据文件（只读）----
    path = data_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            items = payload.get("items") or []

            def _state_of(entry):
                value = str(entry.get("state") or "")
                if value in (STATE_TODO, STATE_WAITING, STATE_DONE):
                    return value
                return STATE_DONE if entry.get("done") else STATE_TODO

            done = len([item for item in items if _state_of(item) == STATE_DONE])
            waiting = len([item for item in items if _state_of(item) == STATE_WAITING])
            row("data", "条目", "%d 条（未完成 %d / 等待中 %d / 已完成 %d）"
                % (len(items), len(items) - done - waiting, waiting, done))
            caps.append(("数据文件可读", "OK", "%d 条" % len(items)))
        except Exception as error:
            row("data", "读取", "失败：%r" % (error,))
            caps.append(("数据文件可读", "FAIL", repr(error)))
    else:
        row("data", "条目", "文件不存在（首次运行会创建）")
        caps.append(("数据文件可读", "N/A", "还没有数据文件"))

    directory = data_dir()
    if os.path.isdir(directory):
        caps.append(("数据目录可写", "OK" if os.access(directory, os.W_OK) else "FAIL",
                     directory))
    else:
        parent = os.path.dirname(directory)
        caps.append(("数据目录可写", "OK" if os.access(parent, os.W_OK) else "WARN",
                     "未建，父目录 %s" % parent))

    # ---- 单实例 / 唤回 ----
    _doctor_instance(rows, caps)

    # ---- 开机自启：Deepin/UOS 上两个文件必须配套，否则 startdde 会删掉自启项 ----
    autostart = autostart_path()
    app_entry = app_entry_target()
    has_auto = os.path.exists(autostart)
    has_app = os.path.exists(app_entry)
    if has_auto and has_app:
        caps.append(("开机自启", "OK", "自启项 + 应用条目都在"))
    elif has_auto and not has_app:
        caps.append(("开机自启", "WARN",
                     "缺应用条目，Deepin/UOS 的 startdde 会把自启项删掉"))
    elif has_app and not has_auto:
        caps.append(("开机自启", "WARN", "有应用条目但还没装自启项"))
    else:
        caps.append(("开机自启", "N/A", "尚未安装（--install-autostart）"))

    # ---- 窗口能力（需要一个隐藏窗口）----
    root = None
    if tk is None:
        row("tk", "tkinter", "缺失：界面与窗口能力检查全部跳过")
        caps.append(("Tk 窗口", "FAIL",
                     "缺少 tkinter（统信 UOS / Debian: sudo apt install python3-tk）"))
        caps.append(("去标题栏", "N/A", "缺少 tkinter"))
        caps.append(("不进任务栏", "N/A", "缺少 tkinter"))
        caps.append(("中文字体", "N/A", "缺少 tkinter"))
    else:
        try:
            root = tk.Tk()
            root.title("desktop-todo doctor")
            root.withdraw()
            root.update_idletasks()
        except Exception as error:
            row("tk", "创建隐藏窗口", "失败：%r" % (error,))
            caps.append(("Tk 窗口", "FAIL", repr(error)))

    if root is not None:
        caps.append(("Tk 窗口", "OK", "Tk %s" % tk.TkVersion))
        try:
            scaling = float(root.tk.call("tk", "scaling"))
            factor = ui_scale(root)
            row("scale", "tk scaling", "%.3f" % scaling)
            row("scale", "界面缩放", "%.2f 倍（面板宽 %d px）"
                % (factor, _scaled(PANEL_W, factor)))
        except Exception:
            pass
        try:
            _doctor_x11(root, rows, caps)
        except Exception as error:
            row("probe", "窗口能力探测", "异常：%r" % (error,))
        # ---- 全局快捷键：真去抓一次，再解开（抓不上通常是键被别的程序/已有实例占了）----
        hotkey_spec = HOTKEY_DEFAULT
        try:
            hotkey_spec = Store(data_file()).ui.get("hotkey") or HOTKEY_DEFAULT
        except Exception:
            pass
        installed = False
        try:
            installed = hotkey_install(root, hotkey_spec, lambda stamp=None: None)
        except Exception as error:
            row("hotkey", "注册", "异常：%r" % (error,))
        finally:
            try:
                hotkey_remove()
            except Exception:
                pass
        row("hotkey", "全局快捷键", "%s（%s）" % (
            format_hotkey(hotkey_spec),
            "注册成功" if installed else
            "没注册上：要么已经有实例在跑（它正占着这个键），要么被别的程序占了；"
            "换一个用 --hotkey Ctrl+Alt+K"))
        caps.append(("全局快捷键", "OK" if installed else "WARN",
                     format_hotkey(hotkey_spec)))
        try:
            families = set(tkfont.families())
            chosen = None
            for name in FONT_STACK:
                if name in families:
                    chosen = name
                    break
            missing = [name for name in FONT_STACK if name not in families]
            row("font", "命中", chosen or "(都没命中，用 Tk 默认字体)")
            row("font", "未命中", ", ".join(missing) or "无")
            caps.append(("中文字体", "OK" if chosen else "WARN", chosen or "回落 Tk 默认"))
        except Exception as error:
            row("font", "枚举", "失败：%r" % (error,))
            caps.append(("中文字体", "FAIL", repr(error)))
        try:
            root.destroy()
        except Exception:
            pass

    # ---- 报告 ----
    print("=" * 68)
    print("desktop-todo %s 诊断报告（--doctor）" % APP_VERSION)
    print("=" * 68)
    for line in rows:
        print(line)
    print("-" * 68)
    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "N/A": 0}
    for name, verdict, detail in caps:
        counts[verdict] = counts.get(verdict, 0) + 1
        print("%-4s %-26s %s" % (verdict, name, detail))
    print("-" * 68)
    print("结论：OK %d / WARN %d / FAIL %d / N/A %d"
          % (counts["OK"], counts["WARN"], counts["FAIL"], counts["N/A"]))
    print("DOCTOR DONE")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="桌面待办清单挂件")
    parser.add_argument("--version", action="version",
                        version="desktop-todo %s" % APP_VERSION)
    parser.add_argument("--check", action="store_true", help="自检数据读写与界面构造后退出")
    parser.add_argument("--doctor", action="store_true", help="打印环境/窗口能力诊断报告后退出")
    parser.add_argument("--install-autostart", action="store_true", help="写入 ~/.config/autostart 开机自启")
    parser.add_argument("--uninstall-autostart", action="store_true", help="移除开机自启")
    parser.add_argument("--data-file", action="store_true", help="打印数据文件路径后退出")
    parser.add_argument("--hotkey", metavar="键位",
                        help="设置全局唤回快捷键后退出（如 Super+T、Ctrl+Alt+K）")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()
    if args.doctor:
        return run_doctor()
    if args.install_autostart:
        return autostart_install()
    if args.uninstall_autostart:
        return autostart_uninstall()
    if args.data_file:
        print(data_file())
        return 0
    if args.hotkey:
        if parse_hotkey(args.hotkey) is None:
            sys.stderr.write("认不出这个快捷键: %s（写成 Super+T / Ctrl+Alt+K / F5 这样）\n"
                             % args.hotkey)
            return 2
        store = Store(data_file())
        store.ui["hotkey"] = args.hotkey
        store.save()
        print("全局唤回快捷键 = %s（下次启动生效；冲突了可以换一个）"
              % format_hotkey(args.hotkey))
        return 0

    if tk is None:
        sys.stderr.write("缺少 tkinter，无法启动界面（统信 UOS / Debian: "
                         "sudo apt install python3-tk）\n")
        return 1

    if raise_running_instance():
        print("桌面待办清单已经在运行，已把它叫到最前面（任务栏里没有它，这是唤回方式）。")
        return 0

    # WM_CLASS 用 APP_ID，与 .desktop 的 StartupWMClass 对上（Deepin/UOS 靠它认应用）
    root = tk.Tk(className=APP_ID)
    widget = TodoWidget(root, Store(data_file()))
    if os.environ.get("DESKTOP_TODO_VERBOSE"):
        # 默认不输出；排障时设 DESKTOP_TODO_VERBOSE=1 就能看到快捷键挂上没有
        # （重定向到文件时 stdout 是块缓冲，所以每行都 flush）
        print("全局快捷键 = %s（%s）" % (
            format_hotkey(getattr(widget, "_hotkey_spec", "")),
            "已注册" if getattr(widget, "_hotkey_on", False) else "没注册上"), flush=True)
        print("数据文件 = %s" % data_file(), flush=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
