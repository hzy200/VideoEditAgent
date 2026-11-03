"""伴生分离 worker：唯一允许 import torch/demucs 的入口。

由 SmartCut-Separator.exe 调用；主程序只通过子进程使用它。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def _model_dir() -> Path | None:
    configured = os.getenv("SMARTCUT_DEMUCS_MODEL_DIR")
    if configured:
        return Path(configured)
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = root / "models" / "demucs"
        if candidate.exists():
            return candidate
    candidate = Path(__file__).resolve().parents[2] / "vendor" / "demucs_models"
    return candidate if candidate.exists() else None


def _write_progress(path: Path | None, percent: int, message: str) -> None:
    if path is None:
        return
    payload = {"percent": max(0, min(99, int(percent))), "message": message}
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def _save(source, destination: Path, samplerate: int) -> None:
    from demucs.api import save_audio

    destination.parent.mkdir(parents=True, exist_ok=True)
    save_audio(source, str(destination), samplerate=samplerate, bits_per_sample=16)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser("smartcut-separator")
    parser.add_argument("tracks", nargs="*", type=Path)
    parser.add_argument("--two-stems", dest="stem", default=None)
    parser.add_argument("-n", "--name", default="htdemucs")
    parser.add_argument("-o", "--out", type=Path, default=None)
    parser.add_argument("-d", "--device", default="auto")
    parser.add_argument("--progress-file", type=Path, default=None)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)

    if args.probe:
        import torch

        print(
            json.dumps(
                {
                    "torch": torch.__version__,
                    "cuda_available": bool(torch.cuda.is_available()),
                    "gpu": (
                        torch.cuda.get_device_name(0)
                        if torch.cuda.is_available()
                        else None
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.out is None or not args.tracks:
        parser.error("需要 -o 与至少一个输入文件")

    progress_file = args.progress_file or (
        Path(os.environ["SMARTCUT_DEMUCS_PROGRESS_FILE"])
        if os.getenv("SMARTCUT_DEMUCS_PROGRESS_FILE")
        else None
    )
    try:
        import torch
        from demucs.api import Separator

        if args.device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            device = args.device
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("请求 CUDA，但当前 PyTorch/驱动不可用")

        model_dir = _model_dir()
        _write_progress(progress_file, 5, f"加载模型 {args.name}（{device}）")

        def callback(info: dict) -> None:
            total = max(1, int(info.get("audio_length") or 1))
            done = int(info.get("segment_offset") or 0)
            percent = 5 + int(85 * min(1.0, done / total))
            state = info.get("state")
            _write_progress(
                progress_file,
                percent,
                f"分离中（{device}，{state or ''}）",
            )

        separator = Separator(
            model=args.name,
            repo=model_dir,
            device=device,
            progress=False,
            callback=callback,
        )
        for track in args.tracks:
            if not track.exists():
                raise FileNotFoundError(f"输入不存在: {track}")
            _write_progress(progress_file, 10, f"读取 {track.name}")
            origin, stems = separator.separate_audio_file(track)
            output_dir = args.out / args.name / track.stem
            samplerate = int(separator.samplerate)
            if args.stem is not None:
                if args.stem not in stems:
                    raise ValueError(
                        f"模型不包含 {args.stem!r}，可选: {', '.join(stems)}"
                    )
                selected = stems[args.stem]
                _save(selected, output_dir / f"{args.stem}.wav", samplerate)
                others = [value for key, value in stems.items() if key != args.stem]
                if others:
                    other = others[0]
                    for value in others[1:]:
                        other = other + value
                else:
                    other = origin - selected
                _save(other, output_dir / f"no_{args.stem}.wav", samplerate)
            else:
                for name, source in stems.items():
                    _save(source, output_dir / f"{name}.wav", samplerate)
        metadata = {
            "separator": "demucs",
            "model": args.name,
            "stems": 2 if args.stem else 4,
            "device": device,
            "torch": torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
            "gpu": (
                torch.cuda.get_device_name(0)
                if torch.cuda.is_available()
                else None
            ),
        }
        try:
            (args.out / "result.json").write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        _write_progress(
            progress_file,
            95,
            f"分离完成（{device}）",
        )
        return 0
    except Exception as exc:
        _write_progress(progress_file, 99, f"失败: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
