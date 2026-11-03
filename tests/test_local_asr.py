from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("faster_whisper")

MODEL_DIR = Path(__file__).resolve().parent.parent / "vendor" / "whisper" / "base"


def test_local_whisper_transcribes_synthetic_speech(tmp_path: Path, ffmpeg_bin: str) -> None:
    """使用内嵌 base 模型 + flite 合成语音，验证完全本地的 ASR 链路。"""
    if not (MODEL_DIR / "model.bin").exists():
        pytest.skip("vendor/whisper/base 不存在；先运行 prepare_vendor.py")

    wav = tmp_path / "speech.wav"
    result = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "flite=text='hello world this is smartcut local transcription':voice=slt",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(wav),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or not wav.exists():
        pytest.skip("当前 ffmpeg 构建不支持 flite 滤镜")

    from smartcut.perception.transcribe import FasterWhisperTranscriber

    transcriber = FasterWhisperTranscriber(
        model_size=str(MODEL_DIR),
        device="cpu",
        compute_type="int8",
    )
    transcript = transcriber.transcribe(wav)

    assert transcript.degraded is False
    assert transcript.segments
    text = " ".join(segment.text.lower() for segment in transcript.segments)
    assert any(
        word in text for word in ("hello", "world", "smartcut", "transcription")
    ), text
