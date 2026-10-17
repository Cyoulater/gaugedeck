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


def build_foundry(root):
    """Miguel's Foundry: JGINYUE X99 Titanium D4 (nct6779 with many unconnected sensors), a Xeon,
    an RX 580 on amdgpu that drives the screen, and a headless Tesla P100 (nvidia-smi only: this
    machine has no /proc/driver/nvidia). Values are what `sensors` reported."""
    _w(root, "proc/stat", "cpu  1000 0 500 8000 100 0 0 0 0 0")
    _w(root, "proc/meminfo", "MemTotal:       65000000 kB\nMemAvailable:   60000000 kB\n"
                             "SwapTotal:       2000000 kB\nSwapFree:        2000000 kB")
    _w(root, "proc/cpuinfo", "model name\t: Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz")
    _chip(root, 0, "coretemp", {"temp1_input": 38000, "temp1_label": "Package id 0", "temp1_max": 90000,
                                "temp1_crit": 100000, "temp2_input": 36000, "temp2_label": "Core 0",
                                "temp2_max": 90000})
    nct = {
        # SYSTIN and CPUTIN are real thermistors
        "temp1_input": 34000, "temp1_label": "SYSTIN", "temp1_max": 80000, "temp1_alarm": 0,
        "temp2_input": 37000, "temp2_label": "CPUTIN", "temp2_max": 80000, "temp2_alarm": 0,
        # AUXTIN0: limit 0 so the alarm is on; AUXTIN1-3: unconnected, read 114-117
        "temp3_input": 29500, "temp3_label": "AUXTIN0", "temp3_max": 0, "temp3_alarm": 1,
        "temp4_input": 114000, "temp4_label": "AUXTIN1", "temp4_max": 80000, "temp4_alarm": 1,
        "temp5_input": 116000, "temp5_label": "AUXTIN2", "temp5_max": 80000, "temp5_alarm": 1,
        "temp6_input": 117000, "temp6_label": "AUXTIN3", "temp6_max": 80000, "temp6_alarm": 1,
        "temp7_input": 38000, "temp7_label": "SMBUSMASTER 0",
        "temp8_input": -6000, "temp8_label": "PECI Agent 0",
        "temp9_input": 0, "temp9_label": "PCH_CHIP_CPU_MAX_TEMP", "temp10_input": 0,
        "temp10_label": "PCH_CHIP_TEMP", "temp11_input": 0, "temp11_label": "PCH_CPU_TEMP",
        "temp12_input": 0, "temp12_label": "PCH_MCH_TEMP",
        # fans: two real, three with nothing connected
        "fan1_input": 1180, "fan2_input": 960, "fan3_input": 0, "fan4_input": 0, "fan5_input": 0,
        "intrusion0_alarm": 0, "intrusion1_alarm": 1,
    }
    # voltages: Vcore (in0) reads 1.82 V over its 1.74 V max (normal here), the rest are 0 V with max 0
    nct.update({"in0_input": 1820, "in0_max": 1744, "in0_alarm": 1,
                "in2_input": 3312, "in2_max": 3632, "in3_input": 3296, "in3_max": 3632,
                "in7_input": 3376, "in7_max": 3632, "in8_input": 3136, "in8_max": 3632})
    for n in (1, 4, 5):
        nct.update({f"in{n}_input": 0, f"in{n}_max": 0})
    for n in (6, 9, 10, 11, 12, 13, 14):
        nct.update({f"in{n}_input": 0, f"in{n}_max": 0, f"in{n}_alarm": 1})
    _chip(root, 1, "nct6779", nct)
    _chip(root, 2, "amdgpu", {"temp1_input": 41000, "temp1_label": "edge", "temp1_crit": 94000,
                              "fan1_input": 0, "pwm1": 0, "power1_average": 28000000,
                              "power1_cap": 185000000, "freq1_input": 300000000, "freq1_label": "sclk",
                              "freq2_input": 300000000, "freq2_label": "mclk", "in0_input": 750,
                              "in0_label": "vddgfx"},
          {"dir": "pci0000:00/0000:03:00.0", "files": {
              "vendor": "0x1002", "device": "0x67df", "gpu_busy_percent": 0,
              "mem_info_vram_used": 268435456, "mem_info_vram_total": 8589934592}})


# nvidia-smi on the headless P100 prints [N/A] for the fan
P100_LINE = "0, Tesla P100-PCIE-16GB, 34, 28.35, 250.00, [N/A], 0, 0, 16280, 1328"


def build_dell(root):
    """The MX Linux test box: a Dell with the dell_smm chip and Dell's own fan names."""
    _w(root, "proc/stat", "cpu  1000 0 500 8000 100 0 0 0 0 0")
    _w(root, "proc/meminfo", "MemTotal:       16000000 kB\nMemAvailable:    9000000 kB\n"
                             "SwapTotal:       4000000 kB\nSwapFree:        4000000 kB")
    _w(root, "proc/cpuinfo", "model name\t: Intel(R) Core(TM) i7-7700 CPU @ 3.60GHz")
    _chip(root, 0, "coretemp", {"temp1_input": 44000, "temp1_label": "Package id 0", "temp1_max": 100000})
    _chip(root, 1, "dell_smm", {
        "temp1_input": 44000, "temp1_label": "CPU", "temp2_input": 41000, "temp2_label": "GPU",
        "temp3_input": 39000, "temp3_label": "SODIMM", "temp4_input": 36000, "temp4_label": "Other",
        "fan1_input": 2310, "fan1_label": "Processor Fan", "fan2_input": 1490,
        "fan2_label": "Motherboard Fan", "fan3_input": 1100, "fan3_label": "Video Fan",
        "fan4_input": 1520, "fan4_label": "Power Supply Fan", "fan5_input": 900, "fan5_label": "Chipset Fan"})
    _chip(root, 2, "nvme", {"temp1_input": 38850, "temp1_label": "Composite", "temp1_max": 83850},
          {"dir": "pci0000:00/nvme0", "files": {"model": "KINGSTON SA2000M81000G"}})
