"""Opens GaugeDeck's deck window on Windows for a few seconds, then fails if anything went wrong.
Used by CI:  python packaging/windows/smoke.py   (from the repository folder, in MSYS2 UCRT64)"""
import os
import sys
import traceback

errors = []


def _hook(*exc):
    errors.append(exc)
    traceback.print_exception(*exc)


sys.excepthook = _hook                    # errors inside GTK callbacks end up here
sys.argv = ["gaugedeck"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import gaugedeck as gd                    # noqa: E402  (installs the Windows sensor layer)
from gi.repository import GLib            # noqa: E402

print(f"GaugeDeck {gd.VERSION}: {len(gd.CATALOG)} sensors")
sensors = gd.Sensors(gd.CATALOG)
sensors.read_all()
for key, v in sensors.read_all().items():
    print(f"  {key:20} {v}")
keys = {s["key"] for s in gd.CATALOG}
errors += [f"missing sensor {k}" for k in ("cpu_load", "ram", "disk_root") if k not in keys]

app = gd.GaugeApp()
GLib.timeout_add_seconds(8, lambda: app.quit() or False)
status = app.run([sys.argv[0]])
if errors or status:
    print("FAILED:", errors or f"exit status {status}")
    sys.exit(1)
print("Deck opened and ran for 8 seconds without errors.")
