# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Miguel Ycaza
"""
GaugeDeck on Windows.

gaugedeck.py reads Linux's /sys and /proc. On Windows, install() swaps in the pieces below before
gaugedeck.py builds its sensor list, so the rest of the app (gauges, looks, alerts, windows) runs
unchanged:

  CPU load, memory, disk    from Windows itself
  NVIDIA cards              from nvidia-smi, same as on Linux
  CPU temperature, fans,    from LibreHardwareMonitor (free, https://github.com/LibreHardwareMonitor),
  board, drives, other GPUs which must be running with its web server on. Windows has no standard
                            way for an app to read these.

The LibreHardwareMonitor parts are plain Python and are tested on Linux (tests/test_windows.py).
"""
import functools
import json
import os
import re
import subprocess
import sys
import types
import urllib.request

LHM_URL = "http://localhost:8085/data.json"
LHM_HELP = ("Start LibreHardwareMonitor, open Options, then Remote Web Server and choose Run "
            "(and Options, Run On Windows Startup, to keep it on).")

# LibreHardwareMonitor sensor type -> (GaugeDeck kind, unit)
LHM_TYPES = {"Temperature": ("temp", "°C"), "Fan": ("fan", "RPM"), "Load": ("load", "%"),
             "Voltage": ("volt", "V"), "Power": ("power", "W"), "Clock": ("freq", "MHz"),
             "Control": ("fan", "%"), "Level": ("load", "%")}
CPU_TEMP_NAMES = ("CPU Package", "Core (Tctl/Tdie)", "Core (Tctl)", "CPU (Tctl/Tdie)", "Core (Tdie)",
                  "Core Max", "Core Average", "CPU Core")
BOARD_TEMP_NAMES = ("System", "Motherboard", "Mainboard", "SYSTIN", "Board")


# ---------------------------------------------------------------- LibreHardwareMonitor
def parse_value(text):
    """'45.0 °C' -> 45.0, '1,234 RPM' -> 1234.0, '1,2 V' -> 1.2 (decimal comma). None if no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"-?[\d.,]+", str(text))
    if not m:
        return None
    num = m.group(0)
    if "," in num and "." in num:                       # 1,234.5 or 1.234,5
        num = num.replace(",", "") if num.rfind(".") > num.rfind(",") else num.replace(".", "").replace(",", ".")
    elif "," in num:                                    # 1,234 (thousands) or 1,2 (decimal)
        whole, _, frac = num.rpartition(",")
        num = whole.replace(",", "") + ("" if len(frac) == 3 else ".") + frac
    try:
        return float(num)
    except ValueError:
        return None


def walk(tree):
    """Every sensor in a data.json tree -> list of dicts: id, name, type, value, hardware, hw_id.
    The tree goes computer > hardware > group (Temperatures, Fans...) > sensor, and each sensor id
    starts with its hardware's id: /amdcpu/0/temperature/2 belongs to /amdcpu/0."""
    out = []

    def visit(node, texts):
        sid = node.get("SensorId")
        if sid and node.get("Type"):
            parts = sid.strip("/").split("/")
            out.append({"id": sid, "name": node.get("Text", ""), "type": node["Type"],
                        "value": parse_value(node.get("RawValue", node.get("Value"))),
                        "hardware": texts[-2] if len(texts) >= 2 else "",
                        "hw_id": "/" + "/".join(parts[:-2]) if len(parts) > 2 else sid})
            return
        for k in node.get("Children") or []:
            visit(k, texts + [node.get("Text", "")])

    visit(tree or {}, [])
    return out


def fetch(url=LHM_URL, timeout=1.0):
    """LibreHardwareMonitor's sensor tree, or None if it isn't running with its web server on."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def _group(hw_id):
    h = hw_id.lower()
    if "cpu" in h:
        return "CPU"
    if "gpu" in h:
        return "GPU"
    if any(x in h for x in ("nvme", "hdd", "ssd", "storage", "ata")):
        return "Storage"
    if any(x in h for x in ("ram", "memory")):
        return "System"
    return "Board"


def lhm_catalog(sensors, gd, taken):
    """Gauges for LibreHardwareMonitor sensors (the list walk() returns).
    gd is the gaugedeck module (for _sensor and friends); taken is the set of keys already used.
    The usual gauges (cpu_temp, gpu_temp, fan1..., board_temp) are picked out and shown by default."""
    found = []

    def add(s):
        base, key, n = s["key"], s["key"], 2
        while key in taken:
            key, n = f"{base}_{n}", n + 1
        s["key"] = key
        taken.add(key)
        found.append(s)

    used = set()
    temps = [s for s in sensors if s["type"] == "Temperature" and s["value"] is not None]

    def first(cands, names):
        for want in names:
            for s in cands:
                if s["name"].lower() == want.lower():
                    return s
        return cands[0] if cands else None

    cpu = first([s for s in temps if _group(s["hw_id"]) == "CPU"], CPU_TEMP_NAMES)
    if cpu and "cpu_temp" not in taken:
        used.add(cpu["id"])
        add(gd._sensor("cpu_temp", "CPU TEMP", "CPU temperature", "temp", "°C", 0, 100,
                       gd._num_cfg("cpu_temp_alert", 80), ("LibreHardwareMonitor", cpu["id"]), True, "CPU",
                       big=True, alert_name="CPU temperature"))
    gpu = first([s for s in temps if _group(s["hw_id"]) == "GPU"], ("GPU Core", "GPU Hot Spot"))
    if gpu and "gpu_temp" not in taken:
        used.add(gpu["id"])
        add(gd._sensor("gpu_temp", "GPU TEMP", "GPU temperature", "temp", "°C", 0, 100,
                       gd._num_cfg("gpu_temp_alert", 85), ("LibreHardwareMonitor", gpu["id"]), True, "GPU",
                       alert_name="GPU temperature"))
    board = [s for s in temps if _group(s["hw_id"]) == "Board"]
    bt = next((s for want in BOARD_TEMP_NAMES for s in board if s["name"].lower() == want.lower()), None)
    if bt and "board_temp" not in taken:
        used.add(bt["id"])
        add(gd._sensor("board_temp", "BOARD", "Board temperature", "temp", "°C", 0, 80,
                       gd._num_cfg("board_temp_alert", 60), ("LibreHardwareMonitor", bt["id"]), True, "Board",
                       alert_name="Board temperature"))
    fan_n = 0
    for s in sensors:
        if s["type"] == "Fan" and _group(s["hw_id"]) == "Board" and (s["value"] or 0) > 0:
            fan_n += 1
            used.add(s["id"])
            top = gd._nice_ceil(max(2500, s["value"] * 1.3))
            add(gd._sensor(f"fan{fan_n}", gd._fan_label(s["name"], fan_n), f"{s['name']} speed", "fan", "RPM",
                           0, top, round(top * 0.88), ("LibreHardwareMonitor", s["id"]), True, "Fans",
                           scale=1000))

    # everything else, to switch on in Choose sensors
    for s in sensors:
        if s["id"] in used or s["type"] not in LHM_TYPES:
            continue
        kind, unit = LHM_TYPES[s["type"]]
        v = s["value"] or 0
        group = _group(s["hw_id"])
        red = 70 if group == "Storage" else 85
        vmax = 100
        if kind == "fan" and unit == "RPM":
            vmax = gd._nice_ceil(max(2500, v * 1.3))
            red = round(vmax * 0.88)
        elif kind in ("volt", "freq"):
            vmax = red = gd._nice_ceil(max(v * 1.5, 2 if kind == "volt" else 500))
        elif kind == "power":
            vmax = gd._nice_ceil(max(v * 2, 50))
            red = vmax * 0.9
        elif kind == "load":
            red = 95
        if kind == "fan":
            group = "Fans"
        elif kind in ("volt", "power"):
            group = "Power and voltage"
        who = s["hardware"] or group
        add(gd._sensor("lhm_" + gd._slug(s["id"]), s["name"].upper()[:12], f"{who} {s['name']}", kind, unit,
                       0, vmax, red, ("LibreHardwareMonitor", s["id"]), False, group,
                       scale=1000 if unit == "RPM" else None))
    return found


# ---------------------------------------------------------------- Windows itself
def _system_times():
    import ctypes
    from ctypes import wintypes
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    t = [(f.dwHighDateTime << 32) | f.dwLowDateTime for f in (idle, kernel, user)]
    return t[0], t[1] + t[2]                 # kernel time includes idle time


def _memory_status():
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    return m if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None


def make_sensors_class(gd):
    class WindowsSensors(gd.Sensors):
        """Reads Windows' own counters and LibreHardwareMonitor, and nvidia-smi like on Linux."""

        def __init__(self, catalog):
            super().__init__(catalog)
            self._times = None
            self._lhm = None

        def read_all(self, want=None):
            self._lhm = None                     # ask LibreHardwareMonitor once per round
            return super().read_all(want)

        def _lhm_values(self):
            if self._lhm is None:
                tree = fetch(gd.CFG.get("lhm_url", LHM_URL))
                self._lhm = {s["id"]: s["value"] for s in walk(tree)} if tree else {}
            return self._lhm

        def _one(self, src, nv):
            if src[0] == "LibreHardwareMonitor":
                return self._lhm_values().get(src[1])
            if src[0] != "Windows":
                return super()._one(src, nv)
            what = src[1]
            if what == "cpu_load":
                now = _system_times()
                last, self._times = self._times, now
                if not now or not last or now[1] <= last[1]:
                    return None
                return 100.0 * (1 - (now[0] - last[0]) / (now[1] - last[1]))
            if what in ("ram", "commit"):
                m = _memory_status()
                if not m:
                    return None
                if what == "ram":
                    return 100.0 * (1 - m.ullAvailPhys / m.ullTotalPhys) if m.ullTotalPhys else None
                return 100.0 * (1 - m.ullAvailPageFile / m.ullTotalPageFile) if m.ullTotalPageFile else None
            if what == "disk":
                import shutil
                try:
                    u = shutil.disk_usage(src[2])
                except OSError:
                    return None
                return 100.0 * u.used / u.total if u.total else None
            return None

    return WindowsSensors


def make_discover(gd, linux_discover):
    def discover(root="/", nvidia=None, cpu_model=None):
        """The Linux scan finds nothing on Windows but still adds CPU load, memory, disk and every
        NVIDIA card (from nvidia-smi). Point those at Windows, then add LibreHardwareMonitor's sensors."""
        nowhere = os.path.join(os.environ.get("TEMP", "C:\\"), "gaugedeck-no-sysfs")
        found = linux_discover(nowhere, nvidia, cpu_model or "")
        drive = os.environ.get("SystemDrive", "C:")
        keep = []
        for s in found:
            if s["key"] == "cpu_load":
                s["src"] = ("Windows", "cpu_load")
            elif s["key"] == "ram":
                s["src"] = ("Windows", "ram")
            elif s["key"] == "swap":
                s.update(src=("Windows", "commit"), label="COMMIT", name="Memory committed (RAM + page file)")
            elif s["key"] == "disk_root":
                s.update(src=("Windows", "disk", drive + "\\"), label=f"DISK {drive}",
                         name=f"Disk space used ({drive})")
            elif s["src"][0] not in ("nv", "nv_vram"):
                continue                         # nothing else from the Linux scan applies here
            keep.append(s)
        tree = fetch(gd.CFG.get("lhm_url", LHM_URL), timeout=2.0)
        if tree:
            keep += lhm_catalog(walk(tree), gd, {s["key"] for s in keep})
        # the usual layout puts CPU temperature first
        order = {"cpu_temp": 0, "cpu_load": 1, "gpu_temp": 2}
        keep.sort(key=lambda s: order.get(s["key"], 3))
        return keep
    return discover


# ---------------------------------------------------------------- the rest of the app
def _frozen():
    return bool(getattr(sys, "frozen", False))


def _quiet_subprocess():
    """subprocess, but programs it starts (nvidia-smi, more GaugeDeck windows) don't flash a console."""
    quiet = types.ModuleType("subprocess")
    quiet.__dict__.update(subprocess.__dict__)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    quiet.run = functools.partial(subprocess.run, creationflags=flags)
    quiet.Popen = functools.partial(subprocess.Popen, creationflags=flags)
    return quiet


def _play_alarm(gd):
    def play_alarm():
        if not gd._on("alert_sound"):
            return
        try:
            import winsound
            winsound.PlaySound("SystemHand", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except (ImportError, RuntimeError):
            pass
    return play_alarm


def _spawn_self(gd):
    def spawn_self(*args):
        """Start another GaugeDeck window as its own program."""
        cmd = [sys.executable] + ([] if _frozen() else [os.path.abspath(gd.__file__)]) + list(args)
        if gd.DEMO:
            cmd.append("--demo")
        try:
            gd._run_detached(cmd)
            return True
        except OSError:
            return False
    return spawn_self


def _start_menu_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "GaugeDeck")


def _ensure_launcher(gd):
    def ensure_launcher(mode, key, label, theme):
        """A Start menu shortcut for a gauge or the strip, so it can be pinned to the taskbar."""
        did = gd.desktop_id_for(mode, key)
        args = f"--gauge {key}" if mode == "gauge" else "--strip"
        target = sys.executable
        if not _frozen():
            args = f'"{os.path.abspath(gd.__file__)}" {args}'
        link = os.path.join(_start_menu_dir(), f"{gd.APP_NAME} - {label}.lnk")
        try:
            os.makedirs(_start_menu_dir(), exist_ok=True)
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:GD_LINK);"
                  "$s.TargetPath=$env:GD_TARGET;$s.Arguments=$env:GD_ARGS;$s.IconLocation=$env:GD_TARGET;$s.Save()")
            env = dict(os.environ, GD_LINK=link, GD_TARGET=target, GD_ARGS=args)
            gd.subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                              env=env, timeout=20, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
        return did
    return ensure_launcher


def _remove_launchers():
    def remove_launchers():
        removed = 0
        try:
            names = os.listdir(_start_menu_dir())
        except OSError:
            return 0
        for n in names:
            if n.startswith("GaugeDeck - ") and n.endswith(".lnk"):
                try:
                    os.remove(os.path.join(_start_menu_dir(), n))
                    removed += 1
                except OSError:
                    pass
        return removed
    return remove_launchers


def _fix_stdio():
    """GaugeDeck.exe is a window app, so Windows gives it no console. Make  GaugeDeck.exe --sensors
    print to the terminal (or file) it was started from."""
    if sys.stdout is not None:
        return
    import ctypes
    import msvcrt
    k32 = ctypes.windll.kernel32
    for num, name in ((-11, "stdout"), (-12, "stderr")):
        h = k32.GetStdHandle(num)
        if h and h != -1:
            try:
                fd = msvcrt.open_osfhandle(h, os.O_WRONLY)
                setattr(sys, name, open(fd, "w", buffering=1, encoding="utf-8", errors="replace"))
                continue
            except OSError:
                pass
        if k32.AttachConsole(-1) or k32.GetConsoleWindow():
            try:
                setattr(sys, name, open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace"))
            except OSError:
                pass


def install(gd):
    """Swap the Linux-only parts of gaugedeck.py (the module gd) for Windows ones."""
    _fix_stdio()
    gd.subprocess = _quiet_subprocess()
    gd.discover = make_discover(gd, gd.discover)
    gd.Sensors = make_sensors_class(gd)
    gd.play_alarm = _play_alarm(gd)
    gd.spawn_self = _spawn_self(gd)
    gd.ensure_launcher = _ensure_launcher(gd)
    gd.remove_launchers = _remove_launchers()
    gd.FONTS.update(sans="Segoe UI", serif="Georgia", mono="Consolas")
    gd.TITLE_FONTS.update(sans="Segoe UI Semibold", serif="Georgia", mono="Consolas")
    gd._NEEDS = "GaugeDeck could not load GTK 4. Please reinstall GaugeDeck."
