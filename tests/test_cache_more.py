from __future__ import annotations

from pathlib import Path

from smartcut.server.cache import ArtifactCache
from smartcut.server.db import Database


def _cache(settings, workspace) -> tuple[ArtifactCache, Database]:
    db = Database(settings.db_path)
    db.init()
    return ArtifactCache(db, workspace.root), db


def test_cache_key_stable_and_sensitive() -> None:
    a = ArtifactCache.key("source", "cut", {"start": 1, "end": 2})
    b = ArtifactCache.key("source", "cut", {"start": 1, "end": 2})
    c = ArtifactCache.key("source", "cut", {"start": 1, "end": 3})
    assert a == b
    assert a != c
    assert len(a) == 64


def test_json_roundtrip_and_latest(settings, workspace) -> None:
    cache, db = _cache(settings, workspace)
    try:
        record = cache.put_json(
            "p_1",
            "test",
            "k1",
            {"hello": "世界", "n": 1},
            source_hash="hash-a",
            meta={"version": 1},
        )
        assert record["cache_key"] == "k1"
        assert cache.get_json("p_1", "test", "k1", "hash-a") == {
            "hello": "世界",
            "n": 1,
        }
        cache.put_json("p_1", "test", "k2", {"n": 2}, source_hash="hash-b")
        latest = cache.latest("p_1", "test", "hash-b")
        assert latest is not None and latest["cache_key"] == "k2"
        assert cache.latest("p_1", "test", "missing") is None
    finally:
        db.dispose()


def test_get_json_returns_none_when_file_deleted(settings, workspace) -> None:
    cache, db = _cache(settings, workspace)
    try:
        record = cache.put_json("p_1", "test", "gone", {"x": 1})
        Path(cache.resolve(record["path"])).unlink()
        assert cache.get_json("p_1", "test", "gone") is None
    finally:
        db.dispose()


def test_source_hash_changes_with_content(settings, workspace) -> None:
    cache, db = _cache(settings, workspace)
    path = workspace.media_dir / "hash.txt"
    try:
        path.write_text("a", encoding="utf-8")
        first = cache.source_hash(path)
        path.write_text("b", encoding="utf-8")
        second = cache.source_hash(path)
        assert first != second
        assert len(first) == 64
        cache.remember_hash(path, "fixed")
        assert cache.source_hash(path) == "fixed"
    finally:
        db.dispose()
