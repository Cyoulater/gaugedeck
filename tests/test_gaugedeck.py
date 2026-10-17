"""Tests for the parts of GaugeDeck that need no screen: layout, looks, alerts, launchers.
Run with:  python3 -m unittest discover -s tests -v"""
import importlib.util
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

_TMP = tempfile.mkdtemp(prefix="gaugedeck-test-")
os.environ["GAUGEDECK_CONFIG_DIR"] = _TMP
sys.argv = ["gaugedeck"]
_spec = importlib.util.spec_from_file_location(
    "gaugedeck", os.path.join(os.path.dirname(__file__), "..", "gaugedeck.py"))
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)


class LayoutTests(unittest.TestCase):
    def test_default_layout_matches_the_original_positions(self):
        gauges = [g for g in gd.GAUGES if g["key"] in
                  ("cpu_temp", "cpu_load", "gpu_temp", "fan1", "fan2", "gpu_fan", "ram", "board_temp")]
        placed, height = gd.deck_layout(gauges)
        got = [(g["key"], x, y, r) for g, x, y, r in placed]
        self.assertEqual(got, [
            ("cpu_temp", 150, 150, 110), ("cpu_load", 390, 150, 110),
            ("gpu_temp", 100, 360, 80), ("fan1", 270, 360, 80), ("fan2", 440, 360, 80),
            ("gpu_fan", 100, 540, 80), ("ram", 270, 540, 80), ("board_temp", 440, 540, 80)])
        self.assertEqual(height, 690)

    def test_short_last_row_is_centered(self):
        gauges = [gd._gauge(f"s{i}", "S", "", 0, 1, 1) for i in range(5)]
        placed, _ = gd.deck_layout(gauges)
        self.assertEqual([x for _g, x, y, _r in placed if y == placed[-1][2]], [185.0, 355.0])

    def test_empty_deck_has_a_height(self):
        self.assertEqual(gd.deck_layout([]), ([], 200))

    def test_clicks_find_the_right_gauge(self):
        placed, bh = gd.deck_layout([g for g in gd.GAUGES if g["key"] in ("cpu_temp", "cpu_load")])
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

    def test_new_style_keys_are_checked(self):
        st = gd.clean_style({"display": "graph", "glow": 5, "image": " ~/p.png ", "image_dim": -1,
                             "bg_image": 7, "bg_dim": 0.3})
        self.assertEqual(st, {"display": "graph", "glow": 1.0, "image": "~/p.png", "image_dim": 0.0, "bg_dim": 0.3})
        self.assertEqual(gd.clean_style({"display": "hologram"}), {})

    def test_every_preset_is_valid(self):
        for name, p in gd.PRESETS.items():
            self.assertEqual(gd.clean_colors(p["colors"]), p["colors"], name)
            self.assertEqual(gd.clean_style(p["style"]), p["style"], name)


class DrawingTests(unittest.TestCase):
    """Every look draws every gauge style without errors, with and without data."""

    def test_all_presets_and_displays_draw(self):
        import cairo
        theme = gd.Theme(os.path.join(_TMP, "draw.json"))
        gauge = gd.GAUGES[0]
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
        gd.draw_gauge(cairo.Context(out), 50, 50, 45, gd.GAUGES[0], None, theme.look(None))
        r, g, b = out.get_data()[(10 * 100 + 50) * 4 + 2], out.get_data()[(10 * 100 + 50) * 4 + 1], 0
        self.assertGreater(r, 150)                # red picture shows near the top of the face


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        gd.play_alarm = lambda: None
        gd.CFG["alerts"] = "on"
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

    def test_a_fan_that_never_spun_does_not_alarm(self):
        for _ in range(5):
            self.feed(fan1=0)
        self.assertEqual(self.sent, [])

    def test_a_fan_that_stops_alarms(self):
        self.feed(fan1=1200)
        for _ in range(4):
            self.feed(fan1=0)
        self.assertEqual(self.sent[0][1], "FAN1 stopped")

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

    def test_desktop_ids_are_valid_application_ids(self):
        for g in gd.GAUGES:
            self.assertTrue(gd.Gio.Application.id_is_valid(f"{gd.APP_ID}.{g['key']}"), g["key"])


if __name__ == "__main__":
    unittest.main()
