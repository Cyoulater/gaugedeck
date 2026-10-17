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

Sensors are read straight from /sys/class/hwmon and /proc. Reading never
writes anything to the system. GaugeDeck writes only its own files:
~/.config/gaugedeck/ (settings, look, window state) and, when you pin a gauge
to the dock, a launcher in ~/.local/share/applications/.

Three kinds of window:
  the deck    all the gauges together (right-click a gauge to pop it out)
  a gauge     one gauge in its own window you can drag anywhere
  the strip   a small bar of readings, easy to keep in view
"""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from xml.sax.saxutils import escape as xml_escape

_NEEDS = ("GaugeDeck needs GTK 4 and its Python support.\n"
          "Install them with:  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0")
try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    import cairo
    gi.require_foreign("cairo")
except (ImportError, ValueError):
    sys.exit(_NEEDS)
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

APP_NAME = "GaugeDeck"
VERSION = "1.3.0"
APP_ID = "io.github.cyoulater.GaugeDeck"
CONFIG_DIR = os.environ.get("GAUGEDECK_CONFIG_DIR") or os.path.expanduser("~/.config/gaugedeck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config")
THEME_PATH = os.path.join(CONFIG_DIR, "theme.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
DEMO = "--demo" in sys.argv       # made-up readings, for trying the look on any machine
# GTK 4.10 added new dialog widgets. Older systems (Debian 12 and MX Linux 23 ship GTK 4.8) get the
# older ones, which still work. GAUGEDECK_OLD_GTK=1 forces the older ones, for testing.
_OLD = bool(os.environ.get("GAUGEDECK_OLD_GTK"))
HAS_COLOR_DIALOG = hasattr(Gtk, "ColorDialogButton") and not _OLD
HAS_ALERT_DIALOG = hasattr(Gtk, "AlertDialog") and not _OLD
HAS_FILE_DIALOG = hasattr(Gtk, "FileDialog") and not _OLD


def load_config(path=None):
    """Read ~/.config/gaugedeck/config (key=value lines). Files from older versions load unchanged:
    unknown keys are ignored, and notes after a # on the same line (as in the README example) are fine."""
    cfg = {"title": "GAUGEDECK", "scale": "0.6", "update_seconds": "1",
           "alerts": "on", "alert_sound": "on", "alert_seconds": "5",
           "cpu_temp_alert": "80", "gpu_temp_alert": "85",
           "board_temp_alert": "60", "ram_alert": "95", "fan_stop_alert": "on",
           "nv_temp_alert": "85", "dock_badge": "on"}
    try:
        with open(path or CONFIG_PATH, errors="replace") as f:
            for ln in f:
                ln = ln.strip().lstrip("\ufeff")
                if "=" in ln and not ln.startswith("#"):
                    k, v = ln.split("=", 1)
                    v = v.strip()
                    if k.strip().lower() != "title":                      # a title may hold a "#"
                        v = re.sub(r"\s+#.*$", "", v)                     # "0.6   # 0.3 = tiny" -> "0.6"
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    cfg[k.strip().lower()] = v
    except OSError:
        pass
    return cfg


CFG = load_config()


def _on(key):
    return CFG.get(key, "on").lower() in ("on", "yes", "true", "1")


def _num_cfg(key, default):
    try:
        return float(CFG.get(key, default))
    except ValueError:
        return float(default)


try:
    SCALE = max(0.3, min(2.0, float(CFG["scale"])))
except ValueError:
    SCALE = 0.6
TITLE = CFG["title"][:24]
try:
    UPDATE_SECONDS = max(0.5, min(10.0, float(CFG["update_seconds"])))
except ValueError:
    UPDATE_SECONDS = 1.0


def _gauge(key, label, unit, vmin, vmax, red, scale=None, big=False):
    return {"key": key, "label": label, "unit": unit, "min": vmin, "max": vmax,
            "red": red, "scale": scale, "big": big}


def _sensor(key, label, name, kind, unit, vmin, vmax, red, src, default=False, group="Other",
            scale=None, big=False, alert_name=None, junk=""):
    """A gauge plus what it is (name, kind, group) and where its reading comes from (src).
    junk: why this sensor looks unconnected or bogus ("" if it looks real). Such a sensor is off by
    default and never raises an alert."""
    g = _gauge(key, label, unit, vmin, vmax, red, scale, big)
    g.update(name=name, kind=kind, src=src, default=default and not junk, group=group,
             alert_name=alert_name or name, junk=junk)
    return g


# ---------------------------------------------------------------- sensors
def _read(path):
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return None


def _num(path, div=1):
    v = _read(path)
    try:
        return float(v) / div
    except (TypeError, ValueError):
        return None


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "x"


def _nice_ceil(x):
    """Round up to a tidy dial maximum such as 80, 100, 150 or 3500."""
    if x <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(x))
    for m in (1, 1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 10):
        if x <= m * mag * 1.0000001:
            return m * mag
    return 10 * mag


CPU_CHIPS   = ["coretemp", "k10temp", "zenpower", "cpu_thermal"]
GPU_CHIPS   = ["amdgpu", "nouveau", "radeon", "i915", "xe"]
BOARD_CHIPS = ["nct", "it87", "it86", "f71", "w83", "asus", "gigabyte", "dell_smm", "thinkpad", "hp"]

# what a hardware chip is called on screen
CHIP_NAMES = (
    ("coretemp", "CPU"), ("k10temp", "CPU"), ("zenpower", "CPU"), ("cpu_thermal", "CPU"),
    ("amdgpu", "GPU"), ("radeon", "GPU"), ("nouveau", "GPU"), ("i915", "iGPU"), ("xe", "GPU"),
    ("nvme", "NVMe"), ("drivetemp", "Disk"), ("acpitz", "ACPI"), ("pch_", "PCH"),
    ("iwlwifi", "WiFi"), ("ath", "WiFi"), ("mt79", "WiFi"), ("nct", "Board"), ("it87", "Board"),
    ("it86", "Board"), ("f71", "Board"), ("w83", "Board"), ("asus", "Board"), ("gigabyte", "Board"),
    ("dell_smm", "Dell"), ("thinkpad", "ThinkPad"), ("hp", "HP"), ("bat", "Battery"),
    ("spd5118", "DIMM"), ("jc42", "DIMM"), ("r8169", "LAN"), ("igc", "LAN"), ("ixgbe", "LAN"),
    ("corsair", "Corsair"), ("nzxt", "NZXT"), ("aquacomputer", "Aqua"), ("applesmc", "Mac"),
    ("macsmc", "Mac"),
)
# warning temperature when the chip does not say, by kind of chip
DEFAULT_TEMP_RED = {"CPU": 85, "GPU": 90, "iGPU": 90, "NVMe": 70, "Disk": 55, "Board": 60}


def _chip_short(name):
    low = name.lower()
    for prefix, nice in CHIP_NAMES:
        if low.startswith(prefix):
            return nice
    return name[:8].upper()


def _fan_label(raw, n):
    """Use the machine's own fan name if it has one (Dell, ThinkPad...), else FAN n."""
    if not raw:
        return f"FAN {n}"
    t = raw.upper().replace("PROCESSOR", "CPU").replace("MOTHERBOARD", "BOARD")
    t = t.replace("POWER SUPPLY", "PSU").replace("SYSTEM", "SYS").replace("CHASSIS", "CASE")
    t = t.replace("  ", " ").strip()
    if len(t) > 12 and t.endswith(" FAN"):
        t = t[:-4]
    return t[:12] if t else f"FAN {n}"


def _board_temp_file(path):
    """The motherboard's own temperature on a chip: a labeled system/board reading if there is one,
    else temp1 unless that is just the CPU or GPU again (Dell's dell_smm chip labels temp1 'CPU').
    None if the chip has no board temperature."""
    labels = {}
    for n in range(1, 16):
        if os.path.exists(f"{path}/temp{n}_input"):
            labels[n] = (_read(f"{path}/temp{n}_label") or "").lower()
    for n, lab in labels.items():
        if any(w in lab for w in ("systin", "motherboard", "board", "system", "ambient", "chassis", "case")):
            return f"{path}/temp{n}_input"
    if 1 in labels and labels[1] not in ("cpu", "gpu", "package id 0"):
        return f"{path}/temp1_input"
    return None


_PCI_IDS = None


def _pci_name(vendor, device):
    """Look a PCI id up in the system's pci.ids list, e.g. 0x1002/0x73bf -> 'Radeon RX 6800'."""
    global _PCI_IDS
    if _PCI_IDS is None:
        _PCI_IDS = {}
        for path in ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids"):
            try:
                with open(path, errors="replace") as f:
                    cur = None
                    for ln in f:
                        if ln.startswith("#") or not ln.strip():
                            continue
                        if not ln.startswith("\t"):
                            cur = ln[:4].lower()
                        elif not ln.startswith("\t\t") and cur:
                            _PCI_IDS[(cur, ln[1:5].lower())] = ln[7:].strip()
                break
            except OSError:
                continue
    raw = _PCI_IDS.get((vendor.lower().replace("0x", ""), device.lower().replace("0x", "")))
    if not raw:
        return None
    m = re.search(r"\[([^\]]+)\]", raw)
    return (m.group(1) if m else raw).strip()          # keep every model the id covers: do not guess one


def _temp_suspect(path, n, v, label):
    """Why a temperature reading looks unconnected or bogus, or "" if it looks real. Motherboard chips
    list sensors with nothing attached: they read 0, a fixed 114-117, or a limit of 0 with the alarm on."""
    if v is None:
        return "no reading"
    if v <= 0:
        return "reads 0 or below, so nothing is probably connected"
    if v >= 110:
        return f"reads {v:.0f} °C, which is not a real temperature"
    if "peci" in label.lower():
        return "a distance to the limit, not a temperature"
    if _num(f"{path}/temp{n}_alarm") == 1 and _num(f"{path}/temp{n}_max", 1000) == 0:
        return "its limit is 0 and its alarm is on, so nothing is probably connected"
    return ""


def _volt_suspect(path, n, v):
    if v is None or v <= 0.004:
        return "reads 0 V, so nothing is probably connected"
    if _num(f"{path}/in{n}_alarm") == 1 and _num(f"{path}/in{n}_max", 1000) == 0:
        return "its limit is 0 and its alarm is on, so nothing is probably connected"
    return ""


def _short_gpu_name(name):
    """'Tesla P100-PCIE-16GB' -> 'P100', 'NVIDIA GeForce RTX 3090' -> 'RTX 3090'."""
    skip = {"NVIDIA", "GEFORCE", "TESLA", "QUADRO", "CORPORATION", "PCIE", "SXM", "SXM2", "SXM4",
            "GPU", "LAPTOP", "OEM", "WORKSTATION", "EDITION", "GRAPHICS", "NVS"}
    out = []
    for tok in name.replace("-", " ").replace("_", " ").split():
        if tok.upper() in skip or re.fullmatch(r"\d+\s?GB", tok.upper()):
            continue
        out.append(tok)
        if re.search(r"\d", tok):
            break
    rest = name.replace("-", " ").replace("_", " ").split()
    after = rest[rest.index(out[-1]) + 1] if out and out[-1] in rest[:-1] else ""
    if after.upper() in ("TI", "SUPER", "XT"):         # "GTX 1080 Ti"
        out.append(after)
    return " ".join(out)[:12] or "NVIDIA"


# --- NVIDIA cards (read with the nvidia-smi tool that comes with the driver)
NV_CACHE_SECONDS = 3.0
NV_FIELDS = ("temperature.gpu", "power.draw", "power.limit", "fan.speed", "utilization.gpu",
             "memory.used", "memory.total", "clocks.sm")


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _nvidia_query_all(timeout=4):
    """{card number: {field: value}} for every NVIDIA card, or {} if there are none.
    Uses only the nvidia-smi tool, so it works without /proc/driver/nvidia (headless server drivers)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "--query-gpu=index,name," + ",".join(NV_FIELDS),
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    cards = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 + len(NV_FIELDS) or not parts[0].isdigit():
            continue
        vals = parts[-len(NV_FIELDS):]
        row = {f: _to_float(v) for f, v in zip(NV_FIELDS, vals)}
        row["name"] = ", ".join(parts[1:-len(NV_FIELDS)])
        cards[int(parts[0])] = row
    return cards


def _cpu_model(root="/"):
    try:
        with open(os.path.join(root, "proc/cpuinfo")) as f:
            for ln in f:
                if ln.lower().startswith("model name"):
                    return re.sub(r"\s+", " ", ln.split(":", 1)[1]).strip()
    except OSError:
        pass
    return ""


def _chips(root):
    base = os.path.join(root, "sys/class/hwmon")
    try:
        entries = sorted(os.listdir(base), key=lambda e: (len(e), e))
    except OSError:
        return []
    chips = []
    for e in entries:
        path = os.path.join(base, e)
        name = _read(path + "/name") or ""
        dev = os.path.realpath(path + "/device") if os.path.exists(path + "/device") else None
        chips.append({"name": name, "path": path, "dev": dev})
    return chips


def _find_chip(chips, prefixes):
    for pref in prefixes:
        for c in chips:
            if c["name"].startswith(pref):
                return c
    return None


def _device_title(chip):
    """The drive or card model behind a chip, when the system says (NVMe, SATA disks, GPUs)."""
    dev = chip["dev"]
    if not dev:
        return ""
    model = _read(os.path.join(dev, "model")) or _read(os.path.join(dev, "product_name"))
    if model:
        return re.sub(r"\s+", " ", model)
    vendor, device = _read(os.path.join(dev, "vendor")), _read(os.path.join(dev, "device"))
    if vendor and device:
        return _pci_name(vendor, device) or ""
    return ""


def discover(root="/", nvidia=None, cpu_model=None):
    """Find every sensor on this computer. Returns a list of sensor dicts (see _sensor).

    The usual gauges (cpu_temp, cpu_load, gpu_temp, fan1..., ram, board_temp, nv_temp, nv_power)
    keep their old names and are shown by default. Everything else found (extra fans, drives,
    other GPU readings, voltages, power, clocks...) is listed so it can be switched on.
    root is only changed by the tests, which point it at a pretend computer."""
    sysd, procd = os.path.join(root, "sys"), os.path.join(root, "proc")
    chips = _chips(root)
    if nvidia is None:
        nvidia = _nvidia_query_all(timeout=15)          # the first call can be slow while the card wakes up
    cpu_name = _cpu_model(root) if cpu_model is None else cpu_model
    found, claimed, keys = [], set(), set()

    def add(s, files=()):
        key = s["key"]
        if not key[0].isalpha():
            key = "s_" + key
        base, n = key, 2
        while key in keys:
            key, n = f"{base}_{n}", n + 1
        s["key"] = key
        keys.add(key)
        claimed.update(files)
        found.append(s)

    cpu = _find_chip(chips, CPU_CHIPS)
    gpu = _find_chip(chips, GPU_CHIPS)
    board = _find_chip(chips, BOARD_CHIPS)

    # --- the usual gauges --------------------------------------------------------
    if cpu:
        f = f"{cpu['path']}/temp1_input"
        for i in range(1, 64):
            lab = _read(f"{cpu['path']}/temp{i}_label")
            if lab and ("Package" in lab or "Tctl" in lab or "Tdie" in lab):
                f = f"{cpu['path']}/temp{i}_input"
                break
        add(_sensor("cpu_temp", "CPU TEMP", "CPU temperature", "temp", "°C", 0, 100,
                    _num_cfg("cpu_temp_alert", 80), ("file", f, 1000), True, "CPU", big=True,
                    alert_name="CPU temperature", junk=_temp_suspect(cpu["path"], f.rsplit("/temp", 1)[1].split("_")[0],
                                                                    _num(f, 1000), "")), [f])
    add(_sensor("cpu_load", "CPU LOAD", "CPU load", "load", "%", 0, 100, 90,
                ("cpu_load", os.path.join(procd, "stat")), True, "CPU", big=True))

    nv0 = nvidia.get(0)
    if gpu:
        f = f"{gpu['path']}/temp1_input"
        add(_sensor("gpu_temp", "GPU TEMP", "GPU temperature", "temp", "°C", 0, 100,
                    _num_cfg("gpu_temp_alert", 85), ("file", f, 1000), True, "GPU",
                    alert_name="GPU temperature", junk=_temp_suspect(gpu["path"], 1, _num(f, 1000), "")), [f])
    elif nv0 is not None:                       # the NVIDIA card is the display card
        add(_sensor("gpu_temp", "GPU TEMP", "GPU temperature", "temp", "°C", 0, 100,
                    _num_cfg("gpu_temp_alert", 85), ("nv", 0, "temperature.gpu"), True, "GPU",
                    alert_name="GPU temperature"))

    fan_n = 0
    if board:
        for i in range(1, 16):
            f = f"{board['path']}/fan{i}_input"
            v = _num(f)
            if v is not None and v > 0:
                fan_n += 1
                raw = _read(f"{board['path']}/fan{i}_label")
                label = _fan_label(raw, fan_n)
                top = _nice_ceil(max(2500, v * 1.3))
                add(_sensor(f"fan{fan_n}", label, f"{raw or f'Fan {fan_n}'} speed", "fan", "RPM", 0, top,
                            round(top * 0.88), ("file", f, 1), True, "Fans", scale=1000), [f])
    if gpu and os.path.exists(f"{gpu['path']}/fan1_input"):
        f = f"{gpu['path']}/fan1_input"
        add(_sensor("gpu_fan", "GPU FAN", "GPU fan speed", "fan", "RPM", 0, 3500, 3000,
                    ("file", f, 1), True, "Fans", scale=1000), [f])

    add(_sensor("ram", "RAM", "Memory used", "mem", "%", 0, 100, _num_cfg("ram_alert", 95),
                ("ram", os.path.join(procd, "meminfo")), True, "System", alert_name="Memory use"))

    f = _board_temp_file(board["path"]) if board else None
    if f:
        n = f.rsplit("/temp", 1)[1].split("_")[0]
        add(_sensor("board_temp", "BOARD", "Board temperature", "temp", "°C", 0, 80,
                    _num_cfg("board_temp_alert", 60), ("file", f, 1000), True, "Board",
                    alert_name="Board temperature",
                    junk=_temp_suspect(board["path"], n, _num(f, 1000), _read(f"{board['path']}/temp{n}_label") or "")),
            [f])

    # --- NVIDIA cards ------------------------------------------------------------
    for idx in sorted(nvidia):
        row = nvidia[idx]
        short = _short_gpu_name(row.get("name", ""))
        pre = "nv" if idx == 0 else f"nv{idx}"
        title = row.get("name") or f"NVIDIA card {idx}"
        shown_by_default = True
        if idx == 0 and not gpu:                     # already the GPU TEMP gauge above
            pass
        else:
            red = _num_cfg("nv_temp_alert", 85)
            add(_sensor(f"{pre}_temp", f"{short} TEMP", f"{title} temperature", "temp", "°C", 0, 100,
                        red, ("nv", idx, "temperature.gpu"), shown_by_default, "GPU",
                        alert_name=f"{short} temperature"))
        limit = row.get("power.limit") or row.get("power.draw") or 300
        top = int(math.ceil(limit / 50.0) * 50) or 50
        add(_sensor(f"{pre}_power", f"{short} POWER", f"{title} power draw", "power", "W", 0, top,
                    top * 0.9, ("nv", idx, "power.draw"), shown_by_default, "GPU"))
        if row.get("fan.speed") is not None:
            add(_sensor(f"{pre}_fan", f"{short} FAN", f"{title} fan", "fan", "%", 0, 100, 90,
                        ("nv", idx, "fan.speed"), False, "Fans"))
        if row.get("utilization.gpu") is not None:
            add(_sensor(f"{pre}_util", f"{short} LOAD", f"{title} load", "load", "%", 0, 100, 95,
                        ("nv", idx, "utilization.gpu"), False, "GPU"))
        if row.get("memory.total"):
            add(_sensor(f"{pre}_vram", f"{short} VRAM", f"{title} memory used", "mem", "%", 0, 100, 95,
                        ("nv_vram", idx), False, "GPU"))
        if row.get("clocks.sm") is not None:
            add(_sensor(f"{pre}_clock", f"{short} CLOCK", f"{title} core clock", "freq", "MHz", 0,
                        _nice_ceil(max(2000, row["clocks.sm"] * 1.3)),
                        _nice_ceil(max(2000, row["clocks.sm"] * 1.3)), ("nv", idx, "clocks.sm"), False, "GPU"))

    # --- everything else the hardware reports ---------------------------------------
    counts, order = {}, {}
    for c in chips:
        counts[c["name"]] = counts.get(c["name"], 0) + 1
    for c in chips:
        order[c["name"]] = order.get(c["name"], 0) + 1
        short = _chip_short(c["name"])
        model = _device_title(c)
        multi = counts[c["name"]] > 1
        suffix = os.path.basename(c["dev"] or c["path"]) if multi else ""
        tag = _slug(c["name"] + ("_" + suffix if suffix else ""))
        who = {"CPU": cpu_name or "CPU", "NVMe": model or "NVMe drive", "Disk": model or "Disk",
               "GPU": model or "GPU", "iGPU": model or "Integrated graphics"}.get(short, short)
        # a short on-screen name; drives and cards of the same kind get a number
        tagged = f"{short} {order[c['name']]}" if multi and short in ("NVMe", "Disk", "GPU") else short
        drive = short in ("NVMe", "Disk")
        path = c["path"]

        def raw_label(prefix, n):
            return _read(f"{path}/{prefix}{n}_label") or ""

        def screen_label(text):
            return text.upper()[:12]

        temps = [n for n in range(1, 40) if os.path.exists(f"{path}/temp{n}_input")]
        for n in temps:
            f = f"{path}/temp{n}_input"
            v = _num(f, 1000)
            if v is None or v <= -50 or f in claimed:
                continue
            lab = raw_label("temp", n)
            suspect = _temp_suspect(path, n, v, lab)
            composite = lab.lower() == "composite"
            plain = not lab or lab.lower() == f"temp{n}"
            limit = _num(f"{path}/temp{n}_max", 1000) or _num(f"{path}/temp{n}_crit", 1000)
            red = limit if limit and limit > 30 else DEFAULT_TEMP_RED.get(short, 80)
            top = 100 if red < 95 else _nice_ceil(red * 1.2)
            if composite or (plain and len(temps) == 1):
                label, name = tagged, f"{who} temperature"
            elif plain:
                label, name = f"{tagged} T{n}", f"{who} temperature {n}"
            else:
                label, name = f"{tagged} {lab}", f"{who} {lab} temperature"
            group = "Storage" if drive else {"GPU": "GPU", "iGPU": "GPU", "CPU": "CPU"}.get(short, "Board")
            add(_sensor(f"{tag}_{_slug(lab or f'temp{n}')}", screen_label(label), name, "temp", "°C",
                        0, top, round(red), ("file", f, 1000), drive and (composite or n == 1), group,
                        alert_name=name, junk=suspect), [f])
        for n in range(1, 40):
            f = f"{path}/fan{n}_input"
            v = _num(f)
            if v is None or f in claimed:
                continue
            lab = raw_label("fan", n)
            plain = not lab or lab.lower() == f"fan{n}"
            top = _nice_ceil(max(_num(f"{path}/fan{n}_max") or 2500, v * 1.3))
            add(_sensor(f"{tag}_{_slug(lab or f'fan{n}')}",
                        screen_label(f"{tagged} FAN{n}" if plain else f"{tagged} {lab}"),
                        f"{who} fan {n}" if plain else f"{who} {lab} fan", "fan", "RPM", 0, top,
                        round(top * 0.88), ("file", f, 1), v > 0, "Fans", scale=1000 if top >= 2000 else None),
                [f])
        for n in range(0, 40):
            f = f"{path}/in{n}_input"
            v = _num(f, 1000)
            if v is None or f in claimed:
                continue
            lab = raw_label("in", n)
            suspect = _volt_suspect(path, n, v)
            plain = not lab or lab.lower() == f"in{n}"
            top = _nice_ceil(max(v * 1.4, 1.5))
            add(_sensor(f"{tag}_{_slug(lab or f'in{n}')}", screen_label(f"{tagged} V{n}" if plain else f"{tagged} {lab}"),
                        f"{who} voltage {n}" if plain else f"{who} {lab} voltage", "volt", "V", 0, top, top,
                        ("file", f, 1000), False, "Power and voltage", junk=suspect), [f])
        for n in range(1, 10):
            for stem in ("average", "input"):
                f = f"{path}/power{n}_{stem}"
                v = _num(f, 1000000)
                if v is None or f in claimed:
                    continue
                lab = raw_label("power", n)
                plain = not lab or lab.lower() == f"power{n}"
                cap = _num(f"{path}/power{n}_cap", 1000000) or _num(f"{path}/power{n}_crit", 1000000)
                top = _nice_ceil(max(cap or 0, v * 2, 10))
                suffix_n = "" if n == 1 else str(n)
                add(_sensor(f"{tag}_{_slug(lab or 'power' + suffix_n)}",
                            screen_label(f"{tagged} POWER{suffix_n}" if plain else f"{tagged} {lab}"),
                            f"{who} power{(' ' + str(n)) if n > 1 else ''}" if plain else f"{who} {lab} power",
                            "power", "W", 0, top, top, ("file", f, 1000000), False, "Power and voltage"), [f])
                break
        for n in range(1, 10):
            f = f"{path}/freq{n}_input"
            v = _num(f, 1000000)
            if v is None or f in claimed:
                continue
            lab = raw_label("freq", n)
            plain = not lab or lab.lower() == f"freq{n}"
            top = _nice_ceil(max(v * 1.5, 500))
            add(_sensor(f"{tag}_{_slug(lab or f'freq{n}')}",
                        screen_label(f"{tagged} CLOCK{n}" if plain else f"{tagged} {lab}"),
                        f"{who} clock {n}" if plain else f"{who} {lab} clock", "freq", "MHz", 0, top, top,
                        ("file", f, 1000000), False, "GPU" if short in ("GPU", "iGPU") else "Board"))
        dev = c["dev"]
        if dev and short in ("GPU", "iGPU"):
            f = os.path.join(dev, "gpu_busy_percent")
            if _num(f) is not None:
                add(_sensor(f"{tag}_load", f"{tagged} LOAD", f"{who} load", "load", "%", 0, 100, 95,
                            ("file", f, 1), False, "GPU"))
            used, total = os.path.join(dev, "mem_info_vram_used"), os.path.join(dev, "mem_info_vram_total")
            if _num(total):
                add(_sensor(f"{tag}_vram", f"{tagged} VRAM", f"{who} memory used", "mem", "%", 0, 100, 95,
                            ("ratio", used, total), False, "GPU"))

    # --- the computer as a whole --------------------------------------------------------
    cpufreq = os.path.join(sysd, "devices/system/cpu")
    if os.path.isdir(cpufreq) and any(n.startswith("cpu") and os.path.exists(
            os.path.join(cpufreq, n, "cpufreq/scaling_cur_freq")) for n in os.listdir(cpufreq)):
        add(_sensor("cpu_clock", "CPU CLOCK", "CPU clock (average)", "freq", "MHz", 0, 6000, 6000,
                    ("cpu_freq", cpufreq), False, "CPU"))
    add(_sensor("swap", "SWAP", "Swap used", "mem", "%", 0, 100, 80, ("swap", os.path.join(procd, "meminfo")),
                False, "System"))
    add(_sensor("disk_root", "DISK /", "Disk space used (/)", "mem", "%", 0, 100, 90,
                ("disk", root if root != "/" else "/"), False, "Storage"))
    return found


class Sensors:
    """Reads the sensors that discover() found. Runs on a background thread."""

    def __init__(self, catalog):
        self.catalog = {s["key"]: s for s in catalog}
        self._last_cpu = None
        self._lock = threading.Lock()     # the deck and the Sensors window may both be reading
        self._nv = ({}, 0.0)              # last nvidia-smi answer and when it was taken

    def _cpu_load(self, path):
        line = _read(path)
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
    def _meminfo(path):
        info = {}
        try:
            with open(path) as f:
                for ln in f:
                    k, v = ln.split(":", 1)
                    info[k] = int(v.split()[0])
        except (OSError, ValueError):
            return {}
        return info

    def _ram(self, path):
        info = self._meminfo(path)
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
        return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])

    def _swap(self, path):
        info = self._meminfo(path)
        if not info.get("SwapTotal"):
            return 0.0
        return 100.0 * (1 - info.get("SwapFree", 0) / info["SwapTotal"])

    @staticmethod
    def _cpu_freq(base):
        vals = []
        try:
            for n in os.listdir(base):
                v = _num(os.path.join(base, n, "cpufreq/scaling_cur_freq"), 1000) if n.startswith("cpu") else None
                if v is not None:
                    vals.append(v)
        except OSError:
            return None
        return sum(vals) / len(vals) if vals else None

    def _nvidia(self):
        """nvidia-smi starts a whole program each time, so ask at most every few seconds."""
        data, taken = self._nv
        now = time.monotonic()
        if not data or now - taken >= max(UPDATE_SECONDS, NV_CACHE_SECONDS):
            fresh = _nvidia_query_all()
            if fresh:
                self._nv = (fresh, now)
                return fresh
        return data

    def _one(self, src, nv):
        kind = src[0]
        if kind == "file":
            return _num(src[1], src[2])
        if kind == "nv":
            return nv.get(src[1], {}).get(src[2])
        if kind == "nv_vram":
            row = nv.get(src[1], {})
            if row.get("memory.total") and row.get("memory.used") is not None:
                return 100.0 * row["memory.used"] / row["memory.total"]
            return None
        if kind == "cpu_load":
            return self._cpu_load(src[1])
        if kind == "ram":
            return self._ram(src[1])
        if kind == "swap":
            return self._swap(src[1])
        if kind == "cpu_freq":
            return self._cpu_freq(src[1])
        if kind == "ratio":
            used, total = _num(src[1]), _num(src[2])
            return 100.0 * used / total if used is not None and total else None
        if kind == "disk":
            try:
                st = os.statvfs(src[1])
                return 100.0 * (1 - st.f_bavail / st.f_blocks) if st.f_blocks else None
            except OSError:
                return None
        return None

    def read_all(self, want=None):
        """Read the wanted sensors (all by default) -> {key: value or None}."""
        keys = list(self.catalog) if want is None else [k for k in want if k in self.catalog]
        with self._lock:
            nv = self._nvidia() if any(self.catalog[k]["src"][0] in ("nv", "nv_vram") for k in keys) else {}
            out = {}
            for k in keys:
                try:
                    out[k] = self._one(self.catalog[k]["src"], nv)
                except Exception:           # one odd sensor must not stop the others
                    out[k] = None
            return out


def demo_catalog():
    """Made-up sensors for --demo: a busy machine with an extra compute card."""
    def s(key, label, name, kind, unit, vmax, red, wave, default=True, group="Other", **kw):
        return _sensor(key, label, name, kind, unit, 0, vmax, red, ("demo",) + wave, default, group, **kw)
    return [
        s("cpu_temp", "CPU TEMP", "CPU temperature", "temp", "°C", 100, 80, (7, 42, 78, 0), group="CPU", big=True),
        s("cpu_load", "CPU LOAD", "CPU load", "load", "%", 100, 90, (3, 6, 94, 1), group="CPU", big=True),
        s("gpu_temp", "GPU TEMP", "GPU temperature", "temp", "°C", 100, 85, (9, 38, 71, 2), group="GPU"),
        s("fan1", "CPU FAN", "Cpu Fan speed", "fan", "RPM", 2500, 2200, (6, 800, 1800, 0), group="Fans", scale=1000),
        s("fan2", "CASE FAN", "Case Fan speed", "fan", "RPM", 2500, 2200, (8, 700, 1500, 1), group="Fans", scale=1000),
        s("fan3", "FAN 3", "Fan 3 speed", "fan", "RPM", 2500, 2200, (5, 600, 1300, 2), group="Fans", scale=1000),
        s("gpu_fan", "GPU FAN", "GPU fan speed", "fan", "RPM", 3500, 3000, (5, 900, 2500, 2), group="Fans", scale=1000),
        s("ram", "RAM", "Memory used", "mem", "%", 100, 95, (20, 38, 66, 0), group="System"),
        s("board_temp", "BOARD", "Board temperature", "temp", "°C", 80, 60, (13, 30, 48, 0), group="Board"),
        s("nvme_composite", "NVME 1", "Samsung SSD 980 PRO 1TB Composite temperature", "temp", "°C", 100, 70,
          (17, 34, 58, 1), group="Storage"),
        s("nvme_2_composite", "NVME 2", "WD Black SN850X 2TB Composite temperature", "temp", "°C", 100, 70,
          (19, 30, 52, 2), group="Storage"),
        s("nv_temp", "P100 TEMP", "Tesla P100-PCIE-16GB temperature", "temp", "°C", 100, 85, (11, 40, 82, 0),
          group="GPU"),
        s("nv_power", "P100 POWER", "Tesla P100-PCIE-16GB power draw", "power", "W", 300, 270, (4, 60, 240, 0),
          group="GPU"),
        s("nv_util", "P100 LOAD", "Tesla P100-PCIE-16GB load", "load", "%", 100, 95, (5, 5, 99, 2), False, "GPU"),
        s("nv_vram", "P100 VRAM", "Tesla P100-PCIE-16GB memory used", "mem", "%", 100, 95, (23, 10, 85, 1), False,
          "GPU"),
        s("k10temp_tccd1", "CCD 1", "AMD Ryzen 9 5950X Tccd1 temperature", "temp", "°C", 100, 85, (8, 40, 74, 1),
          False, "CPU"),
        s("nct6798_in0", "VCORE", "Board Vcore voltage", "volt", "V", 2, 20, (10, 0.9, 1.35, 0), False,
          "Power and voltage"),
        s("swap", "SWAP", "Swap used", "mem", "%", 100, 80, (30, 0, 12, 0), False, "System"),
    ]


class DemoSensors:
    """Made-up readings that drift up and down (run with --demo)."""

    def __init__(self, catalog):
        self.catalog = {s["key"]: s for s in catalog}
        self.t0 = time.time()

    def read_all(self, want=None):
        t = time.time() - self.t0
        out = {}
        for key, s in self.catalog.items():
            if want is not None and key not in want:
                continue
            _tag, period, lo, hi, shift = s["src"]
            out[key] = lo + (hi - lo) * (0.5 + 0.5 * math.sin(t / period + shift))
        return out


class Poller:
    """Reads the sensors on a background thread so a slow tool such as
    nvidia-smi can never freeze the window."""

    def __init__(self, sensors, want, callback):
        self.sensors = sensors
        self.want = want              # function returning the set of sensors to read
        self.callback = callback      # runs on the GTK main thread
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                vals = self.sensors.read_all(self.want())
            except Exception:      # a failed read must not end the updates
                vals = None
            if vals is not None:
                GLib.idle_add(self._deliver, vals)
            self._stop.wait(UPDATE_SECONDS)

    def _deliver(self, vals):
        if not self._stop.is_set():
            self.callback(vals)
        return False


# ---------------------------------------------------------------- which sensors are on the deck
SENSORS_PATH = os.path.join(CONFIG_DIR, "sensors.json")


class Registry:
    """The sensors this computer has, and which of them are shown as gauges.
    The choice (and any changed limits) lives in ~/.config/gaugedeck/sensors.json."""

    def __init__(self, catalog, path=SENSORS_PATH):
        self.catalog = catalog
        self.by_key = {s["key"]: s for s in catalog}
        self.path = path
        self.listeners = []
        self.show = None          # None = the usual ones; or a list of sensor keys
        self.limits = {}          # {key: {"min":..., "max":..., "red":...}}
        self._reload_source = 0
        self._save_source = 0
        self.load()

    def sanitize(self, raw):
        show, limits = None, {}
        if isinstance(raw, dict):
            if isinstance(raw.get("show"), list):
                show = [k for k in raw["show"] if isinstance(k, str)]
            if isinstance(raw.get("limits"), dict):
                for k, d in raw["limits"].items():
                    if isinstance(d, dict):
                        ok = {n: float(v) for n, v in d.items()
                              if n in ("min", "max", "red") and _is_num(v)}
                        if ok:
                            limits[str(k)] = ok
        return show, limits

    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
        self.show, self.limits = self.sanitize(raw)

    def reload(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return False
        show, limits = self.sanitize(raw)
        if (show, limits) == (self.show, self.limits):
            return False
        self.show, self.limits = show, limits
        self._notify()
        return True

    def reload_soon(self):
        if not self._reload_source:
            self._reload_source = GLib.timeout_add(150, self._do_reload)

    def _do_reload(self):
        self._reload_source = 0
        self.reload()
        return False

    def save(self):
        self._save_source = 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"show": self.show, "limits": self.limits, "version": 1}, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass
        return False

    def _changed(self):
        if not self._save_source:
            self._save_source = GLib.timeout_add(300, self.save)
        self._notify()

    def _notify(self):
        for cb in list(self.listeners):
            cb()

    def connect(self, cb):
        self.listeners.append(cb)

    def disconnect(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    # --- looking things up (limits applied)
    def _merged(self, s):
        lim = self.limits.get(s["key"])
        return dict(s, **lim) if lim else s

    def get(self, key):
        s = self.by_key.get(key)
        return self._merged(s) if s else None

    def all(self):
        return [self._merged(s) for s in self.catalog]

    def enabled(self):
        if self.show is None:
            keys = [s["key"] for s in self.catalog if s["default"]]
        else:
            keys = [s["key"] for s in self.catalog if s["key"] in self.show]
        if not keys and "cpu_load" in self.by_key:
            keys = ["cpu_load"]
        return [self._merged(self.by_key[k]) for k in keys]

    def is_shown(self, key):
        if self.show is None:
            return bool(self.by_key.get(key, {}).get("default"))
        return key in self.show

    def set_shown(self, key, on):
        cur = [s["key"] for s in self.catalog if self.is_shown(s["key"])]
        if on and key not in cur:
            cur.append(key)
        elif not on:
            cur = [k for k in cur if k != key]
        self.show = cur
        self._changed()

    def use_usual(self):
        self.show = None
        self._changed()

    def set_limit(self, key, name, value):
        self.limits.setdefault(key, {})[name] = float(value)
        self._changed()

    def clear_limits(self, key):
        if self.limits.pop(key, None) is not None:
            self._changed()


# ---------------------------------------------------------------- look
COLOR_KEYS = (
    ("bg", "Background"), ("face", "Dial face"), ("rim", "Rim"),
    ("tick", "Tick marks"), ("needle", "Needle"), ("red", "Red zone"),
    ("text", "Reading"), ("dim", "Labels and numbers"),
)
DEFAULT_COLORS = {
    "bg": "#0d0d0f", "face": "#171719", "rim": "#8c8c94", "tick": "#d9d9d9",
    "needle": "#ff381f", "red": "#db1a1f", "text": "#f2f2f2", "dim": "#999a9e",
}
STYLE_OPTIONS = {
    "face":   [("circle", "Round"), ("rounded", "Rounded square"),
               ("hexagon", "Hexagon"), ("octagon", "Octagon")],
    "needle": [("line", "Line"), ("arrow", "Arrow"), ("blade", "Blade")],
    "ticks":  [("bars", "Bars"), ("dots", "Dots"), ("none", "None")],
    "hub":    [("dot", "Dot"), ("ring", "Ring"), ("none", "None")],
    "sweep":  [(180, "Half circle"), (220, "Short"), (270, "Car"), (300, "Wide")],
    "font":   [("sans", "Sans"), ("serif", "Serif"), ("mono", "Monospace")],
    "display": [("dial", "Dial"), ("ring", "Ring"), ("bar", "Bar"),
                ("digital", "Digital"), ("graph", "Graph")],
}
STYLE_CHOICES = {k: [v for v, _ in opts] for k, opts in STYLE_OPTIONS.items()}
DEFAULT_STYLE = {"face": "circle", "needle": "line", "ticks": "bars", "hub": "dot",
                 "sweep": 270, "font": "sans", "rim_width": 0.06,
                 "numbers": True, "red_zone": True, "display": "dial", "glow": 0.0,
                 "image": "", "image_dim": 0.5, "bg_image": "", "bg_dim": 0.55}
FONTS = {"sans": "DejaVu Sans", "serif": "DejaVu Serif", "mono": "DejaVu Sans Mono"}
TITLE_FONTS = {"sans": "DejaVu Sans Condensed", "serif": "DejaVu Serif", "mono": "DejaVu Sans Mono"}

PRESETS = {
    "Classic": {"colors": {}, "style": {}},
    "Neon": {
        "colors": {"bg": "#05060f", "face": "#0a0d1f", "rim": "#00e5ff", "tick": "#7df9ff",
                   "needle": "#ff2bd6", "red": "#ff2bd6", "text": "#e8ffff", "dim": "#5fb4c4"},
        "style": {"face": "hexagon", "needle": "blade", "ticks": "dots", "hub": "ring", "glow": 0.9}},
    "Ice": {
        "colors": {"bg": "#0b1320", "face": "#111d30", "rim": "#6fa8dc", "tick": "#cfe3f7",
                   "needle": "#ffd166", "red": "#ef476f", "text": "#f1f7ff", "dim": "#7f98b5"},
        "style": {"face": "rounded", "needle": "arrow", "hub": "ring", "glow": 0.35}},
    "Amber": {
        "colors": {"bg": "#120c05", "face": "#1d140a", "rim": "#b8860b", "tick": "#f2c46d",
                   "needle": "#ff8c1a", "red": "#d62d20", "text": "#ffe3b0", "dim": "#a8844a"},
        "style": {"font": "serif", "sweep": 270, "glow": 0.5}},
    "Paper": {
        "colors": {"bg": "#e7e5df", "face": "#fbfaf7", "rim": "#55555a", "tick": "#222222",
                   "needle": "#c8102e", "red": "#c8102e", "text": "#111111", "dim": "#666666"},
        "style": {"needle": "blade"}},
    "Terminal": {
        "colors": {"bg": "#000000", "face": "#031003", "rim": "#1aff5c", "tick": "#1aff5c",
                   "needle": "#b6ff00", "red": "#ff3b30", "text": "#b6ffcb", "dim": "#2ea35a"},
        "style": {"font": "mono", "face": "octagon", "ticks": "dots", "sweep": 220, "glow": 0.8}},
    "Dashboard": {
        "colors": {"bg": "#07090d", "face": "#0e131b", "rim": "#2a7fff", "tick": "#c9d6e8",
                   "needle": "#38d9ff", "red": "#ff4d5e", "text": "#f4f8ff", "dim": "#7f93ad"},
        "style": {"display": "digital", "face": "rounded", "glow": 0.6, "rim_width": 0.04}},
    "Ring Lights": {
        "colors": {"bg": "#05050a", "face": "#0b0b14", "rim": "#7c3aed", "tick": "#d8c7ff",
                   "needle": "#22d3ee", "red": "#fb2c75", "text": "#f5f3ff", "dim": "#8b82b8"},
        "style": {"display": "ring", "face": "circle", "glow": 1.0, "rim_width": 0.035}},
    "Bar Stack": {
        "colors": {"bg": "#0b0f0c", "face": "#111812", "rim": "#3f6b4a", "tick": "#cfe8d4",
                   "needle": "#7dff9a", "red": "#ff5a3c", "text": "#eaffee", "dim": "#7ea388"},
        "style": {"display": "bar", "face": "rounded", "glow": 0.5, "font": "mono", "rim_width": 0.04}},
    "Graph Wall": {
        "colors": {"bg": "#0a0b10", "face": "#10121a", "rim": "#464b66", "tick": "#cfd3e8",
                   "needle": "#ffb454", "red": "#ff4d5e", "text": "#f3f4fb", "dim": "#8b90ad"},
        "style": {"display": "graph", "face": "rounded", "glow": 0.4, "rim_width": 0.035}},
}

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_to_rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


def _rgb_to_hex(r, g, b):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(x * 255)))) for x in (r, g, b))


def _mix(a, b, t):
    return tuple(a[i] * (1 - t) + b[i] * t for i in range(3))


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clean_colors(d):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if k in DEFAULT_COLORS and isinstance(v, str) and HEX_RE.match(v):
                out[k] = v.lower()
    return out


def clean_style(d):
    out = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        if k in ("face", "needle", "ticks", "hub", "font", "display"):
            if isinstance(v, str) and v in STYLE_CHOICES[k]:
                out[k] = v
        elif k == "sweep":
            if _is_num(v) and int(v) in STYLE_CHOICES["sweep"]:
                out[k] = int(v)
        elif k == "rim_width":
            if _is_num(v):
                out[k] = max(0.02, min(0.15, float(v)))
        elif k in ("numbers", "red_zone"):
            if isinstance(v, bool):
                out[k] = v
        elif k == "glow":
            if _is_num(v):
                out[k] = max(0.0, min(1.0, float(v)))
        elif k in ("image_dim", "bg_dim"):
            if _is_num(v):
                out[k] = max(0.0, min(0.95, float(v)))
        elif k in ("image", "bg_image"):
            if isinstance(v, str) and len(v) <= 1024:
                out[k] = v.strip()
    return out


class Look:
    """The colors (as 0-1 RGB) and shape choices for one gauge."""
    __slots__ = ("c", "s", "sig")

    def __init__(self, colors, style):
        self.c = {k: _hex_to_rgb(v) for k, v in colors.items()}
        self.s = style
        self.sig = hash((tuple(sorted(colors.items())), tuple(sorted(style.items()))))   # changes when the look does


class Theme:
    """The colors and shapes, kept in ~/.config/gaugedeck/theme.json. One look for
    all gauges, plus optional changes for single gauges. Every open GaugeDeck
    window follows the file, so an edit shows up everywhere."""

    def __init__(self, path=THEME_PATH):
        self.path = path
        self.listeners = []
        self.data = self._blank()
        self._cache = {}
        self._save_source = 0
        self._reload_source = 0
        self.load()

    @staticmethod
    def _blank():
        return {"title": "", "global": {"colors": {}, "style": {}}, "gauges": {}}

    @staticmethod
    def sanitize(raw):
        data = Theme._blank()
        if not isinstance(raw, dict):
            return data
        if isinstance(raw.get("title"), str):
            data["title"] = raw["title"].strip()[:24]
        g = raw.get("global")
        if isinstance(g, dict):
            data["global"] = {"colors": clean_colors(g.get("colors")),
                              "style": clean_style(g.get("style"))}
        gauges = raw.get("gauges")
        if isinstance(gauges, dict):
            for key, e in gauges.items():
                if not isinstance(e, dict):
                    continue
                entry = {"colors": clean_colors(e.get("colors")),
                         "style": clean_style(e.get("style"))}
                if isinstance(e.get("label"), str) and e["label"].strip():
                    entry["label"] = e["label"].strip()[:14]
                if entry["colors"] or entry["style"] or "label" in entry:
                    data["gauges"][str(key)] = entry
        return data

    # --- reading and writing the file
    def load(self):
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
        self.data = self.sanitize(raw)
        self._cache = {}

    def reload(self):
        """Pick up a change made by another GaugeDeck window. Returns True if it differs."""
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return False
        new = self.sanitize(raw)
        if new == self.data:
            return False
        self.data = new
        self._cache = {}
        self._notify(external=True)
        return True

    def reload_soon(self):
        if not self._reload_source:
            self._reload_source = GLib.timeout_add(150, self._do_reload)

    def _do_reload(self):
        self._reload_source = 0
        self.reload()
        return False

    def save(self):
        self._save_source = 0
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(dict(self.data, version=1), f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass
        return False

    def _changed(self):
        self._cache = {}
        if not self._save_source:
            self._save_source = GLib.timeout_add(300, self.save)
        self._notify()

    def _notify(self, external=False):
        """Tell the windows the look changed. external: another GaugeDeck window changed it."""
        for cb in list(self.listeners):
            cb(external)

    def connect(self, cb):
        self.listeners.append(cb)

    def disconnect(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    # --- looking things up
    def _scope(self, scope):
        """The dict holding colors/style for 'all' (None) or one gauge key."""
        if scope is None:
            return self.data["global"]
        return self.data["gauges"].setdefault(scope, {"colors": {}, "style": {}})

    def look(self, key=None):
        if key in self._cache:
            return self._cache[key]
        colors, style = dict(DEFAULT_COLORS), dict(DEFAULT_STYLE)
        g = self.data["global"]
        colors.update(g["colors"])
        style.update(g["style"])
        if key is not None and key in self.data["gauges"]:
            e = self.data["gauges"][key]
            colors.update(e.get("colors", {}))
            style.update(e.get("style", {}))
        look = self._cache[key] = Look(colors, style)
        return look

    def hex_color(self, scope, name):
        look = self.look(scope)
        return _rgb_to_hex(*look.c[name])

    def label(self, key):
        return self.data["gauges"].get(key, {}).get("label", "")

    @property
    def title(self):
        return self.data["title"]

    # --- changing things
    def set(self, scope, group, name, value):
        self._scope(scope)[group][name] = value
        self._changed()

    def set_label(self, key, text):
        text = text.strip()[:14]
        e = self._scope(key)
        if text:
            e["label"] = text
        else:
            e.pop("label", None)
        self._tidy(key)
        self._changed()

    def set_title(self, text):
        self.data["title"] = text.strip()[:24]
        self._changed()

    def apply_preset(self, scope, name):
        p = PRESETS[name]
        if scope is None:
            self.data["global"] = {"colors": dict(p["colors"]), "style": dict(p["style"])}
            for key in list(self.data["gauges"]):       # one look for everything
                e = self.data["gauges"][key]
                e["colors"], e["style"] = {}, {}
                self._tidy(key)
        else:
            e = self._scope(scope)
            e["colors"], e["style"] = dict(p["colors"]), dict(p["style"])
        self._changed()

    def reset(self, scope):
        if scope is None:
            self.data = self._blank()
        elif scope in self.data["gauges"]:
            e = self.data["gauges"][scope]
            e["colors"], e["style"] = {}, {}
            self._tidy(scope)
        self._changed()

    def _tidy(self, key):
        e = self.data["gauges"].get(key)
        if e is not None and not (e.get("colors") or e.get("style") or e.get("label")):
            del self.data["gauges"][key]


# ---------------------------------------------------------------- window state
def load_state():
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d):
    if DEMO:
        return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def state_set_popped(key, on):
    s = load_state()
    lst = [k for k in s.get("popped", []) if k != key and isinstance(k, str)]
    if on:
        lst.append(key)
    s["popped"] = lst
    save_state(s)


def win_state(name):
    w = load_state().get("windows", {})
    return w.get(name, {}) if isinstance(w, dict) else {}


def save_win_state(name, **kw):
    s = load_state()
    if not isinstance(s.get("windows"), dict):
        s["windows"] = {}
    s["windows"].setdefault(name, {}).update(kw)
    save_state(s)


# ---------------------------------------------------------------- the sensor list
# GAUGEDECK_ROOT points the scanner at a pretend computer's folders (used only for testing)
CATALOG = demo_catalog() if DEMO else discover(os.environ.get("GAUGEDECK_ROOT") or "/")
REG = Registry(CATALOG)


def gauge_by_key(key):
    """Any sensor GaugeDeck knows (shown on the deck or not), with its limits applied."""
    return REG.get(key)


def enabled_gauges():
    """The sensors that are shown as gauges on the deck."""
    return REG.enabled()


# ---------------------------------------------------------------- pictures
try:
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ValueError, ImportError):
    GdkPixbuf = None


class Pictures:
    """Loads the pictures people choose (shrunk once if huge) and remembers them."""
    MAX = 1400

    def __init__(self):
        self.cache = {}

    @staticmethod
    def stamp(path):
        """When the picture file last changed (None if there is no such file)."""
        if not path:
            return None
        try:
            return os.stat(os.path.expanduser(path)).st_mtime
        except OSError:
            return None

    def get(self, path):
        if not path or GdkPixbuf is None:
            return None
        path = os.path.expanduser(path)
        try:
            stamp = os.stat(path).st_mtime
        except OSError:
            return None
        hit = self.cache.get(path)
        if hit and hit[0] == stamp:
            return hit[1]
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            big = max(pb.get_width(), pb.get_height())
            if big > self.MAX:
                k = self.MAX / float(big)
                pb = pb.scale_simple(max(1, int(pb.get_width() * k)), max(1, int(pb.get_height() * k)),
                                     GdkPixbuf.InterpType.BILINEAR)
        except GLib.Error:
            pb = None
        self.cache[path] = (stamp, pb)
        return pb


PICTURES = Pictures()


def _draw_picture(cr, pb, cx, cy, w, h):
    """Fill the w x h box centered on (cx, cy) with the picture, cropping the edges.
    Returns False (and draws nothing) if the picture can't be drawn."""
    pw, ph = pb.get_width(), pb.get_height()
    if pw <= 0 or ph <= 0:
        return False
    k = max(w / pw, h / ph)
    cr.save()
    try:
        cr.translate(cx - pw * k / 2.0, cy - ph * k / 2.0)
        cr.scale(k, k)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        return True
    except Exception:
        return False
    finally:
        cr.restore()


def _draw_background(cr, w, h, look):
    c, st = look.c, look.s
    cr.set_source_rgb(*c["bg"])
    cr.paint()
    pb = PICTURES.get(st["bg_image"])
    if pb and _draw_picture(cr, pb, w / 2.0, h / 2.0, w, h):
        cr.set_source_rgba(*c["bg"], st["bg_dim"])
        cr.paint()


# ---------------------------------------------------------------- drawing
def _centered(cr, text, x, y):
    ext = cr.text_extents(text)
    cr.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
    cr.show_text(text)


def _glow_text(cr, text, x, y, size, color, glow, anchor="center"):
    """Text that lights up: a soft halo behind crisp letters."""
    cr.set_font_size(size)
    ext = cr.text_extents(text)
    py = y - ext.height / 2 - ext.y_bearing
    px = {"center": x - ext.width / 2 - ext.x_bearing, "left": x - ext.x_bearing,
          "right": x - ext.width - ext.x_bearing}[anchor]
    if glow > 0:
        cr.new_path()
        cr.move_to(px, py)
        cr.text_path(text)
        path = cr.copy_path()
        cr.set_line_join(1)
        for mult, a in ((0.34, 0.10), (0.20, 0.18), (0.10, 0.30)):
            cr.new_path()
            cr.append_path(path)
            cr.set_line_width(size * mult)
            cr.set_source_rgba(*color, min(0.8, a * glow * 1.6))
            cr.stroke()
        cr.new_path()
    cr.set_source_rgb(*color)
    cr.move_to(px, py)
    cr.show_text(text)


def _halo(cr, color, width, glow, path=None):
    """Draw a soft glow around the current path (or the given one). The path is left as it was."""
    if glow <= 0:
        return
    if path is None:
        path = cr.copy_path()
    cr.set_line_cap(1)
    cr.set_line_join(1)
    for mult, a in ((5.0, 0.07), (3.2, 0.12), (1.9, 0.22)):
        cr.new_path()
        cr.append_path(path)
        cr.set_line_width(width * mult)
        cr.set_source_rgba(*color, min(0.8, a * glow * 1.6))
        cr.stroke()
    cr.new_path()
    cr.append_path(path)


def _face_path(cr, x, y, r, shape):
    cr.new_path()
    if shape == "circle":
        cr.arc(x, y, r, 0, 2 * math.pi)
    elif shape == "rounded":
        h, rad = r * 0.95, r * 0.28
        cr.new_sub_path()
        cr.arc(x + h - rad, y - h + rad, rad, -math.pi / 2, 0)
        cr.arc(x + h - rad, y + h - rad, rad, 0, math.pi / 2)
        cr.arc(x - h + rad, y + h - rad, rad, math.pi / 2, math.pi)
        cr.arc(x - h + rad, y - h + rad, rad, math.pi, 1.5 * math.pi)
        cr.close_path()
    else:
        sides, big, turn = (6, r * 1.06, -90) if shape == "hexagon" else (8, r * 1.02, 22.5)
        for i in range(sides):
            a = math.radians(turn + 360.0 * i / sides)
            px, py = x + big * math.cos(a), y + big * math.sin(a)
            cr.move_to(px, py) if i == 0 else cr.line_to(px, py)
        cr.close_path()


def _polygon(cr, pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.close_path()


def _round_rect(cr, x, y, w, h, rad):
    rad = min(rad, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - rad, y + rad, rad, -math.pi / 2, 0)
    cr.arc(x + w - rad, y + h - rad, rad, 0, math.pi / 2)
    cr.arc(x + rad, y + h - rad, rad, math.pi / 2, math.pi)
    cr.arc(x + rad, y + rad, rad, math.pi, 1.5 * math.pi)
    cr.close_path()


def _frame(cr, x, y, r, look, alarm, hot):
    """The face of a gauge: color, optional picture, backlight and a glowing rim."""
    c, st = look.c, look.s
    gl = st["glow"]
    _face_path(cr, x, y, r, st["face"])
    cr.set_source_rgb(*(_mix(c["face"], c["red"], 0.3) if hot else c["face"]))
    cr.fill_preserve()
    outline = cr.copy_path()
    cr.save()
    cr.clip()
    pb = PICTURES.get(st["image"])
    if pb and _draw_picture(cr, pb, x, y, 2 * r, 2 * r):
        cr.set_source_rgba(*c["face"], st["image_dim"])
        cr.paint()
    if gl > 0:                                  # backlight: the face brightens toward the rim
        grad = _radial(cr, x, y, r * 0.35, r * 1.05)
        grad.add_color_stop_rgba(0, *c["rim"], 0.0)
        grad.add_color_stop_rgba(1, *c["rim"], 0.38 * gl)
        cr.set_source(grad)
        cr.paint()
    cr.restore()
    cr.new_path()
    cr.append_path(outline)
    width = r * st["rim_width"] * (1.5 if alarm else 1.0)
    rim = c["red"] if hot else c["rim"]
    _halo(cr, rim, width, gl, outline)
    cr.set_line_width(width)
    cr.set_source_rgb(*rim)
    cr.stroke()


def _radial(cr, x, y, r0, r1):
    return cairo.RadialGradient(x, y, r0, x, y, r1)


def _fit_text(cr, text, size, max_width):
    cr.set_font_size(size)
    w = cr.text_extents(text).x_advance
    return size * max_width / w if w > max_width else size


def _fmt_num(v, g):
    """A reading as text: whole numbers for big ranges, a few decimals for small ones such as volts."""
    if v is None:
        return "--"
    span = g["max"] - g["min"]
    if span >= 20:
        return f"{int(round(v))}"
    return (f"{v:.2f}" if span < 5 else f"{v:.1f}").rstrip("0").rstrip(".") or "0"


def _value_text(g, v):
    return f"{_fmt_num(v, g)} {g['unit']}" if v is not None else "--"


def _dial_static(cr, x, y, r, g, look, label):
    """The parts of a dial that stay the same between readings: red zone, ticks, numbers, name."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red, scale = g["min"], g["max"], g["red"], g["scale"]
    sweep = st["sweep"]
    a0 = 90 + (360 - sweep) / 2.0

    def ang(val):
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
        return math.radians(a0 + sweep * t)

    if st["red_zone"] and red < vmax:
        cr.new_path()
        cr.arc(x, y, r * 0.84, ang(red), ang(vmax))
        _halo(cr, c["red"], r * 0.09, gl)
        cr.set_line_width(r * 0.09)
        cr.set_source_rgb(*c["red"])
        cr.stroke()

    cr.select_font_face(FONTS[st["font"]], 0, 1)
    for i in range(51):
        val = vmin + (vmax - vmin) * i / 50
        a = ang(val)
        major = i % 5 == 0
        col = c["red"] if st["red_zone"] and val >= red else c["tick"]
        if st["ticks"] == "bars":
            r1, r2 = r * (0.72 if major else 0.78), r * 0.88
            cr.new_path()
            cr.move_to(x + r1 * math.cos(a), y + r1 * math.sin(a))
            cr.line_to(x + r2 * math.cos(a), y + r2 * math.sin(a))
            if major:
                _halo(cr, col, r * 0.025, gl)
            cr.set_line_width(r * (0.025 if major else 0.012))
            cr.set_source_rgb(*col)
            cr.stroke()
        elif st["ticks"] == "dots":
            rd = r * (0.80 if major else 0.84)
            dx, dy, dr = x + rd * math.cos(a), y + rd * math.sin(a), r * (0.028 if major else 0.014)
            if gl > 0 and major:
                cr.set_source_rgba(*col, min(0.6, 0.3 * gl))
                cr.arc(dx, dy, dr * 2.6, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgb(*col)
            cr.arc(dx, dy, dr, 0, 2 * math.pi)
            cr.fill()
        if st["numbers"] and major and i % 10 == 0 and 0 < i < 50:
            num = val / (scale or 1)
            span = (vmax - vmin) / (scale or 1)
            s = f"{int(round(num))}" if span >= 20 else f"{num:.2f}".rstrip("0").rstrip(".")
            rt = r * 0.58
            _glow_text(cr, s, x + rt * math.cos(a), y + rt * math.sin(a), r * 0.13, c["dim"], gl * 0.6)

    _glow_text(cr, label, x, y + r * 0.30, r * 0.12, c["dim"], gl * 0.5)


def _dial_dynamic(cr, x, y, r, g, v, look, hist=None):
    """The parts of a dial that change with the reading: the number, the needle and the center cap."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    sweep = st["sweep"]
    a0 = 90 + (360 - sweep) / 2.0

    def ang(val):
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
        return math.radians(a0 + sweep * t)

    cr.select_font_face(FONTS[st["font"]], 0, 1)
    txt = _value_text(g, v)
    size = _fit_text(cr, txt, r * 0.2, r * 1.4)         # keep long readings such as "2100 RPM" inside
    _glow_text(cr, txt, x, y + r * 0.60, size,
               c["red"] if v is not None and st["red_zone"] and v >= red else c["text"], gl)

    if v is not None:
        a = ang(v)
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa, ca                      # sideways from the needle
        cr.set_source_rgb(*c["needle"])
        if st["needle"] == "line":
            cr.new_path()
            cr.move_to(x - r * 0.12 * ca, y - r * 0.12 * sa)
            cr.line_to(x + r * 0.8 * ca, y + r * 0.8 * sa)
            _halo(cr, c["needle"], r * 0.045, gl)
            cr.set_line_cap(1)
            cr.set_line_width(r * 0.045)
            cr.set_source_rgb(*c["needle"])
            cr.stroke()
            cr.set_line_cap(0)
        elif st["needle"] == "arrow":
            cr.new_path()
            cr.move_to(x - r * 0.12 * ca, y - r * 0.12 * sa)
            cr.line_to(x + r * 0.62 * ca, y + r * 0.62 * sa)
            _halo(cr, c["needle"], r * 0.03, gl)
            cr.set_line_width(r * 0.03)
            cr.set_source_rgb(*c["needle"])
            cr.stroke()
            cr.new_path()
            _polygon(cr, [(x + r * 0.58 * ca + r * 0.07 * px, y + r * 0.58 * sa + r * 0.07 * py),
                          (x + r * 0.84 * ca, y + r * 0.84 * sa),
                          (x + r * 0.58 * ca - r * 0.07 * px, y + r * 0.58 * sa - r * 0.07 * py)])
            _halo(cr, c["needle"], r * 0.03, gl)
            cr.set_source_rgb(*c["needle"])
            cr.fill()
        else:
            cr.new_path()
            _polygon(cr, [(x - r * 0.14 * ca, y - r * 0.14 * sa),
                          (x + r * 0.05 * px, y + r * 0.05 * py),
                          (x + r * 0.80 * ca, y + r * 0.80 * sa),
                          (x - r * 0.05 * px, y - r * 0.05 * py)])
            _halo(cr, c["needle"], r * 0.03, gl)
            cr.set_source_rgb(*c["needle"])
            cr.fill()

    if st["hub"] == "dot":
        cr.arc(x, y, r * 0.08, 0, 2 * math.pi)
        cr.set_source_rgb(*c["rim"])
        cr.fill()
    elif st["hub"] == "ring":
        cr.arc(x, y, r * 0.09, 0, 2 * math.pi)
        cr.set_source_rgb(*c["face"])
        cr.fill_preserve()
        cr.set_line_width(r * 0.03)
        cr.set_source_rgb(*c["rim"])
        cr.stroke()


def _ring_static(cr, x, y, r, g, look, label):
    """The unchanging part of a ring: the track, the red zone, the unit and the name."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    sweep = st["sweep"]
    a0 = 90 + (360 - sweep) / 2.0

    def ang(val):
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
        return math.radians(a0 + sweep * t)

    rad, lw = r * 0.70, r * 0.17
    cr.set_line_cap(1)
    cr.new_path()
    cr.arc(x, y, rad, ang(vmin), ang(vmax))
    cr.set_line_width(lw)
    cr.set_source_rgb(*_mix(c["face"], c["rim"], 0.28))
    cr.stroke()
    if st["red_zone"] and red < vmax:
        cr.new_path()
        cr.arc(x, y, rad + lw * 0.78, ang(red), ang(vmax))
        cr.set_line_width(lw * 0.22)
        cr.set_source_rgb(*c["red"])
        cr.stroke()
    cr.set_line_cap(0)
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    _glow_text(cr, g["unit"], x, y + r * 0.22, r * 0.13, c["dim"], gl * 0.5)
    _glow_text(cr, label, x, y + r * 0.44, _fit_text(cr, label, r * 0.12, r * 1.2), c["dim"], gl * 0.5)


def _ring_dynamic(cr, x, y, r, g, v, look, hist=None):
    """A ring that fills up, with the reading in the middle."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    sweep = st["sweep"]
    a0 = 90 + (360 - sweep) / 2.0

    def ang(val):
        t = max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))
        return math.radians(a0 + sweep * t)

    rad, lw = r * 0.70, r * 0.17
    over = v is not None and st["red_zone"] and v >= red
    if v is not None and v > vmin:
        col = c["red"] if over else c["needle"]
        cr.set_line_cap(1)
        cr.new_path()
        cr.arc(x, y, rad, ang(vmin), ang(v))
        _halo(cr, col, lw, gl)
        cr.set_line_width(lw)
        cr.set_source_rgb(*col)
        cr.stroke()
        cr.set_line_cap(0)
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    num = _fmt_num(v, g)
    _glow_text(cr, num, x, y - r * 0.08, _fit_text(cr, num, r * 0.46, r * 1.0),
               c["red"] if over else c["text"], gl)


def _bar_static(cr, x, y, r, g, look, label):
    """The unchanging part of a bar: the name, the empty track with its red zone, and the end numbers."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    _glow_text(cr, label, x, y - r * 0.58, _fit_text(cr, label, r * 0.14, r * 1.4), c["dim"], gl * 0.5)
    bx, bw, by, bh = x - r * 0.75, r * 1.5, y + r * 0.22, r * 0.22
    _round_rect(cr, bx, by, bw, bh, bh / 2)
    cr.set_source_rgb(*_mix(c["face"], c["rim"], 0.25))
    cr.fill()
    if st["red_zone"] and red < vmax:
        t = (red - vmin) / (vmax - vmin)
        cr.save()
        _round_rect(cr, bx, by, bw, bh, bh / 2)
        cr.clip()
        cr.rectangle(bx + bw * t, by, bw * (1 - t), bh)
        cr.set_source_rgba(*c["red"], 0.45)
        cr.fill()
        cr.restore()
    _glow_text(cr, f"{vmin:g}", bx, y + r * 0.66, r * 0.1, c["dim"], 0, "left")
    _glow_text(cr, f"{vmax:g}", bx + bw, y + r * 0.66, r * 0.1, c["dim"], 0, "right")


def _bar_dynamic(cr, x, y, r, g, v, look, hist=None):
    """A horizontal level meter: the reading and the filled part."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    txt = _value_text(g, v)
    over = v is not None and st["red_zone"] and v >= red
    _glow_text(cr, txt, x, y - r * 0.18, _fit_text(cr, txt, r * 0.3, r * 1.45),
               c["red"] if over else c["text"], gl)
    bx, bw, by, bh = x - r * 0.75, r * 1.5, y + r * 0.22, r * 0.22
    if v is not None:
        t = max(0.0, min(1.0, (v - vmin) / (vmax - vmin)))
        if t > 0:
            col = c["red"] if over else c["needle"]
            _round_rect(cr, bx, by, max(bw * t, bh), bh, bh / 2)
            _halo_fill(cr, col, bh * 0.6, gl)
            cr.set_source_rgb(*col)
            cr.fill()


def _halo_fill(cr, color, width, glow):
    """Glow around a filled shape (the shape stays as the current path)."""
    if glow > 0:
        _halo(cr, color, width, glow)


_SEGMENTS = 12


def _segment(cr, x, y, r, i):
    sw, gap = (r * 1.5) / _SEGMENTS, r * 0.04
    _round_rect(cr, x - r * 0.75 + i * sw + gap / 2, y + r * 0.34, sw - gap, r * 0.2, r * 0.04)


def _segment_color(c, st, g, i):
    seg_red = st["red_zone"] and (g["min"] + (g["max"] - g["min"]) * (i + 0.5) / _SEGMENTS) >= g["red"]
    return c["red"] if seg_red else c["needle"]


def _digital_static(cr, x, y, r, g, look, label):
    """The unchanging part of the digital style: the name and the row of unlit segments."""
    c, st = look.c, look.s
    gl = st["glow"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    _glow_text(cr, label, x, y - r * 0.62, _fit_text(cr, label, r * 0.14, r * 1.4), c["dim"], gl * 0.5)
    for i in range(_SEGMENTS):
        _segment(cr, x, y, r, i)
        cr.set_source_rgb(*_mix(c["face"], _segment_color(c, st, g, i), 0.16))
        cr.fill()


def _digital_dynamic(cr, x, y, r, g, v, look, hist=None):
    """A big number with the segments that are lit."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    over = v is not None and st["red_zone"] and v >= red
    col = c["red"] if over else c["text"]
    num = _fmt_num(v, g)
    big = _fit_text(cr, num, r * 0.62, r * 1.2)
    unit = g["unit"] if v is not None else ""
    cr.set_font_size(big)
    n_w = cr.text_extents(num).x_advance
    cr.set_font_size(r * 0.15)
    u_w = cr.text_extents(unit).x_advance + r * 0.06 if unit else 0
    left = x - (n_w + u_w) / 2.0
    _glow_text(cr, num, left, y - r * 0.1, big, col, gl, "left")
    if unit:
        _glow_text(cr, unit, left + n_w + r * 0.06, y + r * 0.04, r * 0.15, c["dim"], gl * 0.5, "left")
    lit = 0 if v is None else int(round(max(0.0, min(1.0, (v - vmin) / (vmax - vmin))) * _SEGMENTS))
    for i in range(lit):
        base = _segment_color(c, st, g, i)
        _segment(cr, x, y, r, i)
        _halo_fill(cr, base, r * 0.1, gl)
        cr.set_source_rgb(*base)
        cr.fill()


def _graph_box(x, y, r):
    return x - r * 0.78, x + r * 0.78, y - r * 0.38, y + r * 0.62


def _graph_static(cr, x, y, r, g, look, label):
    """The unchanging part of the graph style: the name, the grid and the red-zone line."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    _glow_text(cr, label, x - r * 0.78, y - r * 0.62, _fit_text(cr, label, r * 0.14, r * 0.72),
               c["dim"], gl * 0.5, "left")
    px0, px1, py0, py1 = _graph_box(x, y, r)
    cr.set_source_rgb(*_mix(c["face"], c["rim"], 0.25))
    cr.set_line_width(r * 0.012)
    for f in (0.0, 0.5, 1.0):
        cr.move_to(px0, py1 - (py1 - py0) * f)
        cr.line_to(px1, py1 - (py1 - py0) * f)
    cr.stroke()
    if st["red_zone"] and red < vmax:
        yr = py1 - (py1 - py0) * max(0.0, min(1.0, (red - vmin) / (vmax - vmin)))
        cr.set_source_rgba(*c["red"], 0.7)
        cr.set_dash([r * 0.05, r * 0.04])
        cr.move_to(px0, yr)
        cr.line_to(px1, yr)
        cr.stroke()
        cr.set_dash([])


def _graph_dynamic(cr, x, y, r, g, v, look, hist=None):
    """A line that shows the last minute and a half, and the current reading."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    over = v is not None and st["red_zone"] and v >= red
    txt = _value_text(g, v)
    _glow_text(cr, txt, x + r * 0.78, y - r * 0.62, _fit_text(cr, txt, r * 0.2, r * 0.72),
               c["red"] if over else c["text"], gl, "right")
    px0, px1, py0, py1 = _graph_box(x, y, r)

    def py(val):
        return py1 - (py1 - py0) * max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))

    pts = list(hist or [])[-90:]
    if len(pts) < 2:
        _glow_text(cr, "collecting…", x, y + r * 0.12, r * 0.13, c["dim"], 0)
        return
    step = (px1 - px0) / 89.0
    xs = [px1 - step * (len(pts) - 1 - i) for i in range(len(pts))]
    col = c["red"] if over else c["needle"]
    cr.new_path()
    cr.move_to(xs[0], py1)
    for xx, val in zip(xs, pts):
        cr.line_to(xx, py(val))
    cr.line_to(xs[-1], py1)
    cr.close_path()
    cr.set_source_rgba(*col, 0.18)
    cr.fill()
    cr.new_path()
    for i, (xx, val) in enumerate(zip(xs, pts)):
        cr.move_to(xx, py(val)) if i == 0 else cr.line_to(xx, py(val))
    _halo(cr, col, r * 0.035, gl)
    cr.set_line_width(r * 0.035)
    cr.set_source_rgb(*col)
    cr.stroke()


# Each gauge style is two functions: the part that never changes (drawn once into a cached image)
# and the part that changes with the reading (drawn every time).
_DISPLAYS = {"dial": (_dial_static, _dial_dynamic), "ring": (_ring_static, _ring_dynamic),
             "bar": (_bar_static, _bar_dynamic), "digital": (_digital_static, _digital_dynamic),
             "graph": (_graph_static, _graph_dynamic)}


# The face, rim, glow, picture, ticks and numbers of a gauge never change between readings, and the
# glow makes them the slow part to draw. They are drawn once into an image and reused; only the
# reading and the needle are drawn each second. Turn this off to draw everything directly.
CACHE_LAYERS = True
LAYER_MARGIN = 1.45          # room around the face for the glow, in units of the dial radius
_LAYERS = {}


def _layer(cr, r, g, look, label, alarm, hot, dpi):
    """(image, pixels per unit) of the unchanging part of a gauge, or None if it can't be cached."""
    if not CACHE_LAYERS:
        return None
    m = cr.get_matrix()
    px = math.hypot(m.xx, m.yx) * dpi
    side = int(math.ceil(2 * r * LAYER_MARGIN * px))
    if not 4 <= side <= 3000:
        return None
    st = look.s
    key = (g["key"], g["min"], g["max"], g["red"], g["scale"], g["unit"], r, round(px, 3), look.sig, label,
           alarm, hot, PICTURES.stamp(st["image"]))
    surf = _LAYERS.get(key)
    if surf is None:
        if len(_LAYERS) > 80:
            _LAYERS.clear()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, side, side)
        c2 = cairo.Context(surf)
        c2.scale(px, px)
        c2.translate(r * LAYER_MARGIN, r * LAYER_MARGIN)
        _frame(c2, 0, 0, r, look, alarm, hot)
        c2.select_font_face(FONTS[st["font"]], 0, 1)
        _DISPLAYS[st["display"]][0](c2, 0, 0, r, g, look, label)
        _LAYERS[key] = surf
    return surf


def draw_gauge(cr, x, y, r, g, v, look, label=None, alarm=False, blink=False, hist=None, dpi=1):
    """One gauge inside the square of half-size r around (x, y), in the chosen display style.
    dpi: the screen's scale factor (2 on a high-resolution screen), so cached images stay sharp."""
    hot = alarm and blink
    label = label or g["label"]
    display = look.s["display"]
    surf = _layer(cr, r, g, look, label, alarm, hot, dpi)
    if surf is not None:
        ux, uy = cr.user_to_device(x - r * LAYER_MARGIN, y - r * LAYER_MARGIN)
        cr.save()
        cr.identity_matrix()
        cr.translate(round(ux * dpi) / dpi, round(uy * dpi) / dpi)    # land on whole screen pixels
        cr.scale(1.0 / dpi, 1.0 / dpi)
        cr.set_source_surface(surf, 0, 0)
        cr.paint()
        cr.restore()
    else:
        _frame(cr, x, y, r, look, alarm, hot)
        cr.select_font_face(FONTS[look.s["font"]], 0, 1)
        _DISPLAYS[display][0](cr, x, y, r, g, look, label)
    cr.select_font_face(FONTS[look.s["font"]], 0, 1)
    _DISPLAYS[display][1](cr, x, y, r, g, v, look, hist)


BASE_W = 540


def _row_xs(n, per):
    full = {2: [150, 390], 3: [100, 270, 440]}[per]
    if n == per:
        return full
    step = full[1] - full[0]
    start = BASE_W / 2.0 - step * (n - 1) / 2.0
    return [start + step * i for i in range(n)]


def deck_layout(gauges):
    """Rows of dials: big ones two to a row, small ones three to a row.
    Returns ([(gauge, x, y, r), ...], height)."""
    placed, y, last_r = [], 0, None
    for items, per, r in (([g for g in gauges if g["big"]], 2, 110),
                          ([g for g in gauges if not g["big"]], 3, 80)):
        for i in range(0, len(items), per):
            row = items[i:i + per]
            y = 40 + r if last_r is None else y + last_r + 20 + r
            last_r = r
            for g, x in zip(row, _row_xs(len(row), per)):
                placed.append((g, x, y, r))
    height = 200 if last_r is None else y + last_r + 70
    return placed, height


def _fit(w, h, bw, bh):
    s = min(w / bw, h / bh)
    return s, (w - bw * s) / 2.0, (h - bh * s) / 2.0


def draw_deck(cr, w, h, layout, bh, values, labels, theme, alarms, blink, title, history=None, dpi=1):
    look0 = theme.look(None)
    _draw_background(cr, w, h, look0)
    s, ox, oy = _fit(w, h, BASE_W, bh)
    cr.translate(ox, oy)
    cr.scale(s, s)
    if not layout:
        cr.select_font_face(FONTS[look0.s["font"]], 0, 1)
        cr.set_font_size(22)
        cr.set_source_rgb(*look0.c["dim"])
        _centered(cr, "All gauges are in their own windows", BASE_W / 2, bh / 2)
    for g, x, y, r in layout:
        key = g["key"]
        on = key in alarms or (key + "_stop") in alarms
        draw_gauge(cr, x, y, r, g, values.get(key), theme.look(key),
                   theme.label(key) or labels.get(key), on, blink, (history or {}).get(key), dpi)
    cr.select_font_face(TITLE_FONTS[look0.s["font"]], 0, 1)
    cr.set_font_size(30)
    cr.set_source_rgb(*look0.c["red"])
    _centered(cr, (theme.title or TITLE).upper(), BASE_W / 2, bh - 35)


def draw_solo(cr, w, h, g, v, look, label, alarm, blink, hist=None, dpi=1):
    _draw_background(cr, w, h, look)
    s = min(w, h) / 230.0
    cr.translate(w / 2.0, h / 2.0)
    cr.scale(s, s)
    draw_gauge(cr, 0, 0, 100, g, v, look, label, alarm, blink, hist, dpi)


STRIP_CELL_W, STRIP_CELL_H = 100, 64


def strip_size(n, vertical):
    n = max(1, n)
    return (STRIP_CELL_W, STRIP_CELL_H * n) if vertical else (STRIP_CELL_W * n, STRIP_CELL_H)


def draw_strip(cr, w, h, gauges, values, labels, theme, alarms, blink, vertical):
    look = theme.look(None)
    c = look.c
    _draw_background(cr, w, h, look)
    bw, bh = strip_size(len(gauges), vertical)
    s, ox, oy = _fit(w, h, bw, bh)
    cr.translate(ox, oy)
    cr.scale(s, s)
    cr.select_font_face(FONTS[look.s["font"]], 0, 1)
    for i, g in enumerate(gauges):
        x0, y0 = (0, i * STRIP_CELL_H) if vertical else (i * STRIP_CELL_W, 0)
        key = g["key"]
        v = values.get(key)
        hot = key in alarms or (key + "_stop") in alarms
        over = v is not None and v >= g["red"]
        cr.new_path()
        cr.arc(x0 + 12, y0 + 12, 8, math.pi, 1.5 * math.pi)
        cr.arc(x0 + STRIP_CELL_W - 12, y0 + 12, 8, 1.5 * math.pi, 2 * math.pi)
        cr.arc(x0 + STRIP_CELL_W - 12, y0 + STRIP_CELL_H - 12, 8, 0, 0.5 * math.pi)
        cr.arc(x0 + 12, y0 + STRIP_CELL_H - 12, 8, 0.5 * math.pi, math.pi)
        cr.close_path()
        cr.set_source_rgb(*(_mix(c["face"], c["red"], 0.35) if hot and blink else c["face"]))
        cr.fill()
        cx = x0 + STRIP_CELL_W / 2.0
        cr.set_font_size(11)
        cr.set_source_rgb(*c["dim"])
        _centered(cr, theme.label(key) or labels.get(key) or g["label"], cx, y0 + 15)
        num = _fmt_num(v, g)
        unit = g["unit"] if v is not None else ""
        big, small = 24.0, 11.0
        cr.set_font_size(big)
        n_w = cr.text_extents(num).x_advance
        cr.set_font_size(small)
        u_w = cr.text_extents(unit).x_advance + 3 if unit else 0
        if n_w + u_w > STRIP_CELL_W - 14:         # shrink long readings such as "1542 RPM" to fit
            k = (STRIP_CELL_W - 14) / (n_w + u_w)
            big, small, n_w, u_w = big * k, small * k, n_w * k, u_w * k
        left = cx - (n_w + u_w) / 2.0
        cr.set_source_rgb(*(c["red"] if over and look.s["red_zone"] else c["text"]))
        cr.set_font_size(big)
        cr.move_to(left, y0 + 40)
        cr.show_text(num)
        if unit:
            cr.set_font_size(small)
            cr.set_source_rgb(*c["dim"])
            cr.move_to(left + n_w + 3, y0 + 40)
            cr.show_text(unit)
        bx, bwid, by = x0 + 12, STRIP_CELL_W - 24, y0 + STRIP_CELL_H - 15
        cr.rectangle(bx, by, bwid, 5)
        cr.set_source_rgb(*_mix(c["face"], c["rim"], 0.35))
        cr.fill()
        if v is not None:
            t = max(0.0, min(1.0, (v - g["min"]) / (g["max"] - g["min"])))
            cr.rectangle(bx, by, bwid * t, 5)
            cr.set_source_rgb(*(c["red"] if over and look.s["red_zone"] else c["needle"]))
            cr.fill()


def gauge_at(layout, px, py, w, h, bh):
    """The gauge under a click at (px, py) in window pixels, or None."""
    s, ox, oy = _fit(w, h, BASE_W, bh)
    x, y = (px - ox) / s, (py - oy) / s
    for g, gx, gy, r in layout:
        if abs(x - gx) <= r * 1.05 and abs(y - gy) <= r * 1.05:
            return g["key"]
    return None


def strip_cell_at(count, vertical, px, py, w, h):
    bw, bh = strip_size(count, vertical)
    s, ox, oy = _fit(w, h, bw, bh)
    x, y = (px - ox) / s, (py - oy) / s
    if x < 0 or y < 0 or x >= bw or y >= bh:
        return None
    i = int(y // STRIP_CELL_H) if vertical else int(x // STRIP_CELL_W)
    return i if i < count else None


# ---------------------------------------------------------------- alerts
HYSTERESIS = {"board_temp": 3}      # how far below the limit a reading must fall to count as normal; 5 otherwise


def alert_rule(g):
    """(limit, name, unit) if this gauge raises an alert, else None."""
    if g and not g.get("junk") and (g.get("kind") == "temp" or g["key"] == "ram"):
        return g["red"], g.get("alert_name") or g["label"], g["unit"]
    return None


def _plausible(g, v):
    """False for a reading that is a glitch rather than a measurement (0, -128, 255...)."""
    if g.get("kind") == "temp":
        return -40 < v < 130 and v != 0
    return True


SOUND_CMDS = [
    ["canberra-gtk-play", "-i", "alarm-clock-elapsed"],
    ["paplay", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"],
    ["pw-play", "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"],
    ["paplay", "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"],
]


def _run_detached(cmd):
    """Start a program and reap it when it ends, so no zombies pile up."""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()


def play_alarm():
    """Play an alarm sound with whatever player the system has. Never blocks."""
    if not _on("alert_sound"):
        return
    for cmd in SOUND_CMDS:
        if shutil.which(cmd[0]) and (len(cmd) < 2 or not cmd[-1].startswith("/")
                                     or os.path.exists(cmd[-1])):
            try:
                _run_detached(cmd)
                return
            except OSError:
                continue


class Alerts:
    """Watches readings; fires once when a reading stays in the red,
    and again when it's back to normal. No repeat nagging."""

    def __init__(self, notify, label_for):
        self.notify = notify                   # function(key, title, body, urgent)
        self.label_for = label_for             # function(gauge key) -> name on screen
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

    def _forget(self, key):
        self.count[key] = 0
        self.active.discard(key)

    def update(self, v, shown):
        """v: latest readings. shown: the gauges this window is responsible for."""
        if not _on("alerts"):
            return
        for k in list(self.count) + list(self.active):
            if (k[:-5] if k.endswith("_stop") else k) not in shown:
                self._forget(k)
        for key in shown:
            g = gauge_by_key(key)
            val = v.get(key)
            if g is None or val is None:
                continue
            rule = alert_rule(g)
            if rule and _plausible(g, val):
                limit, name, unit = rule
                self._check(key, val >= limit, val < limit - HYSTERESIS.get(key, 5),
                            f"{name} is critical",
                            f"{name} is {val:.0f}{unit} (alert at {limit:.0f}{unit}).",
                            f"{name} is down to {val:.0f}{unit}.")
            if g.get("kind") == "fan" and g["unit"] == "RPM" and _on("fan_stop_alert"):
                if val > 0:
                    self.fan_seen[key] = True
                if not self.fan_seen.get(key):
                    continue      # never saw it spin: probably fan-stop mode, not a failure
                label = self.label_for(key)
                self._check(key + "_stop", val == 0, val > 0,
                            f"{label} stopped",
                            f"{label} was spinning and is now at 0 RPM. Check the fan.",
                            f"{label} is spinning again ({val:.0f} RPM).")


# ---------------------------------------------------------------- dock and launchers
def _fav_settings():
    src = Gio.SettingsSchemaSource.get_default()
    if src and src.lookup("org.gnome.shell", True):
        return Gio.Settings.new("org.gnome.shell")
    return None


def is_pinned(desktop_id):
    s = _fav_settings()
    return bool(s) and desktop_id in s.get_strv("favorite-apps")


def set_pinned(desktop_id, on):
    """Add or remove an app on the GNOME / Ubuntu dock. False if there is no dock to change."""
    s = _fav_settings()
    if s is None:
        return False
    favs = list(s.get_strv("favorite-apps"))
    if on and desktop_id not in favs:
        favs.append(desktop_id)
    elif not on:
        favs = [f for f in favs if f != desktop_id]
    s.set_strv("favorite-apps", favs)
    Gio.Settings.sync()
    return True


def desktop_id_for(mode, key=None):
    if mode == "gauge":
        return f"{APP_ID}.{key}.desktop"
    if mode == "strip":
        return f"{APP_ID}.strip.desktop"
    return f"{APP_ID}.desktop"


def _apps_dir():
    return os.path.join(GLib.get_user_data_dir(), "applications")


def _icons_dir():
    return os.path.join(GLib.get_user_data_dir(), "icons", "hicolor", "scalable", "apps")


def make_icon_svg(label, colors):
    """A small dial icon with the gauge's name on it, so pinned gauges look different."""
    words = label.split(" ", 1)
    line1, line2 = xml_escape(words[0][:6]), xml_escape(words[1][:6]) if len(words) > 1 else ""
    c = colors
    texts = (f'<text x="64" y="{82 if line2 else 90}" text-anchor="middle" font-family="DejaVu Sans" '
             f'font-weight="bold" font-size="17" fill="{c["text"]}">{line1}</text>')
    if line2:
        texts += (f'<text x="64" y="101" text-anchor="middle" font-family="DejaVu Sans" '
                  f'font-weight="bold" font-size="13" fill="{c["dim"]}">{line2}</text>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128">'
            f'<circle cx="64" cy="64" r="58" fill="{c["face"]}" stroke="{c["rim"]}" stroke-width="6"/>'
            f'<path d="M 111.3 55.7 A 48 48 0 0 1 97.9 97.9" fill="none" stroke="{c["red"]}" stroke-width="7"/>'
            f'<line x1="64" y1="64" x2="87" y2="31" stroke="{c["needle"]}" stroke-width="5" stroke-linecap="round"/>'
            f'<circle cx="64" cy="64" r="6" fill="{c["rim"]}"/>' + texts + '</svg>\n')


def _exec_quote(s):
    if re.search(r'[\s"\'`$\\<>~|&;*?#()]', s):
        return '"' + re.sub(r'(["`$\\])', r"\\\1", s) + '"'
    return s


def ensure_launcher(mode, key, label, theme):
    """Write the app-menu launcher (and icon) a pinned gauge or strip needs.
    Returns the desktop file id, e.g. io.github.cyoulater.GaugeDeck.cpu_temp.desktop."""
    did = desktop_id_for(mode, key)
    base = did[:-len(".desktop")]
    look = theme.look(key if mode == "gauge" else None)
    icon = os.path.join(_icons_dir(), base + ".svg")
    os.makedirs(_icons_dir(), exist_ok=True)
    os.makedirs(_apps_dir(), exist_ok=True)
    with open(icon, "w") as f:
        f.write(make_icon_svg(label, {k: _rgb_to_hex(*v) for k, v in look.c.items()}))
    args = ["--gauge", key] if mode == "gauge" else ["--strip"]
    exec_line = " ".join([_exec_quote(sys.executable), _exec_quote(os.path.abspath(__file__))] + args)
    with open(os.path.join(_apps_dir(), did), "w") as f:
        f.write("[Desktop Entry]\nType=Application\n"
                f"Name={APP_NAME} - {label}\n"
                f"Comment=A {label.lower()} gauge from {APP_NAME}\n"
                f"Exec={exec_line}\nIcon={icon}\nTerminal=false\n"
                "Categories=System;Monitor;\n"
                f"StartupWMClass={base}\n")
    return did


def remove_launchers():
    """Take every pinned gauge off the dock and delete the launchers GaugeDeck made."""
    removed = 0
    s = _fav_settings()
    prefix = APP_ID + "."
    if s is not None:
        favs = list(s.get_strv("favorite-apps"))
        keep = [f for f in favs if not (f.startswith(prefix) and f != APP_ID + ".desktop")]
        if keep != favs:
            s.set_strv("favorite-apps", keep)
            Gio.Settings.sync()
    for d, ext in ((_apps_dir(), ".desktop"), (_icons_dir(), ".svg")):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.startswith(prefix) and n.endswith(ext) and n != APP_ID + ext:
                try:
                    os.remove(os.path.join(d, n))
                    removed += 1
                except OSError:
                    pass
    return removed


def spawn_self(*args):
    """Start another GaugeDeck window as its own program (its own dock icon)."""
    cmd = [sys.executable, os.path.abspath(__file__)] + list(args)
    if DEMO:
        cmd.append("--demo")
    try:
        _run_detached(cmd)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- other GaugeDeck programs
class PeerWatcher:
    """Tells us when another GaugeDeck program starts or stops (the deck, the strip, or a gauge
    in its own window). Each one owns a name on the session bus."""

    def __init__(self, callback):
        self.callback = callback           # callback(who, up): who is "deck", "strip" or a gauge key
        self.conn = None
        self.sub = 0
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        self.sub = self.conn.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged", "/org/freedesktop/DBus",
            APP_ID, Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE, self._changed)
        self.conn.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                       None, GLib.VariantType.new("(as)"), Gio.DBusCallFlags.NONE, -1, None, self._listed)

    @staticmethod
    def who(name):
        if name == APP_ID:
            return "deck"
        if name.startswith(APP_ID + "."):
            return name[len(APP_ID) + 1:]
        return None

    def _listed(self, conn, result):
        try:
            names = conn.call_finish(result).unpack()[0]
        except GLib.Error:
            return
        for name in names:
            who = self.who(name)
            if who:
                self.callback(who, True)

    def _changed(self, _conn, _sender, _path, _iface, _signal, params):
        name, _old, new = params.unpack()
        who = self.who(name)
        if who:
            self.callback(who, bool(new))

    def close(self):
        if self.conn and self.sub:
            self.conn.signal_unsubscribe(self.sub)
            self.sub = 0


# ---------------------------------------------------------------- windows
def _alert(parent, message, detail=""):
    """A small message box. Uses the GTK 4.10 dialog when there is one, the older one otherwise."""
    if HAS_ALERT_DIALOG:
        d = Gtk.AlertDialog()
        d.set_message(message)
        d.set_detail(detail)
        d.show(parent)
    else:
        dlg = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=message, secondary_text=detail)
        dlg.connect("response", lambda d, _r: d.destroy())
        dlg.present()


def _menu_item(label, action, target=None):
    if target is None:
        return Gio.MenuItem.new(label, action)
    item = Gio.MenuItem.new(label, None)
    item.set_action_and_target_value(action, GLib.Variant.new_string(target))
    return item


def _add_action(widget, name, callback, param=False):
    act = Gio.SimpleAction.new(name, GLib.VariantType.new("s") if param else None)
    act.connect("activate", (lambda a, p: callback(p.get_string())) if param else (lambda a, p: callback()))
    widget.add_action(act)


class BaseWindow(Gtk.ApplicationWindow):
    """What every GaugeDeck window shares: live readings from a background
    thread, alerts, blinking, redraws when the look changes, and the dock badge."""
    mode = "deck"
    badge_key = "cpu_temp"

    def __init__(self, app, title):
        super().__init__(application=app, title=title)
        self.app = app
        self.values = {}
        self.history = {}                 # the last ~90 readings of each gauge, for the Graph style
        self._snap = None                 # what is on screen, to skip redraws that change nothing
        self.blink = False
        self.test_flash = set()
        self.want = frozenset()
        self._closed = False
        self._badge = None
        self._badge_age = 0
        self.alerts = Alerts(app.notify, app.display_label)
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_draw_func(self._draw)
        self.set_child(self.area)
        self.poller = Poller(app.sensors, lambda: self.want, self._on_values)
        app.theme.connect(self._theme_changed)
        self._flash_source = GLib.timeout_add(500, self._flash)
        self.connect("close-request", self._on_close)

    # --- to be filled in by each kind of window
    def shown_keys(self):
        return set()

    def alert_keys(self):
        return self.shown_keys()

    def _draw(self, _area, cr, w, h):
        pass

    def begin(self):
        self.refresh_want()
        self.poller.start()

    def refresh_want(self):
        self.want = frozenset(self.shown_keys() | {self.badge_key})

    # --- live updates
    def _on_values(self, vals):
        if self._closed:
            return
        self.values = vals
        for key in self.shown_keys():
            if vals.get(key) is not None:
                self.history.setdefault(key, deque(maxlen=90)).append(vals[key])
        self.alerts.update(vals, self.alert_keys())
        self._update_badge(vals)
        shown = self.shown_keys()
        snap = tuple((k, _fmt_num(vals.get(k), gauge_by_key(k)) if gauge_by_key(k) else None)
                     for k in sorted(shown))
        graphs = any(self.app.theme.look(k).s["display"] == "graph" for k in shown)
        if snap != self._snap or graphs or self.alerts.active:    # skip frames that would look identical
            self._snap = snap
            self.area.queue_draw()

    def _theme_changed(self, external=False):
        if not self._closed:
            self._snap = None
            self.area.queue_draw()

    def _flash(self):
        if self._closed:
            return False
        if self.alerts.active or self.test_flash:
            self.blink = not self.blink
            self.area.queue_draw()
        elif self.blink:
            self.blink = False
            self.area.queue_draw()
        return True

    def start_test_flash(self, key, seconds=10):
        self.test_flash = {key}
        GLib.timeout_add_seconds(seconds, self._end_test_flash)

    def _end_test_flash(self):
        self.test_flash = set()
        self.area.queue_draw()
        return False

    # --- the number on this program's dock icon (Ubuntu dock and others that support it)
    def _update_badge(self, vals):
        if not _on("dock_badge"):
            return
        v = vals.get(self.badge_key)
        n = None if v is None else int(round(v))
        self._badge_age += 1
        if n != self._badge or self._badge_age >= 30:      # repeat now and then in case the dock restarted
            self._badge, self._badge_age = n, 0
            self.app.set_badge(n)

    def _on_close(self, _win):
        self._closed = True
        self.poller.stop()
        self.app.theme.disconnect(self._theme_changed)
        GLib.source_remove(self._flash_source)
        if _on("dock_badge"):
            self.app.set_badge(None)
        return False

    # --- pop-up menus
    def show_menu(self, model, x, y):
        pop = Gtk.PopoverMenu.new_from_model(model)
        pop.set_parent(self.area)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.set_has_arrow(False)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def pin_dialog(self, did, name):
        """Pin or unpin on the dock, and tell the person what happened."""
        if is_pinned(did):
            if set_pinned(did, False):
                self.app.notify("pin", f"{name} unpinned", "It was taken off the dock.", False)
            return
        if set_pinned(did, True):
            self.app.notify("pin", f"{name} pinned", "It is now on your dock.", False)
        else:
            _alert(self, "This desktop can't be pinned for you",
                   f"A launcher for \"{name}\" is now in your app menu. Find it there, right-click it and "
                   "choose Add to Favorites, Add to Panel or Pin to Dash, whichever your desktop offers.")


class DeckWindow(BaseWindow):
    """All the gauges together. Right-click one to pop it out."""
    mode = "deck"

    def __init__(self, app):
        super().__init__(app, APP_NAME)
        self.popped = set()        # gauges that are running in their own window
        self.pending = {}          # asked to pop out, not up yet
        self.area.set_tooltip_text("Right-click a gauge to move it into its own window")
        self._layout_cache = None
        placed, bh = self.layout()
        self.set_default_size(int(BASE_W * SCALE), int(bh * SCALE))

        header = Gtk.HeaderBar()
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        self.menu_button.set_create_popup_func(self._build_main_menu)
        header.pack_end(self.menu_button)
        self.set_titlebar(header)

        click = Gtk.GestureClick()
        click.set_button(3)
        click.connect("pressed", self._right_click)
        self.area.add_controller(click)

        _add_action(self, "popout", self.popout, param=True)
        _add_action(self, "customize", self.app.show_customize, param=True)
        _add_action(self, "pin", self.pin_gauge, param=True)
        _add_action(self, "recall", self.recall_all)
        _add_action(self, "strip", lambda: spawn_self("--strip"))
        _add_action(self, "pin-deck", lambda: self.pin_dialog(desktop_id_for("deck"), APP_NAME))

        _add_action(self, "sensors", self.app.show_sensors)
        self.peers = PeerWatcher(self._peer_changed)
        REG.connect(self._sensors_changed)
        self.begin()
        GLib.timeout_add(700, self._reopen_popped)

    def _peer_changed(self, who, up):
        if who not in ("deck", "strip"):
            (self._solo_up if up else self._solo_down)(who)

    def _sensors_changed(self):
        """The list of shown sensors changed (Sensors window, or another GaugeDeck program)."""
        if not self._closed:
            self._relayout()

    # --- which gauges are where
    def shown_keys(self):
        return {g["key"] for g in enabled_gauges()} - self.popped - set(self.pending)

    def layout(self):
        shown = [g for g in enabled_gauges() if g["key"] in self.shown_keys()]
        self._layout_cache = deck_layout(shown)
        return self._layout_cache

    def _relayout(self):
        old_h = self._layout_cache[1] if self._layout_cache else None
        placed, bh = self.layout()
        self.refresh_want()
        if old_h and old_h != bh and not self.is_maximized() and not self.is_fullscreen():
            w = max(self.get_width(), 200)
            self.set_default_size(w, int(w * bh / BASE_W) + 46)
        self.area.queue_draw()

    def _solo_up(self, key):
        self.pending.pop(key, None)
        self.popped.add(key)
        self._relayout()

    def _solo_down(self, key):
        self.pending.pop(key, None)
        if key in self.popped:
            self.popped.discard(key)
            self._relayout()

    def _reopen_popped(self):
        for key in load_state().get("popped", []):
            if gauge_by_key(key) and key not in self.popped and key not in self.pending:
                self.popout(key)
        return False

    def popout(self, key):
        g = gauge_by_key(key)
        if not g or key in self.popped or key in self.pending:
            return
        if not DEMO:
            ensure_launcher("gauge", key, self.app.display_label(key), self.app.theme)
        if not spawn_self("--gauge", key, "--owned"):
            _alert(self, "Couldn't open that gauge", "GaugeDeck could not start a new window.")
            return
        self.pending[key] = True
        GLib.timeout_add_seconds(10, self._pending_timeout, key)
        self._relayout()

    def _pending_timeout(self, key):
        if key in self.pending:
            del self.pending[key]
            self._relayout()
        return False

    def recall(self, key):
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        path = "/" + f"{APP_ID}.{key}".replace(".", "/")
        conn.call(f"{APP_ID}.{key}", path, "org.gtk.Actions", "Activate",
                  GLib.Variant("(sava{sv})", ("close-gauge", [], {})),
                  None, Gio.DBusCallFlags.NONE, -1, None, None)

    def recall_all(self):
        for key in list(self.popped):
            try:
                self.recall(key)
            except GLib.Error:
                pass

    def pin_gauge(self, key):
        if DEMO:
            _alert(self, "Pinning is off in demo mode", "Run GaugeDeck without --demo to pin gauges.")
            return
        did = ensure_launcher("gauge", key, self.app.display_label(key), self.app.theme)
        self.pin_dialog(did, f"{APP_NAME} - {self.app.display_label(key)}")

    # --- menus
    def _build_main_menu(self, button):
        m = Gio.Menu()
        sec = Gio.Menu()
        sec.append_item(_menu_item("Customize look…", "win.customize", ""))
        sec.append("Choose sensors…", "win.sensors")
        sec.append("Open compact strip", "win.strip")
        if self.popped:
            sec.append("Bring all gauges back", "win.recall")
        m.append_section(None, sec)
        sec2 = Gio.Menu()
        if Gio.DesktopAppInfo.new(desktop_id_for("deck")):
            sec2.append("Unpin from dock" if is_pinned(desktop_id_for("deck")) else "Pin to dock", "win.pin-deck")
        sec2.append("About GaugeDeck", "app.about")
        m.append_section(None, sec2)
        button.set_menu_model(m)

    def _right_click(self, gesture, n, x, y):
        key = gauge_at(self._layout_cache[0], x, y, self.area.get_width(), self.area.get_height(),
                       self._layout_cache[1])
        m = Gio.Menu()
        if key:
            name = self.app.display_label(key)
            m.append_item(_menu_item(f"Pop out {name} into its own window", "win.popout", key))
            m.append_item(_menu_item(f"Customize {name}…", "win.customize", key))
            if not DEMO:
                did = desktop_id_for("gauge", key)
                m.append_item(_menu_item(
                    f"Unpin {name} from dock" if is_pinned(did) else f"Pin {name} to dock", "win.pin", key))
        else:
            m.append_item(_menu_item("Customize look…", "win.customize", ""))
            if self.popped:
                m.append("Bring all gauges back", "win.recall")
        self.show_menu(m, x, y)

    def _draw(self, _area, cr, w, h):
        placed, bh = self._layout_cache
        draw_deck(cr, w, h, placed, bh, self.values, {}, self.app.theme,
                  self.alerts.active | self.test_flash, self.blink, TITLE, self.history,
                  self.area.get_scale_factor())

    def _on_close(self, win):
        self.peers.close()
        REG.disconnect(self._sensors_changed)
        return super()._on_close(win)


class OverlayWindow(BaseWindow):
    """A window with no clutter that can drop its title bar and be dragged anywhere."""

    def __init__(self, app, title, name, size):
        super().__init__(app, title)
        self.name = name
        st = win_state(name)
        self.frameless = bool(st.get("frameless", False))
        w, h = st.get("size", size) if isinstance(st.get("size"), list) and len(st["size"]) == 2 else size
        self.set_default_size(int(w), int(h))
        self.set_decorated(not self.frameless)
        drag = Gtk.GestureDrag()
        drag.set_button(1)
        drag.connect("drag-begin", self._drag_begin)
        self.area.add_controller(drag)
        click = Gtk.GestureClick()
        click.set_button(3)
        click.connect("pressed", self._right_click)
        self.area.add_controller(click)
        _add_action(self, "customize", self.app.show_customize, param=True)
        _add_action(self, "frameless", self.toggle_frameless)
        _add_action(self, "bigger", lambda: self.resize_by(1.25))
        _add_action(self, "smaller", lambda: self.resize_by(0.8))
        _add_action(self, "ontop", self.explain_on_top)

    def _drag_begin(self, gesture, x, y):
        if not self.frameless:
            return
        try:
            surface = self.get_native().get_surface()
            surface.begin_move(gesture.get_device(), 1, x, y, gesture.get_current_event_time())
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except (AttributeError, TypeError, GLib.Error):
            pass

    def toggle_frameless(self):
        self.frameless = not self.frameless
        self.set_decorated(not self.frameless)

    def resize_by(self, factor):
        w, h = max(self.get_width(), 80), max(self.get_height(), 40)
        self.set_default_size(int(w * factor), int(h * factor))

    def explain_on_top(self):
        _alert(self, "Keep this window in front",
               "Right-click the window's title bar and choose \"Always on Top\". "
               "If the title bar is hidden, turn it back on first: right-click here and choose "
               "\"Show title bar\".")

    def window_menu(self, extra):
        m = Gio.Menu()
        m.append_section(None, extra)
        sec = Gio.Menu()
        sec.append("Show title bar" if self.frameless else "Hide title bar (drag to move)", "win.frameless")
        sec.append("Bigger", "win.bigger")
        sec.append("Smaller", "win.smaller")
        sec.append("Keep in front…", "win.ontop")
        m.append_section(None, sec)
        return m

    def _right_click(self, gesture, n, x, y):
        self.show_menu(self.context_menu(x, y), x, y)

    def context_menu(self, x, y):
        return Gio.Menu()

    def remember(self):
        save_win_state(self.name, frameless=self.frameless, size=[self.get_width(), self.get_height()])


class GaugeWindow(OverlayWindow):
    """One gauge in its own window."""
    mode = "gauge"

    def __init__(self, app, key):
        self.key = key
        self.badge_key = key
        self.g = gauge_by_key(key)
        self.keep_popped = False         # True when closing only because the deck closed
        self.deck_seen = False
        super().__init__(app, f"{APP_NAME} - {app.display_label(key)}", key, (260, 260))
        _add_action(self, "pin", self.pin)
        state_set_popped(key, True)
        self.peers = PeerWatcher(self._peer) if app.owned else None
        self.begin()

    def _peer(self, who, up):
        """Opened from the deck: close together with it (and come back next time the deck opens)."""
        if who != "deck":
            return
        if up:
            self.deck_seen = True
        elif self.deck_seen and not self._closed:
            self.keep_popped = True
            self.close()

    def shown_keys(self):
        return {self.key}

    def _draw(self, _area, cr, w, h):
        draw_solo(cr, w, h, self.g, self.values.get(self.key), self.app.theme.look(self.key),
                  self.app.display_label(self.key), self.key in self.alerts.active
                  or self.key + "_stop" in self.alerts.active or self.key in self.test_flash, self.blink,
                  self.history.get(self.key), self.area.get_scale_factor())

    def context_menu(self, x, y):
        extra = Gio.Menu()
        extra.append_item(_menu_item("Customize this gauge…", "win.customize", self.key))
        did = desktop_id_for("gauge", self.key)
        if not DEMO:
            extra.append("Unpin from dock" if is_pinned(did) else "Pin to dock", "win.pin")
        extra.append("Bring back to the deck", "app.close-gauge")
        return self.window_menu(extra)

    def pin(self):
        if DEMO:
            _alert(self, "Pinning is off in demo mode", "Run GaugeDeck without --demo to pin gauges.")
            return
        name = self.app.display_label(self.key)
        did = ensure_launcher("gauge", self.key, name, self.app.theme)
        self.pin_dialog(did, f"{APP_NAME} - {name}")

    def _on_close(self, win):
        self.remember()
        if not self.keep_popped:
            state_set_popped(self.key, False)
        if self.peers:
            self.peers.close()
        return super()._on_close(win)


class StripWindow(OverlayWindow):
    """A slim bar of readings: small enough to leave on screen all the time."""
    mode = "strip"
    badge_key = "cpu_temp"

    def __init__(self, app):
        self.vertical = bool(win_state("strip").get("vertical", False))
        self.keys = self._saved_keys()
        w, h = strip_size(len(self.keys), self.vertical)
        super().__init__(app, f"{APP_NAME} strip", "strip", (w, h))
        self.peer_set = set()              # other GaugeDeck programs that already raise alerts
        self.peers = PeerWatcher(self._peer)
        _add_action(self, "toggle", self.toggle_key, param=True)
        _add_action(self, "vertical", self.toggle_vertical)
        _add_action(self, "pin", self.pin)
        self.begin()

    @staticmethod
    def _saved_keys():
        saved = win_state("strip").get("keys")
        valid = [k for k in (saved if isinstance(saved, list) else []) if gauge_by_key(k)]
        return valid or [g["key"] for g in enabled_gauges()]

    def shown_keys(self):
        return set(self.keys)

    def alert_keys(self):
        """Stay quiet where the deck or a gauge window already raises the alert."""
        if "deck" in self.peer_set:
            return set()
        return set(self.keys) - self.peer_set

    def _peer(self, who, up):
        (self.peer_set.add if up else self.peer_set.discard)(who)

    def strip_gauges(self):
        return [g for g in (gauge_by_key(k) for k in self.keys) if g]

    def _draw(self, _area, cr, w, h):
        draw_strip(cr, w, h, self.strip_gauges(), self.values, {}, self.app.theme,
                   self.alerts.active | self.test_flash, self.blink, self.vertical)

    def _resize_to_content(self):
        bw, bh = strip_size(len(self.keys), self.vertical)
        self.set_default_size(bw, bh)
        self.refresh_want()
        self.area.queue_draw()

    def toggle_key(self, key):
        if key in self.keys:
            if len(self.keys) > 1:
                self.keys.remove(key)
        else:
            self.keys = [g["key"] for g in REG.all() if g["key"] in self.keys or g["key"] == key]
        self._resize_to_content()
        self.remember()

    def toggle_vertical(self):
        self.vertical = not self.vertical
        self._resize_to_content()
        self.remember()

    def context_menu(self, x, y):
        extra = Gio.Menu()
        picks = Gio.Menu()
        for g in enabled_gauges():
            mark = "✓  " if g["key"] in self.keys else "      "
            picks.append_item(_menu_item(mark + self.app.display_label(g["key"]), "win.toggle", g["key"]))
        extra.append_submenu("Gauges shown", picks)
        extra.append("Make it a column" if not self.vertical else "Make it a row", "win.vertical")
        extra.append_item(_menu_item("Customize look…", "win.customize", ""))
        if not DEMO:
            extra.append("Unpin from dock" if is_pinned(desktop_id_for("strip")) else "Pin to dock", "win.pin")
        extra.append("Close the strip", "app.close-gauge")
        return self.window_menu(extra)

    def pin(self):
        if DEMO:
            _alert(self, "Pinning is off in demo mode", "Run GaugeDeck without --demo to pin the strip.")
            return
        did = ensure_launcher("strip", None, "STRIP", self.app.theme)
        self.pin_dialog(did, f"{APP_NAME} - STRIP")

    def remember(self):
        save_win_state("strip", frameless=self.frameless, vertical=self.vertical,
                       keys=self.keys, size=[self.get_width(), self.get_height()])

    def _on_close(self, win):
        self.remember()
        self.peers.close()
        return super()._on_close(win)


class CustomizeWindow(Gtk.Window):
    """Pick colors and shapes. Everything applies live, to all gauges or to one."""

    def __init__(self, app, parent):
        super().__init__(title="Customize GaugeDeck", transient_for=parent,
                         destroy_with_parent=True, default_width=720, default_height=620)
        self.app, self.theme = app, app.theme
        self.scope = None
        self._native = None
        self._loading = False
        self.keys = [None] + [g["key"] for g in enabled_gauges()]

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(16)
        self.set_child(root)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.set_size_request(250, -1)
        root.append(left)
        self.preview = Gtk.DrawingArea()
        self.preview.set_content_width(240)
        self.preview.set_content_height(240)
        self.preview.set_draw_func(self._draw_preview)
        left.append(self.preview)
        left.append(self._caption("Change"))
        self.scope_dd = Gtk.DropDown.new_from_strings(
            ["All gauges"] + [app.display_label(g["key"]) for g in enabled_gauges()])
        self.scope_dd.connect("notify::selected", self._scope_changed)
        left.append(self.scope_dd)
        left.append(self._caption("Ready-made looks"))
        self.preset_dd = Gtk.DropDown.new_from_strings(["Choose a look…"] + list(PRESETS))
        self.preset_dd.connect("notify::selected", self._preset_chosen)
        left.append(self.preset_dd)
        self.reset_btn = Gtk.Button(label="Reset")
        self.reset_btn.connect("clicked", lambda b: self.theme.reset(self.scope) or self._refresh())
        left.append(self.reset_btn)
        self.note = Gtk.Label(wrap=True, xalign=0)
        self.note.add_css_class("dim-label")
        left.append(self.note)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroller)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        scroller.set_child(grid)
        row = 0

        def add(label, widget):
            nonlocal row
            lab = Gtk.Label(label=label, xalign=0)
            lab.set_hexpand(True)
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            row += 1
            return lab

        self.title_entry = Gtk.Entry(max_length=24)
        self.title_entry.connect("changed", self._title_edited)
        self.title_row = add("Name under the gauges", self.title_entry)
        self.label_entry = Gtk.Entry(max_length=14)
        self.label_entry.connect("changed", self._label_edited)
        self.label_row = add("Gauge name", self.label_entry)

        grid.attach(self._caption("Colors"), 0, row, 2, 1)
        row += 1
        self.color_btns = {}
        for name, text in COLOR_KEYS:
            if HAS_COLOR_DIALOG:
                dlg = Gtk.ColorDialog()
                dlg.set_with_alpha(False)
                btn = Gtk.ColorDialogButton(dialog=dlg)
            else:                                    # GTK older than 4.10
                btn = Gtk.ColorButton()
                btn.set_use_alpha(False)
            btn.connect("notify::rgba", self._color_changed, name)
            self.color_btns[name] = btn
            add(text, btn)

        grid.attach(self._caption("Shapes"), 0, row, 2, 1)
        row += 1
        self.dds = {}
        for name, text in (("display", "Gauge style"), ("face", "Dial shape"), ("needle", "Needle"),
                           ("ticks", "Tick marks"), ("hub", "Center cap"), ("sweep", "Sweep"),
                           ("font", "Lettering")):
            dd = Gtk.DropDown.new_from_strings([label for _v, label in STYLE_OPTIONS[name]])
            dd.connect("notify::selected", self._style_dd_changed, name)
            self.dds[name] = dd
            add(text, dd)
        self.rim = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.02, 0.15, 0.01)
        self.rim.set_size_request(160, -1)
        self.rim.connect("value-changed", self._rim_changed)
        add("Rim thickness", self.rim)
        self.glow = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.05)
        self.glow.set_size_request(160, -1)
        self.glow.connect("value-changed", self._glow_changed)
        add("Glow", self.glow)
        self.sw_numbers = Gtk.Switch(halign=Gtk.Align.END)
        self.sw_numbers.connect("notify::active", self._switch_changed, "numbers")
        add("Numbers on the dial", self.sw_numbers)
        self.sw_red = Gtk.Switch(halign=Gtk.Align.END)
        self.sw_red.connect("notify::active", self._switch_changed, "red_zone")
        add("Red zone", self.sw_red)

        grid.attach(self._caption("Pictures"), 0, row, 2, 1)
        row += 1
        self.pics = {}
        for img, dim, text in (("image", "image_dim", "Picture inside the dial"),
                               ("bg_image", "bg_dim", "Background picture")):
            btn = Gtk.Button(label="Choose picture…")
            btn.connect("clicked", self._choose_picture, img)
            clear = Gtk.Button(label="Clear")
            clear.connect("clicked", self._clear_picture, img)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.append(btn)
            box.append(clear)
            add(text, box)
            dimmer = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 0.95, 0.05)
            dimmer.set_size_request(160, -1)
            dimmer.connect("value-changed", self._dim_changed, dim)
            add("   Darken it so the numbers stay clear", dimmer)
            self.pics[img] = (btn, dimmer, dim)

        app.theme.connect(self._theme_changed)
        self.connect("close-request", self._closing)
        self._refresh()

    @staticmethod
    def _caption(text):
        lab = Gtk.Label(label=text, xalign=0)
        lab.add_css_class("heading")
        return lab

    def select_scope(self, key):
        if key in self.keys:
            self.scope_dd.set_selected(self.keys.index(key))

    def _closing(self, _w):
        self.theme.disconnect(self._theme_changed)
        self.app.customize_win = None
        return False

    def _theme_changed(self, external=False):
        if external:
            self._refresh()          # someone else changed the look: show the new values
        self.preview.queue_draw()

    def _refresh(self):
        """Show the current values for the chosen gauge (or all gauges)."""
        self._loading = True
        look = self.theme.look(self.scope)
        for name, btn in self.color_btns.items():
            rgba = Gdk.RGBA()
            rgba.parse(_rgb_to_hex(*look.c[name]))
            btn.set_rgba(rgba)
        for name, dd in self.dds.items():
            dd.set_selected(STYLE_CHOICES[name].index(look.s[name]))
        self.rim.set_value(look.s["rim_width"])
        self.glow.set_value(look.s["glow"])
        disp = look.s["display"]
        for name in ("needle", "ticks", "hub"):
            self.dds[name].set_sensitive(disp == "dial")
        self.dds["sweep"].set_sensitive(disp in ("dial", "ring"))
        self.sw_numbers.set_sensitive(disp == "dial")
        for img, (btn, dimmer, dim) in self.pics.items():
            path = look.s[img]
            btn.set_label(os.path.basename(path) if path else "Choose picture…")
            btn.set_tooltip_text(path or None)
            dimmer.set_value(look.s[dim])
        self.sw_numbers.set_active(look.s["numbers"])
        self.sw_red.set_active(look.s["red_zone"])
        self.title_entry.set_text(self.theme.title or TITLE)
        if self.scope:
            self.label_entry.set_text(self.theme.label(self.scope))
            self.label_entry.set_placeholder_text(gauge_by_key(self.scope)["label"])
        self.title_row.set_visible(self.scope is None)
        self.title_entry.set_visible(self.scope is None)
        self.label_row.set_visible(self.scope is not None)
        self.label_entry.set_visible(self.scope is not None)
        self.reset_btn.set_label("Reset everything to default" if self.scope is None
                                 else "Use the look for all gauges")
        self.note.set_text("" if self.scope is None else
                           "Changes here affect only this gauge. The background shows when it is in its own window.")
        self._loading = False
        self.preview.queue_draw()

    def _scope_changed(self, dd, _p):
        self.scope = self.keys[dd.get_selected()]
        self._refresh()

    def _preset_chosen(self, dd, _p):
        i = dd.get_selected()
        if self._loading or i == 0:
            return
        self.theme.apply_preset(self.scope, list(PRESETS)[i - 1])
        self._refresh()
        dd.set_selected(0)

    def _color_changed(self, btn, _p, name):
        if self._loading:
            return
        c = btn.get_rgba()
        self.theme.set(self.scope, "colors", name, _rgb_to_hex(c.red, c.green, c.blue))

    def _style_dd_changed(self, dd, _p, name):
        if not self._loading:
            self.theme.set(self.scope, "style", name, STYLE_CHOICES[name][dd.get_selected()])
            if name == "display":
                self._refresh()

    def _glow_changed(self, scale):
        if not self._loading:
            self.theme.set(self.scope, "style", "glow", round(scale.get_value(), 2))

    def _dim_changed(self, scale, dim):
        if not self._loading:
            self.theme.set(self.scope, "style", dim, round(scale.get_value(), 2))

    def _choose_picture(self, _btn, img):
        flt = Gtk.FileFilter()
        flt.set_name("Pictures")
        flt.add_mime_type("image/*")
        if HAS_FILE_DIALOG:
            dlg = Gtk.FileDialog(title="Choose a picture")
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(flt)
            dlg.set_filters(store)
            dlg.set_default_filter(flt)
            dlg.open(self, None, self._picture_chosen, img)
        else:                                        # GTK older than 4.10
            self._native = Gtk.FileChooserNative.new("Choose a picture", self, Gtk.FileChooserAction.OPEN,
                                                     "_Open", "_Cancel")
            self._native.add_filter(flt)
            self._native.connect("response", self._picture_response, img)
            self._native.show()

    def _picture_response(self, dlg, response, img):
        path = dlg.get_file().get_path() if response == Gtk.ResponseType.ACCEPT and dlg.get_file() else None
        self._native = None
        if path:
            self.theme.set(self.scope, "style", img, path)
            self._refresh()

    def _picture_chosen(self, dlg, result, img):
        try:
            f = dlg.open_finish(result)
        except GLib.Error:
            return                                   # cancelled
        path = f.get_path() if f else None
        if path:
            self.theme.set(self.scope, "style", img, path)
            self._refresh()

    def _clear_picture(self, _btn, img):
        self.theme.set(self.scope, "style", img, "")
        self._refresh()

    def _rim_changed(self, scale):
        if not self._loading:
            self.theme.set(self.scope, "style", "rim_width", round(scale.get_value(), 3))

    def _switch_changed(self, sw, _p, name):
        if not self._loading:
            self.theme.set(self.scope, "style", name, sw.get_active())

    def _title_edited(self, entry):
        if not self._loading:
            self.theme.set_title(entry.get_text())

    def _label_edited(self, entry):
        if not self._loading and self.scope:
            self.theme.set_label(self.scope, entry.get_text())

    def _draw_preview(self, _a, cr, w, h):
        g = gauge_by_key(self.scope) or enabled_gauges()[0]
        v = g["min"] + 0.62 * (g["max"] - g["min"])
        draw_solo(cr, w, h, g, v, self.theme.look(self.scope), self.theme.label(g["key"]) or g["label"], False, False)


class SensorsWindow(Gtk.Window):
    """Every sensor GaugeDeck found on this computer: what it is, what it reads now, and a switch
    to show it as a gauge."""
    ORDER = ("CPU", "GPU", "Fans", "Storage", "Board", "Power and voltage", "System", "Other",
             "Looks unconnected")

    def __init__(self, app, parent):
        super().__init__(title="Sensors", transient_for=parent, destroy_with_parent=True,
                         default_width=720, default_height=660)
        self.app = app
        self._loading = False
        self.rows = {}                 # key -> (switch, value label)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(16)
        self.set_child(root)
        intro = Gtk.Label(xalign=0, wrap=True)
        intro.set_text(f"GaugeDeck found {len(REG.catalog)} sensors on this computer. "
                       "Switch on the ones you want as gauges.")
        root.append(intro)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroller)
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroller.set_child(lst)
        groups = {}
        for s in REG.all():
            name = "Looks unconnected" if s.get("junk") else (s["group"] if s["group"] in self.ORDER else "Other")
            groups.setdefault(name, []).append(s)
        for name in self.ORDER:
            if name not in groups:
                continue
            head = Gtk.Label(label=name, xalign=0)
            head.add_css_class("heading")
            head.set_margin_top(10)
            lst.append(head)
            for s in groups[name]:
                lst.append(self._row(s))
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        usual = Gtk.Button(label="Show the usual ones")
        usual.connect("clicked", lambda b: REG.use_usual())
        bottom.append(usual)
        root.append(bottom)
        REG.connect(self._sync)
        self.poller = Poller(app.sensors, lambda: frozenset(self.rows), self._on_values)
        self.connect("close-request", self._closing)
        self._sync()
        self.poller.start()

    @staticmethod
    def _source_text(s):
        src = s["src"]
        if src[0] == "file":
            return "/".join(src[1].split("/")[-2:])
        if src[0] in ("nv", "nv_vram"):
            return f"nvidia-smi, card {src[1]}"
        return {"cpu_load": "/proc/stat", "ram": "/proc/meminfo", "swap": "/proc/meminfo",
                "demo": "demo"}.get(src[0], src[0])

    def _row(self, s):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.connect("notify::active", self._toggled, s["key"])
        box.append(sw)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        text.set_hexpand(True)
        name = Gtk.Label(label=s["name"], xalign=0, wrap=True)
        sub = Gtk.Label(label=self._source_text(s) + (f" - {s['junk']}" if s.get("junk") else ""), xalign=0, wrap=True)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        text.append(name)
        text.append(sub)
        box.append(text)
        val = Gtk.Label(label="--", xalign=1, width_chars=10)
        box.append(val)
        scale = Gtk.MenuButton(label="Scale")
        scale.set_create_popup_func(lambda b, k=s["key"]: self._scale_popover(b, k))
        box.append(scale)
        self.rows[s["key"]] = (sw, val)
        return box

    def _scale_popover(self, button, key):
        g = REG.get(key)
        pop = Gtk.Popover()
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(grid, f"set_margin_{side}")(12)
        fine = g["max"] - g["min"] < 20
        for row, (label, name) in enumerate((("Top of the dial", "max"), ("Red zone starts at", "red"))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(0, 1000000, 0.1 if fine else 1)
            spin.set_digits(2 if fine else 0)
            spin.set_value(g[name])
            spin.connect("value-changed", lambda sp, n=name: REG.set_limit(key, n, sp.get_value()))
            grid.attach(spin, 1, row, 1, 1)
        reset = Gtk.Button(label="Back to automatic")
        reset.connect("clicked", lambda b: (REG.clear_limits(key), pop.popdown()))
        grid.attach(reset, 0, 2, 2, 1)
        pop.set_child(grid)
        button.set_popover(pop)

    def _toggled(self, sw, _p, key):
        if not self._loading:
            REG.set_shown(key, sw.get_active())

    def _sync(self):
        self._loading = True
        for key, (sw, _val) in self.rows.items():
            sw.set_active(REG.is_shown(key))
        self._loading = False

    def _on_values(self, vals):
        for key, (_sw, label) in self.rows.items():
            g = REG.get(key)
            v = vals.get(key)
            label.set_text(f"{_fmt_num(v, g)} {g['unit']}" if v is not None else "--")

    def _closing(self, _w):
        self.poller.stop()
        REG.disconnect(self._sync)
        self.app.sensors_win = None
        return False


class GaugeApp(Gtk.Application):
    def __init__(self, mode="deck", key=None, test_alert=False, owned=False):
        # one program per window kind: clicking its icon again just brings it forward
        app_id = {"deck": APP_ID, "gauge": f"{APP_ID}.{key}", "strip": f"{APP_ID}.strip"}[mode]
        super().__init__(application_id=app_id)
        self.mode, self.key, self.test_alert, self.owned = mode, key, test_alert, owned
        self.theme = Theme()
        self.sensors = DemoSensors(CATALOG) if DEMO else Sensors(CATALOG)
        self.customize_win = None
        self.sensors_win = None
        self._monitor = None
        self._sensors_monitor = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        _add_action(self, "customize", self.show_customize, param=True)
        _add_action(self, "close-gauge", self.close_windows)
        _add_action(self, "about", self.show_about)
        _add_action(self, "sensors", self.show_sensors)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            self._monitor = Gio.File.new_for_path(THEME_PATH).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *a: self.theme.reload_soon())
            self._sensors_monitor = Gio.File.new_for_path(SENSORS_PATH).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._sensors_monitor.connect("changed", lambda *a: REG.reload_soon())
        except (OSError, GLib.Error):
            pass

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            if self.mode == "gauge":
                win = GaugeWindow(self, self.key)
            elif self.mode == "strip":
                win = StripWindow(self)
            else:
                win = DeckWindow(self)
        win.present()
        if self.test_alert:
            self.test_alert = False
            self.notify("test", "GaugeDeck test alert", "Alerts are working. This is only a test.", True)
            play_alarm()
            shown = win.shown_keys()
            win.start_test_flash("cpu_temp" if "cpu_temp" in shown else sorted(shown)[0], 10)

    # --- helpers the windows use
    def display_label(self, key):
        g = gauge_by_key(key)
        return self.theme.label(key) or (g["label"] if g else key.upper())

    def notify(self, key, title, body, urgent):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_priority(Gio.NotificationPriority.URGENT if urgent else Gio.NotificationPriority.NORMAL)
        self.send_notification("gaugedeck-" + key, n)

    def set_badge(self, count):
        """Show a number on this program's dock icon (Unity LauncherEntry, used by Ubuntu's dock)."""
        conn = self.get_dbus_connection()
        if conn is None:
            return
        did = desktop_id_for(self.mode, self.key)
        props = {"count": GLib.Variant("x", int(count or 0)),
                 "count-visible": GLib.Variant("b", count is not None)}
        try:
            conn.emit_signal(None, "/com/canonical/unity/launcherentry/%d" % zlib.crc32(did.encode()),
                             "com.canonical.Unity.LauncherEntry", "Update",
                             GLib.Variant("(sa{sv})", ("application://" + did, props)))
        except GLib.Error:
            pass

    def close_windows(self):
        """Close every window of this program the normal way (so a gauge records that it is back in the deck)."""
        for w in list(self.get_windows()):
            w.close()

    def show_customize(self, scope=""):
        if self.customize_win is None:
            self.customize_win = CustomizeWindow(self, self.props.active_window)
        self.customize_win.select_scope(scope or None)
        self.customize_win.present()

    def show_sensors(self):
        if self.sensors_win is None:
            self.sensors_win = SensorsWindow(self, self.props.active_window)
        self.sensors_win.present()

    def show_about(self):
        about = Gtk.AboutDialog(program_name=APP_NAME, version=VERSION, logo_icon_name=APP_ID,
                                comments="Car-style gauges for your PC's temps, fans and load.",
                                website="https://github.com/Cyoulater/gaugedeck",
                                license_type=Gtk.License.GPL_3_0, authors=["Miguel Ycaza"],
                                transient_for=self.props.active_window, modal=True)
        about.present()


def print_sensors():
    """List every sensor GaugeDeck found, with its current reading."""
    sensors = DemoSensors(CATALOG) if DEMO else Sensors(CATALOG)
    sensors.read_all()
    time.sleep(0.5)                       # CPU load needs two samples
    vals = sensors.read_all()
    shown = {g["key"] for g in enabled_gauges()}
    print(f"{'KEY':28} {'SHOWN':5} {'READING':>12}  NAME  (where it comes from)")
    for s in CATALOG:
        v = vals.get(s["key"])
        reading = f"{_fmt_num(v, s)} {s['unit']}" if v is not None else "--"
        src = SensorsWindow._source_text(s)
        note = f"  [looks unconnected: {s['junk']}]" if s.get("junk") else ""
        print(f"{s['key']:28} {'yes' if s['key'] in shown else '':5} {reading:>12}  {s['name']}  ({src}){note}")
    return 0


def main(argv):
    mode, key, test_alert, owned = "deck", None, False, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--check":
            print(f"GaugeDeck {VERSION}")
            print(f"{len(CATALOG)} sensors found, {len(enabled_gauges())} shown by default.")
            nv = sorted({s["name"].split(" temperature")[0] for s in CATALOG if s["key"].startswith("nv")
                         and s["kind"] == "temp"})
            print("NVIDIA cards:", ", ".join(nv) if nv else "not found")
            print("Run  gaugedeck --sensors  to list every sensor.")
            return 0
        if a == "--sensors":
            return print_sensors()
        if a == "--version":
            print(f"GaugeDeck {VERSION}")
            return 0
        if a == "--remove-launchers":
            print(f"Removed {remove_launchers()} pinned gauge launcher file(s).")
            return 0
        if a == "--test-alert":
            test_alert = True
        elif a == "--strip":
            mode = "strip"
        elif a == "--owned":
            owned = True
        elif a == "--gauge" and i + 1 < len(argv):
            mode, key = "gauge", argv[i + 1]
            i += 1
            if not gauge_by_key(key):
                return "GaugeDeck: no gauge called '%s'. Run  gaugedeck --sensors  to see the choices." % key
        elif a not in ("--demo",):
            return "GaugeDeck: unknown option '%s'" % a
        i += 1
    return GaugeApp(mode, key, test_alert, owned).run([argv[0]])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
