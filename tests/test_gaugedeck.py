"""Tests for the parts of GaugeDeck that need no screen: sensors, layout, looks, alerts, launchers.
Run with:  python3 -m unittest discover -s tests -v"""
import importlib.util
import os
import stat
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
gd._PCI_IDS = {("1002", "73bf"): "Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]"}


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
        self.assertIn("Radeon RX 6800", self.by["amdgpu_junction"]["name"])
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
