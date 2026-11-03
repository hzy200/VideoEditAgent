from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import select

from smartcut.config import Settings
from smartcut.contracts.candidates import Candidate
from smartcut.contracts.chapters import Chapter, ChapterPlan
from smartcut.contracts.edit_ir import TimeRange
from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.perception.candidates import generate_candidates
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.candidate_index import CandidateIndex
from smartcut.server.db import Database, MediaAsset
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def make_facts(duration: str = "120") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )


def make_candidate(candidate_id: str, start: float, end: float, text: str) -> Candidate:
    return Candidate(
        id=candidate_id,
        source_range=TimeRange(
            start=RationalTime.from_seconds(start, rate=90_000),
            end=RationalTime.from_seconds(end, rate=90_000),
        ),
        text=text,
        score=0.6,
    )


def test_generate_candidates_groups_and_annotates() -> None:
    facts = make_facts()
    transcript = Transcript(
        language="en",
        segments=[
            TranscriptSegment(id=0, text="first topic", start=_rt("0"), end=_rt("10")),
            TranscriptSegment(id=1, text="second topic", start=_rt("12"), end=_rt("20")),
        ],
        degraded=False,
    )
    shots = ShotList(
        src=facts.src,
        shots=[
            ShotBoundary(t=_rt("10"), confidence=0.9),
            ShotBoundary(t=_rt("15"), confidence=0.9),
        ],
    )
    quality = QualityReport(
        src=facts.src,
        marks=[QualityMark(t=_rt("13"), kind=QualityKind.black, score=0.9)],
    )
    faces = FaceReport(
        src=facts.src,
        anchors=[FaceAnchor(t=_rt("14"), x=0.1, y=0.1, w=0.2, h=0.2)],
    )
    chapters = ChapterPlan(
        src=facts.src,
        chapters=[
            Chapter(id="ch_0001", start=_rt("0"), end=_rt("60"), title="Intro")
        ],
    ).model_dump(mode="json")

    result = generate_candidates(
        facts,
        transcript,
        shots,
        quality,
        faces,
        chapters,
        target_seconds=30.0,
    )

    assert len(result.candidates) == 2
    first, second = result.candidates
    assert first.text == "first topic"
    assert second.text == "second topic"
    assert second.quality_flags == ["black"]
    assert second.face_present is True
    assert second.shot_boundary is True
    assert second.tags[0] == "Intro"
    assert second.score > first.score


def test_candidate_index_fts_and_like_fallback(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    index = CandidateIndex(db)
    items = [
        make_candidate("cand_0001", 0, 10, "neural network tutorial for beginners"),
        make_candidate("cand_0002", 10, 20, "主题曲 演奏 现场"),
        make_candidate("cand_0003", 20, 30, "烹饪 美食 教程"),
    ]
    index.replace("p_test", "hash_a", [item.model_dump(mode="json") for item in items])

    english = index.search("p_test", "hash_a", "network tutorial")
    assert [item["id"] for item in english] == ["cand_0001"]

    chinese = index.search("p_test", "hash_a", "主题曲")
    assert [item["id"] for item in chinese] == ["cand_0002"]

    # 两字中文在 trigram 下不满足最小长度，由 LIKE 兜底。
    short_chinese = index.search("p_test", "hash_a", "美食")
    assert [item["id"] for item in short_chinese] == ["cand_0003"]

    assert index.search("p_test", "hash_a", "zzzz-absent") == []
    top = index.top("p_test", "hash_a", limit=2)
    assert len(top) == 2

    def fail(*_args, **_kwargs):
        raise AssertionError("top() should be served from cache")

    index._parse = fail
    assert index.top("p_test", "hash_a", limit=2) == top
    del index._parse

    boosted = items[2].model_copy(update={"score": 0.99})
    index.replace(
        "p_test",
        "hash_a",
        [items[0].model_dump(mode="json"), boosted.model_dump(mode="json")],
    )
    refreshed = index.top("p_test", "hash_a", limit=2)
    assert refreshed[0]["id"] == "cand_0003"
    db.dispose()


def test_candidate_search_returns_top_when_like_also_fails(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    index = CandidateIndex(db)
    if not index.available:
        db.dispose()
        return

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    index._like = broken  # type: ignore[method-assign]
    index.top = lambda *_args, **_kwargs: [{"id": "top"}]  # type: ignore[method-assign]
    assert index.search("p", "h", "network", limit=5) == [{"id": "top"}]
    db.dispose()


def test_candidate_index_degrades_without_fts5() -> None:
    class _FailingEngine:
        def begin(self):
            raise sqlite3.OperationalError("no such module: fts5")

        def connect(self):
            raise sqlite3.OperationalError("no such module: fts5")

    class _FailingDB:
        engine = _FailingEngine()

    index = CandidateIndex(_FailingDB())
    status = index.status()
    assert status["available"] is False
    assert status["tokenizer"] is None
    assert "fts5" in (status["last_error"] or "")

    # 索引不可用时必须静默降级，由 Services 回退到已存候选。
    assert index.replace("p", "h", [{"id": "cand_1"}]) == 0
    assert index.search("p", "h", "query") == []
    assert index.top("p", "h") == []
    index.clear("p", "h")
    index.clear_project("p")


def test_services_build_candidates_and_retrieve(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("candidates")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))

        words = [
            Word(text="neural", start=_rt("0.2"), end=_rt("0.8")),
            Word(text="network", start=_rt("0.8"), end=_rt("1.4")),
        ]
        transcript = Transcript(
            language="en",
            words=words,
            segments=[
                TranscriptSegment(
                    id=0,
                    text="neural network",
                    start=_rt("0.2"),
                    end=_rt("1.4"),
                    words=words,
                )
            ],
            degraded=False,
        )
        with services.db.session() as session:
            asset = session.scalars(
                select(MediaAsset).where(MediaAsset.project_id == project_id)
            ).first()
            assert asset is not None
            asset.transcript_json = transcript.model_dump_json()

        frames = services.extract_frames(project_id)
        assert frames["available"] is True

        built = services.build_candidates(project_id)
        assert built["available"] is True
        assert built["candidate_count"] >= 1
        assert built["cached"] is False
        assert "neural network" in built["candidates"][0]["text"]

        found = services.retrieve_candidates(project_id, "neural network")
        assert found["count"] >= 1
        assert "neural network" in found["candidates"][0]["text"]

        cached = services.build_candidates(project_id)
        assert cached["cached"] is True
        assert cached["candidate_count"] == built["candidate_count"]
        stored = services.get_candidates(project_id)
        assert stored is not None
        assert stored["candidates"][0]["id"].startswith("cand_")

        # 检索结果 JSON 可直接序列化，供 API/桌面复用。
        json.dumps(found, ensure_ascii=False)
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_retrieve_triggers_visual_backfill_for_old_candidates(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project_id = services.create_project("backfill")["id"]
        services.ingest(project_id, str(synthetic_video))
        pool = [
            {
                "id": "cand_0001",
                "score": 0.5,
                "tags": [],
                "source_range": {},
            }
        ]
        monkeypatch.setattr(
            services, "get_candidates", lambda project_id: {"candidates": pool}
        )
        monkeypatch.setattr(services.candidate_index, "search", lambda *args, **kwargs: [])
        calls: list[str] = []
        monkeypatch.setattr(
            services,
            "_ensure_visual_hashes",
            lambda project_id, source_hash: calls.append(project_id),
        )

        services.retrieve_candidates(
            project_id,
            "",
            reference_candidate_id="cand_0001",
        )

        assert calls == [project_id]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
