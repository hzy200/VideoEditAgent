"""分层记忆索引构建：把章节/镜头/候选聚合成 source_hash 级索引。

纯函数：只读入已算好的阶段产物，不读文件、不调模型。
"""

from __future__ import annotations

from smartcut.contracts.chapters import ChapterPlan
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.memory import MemoryChapter, MemoryShot, VideoMemoryIndex
from smartcut.contracts.time import rt_from_seconds, seconds_from_mapping

__all__ = ["build_memory_index"]


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _chapter_items(chapters: dict | ChapterPlan | None) -> list[dict]:
    if chapters is None:
        return []
    if isinstance(chapters, ChapterPlan):
        return [item.model_dump(mode="json") for item in chapters.chapters]
    return list(chapters.get("chapters") or [])


def _shot_items(shots: dict | None) -> list[dict]:
    if not shots:
        return []
    return list(shots.get("shots") or [])


def _candidate_items(candidates: dict | None) -> list[dict]:
    if not candidates:
        return []
    return list(candidates.get("candidates") or [])


def _candidate_start(candidate: dict) -> float | None:
    source_range = candidate.get("source_range") or {}
    return seconds_from_mapping(source_range.get("start"))


def _visual_note_ids(visual_notes: dict | None) -> set[str]:
    if not visual_notes:
        return set()
    return {
        str(item.get("chapter_id"))
        for item in visual_notes.get("chapters") or []
        if item.get("chapter_id")
    }


def build_memory_index(
    *,
    facts: MediaFacts,
    source_hash: str,
    chapters: dict | ChapterPlan | None = None,
    shots: dict | None = None,
    candidates: dict | None = None,
    visual_notes: dict | None = None,
    transcript_segments: int = 0,
    cache_keys: dict[str, str] | None = None,
) -> VideoMemoryIndex:
    """按 source_hash 聚合章节/镜头/候选，生成可缓存的索引。"""
    chapter_items = _chapter_items(chapters)
    shot_items = _shot_items(shots)
    candidate_items = _candidate_items(candidates)
    note_ids = _visual_note_ids(visual_notes)

    candidate_starts = [
        value for value in (_candidate_start(item) for item in candidate_items) if value is not None
    ]

    memory_chapters: list[MemoryChapter] = []
    bounds: list[tuple[float, float]] = []
    for item in chapter_items:
        start = seconds_from_mapping(item.get("start"))
        end = seconds_from_mapping(item.get("end"))
        if start is None or end is None or end <= start:
            continue
        chapter_id = str(item.get("id") or f"ch_{len(memory_chapters) + 1:04d}")
        bounds.append((start, end))
        memory_chapters.append(
            MemoryChapter(
                id=chapter_id,
                start=rt_from_seconds(start),
                end=rt_from_seconds(end),
                title=str(item.get("title") or "")[:80],
                summary=(
                    str(item.get("summary"))[:300]
                    if item.get("summary") is not None
                    else None
                ),
                shot_count=_int(item.get("shot_count")),
                quality_marks=_int(item.get("quality_marks")),
                face_frames=_int(item.get("face_frames")),
                candidate_count=sum(1 for value in candidate_starts if start <= value < end),
                keyframe_count=len(item.get("keyframes") or []),
                has_visual_note=chapter_id in note_ids,
            )
        )

    memory_shots: list[MemoryShot] = []
    for index, item in enumerate(shot_items):
        moment = seconds_from_mapping(item.get("t"))
        if moment is None:
            continue
        chapter_id = None
        for (start, end), chapter in zip(bounds, memory_chapters):
            if start <= moment < end:
                chapter_id = chapter.id
                break
        confidence = None
        if item.get("confidence") is not None:
            try:
                value = float(item["confidence"])
            except (TypeError, ValueError):
                value = None
            if value is not None and 0.0 <= value <= 1.0:
                confidence = value
        memory_shots.append(
            MemoryShot(
                id=f"sh_{index + 1:04d}",
                chapter_id=chapter_id,
                t=rt_from_seconds(moment),
                confidence=confidence,
            )
        )

    stages = {
        "chapters": bool(memory_chapters),
        "shots": bool(memory_shots),
        "candidates": bool(candidate_items),
        "transcript": transcript_segments > 0,
        "visual_notes": bool(note_ids),
    }
    return VideoMemoryIndex(
        source_hash=source_hash,
        src=facts.src,
        duration=facts.duration,
        chapters=memory_chapters,
        shots=memory_shots,
        counts={
            "chapters": len(memory_chapters),
            "shots": len(memory_shots),
            "segments": max(0, _int(transcript_segments)),
            "candidates": len(candidate_items),
            "quality_marks": sum(item.quality_marks for item in memory_chapters),
            "face_frames": sum(item.face_frames for item in memory_chapters),
        },
        stages=stages,
        cache_keys=dict(cache_keys or {}),
    )
