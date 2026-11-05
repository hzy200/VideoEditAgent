"""章节切分：镜头切点 + 时长目标，生成 ChapterPlan。"""

from __future__ import annotations

from smartcut.contracts.chapters import Chapter, ChapterPlan
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import rt_from_seconds
from smartcut.contracts.transcript import Transcript


def build_chapters(
    facts: MediaFacts,
    shots: ShotList | None,
    transcript: Transcript | None,
    quality: QualityReport | None,
    faces: FaceReport | None,
    frames: list[FrameRef] | None,
    target_seconds: float = 300.0,
    shot_clusters: list[tuple[float, float]] | None = None,
) -> ChapterPlan:
    duration = facts.duration.to_float()
    if duration <= 0:
        return ChapterPlan(src=facts.src, chapters=[], rationale="素材时长为 0")

    all_cut_times = sorted(
        shot.t.to_float()
        for shot in (shots.shots if shots else [])
        if 0.0 < shot.t.to_float() < duration
    )
    cut_times = all_cut_times
    if shot_clusters:
        # 只在视觉簇边界处分章，避免同一页 PPT 的内部切点拆出多章。
        cluster_ends = [end for _start, end in shot_clusters if end < duration]
        cut_times = [
            moment
            for moment in all_cut_times
            if any(abs(moment - end) <= 0.5 for end in cluster_ends)
        ]
    boundaries = [0.0]
    cursor = 0.0
    while cursor < duration - 0.5:
        target = cursor + target_seconds
        candidates = [value for value in cut_times if value > cursor + target_seconds * 0.5]
        if candidates:
            end = min(candidates)
            if end > target * 1.6:
                end = min(duration, target)
        else:
            end = min(duration, target)
        if end <= cursor:
            end = duration
        boundaries.append(end)
        cursor = end
    if boundaries[-1] < duration:
        boundaries[-1] = duration
    if len(boundaries) < 2:
        boundaries = [0.0, duration]

    segments = transcript.segments if transcript else []
    quality_marks = quality.marks if quality else []
    face_anchors = faces.anchors if faces else []
    frame_refs = frames or []

    chapters: list[Chapter] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        if end <= start:
            continue
        chapter_segments = [
            segment
            for segment in segments
            if segment.end.to_float() > start and segment.start.to_float() < end
        ]
        text = " ".join(segment.text.strip() for segment in chapter_segments).strip()
        title = chapter_segments[0].text.strip()[:40] if chapter_segments else f"章节 {index + 1}"
        keyframes = [
            frame.path
            for frame in frame_refs
            if start <= frame.t.to_float() < end
        ]
        if len(keyframes) > 3:
            keyframes = [keyframes[0], keyframes[len(keyframes) // 2], keyframes[-1]]
        chapters.append(
            Chapter(
                id=f"ch_{index + 1:04d}",
                start=rt_from_seconds(start),
                end=rt_from_seconds(end),
                title=title,
                summary=text[:300] if text else None,
                shot_count=sum(
                    1 for value in all_cut_times if start <= value < end
                ),
                quality_marks=sum(
                    1 for mark in quality_marks if start <= mark.t.to_float() < end
                ),
                face_frames=sum(
                    1 for anchor in face_anchors if start <= anchor.t.to_float() < end
                ),
                keyframes=keyframes,
            )
        )

    return ChapterPlan(
        src=facts.src,
        chapters=chapters,
        rationale=f"按 {target_seconds:.0f}s 目标与镜头切点生成 {len(chapters)} 个章节",
    )
