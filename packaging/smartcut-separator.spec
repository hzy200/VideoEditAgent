# -*- mode: python ; coding: utf-8 -*-
"""Demucs 伴生 worker：独立 torch/CUDA 环境，主程序不导入 torch。"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve().parent
model_dir = ROOT / "vendor" / "demucs_models"
if not model_dir.exists():
    raise SystemExit(
        "缺少 Demucs 模型。先运行: python scripts/prepare_demucs_models.py"
    )

datas = [(str(model_dir), "models/demucs")]
binaries = []
hiddenimports = ["torch", "torch.cuda", "torch.backends"]
hiddenimports += collect_submodules("demucs")
for package in (
    "demucs",
    "sphn",
    "safetensors",
    "julius",
    "lameenc",
    "einops",
    "numpy",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception:
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
try:
    binaries += collect_dynamic_libs("torch")
    datas += collect_data_files("torch")
except Exception:
    pass

a = Analysis(
    [str(ROOT / "src" / "smartcut" / "separation_worker.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "fastapi",
        "starlette",
        "uvicorn",
        "multipart",
        "pytest",
        "cv2",
        "faster_whisper",
        "ctranslate2",
        "opencv_python",
        "scenedetect",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmartCut-Separator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 由主程序以 CREATE_NO_WINDOW 启动；保留控制台语义以支持 stdout/stderr 管道。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SmartCut-Separator",
)
