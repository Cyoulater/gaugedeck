"""Builds a fake /sys, /proc and nvidia-smi for a few kinds of computer, so sensor
discovery can be tested without the hardware. Point GaugeDeck at one with
GAUGEDECK_ROOT=<dir> and PATH=<dir>/bin:$PATH.

Try it:  python3 tests/fake_machine.py desktop /tmp/fake && \\
         GAUGEDECK_ROOT=/tmp/fake PATH=/tmp/fake/bin:$PATH ./gaugedeck.py --check"""
import os
import sys

PCI_IDS = """\
# a few lines in the format of the real pci.ids
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t73ff  Navi 23 [Radeon RX 6600/6600 XT/6600M]
\t\t1458 2405  Some subsystem
8086  Intel Corporation
\t4680  AlderLake-S GT1 [UHD Graphics 770]
\t46a6  Alder Lake-P GT2 [Iris Xe Graphics]
10de  NVIDIA Corporation
\t15f8  GP100GL [Tesla P100 PCIe 16GB]
\t2206  GA102 [GeForce RTX 3080]
C 00  Unclassified device
"""

CPUINFO = {
    "amd": "processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: AMD Ryzen 9 5900X 12-Core Processor\n",
    "intel": "processor\t: 0\nvendor_id\t: GenuineIntel\n"
             "model name\t: 12th Gen Intel(R) Core(TM) i7-1260P\n",
    "xeon": "processor\t: 0\nmodel name\t: Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz\n",
}


class Machine:
    def __init__(self, root):
        self.root = root
        self.n = 0
        self.w("/proc/stat", "cpu  100 0 50 800 10 0 0 0 0 0\n")
        self.w("/proc/meminfo", "MemTotal:       32000000 kB\nMemAvailable:   24000000 kB\n")
        self.w("/usr/share/misc/pci.ids", PCI_IDS)

    def w(self, path, text):
        full = self.root + path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(str(text) if str(text).endswith("\n") else f"{text}\n")

    def device(self, path, **files):
        for k, v in files.items():
            self.w(f"{path}/{k}", v)
        return path

    def pci(self, addr, vendor, device, cls, boot=False, **files):
        path = self.device(f"/sys/devices/pci0000:00/{addr}", vendor=vendor, device=device,
                           **{"class": cls}, **files)
        if boot:
            self.w(path + "/boot_vga", "1")
        os.makedirs(self.root + "/sys/bus/pci/devices", exist_ok=True)
        os.symlink(os.path.relpath(self.root + path, self.root + "/sys/bus/pci/devices"),
                   self.root + f"/sys/bus/pci/devices/{addr}")
        return path

    def hwmon(self, name, device=None, **files):
        """files: temp1_input=45000, temp1_label='Tctl', ... (millidegrees, RPM, microwatts)."""
        h = f"/sys/class/hwmon/hwmon{self.n}"
        self.n += 1
        if device:
            real = f"{device}/hwmon/hwmon{self.n - 1}"
            self.device(real, name=name, **files)
            os.makedirs(os.path.dirname(self.root + h), exist_ok=True)
            os.symlink(os.path.relpath(self.root + real, self.root + "/sys/class/hwmon"), self.root + h)
            os.symlink(os.path.relpath(self.root + device, self.root + real), self.root + real + "/device")
        else:
            self.device(h, name=name, **files)

    def cpuinfo(self, kind):
        self.w("/proc/cpuinfo", CPUINFO[kind])

    def board(self, vendor, name):
        self.w("/sys/class/dmi/id/board_vendor", vendor)
        self.w("/sys/class/dmi/id/board_name", name)

    def nvidia_smi(self, cards):
        """cards: (index, name, bus, power limit, temp, power, fan, util), as nvidia-smi prints them."""
        info = "\n".join(", ".join(str(x) for x in c) for c in cards)
        vals = "\n".join(", ".join(str(x) for x in (c[0],) + c[4:]) for c in cards)
        self.w("/bin/nvidia-smi", "#!/bin/sh\ncase \"$1\" in\n"
               f"  --query-gpu=index,name*) cat <<'X'\n{info}\nX\n;;\n"
               f"  *) cat <<'X'\n{vals}\nX\n;;\nesac\n")
        os.chmod(self.root + "/bin/nvidia-smi", 0o755)
        os.makedirs(self.root + "/proc/driver/nvidia", exist_ok=True)


def desktop(root):
    """An AMD desktop: Radeon display card, Tesla P100 compute card, Nuvoton board chip, two drives."""
    m = Machine(root)
    m.cpuinfo("amd")
    m.board("ASUSTeK COMPUTER INC.", "ROG STRIX B550-F GAMING")
    m.hwmon("k10temp", m.pci("0000:00:18.3", "0x1022", "0x1653", "0x060000"),
            temp1_input=48250, temp1_label="Tctl", temp3_input=44000, temp3_label="Tccd1",
            temp4_input=46000, temp4_label="Tccd2")
    gpu = m.pci("0000:0c:00.0", "0x1002", "0x73ff", "0x030000", boot=True, gpu_busy_percent=3)
    m.hwmon("amdgpu", gpu, temp1_input=41000, temp1_label="edge", temp2_input=44000, temp2_label="junction",
            temp3_input=50000, temp3_label="mem", fan1_input=0, fan1_max=3200,
            power1_average=15000000, power1_cap=100000000, power1_cap_max=130000000)
    m.pci("0000:03:00.0", "0x10de", "0x15f8", "0x030200")
    m.nvidia_smi([(0, "Tesla P100-PCIE-16GB", "00000000:03:00.0", "250.00", 38, "26.51", "[N/A]", 0)])
    m.hwmon("nct6798", temp1_input=32000, temp1_label="SYSTIN", temp2_input=40000, temp2_label="CPUTIN",
            temp3_input=-62000, temp3_label="AUXTIN0", fan1_input=0, fan2_input=1100, fan3_input=850,
            fan4_input=0, fan5_input=600)
    nvme = m.device("/sys/devices/pci0000:00/0000:01:00.0/nvme/nvme0", model="Samsung SSD 980 PRO 1TB")
    m.hwmon("nvme", nvme, temp1_input=41850, temp1_label="Composite", temp2_input=41850,
            temp2_label="Sensor 1", temp3_input=50850, temp3_label="Sensor 2")
    sata = m.device("/sys/devices/pci0000:00/0000:02:00.1/ata1/host0/target0:0:0/0:0:0:0",
                    vendor="ATA     ", model="WDC WDS500G2B0A")
    m.device(sata + "/block/sda", size=976773168)
    m.hwmon("drivetemp", sata, temp1_input=33000)
    m.hwmon("spd5118", temp1_input=36000)
    m.hwmon("spd5118", temp1_input=37500)
    m.hwmon("iwlwifi_1", temp1_input=45000)
    return m


def laptop(root):
    """An Intel ThinkPad: coretemp per core, the ThinkPad EC fan, Intel graphics, ACPI zone."""
    m = Machine(root)
    m.cpuinfo("intel")
    m.board("LENOVO", "21AHCTO1WW")
    m.hwmon("acpitz", temp1_input=52000)
    m.hwmon("coretemp", m.device("/sys/devices/platform/coretemp.0"),
            temp1_input=55000, temp1_label="Package id 0", temp2_input=51000, temp2_label="Core 0",
            temp3_input=53000, temp3_label="Core 1")
    m.hwmon("thinkpad", m.device("/sys/devices/platform/thinkpad_hwmon"),
            temp1_input=55000, temp2_input=-128000, fan1_input=2900)
    m.pci("0000:00:02.0", "0x8086", "0x46a6", "0x030000", boot=True)
    m.w("/sys/class/powercap/intel-rapl:0/energy_uj", 123456789)
    m.w("/sys/class/powercap/intel-rapl:0/max_energy_range_uj", 262143328850)
    m.w("/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw", 28000000)
    return m


def server(root):
    """A headless Xeon box with two Tesla cards and nothing else readable."""
    m = Machine(root)
    m.cpuinfo("xeon")
    m.pci("0000:03:00.0", "0x10de", "0x15f8", "0x030200")
    m.pci("0000:04:00.0", "0x10de", "0x15f8", "0x030200")
    m.nvidia_smi([(0, "Tesla P100-PCIE-16GB", "00000000:03:00.0", "250.00", 38, "26.51", "[N/A]", 0),
                  (1, "Tesla P100-PCIE-16GB", "00000000:04:00.0", "250.00", 61, "180.20", "[N/A]", 97)])
    return m


def bare(root):
    """A VM or container: only /proc."""
    return Machine(root)


MACHINES = {"desktop": desktop, "laptop": laptop, "server": server, "bare": bare}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in MACHINES:
        sys.exit("usage: fake_machine.py {%s} DIR" % ",".join(MACHINES))
    MACHINES[sys.argv[1]](sys.argv[2])
