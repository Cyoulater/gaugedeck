#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Miguel Ycaza
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version. This program is distributed WITHOUT ANY WARRANTY.
# See the LICENSE file or <https://www.gnu.org/licenses/> for details.
"""
GaugeDeck - car-style sensor dials as a normal GTK4 app window.
Native Wayland (no X11 / XWayland), so it can't hang the desktop the way
Conky did. Open it from the dock icon, close it with the X.

Reads sensors straight from /sys/class/hwmon and /proc. Read-only:
it never writes anything to the system.
"""
import math
import sys

import gi
gi.require_version("Gtk", "4.0")
try:
    gi.require_foreign("cairo")
except ImportError:
    sys.exit("GaugeDeck needs the python3-gi-cairo package.\n"
             "Install it with:  sudo apt install python3-gi-cairo")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

import os
import re
import shutil
import subprocess

APP_NAME = "GaugeDeck"
VERSION = "1.2.1"
CONFIG_PATH = os.path.expanduser("~/.config/gaugedeck/config")


def load_config():
    cfg = {"title": "GAUGEDECK", "scale": "0.6",
           "alerts": "on", "alert_sound": "on", "alert_seconds": "5",
           "cpu_temp_alert": "80", "gpu_temp_alert": "85",
           "board_temp_alert": "60", "ram_alert": "95", "fan_stop_alert": "on",
           "nv_temp_alert": "85"}
    try:
        with open(CONFIG_PATH) as f:
            for ln in f:
                if "=" in ln and not ln.strip().startswith("#"):
                    k, v = ln.split("=", 1)
                    cfg[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return cfg


CFG = load_config()
try:
    SCALE = max(0.3, min(2.0, float(CFG["scale"])))
except ValueError:
    SCALE = 0.6
TITLE = CFG["title"][:24]
UPDATE_SECONDS = 1

GAUGES = [
    # x,   y,   r,   label,      unit,  min, max,  red,  source,       scale
    (150, 150, 110, "CPU TEMP", "°C",  0, 100,  80,   "cpu_temp",   None),
    (390, 150, 110, "CPU LOAD", "%",   0, 100,  90,   "cpu_load",   None),
    (100, 360, 80,  "GPU TEMP", "°C",  0, 100,  85,   "gpu_temp",   None),
    (270, 360, 80,  "FAN 1",    "RPM", 0, 2500, 2200, "fan1",       1000),
    (440, 360, 80,  "FAN 2",    "RPM", 0, 2500, 2200, "fan2",       1000),
    (100, 540, 80,  "GPU FAN",  "RPM", 0, 3500, 3000, "gpu_fan",    1000),
    (270, 540, 80,  "RAM",      "%",   0, 100,  90,   "ram",        None),
    (440, 540, 80,  "BOARD",    "°C",  0, 80,   60,   "board_temp", None),
]
BASE_W, BASE_H = 540, 690


# --- second card: an NVIDIA card (Tesla, etc.) next to an AMD/Intel display card
def _nvidia_model():
    """Short model name of an NVIDIA card, e.g. 'P100'. None if there's no NVIDIA card."""
    model = ""
    base = "/proc/driver/nvidia/gpus"
    try:
        for card in sorted(os.listdir(base)):
            with open(os.path.join(base, card, "information")) as f:
                for ln in f:
                    if ln.startswith("Model:"):
                        model = ln.split(":", 1)[1].strip()
            break
    except OSError:
        pass
    if not model and shutil.which("nvidia-smi"):
        try:
            model = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=3).stdout.split("\n")[0].strip()
        except (OSError, subprocess.SubprocessError):
            model = ""
    if not model or "fail" in model.lower() or "error" in model.lower():
        return None
    m = re.search(r"\b([A-Z]{1,3}\d{2,4}[A-Z]?)\b", model.replace("-", " "))
    return m.group(1) if m else "NVIDIA"


def _has_display_gpu():
    return any(n.startswith(p) for n, _ in _hwmons() for p in GPU_CHIPS)


def _nvidia_query(fields):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=" + fields, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=2).stdout.split("\n")[0]
        return [float(x) for x in out.split(",")]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


NV_MODEL = None
NV_ROW = False


def setup_layout():
    """Add a bottom row for an NVIDIA compute card when the display card is AMD/Intel."""
    global NV_MODEL, NV_ROW, BASE_H
    NV_MODEL = _nvidia_model()
    NV_ROW = bool(NV_MODEL) and _has_display_gpu()
    if not NV_ROW:
        return
    lim = _nvidia_query("power.limit")
    pmax = int(math.ceil((lim[0] if lim else 300) / 50.0) * 50)
    red = _num_cfg("nv_temp_alert", 85)
    GAUGES.append((185, 720, 80, f"{NV_MODEL} TEMP", "°C", 0, 100, red, "nv_temp", None))
    GAUGES.append((355, 720, 80, f"{NV_MODEL} POWER", "W", 0, pmax, pmax * 0.9, "nv_power", None))
    ALERT_RULES["nv_temp"] = (red, f"{NV_MODEL} temperature", "°C")
    BASE_H = 870

C_BG     = (0.05, 0.05, 0.06)
C_FACE   = (0.09, 0.09, 0.10)
C_RIM    = (0.55, 0.55, 0.58)
C_TICK   = (0.85, 0.85, 0.85)
C_RED    = (0.86, 0.10, 0.12)
C_NEEDLE = (1.00, 0.22, 0.12)
C_TEXT   = (0.95, 0.95, 0.95)
C_DIM    = (0.60, 0.60, 0.62)
A0, A1 = 135, 405   # dial sweep in degrees, like a car gauge


# ---------------------------------------------------------------- sensors
def _read(path):
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return None


CPU_CHIPS   = ["coretemp", "k10temp", "zenpower", "cpu_thermal"]
GPU_CHIPS   = ["amdgpu", "nouveau", "radeon", "i915", "xe"]
BOARD_CHIPS = ["nct", "it87", "it86", "f71", "w83", "asus", "gigabyte", "dell_smm", "thinkpad", "hp"]


def _hwmons():
    base = "/sys/class/hwmon"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for e in entries:
        path = os.path.join(base, e)
        out.append((_read(path + "/name") or "", path))
    return out


def _find(prefixes):
    for pref in prefixes:
        for name, path in _hwmons():
            if name.startswith(pref):
                return path
    return None


def _num(path, div=1):
    v = _read(path)
    try:
        return float(v) / div
    except (TypeError, ValueError):
        return None


def _fan_label(raw, n):
    """Use the machine's own fan name if it has one (Dell, ThinkPad...), else FAN n."""
    if not raw:
        return f"FAN {n}"
    t = raw.upper().replace("PROCESSOR", "CPU").replace("MOTHERBOARD", "BOARD")
    t = t.replace("SYSTEM", "SYS").replace("CHASSIS", "CASE").replace("  ", " ").strip()
    return t[:10] if t else f"FAN {n}"


class Sensors:
    """Finds the machine's sensors once at startup, then reads them."""

    def __init__(self):
        self._last_cpu = None
        self.cpu = _find(CPU_CHIPS)
        self.gpu = _find(GPU_CHIPS)
        self.board = _find(BOARD_CHIPS)
        self.nvidia = shutil.which("nvidia-smi") if not self.gpu else None
        self.nv_row = NV_ROW
        self.cpu_temp_file = self._cpu_temp_file()
        self.fans = self._fans()

    def _cpu_temp_file(self):
        if not self.cpu:
            return None
        # prefer the whole-package / Tctl reading if it's labeled
        for i in range(1, 64):
            lab = _read(f"{self.cpu}/temp{i}_label")
            if lab and ("Package" in lab or "Tctl" in lab or "Tdie" in lab):
                return f"{self.cpu}/temp{i}_input"
        return f"{self.cpu}/temp1_input"

    def _fans(self):
        # every fan header on the board chip that is actually spinning
        found = []
        if self.board:
            for i in range(1, 16):
                f = f"{self.board}/fan{i}_input"
                v = _num(f)
                if v is not None and v > 0:
                    found.append((f, _fan_label(_read(f"{self.board}/fan{i}_label"), len(found) + 1)))
        return found

    def _nvidia(self):
        try:
            out = subprocess.run(
                [self.nvidia, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2).stdout.split("\n")[0]
            return float(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def cpu_load(self):
        line = _read("/proc/stat")
        if not line:
            return None
        parts = [int(x) for x in line.split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        if self._last_cpu is None:
            self._last_cpu = (idle, total)
            return None
        li, lt = self._last_cpu
        self._last_cpu = (idle, total)
        dt = total - lt
        return 100.0 * (1 - (idle - li) / dt) if dt > 0 else None

    @staticmethod
    def ram():
        info = {}
        try:
            with open("/proc/meminfo") as f:
                for ln in f:
                    k, v = ln.split(":", 1)
                    info[k] = int(v.split()[0])
        except (OSError, ValueError):
            return None
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
        return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])

    def read_all(self):
        gpu_temp = _num(f"{self.gpu}/temp1_input", 1000) if self.gpu else None
        if gpu_temp is None and self.nvidia:
            gpu_temp = self._nvidia()
        nv = _nvidia_query("temperature.gpu,power.draw") if self.nv_row else None
        return {
            "nv_temp":    nv[0] if nv else None,
            "nv_power":   nv[1] if nv and len(nv) > 1 else None,
            "cpu_temp":   _num(self.cpu_temp_file, 1000) if self.cpu_temp_file else None,
            "board_temp": _num(f"{self.board}/temp1_input", 1000) if self.board else None,
            "fan1":       _num(self.fans[0][0]) if len(self.fans) > 0 else None,
            "fan2":       _num(self.fans[1][0]) if len(self.fans) > 1 else None,
            "gpu_temp":   gpu_temp,
            "gpu_fan":    _num(f"{self.gpu}/fan1_input") if self.gpu else None,
            "cpu_load":   self.cpu_load(),
            "ram":        self.ram(),
        }


# ---------------------------------------------------------------- drawing
def _ang(vmin, vmax, v):
    t = (v - vmin) / (vmax - vmin)
    t = max(0.0, min(1.0, t))
    return math.radians(A0 + (A1 - A0) * t)


def _centered(cr, text, x, y):
    ext = cr.text_extents(text)
    cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
    cr.show_text(text)


def draw_gauge(cr, g, v, label_override=None, alarm=False, blink=False):
    x, y, r, label, unit, vmin, vmax, red, _src, scale = g
    label = label_override or label

    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.set_source_rgb(*((0.35, 0.03, 0.04) if alarm and blink else C_FACE))
    cr.fill_preserve()
    cr.set_line_width(r * (0.09 if alarm else 0.06))
    cr.set_source_rgb(*(C_RED if alarm and blink else C_RIM))
    cr.stroke()

    cr.set_line_width(r * 0.09)
    cr.arc(x, y, r * 0.84, _ang(vmin, vmax, red), _ang(vmin, vmax, vmax))
    cr.set_source_rgb(*C_RED)
    cr.stroke()

    cr.select_font_face("DejaVu Sans", 0, 1)
    for i in range(51):
        val = vmin + (vmax - vmin) * i / 50
        a = _ang(vmin, vmax, val)
        major = i % 5 == 0
        r1 = r * (0.72 if major else 0.78)
        r2 = r * 0.88
        cr.set_line_width(r * (0.025 if major else 0.012))
        cr.set_source_rgb(*(C_RED if val >= red else C_TICK))
        cr.move_to(x + r1 * math.cos(a), y + r1 * math.sin(a))
        cr.line_to(x + r2 * math.cos(a), y + r2 * math.sin(a))
        cr.stroke()
        if major and i % 10 == 0 and 0 < i < 50:
            num = val / (scale or 1)
            s = f"{num:.1f}" if scale and num != int(num) else f"{int(round(num))}"
            cr.set_font_size(r * 0.13)
            cr.set_source_rgb(*C_DIM)
            rt = r * 0.58
            _centered(cr, s, x + rt * math.cos(a), y + rt * math.sin(a))

    cr.set_font_size(r * 0.12)
    cr.set_source_rgb(*C_DIM)
    _centered(cr, label, x, y + r * 0.30)

    txt = f"{int(round(v))} {unit}" if v is not None else "--"
    cr.set_font_size(r * 0.2)
    cr.set_source_rgb(*(C_RED if v is not None and v >= red else C_TEXT))
    _centered(cr, txt, x, y + r * 0.60)

    if v is not None:
        a = _ang(vmin, vmax, v)
        cr.set_line_cap(1)
        cr.set_line_width(r * 0.045)
        cr.set_source_rgb(*C_NEEDLE)
        cr.move_to(x - r * 0.12 * math.cos(a), y - r * 0.12 * math.sin(a))
        cr.line_to(x + r * 0.8 * math.cos(a), y + r * 0.8 * math.sin(a))
        cr.stroke()
        cr.set_line_cap(0)

    cr.arc(x, y, r * 0.08, 0, 2 * math.pi)
    cr.set_source_rgb(*C_RIM)
    cr.fill()


def draw_all(cr, values, labels=None, alarms=frozenset(), blink=False):
    cr.set_source_rgb(*C_BG)
    cr.paint()
    cr.scale(SCALE, SCALE)
    for g in GAUGES:
        src = g[8]
        on = src in alarms or (src + "_stop") in alarms
        draw_gauge(cr, g, values.get(src), (labels or {}).get(src), on, blink)
    cr.select_font_face("DejaVu Sans Condensed", 0, 1)
    cr.set_font_size(30)
    cr.set_source_rgb(*C_RED)
    _centered(cr, TITLE.upper(), BASE_W / 2, BASE_H - 35)


# ---------------------------------------------------------------- alerts
def _on(key):
    return CFG.get(key, "on").lower() in ("on", "yes", "true", "1")


def _num_cfg(key, default):
    try:
        return float(CFG.get(key, default))
    except ValueError:
        return float(default)


ALERT_RULES = {
    # source:      (limit,                                  name,          unit)
    "cpu_temp":   (_num_cfg("cpu_temp_alert", 80),   "CPU temperature",   "°C"),
    "gpu_temp":   (_num_cfg("gpu_temp_alert", 85),   "GPU temperature",   "°C"),
    "board_temp": (_num_cfg("board_temp_alert", 60), "Board temperature", "°C"),
    "ram":        (_num_cfg("ram_alert", 95),        "Memory use",        "%"),
}
HYSTERESIS = {"cpu_temp": 5, "gpu_temp": 5, "board_temp": 3, "ram": 5, "nv_temp": 5}

setup_layout()

SOUND_CMDS = [
    ["canberra-gtk-play", "-i", "alarm-clock-elapsed"],
    ["paplay", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"],
    ["pw-play", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"],
    ["paplay", "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"],
]


def play_alarm():
    """Play an alarm sound with whatever player the system has. Never blocks."""
    if not _on("alert_sound"):
        return
    for cmd in SOUND_CMDS:
        if shutil.which(cmd[0]) and (len(cmd) < 2 or not cmd[-1].startswith("/")
                                     or os.path.exists(cmd[-1])):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue


class Alerts:
    """Watches readings; fires once when a reading stays in the red,
    and again when it's back to normal. No repeat nagging."""

    def __init__(self, notify, fan_labels):
        self.notify = notify                   # function(key, title, body, urgent)
        self.fan_labels = fan_labels
        self.hold = max(1, int(_num_cfg("alert_seconds", 5)))
        self.count = {}                        # seconds a condition has been true
        self.active = set()                    # conditions currently alarmed
        self.fan_seen = {}                     # fan has been seen spinning

    def _check(self, key, bad, clear, title, body_bad, body_ok):
        if bad:
            self.count[key] = self.count.get(key, 0) + 1
            if self.count[key] >= self.hold and key not in self.active:
                self.active.add(key)
                self.notify(key, title, body_bad, True)
                play_alarm()
        else:
            self.count[key] = 0
            if key in self.active and clear:
                self.active.discard(key)
                self.notify(key, "Back to normal", body_ok, False)

    def update(self, v):
        if not _on("alerts"):
            return
        for src, (limit, name, unit) in ALERT_RULES.items():
            val = v.get(src)
            if val is None:
                continue
            self._check(src, val >= limit, val < limit - HYSTERESIS[src],
                        f"{name} is critical",
                        f"{name} is {val:.0f}{unit} (alert at {limit:.0f}{unit}).",
                        f"{name} is down to {val:.0f}{unit}.")
        if _on("fan_stop_alert"):
            for fan in ("fan1", "fan2", "gpu_fan"):
                val = v.get(fan)
                if val is None:
                    continue
                if val > 0:
                    self.fan_seen[fan] = True
                if not self.fan_seen.get(fan):
                    continue      # never saw it spin: probably fan-stop mode, not a failure
                label = self.fan_labels.get(fan, fan.upper().replace("_", " ").replace("FAN1", "FAN 1").replace("FAN2", "FAN 2"))
                self._check(fan + "_stop", val == 0, val > 0,
                            f"{label} stopped",
                            f"{label} was spinning and is now at 0 RPM. Check the fan.",
                            f"{label} is spinning again ({val:.0f} RPM).")


# ---------------------------------------------------------------- app
class GaugeWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(int(BASE_W * SCALE), int(BASE_H * SCALE))
        self.set_resizable(False)
        self.sensors = Sensors()
        self.values = self.sensors.read_all()
        self.labels = {f"fan{i + 1}": lab for i, (_f, lab) in enumerate(self.sensors.fans[:2])}
        self.area = Gtk.DrawingArea()
        self.area.set_content_width(int(BASE_W * SCALE))
        self.area.set_content_height(int(BASE_H * SCALE))
        self.blink = False
        self.test_flash = set()            # gauges flashing for --test-alert only
        self.alerts = Alerts(app.notify, self.labels)
        self.area.set_draw_func(lambda _a, cr, _w, _h: draw_all(
            cr, self.values, self.labels, self.alerts.active | self.test_flash, self.blink))
        self.set_child(self.area)
        GLib.timeout_add_seconds(UPDATE_SECONDS, self.tick)
        GLib.timeout_add(500, self.flash)

    def tick(self):
        self.values = self.sensors.read_all()
        self.alerts.update(self.values)
        self.area.queue_draw()
        return True   # keep updating while the window is open

    def start_test_flash(self, seconds=10):
        self.test_flash = {"cpu_temp"}
        GLib.timeout_add_seconds(seconds, self._end_test_flash)

    def _end_test_flash(self):
        self.test_flash = set()
        self.area.queue_draw()
        return False

    def flash(self):
        if self.alerts.active or self.test_flash:
            self.blink = not self.blink
            self.area.queue_draw()
        elif self.blink:
            self.blink = False
            self.area.queue_draw()
        return True


class GaugeApp(Gtk.Application):
    def __init__(self):
        # single-instance: clicking the icon again just brings the window forward
        super().__init__(application_id="io.github.cyoulater.GaugeDeck")

    def do_activate(self):
        win = self.props.active_window or GaugeWindow(self)
        win.present()
        if "--test-alert" in sys.argv:
            self.notify("test", "GaugeDeck test alert",
                        "Alerts are working. This is only a test.", True)
            play_alarm()
            win.start_test_flash(10)

    def notify(self, key, title, body, urgent):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_priority(Gio.NotificationPriority.URGENT if urgent
                       else Gio.NotificationPriority.NORMAL)
        self.send_notification("gaugedeck-" + key, n)


if __name__ == "__main__":
    if "--check" in sys.argv:
        print(f"GaugeDeck {VERSION}")
        print("NVIDIA card:", NV_MODEL or "not found")
        print("Display card sensor:", "found" if _has_display_gpu() else "not found")
        print("NVIDIA row:", "ON" if NV_ROW else "off")
        sys.exit(0)
    sys.exit(GaugeApp().run([a for a in sys.argv if a != "--test-alert"]))
