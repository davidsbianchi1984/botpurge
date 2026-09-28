# PyInstaller spec for the Bot Purge desktop app.  Build:  pyinstaller packaging/botpurge.spec
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = [(str(root / "botpurge" / "web"), "botpurge/web"), (str(root / "botpurge" / "license_public.txt"), "botpurge"), (str(root / "extension"), "extension")]
binaries, hiddenimports = [], []
hiddenimports += collect_submodules("uvicorn") + ["multipart", "python_multipart"]
for pkg in ("playwright", "webview"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
try:
    datas += collect_data_files("cv2", subdir="data")  # the bundled face-detection models
except Exception:
    pass

a = Analysis([str(root / "packaging" / "launch.py")], pathex=[str(root)], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, excludes=["tkinter", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="BotPurge", console=False, upx=False,
          icon=None, disable_windowed_traceback=False)
if sys.platform == "darwin":
    app = BUNDLE(exe, name="Bot Purge.app", bundle_identifier="app.botpurge.desktop",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.2.0"})
