"""VideoManifest 组装、校验与增量合并。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from smartcut.contracts.chapters import Chapter, ChapterPlan
from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.manifest import (
    MANIFEST_VERSION,
    CandidateDigest,
    TranscriptDigest,
    VideoManifest,
)
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.quality import QualityMark, QualityReport
from smartcut.contracts.transcript import Transcript
from smartcut.contracts.vision import ChapterVisualNote


def transcript_digest(
    transcript: Transcript | None,
    chunk_keys: list[str] | None = None,
) -> TranscriptDigest:
    if transcript is None:
        return TranscriptDigest(chunk_keys=list(chunk_keys or []))
    moments = [segment.start.to_float() for segment in transcript.segments]
    moments.extend(word.start.to_float() for word in transcript.words)
    return TranscriptDigest(
        available=bool(transcript.segments or transcript.words),
        degraded=transcript.degraded,
        segment_count=len(transcript.segments),
        word_count=len(transcript.words),
        first_seconds=min(moments) if moments else None,
        last_seconds=max(moments) if moments else None,
        chunk_keys=list(chunk_keys or []),
        note=transcript.note,
    )


def candidate_digest(candidates: dict | None) -> CandidateDigest:
    items = (candidates or {}).get("candidates") or []
    counts: Counter[str] = Counter()
    for item in items:
        for tag in item.get("tags") or []:
            text = str(tag).strip()
            if text:
                counts[text] += 1
    return CandidateDigest(
        count=len(items),
        top_tags=[tag for tag, _ in counts.most_common(8)],
    )


def build_video_manifest(
    *,
    facts: MediaFacts,
    source_hash: str,
    shots=None,
    chapters=None,
    transcript: Transcript | None = None,
    quality: QualityReport | None = None,
    faces: FaceReport | None = None,
    candidates: dict | None = None,
    visual_notes: list[ChapterVisualNote] | None = None,
    cache_keys: dict[str, str] | None = None,
    chunk_keys: list[str] | None = None,
    created_at: str | None = None,
) -> VideoManifest:
    return VideoManifest(
        version=MANIFEST_VERSION,
        source_hash=source_hash,
        src=facts.src,
        duration=facts.duration,
        shots=shots,
        chapters=chapters,
        transcript=transcript_digest(transcript, chunk_keys),
        quality=quality,
        faces=faces,
        candidates=candidate_digest(candidates),
        visual_notes=list(visual_notes or []),
        cache_keys=dict(cache_keys or {}),
        created_at=created_at,
    )


def merge_video_manifest(
    previous: VideoManifest | None,
    update: VideoManifest,
    *,
    chapter_ids: list[str] | None = None,
) -> VideoManifest:
    """源哈希一致时按章节增量合并；否则整体替换。"""
    if previous is None or previous.source_hash != update.source_hash:
        return update
    if not chapter_ids:
        return update

    ids = set(chapter_ids)
    merged_chapters = _merge_chapters(previous.chapters, update.chapters, ids)
    ranges = _chapter_ranges(merged_chapters, ids)
    merged_keys = {**previous.cache_keys, **update.cache_keys}
    return update.model_copy(
        update={
            "shots": update.shots or previous.shots,
            "chapters": merged_chapters,
            "quality": _merge_quality(previous.quality, update.quality, ranges),
            "faces": _merge_faces(previous.faces, update.faces, ranges),
            "visual_notes": _merge_notes(
                previous.visual_notes,
                update.visual_notes,
                ids,
            ),
            "cache_keys": merged_keys,
        }
    )


def manifest_for_planner(manifest: VideoManifest | dict | None) -> dict | None:
    """Planner 只读的压缩摘要，避免重复传递完整感知 JSON。"""
    if manifest is None:
        return None
    data = (
        manifest
        if isinstance(manifest, dict)
        else manifest.model_dump(mode="json")
    )
    stages = {
        "shots": bool(data.get("shots")),
        "chapters": bool(data.get("chapters")),
        "transcript": bool((data.get("transcript") or {}).get("available")),
        "quality": bool(data.get("quality")),
        "faces": bool(data.get("faces")),
        "candidates": bool((data.get("candidates") or {}).get("count")),
        "visual_notes": bool(data.get("visual_notes")),
    }
    duration = data.get("duration") or {}
    try:
        duration_seconds = float(duration.get("value", 0)) / float(
            duration.get("rate", 1)
        )
    except (TypeError, ValueError, ZeroDivisionError):
        duration_seconds = None
    return {
        "version": data.get("version"),
        "source_hash": data.get("source_hash"),
        "duration_seconds": duration_seconds,
        "stages": stages,
        "transcript": data.get("transcript"),
        "candidates": data.get("candidates"),
        "visual_note_count": len(data.get("visual_notes") or []),
        "cache_keys": data.get("cache_keys") or {},
    }


def _merge_chapters(
    previous: ChapterPlan | None,
    update: ChapterPlan | None,
    ids: set[str],
) -> ChapterPlan | None:
    if previous is None:
        return update
    if update is None:
        return previous
    by_id = {chapter.id: chapter for chapter in update.chapters if chapter.id in ids}
    merged: list[Chapter] = []
    for chapter in previous.chapters:
        merged.append(by_id.pop(chapter.id, chapter))
    merged.extend(by_id.values())
    return ChapterPlan(
        src=update.src or previous.src,
        chapters=merged,
        rationale=update.rationale or previous.rationale,
        origin=previous.origin if previous.origin == "approved" else update.origin,
    )


def _chapter_ranges(
    chapters: ChapterPlan | None,
    ids: set[str],
) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for chapter in (chapters.chapters if chapters else []):
        if chapter.id in ids:
            ranges.append((chapter.start.to_float(), chapter.end.to_float()))
    return ranges


def _inside(value: float, ranges: list[tuple[float, float]]) -> bool:
    return any(start <= value < end for start, end in ranges)


def _merge_quality(
    previous: QualityReport | None,
    update: QualityReport | None,
    ranges: list[tuple[float, float]],
) -> QualityReport | None:
    if previous is None:
        return update
    if update is None or not ranges:
        return previous
    marks: dict[tuple[float, str], QualityMark] = {}
    for mark in previous.marks:
        if not _inside(mark.t.to_float(), ranges):
            marks[(mark.t.to_float(), mark.kind.value)] = mark
    for mark in update.marks:
        marks[(mark.t.to_float(), mark.kind.value)] = mark
    ordered = sorted(marks.values(), key=lambda item: (item.t.to_float(), item.kind.value))
    return QualityReport(
        src=update.src or previous.src,
        frame_count=update.frame_count,
        sampled_fps=update.sampled_fps or previous.sampled_fps,
        marks=ordered,
        engine=update.engine or previous.engine,
    )


def _merge_faces(
    previous: FaceReport | None,
    update: FaceReport | None,
    ranges: list[tuple[float, float]],
) -> FaceReport | None:
    if previous is None:
        return update
    if update is None or not ranges:
        return previous
    anchors: dict[tuple[float, float, float], FaceAnchor] = {}
    for anchor in previous.anchors:
        if not _inside(anchor.t.to_float(), ranges):
            anchors[(anchor.t.to_float(), anchor.x, anchor.y)] = anchor
    for anchor in update.anchors:
        anchors[(anchor.t.to_float(), anchor.x, anchor.y)] = anchor
    ordered = sorted(anchors.values(), key=lambda item: item.t.to_float())
    from smartcut.perception.faces import derive_tracks

    return FaceReport(
        src=update.src or previous.src,
        frame_width=update.frame_width or previous.frame_width,
        frame_height=update.frame_height or previous.frame_height,
        sampled_fps=update.sampled_fps or previous.sampled_fps,
        anchors=ordered,
        tracks=derive_tracks(ordered),
        tracking_version=update.tracking_version or previous.tracking_version,
        engine=update.engine or previous.engine,
    )


def _merge_notes(
    previous: list[ChapterVisualNote],
    update: list[ChapterVisualNote],
    ids: set[str],
) -> list[ChapterVisualNote]:
    update_by_id = {note.chapter_id: note for note in update if note.chapter_id in ids}
    merged: list[ChapterVisualNote] = []
    for note in previous:
        merged.append(update_by_id.pop(note.chapter_id, note))
    merged.extend(update_by_id.values())
    return merged
