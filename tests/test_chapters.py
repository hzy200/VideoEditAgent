from __future__ import annotations

from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment
from smartcut.perception.chapters import build_chapters
from smartcut.planner.context import chapters_payload, filter_transcript_to_chapters


def _rt(seconds: float) -> RationalTime:
    return RationalTime.from_seconds(str(seconds), rate=90_000)


def _facts(duration: float = 1200) -> MediaFacts:
    return MediaFacts(
        src="media/long.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1920, height=1080),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


def test_chapters_cover_timeline_and_aggregate_metrics() -> None:
    facts = _facts(1200)
    shots = ShotList(
        src=facts.src,
        shots=[
            ShotBoundary(t=_rt(300), confidence=0.9),
            ShotBoundary(t=_rt(600), confidence=0.9),
            ShotBoundary(t=_rt(900), confidence=0.9),
        ],
    )
    segments = [
        TranscriptSegment(
            id=index,
            text=f"topic {index}",
            start=_rt(index * 60),
            end=_rt(index * 60 + 30),
        )
        for index in range(20)
    ]
    transcript = Transcript(language="en", segments=segments, degraded=False)
    quality = QualityReport(
        src=facts.src,
        marks=[
            QualityMark(t=_rt(310), kind=QualityKind.black, score=0.9),
            QualityMark(t=_rt(910), kind=QualityKind.blurry, score=0.7),
        ],
    )
    faces = FaceReport(
        src=facts.src,
        anchors=[
            FaceAnchor(t=_rt(350), x=0.4, y=0.2, w=0.2, h=0.3),
        ],
    )

    plan = build_chapters(
        facts,
        shots,
        transcript,
        quality,
        faces,
        [],
        target_seconds=300,
    )

    assert len(plan.chapters) == 4
    assert plan.chapters[0].start.to_ffmpeg_seconds() == "0"
    assert plan.chapters[-1].end.to_ffmpeg_seconds() == "1200"
    assert plan.chapters[0].title == "topic 0"
    assert plan.chapters[1].quality_marks == 1
    assert plan.chapters[1].face_frames == 1
    assert sum(chapter.shot_count for chapter in plan.chapters) >= 2


def test_filter_transcript_to_selected_chapters() -> None:
    transcript = Transcript(
        language="en",
        segments=[
            TranscriptSegment(id=0, text="outside", start=_rt(100), end=_rt(130)),
            TranscriptSegment(id=1, text="inside", start=_rt(350), end=_rt(380)),
        ],
        degraded=False,
    )
    selected = {
        "chapters": [
            {
                "id": "ch_0002",
                "start": _rt(300).model_dump(),
                "end": _rt(600).model_dump(),
            }
        ]
    }
    filtered = filter_transcript_to_chapters(transcript, selected)
    assert filtered is not None
    assert [segment.text for segment in filtered.segments] == ["inside"]
    payload = chapters_payload(selected)
    assert payload is not None
    assert payload["chapter_count"] == 1
    assert payload["chapters"][0]["start_seconds"] == 300.0
