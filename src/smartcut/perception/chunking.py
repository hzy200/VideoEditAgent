"""长音频分块与转写合并。"""

from __future__ import annotations

import array
import math
import wave
from dataclasses import dataclass
from pathlib import Path

from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word


@dataclass(frozen=True)
class AudioChunk:
    index: int
    start: float
    end: float
    path: Path


def split_wav(
    audio_path: Path,
    output_dir: Path,
    chunk_seconds: float = 300.0,
    overlap_seconds: float = 1.0,
) -> list[AudioChunk]:
    """把 16k mono PCM WAV 切成有少量重叠的块。"""
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds 必须大于 0")
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []

    with wave.open(str(audio_path), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        if rate != 16_000 or channels != 1 or sample_width != 2:
            raise ValueError("分块要求 16k 单声道 16-bit PCM WAV")
        total_frames = source.getnframes()
        frames_per_chunk = max(1, int(chunk_seconds * rate))
        overlap_frames = max(0, int(overlap_seconds * rate))

        for index, nominal_start in enumerate(range(0, total_frames, frames_per_chunk)):
            read_start = max(0, nominal_start - overlap_frames)
            read_end = min(total_frames, nominal_start + frames_per_chunk + overlap_frames)
            if read_end <= read_start:
                continue
            source.setpos(read_start)
            data = source.readframes(read_end - read_start)
            chunk_path = output_dir / f"chunk_{index:04d}.wav"
            with wave.open(str(chunk_path), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(rate)
                target.writeframes(data)
            chunks.append(
                AudioChunk(
                    index=index,
                    start=read_start / rate,
                    end=read_end / rate,
                    path=chunk_path,
                )
            )
    return chunks


def merge_transcripts(items: list[tuple[AudioChunk, Transcript]]) -> Transcript:
    words: list[Word] = []
    segments: list[TranscriptSegment] = []
    seen_words: set[tuple[float, str]] = set()
    seen_segments: set[tuple[float, float, str]] = set()
    language = None

    for chunk, transcript in items:
        if language is None and transcript.language:
            language = transcript.language
        for word in transcript.words:
            start = chunk.start + word.start.to_float()
            end = chunk.start + word.end.to_float()
            key = (round(start, 2), word.text.strip())
            if key in seen_words:
                continue
            seen_words.add(key)
            words.append(
                Word(
                    text=word.text,
                    start=RationalTime.from_seconds(start, rate=90_000),
                    end=RationalTime.from_seconds(end, rate=90_000),
                    probability=word.probability,
                )
            )
        for segment in transcript.segments:
            start = chunk.start + segment.start.to_float()
            end = chunk.start + segment.end.to_float()
            key = (round(start, 2), round(end, 2), segment.text.strip())
            if key in seen_segments:
                continue
            seen_segments.add(key)
            segments.append(
                TranscriptSegment(
                    id=segment.id,
                    text=segment.text,
                    start=RationalTime.from_seconds(start, rate=90_000),
                    end=RationalTime.from_seconds(end, rate=90_000),
                    words=[],
                )
            )

    words.sort(key=lambda item: item.start.as_fraction())
    segments.sort(key=lambda item: item.start.as_fraction())
    degraded = bool(items) and all(transcript.degraded for _, transcript in items)
    return Transcript(
        language=language,
        words=words,
        segments=segments,
        degraded=degraded,
        note=f"分块转写：{len(items)} 块" if len(items) > 1 else None,
    )


def wav_rms(path: Path, max_seconds: float = 600.0) -> float | None:
    """读取 16k mono 16-bit WAV 的归一化 RMS；仅用于静音块预筛选。"""
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getframerate() != 16_000
                or handle.getnchannels() != 1
                or handle.getsampwidth() != 2
            ):
                return None
            limit = max(1, int(handle.getframerate() * max(1.0, max_seconds)))
            raw = handle.readframes(min(handle.getnframes(), limit))
    except (wave.Error, OSError, ValueError):
        return None
    if not raw:
        return 0.0
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return 0.0
    mean_square = sum(value * value for value in samples) / len(samples)
    return math.sqrt(mean_square) / 32768.0


def is_silent_chunk(
    chunk: AudioChunk,
    *,
    threshold: float,
    min_seconds: float,
    max_seconds: float = 600.0,
) -> bool:
    if chunk.end - chunk.start < max(0.0, min_seconds):
        return False
    rms = wav_rms(chunk.path, max_seconds=max_seconds)
    return rms is not None and rms < threshold
