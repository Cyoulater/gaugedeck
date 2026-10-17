"""Builds a pretend computer's /sys and /proc folders so sensor discovery can be tested anywhere."""
import os


def _w(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text) + "\n")


def _chip(root, n, name, files, device=None):
    base = f"sys/class/hwmon/hwmon{n}"
    _w(root, f"{base}/name", name)
    for fname, value in files.items():
        _w(root, f"{base}/{fname}", value)
    if device:
        target = os.path.join(root, "sys/devices", device["dir"])
        for fname, value in device.get("files", {}).items():
            _w(root, f"sys/devices/{device['dir']}/{fname}", value)
        os.symlink(target, os.path.join(root, base, "device"))


def build_big_machine(root):
    """Ryzen 9 + AMD graphics card + 2 NVMe drives + a hard drive + Super-IO chip with 5 fans."""
    _w(root, "proc/stat", "cpu  1000 0 500 8000 100 0 0 0 0 0")
    _w(root, "proc/meminfo", "MemTotal:       32000000 kB\nMemAvailable:   24000000 kB\n"
                             "SwapTotal:       8000000 kB\nSwapFree:        6000000 kB")
    _w(root, "proc/cpuinfo", "processor\t: 0\nmodel name\t: AMD Ryzen 9 5950X 16-Core Processor")
    _w(root, "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 3600000)
    _w(root, "sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq", 4400000)
    _chip(root, 0, "nvme", {"temp1_input": 41850, "temp1_label": "Composite", "temp1_max": 84850,
                            "temp1_crit": 94850, "temp2_input": 40000, "temp2_label": "Sensor 1"},
          {"dir": "pci0000:00/nvme0", "files": {"model": "Samsung SSD 980 PRO 1TB"}})
    _chip(root, 1, "nvme", {"temp1_input": 36000, "temp1_label": "Composite", "temp1_max": 81850},
          {"dir": "pci0000:00/nvme1", "files": {"model": "WD_BLACK SN850X 2000GB"}})
    _chip(root, 2, "k10temp", {"temp1_input": 52125, "temp1_label": "Tctl", "temp3_input": 48000,
                               "temp3_label": "Tccd1", "temp4_input": 49000, "temp4_label": "Tccd2"})
    _chip(root, 3, "amdgpu", {"temp1_input": 58000, "temp1_label": "edge", "temp2_input": 61000,
                              "temp2_label": "junction", "temp2_crit": 110000, "temp3_input": 66000,
                              "temp3_label": "mem", "fan1_input": 1180, "power1_average": 75000000,
                              "power1_cap": 255000000, "freq1_input": 1500000000, "freq1_label": "sclk",
                              "in0_input": 900, "in0_label": "vddgfx"},
          {"dir": "pci0000:00/0000:0a:00.0", "files": {
              "vendor": "0x1002", "device": "0x73bf", "gpu_busy_percent": 12,
              "mem_info_vram_used": 2147483648, "mem_info_vram_total": 17163091968}})
    _chip(root, 4, "nct6798", {"temp1_input": 36000, "temp1_label": "SYSTIN", "temp2_input": 41000,
                               "temp2_label": "CPUTIN", "fan1_input": 1100, "fan2_input": 950,
                               "fan3_input": 0, "fan4_input": 1500, "fan5_input": 600, "fan7_input": 0,
                               "in0_input": 1056})
    _chip(root, 5, "drivetemp", {"temp1_input": 33000, "temp1_max": 60000},
          {"dir": "pci0000:00/ata1/host0/target0:0:0/0:0:0:0", "files": {"model": "ST4000DM004-2CV104"}})
    _chip(root, 6, "acpitz", {"temp1_input": 27800})
    _chip(root, 7, "iwlwifi_1", {"temp1_input": 41000})
    _chip(root, 8, "corsaircpro", {"fan1_input": 900, "fan2_input": 0, "temp1_input": 28000,
                                   "temp1_label": "Temp 1"})
    # one sensor that cannot be read and one with a nonsense value
    _chip(root, 9, "broken", {"temp1_input": "x", "temp2_input": -128000})


P100 = {0: {"name": "Tesla P100-PCIE-16GB", "temperature.gpu": 47.0, "power.draw": 33.5,
            "power.limit": 250.0, "fan.speed": None, "utilization.gpu": 0.0, "memory.used": 0.0,
            "memory.total": 16384.0, "clocks.sm": 1328.0}}
