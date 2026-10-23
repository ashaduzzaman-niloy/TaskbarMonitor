"""Data sources and formatting helpers for Taskbar Monitor.

Everything here only *reads* system information; nothing is written or deleted.
"""
import os
import platform
import socket
import sys
import time

import psutil


# --- Formatting ---------------------------------------------------------------
def fmt_bytes(n):
    """15_900_000_000 -> '14.8 GB'."""
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1000 or unit == "TB":
            if unit in ("B", "KB") or v >= 100:
                return f"{v:.0f} {unit}"
            return f"{v:.1f} {unit}"
        v /= 1024


def fmt_bits(bytes_per_sec):
    """Speed the way internet packages are sold: (number, unit), e.g. 1_310_720 B/s -> ('10', 'Mbps').

    Number and unit are returned separately so the taskbar can draw them in fixed slots.
    """
    units = ("bps", "Kbps", "Mbps", "Gbps")
    v = max(0.0, float(bytes_per_sec)) * 8
    i = 0
    while i < len(units) - 1 and (v >= 999.5 or (v >= 9.995 and round(v) >= 1000)):
        v /= 1000
        i += 1
    if i == 0 or v >= 9.95:
        return f"{v:.0f}", units[i]
    return f"{v:.1f}", units[i]


def last_power_on():
    """(unix time, how) of the last time the PC was switched on.

    Windows' Fast Startup turns "Shut down" into a kind of hibernation, so the kernel's boot
    time doesn't reset on power-on. Windows does log event 27 (Kernel-Boot) on every start, with
    boot type 0x0 = full start, 0x1 = Fast Startup, 0x2 = resume from hibernation. The newest
    0x0/0x1 event is the real power-on; hibernation, sleep and lock don't count.
    Falls back to the kernel boot time if the log can't be read.
    """
    fallback = psutil.boot_time()
    try:
        import subprocess
        import xml.etree.ElementTree as ET
        from datetime import datetime, timezone

        query = "*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and (EventID=27)]]"
        out = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{query}", "/c:30", "/rd:true", "/f:xml"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        ns = "{http://schemas.microsoft.com/win/2004/08/events/event}"
        for event in ET.fromstring(f"<events>{out}</events>").iter(f"{ns}Event"):  # newest first
            data = {d.get("Name"): (d.text or "").strip() for d in event.iter(f"{ns}Data")}
            raw = data.get("BootType") or next(iter(data.values()), "")
            boot_type = int(raw, 0)
            if boot_type in (0, 1):
                stamp = event.find(f"{ns}System/{ns}TimeCreated").get("SystemTime")
                t = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                # never earlier than the kernel boot (protects against a cleared/partial log)
                return max(t, fallback), ("Fast Startup" if boot_type == 1 else "full start")
    except Exception:
        pass
    return fallback, "boot time"


def fmt_duration(seconds):
    """93784 -> '1d 2h', 7500 -> '2h 05m', 420 -> '7m'."""
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


# --- Static / slow-changing info -----------------------------------------------
def cpu_name():
    """Marketing name of the CPU, e.g. 'AMD Ryzen 7 5800H with Radeon Graphics'."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
    except Exception:
        return platform.processor() or "Processor"


def local_ip():
    """IPv4 address of the adapter used for internet traffic.

    Connecting a UDP socket sends no packets; it only makes Windows pick the outgoing interface.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "offline"


def windows_uses_light_theme(setting="AppsUseLightTheme"):
    """True when Windows uses light mode for apps ("AppsUseLightTheme") or the taskbar ("SystemUsesLightTheme")."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, setting)[0] == 1
    except Exception:
        return False


class TopProcesses:
    """Busiest programs by CPU, with all processes of the same name added together (e.g. chrome.exe)."""

    def __init__(self):
        self.ncpu = psutil.cpu_count() or 1

    def read(self, n=5):
        groups = {}
        # process_iter reuses Process objects between calls, so cpu_percent() measures
        # usage since the previous call; the very first call reports 0 for everything.
        for p in psutil.process_iter(["name", "memory_info"]):
            if p.pid == 0:  # "System Idle Process"
                continue
            try:
                cpu = p.cpu_percent(None) / self.ncpu
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            mi = p.info["memory_info"]
            g = groups.setdefault(p.info["name"] or "?", [0.0, 0, 0])
            g[0] += cpu
            g[1] += mi.rss if mi else 0
            g[2] += 1
        rows = sorted(groups.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
        return [(name, cpu, mem, count) for name, (cpu, mem, count) in rows[:n]]


def resource_dir():
    """Folder holding bundled files (the LibreHardwareMonitor DLLs), also when frozen by PyInstaller."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def fmt_rate(bytes_per_sec):
    """1536 -> '1.5 KB/s'. Keeps at most 4 significant characters so the overlay doesn't jitter."""
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    v = float(bytes_per_sec)
    i = 0
    while v >= 1000 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        return f"{v:.0f} {units[i]}"
    return f"{v:.1f} {units[i]}" if v < 10 else f"{v:.0f} {units[i]}"


class NetSpeed:
    """Upload/download speed in bytes per second, summed over all adapters except excluded ones."""

    def __init__(self, exclude=("Loopback",)):
        self.exclude = tuple(x.lower() for x in exclude)
        self._last = (*self._totals(), time.monotonic())

    def _totals(self):
        sent = recv = 0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if any(x in name.lower() for x in self.exclude):
                continue
            sent += c.bytes_sent
            recv += c.bytes_recv
        return sent, recv

    def read(self):
        sent, recv = self._totals()
        now = time.monotonic()
        last_sent, last_recv, last_t = self._last
        dt = max(now - last_t, 1e-6)
        self._last = (sent, recv, now)
        # max(0, ...) guards against an adapter disappearing between samples
        return max(0, sent - last_sent) / dt, max(0, recv - last_recv) / dt


# Sensor names LibreHardwareMonitor uses for the "whole CPU" temperature, best first.
PREFERRED_SENSORS = (
    "CPU Package",        # Intel
    "Core (Tctl/Tdie)",   # AMD Ryzen
    "Core (Tdie)",
    "Core (Tctl)",
    "Core Average",
    "Core Max",
)


class CpuTemperature:
    """CPU temperature in °C, or None.

    Tries, in order:
      1. LibreHardwareMonitorLib.dll via pythonnet (accurate; needs Administrator)
      2. WMI MSAcpi_ThermalZoneTemperature (needs Administrator; often a motherboard
         zone rather than the CPU, and many PCs don't expose it at all)
    """

    def __init__(self):
        self.source = "unavailable"
        self.errors = []
        self._read = None
        self._lhm = None
        self._wmi = None
        for init in (self._init_lhm, self._init_wmi):
            try:
                if init():
                    return
            except Exception as e:  # missing package, DLL blocked, no admin, ...
                self.errors.append(f"{init.__name__}: {e}")
        if not is_admin():
            self.errors.append("not running as Administrator")

    def read(self):
        if self._read is None:
            return None
        try:
            return self._read()
        except Exception:
            return None

    def close(self):
        if self._lhm:
            try:
                self._lhm[0].Close()
            except Exception:
                pass

    # LibreHardwareMonitor -------------------------------------------------
    def _init_lhm(self):
        dll = os.path.join(resource_dir(), "LibreHardwareMonitorLib.dll")
        if not os.path.exists(dll):
            self.errors.append("LibreHardwareMonitorLib.dll not found next to the app")
            return False
        import clr  # provided by pythonnet
        clr.AddReference(dll)
        from LibreHardwareMonitor.Hardware import Computer, HardwareType, SensorType

        comp = Computer()
        comp.IsCpuEnabled = True
        comp.Open()
        cpus = [h for h in comp.Hardware if h.HardwareType == HardwareType.Cpu]
        self._lhm = (comp, cpus, SensorType)
        if not cpus or self._read_lhm() is None:
            comp.Close()
            self._lhm = None
            self.errors.append("LibreHardwareMonitor returned no CPU temperature")
            return False
        self._read = self._read_lhm
        self.source = "LibreHardwareMonitor"
        return True

    def _read_lhm(self):
        _, cpus, sensor_type = self._lhm
        temps = {}
        for hw in cpus:
            hw.Update()
            for s in hw.Sensors:
                if s.SensorType == sensor_type.Temperature and s.Value is not None:
                    v = float(s.Value)
                    if 0 < v < 125:
                        temps[str(s.Name)] = v
        if not temps:
            return None
        for name in PREFERRED_SENSORS:
            if name in temps:
                return temps[name]
        return max(temps.values())

    # WMI fallback ---------------------------------------------------------
    def _init_wmi(self):
        import pythoncom
        import wmi

        pythoncom.CoInitialize()  # WMI is COM; each thread must initialise it
        self._wmi = wmi.WMI(namespace="root\\wmi")
        if self._read_wmi() is None:
            return False
        self._read = self._read_wmi
        self.source = "WMI thermal zone"
        return True

    def _read_wmi(self):
        vals = [z.CurrentTemperature / 10.0 - 273.15
                for z in self._wmi.MSAcpi_ThermalZoneTemperature()]
        vals = [v for v in vals if 0 < v < 125]
        return max(vals) if vals else None


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def temperature_help(source, errors):
    """Plain-language explanation of why temperature is (not) available, with the fix."""
    if source not in ("unavailable", "starting…"):
        return f"CPU temperature is being read through {source}."
    err = " | ".join(errors)
    folder = resource_dir()
    tips = []
    if "not found" in err:
        tips.append(
            "The temperature library is missing. Download LibreHardwareMonitor from\n"
            "github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases (the .NET Framework\n"
            f"4.7.2 build) and copy LibreHardwareMonitorLib.dll and HidSharp.dll into:\n{folder}")
    if "No module named 'clr'" in err or "pythonnet" in err:
        tips.append("pythonnet isn't installed. Run:  py -m pip install pythonnet")
    if any(x in err for x in ("Could not load", "BadImageFormat", "HRESULT", "FileLoad")):
        tips.append("Windows refused to load the DLL. Right-click each DLL -> Properties -> tick\n"
                    "'Unblock' -> OK, and make sure you copied the .NET Framework 4.7.2 build.")
    if not is_admin():
        tips.append("The app isn't running as Administrator, which reading CPU sensors requires.\n"
                    "Use 'Restart as administrator' in the right-click menu.")
    elif "returned no CPU temperature" in err:
        tips.append("The library loaded but got no reading. Open LibreHardwareMonitor itself as\n"
                    "Administrator: if it shows no CPU temperature either, its hardware driver is\n"
                    "probably being blocked (Windows Security blocks some older drivers); check its\n"
                    "release notes for the current driver requirement.")
    if not tips:
        tips.append("No known cause matched; the technical details below may help.")
    body = "\n\n".join(f"{i}. {t}" for i, t in enumerate(tips, 1))
    details = "\n".join(f"- {e}" for e in errors) or "- none"
    return f"CPU temperature isn't available yet.\n\n{body}\n\nTechnical details:\n{details}"
