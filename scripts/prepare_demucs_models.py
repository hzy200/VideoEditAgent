"""下载 Demucs 4.1 htdemucs 权重到 vendor/demucs_models。"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "vendor" / "demucs_models"
MODEL = "955717e8-8726e21a"
URL = f"https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/{MODEL}.th"


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                print(f"\r{done / total * 100:5.1f}%  {done / 1048576:.1f} MB", end="")
    print()
    expected = MODEL.rsplit("-", 1)[1]
    actual = digest.hexdigest()[: len(expected)]
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"校验失败: 期望 {expected}, 实际 {actual}")
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    args.dest.mkdir(parents=True, exist_ok=True)
    model_path = args.dest / f"{MODEL}.th"
    if model_path.exists() and not args.force:
        print(f"模型已存在: {model_path}")
    else:
        print(f"下载 {URL}")
        _download(URL, model_path)

    bag = args.dest / "htdemucs.yaml"
    bag.write_text("models: ['955717e8']\n", encoding="utf-8")
    print(f"模型目录就绪: {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
