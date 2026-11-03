# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder 构建：内嵌 ffmpeg/ffprobe 与本地 whisper 模型。

构建：
    python scripts/prepare_vendor.py
    python -m PyInstaller packaging/smartcut.spec --noconfirm --clean
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

ffmpeg = ROOT / "vendor" / "ffmpeg" / "win64" / "ffmpeg.exe"
ffprobe = ROOT / "vendor" / "ffmpeg" / "win64" / "ffprobe.exe"
whisper_model = ROOT / "vendor" / "whisper" / "base"

if not ffmpeg.exists() or not ffprobe.exists():
    raise SystemExit(
        "缺少内嵌 ffmpeg。先运行: python scripts/prepare_vendor.py --ffmpeg-only"
    )
if not whisper_model.exists():
    raise SystemExit(
        "缺少内嵌 whisper 模型。先运行: python scripts/prepare_vendor.py --whisper-only"
    )

binaries = [
    (str(ffmpeg), "bin"),
    (str(ffprobe), "bin"),
]
datas = [
    (str(whisper_model), "models/whisper/base"),
    (str(ROOT / "src" / "smartcut" / "policies"), "smartcut/policies"),
]
hiddenimports = [
    "keyring.backends.Windows",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
]
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += collect_submodules("scenedetect")
datas += collect_data_files("cv2")

for package in (
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "av",
    "onnxruntime",
    "huggingface_hub",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception:
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    [str(ROOT / "src" / "smartcut" / "desktop" / "app.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "fastapi",
        "starlette",
        "uvicorn",
        "multipart",
        "pytest",
        "opentimelineio",
        # 分离器在伴生 worker 中运行，主程序绝不打包 torch/demucs。
        "torch",
        "torchaudio",
        "demucs",
        "sphn",
        "safetensors",
        "julius",
        "lameenc",
        "einops",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmartCut",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SmartCut",
)
