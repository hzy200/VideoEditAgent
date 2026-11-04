"""证据窗口选择：粗扫 → 去重种子 → 合并窗口 → top-K 硬预算。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from smartcut.contracts.chapters import ChapterPlan
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript

DEFAULT_PADDING_SECONDS = 2.0
DEFAULT_MAX_WINDOW_SECONDS = 8.0
MAX_TRANSCRIPT_SEEDS = 64
MAX_FACE_SEEDS = 48


@dataclass(frozen=True)
class _Seed:
    t: Fraction
    weight: float
    reason: str


@dataclass
class EvidenceWindow:
    start: Fraction
    end: Fraction
    score: float
    fps: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    tile_count: int | None = None

    def duration(self) -> Fraction:
        return self.end - self.start

    def tiles(self) -> int:
        if self.tile_count is not None:
            return max(1, self.tile_count)
        return max(1, math.ceil(float(self.duration()) * self.fps))

    def as_dict(self) -> dict:
        return {
            "start": RationalTime.from_fraction(self.start).model_dump(mode="json"),
            "end": RationalTime.from_fraction(self.end).model_dump(mode="json"),
            "fps": self.fps,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "tiles": self.tiles(),
        }


@dataclass
class EvidenceSelection:
    windows: list[EvidenceWindow]
    seed_count: int
    dropped_windows: int
    drop_reasons: dict[str, int]
    estimated_tiles: int
    estimated_tokens: int

    def as_dict(self) -> dict:
        return {
            "windows": [window.as_dict() for window in self.windows],
            "seed_count": self.seed_count,
            "dropped_windows": self.dropped_windows,
            "drop_reasons": self.drop_reasons,
            "estimated_tiles": self.estimated_tiles,
            "estimated_tokens": self.estimated_tokens,
        }

    def signature(self) -> list[tuple]:
        return [
            (window.start, window.end, round(window.fps, 4))
            for window in self.windows
        ]


def _to_fraction(value: Any) -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, RationalTime):
        return value.as_fraction()
    try:
        return RationalTime.model_validate(value).as_fraction()
    except (TypeError, ValueError):
        return None


def _chapter_items(chapters: ChapterPlan | dict | None) -> list[dict]:
    if chapters is None:
        return []
    if isinstance(chapters, ChapterPlan):
        return [chapter.model_dump(mode="json") for chapter in chapters.chapters]
    return list(chapters.get("chapters") or [])


def _sample(values: list[Any], limit: int) -> list[Any]:
    if len(values) <= limit:
        return values
    step = max(1, len(values) // limit)
    return values[::step][:limit]


def _seeds(
    frames: list[FrameRef],
    duration: Fraction,
    *,
    shots: ShotList | None,
    quality: QualityReport | None,
    chapters: list[dict],
    transcript: Transcript | None,
    faces: FaceReport | None,
) -> list[_Seed]:
    seeds: list[_Seed] = []
    if shots is not None:
        for shot in shots.shots:
            seeds.append(_Seed(shot.t.as_fraction(), 3.0, "scene"))
    if quality is not None:
        for mark in quality.marks:
            seeds.append(
                _Seed(mark.t.as_fraction(), 2.0, f"quality:{mark.kind}")
            )
    for chapter in chapters:
        start = _to_fraction(chapter.get("start"))
        end = _to_fraction(chapter.get("end"))
        if start is not None:
            seeds.append(_Seed(start, 2.0, "chapter_start"))
        if end is not None:
            seeds.append(_Seed(end, 1.5, "chapter_end"))
    if transcript is not None:
        segments = _sample(
            [segment for segment in transcript.segments if segment.text.strip()],
            MAX_TRANSCRIPT_SEEDS,
        )
        for segment in segments:
            seeds.append(_Seed(segment.start.as_fraction(), 1.2, "transcript"))
    if faces is not None:
        for anchor in _sample(list(faces.anchors), MAX_FACE_SEEDS):
            seeds.append(_Seed(anchor.t.as_fraction(), 1.0, "face"))

    seeds = [
        _Seed(max(Fraction(0), min(seed.t, duration)), seed.weight, seed.reason)
        for seed in seeds
    ]
    if seeds:
        return seeds

    # 没有结构种子时用粗扫帧做均匀覆盖。
    if frames:
        for frame in _sample(list(frames), 4):
            seeds.append(_Seed(frame.t.as_fraction(), 0.8, "coverage"))
        return seeds
    count = 4
    for index in range(count):
        moment = duration * Fraction(index, max(1, count - 1) if count > 1 else 1)
        seeds.append(_Seed(moment, 0.8, "coverage"))
    return seeds


def _merge_windows(
    seeds: list[_Seed],
    duration: Fraction,
    *,
    padding: Fraction,
    max_window_seconds: Fraction,
) -> list[EvidenceWindow]:
    if not seeds:
        return []
    ordered = sorted(seeds, key=lambda seed: seed.t)
    windows: list[EvidenceWindow] = []
    current_start: Fraction | None = None
    current_end: Fraction | None = None
    current_weight = 0.0
    current_reasons: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_end, current_weight, current_reasons
        if current_start is None or current_end is None:
            return
        windows.append(
            EvidenceWindow(
                start=current_start,
                end=current_end,
                score=current_weight,
                fps=1.0,
                reasons=tuple(dict.fromkeys(current_reasons)),
            )
        )
        current_start = None
        current_end = None
        current_weight = 0.0
        current_reasons = []

    for seed in ordered:
        start = max(Fraction(0), seed.t - padding)
        end = min(duration, seed.t + padding)
        if current_start is None or current_end is None:
            current_start, current_end = start, end
            current_weight = seed.weight
            current_reasons = [seed.reason]
            continue
        merged_end = max(current_end, end)
        if (
            start <= current_end + padding
            and merged_end - current_start <= max_window_seconds
        ):
            current_end = merged_end
            current_weight += seed.weight
            current_reasons.append(seed.reason)
        else:
            flush()
            current_start, current_end = start, end
            current_weight = seed.weight
            current_reasons = [seed.reason]
    flush()
    return windows


def select_evidence_windows(
    frames: list[FrameRef],
    *,
    duration: RationalTime,
    shots: ShotList | None = None,
    quality: QualityReport | None = None,
    chapters: ChapterPlan | dict | None = None,
    chapter_ids: list[str] | None = None,
    transcript: Transcript | None = None,
    faces: FaceReport | None = None,
    max_images: int = 7,
    max_windows: int = 4,
    fps: float = 2.0,
    padding_seconds: float = DEFAULT_PADDING_SECONDS,
    max_window_seconds: float = DEFAULT_MAX_WINDOW_SECONDS,
) -> EvidenceSelection:
    """选择证据窗口；任何超出图片预算的窗口都会被丢弃并记录原因。"""
    total = max(Fraction(0), duration.as_fraction())
    if total <= 0:
        raise ValueError("素材时长为 0，无法选择证据窗口")
    max_images = max(1, int(max_images))
    max_windows = max(1, int(max_windows))
    fps = max(0.25, float(fps))
    padding = Fraction(str(padding_seconds))
    window_limit = Fraction(str(max_window_seconds))

    items = _chapter_items(chapters)
    if chapter_ids:
        wanted = set(chapter_ids)
        items = [item for item in items if item.get("id") in wanted]
    seeds = _seeds(
        frames,
        total,
        shots=shots,
        quality=quality,
        chapters=items,
        transcript=transcript,
        faces=faces,
    )
    windows = _merge_windows(
        seeds,
        total,
        padding=padding,
        max_window_seconds=window_limit,
    )

    ranked = sorted(
        windows,
        key=lambda window: (-window.score, window.duration(), window.start),
    )
    selected: list[EvidenceWindow] = []
    remaining_tiles = max_images
    remaining_windows = max_windows
    dropped = 0
    drop_reasons: dict[str, int] = {}

    for window in ranked:
        if remaining_windows <= 0 or remaining_tiles <= 0:
            dropped += 1
            drop_reasons["budget"] = drop_reasons.get("budget", 0) + 1
            continue
        estimated = max(1, math.ceil(float(window.duration()) * fps))
        adjusted_fps = fps
        if estimated > remaining_tiles:
            adjusted_fps = remaining_tiles / max(float(window.duration()), 1e-6)
            if adjusted_fps < 0.5:
                dropped += 1
                drop_reasons["budget"] = drop_reasons.get("budget", 0) + 1
                continue
            estimated = max(1, remaining_tiles)
        selected.append(
            EvidenceWindow(
                start=window.start,
                end=window.end,
                score=window.score,
                fps=round(adjusted_fps, 4),
                reasons=window.reasons,
                tile_count=estimated,
            )
        )
        remaining_tiles -= estimated
        remaining_windows -= 1

    if not selected:
        best = max(windows, key=lambda window: window.score) if windows else None
        start = best.start if best is not None else Fraction(0)
        end = best.end if best is not None else total
        adjusted_fps = max_images / max(float(end - start), 1e-6)
        selected.append(
            EvidenceWindow(
                start=start,
                end=end,
                score=best.score if best is not None else 0.5,
                fps=max(0.5, min(fps, round(adjusted_fps, 4))),
                reasons=best.reasons if best is not None else ("coverage",),
                tile_count=max_images,
            )
        )
        remaining_tiles = 0

    # 预算仍有剩余时补覆盖窗口，避免长视频只覆盖开头。
    if remaining_tiles > 0 and remaining_windows > 0:
        selected.sort(key=lambda window: window.start)
        gaps: list[tuple[Fraction, Fraction]] = []
        cursor = Fraction(0)
        for window in selected:
            if window.start > cursor:
                gaps.append((cursor, window.start))
            cursor = max(cursor, window.end)
        if cursor < total:
            gaps.append((cursor, total))
        for start, end in gaps:
            if remaining_tiles <= 0 or remaining_windows <= 0:
                break
            if end - start < Fraction(1):
                continue
            span = min(end - start, Fraction(str(max_window_seconds)))
            window = EvidenceWindow(
                start=start,
                end=start + span,
                score=0.5,
                fps=1.0,
                reasons=("coverage",),
            )
            tiles = window.tiles()
            if tiles > remaining_tiles:
                break
            selected.append(window)
            remaining_tiles -= tiles
            remaining_windows -= 1

    selected.sort(key=lambda window: window.start)
    estimated_tiles = sum(window.tiles() for window in selected)
    return EvidenceSelection(
        windows=selected,
        seed_count=len(seeds),
        dropped_windows=dropped,
        drop_reasons=drop_reasons,
        estimated_tiles=estimated_tiles,
        estimated_tokens=estimated_tiles * 1024,
    )
