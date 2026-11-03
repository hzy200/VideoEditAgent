"""候选片段 FTS5 检索索引。

候选文本写入 ``candidate_fts`` 虚拟表，按项目 + 源文件哈希隔离。
优先使用 ``trigram`` 分词器（支持中文子串检索）；不可用时退到
``unicode61``，并在查询无结果时用 LIKE 兜底。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from typing import Any

from smartcut.logging import get_logger
from smartcut.server.db import Database

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]+")
MIN_LATIN_LEN = 3


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def search_terms(query: str) -> list[str]:
    """把用户目标拆成 FTS 查询词：拉丁词 + 中文 3 字 n-gram。"""
    terms: list[str] = []
    for chunk in _CJK.findall(query or ""):
        if len(chunk) <= 3:
            terms.append(chunk)
        else:
            terms.extend(chunk[index : index + 3] for index in range(len(chunk) - 2))
    for word in _LATIN.findall(query or ""):
        if len(word) >= MIN_LATIN_LEN:
            terms.append(word.lower())
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            deduped.append(term)
    return deduped[:32]


class CandidateIndex:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.logger = get_logger("smartcut.candidate_index")
        self._last_error: str | None = None
        self.tokenizer = self._ensure_table()
        self.available = self.tokenizer is not None
        self._top_cache: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
        self._cache_lock = threading.RLock()

    def _ensure_table(self) -> str | None:
        for tokenizer in ("trigram", "unicode61"):
            try:
                with self.db.engine.begin() as conn:
                    conn.exec_driver_sql(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS candidate_fts USING fts5("
                        "candidate_id UNINDEXED, project_id UNINDEXED, "
                        "source_hash UNINDEXED, text, data_json UNINDEXED, "
                        f"tokenize='{tokenizer}')"
                    )
                return tokenizer
            except sqlite3.Error as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self.logger.warning(
                    "candidate_fts_tokenizer_unavailable",
                    tokenizer=tokenizer,
                    error=self._last_error,
                )
                continue
        self.logger.warning(
            "candidate_fts_unavailable_fallback_like",
            error=self._last_error,
        )
        return None

    def status(self) -> dict[str, Any]:
        return {
            "available": getattr(self, "available", False),
            "tokenizer": self.tokenizer,
            "last_error": self._last_error,
        }

    def _invalidate(self, project_id: str, source_hash: str) -> None:
        with self._cache_lock:
            for key in [
                item
                for item in self._top_cache
                if item[0] == project_id and item[1] == source_hash
            ]:
                self._top_cache.pop(key, None)

    def replace(
        self,
        project_id: str,
        source_hash: str,
        candidates: list[dict[str, Any]],
    ) -> int:
        self._invalidate(project_id, source_hash)
        if not self.available:
            return 0
        with self.db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM candidate_fts WHERE project_id = ? AND source_hash = ?",
                (project_id, source_hash),
            )
            for item in candidates:
                conn.exec_driver_sql(
                    "INSERT INTO candidate_fts("
                    "candidate_id, project_id, source_hash, text, data_json"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        str(item.get("id", "")),
                        project_id,
                        source_hash,
                        str(item.get("text") or ""),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
        return len(candidates)

    def clear(self, project_id: str, source_hash: str) -> None:
        self._invalidate(project_id, source_hash)
        if not self.available:
            return
        with self.db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM candidate_fts WHERE project_id = ? AND source_hash = ?",
                (project_id, source_hash),
            )

    def clear_project(self, project_id: str) -> None:
        with self._cache_lock:
            for key in [item for item in self._top_cache if item[0] == project_id]:
                self._top_cache.pop(key, None)
        if not self.available:
            return
        with self.db.engine.begin() as conn:
            conn.exec_driver_sql(
                "DELETE FROM candidate_fts WHERE project_id = ?",
                (project_id,),
            )

    def search(
        self,
        project_id: str,
        source_hash: str,
        query: str,
        *,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        if not self.available:
            return []
        terms = search_terms(query)
        rows: list[tuple[str]] = []
        if terms:
            expression = " OR ".join(_quote(term) for term in terms)
            try:
                with self.db.engine.connect() as conn:
                    rows = conn.exec_driver_sql(
                        "SELECT data_json FROM candidate_fts "
                        "WHERE candidate_fts MATCH ? "
                        "AND project_id = ? AND source_hash = ? "
                        "ORDER BY bm25(candidate_fts), candidate_id LIMIT ?",
                        (expression, project_id, source_hash, int(limit)),
                    ).fetchall()
            except sqlite3.Error as exc:
                self.logger.warning(
                    "candidate_fts_query_failed_fallback_like",
                    project_id=project_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                rows = []
            if not rows:
                try:
                    rows = self._like(project_id, source_hash, terms, limit)
                except sqlite3.Error as exc:
                    # FTS/LIKE 都失败（锁/损坏）时退回 top()；规划不能因
                    # 候选索引不可用而中断。
                    self.logger.warning(
                        "candidate_like_fallback_failed",
                        project_id=project_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return self.top(project_id, source_hash, limit=limit)
        if not terms:
            return self.top(project_id, source_hash, limit=limit)
        return self._parse(rows)

    def top(
        self,
        project_id: str,
        source_hash: str,
        *,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        cache_key = (project_id, source_hash, int(limit))
        with self._cache_lock:
            cached = self._top_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        if not self.available:
            return []
        try:
            with self.db.engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT data_json FROM candidate_fts "
                    "WHERE project_id = ? AND source_hash = ? "
                    "ORDER BY CAST(json_extract(data_json, '$.score') AS REAL) DESC, "
                    "candidate_id LIMIT ?",
                    (project_id, source_hash, int(limit)),
                ).fetchall()
        except sqlite3.Error as exc:
            # 极老 SQLite / 无 JSON1 时回退到内存排序。
            self.logger.warning(
                "candidate_fts_json1_unavailable",
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                with self.db.engine.connect() as conn:
                    rows = conn.exec_driver_sql(
                        "SELECT data_json FROM candidate_fts "
                        "WHERE project_id = ? AND source_hash = ? LIMIT 500",
                        (project_id, source_hash),
                    ).fetchall()
            except sqlite3.Error as fallback_exc:
                self.logger.warning(
                    "candidate_fts_top_failed",
                    project_id=project_id,
                    error=f"{type(fallback_exc).__name__}: {fallback_exc}",
                )
                return []
            items = self._parse(rows)
            items.sort(
                key=lambda item: (
                    -float(item.get("score", 0.0)),
                    _start_seconds(item),
                    str(item.get("id", "")),
                )
            )
            items = items[:limit]
        else:
            items = self._parse(rows)
        with self._cache_lock:
            self._top_cache[cache_key] = list(items)
        return items

    def visual(
        self,
        project_id: str,
        source_hash: str,
        reference_hash: int,
        *,
        limit: int = 60,
        max_distance: int = 10,
    ) -> list[dict[str, Any]]:
        """按 dHash 汉明距离找相似候选；无哈希的候选自动跳过。"""
        if not self.available or reference_hash is None:
            return []
        try:
            with self.db.engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT data_json FROM candidate_fts "
                    "WHERE project_id = ? AND source_hash = ? LIMIT 500",
                    (project_id, source_hash),
                ).fetchall()
        except sqlite3.Error as exc:
            self.logger.warning(
                "candidate_visual_query_failed",
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return []
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for item in self._parse(rows):
            value = item.get("visual_hash")
            if not isinstance(value, int):
                continue
            distance = (int(value) ^ int(reference_hash)).bit_count()
            if distance > max_distance:
                continue
            scored.append(
                (distance, -float(item.get("score") or 0.0), item)
            )
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        return [item for _, _, item in scored[:limit]]

    def _like(
        self,
        project_id: str,
        source_hash: str,
        terms: list[str],
        limit: int,
    ) -> list[tuple[str]]:
        if not terms:
            return []
        clause = " OR ".join("text LIKE ?" for _ in terms)
        params: list[Any] = [project_id, source_hash]
        params.extend(f"%{term}%" for term in terms)
        params.append(int(limit))
        with self.db.engine.connect() as conn:
            return conn.exec_driver_sql(
                "SELECT data_json FROM candidate_fts "
                f"WHERE project_id = ? AND source_hash = ? AND ({clause}) LIMIT ?",
                tuple(params),
            ).fetchall()

    @staticmethod
    def _parse(rows: list[tuple[str]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for (payload,) in rows:
            try:
                value = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                items.append(value)
        return items


def _start_seconds(item: dict[str, Any]) -> float:
    payload = item.get("source_range") or {}
    start = payload.get("start") or {}
    try:
        return float(start.get("value", 0)) / float(start.get("rate", 1))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
