from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx

from smartcut.config import Settings
from smartcut.contracts.tools import ToolCall
from smartcut.llm.client import DeepSeekClient
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.execute import ToolCallExecutor
from smartcut.server.atomic import atomic_write_text
from smartcut.server.cache import ArtifactCache, ArtifactLLMCache
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def test_artifact_cache_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    workspace = tmp_path / "workspace"
    cache = ArtifactCache(db, workspace)
    artifact = workspace / "cache" / "demo.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("hello", encoding="utf-8")

    record = cache.put("p_test", "demo", "abc", artifact, meta={"n": 1})
    assert record["path"] == "cache/demo.txt"
    assert cache.get("p_test", "demo", "abc") is not None
    assert cache.latest("p_test", "demo")["cache_key"] == "abc"
    assert artifact.read_text(encoding="utf-8") == "hello"
    db.dispose()


def test_tool_output_is_atomically_renamed(
    executor: ToolCallExecutor,
    workspace,
    ingested_source: str,
) -> None:
    result = executor.execute(
        ToolCall(
            seq=0,
            tool="extract_audio",
            args={"src": ingested_source, "dst": "audio/atomic.wav"},
        )
    )
    final = workspace.root / "audio/atomic.wav"
    assert final.exists() and final.stat().st_size > 0
    assert not list((workspace.root / "audio").glob("*.part-*"))
    assert result.artifacts == ["audio/atomic.wav"]


def test_atomic_write_text_leaves_no_part_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "value.txt"
    atomic_write_text(path, "one")
    atomic_write_text(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert not list(path.parent.glob("*.part-*"))


def test_project_cache_is_singleton_across_threads(settings: Settings) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    barrier = threading.Barrier(8)
    caches: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        cache = services._cache_for("p_cache")
        with lock:
            caches.append(cache)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert len(caches) == 8
        assert len({id(cache) for cache in caches}) == 1
    finally:
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_cache_json_write_is_atomic(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    db.init()
    cache = ArtifactCache(db, tmp_path / "workspace")
    key = cache.key("atomic")
    cache.put_json("p_test", "kind", key, {"value": 1})
    record = cache.get("p_test", "kind", key)
    assert record is not None
    path = cache.resolve(record["path"])
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}
    assert not list(path.parent.glob("*.part-*"))
    db.dispose()


def test_llm_response_cache_hits(tmp_path: Path) -> None:
    settings = Settings(
        workspace_root=tmp_path / "workspace",
        deepseek_api_key="test",
    )
    db = Database(tmp_path / "db.sqlite")
    db.init()
    cache = ArtifactCache(db, tmp_path / "workspace")
    llm_cache = ArtifactLLMCache(cache)
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "cached-answer"}}]},
        )

    client = DeepSeekClient(
        settings,
        transport=httpx.MockTransport(handler),
        cache=llm_cache,
    )
    messages = [{"role": "user", "content": "hello"}]
    try:
        assert client.chat_text(messages) == "cached-answer"
        assert client.chat_text(messages) == "cached-answer"
    finally:
        client.close()
        db.dispose()
    assert calls["count"] == 1


def test_source_hash_is_persisted_at_ingest(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("hash")
        project_id = project["id"]
        ingested = services.ingest(project_id, str(synthetic_video))
        assert ingested["source_hash"]
        asset = services._require_asset(project_id)
        assert asset.source_hash == ingested["source_hash"]

        def fail(*_args, **_kwargs):
            raise AssertionError("source hash should be served from DB")

        monkeypatch.setattr(ArtifactCache, "source_hash", fail)
        assert services._source_hash(project_id, asset) == ingested["source_hash"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_perception_is_cached_in_memory(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("perception-cache")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        first = services.get_perception(project_id)
        assert first["frame_manifest"]

        from smartcut.server.services import perception as perception_mod

        def fail(*_args, **_kwargs):
            raise AssertionError("perception should be served from memory cache")

        monkeypatch.setattr(perception_mod.frames_mod, "read_manifest", fail)
        second = services.get_perception(project_id)
        assert second is first
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_waveform_is_cached(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("waveform")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        first = services.build_waveform(project_id, buckets=128)
        assert first["available"] is True
        assert first["cached"] is False
        assert len(first["peaks"]) == 128
        assert any(value > 0 for value in first["peaks"])

        second = services.build_waveform(project_id, buckets=128)
        assert second["cached"] is True
        assert second["peaks"] == first["peaks"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_frames_are_reused_from_cache(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
    )
    try:
        project = services.create_project("cache")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))

        first = services.extract_frames(project_id)
        assert first["available"] is True
        assert first["count"] > 0
        assert first["cached"] is False

        def fail(*_args, **_kwargs):
            raise AssertionError("frames should have been served from cache")

        monkeypatch.setattr(ToolCallExecutor, "execute", fail)
        second = services.extract_frames(project_id)
        assert second["available"] is True
        assert second["cached"] is True
        assert second["count"] == first["count"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
