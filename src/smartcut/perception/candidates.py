"""候选片段生成：转写句群 + 镜头/质量/人脸信息。"""

from __future__ import annotations

import bisect
from pathlib import Path

from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.edit_ir import TimeRange
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import rt_from_seconds
from smartcut.contracts.transcript import Transcript
from smartcut.perception.frames import dhash

MAX_TEXT_CHARS = 500


def _text_segments(transcript: Transcript | None, duration: float, target: float):
    if transcript and transcript.segments:
        return [
            (segment.start.to_float(), segment.end.to_float(), segment.text.strip())
            for segment in transcript.segments
            if segment.end.to_float() > segment.start.to_float()
        ]
    segments = []
    cursor = 0.0
    while cursor < duration:
        end = min(duration, cursor + target)
        segments.append((cursor, end, ""))
        cursor = end
    return segments


def generate_candidates(
    facts: MediaFacts,
    transcript: Transcript | None,
    shots: ShotList | None,
    quality: QualityReport | None,
    faces: FaceReport | None,
    chapters: dict | None = None,
    *,
    target_seconds: float = 30.0,
    max_candidates: int = 300,
) -> CandidateList:
    duration = facts.duration.to_float()
    if duration <= 0:
        return CandidateList(src=facts.src, candidates=[])

    segments = _text_segments(transcript, duration, target_seconds)
    shot_times = sorted(
        shot.t.to_float()
        for shot in (shots.shots if shots else [])
        if 0 < shot.t.to_float() < duration
    )
    quality_marks = quality.marks if quality else []
    face_anchors = faces.anchors if faces else []

    groups: list[tuple[float, float, str]] = []
    current_start = None
    current_end = None
    current_text: list[str] = []
    for start, end, text in segments:
        if current_start is None:
            current_start, current_end = start, end
            current_text = [text]
            continue
        gap = start - current_end
        shot_between = any(current_end <= value <= start for value in shot_times)
        too_long = (end - current_start) > target_seconds * 1.5
        if gap > 5.0 or shot_between or too_long:
            groups.append((current_start, current_end, " ".join(t for t in current_text if t)))
            current_start, current_end = start, end
            current_text = [text]
        else:
            current_end = end
            if text:
                current_text.append(text)
    if current_start is not None and current_end is not None:
        groups.append((current_start, current_end, " ".join(t for t in current_text if t)))

    chapter_items = (chapters or {}).get("chapters") or []
    candidates: list[Candidate] = []
    for index, (start, end, text) in enumerate(groups):
        flags = sorted(
            {
                mark.kind.value
                for mark in quality_marks
                if start <= mark.t.to_float() < end
            }
        )
        face_present = any(start <= anchor.t.to_float() < end for anchor in face_anchors)
        shot_boundary = any(start <= value < end for value in shot_times)
        title = None
        for chapter in chapter_items:
            chapter_start = chapter.get("start") or {}
            chapter_end = chapter.get("end") or {}
            if not (
                isinstance(chapter_start, dict)
                and chapter_start.get("rate")
                and isinstance(chapter_end, dict)
                and chapter_end.get("rate")
            ):
                continue
            cs = float(chapter_start["value"]) / float(chapter_start["rate"])
            ce = float(chapter_end["value"]) / float(chapter_end["rate"])
            if cs <= start < ce:
                title = chapter.get("title")
                break
        tags = [value for value in (title, *flags) if value]
        score = 0.5
        if face_present:
            score += 0.15
        if text and len(text) >= 20:
            score += 0.1
        score -= min(0.3, 0.1 * len(flags))
        if shot_boundary:
            score += 0.05
        candidates.append(
            Candidate(
                id=f"cand_{index + 1:04d}",
                source_range=TimeRange(
                    start=rt_from_seconds(start),
                    end=rt_from_seconds(end),
                ),
                text=text[:MAX_TEXT_CHARS],
                score=max(0.0, min(1.0, score)),
                tags=tags,
                quality_flags=flags,
                face_present=face_present,
                shot_boundary=shot_boundary,
            )
        )

    if len(candidates) > max_candidates:
        keep = sorted(candidates, key=lambda item: item.score, reverse=True)[:max_candidates]
        candidates = sorted(keep, key=lambda item: item.source_range.start.as_fraction())
        for index, candidate in enumerate(candidates):
            candidate.id = f"cand_{index + 1:04d}"

    return CandidateList(src=facts.src, candidates=candidates)


def attach_visual_hashes(
    candidate_list: CandidateList,
    frames: list[FrameRef],
    workspace_root: Path,
    *,
    max_items: int = 300,
) -> CandidateList:
    """为候选片段补充代表帧 dHash，用于“找相似片段”。

    纯附加：Pillow 缺失或读图失败时 visual_hash 保持 None，不影响候选生成。
    """
    if not candidate_list.candidates or not frames:
        return candidate_list
    refs = sorted(frames, key=lambda item: item.t.to_float())
    times = [item.t.to_float() for item in refs]
    hashes: dict[str, int | None] = {}
    updated: list[Candidate] = []
    for index, candidate in enumerate(candidate_list.candidates):
        if index >= max_items:
            updated.append(candidate)
            continue
        start = candidate.source_range.start.to_float()
        position = bisect.bisect_left(times, start)
        options = [item for item in (position - 1, position) if 0 <= item < len(refs)]
        if not options:
            updated.append(candidate)
            continue
        frame_index = min(options, key=lambda item: (abs(times[item] - start), item))
        relative = refs[frame_index].path
        if relative not in hashes:
            try:
                hashes[relative] = dhash(workspace_root / relative)
            except Exception:
                hashes[relative] = None
        updated.append(
            candidate.model_copy(
                update={
                    "visual_hash": hashes[relative],
                    "keyframe": relative,
                }
            )
        )
    return CandidateList(src=candidate_list.src, candidates=updated)
