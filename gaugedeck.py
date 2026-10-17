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

Sensors are found at startup and read straight from /sys/class/hwmon,
/sys/class/powercap, /proc and nvidia-smi; `gaugedeck --check` lists them. Reading never
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

import cairo
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
try:
    gi.require_foreign("cairo")
except ImportError:
    sys.exit("GaugeDeck needs the python3-gi-cairo package.\n"
             "Install it with:  sudo apt install python3-gi-cairo")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

APP_NAME = "GaugeDeck"
VERSION = "1.3.0"
APP_ID = "io.github.cyoulater.GaugeDeck"
CONFIG_DIR = os.environ.get("GAUGEDECK_CONFIG_DIR") or os.path.expanduser("~/.config/gaugedeck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config")
THEME_PATH = os.path.join(CONFIG_DIR, "theme.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
DEMO = "--demo" in sys.argv       # made-up readings, for trying the look on any machine
UPDATE_SECONDS = 1
HAS_NEW_DIALOGS = hasattr(Gtk, "ColorDialogButton") and hasattr(Gtk, "AlertDialog")  # GTK 4.10+


def load_config():
    cfg = {"title": "GAUGEDECK", "scale": "0.6",
           "alerts": "on", "alert_sound": "on", "alert_seconds": "5",
           "cpu_temp_alert": "80", "gpu_temp_alert": "85",
           "board_temp_alert": "60", "ram_alert": "95", "fan_stop_alert": "on",
           "nv_temp_alert": "85", "dock_badge": "on"}
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


def _gauge(key, label, unit, vmin, vmax, red, scale=None, big=False, **info):
    g = {"key": key, "label": label, "unit": unit, "min": vmin, "max": vmax,
         "red": red, "scale": scale, "big": big}
    g.update(info)
    return g


# Every sensor GaugeDeck found. GAUGES is the part of it shown in the deck and
# the strip; any other one can still be opened with --gauge KEY.
ALL_GAUGES = []
GAUGES = []


def gauge_by_key(key):
    for g in ALL_GAUGES:
        if g["key"] == key:
            return g
    return None


# ---------------------------------------------------------------- sensors
# Discovery: every temperature, fan, power and load sensor the machine has,
# from /sys/class/hwmon, /sys/class/powercap, /proc and nvidia-smi, named
# after the real device. GAUGEDECK_ROOT points it at a fake machine in the tests.
ROOT = os.environ.get("GAUGEDECK_ROOT", "")


def _p(path):
    return ROOT + path


def _read(path):
    try:
        with open(path, errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def _num(path, div=1):
    v = _read(path)
    try:
        return float(v) / div
    except (TypeError, ValueError):
        return None


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s or "x"


CPU_CHIPS    = ["coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal"]
GPU_CHIPS    = ["amdgpu", "nouveau", "radeon", "i915", "xe"]
DRIVE_CHIPS  = ["nvme", "drivetemp"]
BOARD_CHIPS  = ["nct", "it87", "it86", "f71", "w83", "asus", "gigabyte", "dell_smm", "thinkpad", "hp",
                "applesmc", "lenovo", "msi"]
LAPTOP_CHIPS = ["dell_smm", "thinkpad", "hp", "applesmc", "lenovo", "asus"]
OTHER_CHIPS  = [("spd5118", "memory", "DIMM"), ("jc42", "memory", "DIMM"), ("pch_", "chipset", "CHIPSET"),
                ("iwlwifi", "wifi", "WIFI"), ("mt7", "wifi", "WIFI"), ("ath", "wifi", "WIFI"),
                ("rtw", "wifi", "WIFI"), ("acpitz", "acpi", "ACPI"), ("BAT", "battery", "BATTERY"),
                ("r8169", "network", "NIC"), ("igc", "network", "NIC"), ("ucsi", "usb", "USB-C")]
CHIP_MAKERS  = [("nct", "Nuvoton"), ("it8", "ITE"), ("f71", "Fintek"), ("w83", "Winbond"),
                ("dell_smm", "Dell"), ("thinkpad", "ThinkPad"), ("asus", "ASUS"), ("gigabyte", "Gigabyte"),
                ("hp", "HP"), ("applesmc", "Apple SMC"), ("k10temp", "AMD"), ("zenpower", "AMD"),
                ("coretemp", "Intel"), ("spd5118", "DDR5 SPD"), ("jc42", "DDR4 SPD"), ("iwlwifi", "Intel Wi-Fi"),
                ("acpitz", "ACPI thermal zone"), ("pch_", "Intel PCH"), ("mt7", "MediaTek Wi-Fi"),
                ("ath", "Qualcomm Atheros Wi-Fi"), ("rtw", "Realtek Wi-Fi"), ("r8169", "Realtek network"),
                ("igc", "Intel network"), ("ucsi", "USB-C"), ("lenovo", "Lenovo"), ("msi", "MSI")]
VENDORS      = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA", "0x1a03": "ASPEED", "0x102b": "Matrox"}
PCI_IDS      = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
PCI_ADDR     = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$")
CHANNEL      = re.compile(r"^(temp|fan|power)(\d+)_(input|average)$")
GPU_NOISE    = {"NVIDIA", "AMD", "ATI", "INTEL", "RADEON", "GEFORCE", "TESLA", "QUADRO", "GRAPHICS",
                "CORPORATION", "PCIE", "(R)", "(TM)"}
DRIVE_NOISE  = {"SAMSUNG", "SSD", "WDC", "WD", "CRUCIAL", "KINGSTON", "INTEL", "SEAGATE", "TOSHIBA",
                "NVME", "SK", "HYNIX", "SABRENT", "ADATA", "TEAM", "HDD", "M.2", "WESTERN", "DIGITAL"}
NV_FIELDS    = ("temperature.gpu", "power.draw", "fan.speed", "utilization.gpu")


def _hwmons():
    base = _p("/sys/class/hwmon")
    try:
        entries = sorted(os.listdir(base), key=_natural)
    except OSError:
        return []
    return [(_read(os.path.join(base, e, "name")) or "", os.path.join(base, e)) for e in entries]


def _pci_name(vendor, device):
    """The device's name from the pci.ids list (the one lspci uses), or None."""
    v, d = (vendor or "").lower()[-4:], (device or "").lower()[-4:]
    if len(v) != 4 or len(d) != 4:
        return None
    for path in PCI_IDS:
        try:
            with open(_p(path), errors="replace") as f:
                in_vendor = False
                for ln in f:
                    if ln.startswith("#") or not ln.strip():
                        continue
                    if not ln.startswith("\t"):
                        if ln.startswith("C "):
                            break
                        in_vendor = ln[:4].lower() == v
                    elif in_vendor and not ln.startswith("\t\t") and ln[1:5].lower() == d:
                        return ln[5:].strip()
        except OSError:
            continue
    return None


def _pci_dir(addr):
    return _p(f"/sys/bus/pci/devices/{addr}")


def _short_gpu(name):
    """'Navi 23 [Radeon RX 6600/6600 XT]' -> 'RX 6600', 'Tesla P100-PCIE-16GB' -> 'P100'."""
    m = re.search(r"\[([^\]]+)\]", name or "")
    s = (m.group(1) if m else name or "").split("/")[0]
    s = re.sub(r"\((R|TM)\)", " ", s, flags=re.I).replace("-", " ")
    words = [w for w in s.split() if w.upper() not in GPU_NOISE
             and not re.fullmatch(r"\d+GB|SXM\d*|PCIE|SMX\d*", w, re.I)]
    return " ".join(words)[:12].strip() or None


def _short_drive(model):
    """'Samsung SSD 980 PRO 1TB' -> '980 PRO'."""
    words = [w for w in (model or "").replace("_", " ").split() if w.upper() not in DRIVE_NOISE
             and not re.fullmatch(r"\d+(\.\d+)?(GB|TB|G|T)", w, re.I)]
    return " ".join(words[:2])[:12].strip() or None


def _cpu_name():
    info = {}
    try:
        with open(_p("/proc/cpuinfo"), errors="replace") as f:
            for ln in f:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    info.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    name = info.get("model name") or info.get("Model") or info.get("Hardware") \
        or (_read(_p("/sys/firmware/devicetree/base/model")) or "").strip("\x00")
    name = re.sub(r"\((R|TM)\)|\bCPU\b|@.*$|\b\d+-Core Processor\b|\bProcessor\b", " ", name or "")
    return " ".join(name.split()) or "CPU"


def _board_name():
    vendor = _read(_p("/sys/class/dmi/id/board_vendor")) or ""
    board = _read(_p("/sys/class/dmi/id/board_name")) or ""
    vendor = re.sub(r"\b(COMPUTER|INC|CO|LTD|CORPORATION|TECHNOLOGY)\b\.?|,", "", vendor, flags=re.I)
    return " ".join((vendor + " " + board).split())


def _chip_name(chip):
    for pref, maker in CHIP_MAKERS:
        if chip.startswith(pref):
            return f"{maker} {chip.upper()}" if pref in ("nct", "it8", "f71", "w83") else maker
    return chip


def _channels(path):
    """{('temp', 1): (file, label), ...} for every reading this hwmon chip has."""
    found = {}
    try:
        files = sorted(os.listdir(path), key=_natural)
    except OSError:
        return found
    for fn in files:
        m = CHANNEL.match(fn)
        if not m:
            continue
        kind, n, how = m.group(1), int(m.group(2)), m.group(3)
        if (kind, n) in found and how == "average":
            continue                        # prefer power1_input over power1_average
        found[(kind, n)] = (os.path.join(path, fn), _read(os.path.join(path, f"{kind}{n}_label")))
    return dict(sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1])))


def _valid_temp(v):
    return v is not None and -30 < v < 126


def _round_up(v, step=50):
    return int(math.ceil(max(v, step) / float(step)) * step)


class Finder:
    """Walks the machine once and makes a gauge for every sensor it finds."""

    def __init__(self):
        self.out = []
        self.keys = set()

    def key(self, want):
        key, n = want, 2
        while key in self.keys:
            key, n = f"{want}_{n}", n + 1
        self.keys.add(key)
        return key

    def add(self, key, label, kind, unit, vmin, vmax, red, src, device, name, where,
            on=False, group=9, alert=None, scale=None, big=False, hyst=5):
        self.out.append(_gauge(self.key(key), label, unit, vmin, vmax, red, scale=scale, big=big,
                               kind=kind, src=src, device=device, name=name, where=where,
                               on=on, group=group, alert=alert, hyst=hyst))

    def temp(self, key, label, f, device, name, limit, on=False, group=9, big=False, vmax=100, hyst=5):
        self.add(key, label, "temp", "°C", 0, vmax, limit, ("file", f, 1000), device, name, f,
                 on=on, group=group, alert=limit, big=big, hyst=hyst)

    # --- the parts of the machine
    def run(self):
        chips = []
        for chip, path in _hwmons():
            dev = os.path.realpath(path + "/device") if os.path.exists(path + "/device") else ""
            chips.append((chip, path, dev, _channels(path)))
        self.cpus([c for c in chips if any(c[0].startswith(p) for p in CPU_CHIPS)])
        self.gpus([c for c in chips if any(c[0].startswith(p) for p in GPU_CHIPS)])
        self.ram()
        self.boards([c for c in chips if any(c[0].startswith(p) for p in BOARD_CHIPS)])
        self.drives([c for c in chips if c[0] in DRIVE_CHIPS])
        known = CPU_CHIPS + GPU_CHIPS + DRIVE_CHIPS + BOARD_CHIPS
        self.others([c for c in chips if not any(c[0].startswith(p) for p in known)])
        return sorted(self.out, key=lambda g: g["group"])     # stable: keeps the order found

    def cpus(self, chips):
        name = _cpu_name()
        limit = _num_cfg("cpu_temp_alert", 80)
        if not chips:
            chips = self._thermal_zone_cpu()
        for i, (chip, path, _dev, ch) in enumerate(chips):
            pre, tag = ("cpu", "CPU") if i == 0 else (f"cpu{i + 1}", f"CPU {i + 1}")
            where = f"{name} ({chip})"
            temps = [(n, f, lab) for (k, n), (f, lab) in ch.items() if k == "temp" and _valid_temp(_num(f, 1000))]
            main = None
            for want in ("Package", "Tdie", "Tctl"):
                main = main or next((t for t in temps if t[2] and want in t[2]), None)
            main = main or (temps[0] if temps else None)
            for t in temps:
                if t is main:
                    self.temp(f"{pre}_temp", f"{tag} TEMP", t[1], "cpu", where, limit,
                              on=True, group=0, big=i == 0)
                else:
                    lab = (t[2] or f"temp {t[0]}").upper().replace("PACKAGE ID", "PACKAGE")
                    self.temp(f"{pre}_{_slug(t[2] or 'temp' + str(t[0]))}", f"{tag} {lab}"[:16],
                              t[1], "cpu", where, limit)
            for (k, n), (f, lab) in ch.items():
                if k == "power" and _num(f) is not None:
                    self.add(f"{pre}_power" if n == 1 else f"{pre}_{_slug(lab or 'power' + str(n))}",
                             f"{tag} POWER", "power", "W", 0, 200, 180, ("file", f, 1e6), "cpu",
                             where, f, on=n == 1, group=3)
        self.add("cpu_load", "CPU LOAD", "load", "%", 0, 100, 90, ("cpu_load",), "cpu",
                 name, "/proc/stat", on=True, group=0, big=True)
        if "cpu_power" not in self.keys:
            self._rapl(name)

    def _thermal_zone_cpu(self):
        """No CPU hwmon chip (some ARM boards, VMs): use the kernel's CPU thermal zone."""
        base = _p("/sys/class/thermal")
        try:
            zones = sorted(os.listdir(base), key=_natural)
        except OSError:
            return []
        for z in zones:
            kind = _read(os.path.join(base, z, "type")) or ""
            f = os.path.join(base, z, "temp")
            if kind in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc_thermal", "cpu0-thermal") \
                    and _valid_temp(_num(f, 1000)):
                return [(kind, os.path.join(base, z), "", {("temp", 1): (f, None)})]
        return []

    def _rapl(self, name):
        """Whole-package CPU power from Intel/AMD RAPL, when the system lets users read it."""
        d = _p("/sys/class/powercap/intel-rapl:0")
        f = d + "/energy_uj"
        if _num(f) is None:
            return
        lim = _num(d + "/constraint_0_power_limit_uw", 1e6)
        top = _round_up(lim * 1.25 if lim else 200)
        self.add("cpu_power", "CPU POWER", "power", "W", 0, top, top * 0.9,
                 ("energy", f, d + "/max_energy_range_uj"), "cpu", name, f, on=True, group=3)

    def gpus(self, chips):
        cards = []
        for chip, path, dev, ch in chips:
            addr = os.path.basename(dev) if PCI_ADDR.match(os.path.basename(dev)) else None
            pname = _read(dev + "/product_name") if dev else None
            vendor = _read(dev + "/vendor") if dev else None
            if not pname and dev:
                pname = _pci_name(vendor, _read(dev + "/device"))
            maker = VENDORS.get(vendor or "", "")
            m = re.match(r"^(.*?)\s*\[([^\]]+)\]$", pname or "")
            nice = f"{m.group(2)} ({m.group(1)})" if m and m.group(1) else (m.group(2) if m else pname)
            full = f"{maker} {nice}".strip() if pname else f"{maker or chip} GPU"
            cards.append({"addr": addr, "name": full, "short": _short_gpu(pname) if pname else None,
                          "chip": chip, "path": path, "dev": dev, "ch": ch, "nv": None})
        cards += self._nvidia_cards(cards)
        for c in cards:
            pdir = _pci_dir(c["addr"]) if c["addr"] else c["dev"]
            c["compute"] = (_read(pdir + "/class") or "").startswith("0x0302")
            c["boot"] = _read(pdir + "/boot_vga") == "1"
        primary = next((c for c in cards if c["boot"]), None) \
            or next((c for c in cards if not c["compute"] and c["nv"] is None), None) \
            or next((c for c in cards if not c["compute"]), None)
        shorts = [c["short"] for c in cards]
        seen = {}
        for c in cards:          # two of the same card: P100 1, P100 2
            if c["short"] and shorts.count(c["short"]) > 1:
                seen[c["short"]] = seen.get(c["short"], 0) + 1
                c["short"] = f"{c['short']} {seen[c['short']]}"
        n_other = n_nv = 0
        for c in [primary] + [c for c in cards if c is not primary] if primary else cards:
            if c is primary:
                pre, group = "gpu", 1
            elif c["nv"] is not None:
                n_nv += 1
                pre, group = ("nv" if n_nv == 1 else f"nv{n_nv}"), 5
            else:
                n_other += 1
                pre, group = f"gpu{n_other + 1}", 5
            tag = c["short"] or ("GPU" if pre == "gpu" else pre.upper())
            kind = "compute card" if c["compute"] else "graphics card"
            where = f"{c['name']} ({kind})"
            limit = _num_cfg("nv_temp_alert" if pre.startswith("nv") else "gpu_temp_alert", 85)
            if c["nv"] is not None:
                self._nvidia_gauges(c, pre, tag, where, limit, group)
            else:
                self._hwmon_gpu_gauges(c, pre, tag, where, limit, group)

    def _hwmon_gpu_gauges(self, c, pre, tag, where, limit, group):
        temps = [(n, f, lab) for (k, n), (f, lab) in c["ch"].items() if k == "temp" and _valid_temp(_num(f, 1000))]
        names = {"edge": None, "junction": "HOT SPOT", "mem": "VRAM"}
        for i, (n, f, lab) in enumerate(temps):
            extra = names.get((lab or "").lower(), (lab or f"TEMP {n}").upper())
            if i == 0:
                self.temp(f"{pre}_temp", f"{tag} TEMP", f, "gpu", where, limit, on=True, group=group)
            else:
                self.temp(f"{pre}_{_slug(lab or 'temp' + str(n))}", f"{tag} {extra}", f, "gpu", where, limit)
        fan = c["ch"].get(("fan", 1))
        if fan and _num(fan[0]) is not None:
            top = _round_up(_num(c["path"] + "/fan1_max") or 3500, 500)
            self.add(f"{pre}_fan", f"{tag} FAN", "fan", "RPM", 0, top, top * 0.86, ("file", fan[0], 1),
                     "gpu", where, fan[0], on=True, group=group + (1 if pre == "gpu" else 0), scale=1000)
        power = c["ch"].get(("power", 1))
        if power and _num(power[0]) is not None:
            cap = _num(c["path"] + "/power1_cap_max", 1e6) or _num(c["path"] + "/power1_cap", 1e6)
            top = _round_up(cap or 300)
            self.add(f"{pre}_power", f"{tag} POWER", "power", "W", 0, top, top * 0.9,
                     ("file", power[0], 1e6), "gpu", where, power[0], on=True, group=group + 2)
        busy = c["dev"] + "/gpu_busy_percent" if c["dev"] else None
        if busy and _num(busy) is not None:
            self.add(f"{pre}_load", f"{tag} LOAD", "load", "%", 0, 100, 95, ("file", busy, 1),
                     "gpu", where, busy, on=True, group=group + 2)

    def _nvidia_cards(self, cards):
        exe = shutil.which("nvidia-smi")
        if not exe:
            return []
        try:
            out = subprocess.run([exe, "--query-gpu=index,name,pci.bus_id,power.limit," + ",".join(NV_FIELDS),
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        seen = {c["addr"] for c in cards}
        found = []
        for ln in out.splitlines():
            f = [x.strip() for x in ln.split(",")]
            if len(f) < 4 + len(NV_FIELDS) or not f[0].isdigit():
                continue
            addr = f[2].lower()[-12:]
            if addr in seen:
                continue
            vals = dict(zip(NV_FIELDS, (_nv_value(x) for x in f[4:])))
            found.append({"addr": addr if PCI_ADDR.match(addr) else None, "name": f[1] if f[1].upper().startswith("NVIDIA") else "NVIDIA " + f[1],
                          "short": _short_gpu(f[1]), "chip": "nvidia-smi", "dev": "", "nv": int(f[0]),
                          "limit": _nv_value(f[3]), "vals": vals})
        return found

    def _nvidia_gauges(self, c, pre, tag, where, limit, group):
        i, vals, src = c["nv"], c["vals"], f"nvidia-smi -i {c['nv']}"
        if vals["temperature.gpu"] is not None:
            self.add(f"{pre}_temp", f"{tag} TEMP", "temp", "°C", 0, 100, limit, ("nv", i, "temperature.gpu"),
                     "gpu", where, src, on=True, group=group, alert=limit)
        if vals["power.draw"] is not None:
            top = _round_up(c["limit"] or 300)
            self.add(f"{pre}_power", f"{tag} POWER", "power", "W", 0, top, top * 0.9, ("nv", i, "power.draw"),
                     "gpu", where, src, on=True, group=group if pre != "gpu" else group + 2)
        if vals["utilization.gpu"] is not None:
            self.add(f"{pre}_load", f"{tag} LOAD", "load", "%", 0, 100, 95, ("nv", i, "utilization.gpu"),
                     "gpu", where, src, on=True, group=group if pre != "gpu" else group + 2)
        if vals["fan.speed"] is not None:           # compute cards like the Tesla have no fan of their own
            self.add(f"{pre}_fan", f"{tag} FAN", "fan", "%", 0, 100, 95, ("nv", i, "fan.speed"),
                     "gpu", where, src, on=True, group=group + (1 if pre == "gpu" else 0))

    def ram(self):
        self.add("ram", "RAM", "memory", "%", 0, 100, _num_cfg("ram_alert", 95), ("ram",), "memory",
                 "System memory", "/proc/meminfo", on=True, group=2, alert=_num_cfg("ram_alert", 95))

    def boards(self, chips):
        board = _board_name() or "Motherboard"
        limit = _num_cfg("board_temp_alert", 60)
        n_fan = 0
        have_board_temp = False
        for chip, path, _dev, ch in chips:
            where = f"{board} ({_chip_name(chip)})"
            laptop = any(chip.startswith(p) for p in LAPTOP_CHIPS)
            temps = [(n, f, lab) for (k, n), (f, lab) in ch.items() if k == "temp" and _valid_temp(_num(f, 1000))]
            main = next((t for t in temps if (t[2] or "").upper() in
                         ("SYSTIN", "MOTHERBOARD", "BOARD", "SYSTEM", "MB TEMPERATURE", "AMBIENT")), None)
            if not main and temps and not laptop:      # desktop sensor chips put the board on temp1
                main = temps[0]
            for t in temps:
                if t is main and not have_board_temp:
                    have_board_temp = True
                    self.temp("board_temp", "BOARD", t[1], "board", where, limit, on=True, group=2,
                              vmax=80, hyst=3)
                    continue
                raw = (t[2] or f"{_chip_name(chip).split()[0]} {t[0]}").upper()
                lab = {"CPUTIN": "CPU SOCKET", "SYSTIN": "BOARD"}.get(raw, raw.replace("AUXTIN", "AUX "))
                self.temp(f"board_{_slug(t[2] or 'temp' + str(t[0]))}", lab[:14], t[1], "board", where,
                          limit, vmax=100, hyst=3)
            for (k, n), (f, lab) in ch.items():
                if k == "fan" and (_num(f) or 0) > 0:          # only headers with a fan that spins
                    n_fan += 1
                    top = _round_up(_num(f"{path}/fan{n}_max") or (6000 if laptop else 2500), 500)
                    self.add(f"fan{n_fan}", _fan_label(lab, n_fan), "fan", "RPM", 0, top, top * 0.88,
                             ("file", f, 1), "board", where, f, on=True, group=1.5 if n_fan <= 2 else 3,
                             scale=1000)

    def drives(self, chips):
        limit = _num_cfg("drive_temp_alert", 70)
        for chip, path, dev, ch in chips:
            if chip == "nvme":
                ctrl = os.path.basename(dev) if dev else "nvme"
                model = _read(dev + "/model") if dev else None
            else:            # drivetemp: the device is the SCSI disk
                blocks = []
                try:
                    blocks = sorted(os.listdir(dev + "/block"))
                except OSError:
                    pass
                ctrl = blocks[0] if blocks else "disk"
                model = " ".join(x for x in (_read(dev + "/vendor"), _read(dev + "/model"))
                                 if x and x.upper() != "ATA")
            short = _short_drive(model) or ctrl.upper()
            where = f"{model or 'Drive'} ({ctrl})"
            temps = [(n, f, lab) for (k, n), (f, lab) in ch.items() if k == "temp" and _valid_temp(_num(f, 1000))]
            for i, (n, f, lab) in enumerate(temps):
                if i == 0:
                    self.temp(f"{_slug(ctrl)}_temp", f"{short} TEMP", f, "drive", where, limit, on=True, group=6)
                else:
                    self.temp(f"{_slug(ctrl)}_{_slug(lab or 'temp' + str(n))}",
                              f"{short} {(lab or str(n)).upper()}"[:16], f, "drive", where, limit)

    def others(self, chips):
        count = {}
        for chip, path, dev, ch in chips:
            role, tag = next(((r, t) for pref, r, t in OTHER_CHIPS if chip.startswith(pref)),
                             ("other", chip.upper()[:10]))
            count[tag] = count.get(tag, 0) + 1
            n = count[tag]
            pre = _slug(tag) + (str(n) if role == "memory" or n > 1 else "")
            label = f"{tag} {n}" if role == "memory" or n > 1 else tag
            where = _chip_name(chip)
            for (k, i), (f, lab) in ch.items():
                suffix = "" if i == 1 else f" {(lab or str(i)).upper()}"
                if k == "temp" and _valid_temp(_num(f, 1000)):
                    self.temp(f"{pre}_temp" if i == 1 else f"{pre}_{_slug(lab or 'temp' + str(i))}",
                              (label + suffix)[:16], f, role, where, 90)
                elif k == "fan" and (_num(f) or 0) > 0:
                    self.add(f"{pre}_fan{i}", f"{label} FAN"[:16], "fan", "RPM", 0, 6000, 5500,
                             ("file", f, 1), role, where, f, scale=1000)
                elif k == "power" and _num(f) is not None:
                    self.add(f"{pre}_power{i}", f"{label} POWER"[:16], "power", "W", 0, 100, 90,
                             ("file", f, 1e6), role, where, f)


def _nv_value(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None          # "[N/A]", "[Not Supported]"


def _fan_label(raw, n):
    """Use the machine's own fan name if it has one (Dell, ThinkPad...), else FAN n."""
    if not raw:
        return f"FAN {n}"
    t = raw.upper().replace("PROCESSOR", "CPU").replace("MOTHERBOARD", "BOARD")
    t = t.replace("SYSTEM", "SYS").replace("CHASSIS", "CASE").replace("  ", " ").strip()
    return t[:10] if t else f"FAN {n}"


def _demo_gauges():
    """The look of a typical desktop with a compute card, for --demo."""
    def g(key, label, kind, unit, vmax, red, group, scale=None, big=False, alert=None):
        return _gauge(key, label, unit, 0, vmax, red, scale=scale, big=big, kind=kind, src=("demo",),
                      device="demo", name="Demo machine", where="--demo", on=True, group=group,
                      alert=alert, hyst=5)
    return [g("cpu_temp", "CPU TEMP", "temp", "°C", 100, 80, 0, big=True, alert=80),
            g("cpu_load", "CPU LOAD", "load", "%", 100, 90, 0, big=True),
            g("gpu_temp", "RX 6600 TEMP", "temp", "°C", 100, 85, 1, alert=85),
            g("fan1", "CPU FAN", "fan", "RPM", 2500, 2200, 1, scale=1000),
            g("fan2", "CASE FAN", "fan", "RPM", 2500, 2200, 1, scale=1000),
            g("gpu_fan", "RX 6600 FAN", "fan", "RPM", 3500, 3000, 1, scale=1000),
            g("ram", "RAM", "memory", "%", 100, 95, 2, alert=95),
            g("board_temp", "BOARD", "temp", "°C", 80, 60, 2, alert=60),
            g("nv_temp", "P100 TEMP", "temp", "°C", 100, 85, 5, alert=85),
            g("nv_power", "P100 POWER", "power", "W", 250, 225, 5)]


def _cfg_keys(name):
    return [k.strip() for k in CFG.get(name, "").split(",") if k.strip()]


ALERT_RULES = {}


def setup_layout():
    """Find the machine's sensors and decide which ones the deck shows."""
    ALL_GAUGES[:] = _demo_gauges() if DEMO else Finder().run()
    show, hide = set(_cfg_keys("show")), set(_cfg_keys("hide"))
    GAUGES[:] = [g for g in ALL_GAUGES if (g["on"] or g["key"] in show) and g["key"] not in hide]
    ALERT_RULES.clear()
    for g in ALL_GAUGES:
        if g["alert"] is not None:
            what = "Memory use" if g["kind"] == "memory" else g["label"].replace(" TEMP", "").strip()
            what = {"CPU": "CPU", "BOARD": "Board"}.get(what, what)
            ALERT_RULES[g["key"]] = (g["alert"], f"{what} temperature" if g["kind"] == "temp" else what,
                                     g["unit"], g["hyst"])


class Sensors:
    """Reads the sensors Finder found. Runs on a background thread."""

    def __init__(self):
        self._last_cpu = None
        self._energy = {}

    def cpu_load(self):
        line = _read(_p("/proc/stat"))
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
            with open(_p("/proc/meminfo")) as f:
                for ln in f:
                    k, v = ln.split(":", 1)
                    info[k] = int(v.split()[0])
        except (OSError, ValueError):
            return None
        if "MemTotal" not in info or "MemAvailable" not in info:
            return None
        return 100.0 * (1 - info["MemAvailable"] / info["MemTotal"])

    def energy(self, f, wrap):
        """Watts from an energy counter in µJ, averaged since the last reading."""
        e, now = _num(f), time.monotonic()
        if e is None:
            return None
        last = self._energy.get(f)
        self._energy[f] = (e, now)
        if not last or now <= last[1]:
            return None
        de = e - last[0]
        if de < 0:
            de += _num(wrap) or 0
        return de / 1e6 / (now - last[1]) if de >= 0 else None

    @staticmethod
    def nvidia():
        exe = shutil.which("nvidia-smi")
        if not exe:
            return {}
        try:
            out = subprocess.run([exe, "--query-gpu=index," + ",".join(NV_FIELDS),
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        cards = {}
        for ln in out.splitlines():
            f = [x.strip() for x in ln.split(",")]
            if len(f) > len(NV_FIELDS) and f[0].isdigit():
                cards[int(f[0])] = dict(zip(NV_FIELDS, (_nv_value(x) for x in f[1:])))
        return cards

    def read_all(self, want=None):
        """Read the wanted gauges (all of them by default)."""
        gauges = [g for g in ALL_GAUGES if want is None or g["key"] in want]
        out = dict.fromkeys(g["key"] for g in ALL_GAUGES)
        nv = self.nvidia() if any(g["src"][0] == "nv" for g in gauges) else {}
        for g in gauges:
            src = g["src"]
            if src[0] == "file":
                out[g["key"]] = _num(src[1], src[2])
            elif src[0] == "nv":
                out[g["key"]] = nv.get(src[1], {}).get(src[2])
            elif src[0] == "energy":
                out[g["key"]] = self.energy(src[1], src[2])
            elif src[0] == "cpu_load":
                out[g["key"]] = self.cpu_load()
            elif src[0] == "ram":
                out[g["key"]] = self.ram()
        return out


class DemoSensors:
    """Made-up readings that drift up and down (run with --demo)."""

    def __init__(self):
        self.t0 = time.time()

    def read_all(self, want=None):
        t = time.time() - self.t0
        out = {}
        for i, g in enumerate(ALL_GAUGES):
            lo, hi = g["max"] * 0.3, g["red"] * 0.97
            out[g["key"]] = lo + (hi - lo) * (0.5 + 0.5 * math.sin(t / (3 + i % 7) + i))
        return out


setup_layout()
# the reading on the dock icon: the CPU temperature, or the first one there is
BADGE_KEY = next((g["key"] for g in GAUGES if g["kind"] == "temp"), "cpu_temp")


class Poller:
    """Reads the sensors on a background thread so a slow tool such as
    nvidia-smi can never freeze the window."""

    def __init__(self, sensors, want, callback):
        self.sensors = sensors
        self.want = want              # function returning the set of sources to read
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
    __slots__ = ("c", "s")

    def __init__(self, colors, style):
        self.c = {k: _hex_to_rgb(v) for k, v in colors.items()}
        self.s = style


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
    """Fill the w x h box centered on (cx, cy) with the picture, cropping the edges."""
    pw, ph = pb.get_width(), pb.get_height()
    k = max(w / pw, h / ph)
    cr.save()
    cr.translate(cx - pw * k / 2.0, cy - ph * k / 2.0)
    cr.scale(k, k)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    cr.restore()


def _draw_background(cr, w, h, look):
    c, st = look.c, look.s
    cr.set_source_rgb(*c["bg"])
    cr.paint()
    pb = PICTURES.get(st["bg_image"])
    if pb:
        _draw_picture(cr, pb, w / 2.0, h / 2.0, w, h)
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
    if pb:
        _draw_picture(cr, pb, x, y, 2 * r, 2 * r)
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


def _value_text(g, v):
    return f"{int(round(v))} {g['unit']}" if v is not None else "--"


def _dial(cr, x, y, r, g, v, look, label, hist):
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
            s = f"{num:.1f}" if scale and num != int(num) else f"{int(round(num))}"
            rt = r * 0.58
            _glow_text(cr, s, x + rt * math.cos(a), y + rt * math.sin(a), r * 0.13, c["dim"], gl * 0.6)

    _glow_text(cr, label, x, y + r * 0.30, r * 0.12, c["dim"], gl * 0.5)
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


def _ring(cr, x, y, r, g, v, look, label, hist):
    """A thick ring that fills up, with the reading in the middle."""
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
    over = v is not None and st["red_zone"] and v >= red
    if v is not None and v > vmin:
        col = c["red"] if over else c["needle"]
        cr.new_path()
        cr.arc(x, y, rad, ang(vmin), ang(v))
        _halo(cr, col, lw, gl)
        cr.set_line_width(lw)
        cr.set_source_rgb(*col)
        cr.stroke()
    cr.set_line_cap(0)
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    num = f"{int(round(v))}" if v is not None else "--"
    _glow_text(cr, num, x, y - r * 0.08, _fit_text(cr, num, r * 0.46, r * 1.0),
               c["red"] if over else c["text"], gl)
    _glow_text(cr, g["unit"], x, y + r * 0.22, r * 0.13, c["dim"], gl * 0.5)
    _glow_text(cr, label, x, y + r * 0.44, _fit_text(cr, label, r * 0.12, r * 1.2), c["dim"], gl * 0.5)


def _bar(cr, x, y, r, g, v, look, label, hist):
    """A horizontal level meter."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    _glow_text(cr, label, x, y - r * 0.58, _fit_text(cr, label, r * 0.14, r * 1.4), c["dim"], gl * 0.5)
    txt = _value_text(g, v)
    over = v is not None and st["red_zone"] and v >= red
    _glow_text(cr, txt, x, y - r * 0.18, _fit_text(cr, txt, r * 0.3, r * 1.45),
               c["red"] if over else c["text"], gl)
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
    if v is not None:
        t = max(0.0, min(1.0, (v - vmin) / (vmax - vmin)))
        if t > 0:
            col = c["red"] if over else c["needle"]
            _round_rect(cr, bx, by, max(bw * t, bh), bh, bh / 2)
            _halo_fill(cr, col, bh * 0.6, gl)
            cr.set_source_rgb(*col)
            cr.fill()
    _glow_text(cr, f"{vmin:g}", bx, y + r * 0.66, r * 0.1, c["dim"], 0, "left")
    _glow_text(cr, f"{vmax:g}", bx + bw, y + r * 0.66, r * 0.1, c["dim"], 0, "right")


def _halo_fill(cr, color, width, glow):
    """Glow around a filled shape (the shape stays as the current path)."""
    if glow > 0:
        _halo(cr, color, width, glow)


def _digital(cr, x, y, r, g, v, look, label, hist):
    """A big number with a row of segments that light up."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    over = v is not None and st["red_zone"] and v >= red
    col = c["red"] if over else c["text"]
    _glow_text(cr, label, x, y - r * 0.62, _fit_text(cr, label, r * 0.14, r * 1.4), c["dim"], gl * 0.5)
    num = f"{int(round(v))}" if v is not None else "--"
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
    n = 12
    sw, gap = (r * 1.5) / n, r * 0.04
    lit = 0 if v is None else int(round(max(0.0, min(1.0, (v - vmin) / (vmax - vmin))) * n))
    for i in range(n):
        seg_red = st["red_zone"] and (vmin + (vmax - vmin) * (i + 0.5) / n) >= red
        base = c["red"] if seg_red else c["needle"]
        sx = x - r * 0.75 + i * sw
        _round_rect(cr, sx + gap / 2, y + r * 0.34, sw - gap, r * 0.2, r * 0.04)
        if i < lit:
            _halo_fill(cr, base, r * 0.1, gl)
            cr.set_source_rgb(*base)
        else:
            cr.set_source_rgb(*_mix(c["face"], base, 0.16))
        cr.fill()


def _graph(cr, x, y, r, g, v, look, label, hist):
    """A line that shows the last minute and a half."""
    c, st = look.c, look.s
    gl = st["glow"]
    vmin, vmax, red = g["min"], g["max"], g["red"]
    cr.select_font_face(FONTS[st["font"]], 0, 1)
    over = v is not None and st["red_zone"] and v >= red
    txt = _value_text(g, v)
    value_size = _fit_text(cr, txt, r * 0.2, r * 0.78)
    cr.set_font_size(value_size)
    room = r * 1.56 - cr.text_extents(txt).x_advance - r * 0.1        # what is left for the name
    _glow_text(cr, label, x - r * 0.78, y - r * 0.62, _fit_text(cr, label, r * 0.14, max(room, r * 0.3)),
               c["dim"], gl * 0.5, "left")
    _glow_text(cr, txt, x + r * 0.78, y - r * 0.62, value_size, c["red"] if over else c["text"], gl, "right")
    px0, px1, py0, py1 = x - r * 0.78, x + r * 0.78, y - r * 0.38, y + r * 0.62

    def py(val):
        return py1 - (py1 - py0) * max(0.0, min(1.0, (val - vmin) / (vmax - vmin)))

    cr.set_source_rgb(*_mix(c["face"], c["rim"], 0.25))
    cr.set_line_width(r * 0.012)
    for f in (0.0, 0.5, 1.0):
        cr.move_to(px0, py1 - (py1 - py0) * f)
        cr.line_to(px1, py1 - (py1 - py0) * f)
    cr.stroke()
    if st["red_zone"] and red < vmax:
        cr.set_source_rgba(*c["red"], 0.7)
        cr.set_dash([r * 0.05, r * 0.04])
        cr.move_to(px0, py(red))
        cr.line_to(px1, py(red))
        cr.stroke()
        cr.set_dash([])
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


_DISPLAYS = {"dial": _dial, "ring": _ring, "bar": _bar, "digital": _digital, "graph": _graph}


def draw_gauge(cr, x, y, r, g, v, look, label=None, alarm=False, blink=False, hist=None):
    """One gauge inside the square of half-size r around (x, y), in the chosen display style."""
    hot = alarm and blink
    _frame(cr, x, y, r, look, alarm, hot)
    cr.select_font_face(FONTS[look.s["font"]], 0, 1)
    _DISPLAYS[look.s["display"]](cr, x, y, r, g, v, look, label or g["label"], hist)


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


def draw_deck(cr, w, h, layout, bh, values, labels, theme, alarms, blink, title, history=None):
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
                   theme.label(key) or labels.get(key), on, blink, (history or {}).get(key))
    cr.select_font_face(TITLE_FONTS[look0.s["font"]], 0, 1)
    cr.set_font_size(30)
    cr.set_source_rgb(*look0.c["red"])
    _centered(cr, (theme.title or TITLE).upper(), BASE_W / 2, bh - 35)


def draw_solo(cr, w, h, g, v, look, label, alarm, blink, hist=None):
    _draw_background(cr, w, h, look)
    s = min(w, h) / 230.0
    cr.translate(w / 2.0, h / 2.0)
    cr.scale(s, s)
    draw_gauge(cr, 0, 0, 100, g, v, look, label, alarm, blink, hist)


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
        num = f"{int(round(v))}" if v is not None else "--"
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
        for src, (limit, name, unit, hyst) in ALERT_RULES.items():
            if src not in shown:
                self._forget(src)
                continue
            val = v.get(src)
            if val is None:
                continue
            self._check(src, val >= limit, val < limit - hyst,
                        f"{name} is critical",
                        f"{name} is {val:.0f}{unit} (alert at {limit:.0f}{unit}).",
                        f"{name} is down to {val:.0f}{unit}.")
        if _on("fan_stop_alert"):
            for fan in [g["key"] for g in ALL_GAUGES if g["kind"] == "fan" and g["unit"] == "RPM"]:
                if fan not in shown:
                    self._forget(fan + "_stop")
                    continue
                val = v.get(fan)
                if val is None:
                    continue
                if val > 0:
                    self.fan_seen[fan] = True
                if not self.fan_seen.get(fan):
                    continue      # never saw it spin: probably fan-stop mode, not a failure
                label = self.label_for(fan)
                self._check(fan + "_stop", val == 0, val > 0,
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


# ---------------------------------------------------------------- windows
def _alert(parent, message, detail=""):
    if HAS_NEW_DIALOGS:
        d = Gtk.AlertDialog()
        d.set_message(message)
        d.set_detail(detail)
        d.show(parent)


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
    badge_key = BADGE_KEY

    def __init__(self, app, title):
        super().__init__(application=app, title=title)
        self.app = app
        self.values = {}
        self.history = {}                 # the last ~90 readings of each gauge, for the Graph style
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
        self.area.queue_draw()

    def _theme_changed(self, external=False):
        if not self._closed:
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
                   f"Open your app menu, find \"{name}\", right-click it and choose "
                   "Add to Favorites (or Pin to Dash).")


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

        self._watches = []
        for g in GAUGES:
            name = f"{APP_ID}.{g['key']}"
            self._watches.append(Gio.bus_watch_name(
                Gio.BusType.SESSION, name, Gio.BusNameWatcherFlags.NONE,
                lambda c, n, o, k=g["key"]: self._solo_up(k),
                lambda c, n, k=g["key"]: self._solo_down(k)))
        self.begin()
        GLib.timeout_add(700, self._reopen_popped)

    # --- which gauges are where
    def shown_keys(self):
        return {g["key"] for g in GAUGES} - self.popped - set(self.pending)

    def layout(self):
        shown = [g for g in GAUGES if g["key"] in self.shown_keys()]
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
        if not spawn_self("--gauge", key):
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
                  self.alerts.active | self.test_flash, self.blink, TITLE, self.history)

    def _on_close(self, win):
        for w in self._watches:
            Gio.bus_unwatch_name(w)
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
        super().__init__(app, f"{APP_NAME} - {app.display_label(key)}", key, (260, 260))
        _add_action(self, "pin", self.pin)
        state_set_popped(key, True)
        self.begin()

    def shown_keys(self):
        return {self.key}

    def _draw(self, _area, cr, w, h):
        draw_solo(cr, w, h, self.g, self.values.get(self.key), self.app.theme.look(self.key),
                  self.app.display_label(self.key), self.key in self.alerts.active
                  or self.key + "_stop" in self.alerts.active or self.key in self.test_flash, self.blink,
                  self.history.get(self.key))

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
        state_set_popped(self.key, False)
        return super()._on_close(win)


class StripWindow(OverlayWindow):
    """A slim bar of readings: small enough to leave on screen all the time."""
    mode = "strip"
    badge_key = BADGE_KEY

    def __init__(self, app):
        self.vertical = bool(win_state("strip").get("vertical", False))
        self.keys = self._saved_keys()
        w, h = strip_size(len(self.keys), self.vertical)
        super().__init__(app, f"{APP_NAME} strip", "strip", (w, h))
        self.peers = set()                 # other GaugeDeck programs that already raise alerts
        self._watches = [Gio.bus_watch_name(
            Gio.BusType.SESSION, APP_ID, Gio.BusNameWatcherFlags.NONE,
            lambda c, n, o: self._peer("deck", True), lambda c, n: self._peer("deck", False))]
        for g in GAUGES:
            self._watches.append(Gio.bus_watch_name(
                Gio.BusType.SESSION, f"{APP_ID}.{g['key']}", Gio.BusNameWatcherFlags.NONE,
                lambda c, n, o, k=g["key"]: self._peer(k, True), lambda c, n, k=g["key"]: self._peer(k, False)))
        _add_action(self, "toggle", self.toggle_key, param=True)
        _add_action(self, "vertical", self.toggle_vertical)
        _add_action(self, "pin", self.pin)
        self.begin()

    @staticmethod
    def _saved_keys():
        saved = win_state("strip").get("keys")
        valid = [k for k in (saved if isinstance(saved, list) else []) if gauge_by_key(k)]
        return valid or [g["key"] for g in GAUGES]

    def shown_keys(self):
        return set(self.keys)

    def alert_keys(self):
        """Stay quiet where the deck or a gauge window already raises the alert."""
        if "deck" in self.peers:
            return set()
        return set(self.keys) - self.peers

    def _peer(self, who, up):
        (self.peers.add if up else self.peers.discard)(who)

    def gauges(self):
        return [g for g in GAUGES if g["key"] in self.keys]

    def _draw(self, _area, cr, w, h):
        draw_strip(cr, w, h, self.gauges(), self.values, {}, self.app.theme,
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
            self.keys = [g["key"] for g in GAUGES if g["key"] in self.keys or g["key"] == key]
        self._resize_to_content()
        self.remember()

    def toggle_vertical(self):
        self.vertical = not self.vertical
        self._resize_to_content()
        self.remember()

    def context_menu(self, x, y):
        extra = Gio.Menu()
        picks = Gio.Menu()
        for g in GAUGES:
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
        for w in self._watches:
            Gio.bus_unwatch_name(w)
        return super()._on_close(win)


class CustomizeWindow(Gtk.Window):
    """Pick colors and shapes. Everything applies live, to all gauges or to one."""

    def __init__(self, app, parent):
        super().__init__(title="Customize GaugeDeck", transient_for=parent,
                         destroy_with_parent=True, default_width=720, default_height=620)
        self.app, self.theme = app, app.theme
        self.scope = None
        self._loading = False
        self.keys = [None] + [g["key"] for g in GAUGES]

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
            ["All gauges"] + [app.display_label(g["key"]) for g in GAUGES])
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
            dlg = Gtk.ColorDialog()
            dlg.set_with_alpha(False)
            btn = Gtk.ColorDialogButton(dialog=dlg)
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
        dlg = Gtk.FileDialog(title="Choose a picture")
        flt = Gtk.FileFilter()
        flt.set_name("Pictures")
        flt.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg.set_filters(store)
        dlg.set_default_filter(flt)
        dlg.open(self, None, self._picture_chosen, img)

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
        g = gauge_by_key(self.scope) or GAUGES[0]
        v = g["min"] + 0.62 * (g["max"] - g["min"])
        draw_solo(cr, w, h, g, v, self.theme.look(self.scope), self.theme.label(g["key"]) or g["label"], False, False)


class GaugeApp(Gtk.Application):
    def __init__(self, mode="deck", key=None, test_alert=False):
        # one program per window kind: clicking its icon again just brings it forward
        app_id = {"deck": APP_ID, "gauge": f"{APP_ID}.{key}", "strip": f"{APP_ID}.strip"}[mode]
        super().__init__(application_id=app_id)
        self.mode, self.key, self.test_alert = mode, key, test_alert
        self.theme = Theme()
        self.sensors = DemoSensors() if DEMO else Sensors()
        self.customize_win = None
        self._monitor = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        _add_action(self, "customize", self.show_customize, param=True)
        _add_action(self, "close-gauge", self.quit)
        _add_action(self, "about", self.show_about)
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            self._monitor = Gio.File.new_for_path(THEME_PATH).monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", lambda *a: self.theme.reload_soon())
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

    def show_customize(self, scope=""):
        if not HAS_NEW_DIALOGS:
            _alert(self.props.active_window, "Customize needs a newer GTK", "GTK 4.10 or later is required.")
            return
        if self.customize_win is None:
            self.customize_win = CustomizeWindow(self, self.props.active_window)
        self.customize_win.select_scope(scope or None)
        self.customize_win.present()

    def show_about(self):
        about = Gtk.AboutDialog(program_name=APP_NAME, version=VERSION, logo_icon_name=APP_ID,
                                comments="Car-style gauges for your PC's temps, fans and load.",
                                website="https://github.com/Cyoulater/gaugedeck",
                                license_type=Gtk.License.GPL_3_0, authors=["Miguel Ycaza"],
                                transient_for=self.props.active_window, modal=True)
        about.present()


def print_check():
    """--check: every sensor found, what it is, and a reading from it."""
    sensors = DemoSensors() if DEMO else Sensors()
    sensors.read_all()
    time.sleep(0.5)                  # load and power are measured over a short while
    vals = sensors.read_all()
    shown = {g["key"] for g in GAUGES}
    print(f"GaugeDeck {VERSION}")
    print(f"CPU: {_cpu_name()}")
    print(f"Board: {_board_name() or 'unknown'}")
    print(f"Found {len(ALL_GAUGES)} sensors, {len(GAUGES)} on the deck (*).")
    rows = [("", "KEY", "GAUGE", "TYPE", "READING", "DEVICE", "SOURCE")]
    for g in ALL_GAUGES:
        rows.append(("*" if g["key"] in shown else " ", g["key"], g["label"], g["kind"] if g["device"] == g["kind"] else f"{g['device']} {g['kind']}",
                     _value_text(g, vals.get(g["key"])), g["name"], g["where"].replace(ROOT, "") if ROOT else g["where"]))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)) + "  " + r[-1])
    tips = []
    if not any(g["device"] == "board" for g in ALL_GAUGES):
        tips.append("No board sensors or case fans. Install lm-sensors and run `sudo sensors-detect` once.")
    if os.path.isdir(_p("/proc/driver/nvidia")) and not shutil.which("nvidia-smi"):
        tips.append("An NVIDIA driver is loaded but nvidia-smi is missing, so its cards can't be read.")
    tips.append("Show more on the deck with show=KEY,KEY in ~/.config/gaugedeck/config, or hide=KEY to drop one.")
    for t in tips:
        print("Tip:", t)


def main(argv):
    mode, key, test_alert = "deck", None, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--check":
            print_check()
            return 0
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
        elif a == "--gauge" and i + 1 < len(argv):
            mode, key = "gauge", argv[i + 1]
            i += 1
            if not gauge_by_key(key):
                return "GaugeDeck: no gauge called '%s'. Choices: %s" % (key, ", ".join(g["key"] for g in GAUGES))
        elif a not in ("--demo",):
            return "GaugeDeck: unknown option '%s'" % a
        i += 1
    return GaugeApp(mode, key, test_alert).run([argv[0]])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
