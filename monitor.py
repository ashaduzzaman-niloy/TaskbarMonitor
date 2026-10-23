"""Taskbar Monitor — live system stats on the Windows taskbar.

A borderless, transparent, always-on-top strip over the taskbar, just left of the tray:
  • left-click  : open / close the detail panel
  • drag        : move the strip
  • right-click : extra items, colours, temperature help, autostart, exit

Run:  pyw monitor.py      (as Administrator to get CPU temperature)
"""
import ctypes
import json
import os
import statistics
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from datetime import datetime
from tkinter import messagebox

if sys.platform != "win32":
    sys.exit("Taskbar Monitor only runs on Windows.")

import winreg

import psutil

from sensors import (CpuTemperature, NetSpeed, TopProcesses, cpu_name, fmt_bits, fmt_bytes,
                     fmt_duration, is_admin, last_power_on, local_ip, temperature_help,
                     windows_uses_light_theme)

APP_NAME = "TaskbarMonitor"
CONFIG_VERSION = 3
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "right": None,                   # saved position (right edge, top); None = next to the tray
    "y": None,
    "locked": False,
    "items": ["temp", "uptime"],     # optional items; speed and CPU/RAM are always shown
    "strip_text": "auto",            # auto | light | dark  (text colour on the taskbar)
    "theme": "auto",                 # auto | dark | light  (detail panel)
    "font": ["Segoe UI", 9],
    "interval_ms": 1000,
    "disk_path": "C:\\",
    "hide_on_fullscreen": True,
    "net_exclude": ["Loopback"],     # add e.g. "vEthernet" if Hyper-V/VPN adapters double-count
}

# Strip layout, left to right, two rows per column:
#   [↑ upload / ↓ download]  [CPU / RAM]  [optional items, two per column, in this order]
FIXED = ("up", "down", "cpu", "mem")
ITEMS = {
    "up":      {"labels": ("↑",),   "sample": None,       "bar": False, "rate": True},
    "down":    {"labels": ("↓",),   "sample": None,       "bar": False, "rate": True},
    "cpu":     {"labels": ("CPU",), "sample": "100%",     "bar": True},
    "mem":     {"labels": ("RAM",), "sample": "100%",     "bar": True},
    "temp":    {"labels": ("TEMP",), "sample": "100°C",   "bar": False, "menu": "CPU temperature"},
    "uptime":  {"labels": ("ON",),  "sample": "99d 23h",  "bar": False, "menu": "Time since power-on"},
    "disk":    {"labels": ("DISK",), "sample": "100%",    "bar": True,  "menu": "Disk space used"},
    "battery": {"labels": ("BAT", "PWR"), "sample": "100%", "bar": True, "menu": "Battery"},
}
OPTIONAL = [k for k in ITEMS if k not in FIXED]

# Text colours for the strip. Which one is used depends on the taskbar colour actually measured
# under the strip, so it stays readable on dark, light, accent-coloured or transparent taskbars.
STRIP_PALETTES = {
    "light": {"fg": "#FFFFFF", "dim": "#B8B8B8", "track": "#5C5C5C",       # light text, dark taskbar
              "accent": "#4CC2FF", "warn": "#FFB020", "alert": "#FF6B6B"},
    "dark":  {"fg": "#111111", "dim": "#4A4A4A", "track": "#B4B4B4",       # dark text, light taskbar
              "accent": "#0067C0", "warn": "#9A5B00", "alert": "#C42B1C"},
}
PANEL_PALETTES = {
    "dark":  {"bg": "#202020", "border": "#3A3A3A", "fg": "#FFFFFF", "dim": "#A0A0A0",
              "track": "#4A4A4A", "accent": "#4CC2FF", "warn": "#FFB020", "alert": "#FF5A5A"},
    "light": {"bg": "#F9F9F9", "border": "#D0D0D0", "fg": "#1A1A1A", "dim": "#5F5F5F",
              "track": "#D6D6D6", "accent": "#005FB8", "warn": "#B86E00", "alert": "#C42B1C"},
}

# --- Win32 ----------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

try:  # per-monitor DPI awareness, so Tk coordinates match real screen pixels
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
ERROR_ALREADY_EXISTS = 183
CLR_INVALID = 0xFFFFFFFF

_mutex = None  # single-instance lock, released before "Restart as administrator"


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def monitor_areas(hwnd):
    """(full monitor rect, work area rect) of the monitor the window is on."""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), ctypes.byref(mi))
    m, w = mi.rcMonitor, mi.rcWork
    return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)


def taskbar_geometry():
    """((left, top, right, bottom) of the main taskbar, x where the tray area starts) or None."""
    tb = user32.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        return None
    rect = window_rect(tb)
    tray = user32.FindWindowExW(tb, None, "TrayNotifyWnd", None)
    tray_left = window_rect(tray)[0] if tray else rect[2] - 250
    return rect, tray_left


def foreground_is_fullscreen(own_hwnds):
    fg = user32.GetForegroundWindow()
    if not fg or fg in own_hwnds:
        return False
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(fg, buf, 64)
    if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False  # the desktop and the taskbar itself cover the screen too
    (ml, mt, mr, mb), _ = monitor_areas(fg)
    l, t, r, b = window_rect(fg)
    return l <= ml and t <= mt and r >= mr and b >= mb


def point_on_screen(x, y):
    """True if (x, y) lies on the virtual desktop (handles unplugged monitors)."""
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return vx <= x < vx + vw and vy <= y < vy + vh


def screen_color(points):
    """Median colour (r, g, b) of the given screen pixels, or None."""
    hdc = user32.GetDC(None)
    if not hdc:
        return None
    rgb = []
    try:
        for x, y in points:
            c = gdi32.GetPixel(hdc, int(x), int(y))
            if c != CLR_INVALID:
                rgb.append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))
    finally:
        user32.ReleaseDC(None, hdc)
    if not rgb:
        return None
    return tuple(int(statistics.median(ch)) for ch in zip(*rgb))


def make_tool_window(widget):
    """Keep a window out of Alt+Tab and the taskbar; returns its HWND."""
    widget.update_idletasks()
    hwnd = user32.GetAncestor(widget.winfo_id(), GA_ROOT)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW)
    return hwnd


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


# --- Config & autostart -----------------------------------------------------------
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("config_version") != CONFIG_VERSION:
            # older versions: keep the settings that still mean the same thing; the strip gets
            # re-placed next to the tray because its size and anchor changed
            keep = ("locked", "theme", "font", "interval_ms", "disk_path", "hide_on_fullscreen",
                    "net_exclude", "items")
            saved = {k: v for k, v in saved.items() if k in keep}
            if saved.get("theme") not in PANEL_PALETTES:
                saved.pop("theme", None)
        cfg.update(saved)
        cfg["config_version"] = CONFIG_VERSION
    except (OSError, ValueError):
        pass
    cfg["items"] = [k for k in OPTIONAL if k in cfg["items"]]
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_target():
    """(program, arguments) that start this app again without a console window."""
    if getattr(sys, "frozen", False):  # PyInstaller .exe
        return sys.executable, ""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return exe, f'"{os.path.abspath(__file__)}"'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            exe, args = launch_target()
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" {args}'.strip())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# --- Sampling (background thread, so slow reads never freeze the UI) -------------
class Sampler(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.interval = cfg["interval_ms"] / 1000
        self.net_exclude = cfg["net_exclude"]
        self.disk_path = cfg["disk_path"]
        self.latest = None          # strip values, every interval
        self.detail = None          # extra values, only while the detail panel is open
        self.want_detail = False
        self.temp_source = "starting…"
        self.temp_errors = []
        self.power_on, self.power_on_how = psutil.boot_time(), "boot time"
        self._halt = threading.Event()

    def run(self):
        self.power_on, self.power_on_how = last_power_on()
        temp_sensor = CpuTemperature()   # created here: COM/.NET init belongs to this thread
        self.temp_source, self.temp_errors = temp_sensor.source, temp_sensor.errors
        net = NetSpeed(self.net_exclude)
        top = TopProcesses()
        psutil.cpu_percent(None)         # first call only primes the counters
        psutil.cpu_percent(None, percpu=True)
        temp, last_temp_t = None, 0.0
        ip, last_ip_t = "…", 0.0
        try:
            while not self._halt.wait(self.interval):
                now = time.monotonic()
                if now - last_temp_t >= 2.0:     # temperature changes slowly
                    temp, last_temp_t = temp_sensor.read(), now
                up, down = net.read()
                vm = psutil.virtual_memory()
                try:
                    disk = psutil.disk_usage(self.disk_path).percent
                except OSError:
                    disk = None
                bat = psutil.sensors_battery()
                self.latest = {
                    "cpu": psutil.cpu_percent(None),
                    "mem": vm.percent, "mem_used": vm.total - vm.available, "mem_total": vm.total,
                    "temp": temp, "up": up, "down": down,
                    "uptime": time.time() - self.power_on, "power_on": self.power_on,
                    "disk": disk,
                    "battery": None if bat is None else (bat.percent, bat.power_plugged, bat.secsleft),
                }
                if self.want_detail:
                    if now - last_ip_t >= 10:
                        ip, last_ip_t = local_ip(), now
                    self.detail = self._detail(top, ip)
                else:
                    self.detail = None
        finally:
            temp_sensor.close()

    def _detail(self, top, ip):
        sw = psutil.swap_memory()
        io = psutil.net_io_counters()
        disks = []
        for part in psutil.disk_partitions(all=False):
            if not part.fstype:          # empty card reader / DVD drive
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            disks.append((part.mountpoint.rstrip("\\"), u.used, u.total, u.percent))
        return {
            "per_core": psutil.cpu_percent(None, percpu=True),
            "swap": (sw.used, sw.total, sw.percent),
            "sent": io.bytes_sent, "recv": io.bytes_recv,
            "ip": ip, "disks": disks, "top": top.read(5),
        }

    def stop(self):
        self._halt.set()


def level(key, v):
    """0 normal, 1 warning, 2 alert."""
    if v is None:
        return 0
    if key == "battery":
        return 2 if v < 10 else 1 if v < 20 else 0
    warn, alert = {"cpu": (70, 90), "mem": (75, 90), "temp": (70, 85), "disk": (85, 95)}.get(key, (101, 101))
    return 2 if v >= alert else 1 if v >= warn else 0


def level_color(pal, lvl, bar=False):
    return (pal["accent"] if bar else pal["fg"], pal["warn"], pal["alert"])[lvl]


# --- Detail panel ---------------------------------------------------------------
class Painter:
    """Draws one column of the detail panel top to bottom."""

    def __init__(self, panel, x, width, y):
        self.p, self.c, self.pal, self.sc = panel, panel.canvas, panel.pal, panel.app.scale
        self.x, self.w, self.y = x, width, y

    def fit(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def line(self, left, right="", font=None, fill=None, rfill=None):
        font, fill = font or self.p.f, fill or self.pal["fg"]
        rw = font.measure(right) + int(12 * self.sc) if right else 0
        self.c.create_text(self.x, self.y, anchor="nw", text=self.fit(left, font, self.w - rw),
                           font=font, fill=fill)
        if right:
            self.c.create_text(self.x + self.w, self.y, anchor="ne", text=right, font=font, fill=rfill or fill)
        self.y += font.metrics("linespace") + int(3 * self.sc)

    def bar(self, pct, lvl):
        h = max(3, int(4 * self.sc))
        self.c.create_rectangle(self.x, self.y, self.x + self.w, self.y + h, fill=self.pal["track"], width=0)
        if pct:
            self.c.create_rectangle(self.x, self.y, self.x + self.w * min(pct, 100) / 100, self.y + h,
                                    fill=level_color(self.pal, lvl, bar=True), width=0)
        self.y += h + int(8 * self.sc)

    def section(self, title):
        self.y += int(10 * self.sc)
        self.line(title.upper(), font=self.p.fs, fill=self.pal["dim"])

    def cores(self, values):
        n, gap, h = len(values), max(1, int(2 * self.sc)), int(26 * self.sc)
        bw = (self.w - gap * (n - 1)) / n
        for i, v in enumerate(values):
            x0 = self.x + i * (bw + gap)
            self.c.create_rectangle(x0, self.y, x0 + bw, self.y + h, fill=self.pal["track"], width=0)
            self.c.create_rectangle(x0, self.y + h * (1 - v / 100), x0 + bw, self.y + h,
                                    fill=level_color(self.pal, level("cpu", v), bar=True), width=0)
        self.y += h + int(4 * self.sc)

    def programs(self, rows):
        f, sc = self.p.f, self.sc
        right = self.x + self.w
        cpu_x = right - f.measure("99.9 GB") - int(14 * sc)
        name_w = cpu_x - self.x - f.measure("100.0%") - int(8 * sc)
        for name, cpu, mem, count in rows:
            label = name if count == 1 else f"{name} ({count})"
            self.c.create_text(self.x, self.y, anchor="nw", font=f, fill=self.pal["fg"],
                               text=self.fit(label, f, name_w))
            self.c.create_text(cpu_x, self.y, anchor="ne", font=f, fill=self.pal["fg"], text=f"{cpu:.1f}%")
            self.c.create_text(right, self.y, anchor="ne", font=f, fill=self.pal["dim"], text=fmt_bytes(mem))
            self.y += f.metrics("linespace") + int(3 * sc)


class DetailPanel:
    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.win, highlightthickness=1, bd=0, yscrollincrement=int(24 * app.scale))
        self.canvas.pack()
        self.hwnd = make_tool_window(self.win)
        self.win.bind("<Escape>", lambda e: app.close_panel())
        self.win.bind("<FocusOut>", self._focus_out)
        self.win.bind("<MouseWheel>", self._wheel)
        family, size = app.cfg["font"]
        self.f = tkfont.Font(family=family, size=size + 1)
        self.fb = tkfont.Font(family=family, size=size + 2, weight="bold")
        self.fs = tkfont.Font(family=family, size=size)
        self.cpu_name = cpu_name()
        self.cores = (psutil.cpu_count(logical=False), psutil.cpu_count())
        self.scrolling = False

    def show(self):
        self.render()
        self.win.deiconify()
        self.win.focus_force()

    def destroy(self):
        self.win.destroy()

    def _focus_out(self, _e):
        # Focus moving to another window closes the panel; moving within it doesn't.
        self.win.after(150, self._check_focus)

    def _check_focus(self):
        try:
            inside = self.win.focus_get() is not None
        except (KeyError, tk.TclError):
            inside = False
        if not inside:
            self.app.close_panel()

    def _wheel(self, e):
        if self.scrolling:
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

    # -- content, as a list of sections so they can be arranged in one or two columns --
    def _sections(self, s, d):
        P = self.pal

        def header(p):
            p.line(self.cpu_name, font=self.fb)
            on = datetime.fromtimestamp(s["power_on"])
            p.line(f"On for {fmt_duration(s['uptime'])}", f"since {on:%a %d %b, %I:%M %p}",
                   font=self.fs, fill=P["dim"])

        def processor(p):
            p.section("Processor")
            p.line("Usage", f"{s['cpu']:.0f}%")
            p.bar(s["cpu"], level("cpu", s["cpu"]))
            if d:
                p.cores(d["per_core"])
            p.line(f"{self.cores[0]} cores · {self.cores[1]} threads", font=self.fs, fill=P["dim"])

        def temperature(p):
            p.section("Temperature")
            t = s["temp"]
            if t is None:
                p.line("Not available", "right-click strip → Temperature help", font=self.fs, fill=P["dim"])
            else:
                p.line(f"{t:.0f}°C", self.app.sampler.temp_source, rfill=P["dim"])

        def memory(p):
            p.section("Memory")
            p.line("RAM", f"{fmt_bytes(s['mem_used'])} / {fmt_bytes(s['mem_total'])}  ({s['mem']:.0f}%)")
            p.bar(s["mem"], level("mem", s["mem"]))
            if d and d["swap"][1]:
                p.line("Page file", f"{fmt_bytes(d['swap'][0])} / {fmt_bytes(d['swap'][1])}",
                       font=self.fs, fill=P["dim"])

        def network(p):
            p.section("Network")
            p.line("↑ Upload", " ".join(fmt_bits(s["up"])))
            p.line("↓ Download", " ".join(fmt_bits(s["down"])))
            if d:
                p.line(f"Since power-on: ↑ {fmt_bytes(d['sent'])}  ·  ↓ {fmt_bytes(d['recv'])}",
                       font=self.fs, fill=P["dim"])
                p.line("Local IP", d["ip"], font=self.fs, fill=P["dim"])

        def disks(p):
            p.section("Disks")
            for mount, used, total, pct in d["disks"]:
                p.line(mount, f"{fmt_bytes(total - used)} free of {fmt_bytes(total)}")
                p.bar(pct, level("disk", pct))

        def battery(p):
            pct, plugged, secs = s["battery"]
            p.section("Battery")
            if plugged:
                state = "plugged in"
            elif secs and secs > 0 and secs != psutil.POWER_TIME_UNLIMITED:
                state = f"{fmt_duration(secs)} left"
            else:
                state = "on battery"
            p.line(f"{pct:.0f}%", state)
            p.bar(pct, level("battery", pct))

        def programs(p):
            p.section("Busiest programs")
            p.programs(d["top"])

        out = [header, processor, temperature, memory, network]
        if d and d["disks"]:
            out.append(disks)
        if s["battery"]:
            out.append(battery)
        if d:
            out.append(programs)
        return out

    def render(self):
        s, d = self.app.sampler.latest, self.app.sampler.detail
        self.pal = P = self.app.panel_pal
        c, sc = self.canvas, self.app.scale
        c.configure(bg=P["bg"], highlightbackground=P["border"])
        col_w, pad, gutter = int(340 * sc), int(16 * sc), int(28 * sc)
        _, (wl, wt, wr, wb) = monitor_areas(self.app.hwnd)
        max_h = (wb - wt) - int(24 * sc)

        sections = self._sections(s, d) if s else [lambda p: (p.line(self.cpu_name, font=self.fb),
                                                               p.line("Collecting data…", fill=P["dim"]))]

        def draw(columns):
            """Draw sections split into columns; returns (width, content height)."""
            c.delete("all")
            bottom = pad
            for i, group in enumerate(columns):
                p = Painter(self, pad + i * (col_w + gutter), col_w, pad)
                for section in group:
                    section(p)
                bottom = max(bottom, p.y)
            width = pad * 2 + len(columns) * col_w + (len(columns) - 1) * gutter
            foot = Painter(self, pad, width - 2 * pad, bottom + int(8 * sc))
            foot.line("Esc or click elsewhere to close", font=self.fs, fill=P["dim"])
            return width, foot.y + pad - int(3 * sc)

        # 1) one column; measure each section's height on the way
        heights = []
        c.delete("all")
        p = Painter(self, pad, col_w, 0)
        for section in sections:
            y0 = p.y
            section(p)
            heights.append(p.y - y0)
        W, H = draw([sections])

        # 2) too tall for the screen: split into two balanced columns
        if H > max_h and len(sections) > 1:
            best = min(range(1, len(sections)),
                       key=lambda k: max(sum(heights[:k]), sum(heights[k:])))
            W, H = draw([sections[:best], sections[best:]])

        # 3) still too tall (very small screen): scroll with the mouse wheel
        self.scrolling = H > max_h
        view_h = min(H, max_h)
        c.configure(width=W, height=view_h, scrollregion=(0, 0, W, H))
        if not self.scrolling:
            c.yview_moveto(0)
        self._place(W + 2, view_h + 2)

    def _place(self, w, h):
        root = self.app.root
        sx, sy, sw, sh = root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()
        (ml, mt, mr, mb), (wl, wt, wr, wb) = monitor_areas(self.app.hwnd)
        gap = int(8 * self.app.scale)
        x = min(max(sx + sw - w, wl + gap), wr - w - gap)
        if sy + sh / 2 > (mt + mb) / 2:          # strip in the lower half: open upwards
            y = min(sy, wb) - h - gap
        else:
            y = max(sy + sh, wt) + gap
        y = min(max(y, wt + gap // 2), wb - h)   # never off-screen
        self.win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")


# --- Taskbar strip ----------------------------------------------------------------
class Overlay:
    def __init__(self):
        self.cfg = load_config()
        self.root = root = tk.Tk()
        root.withdraw()
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        self.scale = root.winfo_fpixels("1i") / 96
        self.font = tkfont.Font(family=self.cfg["font"][0], size=self.cfg["font"][1])
        self.has_battery = psutil.sensors_battery() is not None

        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._show_menu)
        self._drag = None
        self.panel = None
        self.sampler = None

        # first guess at the taskbar colour until it can be measured
        self.taskbar_rgb = (238, 238, 238) if windows_uses_light_theme("SystemUsesLightTheme") else (28, 28, 28)
        self._set_strip_colors(build=False)
        self._resolve_panel_theme()
        self._build_menu()
        self._build_layout()

        self.hwnd = make_tool_window(root)
        x, y = self._saved_or_default_position()
        root.geometry(f"+{x}+{y}")
        self._measure_taskbar(x, y)                # measured before showing: nothing of ours in the way
        root.deiconify()
        self._hidden = False
        self._last_color_check = time.monotonic()

        self.sampler = Sampler(self.cfg)
        self.sampler.start()
        root.after(200, self._tick)

    # -- colours --
    def _set_strip_colors(self, build=True):
        mode = self.cfg["strip_text"]
        if mode not in STRIP_PALETTES:
            r, g, b = self.taskbar_rgb
            mode = "light" if 0.299 * r + 0.587 * g + 0.114 * b < 140 else "dark"
        pal = dict(STRIP_PALETTES[mode])
        # The see-through colour is the taskbar's own colour, so anti-aliased text edges blend in.
        # It just mustn't be identical to a colour we draw with.
        rgb = list(self.taskbar_rgb)
        used = {v.lower() for v in pal.values()}
        while hex_color(rgb) in used:
            rgb[2] = rgb[2] + 1 if rgb[2] < 255 else rgb[2] - 1
        pal["key"] = hex_color(rgb)
        self.pal = pal
        self.root.configure(bg=pal["key"])
        self.root.attributes("-transparentcolor", pal["key"])
        self.canvas.configure(bg=pal["key"])
        if build:
            self._build_layout()

    def _measure_taskbar(self, x=None, y=None):
        """Measure the colour just around the strip and adapt the text colour to it."""
        self.root.update_idletasks()
        x = self.root.winfo_x() if x is None else x
        y = self.root.winfo_y() if y is None else y
        w, h = self.canvas.winfo_reqwidth(), self.canvas.winfo_reqheight()
        top, bottom = y - 2, y + h + 1
        geo = taskbar_geometry()
        if geo:                                     # stay inside the taskbar when the strip is on it
            (tl, tt, tr, tb), _ = geo
            if tl <= x + w / 2 <= tr and tt <= y + h / 2 <= tb:
                top, bottom = max(top, tt + 1), min(bottom, tb - 2)
        pts = [(x + w * f, yy) for f in (0.1, 0.3, 0.5, 0.7, 0.9) for yy in (top, bottom)]
        pts += [(x - 4, y + h / 2), (x + w + 4, y + h / 2)]
        rgb = screen_color(pts)
        if rgb is None:
            return
        changed = max(abs(a - b) for a, b in zip(rgb, self.taskbar_rgb)) > 10
        if changed:
            self.taskbar_rgb = rgb
            self._set_strip_colors()

    def _resolve_panel_theme(self):
        name = self.cfg["theme"]
        if name not in PANEL_PALETTES:
            name = "light" if windows_uses_light_theme("AppsUseLightTheme") else "dark"
        self.panel_pal = PANEL_PALETTES[name]

    # -- layout --
    def _visible_items(self):
        extra = [k for k in OPTIONAL if k in self.cfg["items"] and (k != "battery" or self.has_battery)]
        return list(FIXED) + extra

    def _build_layout(self):
        c, f, sc, P = self.canvas, self.font, self.scale, self.pal
        c.delete("all")
        lh = f.metrics("linespace")
        bar_h = max(2, round(2 * sc))
        row_h = lh + bar_h + max(1, round(sc))
        inner, col_gap, pad = round(6 * sc), round(14 * sc), round(2 * sc)
        # speed: number in a fixed right-aligned slot, unit in a fixed left-aligned slot
        num_w = max(f.measure(n) for n in ("999", "9.9"))
        unit_w = max(f.measure(u) for u in ("bps", "Kbps", "Mbps", "Gbps"))
        unit_gap = round(3 * sc)

        keys = self._visible_items()
        self.cells = {}
        x = pad
        for i in range(0, len(keys), 2):
            col = keys[i:i + 2]
            lw = max(f.measure(lbl) for k in col for lbl in ITEMS[k]["labels"])
            if all(ITEMS[k].get("rate") for k in col):
                vw = num_w + unit_gap + unit_w
            else:
                vw = max(f.measure(ITEMS[k]["sample"]) for k in col)
            vx = x + lw + inner
            for row, k in enumerate(col):
                y = row * row_h
                cell = {"label": c.create_text(x, y, anchor="nw", text=ITEMS[k]["labels"][0],
                                               font=f, fill=P["dim"])}
                if ITEMS[k].get("rate"):
                    cell["value"] = c.create_text(vx + num_w, y, anchor="ne", text="--", font=f, fill=P["fg"])
                    cell["unit"] = c.create_text(vx + num_w + unit_gap, y, anchor="nw", text="",
                                                 font=f, fill=P["dim"])
                else:
                    cell["value"] = c.create_text(vx + vw, y, anchor="ne", text="--", font=f, fill=P["fg"])
                if ITEMS[k]["bar"]:
                    by = y + lh
                    c.create_rectangle(vx, by, vx + vw, by + bar_h, fill=P["track"], width=0)
                    cell["bar"] = c.create_rectangle(vx, by, vx, by + bar_h, fill=P["accent"], width=0)
                    cell["bar_box"] = (vx, by, vw, bar_h)
                self.cells[k] = cell
            x = vx + vw + col_gap
        width, height = x - col_gap + pad, 2 * row_h

        # keep the right edge where it is, so the strip grows away from the tray
        mapped = self.root.winfo_ismapped()
        right = self.root.winfo_x() + c.winfo_reqwidth() if mapped else None
        c.configure(width=width, height=height)
        if mapped and right - width != self.root.winfo_x():
            self.root.geometry(f"+{right - width}+{self.root.winfo_y()}")
        if self.sampler and self.sampler.latest:
            self._update_values(self.sampler.latest)

    def _saved_or_default_position(self):
        self.root.update_idletasks()
        w = self.canvas.winfo_reqwidth()
        right, y = self.cfg["right"], self.cfg["y"]
        if right is not None and y is not None and point_on_screen(right - w, y):
            return right - w, y
        return self._default_position()

    def _default_position(self):
        self.root.update_idletasks()
        w, h = self.canvas.winfo_reqwidth(), self.canvas.winfo_reqheight()
        geo = taskbar_geometry()
        if not geo:
            return self.root.winfo_screenwidth() - w - 300, self.root.winfo_screenheight() - h - 4
        (l, t, r, b), tray_left = geo
        if r - l >= b - t:                       # horizontal taskbar (bottom or top)
            return tray_left - w - round(8 * self.scale), t + (b - t - h) // 2
        return l + (r - l - w) // 2, b - h - 220  # vertical taskbar: near the bottom

    def _save_position(self):
        self.cfg["right"] = self.root.winfo_x() + self.canvas.winfo_reqwidth()
        self.cfg["y"] = self.root.winfo_y()
        save_config(self.cfg)

    # -- values --
    def _cell_value(self, k, s):
        """(label, text, unit or None, bar percent or None, level)."""
        if k in ("up", "down"):
            num, unit = fmt_bits(s[k])
            return ITEMS[k]["labels"][0], num, unit, None, 0
        if k in ("cpu", "mem"):
            return ITEMS[k]["labels"][0], f"{s[k]:.0f}%", None, s[k], level(k, s[k])
        if k == "temp":
            t = s["temp"]
            return "TEMP", "--" if t is None else f"{t:.0f}°C", None, None, level("temp", t)
        if k == "uptime":
            return "ON", fmt_duration(s["uptime"]), None, None, 0
        if k == "disk":
            v = s["disk"]
            return "DISK", "--" if v is None else f"{v:.0f}%", None, v, level("disk", v)
        if k == "battery":
            if not s["battery"]:
                return "BAT", "--", None, None, 0
            pct, plugged, _ = s["battery"]
            return ("PWR" if plugged else "BAT"), f"{pct:.0f}%", None, pct, level("battery", pct)
        return k, "?", None, None, 0

    def _update_values(self, s):
        c = self.canvas
        for k, cell in self.cells.items():
            label, text, unit, pct, lvl = self._cell_value(k, s)
            c.itemconfigure(cell["label"], text=label)
            c.itemconfigure(cell["value"], text=text, fill=level_color(self.pal, lvl))
            if "unit" in cell:
                c.itemconfigure(cell["unit"], text=unit)
            if "bar" in cell:
                bx, by, bw, bh = cell["bar_box"]
                c.coords(cell["bar"], bx, by, bx + bw * min(pct or 0, 100) / 100, by + bh)
                c.itemconfigure(cell["bar"], fill=level_color(self.pal, lvl, bar=True))

    # -- mouse --
    def _press(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y(), e.x_root, e.y_root, False)

    def _motion(self, e):
        if self._drag is None or self.locked_var.get():
            return
        dx, dy, x0, y0, moved = self._drag
        if not moved and abs(e.x_root - x0) + abs(e.y_root - y0) < 4:
            return                                # tiny wobble during a click isn't a drag
        self._drag = (dx, dy, x0, y0, True)
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")
        if self.panel:
            self.close_panel()

    def _release(self, _e):
        moved = self._drag and self._drag[4]
        self._drag = None
        if moved:
            self._save_position()
            self._measure_taskbar()               # new spot may have a different background
        else:
            self.toggle_panel()

    # -- detail panel --
    def toggle_panel(self):
        if self.panel:
            self.close_panel()
        else:
            self.sampler.want_detail = True
            self.panel = DetailPanel(self)
            self.panel.show()

    def close_panel(self):
        if self.panel:
            self.panel.destroy()
            self.panel = None
            self.sampler.want_detail = False

    # -- menu --
    def _build_menu(self):
        self.locked_var = tk.BooleanVar(value=self.cfg["locked"])
        self.startup_var = tk.BooleanVar(value=startup_enabled())
        self.strip_text_var = tk.StringVar(value=self.cfg["strip_text"])
        self.theme_var = tk.StringVar(value=self.cfg["theme"])
        self.item_vars = {k: tk.BooleanVar(value=k in self.cfg["items"]) for k in OPTIONAL}

        self.menu = m = tk.Menu(self.root, tearoff=0)
        extra = tk.Menu(m, tearoff=0)
        for k in OPTIONAL:
            label = ITEMS[k]["menu"] + ("" if k != "battery" or self.has_battery else " (no battery found)")
            extra.add_checkbutton(label=label, variable=self.item_vars[k], command=self._toggle_item)
        text = tk.Menu(m, tearoff=0)
        for value, label in (("auto", "Automatic (matches the taskbar)"), ("light", "White"), ("dark", "Black")):
            text.add_radiobutton(label=label, value=value, variable=self.strip_text_var, command=self._set_strip_text)
        theme = tk.Menu(m, tearoff=0)
        for value, label in (("auto", "Match Windows"), ("dark", "Dark"), ("light", "Light")):
            theme.add_radiobutton(label=label, value=value, variable=self.theme_var, command=self._set_panel_theme)

        m.add_command(label="Show details", command=self.toggle_panel)
        m.add_cascade(label="Extra items", menu=extra)
        m.add_cascade(label="Taskbar text colour", menu=text)
        m.add_cascade(label="Detail panel theme", menu=theme)
        m.add_separator()
        m.add_command(label="Temperature help…", command=self._temperature_help)
        m.add_command(label="Restart as administrator", command=self._restart_as_admin,
                      state="disabled" if is_admin() else "normal")
        m.add_separator()
        m.add_checkbutton(label="Lock position", variable=self.locked_var, command=self._toggle_lock)
        m.add_command(label="Reset position", command=self._reset_position)
        m.add_checkbutton(label="Start with Windows", variable=self.startup_var, command=self._toggle_startup)
        m.add_separator()
        m.add_command(label="Exit", command=self.quit)

    def _show_menu(self, e):
        self.close_panel()
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_item(self):
        self.cfg["items"] = [k for k in OPTIONAL if self.item_vars[k].get()]
        save_config(self.cfg)
        self._build_layout()

    def _set_strip_text(self):
        self.cfg["strip_text"] = self.strip_text_var.get()
        save_config(self.cfg)
        self._set_strip_colors()

    def _set_panel_theme(self):
        self.cfg["theme"] = self.theme_var.get()
        save_config(self.cfg)
        self._resolve_panel_theme()

    def _temperature_help(self):
        messagebox.showinfo("Taskbar Monitor — temperature",
                            temperature_help(self.sampler.temp_source, self.sampler.temp_errors),
                            parent=self.root)

    def _restart_as_admin(self):
        global _mutex
        exe, args = launch_target()
        if _mutex:
            kernel32.CloseHandle(_mutex)          # let the elevated copy pass the single-instance check
            _mutex = None
        # "runas" shows the UAC prompt; values above 32 mean the launch succeeded
        if (shell32.ShellExecuteW(None, "runas", exe, args, None, 1) or 0) > 32:
            self.quit()
        else:
            _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")

    def _toggle_lock(self):
        self.cfg["locked"] = self.locked_var.get()
        save_config(self.cfg)

    def _reset_position(self):
        self.cfg["right"] = self.cfg["y"] = None
        save_config(self.cfg)
        x, y = self._default_position()
        self.root.geometry(f"+{x}+{y}")
        self.root.after(100, self._measure_taskbar)

    def _toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
        except OSError:
            self.startup_var.set(startup_enabled())

    def quit(self):
        self.sampler.stop()
        self.root.destroy()

    # -- refresh loop --
    def _tick(self):
        now = time.monotonic()
        if now - self._last_color_check > 3 and not self._hidden:
            # follow taskbar colour changes (Windows theme switch, transparency, wallpaper)
            self._last_color_check = now
            self._measure_taskbar()
            self._resolve_panel_theme()

        s = self.sampler.latest
        if s:
            self._update_values(s)
        if self.panel:
            self.panel.render()

        # Hide over full-screen apps; otherwise keep the strip above the taskbar,
        # which raises itself whenever it is clicked.
        own = {self.hwnd} | ({self.panel.hwnd} if self.panel else set())
        if self.cfg["hide_on_fullscreen"] and foreground_is_fullscreen(own):
            if not self._hidden:
                self.close_panel()
                self.root.withdraw()
                self._hidden = True
        else:
            if self._hidden:
                self.root.deiconify()
                self._hidden = False
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

        self.root.after(500, self._tick)

    def run(self):
        self.root.mainloop()


def main():
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return                                    # already running
    Overlay().run()


if __name__ == "__main__":
    main()
