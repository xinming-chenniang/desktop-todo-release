#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面待办清单挂件（统信 UOS / Deepin X11 + GTK3）。

一个常驻桌面、无边框的小窗口：回车添加、点勾选完成、右键条目标记「等待中」，
点 × 删除，所有改动立即写入 JSON，重开自动恢复待办与窗口位置。

每件事有三种状态：未完成 / 等待中（已着手，但在等别人反馈，自己现在动不了）/ 已完成。

删掉、归档的条目都不会丢：右键标题栏 →「查看归档…」能翻出来，并挑一条恢复回列表。

默认**不置顶**：它就是普通窗口，会被别的窗口盖住，不会一直挡在前面；
需要时在右键菜单里勾「始终置顶」（勾选状态会记住）。

它也**不在任务栏 / dock 里显示图标**（GTK 的 skip-taskbar / skip-pager 提示）。
被别的窗口盖住后，按全局快捷键（默认 `Super+T`，可用 `--hotkey` 改）、用 `Alt+Tab`，
或者**再运行一次本程序**，都能把它叫到最前面。

界面用 GTK3 原生控件（Pango 排版、主题 symbolic 图标、CSS 样式），不再自绘 Canvas：
常驻内存与交互延迟都是原生量级，也不再需要 4× 超采样 PNG 遮锯齿。

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
    右键条目          等待中 / 今天要办的 / 优先级 / 提醒 / 子步骤 / 删除
    按住条目上下拖     调整顺序（松手即保存，只在同一状态组内）
    双击条目          改文字
    点 ×             删除该条（8 秒内可撤销）
    拖标题栏          移动窗口
    双击标题栏        折叠 / 展开（右键标题栏还有菜单，含「查看归档…」）
    Esc              清空输入框

数据文件: ``~/.local/share/desktop-todo/todos.json``
"""

import argparse
import csv
import ctypes
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk, Pango
except (ImportError, ValueError):      # 没装 python3-gi 时 --doctor / --data-file 仍要能跑
    gi = None
    Gdk = GLib = Gtk = Pango = None

APP_ID = "desktop-todo"
# 商店规范要求倒置域名做包名；窗口类名/数据目录仍用 APP_ID，避免老用户数据搬家
APP_STORE_ID = "com.github.xinming-chenniang.desktoptodo"
APP_VERSION = "0.5.0"      # 发布版本；--version 打印它，--doctor 报告头部也带着它
DATA_VERSION = 1           # todos.json 的格式版本；改结构时 +1，并在 MIGRATIONS 里补迁移
MIGRATIONS = {}            # {旧版本号: 函数(raw)->raw}；目前没有历史格式，先留好升级路径


def _lang_is_en():
    """LANG / LC_ALL 以 en 开头即视为英文界面（只读环境变量，不改系统配置）。"""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = (os.environ.get(key) or "").strip().lower()
        if value:
            return value.startswith("en")
    return False


def tr(zh, en):
    """最小字符串表：英文环境返回英文，否则中文。界面与 CLI 共用。"""
    return en if _lang_is_en() else zh


class _TkCompat:
    """旧 X11 集成代码里 ``except tk.TclError`` / ``tk.READABLE`` 的兼容壳。

    那批代码（去标题栏 / 不进任务栏 / EWMH 唤回 / 全局快捷键 / 单实例）是 Tk 时代
    在 KWin 上逐条验出来的，换 GUI 工具箱时原样保留、只补一层 root 适配
    （见 GtkRootShim）；现在不会再有 Tcl 错误，保留异常类型只为让那些 except 能写。
    """

    class TclError(Exception):
        pass

    READABLE = 1


tk = _TkCompat()

# ---------- 尺寸 ----------
PANEL_W = 360              # 面板默认宽度（像素）
PANEL_W_MIN = 360          # 允许拉到的最窄
PANEL_W_MAX = 520          # 允许拉到的最宽；再宽也只是窗口变宽，内容列限宽居中
LIST_MAX_RATIO = 0.70      # 列表最多占屏幕高度的比例，超出滚动

# ---------- 模型常量（Store / 自检共用，和 v0.3.0 一致）----------
UNDO_SECONDS = 8           # 删除后「撤销」保留的秒数
ARCHIVE_MAX = 500          # 归档最多留多少条（超了丢最老的）
TAG_MAX = 3                 # 一条最多几个情境标签
TAG_LEN = 8                 # 单个标签最长几个字
TAG_RE = re.compile(r"(?:^|\s)@([\w\u4e00-\u9fff]{1,%d})(?=\s|$)" % TAG_LEN)
DOUBLE_CLICK_REVERT_S = 0.8     # 双击进入编辑时，把第一次单击的勾选撤回的时间窗
REMIND_POLL_MS = 15000          # 到点提醒的检查间隔（15 秒；只在主窗口里跑）
REMIND_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*$")

# 条目状态：未完成 / 等待中 / 已完成
# 「等待中」= 已经着手了，但在等别人反馈（现在自己动不了），所以既不算完成、也不该被当成待办催自己。
STATE_TODO = "todo"
STATE_WAITING = "waiting"
STATE_DONE = "done"
# 列表默认排序：未完成 → 等待中 → 已完成（同组内保持手动拖出来的顺序）
STATE_ORDER = (STATE_TODO, STATE_WAITING, STATE_DONE)
STEP_MAX = 20               # 一条待办最多几个子步骤（防止数据被灌爆）

# 优先级：0 无 / 1 中 / 2 高（值越大越靠前）；点一下按这个顺序循环
PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH = 0, 1, 2
PRIORITY_CYCLE = (PRIORITY_NONE, PRIORITY_HIGH, PRIORITY_MID)

PLACEHOLDER = "记一条，回车保存"


_GTK_READY = [False]


def screen_size():
    """屏幕尺寸（用显示器的 geometry；Gdk.Screen.get_width 已被废弃）。"""
    display = Gdk.Display.get_default() if Gdk is not None else None
    if display is None:
        return 1920, 1080
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    if monitor is None:
        return 1920, 1080
    geometry = monitor.get_geometry()
    return int(geometry.width), int(geometry.height)


def set_window_opacity(win, value):
    """设置窗口透明度（gtk_window_set_opacity 在 3.24 标了 deprecated，但 X11 上仍然有效）。"""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            win.set_opacity(max(0.3, min(1.0, float(value))))
    except Exception:
        pass


def gtk_ready():
    """初始化 GTK（幂等）。没装 gi / 连不上显示时返回 False，调用方据此给提示。"""
    if Gtk is None:
        return False
    if _GTK_READY[0]:
        return True
    try:
        result = Gtk.init_check(None)
        ok = result[0] if isinstance(result, (tuple, list)) else bool(result)
    except Exception:
        ok = False
    _GTK_READY[0] = bool(ok)
    return _GTK_READY[0]


# --------------------------------------------------------------------------- #
# Tk 风格 root 适配层
#
# 旧的系统集成代码（本文件"集成层"一节）签名都是 Tk 的 root：
# winfo_id / state / deiconify / lift / withdraw / attributes / focus_force / after /
# tk.createfilehandler。这里把 Gtk.Window 包成同样的样子，那批代码一行不用改。
# --------------------------------------------------------------------------- #
class _TkFileHandlers(object):
    """``root.tk.createfilehandler`` 的 GObject 版本（用 GLib.io_add_watch 实现）。"""

    def __init__(self):
        self.sources = {}

    def createfilehandler(self, sock, _mode, callback):
        def on_io(_fd, _condition, sock=sock, callback=callback):
            try:
                callback(sock, _condition)
            except Exception:
                pass
            return True

        self.sources[sock] = GLib.io_add_watch(sock.fileno(), GLib.IO_IN, on_io)

    def deletefilehandler(self, sock):
        source = self.sources.pop(sock, None)
        if source is not None:
            GLib.source_remove(source)


class GtkRootShim(object):
    """把 Gtk.Window 包成旧集成代码认识的 Tk 风格 root（只实现它们用到的方法）。"""

    def __init__(self, win):
        self.win = win
        self.tk = _TkFileHandlers()

    # ---- 身份 ----
    def winfo_id(self):
        gdk_window = self.win.get_window()
        return int(gdk_window.get_xid()) if gdk_window is not None else 0

    # ---- 状态 ----
    def state(self):
        gdk_window = self.win.get_window()
        if gdk_window is None or not self.win.get_visible():
            return "withdrawn"
        if gdk_window.get_state() & Gdk.WindowState.ICONIFIED:
            return "iconic"
        return "normal"

    def winfo_viewable(self):
        # 注意：PyGObject 的 Gdk.WindowState 没有 VIEWABLE 这个成员（C 里有、没导出来），
        # 用它会让 activate_window 直接抛 AttributeError，而旧 _poll 的 except Exception
        # 会把它吞掉 —— 表现就是「快捷键注册了但按了没反应」。用 get_mapped() 判断。
        return bool(self.win.get_visible() and self.win.get_mapped())

    def update_idletasks(self):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    # ---- 动作 ----
    def deiconify(self):
        self.win.deiconify()
        self.win.present()

    def withdraw(self):
        self.win.hide()

    def lift(self):
        self.win.present()

    def focus_force(self):
        try:
            self.win.present_with_time(Gtk.get_current_event_time() or 0)
        except Exception:
            self.win.present()

    def attributes(self, name, value):
        if name == "-topmost":
            self.win.set_keep_above(bool(value))
        elif name == "-alpha":
            self.win.set_opacity(float(value))

    def after(self, milliseconds, callback, *args):
        def fire():
            try:
                callback(*args)
            except Exception:
                pass
            return False

        return GLib.timeout_add(max(1, int(milliseconds)), fire)

    def after_cancel(self, job_id):
        try:
            GLib.source_remove(job_id)
        except Exception:
            pass


# ===========================================================================
# 集成层：X11 唤回 / 全局快捷键 / 单实例 / 开机自启 / 数据路径
#
# 这一节由 v0.3.0（tkinter 版）原样继承：它在 KWin / startdde 上逐条验证过，
# 只把 root 换成 GtkRootShim（见文件头）。不要在没有真机复验的情况下改。
# ===========================================================================

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


def parse_todotxt(text):
    """把 todo.txt 内容解析成 [{"text","done","priority","due"}]。

    支持：``x `` 完成前缀（可带日期）、``(A)``/``(B)`` 优先级、
    ``@context``（留在正文里，交给 Store.parse_tags）、``due:YYYY-MM-DD``（转成当天 09:00 提醒）。
    """
    import datetime

    items = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        done = False
        priority = PRIORITY_NONE
        if line.startswith("x "):
            done = True
            line = line[2:].strip()
            # 完成行常带一到两个日期前缀
            line = re.sub(r"^\d{4}-\d{2}-\d{2}\s+(?:\d{4}-\d{2}-\d{2}\s+)?", "", line)
        match = re.match(r"^\(([A-Z])\)\s+", line)
        if match:
            priority = PRIORITY_HIGH if match.group(1) == "A" else PRIORITY_MID
            line = line[match.end():]
        due_at = None
        due = re.search(r"\bdue:(\d{4})-(\d{2})-(\d{2})\b", line)
        if due:
            try:
                due_at = datetime.datetime(int(due.group(1)), int(due.group(2)),
                                           int(due.group(3)), 9, 0).timestamp()
            except ValueError:
                due_at = None
            line = re.sub(r"\s*\bdue:\d{4}-\d{2}-\d{2}\b", "", line).strip()
        if not line:
            continue
        items.append({"text": line, "done": done,
                      "priority": priority, "due": due_at})
    return items


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
        return tr("明天 ", "tomorrow ") + clock
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


def single_instance_start(root, on_raise, on_add=None):
    """监听套接字：第二个进程发 raise 唤回窗口，发 add\t<文本> 就在运行中的实例里加一条。"""
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
        data = b""
        try:
            data = connection.recv(4096)
            connection.sendall(b"ok\n")
        except OSError:
            pass
        finally:
            connection.close()
        command, text = parse_ipc(data)
        if command == "add" and text and on_add is not None:
            on_add(text)
        else:
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

def data_dir():
    """返回数据目录（遵循 XDG_DATA_HOME）。"""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_ID)


def data_file():
    """返回待办数据文件路径。"""
    return os.path.join(data_dir(), "todos.json")


def parse_ipc(data):
    """解析单实例套接字上的一条命令，返回 (命令, 文本)。

    ``raise`` = 唤回窗口；``add\t<文本>`` = 在运行中的实例里加一条待办。
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    command, _, text = data.partition("\t")
    if command.strip() == "add":
        return "add", text.rstrip("\n")
    return "raise", None


def ipc_socket_path():
    """单实例套接字路径（放在 XDG_RUNTIME_DIR 或临时目录）。"""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "%s.sock" % APP_ID)


def raise_running_instance(payload=b"raise\n"):
    """把一条命令发给已在运行的实例；成功返回 True。

    默认发 ``raise``，即「再运行一次」把窗口叫到最前面（任务栏里看不到它）。
    ``--add`` 复用同一条套接字，发 ``add\t<文本>``。
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(ipc_socket_path())
    except OSError:
        return False
    try:
        client.sendall(payload)
        client.recv(16)
    except OSError:
        return False
    finally:
        client.close()
    return True


# ===========================================================================
# 数据层（v0.3.0 原样）
# ===========================================================================

def _fmt_ts(value):
    """把 epoch 秒格式化成给 CSV 看的本地时间字符串（空值返回空串）。"""
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return ""


class Store(object):
    """待办条目 + 窗口状态的持久化，单文件 JSON，改动即落盘。"""

    def __init__(self, path):
        self.path = path
        self.items = []
        # topmost 默认关闭：不置顶，就不会一直挡在其他窗口前面。
        # 需要时在右键菜单里勾「始终置顶」，勾选状态会被记住。
        self.ui = {"x": None, "y": None, "collapsed": False, "alpha": 0.94,
                   "topmost": False, "hotkey": HOTKEY_DEFAULT,
                   "width": None, "height": None, "done_collapsed": False,
                   "sound": True}
        self.archive = []                 # 归档：完成的（过 00:00）/ 清掉的 / 删掉的都进这里
        self._next_id = 1
        self.load_error = None            # 读取被拒绝的原因（例如数据来自更新版本）
        self.loaded_version = DATA_VERSION
        self.load()

    # ---- 读写 ----
    def load(self):
        """读取数据；文件缺失按空清单处理，损坏则备份后从空清单开始。

        版本策略：没有 version 字段的旧文件按 1 处理；低于当前版本走 MIGRATIONS；
        高于当前版本**拒绝读取**，并把原文件移到 ``.newer`` —— 降级打开时不能把新数据写坏。
        """
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

        raw_version = raw.get("version")
        if raw_version is None:
            version = 1                   # 没有 version 字段的旧文件：结构等同 v1
        else:
            try:
                version = int(raw_version)
            except (TypeError, ValueError):
                version = 1
        if version > DATA_VERSION:
            self.load_error = ("数据文件由更新版本（version %d）写入，当前版本只认到 "
                               "version %d；原文件已移到 %s"
                               % (version, DATA_VERSION, self.path + ".newer"))
            self._backup_newer()
            return
        if version < DATA_VERSION:
            raw = self._migrate(raw, version)
        self.loaded_version = version
        self._apply(raw)

    def _apply(self, raw, with_ui=True):
        """把一份数据字典装进 Store（load 与 import 共用；with_ui=False 时不改窗口状态）。"""
        self.items = []
        self.archive = []
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
        if not with_ui:
            return
        ui = raw.get("ui")
        if isinstance(ui, dict):
            for key in ("x", "y"):
                value = ui.get(key)
                if isinstance(value, (int, float)):
                    self.ui[key] = int(value)
            if isinstance(ui.get("collapsed"), bool):
                self.ui["collapsed"] = ui["collapsed"]
            if isinstance(ui.get("done_collapsed"), bool):
                self.ui["done_collapsed"] = ui["done_collapsed"]
            if isinstance(ui.get("topmost"), bool):
                self.ui["topmost"] = ui["topmost"]
            if isinstance(ui.get("sound"), bool):
                self.ui["sound"] = ui["sound"]
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

    def _migrate(self, raw, version):
        """把旧版本数据升到 DATA_VERSION；没有对应迁移函数时按当前结构读（字段容错兜底）。"""
        while version < DATA_VERSION:
            step = MIGRATIONS.get(version)
            if step is None:
                break
            raw = step(raw)
            version += 1
        return raw

    def _backup_newer(self):
        """把「更新版本写的数据文件」整体移走（不解析、不覆盖），降级打开时不破坏它。"""
        target = self.path + ".newer"
        if os.path.exists(target):
            target = "%s.%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.move(self.path, target)
        except (IOError, OSError):
            pass

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
                json.dump({"version": DATA_VERSION, "items": self.items,
                           "archive": self.archive, "ui": self.ui},
                          handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        except (IOError, OSError):
            pass

    # ---- 备份 / 导出 / 导入 ----
    def backup_copy(self, tag="import-backup"):
        """把当前数据文件复制一份（导入前用）；返回备份路径，没有数据文件时返回 None。"""
        if not os.path.exists(self.path):
            return None
        target = "%s.%s" % (self.path, tag)
        try:
            shutil.copy2(self.path, target)
            return target
        except (IOError, OSError):
            return None

    def export_dict(self):
        return {"version": DATA_VERSION,
                "app_version": APP_VERSION,
                "exported_at": time.time(),
                "items": self.items,
                "archive": self.archive}

    def export_json(self, path):
        """导出完整数据（条目 + 归档）到 JSON；返回导出的条目数。"""
        payload = self.export_dict()
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        return len(payload["items"])

    def export_csv(self, path):
        """导出条目为 CSV（供表格查看，不用于恢复）；返回行数。"""
        fields = ["id", "text", "state", "priority", "today", "tags",
                  "steps_done", "steps_total", "created", "done_at", "remind_at"]
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(fields)
            for item in self.items:
                steps = item.get("steps") or []
                writer.writerow([
                    item.get("id"), item.get("text", ""),
                    self.state_of(item),
                    item.get("priority") or 0,
                    "1" if item.get("today") else "0",
                    " ".join("@" + tag for tag in (item.get("tags") or [])),
                    sum(1 for step in steps if step.get("done")),
                    len(steps),
                    _fmt_ts(item.get("created")),
                    _fmt_ts(item.get("done_at")),
                    _fmt_ts(item.get("remind_at")),
                ])
        return len(self.items)

    def import_json(self, path):
        """用导出的 JSON 覆盖当前数据（先验证文件再动数据）；返回 (条目数, 归档数)。"""
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError("不是有效的待办导出文件（缺少 items 列表）")
        if not raw["items"] and not raw.get("archive"):
            raise ValueError("导出文件里没有条目，拒绝用它清空数据")
        self._apply(raw, with_ui=False)
        self.save()
        return len(self.items), len(self.archive)

    def import_todotxt(self, path):
        """把 todo.txt 文件合并进当前数据（不覆盖已有条目）；返回新增条数。"""
        with open(path, "r", encoding="utf-8") as handle:
            entries = parse_todotxt(handle.read())
        if not entries:
            raise ValueError("文件里没有可导入的条目")
        added = 0
        for entry in entries:
            item = self.add(entry["text"])
            if item is None:
                continue
            added += 1
            if entry["done"]:
                self.toggle(item["id"])
            if entry["priority"]:
                self.set_priority(item["id"], entry["priority"])
            if entry["due"]:
                self.set_reminder(item["id"], entry["due"])
        self.save()
        return added

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
    # 只在本机算时间、只在本机弹：不调用 notify-send、不起外部进程（命令被劫持 /
    # 无桌面通知守护时都会静默失败），因此「不调用外部命令」这条安全声明不用改。
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

    def _bucket_rank(self, item):
        """段内的分组键：先「今天要办的」，再优先级（已完成的不参与今天置顶）。"""
        state = self.state_of(item)
        today_rank = 0 if (item.get("today") and state != STATE_DONE) else 1
        return (today_rank, -self.priority_of(item))

    def resort(self, save=False):
        """按**状态**分段归位（未完成 → 等待中 → 已完成），返回顺序是否真的变了。

        A 方案（2026-10-26 YanH 定）：段内**手动顺序优先**，不再每次归位都按
        「今天 / 优先级」重排 —— 否则用户把某条拖到别的分组里，下一次归位就被弹回去，
        看起来就像"被那条挡住了"。
        改「今天 / 优先级」时用 place_in_bucket() 把那一条插到对应组的最前面。
        列表顺序变了才会写盘。
        """
        rank = dict((state, index) for index, state in enumerate(STATE_ORDER))

        def sort_key(pair):
            index, item = pair
            return (rank.get(self.state_of(item), len(STATE_ORDER)), index)

        before = [item["id"] for item in self.items]
        order = sorted(enumerate(self.items), key=sort_key)
        self.items = [item for _, item in order]
        changed = [item["id"] for item in self.items] != before
        if changed and save:
            self.save()
        return changed

    def place_in_bucket(self, item_id, save=True):
        """把一条插到它所属组（今天 / 优先级）的最前面；同组内保持手动顺序。

        只在「改优先级 / 改今天」时调用。没有这一步的话，A 方案下改优先级不会有任何
        位置变化（resort 只按状态分段），"高优先级浮到前面"就失效了。
        """
        item = None
        for candidate in self.items:
            if candidate["id"] == item_id:
                item = candidate
                break
        if item is None:
            return False
        state = self.state_of(item)
        key = self._bucket_rank(item)
        before = [entry["id"] for entry in self.items]
        self.items.remove(item)
        target = None
        for index, other in enumerate(self.items):
            if self.state_of(other) != state:
                continue
            if self._bucket_rank(other) >= key:
                target = index
                break
        if target is None:
            last = None
            for index, other in enumerate(self.items):
                if self.state_of(other) == state:
                    last = index
            target = 0 if last is None else last + 1
        self.items.insert(target, item)
        changed = [entry["id"] for entry in self.items] != before
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
# 界面层（GTK3 / Adwaita）
#
# 结构：Window（卡片）→ Header / 输入条 / 标签 chip / 列表 / Footer。
# 列表按状态分组，每组若干 Row；Row 是 EventBox + Box 的组合，不再自绘 Canvas。
# 性能要点：
#   * 勾选/等待/优先级/今天/勾子步骤 = 就地改这一行，不重建控件、不重排窗口；
#   * 只有"结构变了"（增删/筛选/折叠/归档/拖拽落位）才重建列表；
#   * 窗口缩放只重排（GTK 自己重排 + 调列表上限），不重建任何控件。
# --------------------------------------------------------------------------- #

# 主题：默认深色（Adwaita dark），DESKTOP_TODO_THEME=light 跟随浅色。
# 颜色一律用 GTK 主题的命名色（@theme_* / @borders / @warning_color…），
# 所以系统换主题/强调色时本挂件自动跟着变，不写死色值。
_THEME_NAME = os.environ.get("DESKTOP_TODO_THEME", "dark").strip().lower()

_CSS = """
window { background-color: transparent; }

.card {
  background-color: @theme_bg_color;
  border: 1px solid alpha(@theme_fg_color, 0.12);
  border-radius: 12px;
}

.title { color: @theme_fg_color; font-size: 15pt; font-weight: bold; }
.subtitle { color: alpha(@theme_fg_color, 0.55); font-size: 9pt; }
/* 折叠态单行 header：标题和计数并排，整体压到 ~34px 高 */
.title-compact { color: @theme_fg_color; font-size: 13pt; font-weight: bold; }
.count-compact { color: alpha(@theme_fg_color, 0.55); font-size: 9pt; }
.compact .iconbtn { min-width: 16px; min-height: 16px; padding: 2px; }

.iconbtn {
  background: transparent; background-image: none; border: none;
  box-shadow: none; border-radius: 6px; padding: 3px;
  color: alpha(@theme_fg_color, 0.75); min-width: 18px; min-height: 18px;
}
.iconbtn:hover { background-color: alpha(@theme_fg_color, 0.08); color: @theme_fg_color; }
.iconbtn:active { background-color: alpha(@theme_fg_color, 0.14); }
.iconbtn.danger:hover { color: @error_color; }

entry.add {
  background-color: @theme_base_color;
  color: @theme_fg_color;
  border: 1px solid alpha(@theme_fg_color, 0.15);
  border-radius: 9px; padding: 7px 10px; min-height: 18px;
}
entry.add:focus { border-color: @theme_selected_bg_color; }

button.plus {
  background-image: none;
  background-color: @theme_selected_bg_color;
  color: @theme_selected_fg_color;
  border: none; border-radius: 9px; padding: 6px 9px;
}
button.plus:hover { background-color: shade(@theme_selected_bg_color, 1.12); }
button.plus:active { background-color: shade(@theme_selected_bg_color, 0.92); }

button.chip {
  background: transparent; background-image: none;
  border: 1px solid transparent; border-radius: 999px;
  padding: 2px 10px; font-size: 9pt;
  color: alpha(@theme_fg_color, 0.55);
}
button.chip:hover { background-color: alpha(@theme_fg_color, 0.07); }
button.chip.on {
  background-color: alpha(@theme_selected_bg_color, 0.22);
  color: @theme_fg_color;
  border-color: alpha(@theme_selected_bg_color, 0.55);
}

.section { color: alpha(@theme_fg_color, 0.55); font-size: 9pt; font-weight: bold; }
.section-count { color: alpha(@theme_fg_color, 0.40); font-size: 9pt; }

.row { background-color: transparent; border-radius: 6px; border: 1px solid transparent; }
.row:hover { background-color: alpha(@theme_fg_color, 0.06); }
.row.selected {
  background-color: alpha(@theme_selected_bg_color, 0.20);
  border-color: alpha(@theme_selected_bg_color, 0.65);
}

.row-title { color: @theme_fg_color; font-size: 11pt; }
.row-title.done { color: alpha(@theme_fg_color, 0.50); }
.row-title.waiting { color: @warning_color; }

.meta { color: alpha(@theme_fg_color, 0.52); font-size: 9pt; }
.meta-strong { color: alpha(@theme_fg_color, 0.72); font-size: 9pt; }
.badge { border-radius: 4px; padding: 0px 5px; font-size: 8.5pt; }
.badge.hi { background-color: alpha(@error_color, 0.18); color: @error_color; }
.badge.mid { background-color: alpha(@warning_color, 0.18); color: @warning_color; }
.badge.today {
  background-color: alpha(@theme_selected_bg_color, 0.18);
  color: @theme_selected_bg_color;
}

.step { color: alpha(@theme_fg_color, 0.72); font-size: 9.5pt; }
.step.done { color: alpha(@theme_fg_color, 0.48); font-size: 9.5pt; }

.hint { color: alpha(@theme_fg_color, 0.45); font-size: 8.5pt; }
.hint.warn { color: @warning_color; }
.link { color: @theme_selected_bg_color; font-size: 8.5pt; }
.link:hover { text-decoration: underline; }
button.linkbtn {
  background: transparent; background-image: none; border: none;
  box-shadow: none; border-radius: 4px; padding: 2px 6px;
  color: @theme_selected_bg_color; font-size: 8.5pt; min-height: 16px;
}
button.linkbtn:hover { text-decoration: underline; }
button.linkbtn.warn { color: @warning_color; }

.empty-title { color: alpha(@theme_fg_color, 0.72); font-size: 11pt; }
.empty-sub { color: alpha(@theme_fg_color, 0.50); font-size: 9pt; }

popover, popover.background { background-color: @theme_bg_color; }
popover modelbutton, popover menuitem { color: @theme_fg_color; padding: 5px 10px; font-size: 9.5pt; }
popover modelbutton:hover { background-color: alpha(@theme_fg_color, 0.08); }
separator { background-color: alpha(@theme_fg_color, 0.12); min-height: 1px; }

.dlg { background-color: @theme_bg_color; }
.dlg-label { color: alpha(@theme_fg_color, 0.72); font-size: 9.5pt; }
.dlg-error { color: @error_color; font-size: 9pt; }

.drophint { background-color: @theme_selected_bg_color; border-radius: 1px; min-height: 2px; }
.dragrow { background-color: alpha(@theme_selected_bg_color, 0.20);
           border: 1px solid alpha(@theme_selected_bg_color, 0.65); border-radius: 6px; }
.alert { color: @warning_color; font-size: 8.5pt; }
"""


def install_css():
    """把界面样式挂到默认屏幕上（应用级优先级，只挂一次）。

    主题口径（用户 2026-10-28 选 B：GNOME / Adwaita 原生）：
      * DESKTOP_TODO_THEME 缺省 dark → 本进程用 Adwaita 深色；
      * =light → Adwaita 浅色；
      * =system → 不动主题，跟随系统（本机是 deepin 主题）并读取系统深浅色偏好。
    只在**本进程**内切主题，不改用户的系统设置。
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    if _THEME_NAME == "system":
        # 跟随系统：先看 color-scheme（GNOME 有，UOS 20 的 schema 里没有这个键），
        # 没有就退回"主题名里带 dark"的判断。注意：对不存在的键调 get_string 是
        # g_error（直接 abort 进程），所以必须先查 schema.has_key。
        dark = False
        try:
            import gi as _gi
            _gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            source = Gio.SettingsSchemaSource.get_default()
            schema = source.lookup("org.gnome.desktop.interface", True) \
                if source is not None else None
            if schema is not None and schema.has_key("color-scheme"):
                prefs = Gio.Settings.new("org.gnome.desktop.interface")
                dark = prefs.get_string("color-scheme") == "prefer-dark"
            else:
                dark = "dark" in str(settings.get_property("gtk-theme-name")).lower()
        except Exception:
            dark = False
        settings.set_property("gtk-application-prefer-dark-theme", dark)
    else:
        settings.set_property("gtk-theme-name", "Adwaita")
        settings.set_property("gtk-application-prefer-dark-theme",
                              _THEME_NAME != "light")


class Palette(object):
    """把 GTK 主题里的命名色取成 RGBA，供 cairo 自绘（勾选圆）使用。

    CSS 里已经直接用 @theme_* 了；这里只解决"Python 画的部分怎么跟主题同色"。
    """

    def __init__(self, style_context):
        def lookup(name, fallback):
            ok, rgba = style_context.lookup_color(name)
            if ok:
                return rgba
            return Gdk.RGBA(*fallback)

        self.bg = lookup("theme_bg_color", (0.14, 0.14, 0.15, 1.0))
        self.fg = lookup("theme_fg_color", (1.0, 1.0, 1.0, 1.0))
        self.accent = lookup("theme_selected_bg_color", (0.21, 0.52, 0.89, 1.0))
        self.accent_fg = lookup("theme_selected_fg_color", (1.0, 1.0, 1.0, 1.0))
        self.borders = lookup("borders", (0.35, 0.35, 0.37, 1.0))
        self.warning = lookup("warning_color", (0.96, 0.76, 0.07, 1.0))
        self.error = lookup("error_color", (0.88, 0.11, 0.14, 1.0))
        self.insensitive = lookup("insensitive_fg_color", (0.55, 0.55, 0.57, 1.0))

    @staticmethod
    def mix(a, b, amount):
        return Gdk.RGBA(a.red + (b.red - a.red) * amount,
                        a.green + (b.green - a.green) * amount,
                        a.blue + (b.blue - a.blue) * amount,
                        a.alpha + (b.alpha - a.alpha) * amount)

    def with_alpha(self, color, alpha):
        return Gdk.RGBA(color.red, color.green, color.blue, alpha)


class Clamp(Gtk.Bin):
    """内容限宽居中：宽度最多 max_width，比它窄时照常铺满（GTK3 没有 AdwClamp）。"""

    def __init__(self, child, max_width):
        super().__init__()
        self.max_width = max_width
        self.add(child)

    def do_get_preferred_width(self):
        minimum, natural = Gtk.Bin.do_get_preferred_width(self)
        return min(minimum, self.max_width), min(natural, self.max_width)

    def do_size_allocate(self, allocation):
        self.set_allocation(allocation)
        child = self.get_child()
        if child is None or not child.get_visible():
            return
        width = min(allocation.width, self.max_width)
        rect = Gdk.Rectangle()
        rect.x = allocation.x + (allocation.width - width) // 2
        rect.y = allocation.y
        rect.width = width
        rect.height = allocation.height
        child.size_allocate(rect)


def icon_image(name, size=16):
    image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON)
    image.set_pixel_size(int(size))
    return image


def icon_button(name, size=15, danger=False, tooltip=None):
    button = Gtk.Button()
    button.get_style_context().add_class("iconbtn")
    if danger:
        button.get_style_context().add_class("danger")
    button.add(icon_image(name, size))
    if tooltip:
        button.set_tooltip_text(tooltip)
    return button


class CheckMark(Gtk.DrawingArea):
    """圆形勾选框（cairo 抗锯齿，颜色取自 GTK 主题）。"""

    def __init__(self, app, size=18, done=False, waiting=False, on_click=None):
        super().__init__()
        self.app = app
        self.size = int(size)
        self.done = bool(done)
        self.waiting = bool(waiting)
        self.hover = False
        self.on_click = on_click
        self.set_size_request(self.size, self.size)
        self.connect("draw", self._draw)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("button-press-event", self._pressed)
        self.connect("enter-notify-event", self._hover_on)
        self.connect("leave-notify-event", self._hover_off)
        self.set_tooltip_text(tr("标记完成 / 取消完成", "Mark done / undo"))
        self._update_a11y()

    def _pressed(self, _widget, event):
        if event.button != 1:
            return False
        if self.on_click is not None:
            self.on_click()
        return True

    def _hover_on(self, _widget, _event):
        self.hover = True
        self.queue_draw()
        return False

    def _hover_off(self, _widget, _event):
        self.hover = False
        self.queue_draw()
        return False

    def _update_a11y(self):
        """给屏幕阅读器一个可读名称（自绘控件本身没有文字）。"""
        state = tr("已完成", "done") if self.done else (
            tr("等待中", "waiting") if self.waiting else tr("未完成", "not done"))
        try:
            self.get_accessible().set_name(tr("完成状态：%s", "Status: %s") % state)
        except Exception:
            pass

    def set_state(self, done=None, waiting=None):
        if done is not None:
            self.done = bool(done)
        if waiting is not None:
            self.waiting = bool(waiting)
        self._update_a11y()
        self.queue_draw()

    def _draw(self, _widget, cr):
        palette = self.app.palette
        size = float(self.size)
        # cairo 的 ANTIALIAS 枚举：DEFAULT=0 / NONE=1 / GRAY=2。
        # 必须显式用 GRAY —— 写成 1 是 NONE，等于关掉抗锯齿，
        # 勾选圆/勾/子步骤方框会退化成 1-bit 硬边（v0.4.0 初版就错在这）。
        cr.set_antialias(2)
        if self.done:
            if self.size >= 16:
                cr.set_source_rgba(palette.accent.red, palette.accent.green,
                                   palette.accent.blue, palette.accent.alpha)
                cr.arc(size / 2.0, size / 2.0, size / 2.0 - 0.8, 0, 6.28318)
                cr.fill()
                cr.set_source_rgba(palette.bg.red, palette.bg.green,
                                   palette.bg.blue, 1.0)
                cr.set_line_width(size * 0.105)
                cr.move_to(size * 0.28, size * 0.52)
                cr.line_to(size * 0.44, size * 0.68)
                cr.line_to(size * 0.74, size * 0.34)
                cr.stroke()
            else:                         # 子步骤的小方框：圆角方形 + 勾
                radius = size * 0.22
                cr.set_source_rgba(palette.accent.red, palette.accent.green,
                                   palette.accent.blue, palette.accent.alpha)
                self._rounded_rect(cr, 0.6, 0.6, size - 1.2, size - 1.2, radius)
                cr.fill()
                cr.set_source_rgba(palette.bg.red, palette.bg.green,
                                   palette.bg.blue, 1.0)
                cr.set_line_width(size * 0.14)
                cr.move_to(size * 0.26, size * 0.52)
                cr.line_to(size * 0.44, size * 0.70)
                cr.line_to(size * 0.76, size * 0.32)
                cr.stroke()
        else:
            if self.hover:
                color = palette.accent
            elif self.waiting:
                color = palette.warning
            else:
                color = Palette.mix(palette.borders, palette.fg, 0.55)
            cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
            cr.set_line_width(1.6 if self.size >= 16 else 1.35)
            if self.size >= 16:
                cr.arc(size / 2.0, size / 2.0, size / 2.0 - 0.9, 0, 6.28318)
            else:
                self._rounded_rect(cr, 0.8, 0.8, size - 1.6, size - 1.6,
                                   size * 0.22)
            cr.stroke()
        return False

    @staticmethod
    def _rounded_rect(cr, x, y, width, height, radius):
        radius = max(0.0, min(radius, width / 2.0, height / 2.0))
        cr.new_sub_path()
        cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
        cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
        cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.14159)
        cr.arc(x + radius, y + radius, radius, 3.14159, 4.71239)
        cr.close_path()


# --------------------------------------------------------------------------- #
# 一行
# --------------------------------------------------------------------------- #
class Row(Gtk.EventBox):
    """一条待办。点击=勾选、双击=改名、右键=菜单、按住拖=排序。"""

    def __init__(self, app, item):
        super().__init__()
        self.app = app
        self.item_id = item["id"]
        self.get_style_context().add_class("row")
        try:
            self.get_accessible().set_name(tr("待办：%s", "To-do: %s") % item["text"])
        except Exception:
            pass
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.set_margin_start(6)
        self.box.set_margin_end(8)
        self.box.set_margin_top(6)
        self.box.set_margin_bottom(6)
        self.add(self.box)

        self.check = CheckMark(app, 18, on_click=self._on_check_clicked)
        self.box.pack_start(self.check, False, False, 0)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.title = Gtk.Label(label=item["text"], xalign=0.0)
        self.title.set_line_wrap(True)
        self.title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title.get_style_context().add_class("row-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.body.pack_start(self.title, False, False, 0)
        self.meta = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.body.pack_start(self.meta, False, False, 0)
        self.steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.body.pack_start(self.steps_box, False, False, 0)
        self.box.pack_start(self.body, True, True, 0)

        self.actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.steps_btn = icon_button("view-list-symbolic", 14,
                                     tooltip=tr("子步骤（点文字也可以编辑）", "Sub-steps (click text to edit)"))
        self.steps_btn.connect("clicked", lambda _b: self.app.begin_steps_edit(self.item_id))
        self.delete_btn = icon_button("user-trash-symbolic", 14, danger=True,
                                      tooltip=tr("删除这条", "Delete this"))
        self.delete_btn.connect("clicked", lambda _b: self.app.remove_item(self.item_id))
        self.actions.pack_start(self.steps_btn, False, False, 0)
        self.actions.pack_start(self.delete_btn, False, False, 0)
        self.box.pack_end(self.actions, False, False, 0)
        self.actions.set_opacity(0.0)

        self._edit_entry = None
        self._steps_editor = None
        self.update_item(item)

        self.connect("enter-notify-event", self._on_enter)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)

    # ---- 内容 ----
    def update_item(self, item):
        """就地更新这一行（不重建控件）。"""
        self.item = item
        state = self.app.store.state_of(item)
        done = state == STATE_DONE
        waiting = state == STATE_WAITING
        font_context = self.title.get_style_context()
        for name in ("done", "waiting"):
            font_context.remove_class(name)
        if done:
            font_context.add_class("done")
        if waiting:
            font_context.add_class("waiting")
        if done:
            self.title.set_markup("<s>%s</s>"
                                  % GLib.markup_escape_text(item["text"]))
        else:
            self.title.set_text(item["text"])
        self.check.set_state(done=done, waiting=waiting)
        self._build_meta(item, done=done)
        self._build_steps(item, done=done)
        # 子步骤按钮：只有存在子步骤时才明显，没步骤时淡一点（仍可点开空编辑器）
        steps = self.app.store.steps_of(item)
        self.steps_btn.set_opacity(0.55 if not steps else 1.0)
        self.set_tooltip_text(None)

    def _build_meta(self, item, done=False):
        for child in self.meta.get_children():
            child.destroy()
        specs = self.app.meta_specs(item, done=done)
        if not specs:
            self.meta.set_no_show_all(True)
            self.meta.hide()
            return
        self.meta.set_no_show_all(False)
        self.meta.show()
        for kind, text, extra in specs:
            if kind == "badge":
                label = Gtk.Label(label=text)
                label.get_style_context().add_class("badge")
                label.get_style_context().add_class(extra)
                self.meta.pack_start(label, False, False, 0)
            elif kind == "clock":
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
                image = icon_image("alarm-symbolic", 12)
                image.get_style_context().add_class("meta")
                label = Gtk.Label(label=text)
                label.get_style_context().add_class("meta-strong")
                box.pack_start(image, False, False, 0)
                box.pack_start(label, False, False, 0)
                self.meta.pack_start(box, False, False, 0)
            elif kind == "tag":
                label = Gtk.Label(label=text)
                label.get_style_context().add_class("meta")
                self.meta.pack_start(label, False, False, 0)
            else:
                label = Gtk.Label(label=text)
                label.get_style_context().add_class("meta")
                self.meta.pack_start(label, False, False, 0)
        self.meta.show_all()

    def _build_steps(self, item, done=False):
        for child in self.steps_box.get_children():
            child.destroy()
        steps = self.app.store.steps_of(item)
        if not steps:
            self.steps_box.set_no_show_all(True)
            self.steps_box.hide()
            return
        self.steps_box.set_no_show_all(False)
        self.steps_box.show()
        for index, step in enumerate(steps):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            line.set_margin_start(0)
            check = CheckMark(self.app, 14, done=step["done"],
                              on_click=lambda idx=index: self.app.toggle_step_item(
                                  self.item_id, idx))
            label_box = Gtk.EventBox()
            label_box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            label = Gtk.Label(label=step["text"], xalign=0.0)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.get_style_context().add_class("step")
            if step["done"]:
                label.set_markup("<s>%s</s>" % GLib.markup_escape_text(step["text"]))
                label.get_style_context().add_class("done")
            label_box.add(label)
            label_box.connect(
                "button-press-event",
                lambda _w, event, idx=index: self._on_step_label(event, idx))
            line.pack_start(check, False, False, 0)
            line.pack_start(label_box, True, True, 0)
            self.steps_box.pack_start(line, False, False, 0)
        self.steps_box.show_all()

    def _on_step_label(self, event, index):
        if event.button != 1:
            return False
        self.app.begin_steps_edit(self.item_id, step_index=index)
        return True

    # ---- 悬停 ----
    def _on_enter(self, _widget, _event):
        self.actions.set_opacity(1.0)
        return False

    def _on_leave(self, _widget, _event):
        if self.app.drag_id != self.item_id:
            self.actions.set_opacity(0.0)
        return False

    def show_actions(self, visible):
        self.actions.set_opacity(1.0 if visible else 0.0)

    # ---- 点按 ----
    def _on_check_clicked(self):
        self.app.toggle_item(self.item_id)

    def _on_press(self, _widget, event):
        if event.button == 3:
            self.app.popup_row_menu(self, event)
            return True
        if event.button != 1:
            return False
        # 编辑态：点别的行只收掉编辑，不顺手勾选（v0.2.5 的规矩）
        if self.app.edit_open and self.app.editing_id not in (None, self.item_id):
            self.app.end_edit_all(save=True)
            return True
        if self.app.steps_edit_open and self.app.steps_edit_id != self.item_id:
            self.app.end_edit_all(save=True)
            return True
        # 双击（第二次按下）＝撤回第一次单击的勾选 + 改名，不再触发勾选
        if self.app.is_double_click(self.item_id):
            self.app.handle_double_click(self)
            return True
        self.app.row_pressed(self, event)
        return False

    def _on_release(self, _widget, event):
        if event.button != 1:
            return False
        return self.app.row_released(self, event)

    def _on_motion(self, _widget, event):
        if self.app._press is None:
            return False
        self.app.row_motion(self, event)
        return False

    # ---- 行内改名 ----
    def begin_edit(self):
        if self._edit_entry is not None:
            return
        item = self.app.find_item(self.item_id)
        if item is None:
            return
        entry = Gtk.Entry()
        entry.get_style_context().add_class("add")
        entry.set_text(item["text"] + "".join(" @" + tag for tag in item["tags"]))
        entry.select_region(0, -1)
        self.body.pack_start(entry, False, False, 0)
        self.body.reorder_child(entry, 0)
        self.title.hide()
        self._edit_entry = entry
        entry.show()
        entry.connect("activate", lambda _e: self.app.end_edit(save=True))
        entry.connect("key-press-event", self._edit_key)
        entry.connect("focus-out-event", self._edit_focus_out)
        entry.grab_focus()

    def _edit_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.app.end_edit(save=False)
            return True
        return False

    def _edit_focus_out(self, _widget, _event):
        # 焦点明确落到别的控件才保存；切到别的应用（无焦点）不算
        def check():
            if self._edit_entry is not None:
                focus = self.app.win.get_focus()
                if focus is not None and focus is not self._edit_entry:
                    self.app.end_edit(save=True)
            return False

        GLib.timeout_add(60, check)
        return False

    def clear_edit(self):
        if self._edit_entry is not None:
            entry, self._edit_entry = self._edit_entry, None
            try:
                entry.destroy()
            except Exception:
                pass
        self.title.show()

    def edit_text(self):
        return self._edit_entry.get_text() if self._edit_entry is not None else ""

    # ---- 子步骤编辑器 ----
    def begin_steps_edit(self, focus_index=None):
        if self._steps_editor is not None:
            if focus_index is not None:
                self._steps_focus(focus_index)
            return
        item = self.app.find_item(self.item_id)
        if item is None:
            return
        steps = self.app.store.steps_of(item)
        while len(steps) < 1:
            steps.append({"text": "", "done": False})
            break
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        holder.set_margin_top(2)
        self.body.pack_start(holder, False, False, 0)
        self.body.reorder_child(holder, 2 if self.meta.get_visible() else 1)
        self.steps_box.hide()
        entries = []
        flags = []
        for step in steps:
            entries.append(self._steps_make_row(holder, step["text"], step["done"]))
            flags.append(bool(step["done"]))
        if steps and steps[-1]["text"]:
            # 末尾留一个空框，方便直接往下写
            entries.append(self._steps_make_row(holder, "", False))
            flags.append(False)
        holder.show_all()
        self._steps_editor = {"holder": holder, "entries": entries, "done": flags}
        self._steps_focus(focus_index or 0)
        self.app.update_hint()

    def _steps_make_row(self, holder, text, done):
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        check = CheckMark(self.app, 14, done=done,
                          on_click=lambda: self._steps_toggle_check(check))
        entry = Gtk.Entry()
        entry.get_style_context().add_class("add")
        entry.set_text(text)
        entry.connect("key-press-event", self._steps_key)
        entry.connect("focus-in-event",
                      lambda _w, _e, ent=entry: self._steps_focus_entry(ent))
        line.pack_start(check, False, False, 0)
        line.pack_start(entry, True, True, 0)
        holder.pack_start(line, False, False, 0)
        line._check = check
        line._entry = entry
        return line

    def _steps_toggle_check(self, check):
        check.set_state(done=not check.done)
        for index, line in enumerate(self._steps_editor["entries"]):
            if line._check is check:
                self._steps_editor["done"][index] = check.done
                break

    def _steps_key(self, widget, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.state & Gdk.ModifierType.CONTROL_MASK:
                self._steps_add_after(widget)
            else:
                self.app.end_steps_edit(save=True)
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.app.end_steps_edit(save=False)
            return True
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            backward = event.keyval == Gdk.KEY_ISO_Left_Tab \
                or bool(event.state & Gdk.ModifierType.SHIFT_MASK)
            self._steps_move_focus(widget, -1 if backward else 1)
            return True
        return False

    def _steps_add_after(self, widget):
        holder = self._steps_editor["holder"]
        entries = self._steps_editor["entries"]
        done_flags = self._steps_editor["done"]
        for index, line in enumerate(entries):
            if line._entry is widget:
                new_line = self._steps_make_row(holder, "", False)
                holder.reorder_child(new_line, index + 1)
                new_line.show_all()
                entries.insert(index + 1, new_line)
                done_flags.insert(index + 1, False)
                if len(entries) > STEP_MAX:
                    entries.pop()
                    new_line.destroy()
                    self.app.flash_hint("一条最多 %d 个子步骤" % STEP_MAX)
                    return
                new_line._entry.grab_focus()
                self.app.fit_window()
                return

    def _steps_move_focus(self, widget, delta):
        entries = [line._entry for line in self._steps_editor["entries"]]
        try:
            index = entries.index(widget)
        except ValueError:
            return
        target = max(0, min(len(entries) - 1, index + delta))
        entries[target].grab_focus()

    def _steps_focus_entry(self, entry):
        entries = [line._entry for line in self._steps_editor["entries"]]
        if entry in entries:
            self.app._steps_active = entries.index(entry)

    def _steps_focus(self, index):
        if self._steps_editor is None:
            return
        entries = self._steps_editor["entries"]
        if not entries:
            return
        index = max(0, min(int(index), len(entries) - 1))
        entry = entries[index]._entry
        entry.grab_focus()
        entry.select_region(0, -1)

    def steps_texts(self):
        """编辑器里的 (text, done) 列表（空框会被丢掉）。"""
        if self._steps_editor is None:
            return []
        result = []
        for index, line in enumerate(self._steps_editor["entries"]):
            text = " ".join(line._entry.get_text().split())
            if not text:
                continue
            result.append({"text": text,
                           "done": bool(self._steps_editor["done"][index])})
        return result

    def clear_steps_edit(self):
        if self._steps_editor is not None:
            holder = self._steps_editor["holder"]
            self._steps_editor = None
            try:
                holder.destroy()
            except Exception:
                pass
        if not self.steps_box.get_visible():
            self.steps_box.show()


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class TodoWindow(object):
    """桌面待办面板（无边框、默认不置顶；条目可拖拽排序）。"""

    REGROUP_DELAY_MS = 1200       # 状态变了多久之后把它归位（留出双击改名的窗口）

    def __init__(self, win, store, visible=True):
        self.win = win
        self.store = store
        self.root = GtkRootShim(win)      # 给旧集成代码的 Tk 风格适配层
        self.panel_w = max(PANEL_W_MIN, min(PANEL_W_MAX,
                                            int(store.ui.get("width") or PANEL_W)))
        self._user_sized = bool(store.ui.get("height"))
        self._done_collapsed = bool(store.ui.get("done_collapsed"))
        self.filter_tag = None
        self.filter_query = None
        self.rows = {}
        self.row_widgets = []
        self.section_widgets = {}
        self.section_counts = {}
        self._rendering = False
        self.drag_id = None
        self.drag_target = None
        self.drop_line = None
        self._press = None
        self._edit_id = None
        self._steps_edit_id = None
        self._steps_active = 0
        self._last_toggle = (None, 0.0)
        self._last_click = (None, 0.0)
        # 标题栏拖动状态：必须先在 __init__ 里置空。否则窗口 map 之后、
        # 用户还没按下鼠标时收到一条 motion/release 事件就会 AttributeError。
        self._header_drag = None
        self._header_last_press = None
        self._regroup_job = None
        self._undo = None
        self._undo_job = None
        self._alert = None
        self._hint_job = None
        self._remind_job = None
        self._archive_window = None
        self._alpha_dialog = None
        self._reminder_dialog = None
        self._row_menu = None
        self._resize_job = None
        self._applying_geometry = False
        self._geometry_pending = None
        self._geometry_since = 0.0
        self._anchor_pending = None      # (x, y, width, height)：等 Configure 到位后再挪窗口
        self._expanded_size = None
        self._hotkey_spec = store.ui.get("hotkey") or HOTKEY_DEFAULT
        self._hotkey_on = False
        self._archived_day = None

        self._migrate_ui()
        self._build()
        self._apply_session_warn()
        self._restore_geometry()
        self._refresh_tags()
        self._archive_on_start()
        self.render(keep_scroll=False)
        if visible:
            win.show_all()
            win.present()
            self._apply_collapsed()
            self._restore_geometry()
            single_instance_start(self.root, self.raise_window, self.add_from_ipc)
            set_diag_path(os.path.dirname(self.store.path))
            if session_kind()[0] == "wayland":
                # 纯 Wayland 下 XGrabKey 不可用：不注册，也不假装成功（界面上已有说明）
                self._hotkey_on = False
            else:
                self._hotkey_on = hotkey_install(self.root, self._hotkey_spec,
                                                 self.raise_window)
            self._remind_tick(raise_it=False)

    def _migrate_ui(self):
        """把旧版（tkinter 自绘）留下的窗口尺寸收敛到新界面能用的大小。

        旧版允许把窗口拉成任意宽：升级后若照搬 1174×855，内容列虽然限宽居中，
        但窗口会伸出屏幕、看点全跑到右边。这里一次性把超宽/超高收掉：
        宽度收到 520（内容列上限），高度交还给"按内容自适应"，位置重新夹进屏幕。
        """
        if getattr(self, "_migrated", False):
            return
        self._migrated = True
        screen_w, screen_h = screen_size()
        width = int(self.store.ui.get("width") or PANEL_W)
        height = self.store.ui.get("height")
        changed = False
        if width > PANEL_W_MAX:
            width = PANEL_W_MAX
            changed = True
        if width < PANEL_W_MIN:
            width = PANEL_W_MIN
            changed = True
        if height is not None and (int(height) > screen_h * 0.6
                                   or int(height) < 160):
            height = None
            changed = True
        if changed:
            self.store.ui["width"] = width
            self.store.ui["height"] = height
            x = self.store.ui.get("x")
            y = self.store.ui.get("y")
            if x is not None:
                self.store.ui["x"] = max(0, min(int(x), screen_w - width - 16))
            if y is not None:
                self.store.ui["y"] = max(0, min(int(y), screen_h - 120))
            try:
                self.store.save()
            except Exception:
                pass
        self.panel_w = width
        self._user_sized = bool(height)

    # ---- 组装 ----
    def _build(self):
        win = self.win
        win.set_title(tr("桌面待办清单", "Desktop To-do"))
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_keep_above(bool(self.store.ui.get("topmost")))
        set_window_opacity(win, float(self.store.ui.get("alpha") or 0.94))
        win.set_resizable(True)
        win.set_default_size(self.panel_w, 420)
        win.connect("delete-event", lambda *_a: self.quit())
        win.connect("destroy", lambda *_a: self.quit())
        win.connect("key-press-event", self._on_window_key)

        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual() if screen else None
        if visual is not None and screen.is_composited():
            win.set_visual(visual)
        win.set_app_paintable(True)

        self.card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.card.get_style_context().add_class("card")
        win.add(self.card)
        win.connect("configure-event", self._on_configure)
        win.connect("button-press-event", self._on_window_press)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.clamp = Clamp(self.content, PANEL_W_MAX)
        self.card.pack_start(self.clamp, True, True, 0)

        self._make_palette()
        self._build_header()
        self._build_input()
        self._build_chips()
        self._build_list()
        self._build_footer()
        self._build_window_menu()

    def _apply_session_warn(self):
        """Wayland 会话下，把「X11 依赖可能失效」写在界面上（不弹窗、不打断）。"""
        kind, _detail = session_kind()
        self._session_warn_on = kind in ("wayland", "xwayland")
        if not self._session_warn_on:
            return
        if kind == "wayland":
            text = "Wayland 会话：全局快捷键 / 置顶可能不可用（本程序为 X11 设计）"
        else:
            text = "Wayland 会话（XWayland）：唤回 / 置顶可能不稳定"
        self.session_warn.set_text(text)
        self.session_warn.show()

    def _make_palette(self):
        self.palette = Palette(self.win.get_style_context())

    def _build_header(self):
        self.header_event = Gtk.EventBox()
        self.header_event.set_visible_window(False)
        self.header_event.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK)
        self.header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.header.set_margin_start(16)
        self.header.set_margin_end(10)
        self.header.set_margin_top(12)
        self.header.set_margin_bottom(6)
        self.header_event.add(self.header)
        self.header_event.connect("button-press-event", self._on_header_press)
        self.header_event.connect("button-release-event", self._on_header_release)
        self.header_event.connect("motion-notify-event", self._on_header_motion)

        # 展开态：两行（标题 / 计数）
        self.header_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.title = Gtk.Label(label=tr("待办", "To-do"), xalign=0.0)
        self.title.get_style_context().add_class("title")
        self.count = Gtk.Label(label="", xalign=0.0)
        self.count.get_style_context().add_class("subtitle")
        self.header_left.pack_start(self.title, False, False, 0)
        self.header_left.pack_start(self.count, False, False, 0)
        self.header.pack_start(self.header_left, True, True, 0)

        # 折叠态：单行（标题 + 计数并排），默认不参与 show_all
        self.compact_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.compact_line.set_no_show_all(True)
        self.compact_title = Gtk.Label(label=tr("待办", "To-do"), xalign=0.0)
        self.compact_title.get_style_context().add_class("title-compact")
        self.compact_count = Gtk.Label(label="", xalign=0.0)
        self.compact_count.get_style_context().add_class("count-compact")
        self.compact_line.pack_start(self.compact_title, False, False, 0)
        self.compact_line.pack_start(self.compact_count, False, False, 0)
        self.header.pack_start(self.compact_line, True, True, 0)

        self.collapse_btn = icon_button("pan-up-symbolic", 15, tooltip=tr("折叠 / 展开", "Collapse / expand"))
        self.collapse_btn.connect("clicked", lambda _b: self.toggle_collapsed())
        self.close_btn = icon_button("window-close-symbolic", 15, tooltip=tr("退出", "Quit"))
        self.close_btn.connect("clicked", lambda _b: self.quit())
        self.header.pack_end(self.close_btn, False, False, 0)
        self.header.pack_end(self.collapse_btn, False, False, 0)

        self.content.pack_start(self.header_event, False, False, 0)

    def _build_input(self):
        self.input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.input_row.set_margin_start(16)
        self.input_row.set_margin_end(16)
        self.input_row.set_margin_top(2)
        self.input_row.set_margin_bottom(8)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(PLACEHOLDER)
        self.entry.get_style_context().add_class("add")
        self.entry.connect("activate", lambda _e: self.add_from_entry())
        self.entry.connect("key-press-event", self._on_entry_key)
        self.input_row.pack_start(self.entry, True, True, 0)
        self.plus_btn = Gtk.Button()
        self.plus_btn.get_style_context().add_class("plus")
        self.plus_btn.add(icon_image("list-add-symbolic", 16))
        self.plus_btn.set_tooltip_text(tr("添加待办", "Add to-do"))
        self.plus_btn.connect("clicked", lambda _b: self.add_from_entry())
        self.input_row.pack_end(self.plus_btn, False, False, 0)
        self.content.pack_start(self.input_row, False, False, 0)

    def _build_chips(self):
        self.tags_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.tags_row.set_margin_start(12)
        self.tags_row.set_margin_end(12)
        self.tags_row.set_margin_bottom(4)
        self.content.pack_start(self.tags_row, False, False, 0)

        # 搜索行（Ctrl+F 或标题栏菜单开关；默认隐藏）
        self.search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_row.set_margin_start(12)
        self.search_row.set_margin_end(12)
        self.search_row.set_margin_bottom(4)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text(tr("搜索待办…", "Search to-dos…"))
        self.search_entry.connect("changed", self._on_search_changed)
        self.search_entry.connect("key-press-event", self._on_search_key)
        self.search_row.pack_start(self.search_entry, True, True, 0)
        search_close = icon_button("window-close-symbolic", 13,
                                   tooltip=tr("关闭搜索", "Close search"))
        search_close.connect("clicked", lambda _b: self.toggle_search(False))
        self.search_row.pack_end(search_close, False, False, 0)
        self.search_row.set_no_show_all(True)
        self.search_row.hide()
        self.content.pack_start(self.search_row, False, False, 0)

    def _build_list(self):
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled.set_overlay_scrolling(True)
        self.scrolled.set_propagate_natural_height(True)
        self.scrolled.set_max_content_height(420)
        self.scrolled.set_margin_start(8)
        self.scrolled.set_margin_end(8)
        self.listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.scrolled.add(self.listbox)
        self.list_area = self.scrolled
        self.content.pack_start(self.scrolled, True, True, 0)
        self._bind_wheel(self.scrolled)
        self._bind_wheel(self.listbox)

    def _build_footer(self):
        self.footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.footer.set_margin_start(16)
        self.footer.set_margin_end(16)
        self.footer.set_margin_top(6)
        self.footer.set_margin_bottom(9)
        self.hint = Gtk.Label(label="", xalign=0.0)
        self.hint.get_style_context().add_class("hint")
        self.hint.set_ellipsize(Pango.EllipsizeMode.END)
        self.undo_btn = Gtk.Button(label=tr("撤销删除", "Undo delete"))
        self.undo_btn.get_style_context().add_class("linkbtn")
        self.undo_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.undo_btn.connect("clicked", lambda _b: self.undo_delete())
        self.alert_btn = Gtk.Button(label=tr("知道了", "Got it"))
        self.alert_btn.get_style_context().add_class("linkbtn")
        self.alert_btn.get_style_context().add_class("warn")
        self.alert_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.alert_btn.connect("clicked", lambda _b: self.dismiss_alert())
        self.archive_btn = Gtk.Button(label=tr("归档 0", "Archive 0"))
        self.archive_btn.get_style_context().add_class("linkbtn")
        self.archive_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.archive_btn.set_tooltip_text(tr("查看归档", "View archive"))
        self.archive_btn.connect("clicked", lambda _b: self.open_archive())
        for widget in (self.undo_btn, self.alert_btn):
            widget.set_no_show_all(True)
            widget.hide()
        self.footer.pack_start(self.hint, True, True, 0)
        self.footer.pack_start(self.undo_btn, False, False, 0)
        self.footer.pack_start(self.alert_btn, False, False, 0)
        self.footer.pack_end(self.archive_btn, False, False, 0)
        # Wayland 会话提示：只有检测到才显示（见 _apply_session_warn）
        self.session_warn = Gtk.Label(label="", xalign=0.0)
        self.session_warn.get_style_context().add_class("hint")
        self.session_warn.get_style_context().add_class("warn")
        self.session_warn.set_no_show_all(True)
        self.session_warn.hide()
        self.session_warn.set_margin_start(16)
        self.session_warn.set_margin_end(16)
        self.content.pack_start(self.session_warn, False, False, 0)
        self.content.pack_start(self.footer, False, False, 0)

    def _build_window_menu(self):
        self.window_menu = Gtk.Menu()
        item = Gtk.MenuItem(label=tr("折叠 / 展开", "Collapse / expand"))
        item.connect("activate", lambda _i: self.toggle_collapsed())
        self.window_menu.append(item)
        item = Gtk.MenuItem(label=tr("隐藏窗口（快捷键唤回）", "Hide window (recall by hotkey)"))
        item.connect("activate", lambda _i: self.toggle_hidden())
        self.window_menu.append(item)
        self._topmost_item = Gtk.CheckMenuItem(label=tr("始终置顶", "Always on top"))
        self._topmost_item.set_active(bool(self.store.ui.get("topmost")))
        self._topmost_item.connect("toggled", self._on_topmost_toggled)
        self.window_menu.append(self._topmost_item)
        self._autostart_item = Gtk.CheckMenuItem(label=tr("开机自启", "Start on login"))
        self._autostart_item.set_active(os.path.exists(autostart_path()))
        self._autostart_item.connect("toggled", self._on_autostart_toggled)
        self.window_menu.append(self._autostart_item)
        item = Gtk.MenuItem(label=tr("窗口透明度…", "Window opacity…"))
        item.connect("activate", lambda _i: self.open_alpha_dialog())
        self.window_menu.append(item)
        self._sound_item = Gtk.CheckMenuItem(label=tr("提醒声音", "Reminder sound"))
        self._sound_item.set_active(bool(self.store.ui.get("sound", True)))
        self._sound_item.connect("toggled", self._on_sound_toggled)
        self.window_menu.append(self._sound_item)
        self.window_menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label=tr("查看归档…", "View archive…"))
        item.connect("activate", lambda _i: self.open_archive())
        self.window_menu.append(item)
        self.window_menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label=tr("退出", "Quit"))
        item.connect("activate", lambda _i: self.quit())
        self.window_menu.append(item)
        self.window_menu.show_all()

    # ---- 数据/可见集 ----
    def _visible_items(self):
        items = self.store.items
        if self.filter_tag:
            items = [item for item in items
                     if self.filter_tag in self.store.tags_of(item)]
        if self.filter_query:
            query = self.filter_query.casefold()
            items = [item for item in items if query in item["text"].casefold()]
        return items

    def find_item(self, item_id):
        for item in self.store.items:
            if item["id"] == item_id:
                return item
        return None

    def meta_specs(self, item, done=False):
        """元信息行：时钟 + @标签 + 已/总步数 + 徽标（高/中/今天）+ 等待备注。"""
        specs = []
        if not done and item.get("remind_at"):
            specs.append(("clock", format_remind_time(item["remind_at"]), None))
        for tag in self.store.tags_of(item):
            specs.append(("tag", "@" + tag, None))
        done_steps, total_steps = self.store.steps_progress(item)
        if total_steps:
            specs.append(("meta", tr("%d/%d 步", "%d/%d steps") % (done_steps, total_steps), None))
        if not done:
            priority = self.store.priority_of(item)
            if priority == PRIORITY_HIGH:
                specs.append(("badge", tr("高", "High"), "hi"))
            elif priority == PRIORITY_MID:
                specs.append(("badge", tr("中", "Mid"), "mid"))
            if item.get("today"):
                specs.append(("badge", tr("今天", "Today"), "today"))
        return specs

    # ---- 渲染 ----
    def render(self, keep_scroll=True):
        """整表重建（只在结构变化时调用；单条状态变化走 redraw_row）。"""
        self._rendering = True
        try:
            if self._steps_edit_id is not None:
                self.end_steps_edit(save=True)
            self._render_body(keep_scroll)
        finally:
            self._rendering = False

    def _render_body(self, keep_scroll):
        position = 0.0
        if keep_scroll:
            try:
                position = self.scrolled.get_vadjustment().get_value()
            except Exception:
                position = 0.0
        self.end_edit(save=False, silend=True)
        for child in self.listbox.get_children():
            child.destroy()
        self.rows = {}
        self.row_widgets = []
        self.section_widgets = {}
        self.section_widgets = {}
        self.listbox.show()
        visible = self._visible_items()
        if not visible:
            self._make_empty_state()
        else:
            for state in STATE_ORDER:
                group = [item for item in visible
                         if self.store.state_of(item) == state]
                if not group:
                    continue
                collapsed = state == STATE_DONE and self._done_collapsed
                self.section_widgets[state] = self._make_section(
                    state, len(group), collapsed=collapsed)
                if not collapsed:
                    for item in group:
                        row = Row(self, item)
                        self.rows[item["id"]] = row
                        self.row_widgets.append((item["id"], row))
                        self.listbox.pack_start(row, False, False, 0)
        self.listbox.show_all()
        self._update_counts()
        self._update_footer()
        self._refresh_tags()
        self.fit_window()
        if keep_scroll and position:
            try:
                adjustment = self.scrolled.get_vadjustment()
                GLib.idle_add(lambda: (adjustment.set_value(position), False)[1])
            except Exception:
                pass

    def redraw_row(self, item_id):
        """只更新这一行（不重建控件）。"""
        row = self.rows.get(item_id)
        item = self.find_item(item_id)
        if row is None or item is None:
            return False
        if self._steps_edit_id == item_id or self._edit_id == item_id:
            return False
        row.update_item(item)
        return True

    def _make_section(self, state, count, collapsed=False):
        wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        wrap.set_margin_start(6)
        wrap.set_margin_end(6)
        wrap.set_margin_top(12)
        wrap.set_margin_bottom(2)
        name = Gtk.Label(label={STATE_TODO: tr("未完成", "Not done"),
                                STATE_WAITING: tr("等待中", "Waiting"),
                                STATE_DONE: tr("已完成", "Done")}.get(state, state),
                         xalign=0.0)
        name.get_style_context().add_class("section")
        num = Gtk.Label(label=str(count), xalign=0.0)
        num.get_style_context().add_class("section-count")
        self.section_counts[state] = num
        wrap.pack_start(name, False, False, 0)
        wrap.pack_start(num, False, False, 0)
        if state == STATE_DONE:
            if not collapsed:
                clear = Gtk.Button(label=tr("归档已完成", "Archive completed"))
                clear.get_style_context().add_class("linkbtn")
                clear.set_relief(Gtk.ReliefStyle.NONE)
                clear.set_tooltip_text(tr("把所有已完成的条目移进归档",
                                                "Move all completed items to the archive"))
                clear.connect("clicked", lambda _b: self.clear_done())
                wrap.pack_end(clear, False, False, 0)
            arrow = icon_button("pan-down-symbolic" if not collapsed
                                else "pan-end-symbolic", 13,
                                tooltip=tr("折叠 / 展开已完成", "Collapse / expand completed"))
            arrow.connect("clicked", lambda _b: self.toggle_done_collapsed())
            wrap.pack_end(arrow, False, False, 0)
        self.listbox.pack_start(wrap, False, False, 0)
        return wrap

    def _make_empty_state(self):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        wrap.set_margin_top(30)
        wrap.set_margin_bottom(30)
        image = icon_image("view-list-symbolic", 28)
        image.get_style_context().add_class("empty-sub")
        wrap.pack_start(image, False, False, 0)
        if self.filter_tag:
            headline = "没有 @%s 的条目" % self.filter_tag
            sub = "点上面的「全部」看所有待办"
        else:
            headline = tr("还没有待办", "No to-dos yet")
            sub = tr("在上面写一条，回车就记下了",
                     "Type one above and press Enter")
        head = Gtk.Label(label=headline)
        head.get_style_context().add_class("empty-title")
        tail = Gtk.Label(label=sub)
        tail.get_style_context().add_class("empty-sub")
        wrap.pack_start(head, False, False, 0)
        wrap.pack_start(tail, False, False, 0)
        self.listbox.pack_start(wrap, False, False, 0)

    # ---- 尺寸 ----
    def chrome_height(self):
        total = 0
        for widget in (self.header, self.input_row, self.tags_row,
                       self.search_row, self.session_warn, self.footer):
            if widget is not None and widget.get_visible():
                minimum, _natural = widget.get_preferred_height()
                total += minimum
        return total

    def fit_window(self):
        """按内容算窗口高度；列表超上限就滚动。用户手动定过高度就以用户为准。"""
        if self.store.ui.get("collapsed"):
            return
        _screen_w, screen_h = screen_size()
        chrome = self.chrome_height()
        self.scrolled.set_max_content_height(
            int(max(120, screen_h * LIST_MAX_RATIO - chrome)))
        # 内容列最宽 520（Clamp 负责居中），但窗口本身可以拉得更宽
        content_w = min(self.panel_w, PANEL_W_MAX)
        if self._user_sized and self.store.ui.get("height"):
            height = int(self.store.ui["height"])
        else:
            _minimum, list_natural = self.scrolled.get_preferred_height_for_width(
                max(120, content_w - 18))
            height = chrome + min(list_natural, int(screen_h * LIST_MAX_RATIO)) + 8
        self._apply_geometry(self.panel_w, max(140, int(height)))

    def _apply_geometry(self, width, height):
        """我们主动改窗口尺寸：记下目标尺寸，之后收到的同名 Configure 不算用户改大小。"""
        width, height = int(width), int(height)
        self._geometry_pending = (width, height)
        self._geometry_since = time.time()
        self._applying_geometry = True
        try:
            self.win.resize(width, height)
        finally:
            self._applying_geometry = False

    def _restore_geometry(self):
        if self._anchor_pending is not None:
            return          # 折叠/展开的锚点还没落位，别抢窗口位置
        screen_w, screen_h = screen_size()
        x = self.store.ui.get("x")
        y = self.store.ui.get("y")
        if x is None or y is None:
            x = screen_w - min(self.panel_w, PANEL_W_MAX) - 48
            y = 96
        visible_w = min(self.panel_w, PANEL_W_MAX)
        x = max(0, min(int(x), screen_w - visible_w - 16))
        if self.store.ui.get("collapsed"):
            # 折叠态窗口更窄：从展开位置按"离哪边近就保哪边"推出收窄后的 x，
            # 否则每次启动都会从屏幕右边跳回来。
            compact_w = self.win.get_size()[0]
            if compact_w > 100:
                if x + visible_w / 2.0 > screen_w / 2.0:
                    x += visible_w - compact_w
                x = max(0, min(int(x), int(screen_w - compact_w)))
        y = max(0, min(int(y), screen_h - 120))
        self.win.move(int(x), int(y))

    def _on_configure(self, _widget, event):
        if event.width > 1 and event.height > 1:
            self._flush_anchor(event.width)      # 折叠/展开的锚点移动等这里
        if self._applying_geometry or self.store.ui.get("collapsed"):
            return False
        if event.width <= 1 or event.height <= 1:
            return False
        width, height = int(event.width), int(event.height)
        pending = self._geometry_pending
        if pending is not None and abs(pending[0] - width) <= 2 \
                and abs(pending[1] - height) <= 2:
            self._geometry_pending = None        # 自己这次 resize 的回声
            return False
        if time.time() - self._geometry_since < 0.35:
            return False                          # WM 动画里的中间尺寸，不当用户改
        if self._resize_job is not None:
            try:
                GLib.source_remove(self._resize_job)
            except Exception:
                pass
        self._resize_job = GLib.timeout_add(
            180, lambda: (self._apply_user_size(width, height), False)[1])
        return False

    def _apply_user_size(self, width, height):
        """用户拉过窗口：宽度只守下限（内容由 Clamp 限到 520 居中），高度记下来。"""
        self._resize_job = None
        width = max(PANEL_W_MIN, int(width))
        height = max(140, int(height))
        if (width, height) == (self.panel_w, int(self.store.ui.get("height") or 0)):
            return
        self._user_sized = True
        self.panel_w = width
        self.store.ui["width"] = width
        self.store.ui["height"] = height
        self.store.save()
        if width != int(self.store.ui.get("width") or width):
            self._apply_geometry(width, height)
        self.fit_window()

    # ---- 计数 / 提示 ----
    def _update_counts(self):
        visible = self._visible_items()
        remaining = len([item for item in visible
                         if self.store.state_of(item) != STATE_DONE])
        waiting = len([item for item in visible
                       if self.store.state_of(item) == STATE_WAITING])
        reminders = self.store.reminder_count(visible)
        if remaining:
            text = tr("%d 项未完成", "%d unfinished") % remaining
            if waiting:
                text += tr(" · %d 等待", " · %d waiting") % waiting
            if reminders:
                text += tr(" · %d 提醒", " · %d reminders") % reminders
        else:
            text = tr("全部完成", "All done")
        self.count.set_text(text)
        self.compact_count.set_text(text)       # 折叠态单行用的那枚
        today_done, today_total = self.store.today_stats()
        return dict(remaining=remaining, waiting=waiting, reminders=reminders,
                    today_done=today_done, today_total=today_total,
                    archive=len(self.store.archive))

    def update_hint(self):
        """底部提示按优先级选一条：提醒 > 撤销 > 子步骤编辑 > 筛选 > 折叠 > 今天 > 常规。"""
        if self._alert is not None:
            return                       # 提醒横幅由 show_alert 自己写
        self.hint.get_style_context().remove_class("warn")
        if self.undo_btn.get_visible():
            text = tr("已删除「%s」", "Deleted \"%s\"") % getattr(self, "_undo_text", "")
        elif self.steps_edit_open:
            text = tr("回车=保存 · Ctrl+回车=加一步 · Esc=取消",
                        "Enter=save · Ctrl+Enter=add step · Esc=cancel")
        elif self.edit_open:
            text = tr("回车=保存 · Esc=取消", "Enter=save · Esc=cancel")
        elif self.filter_query:
            text = tr("搜索「%s」· %d 条", "Search \"%s\" · %d items") \
                % (self.filter_query, len(self._visible_items()))
        elif self.filter_tag:
            text = tr("只看 @%s · 点「全部」看所有", "Only @%s · click All to reset") % self.filter_tag
        elif self.store.ui.get("collapsed"):
            text = tr("双击标题栏展开", "Double-click the header to expand")
        elif getattr(self, "_flash", None):
            text = self._flash
        else:
            today_done, today_total = self.store.today_stats()
            if today_total:
                text = tr("今天 %d/%d · 拖动排序 · 双击改名",
                          "Today %d/%d · drag to sort · double-click to rename") \
                    % (today_done, today_total)
            else:
                text = tr("拖动排序 · 双击改名", "Drag to sort · double-click to rename")
        self.hint.set_text(text)

    def _update_footer(self):
        stats = self._update_counts()
        self.archive_btn.set_label(tr("归档 %d", "Archive %d") % stats["archive"])
        self._sync_section_counts()
        self.update_hint()

    def _sync_section_counts(self):
        """分组标题里的计数跟着单条操作更新；组空了/新组出现才整表重建。"""
        if self._rendering:
            return
        visible = self._visible_items()
        groups = {}
        for item in visible:
            state = self.store.state_of(item)
            groups[state] = groups.get(state, 0) + 1
        if set(groups) != set(self.section_widgets):
            self.render(keep_scroll=True)
            return
        for state, count in groups.items():
            label = self.section_counts.get(state)
            if label is not None and label.get_text() != str(count):
                label.set_text(str(count))

    def flash_hint(self, text):
        self._flash = text
        if self._hint_job is not None:
            try:
                GLib.source_remove(self._hint_job)
            except Exception:
                pass
        self._hint_job = GLib.timeout_add(3000, lambda: (self._restore_hint(), False)[1])
        self.update_hint()

    def _restore_hint(self):
        self._hint_job = None
        self._flash = None
        self.update_hint()

    # ---- 输入 ----
    def _on_entry_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.entry.set_text("")
            return True
        if (event.state & Gdk.ModifierType.CONTROL_MASK
                and event.keyval in (Gdk.KEY_f, Gdk.KEY_F)):
            self.toggle_search(True)
            return True
        return False

    def _on_window_key(self, _widget, event):
        if (event.state & Gdk.ModifierType.CONTROL_MASK
                and event.keyval in (Gdk.KEY_f, Gdk.KEY_F)):
            self.toggle_search(True)
            return True
        return False

    # ---- 搜索 ----
    def toggle_search(self, show=None):
        if show is None:
            show = not self.search_row.get_visible()
        if show:
            self.search_row.show()
            self.search_entry.grab_focus()
        else:
            self.search_entry.set_text("")          # 触发 changed → 恢复列表
            self.search_row.hide()
            self.filter_query = None
            self.render(keep_scroll=False)
        self.update_hint()

    def _on_search_changed(self, entry):
        text = entry.get_text().strip()
        if text == (self.filter_query or ""):
            return
        self.filter_query = text or None
        self.render(keep_scroll=False)

    def _on_search_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.toggle_search(False)
            return True
        return False

    def add_from_entry(self):
        text = self.entry.get_text()
        item = self.store.add(text)
        self.entry.set_text("")
        self._refresh_tags()
        if item is not None:
            self.insert_item(item)
        self.entry.grab_focus()

    def insert_item(self, item):
        """新增一条：只插这一行（新条目总在未完成组最前），不整表重建。"""
        state = self.store.state_of(item)
        visible = self._visible_items()
        header = self.section_widgets.get(state)
        if item in visible and header is not None:
            row = Row(self, item)
            self.listbox.pack_start(row, False, False, 0)
            try:
                first = self.listbox.get_children().index(header) + 1
            except ValueError:
                first = None
            if first is None:
                self.render(keep_scroll=False)
                return
            self.listbox.reorder_child(row, first)
            row.show_all()
            self.rows[item["id"]] = row
            position = visible.index(item)
            self.row_widgets.insert(position, (item["id"], row))
        else:
            self.render(keep_scroll=False)
            return
        self._update_counts()
        self._update_footer()
        self.fit_window()

    # ---- 单条操作（只重画该行） ----
    def toggle_item(self, item_id):
        item = self.store.toggle(item_id)
        if item is None:
            return
        self._last_toggle = (item_id, time.time())
        self.redraw_row(item_id)
        self._update_footer()
        self.schedule_regroup()

    def toggle_waiting_item(self, item_id):
        item = self.store.toggle_waiting(item_id)
        if item is None:
            return
        self.redraw_row(item_id)
        self._update_footer()
        self.schedule_regroup()

    def toggle_today_item(self, item_id):
        item = self.store.toggle_today(item_id)
        if item is None:
            return
        self.store.place_in_bucket(item_id)
        self.render()

    def set_priority_item(self, item_id, value):
        item = self.store.set_priority(item_id, value)
        if item is None:
            return
        self.store.place_in_bucket(item_id)
        self.render()

    def toggle_step_item(self, item_id, index):
        item = self.store.toggle_step(item_id, index)
        if item is None:
            return
        self.redraw_row(item_id)
        self._update_footer()

    def remove_item(self, item_id):
        record = self.store.pop(item_id)
        if record is None:
            return
        self.dismiss_alert()
        self.render()
        self.show_undo(record)

    def undo_delete(self):
        if self._undo is None:
            return
        record = self._undo
        self._undo = None
        self._hide_undo()
        if self.store.restore(record):
            self.render()

    def show_undo(self, record):
        text = record["item"]["text"].strip()
        if len(text) > 14:
            text = text[:14] + "…"
        self._undo = record
        self._undo_text = text
        self.undo_btn.set_label(tr("撤销删除", "Undo delete"))
        self.undo_btn.set_no_show_all(False)
        self.undo_btn.show()
        self.update_hint()
        if self._undo_job is not None:
            try:
                GLib.source_remove(self._undo_job)
            except Exception:
                pass
        self._undo_job = GLib.timeout_add(
            int(UNDO_SECONDS * 1000), lambda: (self._hide_undo(), False)[1])

    def _hide_undo(self):
        self._undo = None
        if self._undo_job is not None:
            try:
                GLib.source_remove(self._undo_job)
            except Exception:
                pass
            self._undo_job = None
        self.undo_btn.hide()

    def clear_done(self):
        moved = self.store.clear_done()
        if moved:
            self.render()
            self.flash_hint(tr("已归档 %d 条", "Archived %d") % moved)

    def toggle_done_collapsed(self):
        self._done_collapsed = not self._done_collapsed
        self.store.ui["done_collapsed"] = self._done_collapsed
        self.store.save()
        self.render(keep_scroll=False)

    # ---- 归位（延迟 1.2s，给双击改名让路） ----
    def schedule_regroup(self):
        self._cancel_regroup()
        if self._edit_id is not None or self._steps_edit_id is not None:
            return
        self._regroup_job = GLib.timeout_add(
            self.REGROUP_DELAY_MS, lambda: (self._regroup_now(), False)[1])

    def _cancel_regroup(self):
        if self._regroup_job is not None:
            try:
                GLib.source_remove(self._regroup_job)
            except Exception:
                pass
            self._regroup_job = None

    def _regroup_now(self):
        self._regroup_job = None
        if self._edit_id is not None or self._steps_edit_id is not None:
            self.schedule_regroup()
            return
        if self.drag_id is not None:
            self.schedule_regroup()
            return
        self.store.resort()
        self.render()

    # ---- 改名 ----
    @property
    def edit_open(self):
        return self._edit_id is not None

    @property
    def editing_id(self):
        return self._edit_id

    @property
    def steps_edit_open(self):
        return self._steps_edit_id is not None

    @property
    def steps_edit_id(self):
        return self._steps_edit_id

    def begin_edit(self, item_id):
        if self.edit_open and self._edit_id == item_id:
            return
        self.end_edit_all(save=True)
        self._cancel_regroup()
        row = self.rows.get(item_id)
        item = self.find_item(item_id)
        if row is None or item is None:
            return
        self._edit_id = item_id
        row.begin_edit()
        self.update_hint()

    def end_edit(self, save=True, silend=False):
        row = self.rows.get(self._edit_id) if self._edit_id is not None else None
        item_id = self._edit_id
        self._edit_id = None
        if row is None:
            if not silend:
                self.update_hint()
            return False
        text = row.edit_text()
        row.clear_edit()
        changed = False
        if save and item_id is not None:
            changed = bool(self.store.rename(item_id, text))
            self.redraw_row(item_id)
            self._update_footer()
        if not silend:
            self.schedule_regroup()
            self.update_hint()
        return changed

    def end_steps_edit(self, save=True):
        row = self.rows.get(self._steps_edit_id) if self._steps_edit_id else None
        item_id = self._steps_edit_id
        self._steps_edit_id = None
        if row is None:
            self.update_hint()
            return False
        changed = False
        if save and item_id is not None:
            steps = row.steps_texts()
            record = self.store.set_steps(item_id, steps)
            if record is not None:
                changed = True
            self.redraw_row(item_id)
            self._update_footer()
        row.clear_steps_edit()
        self.schedule_regroup()
        self.update_hint()
        GLib.idle_add(lambda: (self.fit_window(), False)[1])
        return changed

    def end_edit_all(self, save=True):
        if self.edit_open:
            self.end_edit(save=save)
        if self.steps_edit_open:
            self.end_steps_edit(save=save)

    def begin_steps_edit(self, item_id, step_index=None):
        if self.steps_edit_open and self._steps_edit_id == item_id:
            row = self.rows.get(item_id)
            if row is not None:
                row._steps_focus(step_index or self._steps_active)
            return
        self.end_edit_all(save=True)
        row = self.rows.get(item_id)
        item = self.find_item(item_id)
        if row is None or item is None:
            return
        self._cancel_regroup()
        self._steps_edit_id = item_id
        row.begin_steps_edit(focus_index=step_index)
        self.update_hint()

    # ---- 标签筛选 ----
    def _refresh_tags(self):
        tags = self.store.all_tags()
        if self.filter_tag and self.filter_tag not in tags:
            self.filter_tag = None
        existing = getattr(self, "_tag_values", None)
        if existing == (tags, self.filter_tag):
            return
        self._tag_values = (tags, self.filter_tag)
        for child in self.tags_row.get_children():
            child.destroy()
        if not tags:
            self.tags_row.hide()
            return
        self.tags_row.show()
        buttons = []

        def make_chip(label, value):
            button = Gtk.Button(label=label)
            button.get_style_context().add_class("chip")
            context = button.get_style_context()
            active = value == self.filter_tag or (value is None
                                                  and self.filter_tag is None)
            if active:
                context.add_class("on")
            button.connect("clicked", lambda _b, v=value: self.set_filter(v))
            buttons.append(button)
            self.tags_row.pack_start(button, False, False, 0)

        make_chip(tr("全部", "All"), None)
        for tag in tags:
            make_chip("@" + tag, tag)
        self.tags_row.show_all()

    def set_filter(self, tag):
        self.filter_tag = tag
        self.render(keep_scroll=False)

    # ---- 右键 ----
    def popup_row_menu(self, row, event):
        self.end_edit_all(save=True)
        item = self.find_item(row.item_id)
        if item is None:
            return
        if self._row_menu is not None:
            try:
                self._row_menu.destroy()
            except Exception:
                pass
        menu = Gtk.Menu()
        waiting = self.store.state_of(item) == STATE_WAITING
        mix = Gtk.MenuItem(label=tr("取消「等待中」", "Clear \"Waiting\"") if waiting
                          else tr("标记为「等待中」", "Mark as \"Waiting\""))
        mix.connect("activate", lambda _i: self.toggle_waiting_item(item["id"]))
        menu.append(mix)
        today = bool(item.get("today"))
        mix = Gtk.MenuItem(label=tr("取消「今天要办的」", "Clear \"Today\"") if today
                          else tr("标记为「今天要办的」", "Mark as \"Today\""))
        mix.connect("activate", lambda _i: self.toggle_today_item(item["id"]))
        menu.append(mix)

        priority = self.store.priority_of(item)
        sub = Gtk.Menu()
        for value, label in ((PRIORITY_HIGH, tr("高", "High")),
                             (PRIORITY_MID, tr("中", "Mid")),
                             (PRIORITY_NONE, tr("无", "None"))):
            entry = Gtk.RadioMenuItem(label=label)
            if value == priority:
                entry.set_active(True)
            entry.connect("activate",
                          lambda _i, v=value: self.set_priority_item(item["id"], v))
            sub.append(entry)
        priority_item = Gtk.MenuItem(label=tr("优先级（现在：%s）", "Priority (now: %s)") %
                                     {PRIORITY_HIGH: tr("高", "High"),
                                      PRIORITY_MID: tr("中", "Mid")}.get(
                                         priority, tr("无", "None")))
        priority_item.set_submenu(sub)
        menu.append(priority_item)

        reminder = item.get("remind_at")
        label = (tr("改提醒（%s）…", "Change reminder (%s)…")
                 % format_remind_time(reminder)) if reminder else tr("设提醒…", "Set reminder…")
        mix = Gtk.MenuItem(label=label)
        mix.connect("activate", lambda _i: self.ask_reminder(item["id"]))
        menu.append(mix)
        if reminder:
            mix = Gtk.MenuItem(label=tr("取消提醒", "Clear reminder"))
            mix.connect("activate", lambda _i: self.clear_reminder_item(item["id"]))
            menu.append(mix)

        steps = self.store.steps_of(item)
        done_steps, total_steps = self.store.steps_progress(item)
        label = tr("编辑子步骤…", "Edit sub-steps…")
        if total_steps:
            label = tr("编辑子步骤（%d/%d 已完成）…", "Edit sub-steps (%d/%d done)…") \
                % (done_steps, total_steps)
        mix = Gtk.MenuItem(label=label)
        mix.connect("activate", lambda _i: self.begin_steps_edit(item["id"]))
        menu.append(mix)
        mix = Gtk.MenuItem(label=tr("＋ 加一条子步骤", "＋ Add a sub-step"))
        mix.connect("activate", lambda _i: self.add_step_item(item["id"]))
        menu.append(mix)
        if steps:
            mix = Gtk.MenuItem(label=tr("清空子步骤", "Clear sub-steps"))
            mix.connect("activate", lambda _i: self.clear_steps(item["id"]))
            menu.append(mix)
        menu.append(Gtk.SeparatorMenuItem())
        mix = Gtk.MenuItem(label=tr("删除这条", "Delete this"))
        mix.connect("activate", lambda _i: self.remove_item(item["id"]))
        menu.append(mix)
        menu.show_all()
        self._row_menu = menu
        menu.popup_at_pointer(event)

    def add_step_item(self, item_id):
        steps = self.store.steps_of(self.find_item(item_id))
        if len(steps) >= STEP_MAX:
            self.flash_hint(tr("一条最多 %d 个子步骤", "At most %d sub-steps") % STEP_MAX)
            return
        self.store.add_step(item_id, "新步骤")
        self.redraw_row(item_id)
        self._update_footer()

    def clear_steps(self, item_id):
        self.store.set_steps(item_id, [])
        self.redraw_row(item_id)
        self._update_footer()

    # ---- 窗口菜单动作 ----
    def popup_window_menu(self, event):
        self.window_menu.popup_at_pointer(event)

    def toggle_collapsed(self):
        collapsed = not bool(self.store.ui.get("collapsed"))
        if collapsed:
            width, height = self.win.get_size()
            self._expanded_size = (width, height)
        self.store.ui["collapsed"] = collapsed
        self.store.save()
        self._apply_collapsed()

    def _apply_collapsed(self):
        collapsed = bool(self.store.ui.get("collapsed"))
        for widget in (self.input_row, self.tags_row, self.search_row,
                       self.scrolled, self.footer):
            if collapsed:
                widget.hide()
            elif widget is self.search_row:
                widget.set_visible(bool(self.filter_query))
            else:
                widget.show()
        if collapsed or not getattr(self, "_session_warn_on", False):
            self.session_warn.hide()
        else:
            self.session_warn.show()
        self._draw_collapse_icon(collapsed)
        if collapsed:
            width, height = self._collapsed_size()
            # 用 panel_w（展开宽）当锚点基准：启动即折叠时窗口尺寸还不可靠
            self._resize_anchored(width, height, from_width=self.panel_w)
        else:
            width = self.panel_w
            height = None
            if self._expanded_size:
                width, height = self._expanded_size
            elif self.store.ui.get("width") and self.store.ui.get("height"):
                width = int(self.store.ui["width"])
                height = int(self.store.ui["height"])
            if width != self.panel_w:
                self.panel_w = max(PANEL_W_MIN, min(PANEL_W_MAX, int(width)))
            # 先改大、再换回展开态 header：反过来 GTK 会先按"内容最小尺寸"
            # 把窗口撑到 248×204，然后把我们的 resize 盖掉（实测）。
            self._resize_anchored(self.panel_w,
                                  int(height) if height
                                  else max(140, self.win.get_size()[1]))
            self._compact_header(False)
            self.fit_window()
        self.update_hint()

    def _compact_header(self, compact):
        """折叠态把两行 header 换成单行：标题 13pt + 计数并排，边距收紧。"""
        if compact:
            self.header.get_style_context().add_class("compact")
            self.header_left.hide()
            # no_show_all 会让 show_all() 直接跳过，只能逐个 show()
            self.compact_line.show()
            self.compact_title.show()
            self.compact_count.show()
            self.header.set_spacing(6)
            self.header.set_margin_start(9)
            self.header.set_margin_end(4)
            self.header.set_margin_top(3)
            self.header.set_margin_bottom(3)
        else:
            self.header.get_style_context().remove_class("compact")
            self.compact_line.hide()
            self.header_left.show_all()
            self.header.set_spacing(8)
            self.header.set_margin_start(16)
            self.header.set_margin_end(10)
            self.header.set_margin_top(12)
            self.header.set_margin_bottom(6)

    def _collapsed_size(self):
        """折叠态窗口尺寸：单行 header 的自然宽 + 卡片边框 2px。

        宽度随内容，不再跟着展开时的 window width 走；高度按单行 header 算，
        不再加展开态那 14px 留白（行为契约 §2.8 写的也是 header + 2）。
        """
        self._compact_header(True)
        _min_w, natural_w = self.header.get_preferred_width()
        min_h, _natural_h = self.header.get_preferred_height()
        return int(natural_w) + 2, int(min_h) + 2

    def _resize_anchored(self, width, height, from_width=None):
        """改窗口尺寸时保持"离哪边近，哪边不动"，贴边收窄/展开不会漂走。

        from_width：这次变更前的宽度基准。折叠时要传 panel_w —— 启动即折叠
        时 win.get_size() 还是窗口没 resize 前的旧值，拿它算锚点会把窗口丢在
        屏幕中间。

        move 必须等 resize 生效之后再发：KWin 会按"当前宽度"把窗口夹回屏幕内，
        先 move 再收窄的话（比如想贴右边、旧宽度下越界）会被夹到屏幕中间。
        """
        screen_w, _screen_h = screen_size()
        x, y = self.win.get_position()
        old_w = self.win.get_size()[0] if from_width is None else from_width
        if not self.win.get_mapped() or (int(x) == 0 and int(y) == 0):
            # 还没 map（启动即折叠）：get_position() 是 0,0，拿它算锚点会把
            # 窗口丢到左上角。改用落盘的展开位置当基准。
            x = int(self.store.ui.get("x") or 0)
            y = int(self.store.ui.get("y") or 0)
            x = max(0, min(x, int(screen_w - old_w - 16)))
        if old_w > 0 and x + old_w / 2.0 > screen_w / 2.0:
            x += old_w - width
        x = max(0, min(int(x), int(screen_w - width)))
        self._anchor_pending = (int(x), int(y), int(width), int(height))
        self._apply_geometry(width, height)
        GLib.timeout_add(160, self._flush_anchor_fallback)

    def _flush_anchor(self, width):
        """新宽度到位后把窗口挪到锚点位置（宽度一致才动，动画中间帧不算）。"""
        pending = self._anchor_pending
        if pending is None or abs(int(width) - pending[2]) > 2:
            return
        self._anchor_pending = None
        self.win.move(pending[0], pending[1])

    def _flush_anchor_fallback(self):
        pending = self._anchor_pending
        if pending is not None:                  # 没收到匹配的 Configure 也兜底挪
            self._anchor_pending = None
            self.win.move(pending[0], pending[1])
        return False

    def _draw_collapse_icon(self, collapsed):
        button = self.collapse_btn
        child = button.get_child()
        if child is not None:
            button.remove(child)
        button.add(icon_image("pan-end-symbolic" if collapsed else "pan-up-symbolic", 15))
        button.show_all()

    def toggle_hidden(self):
        if self.win.get_visible():
            self.win.hide()
        else:
            self.raise_window()

    def _on_sound_toggled(self, item):
        self.store.ui["sound"] = bool(item.get_active())
        self.store.save()

    def _on_topmost_toggled(self, item):
        value = bool(item.get_active())
        self.store.ui["topmost"] = value
        self.win.set_keep_above(value)
        self.store.save()

    def _on_autostart_toggled(self, item):
        want = bool(item.get_active())
        if want:
            autostart_install()
        else:
            autostart_uninstall()
        self._autostart_item.set_active(os.path.exists(autostart_path()))
        self.flash_hint(tr("开机自启：%s", "Start on login: %s")
                        % tr("已开启", "on") if want else
                        tr("开机自启：%s", "Start on login: %s") % tr("已关闭", "off"))

    def set_alpha(self, value, save=True):
        alpha = max(0.3, min(1.0, float(value)))
        set_window_opacity(self.win, alpha)
        if save:
            self.store.ui["alpha"] = alpha
            self.store.save()

    def open_alpha_dialog(self):
        if self._alpha_dialog is not None:
            self._alpha_dialog.present()
            return self._alpha_dialog
        dialog = Gtk.Dialog(title=tr("窗口透明度", "Window opacity"), transient_for=self.win)
        dialog.get_style_context().add_class("dlg")
        dialog.add_button(tr("关闭", "Close"), Gtk.ResponseType.CLOSE)
        dialog.set_resizable(False)
        box = dialog.get_content_area()
        box.set_spacing(4)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(8)
        current = int(round(float(self.store.ui.get("alpha") or 0.94) * 100))
        label = Gtk.Label(label="%d%%" % current)
        label.get_style_context().add_class("dlg-label")
        box.pack_start(label, False, False, 0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 30, 100, 1)
        scale.set_value(current)
        scale.set_size_request(220, -1)
        scale.connect("value-changed",
                      lambda sc: (label.set_text("%d%%" % int(sc.get_value())),
                                  self.set_alpha(sc.get_value() / 100.0, save=False)))
        box.pack_start(scale, False, False, 0)
        tip = Gtk.Label(label=tr("拖动即时预览，关掉即保存", "Live preview, saved on close"))
        tip.get_style_context().add_class("hint")
        box.pack_start(tip, False, False, 0)
        dialog.connect("response", lambda _d, _r: dialog.close())
        dialog.connect("destroy", lambda _d: self._alpha_dialog_destroyed())
        dialog.show_all()
        self._alpha_dialog = dialog
        return dialog

    def _alpha_dialog_destroyed(self):
        self.set_alpha(self.win.get_opacity(), save=True)
        self._alpha_dialog = None

    def open_archive(self):
        existing = self._archive_window
        if existing is not None:
            try:
                existing.show()
                return existing
            except Exception:
                self._archive_window = None
        self._archive_window = ArchiveWindow(self)
        return self._archive_window

    # ---- 双击 / 点别处 ----
    def is_double_click(self, item_id):
        last_id, last_at = self._last_click
        return last_id == item_id and (time.time() - last_at) < 0.35

    def handle_double_click(self, row):
        """双击条目：撤回第一次单击的勾选，进入改名。"""
        item_id = row.item_id
        # 第一次点击把状态翻过一次了：这里翻回去，恢复双击前的样子
        self.store.toggle(item_id)
        self.redraw_row(item_id)
        self._update_footer()
        self._last_click = (None, 0.0)
        self._last_toggle = (None, 0.0)
        self.begin_edit(item_id)

    def _on_window_press(self, _widget, event):
        """点编辑框/编辑器以外的地方：保存退出（与 v0.2.x 一致）。"""
        if event.button != 1:
            return False
        if not (self.edit_open or self.steps_edit_open):
            return False
        target = None
        try:
            target = Gtk.get_event_widget(event)
        except Exception:
            target = None
        editing_id = self._edit_id if self._edit_id is not None else self._steps_edit_id
        row = self.rows.get(editing_id)
        node = target
        while node is not None and node is not self.win:
            if row is not None and node is row:
                return False             # 点在正在编辑的那一行里，不退出
            node = node.get_parent()
        self.end_edit_all(save=True)
        self._press = None
        return False

    # ---- 头部拖动 / 菜单 ----
    def _on_header_press(self, _widget, event):
        if event.button == 3:
            self.popup_window_menu(event)
            return True
        if event.button != 1:
            return False
        now = time.time()
        last = getattr(self, "_header_last_press", None)
        self._header_last_press = (now, event.x_root, event.y_root)
        if last is not None and (now - last[0]) < 0.35 \
                and abs(event.x_root - last[1]) < 6 \
                and abs(event.y_root - last[2]) < 6:
            self._header_drag = None
            self._header_last_press = None
            self.toggle_collapsed()
            return True
        x, y = self.win.get_position()
        self._header_drag = {"x": int(event.x_root - x),
                             "y": int(event.y_root - y), "moved": False}
        return True

    def _on_header_motion(self, _widget, event):
        if self._header_drag is None:
            return False
        self._header_drag["moved"] = True
        self.win.move(int(event.x_root - self._header_drag["x"]),
                      int(event.y_root - self._header_drag["y"]))
        return True

    def _on_header_release(self, _widget, event):
        if event.button != 1 or self._header_drag is None:
            return False
        if self._header_drag.get("moved"):
            x, y = self.win.get_position()
            if self.store.ui.get("collapsed"):
                # 折叠态存"展开后的位置"，否则下次展开/重启窗口会漂
                width = self.win.get_size()[0]
                if x + width / 2.0 > screen_size()[0] / 2.0:
                    x = x + width - self.panel_w
            self.store.ui["x"] = int(x)
            self.store.ui["y"] = int(y)
            self.store.save()
        self._header_drag = None
        return False

    # ---- 拖拽排序 ----
    def _row_origin_y(self, row):
        """这一行在屏幕上的顶边 y（用 translate_coordinates，别依赖 GdkWindow.get_origin）。"""
        try:
            ok, _x, y = row.translate_coordinates(self.win, 0, 0)
            if ok:
                _win_x, win_y = self.win.get_position()
                return int(win_y + y)
        except Exception:
            pass
        gdk_window = row.get_window()
        if gdk_window is None:
            return 0
        return int(gdk_window.get_origin()[1])

    def row_pressed(self, row, event):
        item = self.find_item(row.item_id)
        if item is None:
            return
        self._press = {"id": row.item_id, "y": event.y_root,
                       "time": time.time(), "moved": False, "event": event}

    def row_motion(self, row, event):
        press = self._press
        if press is None or press["id"] != row.item_id:
            return
        if self.drag_id is None:
            if abs(event.y_root - press["y"]) < 5:
                return
            self.begin_drag(row.item_id)
        self.drag_to(event.y_root)

    def row_released(self, row, event):
        press, self._press = self._press, None
        if self.drag_id is not None:
            self.end_drag()
            return True
        if press is None or press["id"] != row.item_id:
            return False
        if abs(event.y_root - press["y"]) < 5:
            item_id = row.item_id
            if self._edit_id == item_id or self._steps_edit_id == item_id:
                return False
            self.toggle_item(item_id)
            self._last_click = (item_id, time.time())
            return True
        return False

    def begin_drag(self, item_id):
        self._cancel_regroup()
        self.drag_id = item_id
        row = self.rows.get(item_id)
        if row is not None:
            row.get_style_context().add_class("selected")
            row.show_actions(True)
        line = Gtk.Box()
        line.get_style_context().add_class("drophint")
        line.set_margin_top(0)
        line.set_margin_bottom(0)
        self.drop_line = line
        self.listbox.pack_start(line, False, False, 0)
        line.show()

    def _drag_target_index(self, y_root):
        if not self.row_widgets:
            return 0
        target = None
        for index, (item_id, row) in enumerate(self.row_widgets):
            top = self._row_origin_y(row)
            height = row.get_allocated_height()
            if top <= y_root < top + height:
                target = index
                break
        if target is None:
            first_top = self._row_origin_y(self.row_widgets[0][1])
            target = 0 if y_root < first_top else len(self.row_widgets) - 1
        return target

    def drag_to(self, y_root):
        if self.drop_line is None or not self.row_widgets:
            return
        target = self._drag_target_index(y_root)
        target = self._clamp_to_group(self.drag_id, target)
        self.drag_target = target
        row = self.rows[self.drag_id]
        base = self.row_widgets.index((self.drag_id, row))
        # 行下标 → listbox 子控件位置（中间还有分组标题，不能直接拿行号当位置）
        target_row = self.row_widgets[target][1]
        try:
            node = self.listbox.get_children().index(target_row)
        except ValueError:
            return
        if target > base:
            node += 1
        self.listbox.reorder_child(self.drop_line, node)

    def _bucket_of(self, item):
        return self.store.state_of(item)

    def _clamp_to_group(self, item_id, target):
        item = self.find_item(item_id)
        if item is None or not self.row_widgets:
            return target
        bucket = self._bucket_of(item)
        indices = [index for index, (other, _row) in enumerate(self.row_widgets)
                   if self._bucket_of(self.find_item(other)) == bucket]
        if not indices:
            return target
        return max(indices[0], min(target, indices[-1]))

    def end_drag(self):
        item_id, target = self.drag_id, self.drag_target
        self.drag_id = None
        self.drag_target = None
        if self.drop_line is not None:
            try:
                self.drop_line.destroy()
            except Exception:
                pass
            self.drop_line = None
        row = self.rows.get(item_id)
        if row is not None:
            row.get_style_context().remove_class("selected")
            row.show_actions(False)
        if item_id is not None and target is not None:
            data_index = self._store_index_for_row(target)
            self.store.move(item_id, data_index, save=False)
        self.store.save()
        self.render()
        self.schedule_regroup()

    def _store_index_for_row(self, row_index):
        if not self.row_widgets:
            return row_index
        try:
            row_index = max(0, min(int(row_index), len(self.row_widgets) - 1))
        except (TypeError, ValueError):
            return 0
        item_id = self.row_widgets[row_index][0]
        for index, item in enumerate(self.store.items):
            if item["id"] == item_id:
                return index
        return row_index

    # ---- 提醒 ----
    def ask_reminder(self, item_id):
        item = self.find_item(item_id)
        if item is None:
            return
        if self._reminder_dialog is not None:
            try:
                self._reminder_dialog.destroy()
            except Exception:
                pass
            self._reminder_dialog = None
        dialog = Gtk.Dialog(title=tr("设提醒", "Set reminder"), transient_for=self.win)
        dialog.add_button(tr("取消", "Cancel"), Gtk.ResponseType.CANCEL)
        ok = dialog.add_button(tr("设好", "Set"), Gtk.ResponseType.OK)
        ok.set_receives_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(12)
        box.set_margin_bottom(8)
        label = Gtk.Label(label=tr("什么时候提醒（HH:MM）", "When to remind (HH:MM)"))
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)
        entry = Gtk.Entry()
        current = item.get("remind_at")
        if current:
            entry.set_text(format_remind_time(current))
            entry.select_region(0, -1)
        else:
            entry.set_placeholder_text(tr("比如 15:30（过了这个点就落到明天）",
                                                "e.g. 15:30 (past today → tomorrow)"))
        box.pack_start(entry, False, False, 0)
        error = Gtk.Label(label="")
        error.get_style_context().add_class("dlg-error")
        error.set_xalign(0)
        box.pack_start(error, False, False, 0)
        dialog.show_all()
        self._reminder_dialog = dialog

        def submit(_dialog=None, _response=None):
            when = parse_remind_time(entry.get_text())
            if when is None:
                error.set_text(tr("写成 15:30 这样", "Use a time like 15:30"))
                return
            self.store.set_reminder(item_id, when)
            self.redraw_row(item_id)
            self._update_footer()
            dialog.destroy()
            self._reminder_dialog = None
            self.flash_hint(tr("已设提醒：%s", "Reminder set: %s")
                        % format_remind_time(when))

        entry.connect("activate", lambda _e: submit())
        dialog.connect("response", lambda d, response: submit() if response == Gtk.ResponseType.OK else d.destroy())
        entry.grab_focus()

    def clear_reminder_item(self, item_id):
        self.store.clear_reminder(item_id)
        self.redraw_row(item_id)
        self._update_footer()
        self.flash_hint(tr("已取消提醒", "Reminder cleared"))

    def set_reminder_item(self, item_id, when):
        self.store.set_reminder(item_id, when)
        self.redraw_row(item_id)
        self._update_footer()
        self.flash_hint(tr("已设提醒：%s", "Reminder set: %s")
                        % format_remind_time(when))

    def _remind_tick(self, raise_it=True):
        self._remind_job = None
        try:
            due = self.store.due_reminders()
        except Exception:
            due = []
        if due:
            for item in due:
                item["remind_at"] = None
            self.store.save()
            for item in due:
                self.redraw_row(item["id"])
            self._update_footer()
            self.show_alert(due)
            try:
                if self.store.ui.get("sound", True):
                    Gdk.beep()
            except Exception:
                pass
            if raise_it:
                self.raise_window()
        self._remind_job = GLib.timeout_add(
            REMIND_POLL_MS, lambda: (self._remind_tick(True), False)[1])
        return False

    def show_alert(self, items):
        names = []
        for item in items[:3]:
            text = item["text"].strip()
            names.append(text[:16] + ("…" if len(text) > 16 else ""))
        self._alert = items
        self.hint.get_style_context().add_class("warn")
        self.hint.set_text("⏰ %s%s" % (tr("；", ", ").join(names),
                                      tr(" 等 %d 条", " and %d more") % len(items)
                                      if len(items) > 3 else ""))
        self.alert_btn.set_no_show_all(False)
        self.alert_btn.show()
        self.undo_btn.hide()

    def dismiss_alert(self):
        self._alert = None
        self.alert_btn.hide()
        self.hint.get_style_context().remove_class("warn")
        self._update_footer()

    def _remind_stop(self):
        if self._remind_job is not None:
            try:
                GLib.source_remove(self._remind_job)
            except Exception:
                pass
            self._remind_job = None

    # ---- 归档 ----
    def _archive_on_start(self):
        self._archive_tick()

    def _archive_tick(self):
        day = self.store.day_start()
        if self._archived_day == day:
            return 0
        self._archived_day = day
        moved = self.store.archive_completed()
        if moved:
            self.render()
        return moved

    # ---- 唤回 ----
    def raise_window(self, timestamp=None):
        activate_window(self.root, self.store.ui["topmost"], timestamp=timestamp or 0)

    def add_from_ipc(self, text):
        """另一个进程通过套接字加一条（--add）：不新开窗口、不抢焦点。"""
        item = self.store.add(text)
        if item is None:
            return
        self.render(keep_scroll=False)
        self.flash_hint(tr("已添加：%s", "Added: %s") % item["text"])
        if self.store.ui.get("collapsed"):
            self.toggle_collapsed()

    def _bind_wheel(self, widget):
        widget.add_events(Gdk.EventMask.SCROLL_MASK)
        widget.connect("scroll-event", self._on_wheel)

    def _on_wheel(self, _widget, event):
        if event.direction in (Gdk.ScrollDirection.UP, Gdk.ScrollDirection.SMOOTH):
            dy = event.delta_y if hasattr(event, "delta_y") else -2.0
            self.scrolled.get_vadjustment().set_value(
                self.scrolled.get_vadjustment().get_value() + dy * 12)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.scrolled.get_vadjustment().set_value(
                self.scrolled.get_vadjustment().get_value() + 24)
        return True

    # ---- 退出 ----
    def quit(self):
        if getattr(self, "_quitting", False):
            return False
        self._quitting = True
        try:
            x, y = self.win.get_position()
            self.store.ui["x"] = int(x)
            self.store.ui["y"] = int(y)
        except Exception:
            pass
        self._remind_stop()
        self._cancel_regroup()
        hotkey_remove()
        single_instance_stop(self.root)
        self.store.resort()
        self.store.save()
        try:
            self.win.destroy()
        except Exception:
            pass
        if Gtk.main_level() > 0:
            Gtk.main_quit()
        return False



# --------------------------------------------------------------------------- #
# 归档浏览器
# --------------------------------------------------------------------------- #
class ArchiveWindow(object):
    """查看归档（完成的 / 清掉的 / 删掉的），挑一条恢复回列表。"""

    REASONS = {
        "done": (tr("完成", "Done"), "badge today"),
        "cleared": (tr("清掉", "Cleared"), "badge mid"),
        "deleted": (tr("删除", "Deleted"), "badge hi"),
    }

    def __init__(self, app):
        self.app = app
        self._drag = None
        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title(tr("归档 · 桌面待办清单", "Archive · Desktop To-do"))
        self.win.set_decorated(False)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_transient_for(app.win)
        self.win.set_destroy_with_parent(True)
        self.win.set_default_size(360, 420)
        self.win.connect("key-press-event", self._on_key)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("card")
        self.win.add(card)

        header_event = Gtk.EventBox()
        header_event.set_visible_window(False)
        header_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                                | Gdk.EventMask.BUTTON_RELEASE_MASK
                                | Gdk.EventMask.POINTER_MOTION_MASK)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(10)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header_event.add(header)
        header_event.connect("button-press-event", self._on_header_press)
        header_event.connect("button-release-event", self._on_header_release)
        header_event.connect("motion-notify-event", self._on_header_motion)
        self.title = Gtk.Label(label=tr("归档", "Archive"), xalign=0.0)
        self.title.get_style_context().add_class("title")
        header.pack_start(self.title, True, True, 0)
        export_btn = Gtk.Button(label=tr("导出…", "Export…"))
        export_btn.get_style_context().add_class("linkbtn")
        export_btn.set_relief(Gtk.ReliefStyle.NONE)
        export_btn.set_tooltip_text(tr("导出全部待办和归档为 JSON 备份",
                                              "Export all to-dos and archive as JSON"))
        export_btn.connect("clicked", lambda _b: self._export_dialog())
        header.pack_end(export_btn, False, False, 0)
        close = icon_button("window-close-symbolic", 15, tooltip=tr("关闭", "Close"))
        close.connect("clicked", lambda _b: self.close())
        header.pack_end(close, False, False, 0)
        card.pack_start(header_event, False, False, 0)

        self.listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_max_content_height(420)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_margin_start(8)
        scrolled.set_margin_end(8)
        scrolled.set_margin_bottom(8)
        scrolled.add(self.listbox)
        card.pack_start(scrolled, True, True, 0)
        self.scrolled = scrolled

        self.refresh()
        self.win.show_all()
        self._place()
        self.win.connect("destroy", lambda _w: self._on_destroy())

    # ---- 位置 ----
    def _place(self):
        screen_w, screen_h = screen_size()
        try:
            main_x, main_y = self.app.win.get_position()
            main_w, main_h = self.app.win.get_size()
        except Exception:
            main_x, main_y, main_w, main_h = 0, 0, 360, 480
        width, height = self.win.get_size()
        x = main_x + main_w - width
        y = main_y + main_h - height
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        self.win.move(int(x), int(y))

    # ---- 列表 ----
    def refresh(self):
        for child in self.listbox.get_children():
            child.destroy()
        records = sorted(self.app.store.archive,
                         key=lambda record: record.get("archived_at") or 0,
                         reverse=True)
        self.title.set_text(tr("归档", "Archive") if not records
                          else tr("归档 · %d 条", "Archive · %d") % len(records))
        if not records:
            tip = Gtk.Label(label=tr("还没有归档。\n完成的、清掉的、删掉的条目都会留在这里。",
                               "No archive yet.\nCompleted, cleared and deleted items are kept here."))
            tip.set_justify(Gtk.Justification.CENTER)
            tip.get_style_context().add_class("empty-sub")
            tip.set_margin_top(24)
            tip.set_margin_bottom(24)
            self.listbox.pack_start(tip, False, False, 0)
        else:
            for record in records:
                self.listbox.pack_start(self._make_row(record), False, False, 0)
        self.listbox.show_all()

    def _make_row(self, record):
        item = record.get("item") or {}
        reason = self.REASONS.get(record.get("reason") or "done",
                                       (tr("完成", "Done"), "badge today"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(8)
        row.set_margin_end(8)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        badge = Gtk.Label(label=reason[0])
        badge.get_style_context().add_class("badge")
        badge.get_style_context().add_class(reason[1])
        badge.set_valign(Gtk.Align.START)
        row.pack_start(badge, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text = Gtk.Label(label=str(item.get("text") or "（空）"), xalign=0.0)
        text.set_line_wrap(True)
        text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.get_style_context().add_class("step")
        when = record.get("archived_at")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else ""
        meta = Gtk.Label(label=stamp, xalign=0.0)
        meta.get_style_context().add_class("meta")
        body.pack_start(text, False, False, 0)
        body.pack_start(meta, False, False, 0)
        row.pack_start(body, True, True, 0)

        restore = Gtk.Button(label=tr("恢复", "Restore"))
        restore.connect("clicked", lambda _b, r=record: self._restore(r))
        row.pack_end(restore, False, False, 0)
        return row

    def _restore(self, record):
        if self.app.store.restore(record):
            self.app.render()
            self.refresh()
            self.app.flash_hint(tr("已恢复 1 条", "Restored 1 item"))
            return True
        self.app.flash_hint(tr("这一条已经在列表里了", "That item is already in the list"))
        return False

    def _export_dialog(self):
        """从归档窗口导出完整数据（JSON）；失败不打断主窗口。"""
        dialog = Gtk.FileChooserDialog(
            title=tr("导出待办数据", "Export to-do data"), transient_for=self.win,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(tr("取消", "Cancel"), Gtk.ResponseType.CANCEL,
                           tr("导出", "Export"), Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("desktop-todo-%s.json"
                                % time.strftime("%Y%m%d-%H%M%S"))
        try:
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                path = dialog.get_filename()
                if path:
                    count = self.app.store.export_json(path)
                    self.app.flash_hint(tr("已导出 %d 条到 %s", "Exported %d items to %s")
                                        % (count, os.path.basename(path)))
        except (IOError, OSError) as error:
            self.app.flash_hint(tr("导出失败：%s", "Export failed: %s") % (error,))
        finally:
            dialog.destroy()

    # ---- 交互 ----
    def _on_header_press(self, _widget, event):
        if event.button != 1:
            return False
        x, y = self.win.get_position()
        self._drag = {"x": int(event.x_root - x), "y": int(event.y_root - y)}
        return True

    def _on_header_motion(self, _widget, event):
        if getattr(self, "_drag", None) is None:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        self.win.move(int(event.x_root - self._drag["x"]),
                      int(event.y_root - self._drag["y"]))
        return True

    def _on_header_release(self, _widget, event):
        if event.button == 1:
            self._drag = None
        return False

    def _on_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_destroy(self):
        if self.app._archive_window is self:
            self.app._archive_window = None

    def show(self):
        self.refresh()
        self.win.show_all()
        self.win.present()
        return self

    def close(self):
        self.win.destroy()



# --------------------------------------------------------------------------- #
# 自检（--check）
# --------------------------------------------------------------------------- #
def run_check():
    """数据读写与界面构造自检，成功打印 SELFTEST OK 并返回 0。"""
    import tempfile

    if Gtk is None:
        sys.stderr.write("自检失败: 缺少 python3-gi / GTK3（统信 UOS / Debian: "
                         "sudo apt install python3-gi gir1.2-gtk-3.0）\n")
        return 1
    try:                            # 勾选圆是 cairo 自绘的，自检要验它的抗锯齿
        import cairo
    except ImportError:
        cairo = None
    install_css()
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
        if Store(path).ui.get("sound") is not True:
            problems.append("ui 默认没有提醒声音开关")

        reloaded.toggle(second["id"])
        if reloaded.remaining() != 1 or not reloaded.has_done():
            problems.append("切换完成状态失败")
        if Store(path).remaining() != 1:
            problems.append("完成状态未落盘")

        # ---- 备份 / 导出 / 导入 ----
        json_path = os.path.join(temp_dir, "backup.json")
        if reloaded.export_json(json_path) != len(reloaded.items):
            problems.append("导出 JSON 的条目数不对")
        if not os.path.exists(json_path):
            problems.append("导出 JSON 没有生成")
        restore_store = Store(os.path.join(temp_dir, "restore.json"))
        try:
            got_items, _got_archive = restore_store.import_json(json_path)
        except ValueError as error:
            problems.append("导入自己导出的 JSON 失败: %r" % (error,))
        else:
            if got_items != len(reloaded.items):
                problems.append("导入后条目数不一致")
            if sorted(i["id"] for i in restore_store.items) != \
                    sorted(i["id"] for i in reloaded.items):
                problems.append("导入后条目 id 不一致")
            # 导入后按状态归位（resort）是预期行为，比较内容时按 id 对齐
            if sorted((i["id"], i["text"]) for i in restore_store.items) != \
                    sorted((i["id"], i["text"]) for i in reloaded.items):
                problems.append("导入后条目内容不一致")
        csv_path = os.path.join(temp_dir, "backup.csv")
        if reloaded.export_csv(csv_path) != len(reloaded.items):
            problems.append("导出 CSV 的行数不对")
        with open(csv_path, "r", encoding="utf-8-sig") as handle:
            if len(handle.read().strip().splitlines()) != len(reloaded.items) + 1:
                problems.append("CSV 应是表头 + 每条一行")

        # ---- 数据格式版本 ----
        # 1) 没有 version 字段的旧文件：按 v1 读
        legacy_path = os.path.join(temp_dir, "legacy.json")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump({"items": [{"id": 7, "text": "老格式", "done": True}],
                       "archive": [], "ui": {}}, handle, ensure_ascii=False)
        legacy = Store(legacy_path)
        if len(legacy.items) != 1 or legacy.state_of(legacy.items[0]) != STATE_DONE:
            problems.append("没有 version 字段的旧文件没有按 v1 读出")
        # 2) 版本过新：拒绝读取、原文件移到 .newer，绝不能读成空清单再覆盖
        newer_path = os.path.join(temp_dir, "newer.json")
        with open(newer_path, "w", encoding="utf-8") as handle:
            json.dump({"version": DATA_VERSION + 1,
                       "items": [{"id": 1, "text": "未来格式"}],
                       "archive": [], "ui": {}}, handle, ensure_ascii=False)
        guard = Store(newer_path)
        if not guard.load_error:
            problems.append("过高版本的数据文件没有被拒绝读取")
        if guard.items:
            problems.append("过高版本的数据文件不应读出条目")
        if os.path.exists(newer_path):
            problems.append("过高版本的数据文件应被移走（否则会被空清单覆盖）")
        if not os.path.exists(newer_path + ".newer"):
            problems.append("过高版本的数据文件没有留下 .newer 备份")

        # ---- todo.txt 导入 ----
        parsed = parse_todotxt(
            "x 2026-01-01 2026-01-02 已完成的一条\n"
            "(A) 高优先级 @电话 due:2026-12-31\n"
            "\n"
            "普通一条\n")
        if len(parsed) != 3:
            problems.append("parse_todotxt 行数不对: %r" % (len(parsed),))
        else:
            if not parsed[0]["done"]:
                problems.append("parse_todotxt 没认出 x 完成行")
            if parsed[1]["priority"] != PRIORITY_HIGH:
                problems.append("parse_todotxt 没认出 (A) 优先级")
            if parsed[1]["due"] is None:
                problems.append("parse_todotxt 没认出 due:")
            if "@电话" not in parsed[1]["text"]:
                problems.append("parse_todotxt 不该吃掉 @context")
        todo_path = os.path.join(temp_dir, "todo.txt")
        with open(todo_path, "w", encoding="utf-8") as handle:
            handle.write("(A) 导入一条 @电话\nx 已完成的一条\n")
        merge_store = Store(os.path.join(temp_dir, "merge.json"))
        added = merge_store.import_todotxt(todo_path)
        if added != 2:
            problems.append("import_todotxt 新增条数不对: %r" % (added,))
        if not any(merge_store.state_of(i) == STATE_DONE for i in merge_store.items):
            problems.append("import_todotxt 没导入已完成条目")
        if not any(merge_store.priority_of(i) == PRIORITY_HIGH
                   for i in merge_store.items):
            problems.append("import_todotxt 没导入优先级")

        # ---- 单实例命令解析（--add 走同一条套接字）----
        if parse_ipc(b"raise\n") != ("raise", None):
            problems.append("parse_ipc 没把 raise 认出来")
        if parse_ipc("add\t买牛奶\n".encode("utf-8")) != ("add", "买牛奶"):
            problems.append("parse_ipc 没把 add 命令 / 文本认出来")
        if parse_ipc(b"")[0] != "raise":
            problems.append("空消息应按 raise 处理")

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
        marks.place_in_bucket(m1["id"])          # A 方案：改「今天」时插到对应组最前
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
        marks.place_in_bucket(m2["id"])          # 高优先级 + 今天 → 插到最前
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

        # ---- A 方案（2026-10-26）：段内手动顺序优先 ----
        bucket = Store(os.path.join(temp_dir, "bucket.json"))
        plain = bucket.add("普通")
        high = bucket.add("高优先级")
        bucket.set_priority(high["id"], PRIORITY_HIGH)
        bucket.place_in_bucket(high["id"])
        if [i["text"] for i in bucket.items] != ["高优先级", "普通"]:
            problems.append("改优先级后没有插到对应组最前: %r"
                            % ([i["text"] for i in bucket.items],))
        bucket.move(high["id"], 1)               # 手动把它拖到后面
        bucket.resort()
        if [i["text"] for i in bucket.items] != ["普通", "高优先级"]:
            problems.append("A 方案：resort 又把手动拖好的顺序按优先级弹回去了: %r"
                            % ([i["text"] for i in bucket.items],))
        bucket.toggle_today(plain["id"])
        bucket.place_in_bucket(plain["id"])
        if [i["text"] for i in bucket.items] != ["普通", "高优先级"]:
            problems.append("标「今天」后没有插到段内最前: %r"
                            % ([i["text"] for i in bucket.items],))
        if bucket.resort():
            problems.append("A 方案：已经排好的列表再 resort 不该有变化")

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
        if not format_remind_time(base + 23 * 3600, now=base).startswith(
                tr("明天", "tomorrow")):
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
        order.add("B")
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

        # ---- GTK 界面构造（窗口保持隐藏，不打扰桌面）----
        win = None
        try:
            ui_path = os.path.join(temp_dir, "ui.json")
            ui_store = Store(ui_path)
            first = ui_store.add("界面自检一")
            second = ui_store.add("界面自检二 @工作")
            third = ui_store.add("界面自检三")
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            app = TodoWindow(win, ui_store, visible=False)
            app.render(keep_scroll=False)
            if len(app.row_widgets) != 3:
                problems.append("界面没把 3 条都排出来: %r" % (len(app.row_widgets),))
            if tr("3 项未完成", "3 unfinished") not in app.count.get_text():
                problems.append("摘要没显示 3 项未完成: %r" % (app.count.get_text(),))

            # 勾选只重画该行（不重建控件）——双击改名能工作、也不卡的前提。
            # 先让「已完成」组存在，这样这次勾选只改计数、不改变分组结构。
            ui_store.toggle(second["id"])
            app.render(keep_scroll=False)
            row_before = app.rows.get(first["id"])
            app.toggle_item(first["id"])
            row_after = app.rows.get(first["id"])
            if row_after is not row_before:
                problems.append("勾选把行控件重建了（应当原地重画）")
            if ui_store.state_of(app.find_item(first["id"])) != STATE_DONE:
                problems.append("勾选没有把状态改成已完成")
            if not row_after.check.done:
                problems.append("勾选后勾选框没有变成已完成态")
            if Store(ui_path).state_of([i for i in Store(ui_path).items
                                        if i["id"] == first["id"]][0]) != STATE_DONE:
                problems.append("勾选状态没有落盘")
            app.toggle_item(first["id"])

            # 勾选圆是 cairo 自绘的（GTK 主题不管它）：抗锯齿必须真的混出
            # 过渡像素，否则就是 1-bit 硬边。v0.4.0 初版把 ANTIALIAS 写成
            # 1=NONE，圆又变毛了 —— 这里渲染一颗 18px 圆直接数颜色。
            a11y_check = app.rows.get(first["id"]).check
            if tr("完成状态", "Status") not in (a11y_check.get_accessible().get_name() or ""):
                problems.append("勾选圆没有无障碍名称: %r"
                                % (a11y_check.get_accessible().get_name(),))
            if not (app.rows.get(first["id"]).get_accessible().get_name()
                    or "").startswith(tr("待办：", "To-do: ")):
                problems.append("条目行没有无障碍名称: %r"
                                % (app.rows.get(first["id"]).get_accessible().get_name(),))
            if cairo is not None:
                ring = app.rows.get(first["id"]).check
                ring_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 18, 18)
                ring_context = cairo.Context(ring_surface)
                ring_context.set_source_rgba(0, 0, 0, 1)
                ring_context.paint()
                ring._draw(None, ring_context)
                ring_surface.flush()
                ring_data = ring_surface.get_data()
                ring_stride = ring_surface.get_stride()
                ring_tones = set()
                for y in range(18):
                    for x in range(18):
                        start = y * ring_stride + x * 4
                        ring_tones.add(bytes(ring_data[start:start + 3]))
                if len(ring_tones) < 5:
                    problems.append("勾选圆没有抗锯齿（只有 %d 种颜色）"
                                    % len(ring_tones))

            # 等待中：整行琥珀色 + 状态落盘
            app.toggle_waiting_item(second["id"])
            if ui_store.state_of(app.find_item(second["id"])) != STATE_WAITING:
                problems.append("「等待中」没有生效")
            waiting_row = app.rows.get(second["id"])
            if not waiting_row.title.get_style_context().has_class("waiting"):
                problems.append("等待中的行没有等待态样式")
            app.toggle_waiting_item(second["id"])

            # 优先级：改高之后插到本段最前
            app.set_priority_item(third["id"], PRIORITY_HIGH)
            if ui_store.priority_of(app.find_item(third["id"])) != PRIORITY_HIGH:
                problems.append("优先级没有设成高")
            if ui_store.items[0]["id"] != third["id"]:
                problems.append("高优先级没有浮到本段最前")

            # 今天：徽标 + 组内靠前
            app.toggle_today_item(third["id"])
            if not app.find_item(third["id"]).get("today"):
                problems.append("「今天要办的」没有生效")

            # 筛选 chip
            app.set_filter("工作")
            if len(app.row_widgets) != 1:
                problems.append("标签筛选后行数不对: %r" % (len(app.row_widgets),))
            app.set_filter("不存在的标签")
            if app.row_widgets:
                problems.append("筛选无匹配时不该有行")
            app.set_filter(None)
            if len(app.row_widgets) != 3:
                problems.append("切回「全部」后行数不对")

            # 行内改名
            app.begin_edit(first["id"])
            edit_row = app.rows.get(first["id"])
            if edit_row is None or edit_row._edit_entry is None:
                problems.append("双击改名没有打开输入框")
            else:
                edit_row._edit_entry.set_text("  改过的文字  ")
                app.end_edit(save=True)
                if app.find_item(first["id"])["text"] != "改过的文字":
                    problems.append("行内改名没有保存/去空白")

            # 子步骤：保存、勾选
            ui_store.set_steps(first["id"], [{"text": "步骤甲", "done": False},
                                             {"text": "步骤乙", "done": True}])
            app.render(keep_scroll=False)
            app.begin_steps_edit(first["id"])
            steps_row = app.rows.get(first["id"])
            if steps_row is None or not steps_row._steps_editor:
                problems.append("子步骤编辑器没有打开")
            else:
                entries = steps_row._steps_editor["entries"]
                entries[0]._entry.set_text("步骤甲改")
                app.end_steps_edit(save=True)
                saved = ui_store.steps_of(app.find_item(first["id"]))
                if [step["text"] for step in saved] != ["步骤甲改", "步骤乙"]:
                    problems.append("子步骤保存结果不对: %r"
                                    % ([step["text"] for step in saved],))
                if not saved[1]["done"]:
                    problems.append("子步骤的 done 没有按位置保留")
            app.toggle_step_item(first["id"], 0)
            if not ui_store.steps_of(app.find_item(first["id"]))[0]["done"]:
                problems.append("点小方框没有勾上子步骤")

            # 删除 + 撤销
            before_count = len(ui_store.items)
            app.remove_item(second["id"])
            if len(ui_store.items) != before_count - 1:
                problems.append("删除没有生效")
            if not app.undo_btn.get_visible():
                problems.append("删除后没有出现「撤销删除」")
            app.undo_delete()
            if len(ui_store.items) != before_count:
                problems.append("撤销删除没有把条目放回去")

            # 已完成组折叠
            done_item = ui_store.add("界面自检已完成")
            ui_store.toggle(done_item["id"])
            app.render(keep_scroll=False)
            shown = len(app.row_widgets)
            app.toggle_done_collapsed()
            if len(app.row_widgets) >= shown:
                problems.append("折叠已完成组后行数没变少")
            app.toggle_done_collapsed()

            # 到点提醒：横幅出现 + 清掉提醒（只响一次）+ 「知道了」收起
            remind_item = ui_store.add("界面自检提醒")
            ui_store.set_reminder(remind_item["id"], time.time() - 1)
            app._remind_tick(raise_it=False)
            if "⏰" not in app.hint.get_text():
                problems.append("到点后没有弹出提醒横幅: %r" % (app.hint.get_text(),))
            if not app.alert_btn.get_visible():
                problems.append("到点后没有显示「知道了」")
            if ui_store.due_reminders():
                problems.append("到点提醒没有被清掉，会重复响")
            app.dismiss_alert()
            if app.alert_btn.get_visible():
                problems.append("点「知道了」后横幅没有收起")

            # 折叠 / 展开
            app.toggle_collapsed()
            if app.input_row.get_visible():
                problems.append("折叠之后输入条还占着位置")
            if not app.header.get_style_context().has_class("compact"):
                problems.append("折叠之后 header 没有切到单行样式")
            if app.header_left.get_visible():
                problems.append("折叠之后两行标题还占着位置")
            app.toggle_collapsed()
            if not app.input_row.get_visible():
                problems.append("展开之后输入条没有回来")
            if app.header.get_style_context().has_class("compact"):
                problems.append("展开之后 header 还是单行样式")

            # 折叠尺寸：主检查窗口不显示、量不到字体，单独开一个离屏窗口量
            off = Gtk.OffscreenWindow()
            off_store = Store(os.path.join(temp_dir, "collapsed.json"))
            off_store.add("折叠自检一")
            off_app = TodoWindow(off, off_store, visible=False)
            off_app.render(keep_scroll=False)
            off.show_all()
            while Gtk.events_pending():
                Gtk.main_iteration()
            expanded_w = off_app.panel_w
            off_app.toggle_collapsed()
            compact_w, compact_h = off_app._collapsed_size()
            if not (120 <= compact_w < PANEL_W_MIN):
                problems.append("折叠态宽度没有收成内容宽: %r" % (compact_w,))
            if not (24 <= compact_h <= 44):
                problems.append("折叠态高度不对（应在 24~44）: %r" % (compact_h,))
            if off_app._geometry_pending != (compact_w, compact_h):
                problems.append("折叠尺寸没落到 resize 目标上: %r"
                                % (off_app._geometry_pending,))
            if tr("项未完成", "unfinished") not in off_app.compact_count.get_text():
                problems.append("折叠态计数没有同步: %r"
                                % off_app.compact_count.get_text())
            off_app.toggle_collapsed()
            if off_app._geometry_pending[0] != expanded_w:
                problems.append("展开没有恢复原来的宽度: %r/%r"
                                % (off_app._geometry_pending[0], expanded_w))
            off.destroy()

            # 归档浏览器：恢复一条
            app.clear_done()
            app.render(keep_scroll=False)
            before_items = len(ui_store.items)
            archive = ArchiveWindow(app)
            if not ui_store.archive:
                problems.append("清空已完成没有进归档")
            else:
                archive._restore(ui_store.archive[-1])
                if len(ui_store.items) != before_items + 1:
                    problems.append("从归档恢复后条目数不对")
            archive.win.destroy()

            # 底部提示优先级
            app.set_filter("工作")
            if tr("只看", "Only") not in app.hint.get_text():
                problems.append("筛选状态下底部提示不对: %r" % (app.hint.get_text(),))
            app.set_filter(None)

            # 搜索：过滤 → 关闭恢复
            before_visible = len(app._visible_items())
            sample = app.store.items[0]["text"]
            app.toggle_search(True)
            app.search_entry.set_text(sample)
            if len(app._visible_items()) > before_visible:
                problems.append("搜索没有过滤条目")
            if not app.search_row.get_visible():
                problems.append("搜索框没有显示")
            app.toggle_search(False)
            if len(app._visible_items()) != before_visible:
                problems.append("关闭搜索后条目没有恢复")

            # 尺寸：宽度守下限 360、允许拉宽；内容列始终 ≤ 520（Clamp）
            app._apply_user_size(200, 600)
            if app.panel_w != PANEL_W_MIN:
                problems.append("窗口宽度没有守住下限 360: %r" % (app.panel_w,))
            app._apply_user_size(900, 600)
            if app.panel_w != 900:
                problems.append("窗口应当允许拉宽到 900: %r" % (app.panel_w,))
            if app.clamp.max_width != PANEL_W_MAX:
                problems.append("内容列限宽不是 520: %r" % (app.clamp.max_width,))

            app._remind_stop()
            app._cancel_regroup()
            app._hide_undo()
        except Exception as error:      # 代码里的笔误也要在这里暴露
            import traceback
            problems.append("界面自检失败: %r\n%s"
                            % (error, traceback.format_exc()))
        finally:
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
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
# 用途：把「必须盯着桌面看一眼」才能确认的事，压成一段可以复制粘贴回来的文本。
# 只读：不改数据、不写配置；只额外建一个隐藏窗口做探测。
# --------------------------------------------------------------------------- #
def session_kind():
    """判断图形会话类型，返回 (kind, detail)。

    kind: "x11" / "wayland" / "xwayland" / "headless"。
    本程序的全局快捷键、窗口置顶、唤回都走 X11（libX11 + XGrabKey），
    在 Wayland 会话下可能**静默失效** —— 这里只负责判断，好让界面和 --doctor
    把话说清楚，而不是让用户以为程序坏了。
    """
    backend = None
    if Gdk is not None:
        try:
            display = Gdk.Display.get_default()
        except Exception:
            display = None
        if display is not None:
            name = type(display).__name__.lower()
            if "wayland" in name:
                backend = "wayland"
            elif "x11" in name:
                backend = "x11"
    session = (os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if backend == "wayland":
        return "wayland", "Wayland（GTK 使用 Wayland 后端）"
    if session == "wayland":
        if backend == "x11":
            return "xwayland", "Wayland 会话 + XWayland（GTK 走 X11）"
        return "wayland", "Wayland（XDG_SESSION_TYPE=wayland）"
    if backend == "x11" or session == "x11":
        return "x11", "X11"
    return "headless", "无法判断会话类型（没有可用的图形显示）"


def _x11_load():
    try:
        return ctypes.CDLL("libX11.so.6")
    except (ImportError, OSError):
        return None


def run_doctor():
    rows = []
    caps = []
    if gtk_ready():
        install_css()

    def row(name, verdict, detail=""):
        rows.append("%-4s %-24s %s" % (verdict, name, detail))
        caps.append((name, verdict, detail))

    row(tr("版本", "version"), "OK", tr("desktop-todo %s（GTK3 版）", "desktop-todo %s (GTK3)") % APP_VERSION)
    row("Python", "OK", sys.version.split()[0])

    if Gtk is None:
        row("GTK", "FAIL", "缺少 python3-gi / GTK3（sudo apt install python3-gi "
                           "gir1.2-gtk-3.0）")
    else:
        row("PyGObject", "OK", getattr(gi, "__version__", "?"))
        row("GTK", "OK", "%d.%d.%d" % (Gtk.get_major_version(),
                                       Gtk.get_minor_version(),
                                       Gtk.get_micro_version()))
        display = Gdk.Display.get_default()
        row(tr("GDK 显示", "GDK display"), "OK" if display else "FAIL",
            display.get_name() if display else tr("连不上 X 显示", "cannot connect to the X display"))
        kind, detail = session_kind()
        if kind == "x11":
            row(tr("会话类型", "session type"), "OK", detail)
        elif kind == "headless":
            row(tr("会话类型", "session type"), "WARN", detail)
        else:
            row(tr("会话类型", "session type"), "WARN",
                tr("%s：全局快捷键 / 置顶 / 唤回依赖 X11，可能不可用", "%s: global hotkey / always-on-top / recall depend on X11 and may not work") % detail)
        screen = Gdk.Screen.get_default()
        row(tr("合成器", "compositor"), "OK" if screen and screen.is_composited() else "WARN",
            tr("已开启（窗口圆角/阴影由 KWin 合成）", "on (window corners/shadows composited)") if screen and screen.is_composited()
            else tr("未开启：圆角不会生效，窗口会显示成直角", "off: corners will not be rounded"))
        row("RGBA visual", "OK" if screen and screen.get_rgba_visual() else "WARN",
            tr("可用", "available") if screen and screen.get_rgba_visual() else tr("不可用", "unavailable"))
        settings = Gtk.Settings.get_default()
        if settings is not None:
            row(tr("GTK 主题", "GTK theme"), "OK", str(settings.get_property("gtk-theme-name")))
            row(tr("深色偏好", "dark preference"), "OK",
                tr("深色", "dark") if settings.get_property("gtk-application-prefer-dark-theme")
                else tr("浅色", "light"))
            row(tr("系统字体", "system font"), "OK", str(settings.get_property("gtk-font-name")))
        else:
            row(tr("GTK 主题", "GTK theme"), "WARN", tr("取不到 GtkSettings", "cannot read GtkSettings"))

    # ---- 窗口能力：真的建一个隐藏窗口，写进去再读回来 ----
    if Gtk is not None and gtk_ready():
        win = None
        try:
            win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
            win.set_decorated(False)
            win.set_skip_taskbar_hint(True)
            win.set_skip_pager_hint(True)
            win.set_keep_above(True)
            set_window_opacity(win, 0.9)
            win.show_all()
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            row(tr("去标题栏", "undecorated"), "OK" if not win.get_decorated() else "FAIL",
                "set_decorated(False)")
            row(tr("不进任务栏", "skip taskbar"), "OK" if win.get_skip_taskbar_hint() else "FAIL",
                "skip-taskbar=%s skip-pager=%s"
                % (win.get_skip_taskbar_hint(), win.get_skip_pager_hint()))
            row(tr("置顶能力", "always-on-top"), "OK", tr("set_keep_above(True) 已调用", "set_keep_above(True) called"))
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                opacity = win.get_opacity()
            row(tr("透明度", "opacity"), "OK" if abs(opacity - 0.9) < 0.01 else "WARN",
                "set_opacity=%0.2f" % opacity)
            xid = win.get_window().get_xid() if win.get_window() else 0
            row(tr("X11 窗口 id", "X11 window id"), "OK" if xid else "FAIL", "0x%x" % (xid or 0))
            if xid:
                shim = GtkRootShim(win)

                reached = _on_wm_window(shim, lambda *_args: None)
                row("X11 / EWMH", "OK" if reached else "WARN",
                    tr("XQueryTree 能找到 WM 窗口", "XQueryTree found the WM window") if reached
                    else tr("找不到 WM 窗口（窗口还没建出来？）", "WM window not found (window not mapped yet?)"))
        except Exception as error:
            row(tr("窗口能力", "window capability"), "FAIL", "%r" % (error,))
        finally:
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
    else:
        row(tr("窗口能力", "window capability"), "N/A", tr("缺少 GTK / 显示，跳过", "no GTK / display, skipped"))

    if _x11_load() is None:
        row("libX11", "FAIL", tr("libX11.so.6 载入失败（全局快捷键/唤回不可用）", "libX11.so.6 failed to load (hotkey/recall unavailable)"))
    else:
        row("libX11", "OK", "libX11.so.6")

    hotkey_ok = parse_hotkey(HOTKEY_DEFAULT) is not None
    row(tr("快捷键解析", "hotkey parsing"), "OK" if hotkey_ok else "FAIL", HOTKEY_DEFAULT)

    path = data_file()
    row(tr("数据文件", "data file"), "OK" if os.path.exists(path) else "WARN",
        "%s%s" % (path, "" if os.path.exists(path) else tr("（还没有，首次保存时创建）", " (not yet; created on first save)")))
    directory = os.path.dirname(path)
    writable = os.path.isdir(directory) or os.path.isdir(os.path.dirname(directory))
    row(tr("数据目录可写", "data directory writable"), "OK" if writable else "FAIL", directory)
    try:
        store = Store(path)
        row(tr("条目 / 归档", "items / archive"), "OK", tr("%d 条待办 / %d 条归档", "%d to-dos / %d archived")
            % (len(store.items), len(store.archive)))
        row(tr("数据格式", "data format"), "OK", "version %d" % DATA_VERSION)
        if store.load_error:
            row(tr("数据版本检查", "data version check"), "WARN", store.load_error)
        newer_path = path + ".newer"
        if os.path.exists(newer_path):
            row(tr("更新版本备份", "newer-version backup"), "WARN",
                tr("发现 %s（更新版本写的数据，已保留未覆盖）", "found %s (data written by a newer version, kept untouched)") % newer_path)
    except Exception as error:
        row(tr("条目 / 归档", "items / archive"), "FAIL", "%r" % (error,))

    row(tr("单实例套接字", "single-instance socket"), "OK", ipc_socket_path())
    row(tr("唤回日志", "recall log"), "OK", os.path.join(os.path.dirname(path), "hotkey.log"))
    crash_path = os.path.join(directory, "crash.log")
    if os.path.exists(crash_path):
        row(tr("崩溃日志", "crash log"), "WARN", tr("存在 %s（反馈时附上）", "found %s (attach it when reporting)") % crash_path)
    row(tr("主题开关", "theme switch"), "OK", tr("DESKTOP_TODO_THEME=%s（默认 dark；light 跟随浅色）", "DESKTOP_TODO_THEME=%s (default dark; light follows light)")
        % ("light" if _THEME_NAME == "light" else "dark"))
    if display is not None:
        try:
            count = display.get_n_monitors()
            parts = []
            for index in range(min(count, 4)):
                geometry = display.get_monitor(index).get_geometry()
                parts.append("%dx%d@%d,%d" % (geometry.width, geometry.height,
                                              geometry.x, geometry.y))
            row(tr("显示器", "monitors"), "OK", tr("%d 个：%s", "%d: %s") % (count, " / ".join(parts)))
        except Exception:
            pass
    row(tr("界面缩放", "UI scaling"), "OK", tr("由系统 DPI / GTK 缩放决定（不再自己算像素）", "decided by system DPI / GTK scaling"))

    print("=" * 68)
    print(tr("desktop-todo %s 诊断报告（--doctor）", "desktop-todo %s diagnostics (--doctor)") % APP_VERSION)
    print("=" * 68)
    for line in rows:
        print(line)
    print("-" * 68)
    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "N/A": 0}
    for _name, verdict, _detail in caps:
        counts[verdict] = counts.get(verdict, 0) + 1
    print(tr("结论：OK %d / WARN %d / FAIL %d / N/A %d", "summary: OK %d / WARN %d / FAIL %d / N/A %d")
          % (counts["OK"], counts["WARN"], counts["FAIL"], counts["N/A"]))
    print("DOCTOR DONE")
    return 0



# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def _write_crash_log(error):
    """把未捕获异常写到本地 crash.log；返回路径（失败返回 None）。"""
    try:
        directory = data_dir()
        if not os.path.isdir(directory):
            os.makedirs(directory)
        path = os.path.join(directory, "crash.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("=" * 64 + "\n")
            handle.write("%s  desktop-todo %s  session=%s  python=%s\n"
                         % (time.strftime("%Y-%m-%d %H:%M:%S"), APP_VERSION,
                            session_kind()[0], sys.version.split()[0]))
            handle.write("".join(traceback.format_exception(
                type(error), error, error.__traceback__)))
        return path
    except Exception:
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=tr("桌面待办清单挂件", "Desktop to-do widget"))
    parser.add_argument("--version", action="version",
                        version="desktop-todo %s" % APP_VERSION)
    parser.add_argument("--check", action="store_true",
                        help=tr("自检数据读写与界面构造后退出",
                              "Run self-check (data + UI) and exit"))
    parser.add_argument("--doctor", action="store_true",
                        help=tr("打印环境/窗口能力诊断报告后退出",
                              "Print environment/window diagnostics and exit"))
    parser.add_argument("--install-autostart", action="store_true",
                        help=tr("写入 ~/.config/autostart 开机自启",
                              "Install ~/.config/autostart entry"))
    parser.add_argument("--uninstall-autostart", action="store_true",
                        help=tr("移除开机自启", "Remove the autostart entry"))
    parser.add_argument("--data-file", action="store_true",
                        help=tr("打印数据文件路径后退出", "Print the data file path and exit"))
    parser.add_argument("--import-todotxt", dest="import_todotxt", metavar=tr("文件", "FILE"),
                        help=tr("把 todo.txt 文件合并进当前数据后退出",
                                "Merge a todo.txt file into the current data and exit"))
    parser.add_argument("--add", metavar=tr("文本", "TEXT"),
                        help=tr("把一条待办发给正在运行的实例（没有实例则直接写数据文件）后退出",
                              "Send a to-do to the running instance (or write it directly)"))
    parser.add_argument("--export", metavar=tr("文件", "FILE"),
                        help=tr("导出数据后退出（.json 为完整备份；.csv 供表格查看）",
                              "Export data and exit (.json = full backup; .csv = spreadsheet)"))
    parser.add_argument("--import", dest="import_path", metavar=tr("文件", "FILE"),
                        help=tr("从 JSON 备份恢复（会先备份当前数据）后退出",
                              "Restore from a JSON backup (current data is backed up first)"))
    parser.add_argument("--hotkey", metavar=tr("键位", "KEY"),
                        help=tr("设置全局唤回快捷键后退出（如 Super+T、Ctrl+Alt+K）",
                              "Set the global recall hotkey (e.g. Super+T, Ctrl+Alt+K)"))
    args = parser.parse_args(argv)

    if args.install_autostart:
        return autostart_install()
    if args.uninstall_autostart:
        return autostart_uninstall()
    if args.data_file:
        print(data_file())
        return 0
    if args.add is not None:
        text = args.add.strip()
        if not text:
            sys.stderr.write("--add 需要非空文本\n")
            return 2
        if raise_running_instance(("add\t%s\n" % text).encode("utf-8")):
            print("已添加到正在运行的桌面待办：%s" % text)
            return 0
        store = Store(data_file())
        item = store.add(text)
        if item is None:
            sys.stderr.write("添加失败（文本为空？）\n")
            return 1
        print("没有运行中的实例，已直接写入数据文件：%s（下次启动可见）" % text)
        return 0
    if args.export:
        store = Store(data_file())
        out = os.path.abspath(os.path.expanduser(args.export))
        try:
            if out.lower().endswith(".csv"):
                count = store.export_csv(out)
                print("已导出 %d 条待办到 %s（CSV，供表格查看，不能用于恢复）"
                      % (count, out))
            else:
                count = store.export_json(out)
                print("已导出 %d 条待办、%d 条归档到 %s"
                      % (count, len(store.archive), out))
        except (IOError, OSError) as error:
            sys.stderr.write("导出失败：%s\n" % (error,))
            return 1
        return 0
    if args.import_path:
        store = Store(data_file())
        source = os.path.abspath(os.path.expanduser(args.import_path))
        backup = store.backup_copy("import-backup")
        try:
            count, archived = store.import_json(source)
        except (IOError, OSError, ValueError) as error:
            sys.stderr.write("导入失败：%s\n" % (error,))
            if backup:
                sys.stderr.write("当前数据没有改动（已备份到 %s）\n" % backup)
            return 1
        print("已导入 %d 条待办、%d 条归档。导入前的数据备份在：%s"
              % (count, archived, backup or "（当时还没有数据文件）"))
        return 0
    if args.import_todotxt:
        store = Store(data_file())
        source = os.path.abspath(os.path.expanduser(args.import_todotxt))
        backup = store.backup_copy("import-backup")
        try:
            added = store.import_todotxt(source)
        except (IOError, OSError, ValueError) as error:
            sys.stderr.write(tr("导入失败：%s\n", "Import failed: %s\n") % (error,))
            if backup:
                sys.stderr.write(tr("当前数据没有改动（已备份到 %s）\n",
                                    "Current data unchanged (backup: %s)\n") % backup)
            return 1
        print(tr("已从 todo.txt 合并 %d 条（导入前备份：%s）",
                 "Merged %d items from todo.txt (backup: %s)")
              % (added, backup or tr("当时还没有数据文件", "none")))
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

    if not gtk_ready():
        sys.stderr.write("缺少 GTK3（统信 UOS / Debian: "
                         "sudo apt install python3-gi gir1.2-gtk-3.0），"
                         "或连不上图形显示。\n")
        return 1

    if args.check:
        return run_check()
    if args.doctor:
        return run_doctor()

    if raise_running_instance():
        print("桌面待办清单已经在运行，已把它叫到最前面（任务栏里没有它，这是唤回方式）。")
        return 0

    kind, detail = session_kind()
    if kind in ("wayland", "xwayland"):
        sys.stderr.write("提示：%s。全局快捷键 / 置顶 / 唤回依赖 X11，可能不可用。\n"
                         % detail)

    install_css()
    # WM_CLASS 用 APP_ID，与 .desktop 的 StartupWMClass 对上（Deepin/UOS 靠它认应用）
    win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            win.set_wmclass(APP_ID, APP_ID)   # StartupWMClass 靠它认应用
    except Exception:
        pass
    window = TodoWindow(win, Store(data_file()))
    if os.environ.get("DESKTOP_TODO_VERBOSE"):
        # 默认不输出；排障时设 DESKTOP_TODO_VERBOSE=1 就能看到快捷键挂上没有
        print("全局快捷键 = %s（%s）" % (
            format_hotkey(getattr(window, "_hotkey_spec", "")),
            "已注册" if getattr(window, "_hotkey_on", False) else "没注册上"),
            flush=True)
        print("数据文件 = %s" % data_file(), flush=True)
    Gtk.main()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:      # 未捕获异常留现场：crash.log 里没有待办正文
        path = _write_crash_log(error)
        sys.stderr.write("崩溃：%r\n" % (error,))
        if path:
            sys.stderr.write("现场已写入 %s（反馈时附上即可）\n" % path)
        raise
