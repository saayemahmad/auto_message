"""
Auto Messenger (native Windows GUI) - automatic typing / pasting message sender.

Install:   pip install wxPython pyperclip
Run:       python auto_messenger_native.py
Build EXE: pip install pyinstaller
           pyinstaller --noconsole --onefile --name AutoMessenger auto_messenger_native.py

Global hotkeys (work even when this window is not focused):
    F8 = Pause / Resume        F9 = Stop
Failsafe: move the mouse to the top-left corner of the screen to stop instantly.
"""
import sys
import time
import random
import struct
import threading

# ----------------------------------------------------------------------------
# Pure helpers (numbering + message building)
# ----------------------------------------------------------------------------
ROMAN = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
         (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]


def to_alpha(n):
    """1->a ... 26->z, 27->aa, 28->ab, 29->ac ... (spreadsheet style)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def to_roman(n):
    if n <= 0 or n > 3999:
        return str(n)
    out = ""
    for value, sym in ROMAN:
        while n >= value:
            out += sym
            n -= value
    return out


def format_number(n, style, upper):
    """style: 0 none, 1 digits, 2 letters, 3 roman."""
    if style == 1:
        s = str(n)
    elif style == 2:
        s = to_alpha(n)
    elif style == 3:
        s = to_roman(n)
    else:
        return ""
    return s.upper() if upper else s


def build_message(idx, c, rng=random):
    """idx is the 0-based index of the message being sent."""
    lines = c["lines"]
    if c["order"] == 0:            # go through lines in order (loop)
        msg = lines[idx % len(lines)]
    elif c["order"] == 1:          # random line
        msg = rng.choice(lines)
    else:                          # repeat first line
        msg = lines[0]
    if c["num_style"] == 0:
        return msg
    num = format_number(c["start"] + idx, c["num_style"], c["upper"])
    return f"{num}{c['suffix']} {msg}"


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
# ---- END HELPERS ----

if sys.platform != "win32":
    print("This app is for Windows only.")
    sys.exit(1)

import ctypes
from ctypes import wintypes
import pyperclip
import wx
from wx.lib.mixins.listctrl import ListCtrlAutoWidthMixin

# ----------------------------------------------------------------------------
# Windows keyboard input (SendInput) - supports every Unicode character
# ----------------------------------------------------------------------------
user32 = ctypes.windll.user32
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
VK_RETURN, VK_CONTROL, VK_V, VK_F8, VK_F9 = 0x0D, 0x11, 0x56, 0x77, 0x78


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _key(vk=0, scan=0, flags=0):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def _send(items):
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


def type_unit(code):
    _send([_key(0, code, KEYEVENTF_UNICODE), _key(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])


def tap_vk(vk):
    _send([_key(vk), _key(vk, 0, KEYEVENTF_KEYUP)])


def ctrl_v():
    _send([_key(VK_CONTROL), _key(VK_V), _key(VK_V, 0, KEYEVENTF_KEYUP), _key(VK_CONTROL, 0, KEYEVENTF_KEYUP)])


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def mouse_in_corner():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x <= 0 and p.y <= 0


# ----------------------------------------------------------------------------
# Worker thread
# ----------------------------------------------------------------------------
class _Stop(Exception):
    pass


class Worker(threading.Thread):
    def __init__(self, cfg, frame):
        super().__init__(daemon=True)
        self.c, self.f = cfg, frame
        self.stop = False
        self.paused = False

    def request_stop(self):
        self.stop = True

    def toggle_pause(self):
        self.paused = not self.paused
        wx.CallAfter(self.f.on_status, "Paused" if self.paused else "Running")

    def _check(self):
        while self.paused and not self.stop:
            time.sleep(0.05)
        if self.stop:
            raise _Stop("Stopped by user")
        if mouse_in_corner():
            raise _Stop("Failsafe (mouse in corner)")

    def _wait(self, secs):
        remaining, last = secs, time.perf_counter()
        while remaining > 0:
            self._check()
            time.sleep(min(0.02, remaining))
            now = time.perf_counter()
            if not self.paused:
                remaining -= now - last
            last = now

    def _type(self, text):
        data = text.encode("utf-16-le")
        units = struct.unpack(f"<{len(data) // 2}H", data)
        per = self.c["type_secs"] / max(1, len(units))
        for u in units:
            self._check()
            type_unit(u)
            d = per * random.uniform(0.5, 1.5) if self.c["humanize"] else per
            if d > 0:
                self._wait(d)

    def run(self):
        c, reason, saved_clip = self.c, "Finished", None
        try:
            if c["mode"] == "paste":
                try:
                    saved_clip = pyperclip.paste()
                except Exception:
                    pass
            for s in range(c["countdown"], 0, -1):
                wx.CallAfter(self.f.on_status, f"Starting in {s}... click your target box!")
                self._wait(1)
            wx.CallAfter(self.f.on_status, "Running")
            total, i = c["count"], 0
            while total == 0 or i < total:
                msg = build_message(i, c)
                if c["mode"] == "type":
                    self._type(msg)
                    self._wait(c["after_type"])
                else:
                    pyperclip.copy(msg)
                    self._wait(0.05)
                    ctrl_v()
                    self._wait(c["paste_delay"])
                if c["enter"]:
                    tap_vk(VK_RETURN)
                i += 1
                wx.CallAfter(self.f.on_progress, i, total, msg)
                if total == 0 or i < total:
                    self._wait(c["gap"] + random.uniform(0, c["gap_rand"]))
        except _Stop as e:
            reason = str(e)
        except Exception as e:  # noqa
            reason = f"Error: {e}"
        finally:
            if saved_clip is not None:
                try:
                    pyperclip.copy(saved_clip)
                except Exception:
                    pass
        wx.CallAfter(self.f.on_done, reason)


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class AutoList(wx.ListCtrl, ListCtrlAutoWidthMixin):
    def __init__(self, parent):
        wx.ListCtrl.__init__(self, parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        ListCtrlAutoWidthMixin.__init__(self)


GRAY = wx.Colour(105, 105, 105)
NUM_STYLES = ["None  (I Love You)", "Numbers  (1, 2, 3)", "Letters  (a..z, aa, ab, ac)", "Roman  (i, ii, iii)"]
ORDERS = ["In order (loop)", "Random line", "First line only"]


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Auto Messenger", size=(960, 880))
        self.worker = None
        self.t0 = 0.0
        self._closing = False
        self._f8 = self._f9 = False
        self._last_tick = 0.0
        self.config = wx.Config("AutoMessenger")

        self._build_menu()
        self._build_toolbar_and_body()
        self._build_statusbar()
        self._setup_fields()
        self._load()
        self._on_change()
        self._set_running(False)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_SIZE, self.on_size)
        for ev in (wx.EVT_TEXT, wx.EVT_CHOICE, wx.EVT_SPINCTRL, wx.EVT_SPINCTRLDOUBLE,
                   wx.EVT_CHECKBOX, wx.EVT_RADIOBUTTON):
            self.Bind(ev, self._on_change)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll, self.timer)
        self.timer.Start(40)
        self._fit_window()

    # ---------------- menu ----------------
    def _build_menu(self):
        mb = wx.MenuBar()
        m_file, m_tools, m_help = wx.Menu(), wx.Menu(), wx.Menu()
        self.mi_start = m_file.Append(wx.ID_ANY, "&Start sending")
        self.mi_pause = m_file.Append(wx.ID_ANY, "&Pause / Resume   (F8)")
        self.mi_stop = m_file.Append(wx.ID_ANY, "S&top   (F9)")
        m_file.AppendSeparator()
        mi_exit = m_file.Append(wx.ID_EXIT, "E&xit")
        mi_load = m_tools.Append(wx.ID_ANY, "&Load messages from file...")
        mi_save = m_tools.Append(wx.ID_ANY, "&Save messages to file...")
        m_tools.AppendSeparator()
        mi_clear = m_tools.Append(wx.ID_ANY, "&Clear sent list")
        mi_reset = m_tools.Append(wx.ID_ANY, "&Reset all settings")
        mi_help = m_help.Append(wx.ID_ANY, "&Hotkeys && tips")
        mi_about = m_help.Append(wx.ID_ABOUT, "&About")
        mb.Append(m_file, "&File")
        mb.Append(m_tools, "&Tools")
        mb.Append(m_help, "&Help")
        self.SetMenuBar(mb)
        self.Bind(wx.EVT_MENU, lambda e: self.start(), self.mi_start)
        self.Bind(wx.EVT_MENU, lambda e: self.pause(), self.mi_pause)
        self.Bind(wx.EVT_MENU, lambda e: self.stop(), self.mi_stop)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), mi_exit)
        self.Bind(wx.EVT_MENU, self.load_file, mi_load)
        self.Bind(wx.EVT_MENU, self.save_file, mi_save)
        self.Bind(wx.EVT_MENU, lambda e: self.list.DeleteAllItems(), mi_clear)
        self.Bind(wx.EVT_MENU, self.reset, mi_reset)
        self.Bind(wx.EVT_MENU, self.show_help, mi_help)
        self.Bind(wx.EVT_MENU, lambda e: wx.MessageBox(
            "Auto Messenger\nTypes or pastes messages automatically into any window.",
            "About", wx.OK | wx.ICON_INFORMATION, self), mi_about)

    # ---------------- layout ----------------
    def _group(self, title, orient=wx.VERTICAL):
        box = wx.StaticBoxSizer(orient, self.body, title)
        return box, box.GetStaticBox()

    @staticmethod
    def _grid():
        g = wx.FlexGridSizer(2, 6, 10)
        g.AddGrowableCol(1)
        return g

    @staticmethod
    def _row(g, parent, label, ctrl, expand=False):
        g.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        g.Add(ctrl, 1 if expand else 0, wx.EXPAND if expand else 0)

    def _build_toolbar_and_body(self):
        # --- toolbar (fixed on top, like the reference screenshot)
        tb = wx.Panel(self)
        tbs = wx.BoxSizer(wx.HORIZONTAL)
        self.b_start = wx.Button(tb, label="Start Sending")
        self.b_pause = wx.Button(tb, label="Pause (F8)")
        self.b_stop = wx.Button(tb, label="Stop (F9)")
        self.b_clear = wx.Button(tb, label="Clear List")
        for b in (self.b_start, self.b_pause, self.b_stop):
            tbs.Add(b, 0, wx.ALL, 4)
        tbs.Add(wx.StaticLine(tb, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.ALL, 6)
        tbs.Add(self.b_clear, 0, wx.ALL, 4)
        tb.SetSizer(tbs)
        self.b_start.Bind(wx.EVT_BUTTON, lambda e: self.start())
        self.b_pause.Bind(wx.EVT_BUTTON, lambda e: self.pause())
        self.b_stop.Bind(wx.EVT_BUTTON, lambda e: self.stop())
        self.b_clear.Bind(wx.EVT_BUTTON, lambda e: self.list.DeleteAllItems())

        # --- body: everything visible at once, three columns, no scrolling
        self.body = wx.Panel(self)

        # Messages
        g_msg, gp = self._group("Messages (one per line)")
        self.text = wx.TextCtrl(gp, style=wx.TE_MULTILINE, size=(-1, 90))
        self.order = wx.Choice(gp, choices=ORDERS)
        self.count = wx.SpinCtrl(gp, min=1, max=1000000, initial=10, size=(80, -1))
        self.endless = wx.CheckBox(gp, label="Endless (F9 stops)")
        g_msg.Add(self.text, 1, wx.EXPAND | wx.ALL, 4)
        g = self._grid()
        self._row(g, gp, "Line order:", self.order, True)
        cnt = wx.BoxSizer(wx.HORIZONTAL)
        cnt.Add(self.count, 0, wx.ALIGN_CENTER_VERTICAL)
        cnt.Add(self.endless, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        self._row(g, gp, "How many:", cnt, True)
        g_msg.Add(g, 0, wx.EXPAND | wx.ALL, 4)

        # Numbering
        g_num, np_ = self._group("Numbering")
        self.num_style = wx.Choice(np_, choices=NUM_STYLES)
        self.suffix = wx.ComboBox(np_, choices=[".", ")", ":", " -", ""], size=(80, -1))
        self.start_at = wx.SpinCtrl(np_, min=1, max=100000, initial=1, size=(80, -1))
        self.upper = wx.CheckBox(np_, label="UPPERCASE letters / numerals")
        g = self._grid()
        self._row(g, np_, "Style:", self.num_style, True)
        self._row(g, np_, "After number:", self.suffix)
        self._row(g, np_, "Start from:", self.start_at)
        g_num.Add(g, 0, wx.EXPAND | wx.ALL, 4)
        g_num.Add(self.upper, 0, wx.LEFT | wx.BOTTOM, 6)

        # Sending method
        g_snd, sp = self._group("How to send")
        self.rb_type = wx.RadioButton(sp, label="Type it (keystrokes)", style=wx.RB_GROUP)
        self.rb_paste = wx.RadioButton(sp, label="Copy and paste it (Ctrl+V)")
        self.type_secs = wx.SpinCtrlDouble(sp, min=0, max=600, initial=3, inc=0.5, size=(80, -1))
        self.after_type = wx.SpinCtrlDouble(sp, min=0, max=60, initial=0.3, inc=0.1, size=(80, -1))
        self.humanize = wx.CheckBox(sp, label="Human-like random speed")
        self.paste_delay = wx.SpinCtrlDouble(sp, min=0, max=600, initial=2, inc=0.5, size=(80, -1))
        for sc in (self.type_secs, self.after_type, self.paste_delay):
            sc.SetDigits(1)
        g_snd.Add(self.rb_type, 0, wx.ALL, 4)
        g = self._grid()
        self._row(g, sp, "Typing time (sec):", self.type_secs)
        self._row(g, sp, "Pause after (sec):", self.after_type)
        g_snd.Add(g, 0, wx.EXPAND | wx.LEFT, 20)
        g_snd.Add(self.humanize, 0, wx.LEFT | wx.TOP, 24)
        g_snd.Add(self.rb_paste, 0, wx.ALL, 4)
        g = self._grid()
        self._row(g, sp, "Wait, then Enter (sec):", self.paste_delay)
        g_snd.Add(g, 0, wx.EXPAND | wx.LEFT | wx.BOTTOM, 20)

        # Timing
        g_tim, tp = self._group("Timing")
        self.countdown = wx.SpinCtrl(tp, min=0, max=120, initial=5, size=(80, -1))
        self.gap = wx.SpinCtrlDouble(tp, min=0, max=3600, initial=1, inc=0.5, size=(80, -1))
        self.gap_rand = wx.SpinCtrlDouble(tp, min=0, max=3600, initial=0, inc=0.5, size=(80, -1))
        self.gap.SetDigits(2)
        self.gap_rand.SetDigits(2)
        self.enter = wx.CheckBox(tp, label="Press Enter after each message")
        self.minimize = wx.CheckBox(tp, label="Minimize window while running")
        g = self._grid()
        self._row(g, tp, "Start countdown (sec):", self.countdown)
        self._row(g, tp, "Gap between (sec):", self.gap)
        self._row(g, tp, "+ random extra (sec):", self.gap_rand)
        g_tim.Add(g, 0, wx.EXPAND | wx.ALL, 4)
        g_tim.Add(self.enter, 0, wx.LEFT | wx.TOP, 6)
        g_tim.Add(self.minimize, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 6)

        # Preview
        g_prev, pp = self._group("Preview of the first messages")
        self.preview = wx.TextCtrl(pp, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 88))
        g_prev.Add(self.preview, 1, wx.EXPAND | wx.ALL, 4)

        # Sent list
        g_list, lp = self._group("Sent messages")
        self.list = AutoList(lp)
        self.list.InsertColumn(0, "#", width=45)
        self.list.InsertColumn(1, "Time", width=70)
        self.list.InsertColumn(2, "Message", width=200)
        self.list.SetMinSize((-1, 110))
        g_list.Add(self.list, 1, wx.EXPAND | wx.ALL, 4)

        # Current run details
        g_run, rp = self._group("Current run")
        grid = wx.FlexGridSizer(2, 3, 12)
        grid.AddGrowableCol(1)
        self.v = {}
        for key, label in (("status", "Status:"), ("last", "Last message:"), ("sent", "Sent:"),
                           ("elapsed", "Elapsed:"), ("eta", "Time left:"), ("mode", "Method:")):
            grid.Add(wx.StaticText(rp, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            val = wx.StaticText(rp, label="\u2014", style=wx.ST_ELLIPSIZE_END)
            self.v[key] = val
            grid.Add(val, 1, wx.EXPAND)
        g_run.Add(grid, 1, wx.EXPAND | wx.ALL, 6)

        # three balanced columns
        c1, c2, c3 = wx.BoxSizer(wx.VERTICAL), wx.BoxSizer(wx.VERTICAL), wx.BoxSizer(wx.VERTICAL)
        c1.Add(g_msg, 1, wx.EXPAND | wx.BOTTOM, 6)
        c1.Add(g_num, 0, wx.EXPAND)
        c2.Add(g_snd, 0, wx.EXPAND | wx.BOTTOM, 6)
        c2.Add(g_tim, 0, wx.EXPAND | wx.BOTTOM, 6)
        c2.Add(g_prev, 1, wx.EXPAND)
        c3.Add(g_list, 1, wx.EXPAND | wx.BOTTOM, 6)
        c3.Add(g_run, 0, wx.EXPAND)
        cols = wx.BoxSizer(wx.HORIZONTAL)
        cols.Add(c1, 1, wx.EXPAND | wx.RIGHT, 5)
        cols.Add(c2, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        cols.Add(c3, 1, wx.EXPAND | wx.LEFT, 5)
        bs = wx.BoxSizer(wx.VERTICAL)
        bs.Add(cols, 1, wx.EXPAND | wx.ALL, 8)
        self.body.SetSizer(bs)

        fs = wx.BoxSizer(wx.VERTICAL)
        fs.Add(tb, 0, wx.EXPAND)
        fs.Add(wx.StaticLine(self), 0, wx.EXPAND)
        fs.Add(self.body, 1, wx.EXPAND)
        self.SetSizer(fs)

    def _build_statusbar(self):
        self.sb = self.CreateStatusBar(2)
        self.sb.SetStatusWidths([-1, 230])
        self.sb.SetStatusText("Ready", 0)
        self.gauge = wx.Gauge(self.sb, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        wx.CallAfter(self._place_gauge)

    def _place_gauge(self):
        if self._closing:
            return
        r = self.sb.GetFieldRect(1)
        self.gauge.SetPosition((r.x + 2, r.y + 2))
        self.gauge.SetSize((max(10, r.width - 6), max(8, r.height - 4)))

    def on_size(self, e):
        wx.CallAfter(self._place_gauge)
        e.Skip()

    def _fit_window(self):
        """Size the window so every control is visible at once (no scrolling)."""
        self.GetSizer().SetSizeHints(self)
        self.Fit()
        w, h = self.GetSize()
        area = wx.GetClientDisplayRect()
        w, h = min(max(w, 1000), area.width), min(max(h, 640), area.height)
        self.SetSize((w, h))
        self.Centre()

    # ---------------- state ----------------
    def _on_change(self, e=None):
        if e is not None:
            e.Skip()
        paste = self.rb_paste.GetValue()
        self.type_secs.Enable(not paste)
        self.after_type.Enable(not paste)
        self.humanize.Enable(not paste)
        self.paste_delay.Enable(paste)
        self.count.Enable(not self.endless.GetValue())
        self.start_at.Enable(self.num_style.GetSelection() != 0)
        self.suffix.Enable(self.num_style.GetSelection() != 0)
        self.upper.Enable(self.num_style.GetSelection() in (2, 3))
        self._preview()

    def cfg(self):
        lines = [l.strip() for l in self.text.GetValue().splitlines() if l.strip()]
        return dict(
            lines=lines, order=self.order.GetSelection(),
            count=0 if self.endless.GetValue() else self.count.GetValue(),
            num_style=self.num_style.GetSelection(), upper=self.upper.GetValue(),
            start=self.start_at.GetValue(), suffix=self.suffix.GetValue(),
            mode="paste" if self.rb_paste.GetValue() else "type",
            type_secs=self.type_secs.GetValue(), humanize=self.humanize.GetValue(),
            after_type=self.after_type.GetValue(), paste_delay=self.paste_delay.GetValue(),
            enter=self.enter.GetValue(), countdown=self.countdown.GetValue(),
            gap=self.gap.GetValue(), gap_rand=self.gap_rand.GetValue(), minimize=self.minimize.GetValue())

    def _preview(self):
        c = self.cfg()
        if not c["lines"]:
            self.preview.ChangeValue("(type at least one message above)")
            return
        c2 = dict(c, order=0 if c["order"] == 1 else c["order"])
        txt = "\n".join(build_message(i, c2) for i in range(6))
        if c["order"] == 1:
            txt += "\n(lines will be picked randomly when sending)"
        self.preview.ChangeValue(txt)

    def _set_running(self, running):
        self.b_start.Enable(not running)
        self.b_pause.Enable(running)
        self.b_stop.Enable(running)
        self.mi_start.Enable(not running)
        self.mi_pause.Enable(running)
        self.mi_stop.Enable(running)

    # ---------------- run control ----------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        c = self.cfg()
        if not c["lines"]:
            wx.MessageBox("Please type at least one message.", "No message", wx.OK | wx.ICON_WARNING, self)
            return
        self._save()
        self.t0 = time.perf_counter()
        self.gauge.SetValue(0)
        if c["count"]:
            self.gauge.SetRange(c["count"])
        for k in ("last", "sent", "elapsed", "eta"):
            self.v[k].SetLabel("\u2014")
        self.v["mode"].SetLabel("Typing" if c["mode"] == "type" else "Copy-paste")
        self._set_running(True)
        self.worker = Worker(c, self)
        if c["minimize"]:
            self.Iconize(True)
        self.worker.start()

    def pause(self):
        if self.worker and self.worker.is_alive():
            self.worker.toggle_pause()

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()

    def on_status(self, s):
        if self._closing:
            return
        self.sb.SetStatusText(s, 0)
        self.v["status"].SetLabel(s)
        self.b_pause.SetLabel("Resume (F8)" if s == "Paused" else "Pause (F8)")

    def on_progress(self, sent, total, msg):
        if self._closing:
            return
        el = time.perf_counter() - self.t0
        i = self.list.InsertItem(self.list.GetItemCount(), str(sent))
        self.list.SetItem(i, 1, time.strftime("%H:%M:%S"))
        self.list.SetItem(i, 2, msg)
        if self.list.GetItemCount() > 1000:
            self.list.DeleteItem(0)
        self.list.EnsureVisible(self.list.GetItemCount() - 1)
        self.v["last"].SetLabel(msg)
        if total:
            self.gauge.SetValue(sent)
            self.v["sent"].SetLabel(f"{sent} of {total}")
            self.v["eta"].SetLabel(fmt_time(el / sent * (total - sent)))
        else:
            self.gauge.Pulse()
            self.v["sent"].SetLabel(f"{sent}  (endless)")
            self.v["eta"].SetLabel("\u2014")

    def on_done(self, reason):
        if self._closing:
            return
        ok = reason == "Finished"
        if ok and self.gauge.GetRange():
            self.gauge.SetValue(self.gauge.GetRange())
        self._set_running(False)
        self.b_pause.SetLabel("Pause (F8)")
        self.sb.SetStatusText(reason, 0)
        self.v["status"].SetLabel(reason)
        if self.IsIconized():
            self.Iconize(False)
        self.Raise()

    def _poll(self, e):
        f8, f9 = key_down(VK_F8), key_down(VK_F9)
        running = self.worker is not None and self.worker.is_alive()
        if running:
            if f8 and not self._f8:
                self.worker.toggle_pause()
            if f9 and not self._f9:
                self.worker.request_stop()
            now = time.perf_counter()
            if now - self._last_tick > 0.5 and not self.worker.paused:
                self._last_tick = now
                self.v["elapsed"].SetLabel(fmt_time(now - self.t0))
        self._f8, self._f9 = f8, f9

    # ---------------- settings ----------------
    def _setup_fields(self):
        def set_paste(v):
            (self.rb_paste if v else self.rb_type).SetValue(True)
        self.fields = {
            "text": (self.text.GetValue, self.text.SetValue, "I Love You"),
            "order": (self.order.GetSelection, self.order.SetSelection, 0),
            "count": (self.count.GetValue, self.count.SetValue, 10),
            "endless": (self.endless.GetValue, self.endless.SetValue, False),
            "num_style": (self.num_style.GetSelection, self.num_style.SetSelection, 1),
            "suffix": (self.suffix.GetValue, self.suffix.SetValue, "."),
            "start_at": (self.start_at.GetValue, self.start_at.SetValue, 1),
            "upper": (self.upper.GetValue, self.upper.SetValue, False),
            "paste_mode": (self.rb_paste.GetValue, set_paste, False),
            "type_secs": (self.type_secs.GetValue, self.type_secs.SetValue, 3.0),
            "after_type": (self.after_type.GetValue, self.after_type.SetValue, 0.3),
            "humanize": (self.humanize.GetValue, self.humanize.SetValue, False),
            "paste_delay": (self.paste_delay.GetValue, self.paste_delay.SetValue, 2.0),
            "countdown": (self.countdown.GetValue, self.countdown.SetValue, 5),
            "gap": (self.gap.GetValue, self.gap.SetValue, 1.0),
            "gap_rand": (self.gap_rand.GetValue, self.gap_rand.SetValue, 0.0),
            "enter": (self.enter.GetValue, self.enter.SetValue, True),
            "minimize": (self.minimize.GetValue, self.minimize.SetValue, False),
        }

    def _save(self):
        for k, (get, _, _) in self.fields.items():
            self.config.Write(k, str(get()))
        self.config.Flush()

    def _load(self):
        for k, (_, setter, default) in self.fields.items():
            val = default
            if self.config.HasEntry(k):
                s = self.config.Read(k)
                try:
                    if isinstance(default, bool):
                        val = s == "True"
                    elif isinstance(default, int):
                        val = int(float(s))
                    elif isinstance(default, float):
                        val = float(s)
                    else:
                        val = s
                except ValueError:
                    val = default
            setter(val)

    def reset(self, e=None):
        if wx.MessageBox("Reset all settings to defaults?", "Reset", wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        for _, (_, setter, default) in self.fields.items():
            setter(default)
        self._on_change()

    # ---------------- tools / help ----------------
    def load_file(self, e):
        with wx.FileDialog(self, "Load messages", wildcard="Text files (*.txt)|*.txt|All files|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as d:
            if d.ShowModal() == wx.ID_OK:
                try:
                    with open(d.GetPath(), encoding="utf-8") as f:
                        self.text.SetValue(f.read())
                except Exception as ex:
                    wx.MessageBox(str(ex), "Could not open file", wx.OK | wx.ICON_ERROR, self)

    def save_file(self, e):
        with wx.FileDialog(self, "Save messages", wildcard="Text files (*.txt)|*.txt",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as d:
            if d.ShowModal() == wx.ID_OK:
                try:
                    with open(d.GetPath(), "w", encoding="utf-8") as f:
                        f.write(self.text.GetValue())
                except Exception as ex:
                    wx.MessageBox(str(ex), "Could not save file", wx.OK | wx.ICON_ERROR, self)

    def show_help(self, e):
        wx.MessageBox(
            "1. Type your message(s) and choose numbering, method and timing.\n"
            "2. Press Start Sending, then click the text box you want to fill\n"
            "    during the countdown.\n\n"
            "F8  - Pause / Resume (works in any window)\n"
            "F9  - Stop (works in any window)\n"
            "Mouse to the top-left screen corner - emergency stop\n\n"
            "If the target app runs as Administrator, run this app as Administrator too.",
            "Hotkeys && tips", wx.OK | wx.ICON_INFORMATION, self)

    def on_close(self, e):
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
            self.worker.join(1.5)
        self._closing = True
        self.timer.Stop()
        self._save()
        self.Destroy()


if __name__ == "__main__":
    app = wx.App(False)
    MainFrame().Show()
    app.MainLoop()
