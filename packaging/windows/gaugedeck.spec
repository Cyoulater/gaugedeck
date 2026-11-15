# PyInstaller recipe for the Windows app. Run in an MSYS2 UCRT64 shell (see .github/workflows/release.yml):
#   pyinstaller --noconfirm --distpath dist --workpath build packaging/windows/gaugedeck.spec
# Makes dist/GaugeDeck/: GaugeDeck.exe plus Python, GTK 4 and everything they need.
import os
import shutil

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
binaries = []
gdbus = shutil.which("gdbus.exe")      # lets GaugeDeck's windows find each other (pop-out gauges, Bring back)
if gdbus:
    binaries.append((gdbus, "."))

a = Analysis(
    [os.path.join(root, "gaugedeck.py")],
    pathex=[root],
    binaries=binaries,
    datas=[(os.path.join(root, "io.github.cyoulater.GaugeDeck.svg"), "share/icons/hicolor/scalable/apps")],
    hiddenimports=["gaugedeck_windows", "cairo"],
    hooksconfig={"gi": {"module-versions": {"Gtk": "4.0", "Gdk": "4.0"},
                        "icons": ["Adwaita", "hicolor"], "themes": ["Adwaita"], "languages": ["en_US"]}},
    excludes=["tkinter", "unittest", "pydoc"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GaugeDeck",
          icon=os.path.join(SPECPATH, "gaugedeck.ico"), console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="GaugeDeck")
