"""准备打包资源：内嵌 ffmpeg/ffprobe 与本地 faster-whisper 模型。

用法：
    conda activate video_edit
    python scripts/prepare_vendor.py
    python scripts/prepare_vendor.py --ffmpeg-only
    python scripts/prepare_vendor.py --whisper-only --model base --source modelscope

国内网络访问 HuggingFace 不稳定时，默认使用 ModelScope 镜像；
如可直连 HuggingFace，可加 --source hf。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLATFORM_DIR = "win64" if os.name == "nt" else sys.platform
FFMPEG_DIR = ROOT / "vendor" / "ffmpeg" / PLATFORM_DIR
WHISPER_DIR = ROOT / "vendor" / "whisper"
MODELSCOPE_BASE = "https://www.modelscope.cn/api/v1/models"
MODELSCOPE_REPO = "Systran/faster-whisper-{model}"
SKIP_FILES = {".gitattributes", "README.md"}


def _binary_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def prepare_ffmpeg(force: bool = False) -> int:
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in ("ffmpeg", "ffprobe"):
        source = shutil.which(name)
        if not source:
            missing.append(name)
            continue
        destination = FFMPEG_DIR / _binary_name(name)
        if destination.exists() and not force:
            print(f"[ffmpeg] 已存在: {destination}")
        else:
            shutil.copy2(source, destination)
            print(f"[ffmpeg] 复制 {source} -> {destination}")
        try:
            result = subprocess.run(
                [str(destination), "-version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                print(f"[ffmpeg] 警告: {destination} 无法正常运行")
        except Exception as exc:
            print(f"[ffmpeg] 警告: 无法验证 {destination}: {exc}")

    if missing:
        print(
            "[ffmpeg] 未找到: "
            + ", ".join(missing)
            + "。请安装 ffmpeg 或手动放入 "
            + str(FFMPEG_DIR)
        )
        return 1
    return 0


def _download_huggingface(model: str, target: Path) -> int:
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        print(
            "[whisper] 未安装 faster-whisper："
            'python -m pip install -e ".[transcribe]"'
        )
        return 1
    download_model(model, output_dir=str(target))
    return 0


def _download_modelscope(model: str, target: Path) -> int:
    try:
        import httpx
    except ImportError:
        print("[whisper] 需要 httpx（项目核心依赖）")
        return 1

    repo = MODELSCOPE_REPO.format(model=model)
    list_url = f"{MODELSCOPE_BASE}/{repo}/repo/files"
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            listing = client.get(
                list_url,
                params={"Revision": "master", "Recursive": "true"},
            )
            listing.raise_for_status()
            files = listing.json()["Data"]["Files"]
            for item in files:
                path = item.get("Path")
                if not path or path in SKIP_FILES or item.get("Type") != "blob":
                    continue
                destination = target / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.stat().st_size == item.get("Size"):
                    print(f"[whisper] 已存在: {path}")
                    continue
                print(f"[whisper] 下载 {path} ({item.get('Size', 0)} bytes)")
                temporary = destination.with_suffix(destination.suffix + ".part")
                with client.stream(
                    "GET",
                    f"{MODELSCOPE_BASE}/{repo}/repo",
                    params={"Revision": "master", "FilePath": path},
                ) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            handle.write(chunk)
                os.replace(temporary, destination)
    except Exception as exc:
        print(f"[whisper] ModelScope 下载失败: {exc}")
        return 1

    if not (target / "model.bin").exists():
        print("[whisper] 下载完成但缺少 model.bin")
        return 1
    return 0


def prepare_whisper(model: str, force: bool = False, source: str = "modelscope") -> int:
    target = WHISPER_DIR / model
    if (target / "model.bin").exists() and not force:
        print(f"[whisper] 已存在: {target}")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    print(f"[whisper] 准备 {model} -> {target}（source={source}）")
    if source == "modelscope":
        code = _download_modelscope(model, target)
    else:
        code = _download_huggingface(model, target)
    if code == 0:
        print(f"[whisper] 完成: {target}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="准备 SmartCut 打包资源")
    parser.add_argument("--ffmpeg-only", action="store_true")
    parser.add_argument("--whisper-only", action="store_true")
    parser.add_argument("--model", default="base", help="faster-whisper 模型: tiny/base/small")
    parser.add_argument(
        "--source",
        default="modelscope",
        choices=["modelscope", "hf"],
        help="模型下载源：modelscope（国内推荐）或 hf",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.ffmpeg_only and args.whisper_only:
        parser.error("--ffmpeg-only 与 --whisper-only 不能同时使用")

    code = 0
    if not args.whisper_only:
        code = max(code, prepare_ffmpeg(args.force))
    if not args.ffmpeg_only:
        code = max(code, prepare_whisper(args.model, args.force, args.source))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
