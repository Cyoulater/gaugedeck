"""Tests for the Windows sensor layer (gaugedeck_windows.py). These run on Linux too: they use a saved
LibreHardwareMonitor answer instead of a real Windows machine.
Run with:  python3 -m unittest discover -s tests -v"""
import importlib.util
import os
import sys
import tempfile
import unittest

os.environ.setdefault("GAUGEDECK_CONFIG_DIR", tempfile.mkdtemp(prefix="gaugedeck-test-"))
sys.argv = ["gaugedeck"]
_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, ".."))
import fake_sysfs  # noqa: E402
import gaugedeck_windows as gw  # noqa: E402

_spec = importlib.util.spec_from_file_location("gaugedeck", os.path.join(_HERE, "..", "gaugedeck.py"))
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)


def sensor(sid, text, typ, value):
    return {"id": 0, "Text": text, "SensorId": sid, "Type": typ, "Value": value, "Children": []}


def group(text, *kids):
    return {"id": 0, "Text": text, "Children": list(kids)}


# the shape of http://localhost:8085/data.json from LibreHardwareMonitor
LHM_TREE = group("Sensor", group("DESKTOP-PC",
    group("ASUS PRIME B550-PLUS",
          group("Nuvoton NCT6798D",
                group("Voltages", sensor("/lpc/nct6798d/0/voltage/0", "Vcore", "Voltage", "1.288 V")),
                group("Temperatures", sensor("/lpc/nct6798d/0/temperature/0", "CPU Core", "Temperature", "48.0 °C"),
                      sensor("/lpc/nct6798d/0/temperature/1", "System", "Temperature", "34.5 °C")),
                group("Fans", sensor("/lpc/nct6798d/0/fan/0", "CPU Fan", "Fan", "1,152 RPM"),
                      sensor("/lpc/nct6798d/0/fan/1", "Chassis Fan #1", "Fan", "820 RPM"),
                      sensor("/lpc/nct6798d/0/fan/2", "Fan #3", "Fan", "0 RPM")))),
    group("AMD Ryzen 7 5800X",
          group("Temperatures", sensor("/amdcpu/0/temperature/0", "Core (Tctl/Tdie)", "Temperature", "52,4 °C"),
                sensor("/amdcpu/0/temperature/2", "CCD1 (Tdie)", "Temperature", "50.0 °C")),
          group("Load", sensor("/amdcpu/0/load/0", "CPU Total", "Load", "7.3 %"))),
    group("AMD Radeon RX 6800",
          group("Temperatures", sensor("/gpu-amd/0/temperature/0", "GPU Core", "Temperature", "41.0 °C"))),
    group("Samsung SSD 980 PRO 1TB",
          group("Temperatures", sensor("/nvme/0/temperature/0", "Composite Temperature", "Temperature", "39.0 °C")),
          group("Data", sensor("/nvme/0/data/0", "Data Read", "Data", "1,234.5 GB")))))


class ParseTests(unittest.TestCase):
    def test_numbers_in_any_format(self):
        self.assertEqual(gw.parse_value("45.0 °C"), 45.0)
        self.assertEqual(gw.parse_value("1,152 RPM"), 1152.0)
        self.assertEqual(gw.parse_value("52,4 °C"), 52.4)
        self.assertEqual(gw.parse_value("1,234.5 GB"), 1234.5)
        self.assertEqual(gw.parse_value("1.234,5 GB"), 1234.5)
        self.assertEqual(gw.parse_value(12), 12.0)
        self.assertIsNone(gw.parse_value("-"))
        self.assertIsNone(gw.parse_value(None))

    def test_walk_finds_every_sensor_and_its_hardware(self):
        found = {s["id"]: s for s in gw.walk(LHM_TREE)}
        self.assertEqual(len(found), 12)
        cpu = found["/amdcpu/0/temperature/0"]
        self.assertEqual((cpu["hardware"], cpu["hw_id"], cpu["value"]), ("AMD Ryzen 7 5800X", "/amdcpu/0", 52.4))
        self.assertEqual(found["/lpc/nct6798d/0/fan/0"]["hardware"], "Nuvoton NCT6798D")
        self.assertEqual(gw.walk(None), [])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.cat = gw.lhm_catalog(gw.walk(LHM_TREE), gd, {"cpu_load", "ram"})
        self.by = {s["key"]: s for s in self.cat}

    def test_usual_gauges_are_picked_and_shown(self):
        self.assertEqual(self.by["cpu_temp"]["src"], ("LibreHardwareMonitor", "/amdcpu/0/temperature/0"))
        self.assertEqual(self.by["gpu_temp"]["src"], ("LibreHardwareMonitor", "/gpu-amd/0/temperature/0"))
        self.assertEqual(self.by["board_temp"]["src"], ("LibreHardwareMonitor", "/lpc/nct6798d/0/temperature/1"))
        self.assertEqual([self.by[k]["label"] for k in ("fan1", "fan2")], ["CPU FAN", "CASE FAN #1"])
        self.assertNotIn("fan3", self.by)                  # a stopped fan isn't one of the usual gauges
        for k in ("cpu_temp", "gpu_temp", "board_temp", "fan1", "fan2"):
            self.assertTrue(self.by[k]["default"], k)

    def test_everything_else_can_be_switched_on(self):
        extra = [s for s in self.cat if not s["default"]]
        names = {s["name"] for s in extra}
        self.assertIn("AMD Ryzen 7 5800X CCD1 (Tdie)", names)
        self.assertIn("Samsung SSD 980 PRO 1TB Composite Temperature", names)
        self.assertIn("Nuvoton NCT6798D Vcore", names)
        self.assertFalse(any("Data Read" in n for n in names))      # amounts of data aren't gauges
        nvme = next(s for s in extra if "Samsung" in s["name"])
        self.assertEqual((nvme["group"], nvme["red"]), ("Storage", 70))

    def test_keys_never_clash(self):
        cat = gw.lhm_catalog(gw.walk(LHM_TREE), gd, {"cpu_temp", "fan1"})
        keys = [s["key"] for s in cat]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("cpu_temp", keys)                   # already there (from nvidia-smi or before)
        self.assertIn("fan1_2", keys)


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self._fetch = gw.fetch

    def tearDown(self):
        gw.fetch = self._fetch

    def test_windows_sources_nvidia_and_lhm(self):
        gw.fetch = lambda url=None, timeout=1.0: LHM_TREE
        cat = gw.make_discover(gd, gd.discover)(nvidia=fake_sysfs.P100)
        by = {s["key"]: s for s in cat}
        self.assertEqual(cat[0]["key"], "cpu_temp")
        self.assertEqual(by["cpu_load"]["src"], ("Windows", "cpu_load"))
        self.assertEqual(by["ram"]["src"], ("Windows", "ram"))
        self.assertEqual(by["swap"]["src"], ("Windows", "commit"))
        self.assertEqual(by["disk_root"]["src"][:2], ("Windows", "disk"))
        self.assertTrue(any(s["src"][0] == "nv" for s in cat))
        self.assertFalse(any(s["src"][0] in ("file", "max", "cpu_freq", "ram", "cpu_load") for s in cat))

    def test_without_librehardwaremonitor(self):
        gw.fetch = lambda url=None, timeout=1.0: None
        keys = [s["key"] for s in gw.make_discover(gd, gd.discover)(nvidia={})]
        self.assertEqual(keys, ["cpu_load", "ram", "swap", "disk_root"])

    def test_sensors_read_lhm_and_disk(self):
        gw.fetch = lambda url=None, timeout=1.0: LHM_TREE
        cat = gw.make_discover(gd, gd.discover)(nvidia={})
        for s in cat:
            if s["key"] == "disk_root":
                s["src"] = ("Windows", "disk", os.path.abspath(os.sep))
        vals = gw.make_sensors_class(gd)(cat).read_all(["cpu_temp", "fan1", "disk_root"])
        self.assertEqual(vals["cpu_temp"], 52.4)
        self.assertEqual(vals["fan1"], 1152.0)
        self.assertTrue(0 < vals["disk_root"] <= 100)


if __name__ == "__main__":
    unittest.main()
