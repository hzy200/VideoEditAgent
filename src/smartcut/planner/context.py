"""规划上下文与模型输出草稿。

模型只负责“选哪几段”；media_facts / project_id / revision 由系统权威填充。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.effects import Effect, effect_catalog_payload
from smartcut.contracts.edit_ir import AudioSpec, ExportSpec, TimeRange
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.time import seconds_from_mapping
from smartcut.contracts.transcript import Transcript

MAX_TRANSCRIPT_CHARS = 200_000
DEFAULT_CONTEXT_MAX_TOKENS = 24_000
# 预算分配：转写是主体，候选次之；章节/感知/视觉继续用条目上限控制。
TRANSCRIPT_BUDGET_RATIO = 0.50
CANDIDATE_BUDGET_RATIO = 0.25


def estimate_tokens(text: str) -> int:
    """轻量 token 估算：ASCII 约 4 字符/token，CJK 约 1 字符/token。"""
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return (ascii_chars + 3) // 4 + (len(text) - ascii_chars)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


class DraftTrimOp(BaseModel):
    """模型输出的 trim；不包含 id，片段身份由系统在保存时分配。"""

    model_config = ConfigDict(extra="forbid")

    op: Literal["trim"] = "trim"
    src: str
    range: TimeRange
    tag: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class EditIRDraft(BaseModel):
    """模型结构化输出契约：不含任何路径、命令、id 或不可变事实。"""

    model_config = ConfigDict(extra="forbid")

    sequence: list[DraftTrimOp] = Field(min_length=1)
    effects: list[Effect] = Field(default_factory=list)
    audio: AudioSpec = Field(default_factory=AudioSpec)
    export: ExportSpec = Field(default_factory=ExportSpec)


class ChapterSelection(BaseModel):
    """长视频第一阶段的章节选择结果。"""

    model_config = ConfigDict(extra="forbid")

    chapter_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


def transcript_payload(
    transcript: Transcript | None,
    *,
    max_tokens: int | None = None,
) -> dict | None:
    if transcript is None:
        return None
    segments: list[dict] = []
    used_chars = 0
    used_tokens = 0
    truncated = False
    for segment in transcript.segments:
        text = segment.text.strip()
        cost = estimate_tokens(text) + len(segment.words) * 8
        if used_chars + len(text) > MAX_TRANSCRIPT_CHARS or (
            max_tokens is not None and used_tokens + cost > max_tokens
        ):
            truncated = True
            break
        segments.append(
            {
                "start": segment.start.to_ffmpeg_seconds(),
                "end": segment.end.to_ffmpeg_seconds(),
                "text": text,
                "words": [
                    {
                        "text": word.text,
                        "start": word.start.to_ffmpeg_seconds(),
                        "end": word.end.to_ffmpeg_seconds(),
                    }
                    for word in segment.words
                ],
            }
        )
        used_chars += len(text)
        used_tokens += cost
    return {
        "language": transcript.language,
        "degraded": transcript.degraded,
        "note": transcript.note,
        "truncated": truncated,
        "segments": segments,
    }


def perception_payload(perception: dict | None) -> dict | None:
    if not perception:
        return None
    shots = perception.get("shots") or {}
    quality = perception.get("quality") or {}
    faces = perception.get("faces") or {}
    shot_items = shots.get("shots") or []
    marks = quality.get("marks") or []
    by_kind: dict[str, int] = {}
    for mark in marks:
        kind = str(mark.get("kind", "unknown"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
    anchors = faces.get("anchors") or []
    avg_center = None
    if anchors:
        avg_center = {
            "x": sum(float(item.get("x", 0)) + float(item.get("w", 0)) / 2 for item in anchors)
            / len(anchors),
            "y": sum(float(item.get("y", 0)) + float(item.get("h", 0)) / 2 for item in anchors)
            / len(anchors),
        }
    return {
        "shot_count": len(shot_items),
        "shots": [
            {
                "t": item.get("t"),
                "confidence": item.get("confidence"),
                "type": item.get("type"),
            }
            for item in shot_items[:30]
        ],
        "quality_counts": by_kind,
        "quality_examples": [
            {"t": item.get("t"), "kind": item.get("kind"), "score": item.get("score")}
            for item in marks[:20]
        ],
        "face_frame_count": len(anchors),
        "face_average_center": avg_center,
        "contact_sheet": perception.get("contact_sheet"),
        "warnings": perception.get("warnings") or [],
        "manifest": perception.get("manifest"),
    }


def chapters_payload(chapters: dict | None) -> dict | None:
    if not chapters:
        return None
    items = chapters.get("chapters") or []
    payload = []
    for chapter in items[:60]:
        start = chapter.get("start") or {}
        end = chapter.get("end") or {}
        start_seconds = (
            float(start.get("value", 0)) / float(start.get("rate", 1))
            if isinstance(start, dict) and start.get("rate")
            else None
        )
        end_seconds = (
            float(end.get("value", 0)) / float(end.get("rate", 1))
            if isinstance(end, dict) and end.get("rate")
            else None
        )
        payload.append(
            {
                "id": chapter.get("id"),
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "title": chapter.get("title"),
                "summary": chapter.get("summary"),
                "shot_count": chapter.get("shot_count", 0),
                "quality_marks": chapter.get("quality_marks", 0),
                "face_frames": chapter.get("face_frames", 0),
            }
        )
    return {
        "chapter_count": len(items),
        "chapters": payload,
        "rationale": chapters.get("rationale"),
    }


def candidates_payload(
    candidates: dict | None,
    limit: int = 80,
    *,
    max_tokens: int | None = None,
) -> dict | None:
    if not candidates:
        return None
    items = candidates.get("candidates") or []
    payload = []
    used_tokens = 0
    for candidate in items[:limit]:
        source_range = candidate.get("source_range") or {}
        start = source_range.get("start") or {}
        end = source_range.get("end") or {}
        try:
            start_seconds = float(start.get("value", 0)) / float(start.get("rate", 1))
            end_seconds = float(end.get("value", 0)) / float(end.get("rate", 1))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        text = str(candidate.get("text") or "")[:200]
        tags = candidate.get("tags") or []
        cost = estimate_tokens(text) + len(tags) * 2 + 24
        if max_tokens is not None and payload and used_tokens + cost > max_tokens:
            break
        used_tokens += cost
        payload.append(
            {
                "id": candidate.get("id"),
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "text": text,
                "score": candidate.get("score"),
                "tags": tags,
                "quality_flags": candidate.get("quality_flags") or [],
                "face_present": bool(candidate.get("face_present")),
                "shot_boundary": bool(candidate.get("shot_boundary")),
            }
        )
    return {
        "query": candidates.get("query"),
        "count": len(items),
        "returned": len(payload),
        "candidates": payload,
    }


def visual_notes_payload(notes: dict | None) -> dict | None:
    if not notes:
        return None
    items = notes.get("chapters") or []
    return {
        "overall": notes.get("overall") or "",
        "chapters": [
            {
                "chapter_id": item.get("chapter_id"),
                "summary": item.get("summary") or "",
                "visual_tags": item.get("visual_tags") or [],
                "cautions": item.get("cautions") or [],
            }
            for item in items[:60]
        ],
        "image_count": notes.get("image_count", 0),
        "model": notes.get("model"),
    }


def filter_transcript_to_chapters(
    transcript: Transcript | None,
    chapters: dict | None,
) -> Transcript | None:
    """只保留落在选中章节内的词/句，压缩长视频上下文。"""
    if transcript is None or not chapters:
        return transcript
    ranges: list[tuple[float, float]] = []
    for chapter in chapters.get("chapters") or []:
        start = chapter.get("start") or {}
        end = chapter.get("end") or {}
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        if not start.get("rate") or not end.get("rate"):
            continue
        ranges.append(
            (
                float(start["value"]) / float(start["rate"]),
                float(end["value"]) / float(end["rate"]),
            )
        )
    if not ranges:
        return transcript

    def inside(value: float) -> bool:
        return any(start <= value < end for start, end in ranges)

    words = [word for word in transcript.words if inside(word.start.to_float())]
    segments = [
        segment
        for segment in transcript.segments
        if inside(segment.start.to_float())
        or any(inside(word.start.to_float()) for word in segment.words)
    ]
    return Transcript(
        language=transcript.language,
        words=words,
        segments=segments,
        degraded=transcript.degraded,
        note=transcript.note,
    )


def timeline_payload(
    timeline,
    *,
    max_used: int = 60,
    max_gaps: int = 20,
) -> dict | None:
    """时间线账本的紧凑视图：给覆盖/重复统计，明细只截前若干条。"""
    if timeline is None:
        return None
    data = timeline if isinstance(timeline, dict) else timeline.model_dump(mode="json")
    used_items = data.get("used") or []
    gap_items = data.get("unused_gaps") or []
    used = [
        {
            "src_start_seconds": seconds_from_mapping(item.get("src_start")),
            "src_end_seconds": seconds_from_mapping(item.get("src_end")),
            "out_start_seconds": seconds_from_mapping(item.get("out_start")),
            "out_end_seconds": seconds_from_mapping(item.get("out_end")),
            "tag": item.get("tag"),
            "origin": item.get("origin"),
        }
        for item in used_items[:max_used]
    ]
    gaps = [
        {
            "start_seconds": seconds_from_mapping(item.get("start")),
            "end_seconds": seconds_from_mapping(item.get("end")),
        }
        for item in gap_items[:max_gaps]
    ]
    return {
        "revision": data.get("revision"),
        "coverage_ratio": round(float(data.get("coverage_ratio") or 0.0), 4),
        "duplicate_ratio": round(float(data.get("duplicate_ratio") or 0.0), 4),
        "output_seconds": seconds_from_mapping(data.get("output_duration")),
        "used_count": len(used_items),
        "used": used,
        "unused_gap_count": len(gap_items),
        "unused_gaps": gaps,
    }


def memory_payload(
    memory,
    *,
    chapter_ids: list[str] | None = None,
    max_chapters: int = 60,
    max_shots: int = 120,
) -> dict | None:
    """分层索引的 Planner 视图：顶层章节摘要 + 选中章节的镜头切点。"""
    if memory is None:
        return None
    data = memory if isinstance(memory, dict) else memory.model_dump(mode="json")
    chapter_items = data.get("chapters") or []
    shot_items = data.get("shots") or []
    selected_ids = set(chapter_ids or [])
    selected = (
        [item for item in chapter_items if item.get("id") in selected_ids]
        if selected_ids
        else chapter_items[:max_chapters]
    )
    # 只有显式选中章节或章节很少时才展开镜头，避免长视频 prompt 膨胀。
    include_shots = bool(selected_ids) or len(chapter_items) <= 12
    selected_scope = {item.get("id") for item in selected}
    chapter_shots: dict[str, list[float]] = {}
    shot_total = 0
    if include_shots:
        for shot in shot_items:
            chapter_id = shot.get("chapter_id")
            if not chapter_id or chapter_id not in selected_scope:
                continue
            moment = seconds_from_mapping(shot.get("t"))
            if moment is None or shot_total >= max_shots:
                continue
            bucket = chapter_shots.setdefault(str(chapter_id), [])
            if len(bucket) >= 40:
                continue
            bucket.append(round(moment, 3))
            shot_total += 1
    return {
        "version": data.get("version"),
        "source_hash": data.get("source_hash"),
        "duration_seconds": seconds_from_mapping(data.get("duration")),
        "counts": data.get("counts") or {},
        "stages": data.get("stages") or {},
        "chapters": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "start_seconds": seconds_from_mapping(item.get("start")),
                "end_seconds": seconds_from_mapping(item.get("end")),
                "shot_count": item.get("shot_count", 0),
                "candidate_count": item.get("candidate_count", 0),
                "has_visual_note": bool(item.get("has_visual_note")),
                "summary": str(item.get("summary") or "")[:120],
            }
            for item in selected[:max_chapters]
        ],
        "chapter_shots": chapter_shots,
        "omitted_chapters": max(0, len(chapter_items) - len(selected)),
    }


def preference_payload(preference) -> dict | None:
    """偏好记忆只给统计与约束说明，不给可直接照抄的片段列表。"""
    if not preference:
        return None
    data = (
        preference
        if isinstance(preference, dict)
        else preference.model_dump(mode="json")
    )
    stats = data.get("stats") or {}
    if int(stats.get("sample_size") or 0) < 2:
        return None
    return {
        "sample_size": int(stats.get("sample_size") or 0),
        "segment_count": int(stats.get("segment_count") or 0),
        "median_segment_seconds": float(stats.get("median_segment_seconds") or 0.0),
        "max_segment_seconds": float(stats.get("max_segment_seconds") or 0.0),
        "keep_ratio": float(stats.get("keep_ratio") or 0.0),
        "top_tags": list(stats.get("top_tags") or [])[:5],
        "notes": list(data.get("notes") or [])[:3],
    }


def build_context(
    facts: MediaFacts,
    transcript: Transcript | None,
    goal: str,
    perception: dict | None = None,
    chapters: dict | None = None,
    candidates: dict | None = None,
    visual_notes: dict | None = None,
    timeline=None,
    memory=None,
    preference=None,
    chapter_ids: list[str] | None = None,
    max_tokens: int | None = None,
    stats: dict | None = None,
) -> dict:
    transcript_budget = (
        max(1_000, int(max_tokens * TRANSCRIPT_BUDGET_RATIO))
        if max_tokens is not None
        else None
    )
    candidate_budget = (
        max(500, int(max_tokens * CANDIDATE_BUDGET_RATIO))
        if max_tokens is not None
        else None
    )
    context = {
        "goal": goal,
        "media_facts": {
            "src": facts.src,
            "duration_seconds": facts.duration.to_ffmpeg_seconds(),
            "fps": facts.fps,
            "has_audio": facts.has_audio,
            "width": facts.width,
            "height": facts.height,
            "rotation": facts.rotation,
            "vfr": facts.vfr,
        },
        "transcript": transcript_payload(transcript, max_tokens=transcript_budget),
        "perception": perception_payload(perception),
        "chapters": chapters_payload(chapters),
        "candidates": candidates_payload(candidates, max_tokens=candidate_budget),
        "visual_notes": visual_notes_payload(visual_notes),
        "timeline": timeline_payload(timeline),
        "memory": memory_payload(memory, chapter_ids=chapter_ids),
        "preference": preference_payload(preference),
        "effect_catalog": effect_catalog_payload(),
    }
    if stats is not None:
        stats.update(
            {
                "budget_tokens": max_tokens,
                "estimated_tokens": estimate_tokens(_dumps(context)),
                "sections": {
                    name: {
                        "chars": len(_dumps(block)),
                        "tokens": estimate_tokens(_dumps(block)),
                    }
                    for name, block in context.items()
                },
                "transcript_truncated": bool(
                    (context.get("transcript") or {}).get("truncated")
                ),
            }
        )
    return context

