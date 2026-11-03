from __future__ import annotations

from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.perception.memory import build_memory_index
from smartcut.planner.context import memory_payload


def _rt(seconds: float) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(duration: str = "20") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=RationalTime.from_seconds(duration, rate=90_000),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
        ),
    )


def _chapters() -> dict:
    return {
        "chapters": [
            {
                "id": "ch_0001",
                "start": {"value": 0, "rate": 90_000},
                "end": {"value": 900_000, "rate": 90_000},
                "title": "开场",
                "summary": "室内口播",
                "shot_count": 2,
                "quality_marks": 1,
                "face_frames": 3,
                "keyframes": ["frames/a.jpg"],
            },
            {
                "id": "ch_0002",
                "start": {"value": 900_000, "rate": 90_000},
                "end": {"value": 1_800_000, "rate": 90_000},
                "title": "演示",
                "summary": "屏幕录制",
                "shot_count": 1,
                "quality_marks": 2,
                "face_frames": 0,
                "keyframes": ["frames/b.jpg", "frames/c.jpg"],
            },
        ]
    }


def _shots() -> dict:
    return {
        "shots": [
            {"t": {"value": 90_000, "rate": 90_000}, "confidence": 0.9},
            {"t": {"value": 1_350_000, "rate": 90_000}, "confidence": 0.8},
        ]
    }


def _candidates() -> dict:
    return {
        "candidates": [
            {
                "id": "c1",
                "source_range": {
                    "start": {"value": 180_000, "rate": 90_000},
                    "end": {"value": 360_000, "rate": 90_000},
                },
            },
            {
                "id": "c2",
                "source_range": {
                    "start": {"value": 1_080_000, "rate": 90_000},
                    "end": {"value": 1_260_000, "rate": 90_000},
                },
            },
        ]
    }


def test_memory_index_aggregates_chapters_shots_and_candidates() -> None:
    index = build_memory_index(
        facts=_facts(),
        source_hash="hash_a",
        chapters=_chapters(),
        shots=_shots(),
        candidates=_candidates(),
        visual_notes={"chapters": [{"chapter_id": "ch_0002", "summary": "屏幕"}]},
        transcript_segments=42,
        cache_keys={"scenes": "k1"},
    )

    assert index.source_hash == "hash_a"
    assert index.counts == {
        "chapters": 2,
        "shots": 2,
        "segments": 42,
        "candidates": 2,
        "quality_marks": 3,
        "face_frames": 3,
    }
    assert [shot.chapter_id for shot in index.shots] == ["ch_0001", "ch_0002"]
    assert index.chapters[0].candidate_count == 1
    assert index.chapters[1].candidate_count == 1
    assert index.chapters[1].has_visual_note is True
    assert index.chapters[0].has_visual_note is False
    assert index.stages["visual_notes"] is True
    assert index.cache_keys == {"scenes": "k1"}


def test_memory_payload_scopes_shot_expansion() -> None:
    index = build_memory_index(
        facts=_facts(),
        source_hash="hash_a",
        chapters=_chapters(),
        shots=_shots(),
        candidates=_candidates(),
        transcript_segments=10,
    )

    full = memory_payload(index)
    assert len(full["chapters"]) == 2
    assert set(full["chapter_shots"]) == {"ch_0001", "ch_0002"}
    assert full["chapter_shots"]["ch_0001"] == [1.0]

    scoped = memory_payload(index, chapter_ids=["ch_0002"])
    assert [item["id"] for item in scoped["chapters"]] == ["ch_0002"]
    assert set(scoped["chapter_shots"]) == {"ch_0002"}
    assert scoped["omitted_chapters"] == 1
    assert scoped["duration_seconds"] == 20.0
