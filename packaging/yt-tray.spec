import os
import sys
from pathlib import Path

root = Path(SPECPATH).parent
pkg = Path(SPECPATH)

binaries = []
libs = os.environ.get("MPV_LIBS_DIR")
if libs:
    for f in sorted(Path(libs).resolve().iterdir()):
        if f.is_file():
            binaries.append((str(f), "."))

hidden = ["pypresence", "truststore", "mpv"]
excluded = ["yt_dlp"]
if sys.platform == "darwin":
    hidden += ["pystray._darwin", "AppKit", "Foundation", "objc", "MediaPlayer", "PyObjCTools.AppHelper"]
elif sys.platform == "win32":
    hidden += ["pystray._win32"]
else:
    hidden += ["gi", "gi.repository.Gio", "gi.repository.GLib", "Xlib", "pystray._xorg"]
    excluded += [
        "gi.repository.Gtk",
        "gi.repository.Gdk",
        "gi.repository.GdkPixbuf",
        "gi.repository.AppIndicator3",
        "gi.repository.AyatanaAppIndicator3",
        "pystray._appindicator",
        "pystray._gtk",
        "pystray._util.gtk",
    ]

a = Analysis(
    [str(root / "yt-tray.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=[],
    hiddenimports=hidden,
    excludes=excluded,
)
pyz = PYZ(a.pure)

icon = None
if sys.platform == "darwin":
    icon = str(pkg / "icon.icns")
elif sys.platform == "win32":
    icon = str(pkg / "icon.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yt-tray",
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="yt-tray")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="yt-tray.app",
        icon=icon,
        bundle_identifier="com.justinoros.yt-tray",
        info_plist={
            "LSUIElement": True,
            "CFBundleName": "yt-tray",
            "CFBundleDisplayName": "yt-tray",
            "NSHighResolutionCapable": True,
        },
    )
