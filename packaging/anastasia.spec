# PyInstaller spec for Anastasia (Anna).
# Build from the repository root with:
#   .\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\anastasia.spec

from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent


def add_dir(path: Path, dest: str):
    return (str(path), dest) if path.exists() else None


datas = []
for item in (
    add_dir(ROOT / "app" / "web", "app/web"),
    add_dir(ROOT / "assets", "assets"),
):
    if item:
        datas.append(item)

avatar = ROOT / "assets" / "avatar.png"
if avatar.exists():
    datas.append((str(avatar), "app/web/assets"))

example_config = ROOT / "config.example.json"
if example_config.exists():
    datas.append((str(example_config), "."))

try:
    datas.extend(copy_metadata("piper-tts"))
except Exception:
    pass

# Never bundle app/data/config.json, memory.json, history.sqlite, or API keys.
# Set ANNA_INCLUDE_LOCAL_VOICE_DATA=1 only for a personal/offline installer.
if os.environ.get("ANNA_INCLUDE_LOCAL_VOICE_DATA") == "1":
    for source, dest in (
        (ROOT / "app" / "data" / "voices", "app/data/voices"),
        (ROOT / "app" / "data" / "piper", "app/data/piper"),
    ):
        item = add_dir(source, dest)
        if item:
            datas.append(item)

hiddenimports = [
    "webview.platforms.edgechromium",
    "websocket",
    "sounddevice",
    "numpy",
    "faster_whisper",
    "piper",
    "piper.config",
]
for package in ("webview", "piper"):
    try:
        hiddenimports.extend(collect_submodules(package))
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Anastasia",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Anastasia",
)