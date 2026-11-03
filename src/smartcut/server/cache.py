"""内容寻址缓存。

缓存键 = hash(源文件哈希 + 工具 + 参数)，索引落 SQLite ``artifacts`` 表，
实际文件保存在工作区 ``cache/{kind}/{key}``。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from smartcut.server.atomic import atomic_write_text
from smartcut.server.db import Artifact, Database


class ArtifactCache:
    def __init__(self, db: Database, workspace_root: Path) -> None:
        self.db = db
        self.workspace_root = Path(workspace_root).resolve()
        self._hashes: dict[str, tuple[tuple[int, int], str]] = {}
        self._trusted: set[str] = set()
        self._hash_lock = threading.Lock()

    def source_hash(self, path: Path) -> str:
        path = Path(path).resolve()
        stat = path.stat()
        signature = (stat.st_size, stat.st_mtime_ns)
        key = str(path)
        # 刚写入的文件可能在同一时间戳粒度内被再次修改；近期文件不信任
        # (size, mtime) 缓存，强制重算，避免内容变化被误判为命中。
        recently_modified = time.time_ns() - stat.st_mtime_ns < 2_000_000_000
        with self._hash_lock:
            cached = self._hashes.get(key)
            if (
                cached is not None
                and cached[0] == signature
                and (not recently_modified or key in self._trusted)
            ):
                return cached[1]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        with self._hash_lock:
            self._hashes[key] = (signature, value)
        return value

    def remember_hash(self, path: Path, value: str) -> None:
        path = Path(path).resolve()
        try:
            stat = path.stat()
        except OSError:
            return
        key = str(path)
        with self._hash_lock:
            self._hashes[key] = ((stat.st_size, stat.st_mtime_ns), value)
            # ingest 已对复制后的文件做过完整哈希；显式允许近期文件复用该值。
            self._trusted.add(key)

    @staticmethod
    def key(*parts: Any) -> str:
        payload = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.workspace_root).as_posix()

    def resolve(self, relative: str) -> Path:
        return (self.workspace_root / relative).resolve()

    def cache_path(self, kind: str, key: str, suffix: str = "") -> Path:
        directory = self.workspace_root / "cache" / kind / key[:2]
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}{suffix}"

    def cache_dir(self, kind: str, key: str) -> Path:
        directory = self.workspace_root / "cache" / kind / key[:2] / key
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _record_dict(self, row: Artifact) -> dict:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "kind": row.kind,
            "cache_key": row.cache_key,
            "source_hash": row.source_hash,
            "path": row.path,
            "meta": json.loads(row.meta_json) if row.meta_json else {},
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def get(
        self,
        project_id: str,
        kind: str,
        key: str,
        source_hash: str | None = None,
    ) -> dict | None:
        with self.db.session() as session:
            row = session.scalar(
                select(Artifact).where(
                    Artifact.project_id == project_id,
                    Artifact.kind == kind,
                    Artifact.cache_key == key,
                )
            )
            if row is None:
                return None
            record = self._record_dict(row)
        if not self.resolve(record["path"]).exists():
            return None
        if source_hash is not None and record["source_hash"] != source_hash:
            return None
        return record

    def put(
        self,
        project_id: str,
        kind: str,
        key: str,
        path: Path,
        *,
        source_hash: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        relative = self.relative(path)
        payload = json.dumps(meta or {}, ensure_ascii=False)
        last_error: IntegrityError | None = None
        # 并发任务可能同时生成同一 cache key；唯一约束冲突时重试为更新。
        for _attempt in range(3):
            try:
                with self.db.session() as session:
                    row = session.scalar(
                        select(Artifact).where(
                            Artifact.project_id == project_id,
                            Artifact.kind == kind,
                            Artifact.cache_key == key,
                        )
                    )
                    if row is None:
                        row = Artifact(
                            project_id=project_id,
                            kind=kind,
                            cache_key=key,
                            source_hash=source_hash,
                            path=relative,
                            meta_json=payload,
                        )
                        session.add(row)
                    else:
                        row.source_hash = source_hash
                        row.path = relative
                        row.meta_json = payload
                    session.flush()
                    record = self._record_dict(row)
                return record
            except IntegrityError as exc:
                last_error = exc
                continue
        if last_error is None:
            raise RuntimeError("artifact cache write failed")
        raise last_error

    def list_records(
        self,
        project_id: str,
        kind: str,
        source_hash: str | None = None,
    ) -> list[dict]:
        with self.db.session() as session:
            query = select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.kind == kind,
            )
            if source_hash is not None:
                query = query.where(Artifact.source_hash == source_hash)
            rows = session.scalars(query.order_by(Artifact.created_at)).all()
            return [self._record_dict(row) for row in rows]

    def latest(
        self,
        project_id: str,
        kind: str,
        source_hash: str | None = None,
    ) -> dict | None:
        with self.db.session() as session:
            query = select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.kind == kind,
            )
            if source_hash is not None:
                query = query.where(Artifact.source_hash == source_hash)
            row = session.scalar(query.order_by(Artifact.updated_at.desc()).limit(1))
            if row is None:
                return None
            record = self._record_dict(row)
        if not self.resolve(record["path"]).exists():
            return None
        return record

    def get_json(
        self,
        project_id: str,
        kind: str,
        key: str,
        source_hash: str | None = None,
    ) -> Any | None:
        record = self.get(project_id, kind, key, source_hash)
        if record is None:
            return None
        try:
            return json.loads(self.resolve(record["path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put_json(
        self,
        project_id: str,
        kind: str,
        key: str,
        payload: Any,
        *,
        source_hash: str | None = None,
        meta: dict | None = None,
        suffix: str = ".json",
    ) -> dict:
        path = self.cache_path(kind, key, suffix)
        atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return self.put(
            project_id,
            kind,
            key,
            path,
            source_hash=source_hash,
            meta=meta,
        )

    def get_text(self, project_id: str, kind: str, key: str) -> str | None:
        record = self.get(project_id, kind, key)
        if record is None:
            return None
        try:
            return self.resolve(record["path"]).read_text(encoding="utf-8")
        except OSError:
            return None

    def put_text(self, project_id: str, kind: str, key: str, value: str) -> dict:
        path = self.cache_path(kind, key, ".txt")
        atomic_write_text(path, value)
        return self.put(project_id, kind, key, path)


class ArtifactLLMCache:
    """DeepSeekClient 的响应缓存适配器；全局共享同 prompt 结果。"""

    def __init__(self, cache: ArtifactCache, scope: str = "global") -> None:
        self.cache = cache
        self.scope = scope

    def get(self, key: str) -> str | None:
        return self.cache.get_text(self.scope, "llm", key)

    def put(self, key: str, value: str) -> None:
        self.cache.put_text(self.scope, "llm", key, value)
