from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from smartcut.config import Settings
from smartcut.contracts.chapters import Chapter, ChapterPlan
from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.contracts.vision import ChapterVisualNote
from smartcut.perception.manifest import (
    build_video_manifest,
    manifest_for_planner,
    merge_video_manifest,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.main import create_app
from smartcut.server.services import Services


def _rt(value: str) -> RationalTime:
    return RationalTime.from_seconds(value, rate=90_000)


def _facts() -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt("2"),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=320,
            height=240,
            avg_frame_rate="30/1",
        ),
    )


def _chapters() -> ChapterPlan:
    return ChapterPlan(
        src="media/a.mp4",
        chapters=[
            Chapter(id="ch_0001", start=_rt("0"), end=_rt("1"), title="A"),
            Chapter(id="ch_0002", start=_rt("1"), end=_rt("2"), title="B"),
        ],
    )


def _transcript() -> Transcript:
    words = [
        Word(text="a", start=_rt("0.1"), end=_rt("0.4")),
        Word(text="b", start=_rt("1.1"), end=_rt("1.5")),
    ]
    segments = [
        TranscriptSegment(id=0, text="a", start=words[0].start, end=words[0].end),
        TranscriptSegment(id=1, text="b", start=words[1].start, end=words[1].end),
    ]
    return Transcript(language="en", words=words, segments=segments)


def test_manifest_roundtrip_and_digest() -> None:
    manifest = build_video_manifest(
        facts=_facts(),
        source_hash="a" * 64,
        shots=ShotList(
            src="media/a.mp4",
            shots=[ShotBoundary(t=_rt("1"), confidence=0.9)],
        ),
        chapters=_chapters(),
        transcript=_transcript(),
        quality=QualityReport(
            src="media/a.mp4",
            frame_count=2,
            marks=[QualityMark(t=_rt("0.5"), kind=QualityKind.black, score=0.8)],
        ),
        faces=FaceReport(
            src="media/a.mp4",
            frame_width=320,
            frame_height=240,
            anchors=[FaceAnchor(t=_rt("0.5"), x=0.1, y=0.1, w=0.2, h=0.2)],
        ),
        candidates={
            "candidates": [
                {"id": "c1", "tags": ["主题", "人物"]},
                {"id": "c2", "tags": ["主题"]},
            ]
        },
        visual_notes=[
            ChapterVisualNote(chapter_id="ch_0001", summary="近景")
        ],
        cache_keys={"frames": "f1", "scenes": "s1"},
        chunk_keys=["k1", "k2"],
        created_at="2026-11-02T00:00:00",
    )
    assert manifest.transcript.segment_count == 2
    assert manifest.transcript.word_count == 2
    assert manifest.transcript.first_seconds == 0.1
    assert manifest.transcript.last_seconds == 1.1
    assert manifest.transcript.chunk_keys == ["k1", "k2"]
    assert manifest.candidates.count == 2
    assert manifest.candidates.top_tags[0] == "主题"
    assert manifest.cache_keys == {"frames": "f1", "scenes": "s1"}

    restored = VideoManifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest
    summary = manifest_for_planner(manifest)
    assert summary is not None
    assert summary["stages"]["chapters"] is True
    assert summary["duration_seconds"] == 2.0


def test_manifest_incremental_merge_preserves_other_chapters() -> None:
    previous = build_video_manifest(
        facts=_facts(),
        source_hash="a" * 64,
        chapters=_chapters(),
        quality=QualityReport(
            src="media/a.mp4",
            frame_count=2,
            marks=[
                QualityMark(t=_rt("0.5"), kind=QualityKind.black, score=0.5),
                QualityMark(t=_rt("1.5"), kind=QualityKind.blurry, score=0.5),
            ],
        ),
        faces=FaceReport(
            src="media/a.mp4",
            frame_width=320,
            frame_height=240,
            anchors=[
                FaceAnchor(t=_rt("0.5"), x=0.1, y=0.1, w=0.2, h=0.2),
                FaceAnchor(t=_rt("1.5"), x=0.2, y=0.2, w=0.2, h=0.2),
            ],
        ),
        visual_notes=[
            ChapterVisualNote(chapter_id="ch_0001", summary="旧 A"),
            ChapterVisualNote(chapter_id="ch_0002", summary="旧 B"),
        ],
        cache_keys={"quality": "old"},
    )
    update = build_video_manifest(
        facts=_facts(),
        source_hash="a" * 64,
        chapters=ChapterPlan(
            src="media/a.mp4",
            chapters=[
                Chapter(id="ch_0002", start=_rt("1"), end=_rt("2"), title="B2")
            ],
        ),
        quality=QualityReport(
            src="media/a.mp4",
            frame_count=1,
            marks=[QualityMark(t=_rt("1.6"), kind=QualityKind.black, score=0.9)],
        ),
        faces=FaceReport(
            src="media/a.mp4",
            frame_width=320,
            frame_height=240,
            anchors=[FaceAnchor(t=_rt("1.6"), x=0.3, y=0.3, w=0.2, h=0.2)],
        ),
        visual_notes=[
            ChapterVisualNote(chapter_id="ch_0002", summary="新 B")
        ],
        cache_keys={"quality": "new-chapter"},
    )
    merged = merge_video_manifest(previous, update, chapter_ids=["ch_0002"])
    assert [chapter.id for chapter in merged.chapters.chapters] == [
        "ch_0001",
        "ch_0002",
    ]
    assert merged.chapters.chapters[1].title == "B2"
    assert [mark.t.to_float() for mark in merged.quality.marks] == [0.5, 1.6]
    assert [anchor.t.to_float() for anchor in merged.faces.anchors] == [0.5, 1.6]
    assert {note.chapter_id: note.summary for note in merged.visual_notes} == {
        "ch_0001": "旧 A",
        "ch_0002": "新 B",
    }
    assert merged.cache_keys["quality"] == "new-chapter"

    other = update.model_copy(update={"source_hash": "b" * 64})
    assert merge_video_manifest(previous, other, chapter_ids=["ch_0002"]) is other


def test_services_manifest_cache_and_scope(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings.chapter_target_seconds = 1.0
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("manifest")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_contact_sheet(project_id)
        services.build_chapters(project_id)

        first = services.build_video_manifest(project_id)
        assert first["cached"] is False
        manifest = first["manifest"]
        assert manifest["source_hash"]
        assert manifest["chapters"]["chapters"]
        assert manifest["cache_keys"]["frames"]
        assert manifest["cache_keys"]["chapters"]

        second = services.build_video_manifest(project_id)
        assert second["cached"] is True
        assert second["manifest"] == manifest

        scoped = services.build_video_manifest(
            project_id,
            chapter_ids=["ch_0001"],
        )
        assert scoped["cached"] is False
        assert scoped["cache_key"] != first["cache_key"]
        assert [
            item["id"] for item in scoped["manifest"]["chapters"]["chapters"]
        ] == ["ch_0001"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_manifest_api_endpoint(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    app = create_app(settings, transcriber=NullTranscriber())
    with TestClient(app) as client:
        project = client.post("/api/projects", json={"name": "manifest-api"}).json()
        project_id = project["id"]
        client.post(
            f"/api/projects/{project_id}/ingest",
            json={"src": str(synthetic_video)},
        )
        response = client.get(f"/api/projects/{project_id}/manifest")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["manifest"]["source_hash"]
        assert payload["cached"] is False
        again = client.get(f"/api/projects/{project_id}/manifest").json()
        assert again["cached"] is True
