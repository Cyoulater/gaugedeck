"""Tests for the parts of GaugeDeck that need no screen: sensors, layout, looks, alerts, launchers.
Run with:  python3 -m unittest discover -s tests -v"""
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

_TMP = tempfile.mkdtemp(prefix="gaugedeck-test-")
os.environ["GAUGEDECK_CONFIG_DIR"] = _TMP
sys.argv = ["gaugedeck"]
sys.path.insert(0, os.path.dirname(__file__))
import fake_sysfs  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "gaugedeck", os.path.join(os.path.dirname(__file__), "..", "gaugedeck.py"))
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)

# the real PCI name list may not exist on the machine running the tests
gd._PCI_IDS = {("1002", "73bf"): "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]",
               ("1002", "67df"): "Ellesmere [Radeon RX 470/480/570/570X/580/580X/590]"}


def make_gauge(key, big=False, red=80, vmax=100, unit="°C", **kw):
    return gd._gauge(key, key.upper(), unit, 0, vmax, red, big=big, **kw)


ORIGINAL_EIGHT = [make_gauge("cpu_temp", True), make_gauge("cpu_load", True, 90, unit="%"),
                  make_gauge("gpu_temp"), make_gauge("fan1"), make_gauge("fan2"), make_gauge("gpu_fan"),
                  make_gauge("ram"), make_gauge("board_temp")]


class LayoutTests(unittest.TestCase):
    def test_default_layout_matches_the_original_positions(self):
        placed, height = gd.deck_layout(ORIGINAL_EIGHT)
        got = [(g["key"], x, y, r) for g, x, y, r in placed]
        self.assertEqual(got, [
            ("cpu_temp", 150, 150, 110), ("cpu_load", 390, 150, 110),
            ("gpu_temp", 100, 360, 80), ("fan1", 270, 360, 80), ("fan2", 440, 360, 80),
            ("gpu_fan", 100, 540, 80), ("ram", 270, 540, 80), ("board_temp", 440, 540, 80)])
        self.assertEqual(height, 690)

    def test_short_last_row_is_centered(self):
        gauges = [make_gauge(f"s{i}") for i in range(5)]
        placed, _ = gd.deck_layout(gauges)
        self.assertEqual([x for _g, x, y, _r in placed if y == placed[-1][2]], [185.0, 355.0])

    def test_many_gauges_just_add_rows(self):
        placed, height = gd.deck_layout([make_gauge(f"s{i}") for i in range(30)])
        self.assertEqual(len({y for _g, _x, y, _r in placed}), 10)
        self.assertEqual(height, placed[-1][2] + 80 + 70)

    def test_empty_deck_has_a_height(self):
        self.assertEqual(gd.deck_layout([]), ([], 200))

    def test_clicks_find_the_right_gauge(self):
        placed, bh = gd.deck_layout(ORIGINAL_EIGHT[:2])
        s = 540 / gd.BASE_W
        self.assertEqual(gd.gauge_at(placed, 150 * s, 150 * s, 540, bh * s, bh), "cpu_temp")
        self.assertEqual(gd.gauge_at(placed, 390 * s, 150 * s, 540, bh * s, bh), "cpu_load")
        self.assertIsNone(gd.gauge_at(placed, 270 * s, 150 * s, 540, bh * s, bh))


class ThemeTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(_TMP, "t.json")
        self.theme = gd.Theme(self.path)

    def test_defaults(self):
        look = self.theme.look(None)
        self.assertEqual(look.s["face"], "circle")
        self.assertAlmostEqual(look.c["needle"][0], 1.0)

    def test_bad_values_in_the_file_are_ignored(self):
        t = gd.Theme.sanitize({"global": {"colors": {"rim": "red", "face": "#ABCDEF", "bogus": "#000000"},
                                          "style": {"face": "star", "sweep": True, "rim_width": 9,
                                                    "numbers": "yes", "needle": "arrow"}},
                               "gauges": {"ram": "nope", "fan1": {"label": "  CPU FAN  "}}})
        self.assertEqual(t["global"]["colors"], {"face": "#abcdef"})
        self.assertEqual(t["global"]["style"], {"rim_width": 0.15, "needle": "arrow"})
        self.assertEqual(t["gauges"], {"fan1": {"colors": {}, "style": {}, "label": "CPU FAN"}})

    def test_new_style_keys_are_checked(self):
        st = gd.clean_style({"display": "graph", "glow": 5, "image": " ~/p.png ", "image_dim": -1,
                             "bg_image": 7, "bg_dim": 0.3})
        self.assertEqual(st, {"display": "graph", "glow": 1.0, "image": "~/p.png", "image_dim": 0.0, "bg_dim": 0.3})
        self.assertEqual(gd.clean_style({"display": "hologram"}), {})

    def test_garbage_file_does_not_crash(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.theme.load()
        self.assertEqual(self.theme.data, gd.Theme._blank())

    def test_a_gauge_can_differ_from_the_global_look(self):
        self.theme.set(None, "colors", "rim", "#00ff00")
        self.theme.set("ram", "colors", "rim", "#0000ff")
        self.assertEqual(self.theme.hex_color(None, "rim"), "#00ff00")
        self.assertEqual(self.theme.hex_color("ram", "rim"), "#0000ff")
        self.assertEqual(self.theme.hex_color("fan1", "rim"), "#00ff00")

    def test_preset_for_all_clears_single_gauge_looks_but_keeps_names(self):
        self.theme.set("ram", "style", "needle", "blade")
        self.theme.set_label("ram", "MEMORY")
        self.theme.apply_preset(None, "Neon")
        self.assertEqual(self.theme.look("ram").s["face"], "hexagon")
        self.assertEqual(self.theme.look("ram").s["needle"], "blade")   # Neon's own needle
        self.assertEqual(self.theme.label("ram"), "MEMORY")

    def test_reset_one_gauge_keeps_its_name(self):
        self.theme.set("ram", "style", "face", "octagon")
        self.theme.set_label("ram", "MEMORY")
        self.theme.reset("ram")
        self.assertEqual(self.theme.look("ram").s["face"], "circle")
        self.assertEqual(self.theme.label("ram"), "MEMORY")

    def test_save_and_reload_between_windows(self):
        self.theme.set(None, "style", "face", "hexagon")
        self.theme.save()
        other = gd.Theme(self.path)
        self.assertEqual(other.look(None).s["face"], "hexagon")
        self.theme.set(None, "style", "face", "octagon")
        self.theme.save()
        seen = []
        other.connect(seen.append)
        self.assertTrue(other.reload())
        self.assertEqual(seen, [True])           # listeners are told it came from outside
        self.assertEqual(other.look(None).s["face"], "octagon")
        self.assertFalse(other.reload())      # nothing new

    def test_every_preset_is_valid(self):
        for name, p in gd.PRESETS.items():
            self.assertEqual(gd.clean_colors(p["colors"]), p["colors"], name)
            self.assertEqual(gd.clean_style(p["style"]), p["style"], name)


class DrawingTests(unittest.TestCase):
    """Every look draws every gauge style without errors, with and without data."""

    def test_all_presets_and_displays_draw(self):
        import cairo
        theme = gd.Theme(os.path.join(_TMP, "draw.json"))
        gauge = make_gauge("cpu_temp")
        for preset in gd.PRESETS:
            theme.apply_preset(None, preset)
            for display in gd.STYLE_CHOICES["display"]:
                for face in gd.STYLE_CHOICES["face"]:
                    theme.set(None, "style", "display", display)
                    theme.set(None, "style", "face", face)
                    for v, hist in ((None, None), (50, []), (120, [10, 50, 90])):
                        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 120)
                        gd.draw_gauge(cairo.Context(surf), 60, 60, 50, gauge, v, theme.look(None),
                                      None, True, True, hist)

    def test_small_ranges_read_in_decimals(self):
        volts = gd._gauge("v", "VCORE", "V", 0, 1.5, 1.5)
        self.assertEqual(gd._fmt_num(1.056, volts), "1.06")
        self.assertEqual(gd._fmt_num(0.9, volts), "0.9")
        self.assertEqual(gd._fmt_num(62.4, make_gauge("t")), "62")
        self.assertEqual(gd._fmt_num(None, volts), "--")

    def test_a_missing_or_broken_picture_is_ignored(self):
        self.assertIsNone(gd.PICTURES.get(""))
        self.assertIsNone(gd.PICTURES.get("/no/such/picture.png"))
        bad = os.path.join(_TMP, "bad.png")
        with open(bad, "w") as f:
            f.write("not a picture")
        self.assertIsNone(gd.PICTURES.get(bad))

    def test_a_picture_draws_inside_a_dial(self):
        import cairo
        pic = os.path.join(_TMP, "pic.png")
        s = cairo.ImageSurface(cairo.FORMAT_RGB24, 40, 20)
        c = cairo.Context(s)
        c.set_source_rgb(1, 0, 0)
        c.paint()
        s.write_to_png(pic)
        theme = gd.Theme(os.path.join(_TMP, "pic.json"))
        theme.set(None, "style", "image", pic)
        theme.set(None, "style", "image_dim", 0.0)
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 100)
        gd.draw_gauge(cairo.Context(out), 50, 50, 45, make_gauge("cpu_temp"), None, theme.look(None))
        data = out.get_data()
        self.assertGreater(data[(10 * 100 + 50) * 4 + 2], 150)   # red picture shows near the top of the face


class SensorDiscoveryTests(unittest.TestCase):
    """Discovery on a pretend high-end computer (see fake_sysfs.py)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="gaugedeck-sysfs-")
        fake_sysfs.build_big_machine(cls.root)
        cls.cat = gd.discover(cls.root, nvidia=fake_sysfs.P100)
        cls.by = {s["key"]: s for s in cls.cat}

    def test_keys_are_unique_and_valid_application_ids(self):
        keys = [s["key"] for s in self.cat]
        self.assertEqual(len(keys), len(set(keys)))
        for k in keys:
            self.assertTrue(gd.Gio.Application.id_is_valid(f"{gd.APP_ID}.{k}"), k)

    def test_everything_has_a_name_a_unit_and_a_sane_scale(self):
        for s in self.cat:
            self.assertTrue(s["name"] and s["label"] and s["unit"], s["key"])
            self.assertLess(s["min"], s["max"], s["key"])
            self.assertLessEqual(len(s["label"]), 12, s["key"])
            if s["kind"] == "temp":
                self.assertLess(s["red"], s["max"], s["key"])

    def test_the_usual_gauges_keep_their_old_names(self):
        for key in ("cpu_temp", "cpu_load", "gpu_temp", "fan1", "fan2", "gpu_fan", "ram", "board_temp"):
            self.assertTrue(self.by[key]["default"], key)
        self.assertEqual(self.by["cpu_temp"]["src"][1].split("/")[-2:], ["hwmon2", "temp1_input"])  # Tctl
        self.assertTrue(self.by["cpu_temp"]["big"] and self.by["cpu_load"]["big"])

    def test_every_spinning_fan_is_found_not_just_two(self):
        fans = [self.by[f"fan{i}"]["src"][1].split("/")[-1] for i in (1, 2, 3, 4)]
        self.assertEqual(fans, ["fan1_input", "fan2_input", "fan4_input", "fan5_input"])
        self.assertNotIn("fan5", self.by)

    def test_a_fan_that_is_not_spinning_is_listed_but_off(self):
        self.assertFalse(self.by["nct6798_fan3"]["default"])
        self.assertFalse(self.by["corsaircpro_fan2"]["default"])
        self.assertTrue(self.by["corsaircpro_fan1"]["default"])

    def test_drives_get_their_model_names_and_are_on(self):
        a, b = self.by["nvme_nvme0_composite"], self.by["nvme_nvme1_composite"]
        self.assertIn("Samsung SSD 980 PRO 1TB", a["name"])
        self.assertIn("WD_BLACK SN850X", b["name"])
        self.assertEqual((a["label"], b["label"]), ("NVME 1", "NVME 2"))
        self.assertTrue(a["default"] and b["default"])
        self.assertEqual(a["red"], 85)                       # the drive's own warning temperature
        self.assertFalse(self.by["nvme_nvme0_sensor_1"]["default"])
        self.assertIn("ST4000DM004", self.by["drivetemp_temp1"]["name"])
        self.assertTrue(self.by["drivetemp_temp1"]["default"])

    def test_the_graphics_card_is_named_from_the_pci_list(self):
        # one id covers several models, so the name keeps all of them instead of guessing the first
        self.assertIn("Radeon RX 6800/6800 XT / 6900 XT", self.by["amdgpu_junction"]["name"])
        self.assertEqual(self.by["amdgpu_junction"]["red"], 110)
        for key in ("amdgpu_junction", "amdgpu_mem", "amdgpu_power", "amdgpu_sclk", "amdgpu_load", "amdgpu_vram"):
            self.assertFalse(self.by[key]["default"], key)

    def test_the_tesla_p100_is_found_and_named(self):
        self.assertEqual(self.by["nv_temp"]["label"], "P100 TEMP")
        self.assertEqual(self.by["nv_power"]["label"], "P100 POWER")
        self.assertEqual(self.by["nv_power"]["max"], 250)
        self.assertIn("Tesla P100-PCIE-16GB", self.by["nv_temp"]["name"])
        self.assertTrue(self.by["nv_temp"]["default"] and self.by["nv_power"]["default"])
        self.assertFalse(self.by["nv_util"]["default"])
        self.assertNotIn("nv_fan", self.by)                  # the card has no fan reading

    def test_unreadable_and_nonsense_sensors_are_skipped(self):
        self.assertFalse([k for k in self.by if k.startswith("broken")])

    def test_readings_come_back_in_the_right_units(self):
        sensors = gd.Sensors(self.cat)
        v = sensors.read_all()
        self.assertAlmostEqual(v["cpu_temp"], 52.125)
        self.assertEqual(v["fan1"], 1100)
        self.assertAlmostEqual(v["ram"], 25.0)
        self.assertAlmostEqual(v["swap"], 25.0)
        self.assertAlmostEqual(v["cpu_clock"], 4000.0)
        self.assertAlmostEqual(v["amdgpu_power"], 75.0)
        self.assertAlmostEqual(v["amdgpu_sclk"], 1500.0)
        self.assertAlmostEqual(v["amdgpu_vddgfx"], 0.9)
        self.assertAlmostEqual(v["amdgpu_vram"], 100.0 * 2147483648 / 17163091968)
        self.assertIsNone(sensors.read_all(["cpu_temp"]).get("fan1"))     # only what was asked for

    def test_two_threads_reading_at_once_do_not_trip_over_each_other(self):
        import threading
        sensors = gd.Sensors(self.cat)
        errors = []

        def work():
            try:
                for _ in range(50):
                    sensors.read_all(["cpu_load", "cpu_temp", "ram"])
            except Exception as e:      # pragma: no cover
                errors.append(e)
        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_a_computer_with_almost_no_sensors_still_works(self):
        empty = tempfile.mkdtemp()
        cat = gd.discover(empty, nvidia={})
        reg = gd.Registry(cat, path=os.path.join(_TMP, "none.json"))
        self.assertEqual([g["key"] for g in reg.enabled()], ["cpu_load", "ram"])

    def test_an_nvidia_card_that_is_the_display_card_becomes_gpu_temp(self):
        root = tempfile.mkdtemp()
        cat = gd.discover(root, nvidia=fake_sysfs.P100)
        by = {s["key"]: s for s in cat}
        self.assertEqual(by["gpu_temp"]["src"], ("nv", 0, "temperature.gpu"))
        self.assertNotIn("nv_temp", by)
        self.assertTrue(by["nv_power"]["default"])

    def test_a_second_nvidia_card_gets_its_own_gauges(self):
        two = dict(fake_sysfs.P100)
        two[1] = dict(fake_sysfs.P100[0], name="NVIDIA GeForce RTX 3090")
        by = {s["key"]: s for s in gd.discover(tempfile.mkdtemp(), nvidia=two)}
        self.assertEqual(by["nv1_temp"]["label"], "RTX 3090 TEMP")
        self.assertEqual(by["nv1_power"]["src"], ("nv", 1, "power.draw"))

    def test_gpu_names_are_shortened(self):
        for full, short in (("Tesla P100-PCIE-16GB", "P100"), ("NVIDIA GeForce RTX 3090", "RTX 3090"),
                            ("NVIDIA A100-SXM4-40GB", "A100"), ("Tesla T4", "T4"),
                            ("Quadro RTX 4000", "RTX 4000"), ("NVIDIA GeForce GTX 1080 Ti", "GTX 1080 Ti"),
                            ("", "NVIDIA")):
            self.assertEqual(gd._short_gpu_name(full), short, full)

    def test_dial_maximums_are_tidy(self):
        for x, want in ((76, 80), (1.8, 2), (12.6, 15), (2500, 2500), (3300, 3500), (101, 150), (0, 1)):
            self.assertEqual(gd._nice_ceil(x), want, x)


class NvidiaSmiTests(unittest.TestCase):
    def test_output_is_parsed_even_with_a_comma_in_the_name_and_missing_values(self):
        d = tempfile.mkdtemp()
        exe = os.path.join(d, "nvidia-smi")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\n"
                    "echo '0, Tesla P100-PCIE-16GB, 47, 33.5, 250.00, [N/A], 0, 0, 16384, 1328'\n"
                    "echo '1, Weird, Name GPU, 60, [N/A], 100, 30, 55, 1000, 2000, 900'\n"
                    "echo 'not a gpu line'\n")
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + old
        try:
            cards = gd._nvidia_query_all()
        finally:
            os.environ["PATH"] = old
        self.assertEqual(sorted(cards), [0, 1])
        self.assertEqual(cards[0]["name"], "Tesla P100-PCIE-16GB")
        self.assertEqual(cards[0]["temperature.gpu"], 47.0)
        self.assertIsNone(cards[0]["fan.speed"])
        self.assertEqual(cards[1]["name"], "Weird, Name GPU")
        self.assertIsNone(cards[1]["power.draw"])
        self.assertEqual(cards[1]["clocks.sm"], 900.0)

    def test_no_nvidia_smi_means_no_cards(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = tempfile.mkdtemp()
        try:
            self.assertEqual(gd._nvidia_query_all(), {})
        finally:
            os.environ["PATH"] = old


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        fake_sysfs.build_big_machine(self.root)
        self.cat = gd.discover(self.root, nvidia=fake_sysfs.P100)
        self.path = os.path.join(tempfile.mkdtemp(), "sensors.json")
        self.reg = gd.Registry(self.cat, self.path)

    def keys(self):
        return [g["key"] for g in self.reg.enabled()]

    def test_the_usual_ones_are_shown_to_begin_with(self):
        self.assertEqual(self.keys()[:3], ["cpu_temp", "cpu_load", "gpu_temp"])
        self.assertIn("nv_temp", self.keys())
        self.assertNotIn("amdgpu_junction", self.keys())

    def test_switching_a_sensor_on_and_off(self):
        self.reg.set_shown("amdgpu_junction", True)
        self.assertIn("amdgpu_junction", self.keys())
        self.assertIn("cpu_temp", self.keys())              # the rest stay
        self.reg.set_shown("cpu_temp", False)
        self.assertNotIn("cpu_temp", self.keys())
        self.reg.use_usual()
        self.assertIn("cpu_temp", self.keys())
        self.assertNotIn("amdgpu_junction", self.keys())

    def test_limits_change_the_scale_and_can_be_cleared(self):
        self.reg.set_limit("cpu_temp", "red", 70)
        self.reg.set_limit("cpu_temp", "max", 120)
        g = self.reg.get("cpu_temp")
        self.assertEqual((g["red"], g["max"]), (70, 120))
        self.assertEqual(self.reg.by_key["cpu_temp"]["red"], 80)    # the original is untouched
        self.reg.clear_limits("cpu_temp")
        self.assertEqual(self.reg.get("cpu_temp")["red"], 80)

    def test_the_choice_is_saved_and_other_windows_pick_it_up(self):
        self.reg.set_shown("amdgpu_junction", True)
        self.reg.set_limit("ram", "red", 90)
        self.reg.save()
        other = gd.Registry(self.cat, self.path)
        self.assertIn("amdgpu_junction", [g["key"] for g in other.enabled()])
        self.assertEqual(other.get("ram")["red"], 90)
        self.reg.set_shown("amdgpu_junction", False)
        self.reg.save()
        seen = []
        other.connect(lambda: seen.append(1))
        self.assertTrue(other.reload())
        self.assertEqual(len(seen), 1)
        self.assertNotIn("amdgpu_junction", [g["key"] for g in other.enabled()])

    def test_junk_in_the_file_is_ignored(self):
        show, limits = self.reg.sanitize({"show": ["cpu_temp", 5, None], "limits": {
            "ram": {"red": 90, "bogus": 1, "max": "x"}, "fan1": "no"}})
        self.assertEqual(show, ["cpu_temp"])
        self.assertEqual(limits, {"ram": {"red": 90.0}})
        self.assertEqual(self.reg.sanitize("nonsense"), (None, {}))

    def test_a_choice_that_matches_nothing_still_shows_a_gauge(self):
        self.reg.show = ["no_such_sensor"]
        self.assertEqual(self.keys(), ["cpu_load"])

    def test_unknown_sensors_in_the_file_do_not_break_anything(self):
        with open(self.path, "w") as f:
            f.write('{"show": ["cpu_temp", "gone_sensor"]}')
        reg = gd.Registry(self.cat, self.path)
        self.assertEqual([g["key"] for g in reg.enabled()], ["cpu_temp"])



def foundry_nvidia():
    """What nvidia-smi reports for the headless Tesla P100: no fan (Fan N/A)."""
    return {0: {"name": "Tesla P100-PCIE-16GB", "temperature.gpu": 34.0, "power.draw": 28.35,
                "power.limit": 250.0, "fan.speed": None, "utilization.gpu": 0.0, "memory.used": 0.0,
                "memory.total": 16280.0, "clocks.sm": 1328.0}}


class FoundryTests(unittest.TestCase):
    """The JGINYUE X99 Titanium D4 board (nct6779) reports many sensors with nothing connected."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="gaugedeck-foundry-")
        fake_sysfs.build_foundry(cls.root)
        cls.cat = gd.discover(cls.root, nvidia=foundry_nvidia())
        cls.by = {s["key"]: s for s in cls.cat}

    def test_only_the_ten_real_gauges_are_on_by_default(self):
        reg = gd.Registry(self.cat, os.path.join(_TMP, "foundry-sensors.json"))
        self.assertEqual(sorted(g["key"] for g in reg.enabled()), sorted([
            "cpu_temp", "cpu_load", "gpu_temp", "fan1", "fan2", "gpu_fan", "ram", "board_temp",
            "nv_temp", "nv_power"]))

    def test_both_graphics_cards_show(self):
        self.assertTrue(self.by["gpu_temp"]["src"][1].endswith("hwmon2/temp1_input"))   # the RX 580
        self.assertTrue(self.by["gpu_temp"]["default"] and self.by["gpu_fan"]["default"])
        self.assertEqual(self.by["nv_temp"]["label"], "P100 TEMP")                      # the Tesla P100
        self.assertTrue(self.by["nv_temp"]["default"] and self.by["nv_power"]["default"])
        self.assertNotIn("nv_fan", self.by)                  # Pascal compute card: Fan N/A

    def test_the_rx_580_is_not_mistaken_for_an_rx_470(self):
        name = self.by["amdgpu_power"]["name"]
        self.assertIn("470/480/570/570X/580/580X/590", name)

    def test_unconnected_temperatures_are_flagged_and_off(self):
        for key in ("nct6779_auxtin0", "nct6779_auxtin1", "nct6779_auxtin2", "nct6779_auxtin3",
                    "nct6779_peci_agent_0", "nct6779_pch_chip_cpu_max_temp", "nct6779_pch_chip_temp",
                    "nct6779_pch_cpu_temp", "nct6779_pch_mch_temp"):
            self.assertTrue(self.by[key]["junk"], key)
            self.assertFalse(self.by[key]["default"], key)

    def test_real_board_temperatures_are_not_flagged(self):
        for key in ("board_temp", "nct6779_cputin", "nct6779_smbusmaster_0", "cpu_temp"):
            self.assertEqual(self.by[key]["junk"], "", key)

    def test_voltages_are_off_and_the_dead_ones_are_flagged(self):
        for n in range(15):
            s = self.by.get(f"nct6779_in{n}")
            if s is None:
                continue
            self.assertFalse(s["default"], n)
            self.assertEqual(bool(s["junk"]), n in (1, 4, 5, 6, 9, 10, 11, 12, 13, 14), n)
        self.assertEqual(self.by["nct6779_in0"]["junk"], "")        # Vcore: odd but real, never alerts

    def test_there_are_no_intrusion_sensors(self):
        self.assertFalse([k for k in self.by if "intrusion" in k])

    def test_fans_with_nothing_connected_are_off(self):
        for n in (3, 4, 5):
            self.assertFalse(self.by[f"nct6779_fan{n}"]["default"], n)
        self.assertTrue(self.by["fan1"]["default"] and self.by["fan2"]["default"])
        self.assertNotIn("fan3", self.by)

    def _alerts(self, sent):
        self.addCleanup(setattr, gd, "REG", gd.REG)
        gd.REG = gd.Registry(self.cat, os.path.join(_TMP, "foundry-alerts.json"))
        gd.play_alarm = lambda: None
        gd.CFG["alerts"] = "on"
        alerts = gd.Alerts(lambda k, t, b, u: sent.append((k, t)), lambda k: k)
        alerts.hold = 2
        return alerts

    def test_nothing_unconnected_ever_raises_an_alert(self):
        sent = []
        alerts = self._alerts(sent)
        values = gd.Sensors(self.cat).read_all()              # what the board really reports
        everything = {s["key"] for s in self.cat}
        for _ in range(12):
            alerts.update(values, everything)
        self.assertEqual(sent, [])

    def test_a_flagged_sensor_stays_quiet_even_when_switched_on_and_very_hot(self):
        sent = []
        alerts = self._alerts(sent)
        for _ in range(6):
            alerts.update({"nct6779_auxtin1": 117}, {"nct6779_auxtin1"})
        self.assertEqual(sent, [])

    def test_a_real_sensor_still_alarms(self):
        sent = []
        alerts = self._alerts(sent)
        for _ in range(6):
            alerts.update({"cpu_temp": 96}, {"cpu_temp"})
        self.assertEqual([t for _k, t in sent], ["CPU temperature is critical"])

    def test_a_glitch_reading_on_a_real_sensor_does_not_alarm(self):
        sent = []
        alerts = self._alerts(sent)
        for bad in (255, 0, -128, 65535):
            for _ in range(5):
                alerts.update({"cpu_temp": bad}, {"cpu_temp"})
        self.assertEqual(sent, [])


class HeadlessNvidiaTests(unittest.TestCase):
    """No /proc/driver/nvidia (headless server driver): the card is found through nvidia-smi alone."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.counter = os.path.join(self.bin, "calls")
        exe = os.path.join(self.bin, "nvidia-smi")
        with open(exe, "w") as f:
            f.write(f"#!/bin/sh\necho x >> '{self.counter}'\necho '{fake_sysfs.P100_LINE}'\n")
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)
        self.old = os.environ["PATH"]
        os.environ["PATH"] = self.bin + os.pathsep + self.old
        self.addCleanup(os.environ.__setitem__, "PATH", self.old)
        self.root = tempfile.mkdtemp()
        fake_sysfs.build_foundry(self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "proc/driver/nvidia")))

    def calls(self):
        with open(self.counter) as f:
            return len(f.read().split())

    def test_the_p100_is_found_next_to_the_rx_580_without_proc_driver_nvidia(self):
        by = {s["key"]: s for s in gd.discover(self.root)}        # asks nvidia-smi itself
        self.assertTrue(by["gpu_temp"]["default"])                # the RX 580
        self.assertEqual(by["nv_temp"]["label"], "P100 TEMP")     # the P100
        self.assertNotIn("nv_fan", by)

    def test_readings_come_from_nvidia_smi(self):
        sensors = gd.Sensors(gd.discover(self.root))
        v = sensors.read_all(["nv_temp", "nv_power", "gpu_temp"])
        self.assertEqual(v["nv_temp"], 34.0)
        self.assertEqual(v["nv_power"], 28.35)
        self.assertEqual(v["gpu_temp"], 41.0)

    def test_nvidia_smi_is_not_started_every_second(self):
        sensors = gd.Sensors(gd.discover(self.root))
        before = self.calls()
        for _ in range(10):
            sensors.read_all(["nv_temp", "nv_power"])
        self.assertEqual(self.calls() - before, 1)               # one call, not ten

    def test_a_failed_call_is_retried_next_time(self):
        sensors = gd.Sensors(gd.discover(self.root))
        sensors.read_all(["nv_temp"])
        os.remove(os.path.join(self.bin, "nvidia-smi"))
        sensors._nv = ({}, 0.0)                                  # as if the cache had gone stale
        self.assertIsNone(sensors.read_all(["nv_temp"])["nv_temp"])


class DellTests(unittest.TestCase):
    """The MX Linux test box: a Dell with the dell_smm chip."""

    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp(prefix="gaugedeck-dell-")
        fake_sysfs.build_dell(root)
        cls.by = {s["key"]: s for s in gd.discover(root, nvidia={})}

    def test_dell_fan_names_are_used_and_fit(self):
        labels = [self.by[f"fan{i}"]["label"] for i in range(1, 6)]
        self.assertEqual(labels, ["CPU FAN", "BOARD FAN", "VIDEO FAN", "PSU FAN", "CHIPSET FAN"])
        self.assertEqual(self.by["fan4"]["name"], "Power Supply Fan speed")
        self.assertTrue(all(len(x) <= 12 for x in labels))

    def test_the_board_gauge_does_not_repeat_the_cpu_temperature(self):
        self.assertNotIn("board_temp", self.by)
        self.assertEqual(self.by["dell_smm_cpu"]["name"], "Dell CPU temperature")
        self.assertFalse(self.by["dell_smm_cpu"]["default"])

    def test_the_drive_is_named(self):
        self.assertIn("KINGSTON", self.by["nvme_composite"]["name"])



class LatitudeTests(unittest.TestCase):
    """'mini tank', a Dell Latitude E6410 with a Core i5 M 540: checked against its real --sensors list."""

    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp(prefix="gaugedeck-e6410-")
        fake_sysfs.build_latitude_e6410(root)
        cls.cat = gd.discover(root, nvidia={})
        cls.by = {s["key"]: s for s in cls.cat}

    def test_the_default_deck_has_the_four_gauges_that_make_sense(self):
        reg = gd.Registry(self.cat, os.path.join(_TMP, "e6410.json"))
        self.assertEqual([g["key"] for g in reg.enabled()], ["cpu_temp", "cpu_load", "fan1", "ram"])

    def test_a_processor_with_no_whole_cpu_reading_uses_the_hottest_core(self):
        cpu = self.by["cpu_temp"]
        self.assertEqual(cpu["junk"], "")
        self.assertTrue(cpu["default"])
        self.assertEqual(cpu["src"][0], "max")
        self.assertIn("hottest core", cpu["name"])
        self.assertEqual(gd.Sensors(self.cat).read_all(["cpu_temp"])["cpu_temp"], 59.0)

    def test_the_hottest_core_follows_the_cores(self):
        sensors = gd.Sensors(self.cat)
        files = self.by["cpu_temp"]["src"][1]
        with open(files[-1], "w") as f:                  # core 2 gets hotter than core 0
            f.write("71000\n")
        self.assertEqual(sensors.read_all(["cpu_temp"])["cpu_temp"], 71.0)

    def test_each_core_is_still_listed_on_its_own(self):
        self.assertFalse(self.by["coretemp_core_0"]["default"])
        self.assertFalse(self.by["coretemp_core_2"]["default"])

    def test_a_dell_fan_that_is_stopped_because_it_is_cool_is_still_shown(self):
        fan = self.by["fan1"]
        self.assertTrue(fan["default"])
        self.assertEqual((fan["label"], fan["name"]), ("CPU FAN", "Processor Fan speed"))

    def test_that_stopped_fan_does_not_raise_a_false_alarm_but_does_if_it_dies_later(self):
        sent = []
        self.addCleanup(setattr, gd, "REG", gd.REG)
        gd.REG = gd.Registry(self.cat, os.path.join(_TMP, "e6410-alerts.json"))
        gd.play_alarm = lambda: None
        gd.CFG["alerts"] = "on"
        alerts = gd.Alerts(lambda k, t, b, u: sent.append(t), lambda k: "CPU FAN")
        alerts.hold = 2
        for _ in range(6):
            alerts.update({"fan1": 0}, {"fan1"})
        self.assertEqual(sent, [])
        alerts.update({"fan1": 2200}, {"fan1"})
        for _ in range(4):
            alerts.update({"fan1": 0}, {"fan1"})
        self.assertEqual(sent, ["CPU FAN stopped"])

    def test_sensors_that_share_a_label_get_their_own_numbers(self):
        names = [self.by[k]["name"] for k in ("dell_smm_other", "dell_smm_other_2", "dell_smm_other_3")]
        self.assertEqual(len(set(names)), 3)
        self.assertIn("(sensor 4)", names[1])

    def test_no_name_says_fan_twice(self):
        self.assertFalse([s["name"] for s in self.cat if "fan fan" in s["name"].lower()])


class OldSettingsTests(unittest.TestCase):
    """Settings files written by older versions keep working."""

    def load(self, text, raw=False):
        path = os.path.join(_TMP, "old-config")
        with open(path, "wb" if raw else "w") as f:
            f.write(text)
        return gd.load_config(path)

    def test_the_config_from_version_1_2_1(self):
        cfg = self.load("title=THE FOUNDRY\nscale=0.6\nnv_temp_alert=88\n")
        self.assertEqual((cfg["title"], cfg["scale"], cfg["nv_temp_alert"]), ("THE FOUNDRY", "0.6", "88"))
        self.assertEqual(cfg["alerts"], "on")                      # everything else keeps its default

    def test_the_readme_example_with_notes_after_the_value(self):
        cfg = self.load("title=MY RIG\nscale=0.6      # 0.3 = tiny, 1.0 = large\n")
        self.assertEqual(cfg["scale"], "0.6")

    def test_a_hash_inside_a_title_is_kept(self):
        self.assertEqual(self.load("title=RIG #1\n")["title"], "RIG #1")

    def test_windows_line_endings_a_bom_and_quotes(self):
        cfg = self.load('\ufefftitle="THE FOUNDRY"\r\nscale=0.5\r\n')
        self.assertEqual((cfg["title"], cfg["scale"]), ("THE FOUNDRY", "0.5"))

    def test_bytes_that_are_not_text_do_not_crash(self):
        cfg = self.load(b"title=CAF\xc9 RIG\n\xff\xfe\nscale=0.7\n", raw=True)
        self.assertEqual(cfg["scale"], "0.7")

    def test_a_missing_file_gives_the_defaults(self):
        self.assertEqual(gd.load_config(os.path.join(_TMP, "nope"))["scale"], "0.6")

    def test_nonsense_numbers_fall_back_instead_of_crashing(self):
        old = dict(gd.CFG)
        self.addCleanup(lambda: (gd.CFG.clear(), gd.CFG.update(old)))
        gd.CFG["cpu_temp_alert"] = "hot"
        self.assertEqual(gd._num_cfg("cpu_temp_alert", 80), 80.0)

    def test_the_old_alert_limit_still_sets_the_red_zone(self):
        code = ("import sys; sys.argv=['gd']; import importlib.util as u;"
                "s=u.spec_from_file_location('g', %r); g=u.module_from_spec(s); s.loader.exec_module(g);"
                "print(g.discover(%r, nvidia={0: %r})[0]['red'])")
        root = tempfile.mkdtemp()
        fake_sysfs.build_foundry(root)
        cfgdir = tempfile.mkdtemp()
        with open(os.path.join(cfgdir, "config"), "w") as f:
            f.write("cpu_temp_alert=70\nnv_temp_alert=88\n")
        out = subprocess.run([sys.executable, "-c", code % (os.path.join(os.path.dirname(__file__), "..", "gaugedeck.py"),
                                                           root, foundry_nvidia()[0] and foundry_nvidia())],
                             env=dict(os.environ, GAUGEDECK_CONFIG_DIR=cfgdir), capture_output=True, text=True)
        self.assertEqual(out.stdout.strip().split()[-1], "70.0")


class PictureSafetyTests(unittest.TestCase):
    def draw(self, **style):
        import cairo
        theme = gd.Theme(os.path.join(_TMP, "picsafe.json"))
        for k, v in style.items():
            theme.set(None, "style", k, v)
        placed, bh = gd.deck_layout(ORIGINAL_EIGHT)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 400)
        cr = cairo.Context(surf)
        gd.draw_deck(cr, 300, 400, placed, bh, {}, {}, theme, set(), False, "T")
        gd.draw_solo(cairo.Context(surf), 200, 200, ORIGINAL_EIGHT[0], 50, theme.look(None), "X", False, False)

    def test_a_picture_that_was_moved_or_deleted_does_not_break_drawing(self):
        self.draw(bg_image="/no/such/place/wallpaper.jpg", image="/gone/dial.png")

    def test_a_folder_or_empty_file_instead_of_a_picture(self):
        empty = os.path.join(_TMP, "empty.png")
        open(empty, "w").close()
        self.draw(bg_image=_TMP, image=empty)

    def test_a_picture_that_is_deleted_after_it_was_shown(self):
        import cairo
        pic = os.path.join(_TMP, "soon-gone.png")
        s = cairo.ImageSurface(cairo.FORMAT_RGB24, 20, 20)
        s.write_to_png(pic)
        self.draw(bg_image=pic, image=pic)
        os.remove(pic)
        self.draw(bg_image=pic, image=pic)


class RepoFileTests(unittest.TestCase):
    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def path(self, name):
        return os.path.join(self.ROOT, name)

    def test_the_scripts_are_executable(self):
        for name in ("install.sh", "uninstall.sh", "gaugedeck.py"):
            self.assertTrue(os.access(self.path(name), os.X_OK), name)

    def test_git_records_them_as_executable(self):
        out = subprocess.run(["git", "ls-files", "-s", "install.sh", "uninstall.sh", "gaugedeck.py"],
                             cwd=self.ROOT, capture_output=True, text=True)
        if out.returncode != 0 or not out.stdout:
            self.skipTest("not a git checkout")
        for line in out.stdout.splitlines():
            self.assertTrue(line.startswith("100755"), line)

    def test_the_scripts_parse_and_the_readme_says_sh_install(self):
        for name in ("install.sh", "uninstall.sh"):
            self.assertEqual(subprocess.run(["sh", "-n", self.path(name)]).returncode, 0, name)
        with open(self.path("README.md")) as f:
            self.assertIn("sh install.sh", f.read())

    def test_the_version_matches_the_app_info_file(self):
        with open(self.path("io.github.cyoulater.GaugeDeck.metainfo.xml")) as f:
            tree = ET.parse(f)
        releases = tree.getroot().find("releases")
        self.assertEqual(releases[0].get("version"), gd.VERSION)

    def test_uninstall_removes_what_the_update_added(self):
        with open(self.path("uninstall.sh")) as f:
            text = f.read()
        for needle in ("GaugeDeck.*.desktop", "GaugeDeck.*.svg", ".config/gaugedeck", "--remove-launchers"):
            self.assertIn(needle, text)

    def test_no_libadwaita(self):
        with open(self.path("gaugedeck.py")) as f:
            self.assertNotIn("Adw", f.read())

    def test_older_gtk_falls_back_to_the_older_dialogs(self):
        code = ("import sys; sys.argv=['gd']; import importlib.util as u;"
                "s=u.spec_from_file_location('g', %r); g=u.module_from_spec(s); s.loader.exec_module(g);"
                "print(g.HAS_COLOR_DIALOG, g.HAS_ALERT_DIALOG, g.HAS_FILE_DIALOG)")
        out = subprocess.run([sys.executable, "-c", code % self.path("gaugedeck.py")],
                             env=dict(os.environ, GAUGEDECK_OLD_GTK="1", GAUGEDECK_CONFIG_DIR=_TMP),
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip().split()[-3:], ["False", "False", "False"])



class LayerCacheTests(unittest.TestCase):
    """The unchanging part of each gauge is drawn once and reused. It must look the same as drawing
    everything directly, and it must never show an out-of-date look."""

    @classmethod
    def setUpClass(cls):
        cls.gauges = gd.enabled_gauges() or [make_gauge("cpu_temp", True)]
        cls.gauges = [make_gauge("cpu_temp", True), make_gauge("cpu_load", True, 90, unit="%"),
                      make_gauge("fan1", red=2200, vmax=2500, unit="RPM", scale=1000), make_gauge("ram", unit="%")]
        cls.vals = {"cpu_temp": 62, "cpu_load": 41, "fan1": 1300, "ram": 55}

    def tearDown(self):
        gd.CACHE_LAYERS = True
        gd._LAYERS.clear()

    def render(self, theme, cached, dpi=1, hot=False):
        import cairo
        gd.CACHE_LAYERS = cached
        placed, bh = gd.deck_layout(self.gauges)
        w, h = 300, int(bh * 0.55)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * dpi, h * dpi)
        surf.set_device_scale(dpi, dpi)
        gd.draw_deck(cairo.Context(surf), w, h, placed, bh, self.vals, {}, theme,
                     {"cpu_temp"} if hot else set(), hot, "T", {}, dpi)
        return bytes(surf.get_data())

    @staticmethod
    def difference(a, b):
        mean = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
        far = sum(1 for x, y in zip(a, b) if abs(x - y) > 48) / len(a) * 100
        return mean, far

    def test_cached_drawing_matches_direct_drawing(self):
        for preset in gd.PRESETS:
            theme = gd.Theme(os.path.join(_TMP, "cache.json"))
            theme.apply_preset(None, preset)
            for dpi in (1, 2):
                for hot in (False, True):
                    gd._LAYERS.clear()
                    mean, far = self.difference(self.render(theme, True, dpi, hot), self.render(theme, False, dpi, hot))
                    self.assertLess(mean, 6, (preset, dpi, hot))
                    self.assertLess(far, 6, (preset, dpi, hot))

    def test_a_changed_look_is_not_served_from_the_cache(self):
        theme = gd.Theme(os.path.join(_TMP, "cache2.json"))
        before = self.render(theme, True)
        theme.set(None, "style", "glow", 1.0)
        theme.set(None, "colors", "rim", "#ff00aa")
        after = self.render(theme, True)
        self.assertGreater(self.difference(before, after)[0], 3)                  # it really changed
        self.assertLess(self.difference(after, self.render(theme, False))[0], 6)  # and matches a fresh draw

    def test_changed_limits_and_names_are_not_served_from_the_cache(self):
        theme = gd.Theme(os.path.join(_TMP, "cache3.json"))
        before = self.render(theme, True)
        self.gauges[0] = dict(self.gauges[0], red=60)
        after = self.render(theme, True)
        self.assertGreater(self.difference(before, after)[0], 0.3)
        theme.set_label("cpu_temp", "ENGINE")
        placed, _bh = gd.deck_layout(self.gauges)
        self.assertLess(self.difference(self.render(theme, True), self.render(theme, False))[0], 6)

    def test_the_alarm_flash_state_has_its_own_picture(self):
        theme = gd.Theme(os.path.join(_TMP, "cache4.json"))
        calm, flash = self.render(theme, True, hot=False), self.render(theme, True, hot=True)
        self.assertGreater(self.difference(calm, flash)[0], 0.3)

    def test_a_replaced_picture_file_is_picked_up(self):
        import cairo
        pic = os.path.join(_TMP, "layer-pic.png")

        def make(rgb):
            surf = cairo.ImageSurface(cairo.FORMAT_RGB24, 30, 30)
            c = cairo.Context(surf)
            c.set_source_rgb(*rgb)
            c.paint()
            surf.write_to_png(pic)
        theme = gd.Theme(os.path.join(_TMP, "cache5.json"))
        theme.set(None, "style", "image", pic)
        theme.set(None, "style", "image_dim", 0.0)
        make((1, 0, 0))
        red = self.render(theme, True)
        make((0, 0, 1))
        os.utime(pic, (os.stat(pic).st_atime, os.stat(pic).st_mtime + 5))
        blue = self.render(theme, True)
        self.assertGreater(self.difference(red, blue)[0], 5)

    def test_the_cache_does_not_grow_without_limit(self):
        import cairo
        theme = gd.Theme(os.path.join(_TMP, "cache6.json"))
        placed, bh = gd.deck_layout(self.gauges)
        for width in range(200, 600, 5):                 # dragging a window to many sizes
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, int(bh * width / gd.BASE_W) + 1)
            gd.draw_deck(cairo.Context(surf), width, int(bh * width / gd.BASE_W) + 1, placed, bh, self.vals,
                         {}, theme, set(), False, "T", {}, 1)
        self.assertLessEqual(len(gd._LAYERS), 81)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        gd.play_alarm = lambda: None
        gd.CFG["alerts"] = "on"
        root = tempfile.mkdtemp()
        fake_sysfs.build_big_machine(root)
        self.old_reg = gd.REG
        gd.REG = gd.Registry(gd.discover(root, nvidia={}), os.path.join(_TMP, "alerts-sensors.json"))
        self.addCleanup(setattr, gd, "REG", self.old_reg)
        self.alerts = gd.Alerts(lambda k, t, b, u: self.sent.append((k, t, u)), lambda k: k.upper())
        self.alerts.hold = 3
        self.shown = {"cpu_temp", "fan1"}

    def feed(self, **v):
        self.alerts.update(v, self.shown)

    def test_a_quick_spike_is_ignored(self):
        self.feed(cpu_temp=90)
        self.feed(cpu_temp=90)
        self.feed(cpu_temp=60)
        self.assertEqual(self.sent, [])

    def test_alerts_once_then_says_back_to_normal(self):
        for _ in range(6):
            self.feed(cpu_temp=90)
        self.assertEqual([s[1] for s in self.sent], ["CPU temperature is critical"])
        self.feed(cpu_temp=78)                 # inside the hysteresis band: still alarmed
        self.assertIn("cpu_temp", self.alerts.active)
        self.feed(cpu_temp=60)
        self.assertEqual(self.sent[-1][1], "Back to normal")
        self.assertNotIn("cpu_temp", self.alerts.active)

    def test_a_drive_that_runs_hot_is_named_in_the_alert(self):
        self.shown = {"nvme_nvme0_composite"}
        for _ in range(4):
            self.feed(nvme_nvme0_composite=90)
        self.assertIn("Samsung SSD 980 PRO 1TB temperature is critical", [s[1] for s in self.sent])

    def test_voltages_and_clocks_never_alert(self):
        self.shown = {"amdgpu_vddgfx", "amdgpu_sclk", "cpu_load"}
        for _ in range(6):
            self.feed(amdgpu_vddgfx=99, amdgpu_sclk=99999, cpu_load=100)
        self.assertEqual(self.sent, [])

    def test_a_fan_that_never_spun_does_not_alarm(self):
        for _ in range(5):
            self.feed(fan1=0)
        self.assertEqual(self.sent, [])

    def test_a_fan_that_stops_alarms(self):
        self.feed(fan1=1200)
        for _ in range(4):
            self.feed(fan1=0)
        self.assertEqual(self.sent[0][1], "FAN1 stopped")

    def test_any_fan_gauge_is_watched_not_just_the_first_two(self):
        self.shown = {"fan4"}
        self.feed(fan4=800)
        for _ in range(4):
            self.feed(fan4=0)
        self.assertEqual(self.sent[0][1], "FAN4 stopped")

    def test_gauges_in_other_windows_are_not_alerted_here(self):
        self.shown = {"fan1"}
        for _ in range(6):
            self.feed(cpu_temp=95)
        self.assertEqual(self.sent, [])

    def test_popping_a_gauge_out_clears_its_alarm(self):
        for _ in range(4):
            self.feed(cpu_temp=95)
        self.assertIn("cpu_temp", self.alerts.active)
        self.shown = {"fan1"}
        self.feed(cpu_temp=95)
        self.assertNotIn("cpu_temp", self.alerts.active)


class LauncherTests(unittest.TestCase):
    def test_icon_is_valid_svg_even_with_odd_names(self):
        svg = gd.make_icon_svg("A&B <TEMP>", {k: v for k, v in gd.DEFAULT_COLORS.items()})
        ET.fromstring(svg)

    def test_exec_quoting(self):
        self.assertEqual(gd._exec_quote("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(gd._exec_quote("/home/my user/gd.py"), '"/home/my user/gd.py"')

    def test_demo_sensors_are_all_valid_application_ids(self):
        for s in gd.demo_catalog():
            self.assertTrue(gd.Gio.Application.id_is_valid(f"{gd.APP_ID}.{s['key']}"), s["key"])


if __name__ == "__main__":
    unittest.main()
