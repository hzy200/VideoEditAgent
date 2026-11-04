"""后台任务：线程池 + SQLite 状态机。P0 用轮询，不引入 Redis/Celery。"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select

from smartcut.cancel import CancellationToken
from smartcut.contracts.tools import ToolResult
from smartcut.errors import IRValidationError, RunCancelledError
from smartcut.logging import get_logger
from smartcut.server.db import Database, Job, ToolCallRecord, utcnow

JobHandler = Callable[["JobReporter"], dict]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class JobService:
    def __init__(
        self,
        db: Database,
        max_workers: int = 2,
        logger: Any | None = None,
        *,
        max_pending: int = 64,
        tool_audit_retention_days: int = 30,
    ) -> None:
        self.db = db
        self.logger = logger or get_logger("smartcut.jobs")
        self.max_pending = max(0, int(max_pending))
        self.tool_audit_retention_days = max(0, int(tool_audit_retention_days))
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="smartcut-job",
        )
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Any] = {}
        self._cancel_hooks: dict[str, Callable[[], None]] = {}
        self._project_by_job: dict[str, str] = {}
        self._project_locks: dict[str, threading.RLock] = {}
        self._project_state_lock = threading.Lock()
        self._deleting_projects: set[str] = set()
        self._deleted_projects: set[str] = set()
        self._shutting_down = False
        self._lock = threading.Lock()

    def _project_lock(self, project_id: str) -> threading.RLock:
        with self._project_state_lock:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._project_locks[project_id] = lock
            return lock

    @contextmanager
    def project_guard(self, project_id: str):
        """普通项目写入闸门：与 delete_project 的项目锁互斥。"""
        with self._project_state_lock:
            if project_id in self._deleted_projects:
                raise IRValidationError(f"项目已删除: {project_id}")
            if project_id in self._deleting_projects:
                raise IRValidationError(f"项目正在删除: {project_id}")
        lock = self._project_lock(project_id)
        with lock:
            with self._project_state_lock:
                if project_id in self._deleted_projects:
                    raise IRValidationError(f"项目已删除: {project_id}")
                if project_id in self._deleting_projects:
                    raise IRValidationError(f"项目正在删除: {project_id}")
            yield

    @contextmanager
    def project_lifecycle(self, project_id: str, *, for_delete: bool = False):
        """删除生命周期锁：成功退出后项目永久标记为 deleted。"""
        lock = self._project_lock(project_id)
        with lock:
            with self._project_state_lock:
                if project_id in self._deleted_projects:
                    raise IRValidationError(f"项目已删除: {project_id}")
                if for_delete:
                    if project_id in self._deleting_projects:
                        raise IRValidationError(f"项目正在删除: {project_id}")
                    self._deleting_projects.add(project_id)
                elif project_id in self._deleting_projects:
                    raise IRValidationError(f"项目正在删除: {project_id}")
            try:
                yield
            except BaseException:
                raise
            else:
                if for_delete:
                    with self._project_state_lock:
                        self._deleted_projects.add(project_id)
            finally:
                if for_delete:
                    with self._project_state_lock:
                        self._deleting_projects.discard(project_id)

    def submit(
        self,
        project_id: str,
        kind: str,
        handler: JobHandler,
        *,
        on_cancel: Callable[[], None] | None = None,
    ) -> str:
        with self.project_guard(project_id):
            return self._submit_locked(project_id, kind, handler, on_cancel=on_cancel)

    def _submit_locked(
        self,
        project_id: str,
        kind: str,
        handler: JobHandler,
        *,
        on_cancel: Callable[[], None] | None = None,
    ) -> str:
        with self._lock:
            if self._shutting_down:
                raise RuntimeError("JobService 正在关闭，拒绝新任务")
            if self.max_pending and len(self._tokens) >= self.max_pending:
                raise IRValidationError(
                    f"任务队列已满（上限 {self.max_pending}），请稍后重试"
                )
            job_id = uuid4().hex[:12]
            token = CancellationToken()
            # 先占位再写 DB，避免多个提交线程同时越过上限。
            self._tokens[job_id] = token
            self._project_by_job[job_id] = project_id
            if on_cancel is not None:
                self._cancel_hooks[job_id] = on_cancel
        try:
            with self.db.session() as session:
                session.add(
                    Job(
                        id=job_id,
                        project_id=project_id,
                        kind=kind,
                        status="queued",
                        progress=0,
                        message="排队中",
                    )
                )
            with self._lock:
                future = self.executor.submit(self._run, job_id, handler, token)
                self._futures[job_id] = future
        except Exception:
            with self._lock:
                self._tokens.pop(job_id, None)
                self._project_by_job.pop(job_id, None)
                self._cancel_hooks.pop(job_id, None)
                self._futures.pop(job_id, None)
            self._delete_job(job_id)
            raise
        return job_id

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(job_id)
            future = self._futures.get(job_id)
            hook = self._cancel_hooks.pop(job_id, None)
        if token is None:
            return False
        token.cancel()
        try:
            cancelled_immediately = False
            if future is not None:
                try:
                    cancelled_immediately = future.cancel()
                except Exception as exc:
                    self.logger.warning(
                        "job_future_cancel_failed",
                        job_id=job_id,
                        error=str(exc),
                    )
            try:
                if cancelled_immediately:
                    self._update(
                        job_id,
                        status="cancelled",
                        progress=0,
                        message="已取消",
                        error=None,
                    )
                else:
                    self._update(job_id, message="取消中")
            except Exception as exc:
                # DB 写失败不能阻断 hook 与字典清理；否则 run 会永久
                # 停在 queued/running，token/future 也会泄漏。
                self.logger.warning(
                    "job_cancel_status_write_failed",
                    job_id=job_id,
                    error=str(exc),
                )
            if cancelled_immediately:
                with self._lock:
                    self._tokens.pop(job_id, None)
                    self._futures.pop(job_id, None)
            return True
        finally:
            # 排队中的 Job 不会执行 handler，必须由 hook 把关联的
            # OrchestrationRun 从 queued/running 收敛到 cancelled。
            if hook is not None:
                try:
                    hook()
                except Exception as exc:
                    self.logger.warning(
                        "job_cancel_hook_failed",
                        job_id=job_id,
                        error=str(exc),
                    )

    def cancel_project(self, project_id: str, *, timeout: float = 15.0) -> bool:
        """取消项目全部任务并限时等待线程收敛；用于删除项目前的 quiesce。"""
        with self._lock:
            job_ids = [
                job_id
                for job_id, owner in self._project_by_job.items()
                if owner == project_id
            ]
        for job_id in job_ids:
            self.cancel(job_id)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                pending = [
                    self._futures[job_id]
                    for job_id in job_ids
                    if job_id in self._futures and not self._futures[job_id].done()
                ]
            if not pending:
                return True
            time.sleep(0.05)
        return False

    def has_active_jobs(self, project_id: str) -> bool:
        with self._lock:
            return any(
                owner == project_id for owner in self._project_by_job.values()
            )

    def prune_project_cache(
        self,
        project_id: str,
        cache: Any,
        *,
        max_bytes: int,
        dry_run: bool = False,
    ) -> dict:
        """项目无活动任务时淘汰可重算缓存；有任务则整体跳过。"""
        with self.project_guard(project_id):
            if self.has_active_jobs(project_id):
                return {
                    "project_id": project_id,
                    "pruned": 0,
                    "freed_bytes": 0,
                    "remaining_bytes": 0,
                    "dry_run": dry_run,
                    "skipped": True,
                    "reason": "active_jobs",
                }
            result = cache.prune(
                project_id,
                max_bytes=max_bytes,
                dry_run=dry_run,
            )
            return {**result, "skipped": False}

    def purge_expired_tool_records(self) -> int:
        """清理超过保留期的工具审计；retention<=0 表示永久保留。"""
        if self.tool_audit_retention_days <= 0:
            return 0
        cutoff = utcnow() - timedelta(days=self.tool_audit_retention_days)
        with self.db.session() as session:
            result = session.execute(
                delete(ToolCallRecord).where(ToolCallRecord.created_at < cutoff)
            )
            return int(result.rowcount or 0)

    def _delete_job(self, job_id: str) -> None:
        try:
            with self.db.session() as session:
                job = session.get(Job, job_id)
                if job is not None:
                    session.delete(job)
        except Exception as exc:
            self.logger.warning(
                "job_submit_cleanup_failed",
                job_id=job_id,
                error=str(exc),
            )

    def _run(
        self,
        job_id: str,
        handler: JobHandler,
        token: CancellationToken,
    ) -> None:
        reporter = JobReporter(self, job_id, token)
        try:
            # 初始状态写入也必须在 try 内：DB 写锁/IO 异常时不能
            # 让线程静默死亡，否则 token/future 会永久泄漏。
            self._update(job_id, status="running", progress=1, message="开始执行")
            result = handler(reporter)
            status = "cancelled" if token.cancelled else "succeeded"
            self._update(
                job_id,
                status=status,
                progress=100 if status == "succeeded" else reporter.progress_value,
                message="已取消" if status == "cancelled" else "完成",
                result_json=json.dumps(result, ensure_ascii=False, default=str),
                error=None,
            )
        except RunCancelledError as exc:
            self.logger.info("job_cancelled", job_id=job_id)
            self._update(
                job_id,
                status="cancelled",
                message="已取消",
                error=str(exc),
            )
        except Exception as exc:  # 任务边界必须兜底，避免线程静默死亡
            self.logger.exception("job_failed", job_id=job_id, error=str(exc))
            self._update(
                job_id,
                status="failed",
                message="失败",
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                reporter.flush_tools()
            except Exception as exc:
                self.logger.warning(
                    "tool_audit_flush_failed",
                    job_id=job_id,
                    error=str(exc),
                )
            try:
                self.purge_expired_tool_records()
            except Exception as exc:
                self.logger.warning(
                    "tool_audit_purge_failed",
                    job_id=job_id,
                    error=str(exc),
                )
            with self._lock:
                self._tokens.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._cancel_hooks.pop(job_id, None)
                self._project_by_job.pop(job_id, None)

    def _update(self, job_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def update_progress(self, job_id: str, progress: int, message: str | None = None) -> None:
        clamped = max(0, min(99, progress))
        fields: dict[str, Any] = {"progress": clamped}
        if message is not None:
            fields["message"] = message
        self._update(job_id, **fields)

    def record_tools(self, job_id: str, items: list[tuple[int, ToolResult]]) -> None:
        """批量写入工具审计，降低长渲染的 WAL 写放大。"""
        if not items:
            return
        with self.db.session() as session:
            session.add_all(
                [
                    ToolCallRecord(
                        job_id=job_id,
                        seq=seq,
                        tool=result.tool,
                        argv_json=json.dumps(result.argv, ensure_ascii=False),
                        exit_code=result.exit_code,
                        duration_ms=result.duration_ms,
                        artifacts_json=json.dumps(result.artifacts, ensure_ascii=False),
                    )
                    for seq, result in items
                ]
            )

    def record_tool(self, job_id: str, seq: int, result: ToolResult) -> None:
        self.record_tools(job_id, [(seq, result)])

    def list_jobs(
        self,
        project_id: str | None = None,
        *,
        limit: int = 200,
    ) -> list[dict]:
        with self.db.session() as session:
            query = select(Job)
            if project_id is not None:
                query = query.where(Job.project_id == project_id)
            jobs = session.scalars(
                query.order_by(Job.created_at.desc()).limit(max(1, min(limit, 1000)))
            ).all()
            return [
                {
                    "id": job.id,
                    "project_id": job.project_id,
                    "kind": job.kind,
                    "status": job.status,
                    "progress": job.progress,
                    "message": job.message,
                    "error": job.error,
                    "created_at": _iso(job.created_at),
                    "updated_at": _iso(job.updated_at),
                }
                for job in jobs
            ]

    def get(self, job_id: str) -> dict | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return None
            return {
                "id": job.id,
                "project_id": job.project_id,
                "kind": job.kind,
                "status": job.status,
                "progress": job.progress,
                "message": job.message,
                "error": job.error,
                "result": json.loads(job.result_json) if job.result_json else None,
                "created_at": _iso(job.created_at),
                "updated_at": _iso(job.updated_at),
            }

    def shutdown(
        self,
        wait: bool = False,
        *,
        timeout: float | None = None,
        cancel: bool = False,
    ) -> bool:
        """停止接收新任务；可选取消并限时等待，避免先关客户端再等任务。

        返回 True 表示所有已跟踪任务均已收敛。
        """
        with self._lock:
            self._shutting_down = True
            job_ids = list(self._tokens)
        if cancel:
            for job_id in job_ids:
                try:
                    self.cancel(job_id)
                except Exception as exc:
                    self.logger.warning(
                        "job_shutdown_cancel_failed",
                        job_id=job_id,
                        error=str(exc),
                    )
        if not wait:
            self.executor.shutdown(wait=False, cancel_futures=cancel)
            return False
        if timeout is None:
            self.executor.shutdown(wait=True, cancel_futures=cancel)
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                pending = [
                    future
                    for future in self._futures.values()
                    if not future.done()
                ]
            if not pending:
                break
            time.sleep(0.05)
        with self._lock:
            pending = [
                future for future in self._futures.values() if not future.done()
            ]
        all_done = not pending
        self.executor.shutdown(wait=all_done, cancel_futures=True)
        if not all_done:
            self.logger.warning(
                "job_shutdown_timeout",
                pending=len(pending),
                timeout=timeout,
            )
        return all_done


class JobReporter:
    BATCH_SIZE = 20

    def __init__(
        self,
        jobs: JobService,
        job_id: str,
        cancel_token: CancellationToken | None = None,
    ) -> None:
        self._jobs = jobs
        self.job_id = job_id
        self.cancel_token = cancel_token
        self.progress_value = 0
        self._tool_buffer: list[tuple[int, ToolResult]] = []
        self._tool_lock = threading.Lock()

    def progress(self, percent: int, message: str | None = None) -> None:
        self.progress_value = max(0, min(99, percent))
        self._jobs.update_progress(self.job_id, percent, message)

    def record_tool(self, seq: int, result: ToolResult) -> None:
        with self._tool_lock:
            self._tool_buffer.append((seq, result))
            if len(self._tool_buffer) < self.BATCH_SIZE:
                return
            batch = self._tool_buffer
            self._tool_buffer = []
        self._jobs.record_tools(self.job_id, batch)

    def flush_tools(self) -> None:
        with self._tool_lock:
            batch = self._tool_buffer
            self._tool_buffer = []
        self._jobs.record_tools(self.job_id, batch)
