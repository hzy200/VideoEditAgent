"""编排域：运行生命周期、续跑、大纲确认与批处理。"""

from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, update

from smartcut.compiler import lower, validate_ir
from smartcut.compiler.audio_mix import resolve_mix_tracks
from smartcut.compiler.captions import build_cues, format_srt, format_vtt
from smartcut.compiler.loudness import parse_loudnorm_measure, parse_target_lufs
from smartcut.compiler.diff import diff_ir
from smartcut.config import Settings
from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.chapters import Chapter, ChapterPlan, OutlineApproval
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect, validate_effects
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.history import RunDigest
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.perception import PerceptionData
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.review import EditReview, RenderReview, ReviewIssue
from smartcut.contracts.shots import ShotList
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.time import RationalTime
from smartcut.contracts.tools import ToolCall, ToolResult
from smartcut.contracts.transcode import (
    AudioOutput,
    TranscodeProfile,
    TranscodeRequest,
)
from smartcut.contracts.transcript import Transcript
from smartcut.contracts.vision import (
    ChapterVisualNote,
    ImageVisualNote,
    ImageVisualNotes,
    VisualNotes,
)
from smartcut.errors import (
    IRValidationError,
    PlannerUnavailable,
    ResourceNotFound,
    RunCancelledError,
    ToolExecutionError,
    UnsupportedFeature,
    VerificationError,
)
from smartcut.llm.singleflight import SingleFlight
from smartcut.llm.vision import build_vision_client
from smartcut.logging import get_logger
from smartcut.orchestrator import build_orchestrator
from smartcut.orchestrator.context import accepts_keyword
from smartcut.perception import candidates as candidates_mod
from smartcut.perception import chapters as chapters_mod
from smartcut.perception import chunking
from smartcut.perception import contactsheet as contactsheet_mod
from smartcut.perception import faces as faces_mod
from smartcut.perception import frames as frames_mod
from smartcut.perception import manifest as manifest_mod
from smartcut.perception import process_pool
from smartcut.perception import quality as quality_mod
from smartcut.perception import scenes as scenes_mod
from smartcut.perception.transcribe import Transcriber, build_transcriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.planner.context import filter_transcript_to_chapters
from smartcut.planner.vision import (
    VisionImage,
    build_image_note_messages,
    chapters_needing_vision,
    select_images,
)
from smartcut.policy import load_policy
from smartcut.resources import ResourceManager
from smartcut.runner.decoders import HardwareDecodeSelector
from smartcut.runner.encoders import EncoderSelector, profile_from_policy
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.separation import (
    available as demucs_available,
    build_separator,
)
from smartcut.runner.timeouts import compute_tool_timeout
from smartcut.runner.tools.cut import CutArgs
from smartcut.server.atomic import atomic_write_text
from smartcut.server.cache import ArtifactCache, ArtifactLLMCache
from smartcut.server.candidate_index import CandidateIndex
from smartcut.server.db import (
    Artifact,
    Database,
    Job,
    MediaAsset,
    OrchestrationRun,
    OrchestrationTaskOutcome,
    OutlineApprovalRecord,
    Project,
    ProjectAudioAsset,
    Revision,
    ToolCallRecord,
    utcnow,
)
from smartcut.server.jobs import JobReporter, JobService
from smartcut.server.templates import TemplateStore
from smartcut.workspace import WorkspaceManager

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_CHUNK = 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or "input.mp4"



_DIGEST_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _digest_filename(task_id: str) -> str:
    """任务 id 已是 SafeId；这里再做一次防御，避免任意路径写入。"""
    cleaned = _DIGEST_UNSAFE.sub("_", str(task_id))[:64]
    return cleaned or "task"


def _run_not_claimed(reason: str) -> IRValidationError:
    """run 已被领取或已终态；包装层不能把它误标为 failed。"""
    exc = IRValidationError(reason)
    setattr(exc, "run_not_claimed", True)
    return exc


class OrchestrationMixin:
    def create_orchestration_run(
        self,
        project_id: str,
        goal: str,
        *,
        allow_render: bool = True,
        manual_ir: EditIR | None = None,
        pinned_effects: list[Effect] | None = None,
        phase: str = "plan",
        approved_outline_id: str | None = None,
    ) -> dict:
        with self.jobs.project_guard(project_id):
            return self._create_orchestration_run_locked(
                project_id,
                goal,
                allow_render=allow_render,
                manual_ir=manual_ir,
                pinned_effects=pinned_effects,
                phase=phase,
                approved_outline_id=approved_outline_id,
            )

    def _create_orchestration_run_locked(
        self,
        project_id: str,
        goal: str,
        *,
        allow_render: bool = True,
        manual_ir: EditIR | None = None,
        pinned_effects: list[Effect] | None = None,
        phase: str = "plan",
        approved_outline_id: str | None = None,
    ) -> dict:
        self._require_project(project_id)
        asset = self._require_asset(project_id)
        if phase not in {"outline", "fast_outline", "audio_first", "plan"}:
            raise IRValidationError(f"未知 phase: {phase!r}")
        if manual_ir is not None and pinned_effects is not None:
            raise IRValidationError("pinned_effects 与 manual_ir 不能同时使用")
        if pinned_effects is not None:
            facts = MediaFacts.model_validate_json(asset.facts_json)
            validate_effects(pinned_effects, total_duration=facts.duration)
        run_id = f"run_{uuid4().hex[:10]}"
        with self.db.session() as session:
            session.add(
                OrchestrationRun(
                    id=run_id,
                    project_id=project_id,
                    goal=goal,
                    status="queued",
                    allow_render=allow_render,
                    phase=phase,
                    approved_outline_id=approved_outline_id,
                    manual_ir_json=(
                        manual_ir.model_dump_json(by_alias=True)
                        if manual_ir is not None
                        else None
                    ),
                    pinned_effects_json=(
                        json.dumps(
                            [effect.model_dump(mode="json") for effect in pinned_effects],
                            ensure_ascii=False,
                        )
                        if pinned_effects is not None
                        else None
                    ),
                )
            )
        return self.get_orchestration_run(run_id)

    def recent_run_digests(
        self,
        project_id: str,
        *,
        limit: int = 5,
        exclude_run_id: str | None = None,
    ) -> list[dict]:
        """只读最近完成 run 的摘要；素材更换后旧经验不再注入。"""
        asset = self._require_asset(project_id)
        with self.db.session() as session:
            query = select(OrchestrationRun).where(
                OrchestrationRun.project_id == project_id,
                OrchestrationRun.status.in_(
                    ("succeeded", "partial", "failed", "cancelled")
                ),
                OrchestrationRun.created_at >= asset.created_at,
            )
            if exclude_run_id:
                query = query.where(OrchestrationRun.id != exclude_run_id)
            rows = session.scalars(
                query.order_by(OrchestrationRun.created_at.desc()).limit(
                    max(1, int(limit))
                )
            ).all()
        digests: list[dict] = []
        for row in rows:
            try:
                digests.append(self._run_digest(row).model_dump(mode="json"))
            except Exception:  # 旧库脏数据不能阻断规划
                continue
        return digests

    @staticmethod
    def _run_digest(row: OrchestrationRun) -> RunDigest:
        revision: int | None = None
        qa_score: float | None = None
        qa_summary: str | None = None
        changes: list[str] = []
        try:
            ledger = json.loads(row.ledger_json) if row.ledger_json else []
        except (TypeError, ValueError):
            ledger = []
        for item in ledger:
            if not isinstance(item, dict):
                continue
            output = item.get("output") or {}
            agent = item.get("agent")
            value = output.get("revision")
            if isinstance(value, int):
                revision = max(revision or 0, value)
            if agent == "qa":
                raw_score = output.get("score")
                try:
                    qa_score = (
                        float(raw_score) if raw_score is not None else None
                    )
                except (TypeError, ValueError):
                    qa_score = None
                qa_summary = str(output.get("summary") or "")[:160] or None
            elif agent == "critic" and output.get("changed"):
                changes.append(
                    f"critic: {len(output.get('changes') or [])} 项结构修正"
                )
            elif agent == "audio" and output.get("changed"):
                changes.append(
                    f"audio: 移除 {output.get('removed_words', 0)} 个口癖"
                )
        return RunDigest(
            run_id=row.id,
            goal=(row.goal or "")[:200],
            phase=row.phase or "plan",
            status=row.status,
            revision=revision,
            qa_score=qa_score,
            qa_summary=qa_summary,
            changes=changes[:5],
            created_at=_iso(row.created_at),
        )

    def orchestrate_run(
        self,
        run_id: str,
        reporter: JobReporter | None = None,
    ) -> dict:
        """编排入口：任何异常都必须把 run 从 queued/running 收敛到终态。"""
        try:
            return self._orchestrate_run_impl(run_id, reporter)
        except RunCancelledError as exc:
            self._mark_run_cancelled(run_id, str(exc))
            raise
        except IRValidationError as exc:
            if getattr(exc, "run_not_claimed", False):
                raise
            self._mark_run_failed(run_id, exc)
            raise
        except Exception as exc:
            self._mark_run_failed(run_id, exc)
            raise

    def _mark_run_cancelled(self, run_id: str, reason: str) -> None:
        self._set_run_terminal(run_id, "cancelled", reason)

    def _mark_run_failed(self, run_id: str, exc: BaseException) -> None:
        self._set_run_terminal(run_id, "failed", f"{type(exc).__name__}: {exc}")

    def _set_run_terminal(self, run_id: str, status: str, error: str) -> None:
        """只收敛仍在 queued/running 的 run，绝不覆盖已终态。"""
        try:
            with self.db.session() as session:
                session.execute(
                    update(OrchestrationRun)
                    .where(
                        OrchestrationRun.id == run_id,
                        OrchestrationRun.status.in_(("queued", "running")),
                    )
                    .values(status=status, error=error, updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
        except Exception as exc:
            self.logger.warning(
                "orchestration_terminal_write_failed",
                run_id=run_id,
                status=status,
                error=str(exc),
            )

    def cancel_orchestration_run(self, run_id: str, reason: str = "用户取消") -> None:
        """Job 取消 hook：把尚未结束的 run 同步收敛，避免永久 queued/running。"""
        self._set_run_terminal(run_id, "cancelled", reason)

    def _orchestrate_run_impl(
        self,
        run_id: str,
        reporter: JobReporter | None = None,
    ) -> dict:
        with self.db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            if run is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            current_status = run.status
            # CAS：只有 queued 可以进入 running，拒绝同一 run 的重复执行。
            claimed = session.execute(
                update(OrchestrationRun)
                .where(
                    OrchestrationRun.id == run_id,
                    OrchestrationRun.status == "queued",
                )
                .values(status="running")
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                raise _run_not_claimed(
                    f"运行状态不可执行: {current_status}（只允许 queued）"
                )
            session.expire_all()
            run = session.get(OrchestrationRun, run_id)
            if run is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            project_id = run.project_id
            goal = run.goal
            allow_render = bool(run.allow_render)
            phase = run.phase or "plan"
            approved_outline_id = run.approved_outline_id
            manual_ir_json = run.manual_ir_json
            pinned_effects_json = run.pinned_effects_json

        manual_ir = (
            EditIR.model_validate_json(manual_ir_json)
            if manual_ir_json is not None
            else None
        )
        pinned_effects = (
            TypeAdapter(list[Effect]).validate_json(pinned_effects_json)
            if pinned_effects_json
            else None
        )
        approved_outline: OutlineApproval | None = None
        if approved_outline_id:
            asset_src = self._require_asset(project_id).src
            with self.db.session() as session:
                record = session.get(OutlineApprovalRecord, approved_outline_id)
                if record is None:
                    raise ResourceNotFound(f"大纲确认记录不存在: {approved_outline_id}")
                approved_outline = OutlineApproval(
                    project_id=record.project_id,
                    run_id=record.run_id,
                    src=asset_src,
                    source_hash=record.source_hash,
                    goal=record.goal,
                    chapters=[
                        Chapter.model_validate(item)
                        for item in json.loads(record.chapters_json)
                    ],
                    notes=record.notes or "",
                )

        def checkpoint(outcomes) -> None:
            """增量 upsert 每个 task 的终态；不再每轮重写完整 ledger。"""
            if not outcomes:
                return
            try:
                with self.db.session() as session:
                    stored = session.get(OrchestrationRun, run_id)
                    if stored is None:
                        return
                    # 用户已取消/run 已终态时，迟到的 checkpoint 不能把它复活。
                    if stored.status not in ("queued", "running"):
                        return
                    stored.status = "running"
                    for item in outcomes:
                        status = (
                            item.status.value
                            if hasattr(item.status, "value")
                            else str(item.status)
                        )
                        payload = json.dumps(
                            item.model_dump(mode="json"),
                            ensure_ascii=False,
                        )
                        row = session.scalar(
                            select(OrchestrationTaskOutcome).where(
                                OrchestrationTaskOutcome.run_id == run_id,
                                OrchestrationTaskOutcome.task_id == item.task_id,
                            )
                        )
                        if row is None:
                            session.add(
                                OrchestrationTaskOutcome(
                                    project_id=project_id,
                                    run_id=run_id,
                                    task_id=item.task_id,
                                    status=status,
                                    data_json=payload,
                                )
                            )
                        else:
                            row.status = status
                            row.data_json = payload
                            row.updated_at = utcnow()
                # 每个任务的上下文摘要单独落盘，便于回放"agent 当时看到了什么"。
                context_dir = self.workspaces.run_tmp(project_id, run_id) / "context"
                for item in outcomes:
                    if not item.context_digest:
                        continue
                    context_dir.mkdir(parents=True, exist_ok=True)
                    name = _digest_filename(item.task_id)
                    atomic_write_text(
                        context_dir / f"{name}.json",
                        json.dumps(
                            item.context_digest,
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
            except Exception as exc:
                self.logger.warning(
                    "orchestration_checkpoint_failed",
                    run_id=run_id,
                    error=str(exc),
                )

        try:
            result = self.orchestrator.run(
                run_id=run_id,
                project_id=project_id,
                goal=goal,
                allow_render=allow_render,
                manual_ir=manual_ir,
                pinned_effects=pinned_effects,
                phase=phase,
                approved_outline=approved_outline,
                reporter=reporter,
                cancel_token=getattr(reporter, "cancel_token", None),
                on_task_complete=checkpoint,
            )
        except RunCancelledError as exc:
            with self.db.session() as session:
                stored = session.get(OrchestrationRun, run_id)
                if stored is not None:
                    stored.status = "cancelled"
                    stored.error = str(exc)
            raise
        except Exception as exc:
            with self.db.session() as session:
                stored = session.get(OrchestrationRun, run_id)
                if stored is not None:
                    stored.status = "failed"
                    stored.error = f"{type(exc).__name__}: {exc}"
            raise

        with self.db.session() as session:
            stored = session.get(OrchestrationRun, run_id)
            if stored is not None:
                stored.plan_json = result.plan.model_dump_json(by_alias=True)
                stored.ledger_json = json.dumps(
                    [item.model_dump(mode="json") for item in result.ledger],
                    ensure_ascii=False,
                )
                stored.metrics_json = json.dumps(result.metrics, ensure_ascii=False)
                stored.answer = result.answer
                stored.confidence = result.confidence
                stored.artifacts_json = json.dumps(result.artifacts, ensure_ascii=False)
                stored.warnings_json = json.dumps(result.warnings, ensure_ascii=False)
                # 用户已取消时保留 cancelled，不用迟到的成功结果覆盖。
                if stored.status != "cancelled":
                    stored.status = result.status
                    stored.error = None

        # 每次编排独占的临时工作区，保存可回放的结果快照。
        try:
            run_dir = self.workspaces.run_tmp(project_id, run_id)
            atomic_write_text(
                run_dir / "result.json",
                json.dumps(result.dump(), ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            # 回放快照是诊断增强项，写失败不能把已完成的 run 判成失败。
            self.logger.warning(
                "orchestration_trace_write_failed",
                run_id=run_id,
                error=str(exc),
            )
        return result.dump()

    def list_resumable_runs(self, project_id: str | None = None) -> list[dict]:
        """启动恢复提示用：列出被进程中断、尚未续跑的运行。"""
        with self.db.session() as session:
            query = select(OrchestrationRun).where(
                OrchestrationRun.status == "interrupted"
            )
            if project_id is not None:
                query = query.where(OrchestrationRun.project_id == project_id)
            runs = session.scalars(
                query.order_by(OrchestrationRun.created_at.desc())
            ).all()
            return [
                {
                    "id": run.id,
                    "project_id": run.project_id,
                    "goal": run.goal,
                    "phase": run.phase,
                    "status": run.status,
                    "created_at": _iso(run.created_at),
                }
                for run in runs
            ]

    def resume_orchestration_run(self, run_id: str) -> dict:
        """缓存辅助续跑：沿用 goal/manual_ir 新建 run，已完成感知/转写/LLM 会命中缓存。"""
        with self.db.session() as session:
            old = session.get(OrchestrationRun, run_id)
            if old is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            previous_status = old.status
            if previous_status not in {"interrupted", "failed"}:
                raise IRValidationError(f"运行状态不可续跑: {previous_status}")
            claimed = session.execute(
                update(OrchestrationRun)
                .where(
                    OrchestrationRun.id == run_id,
                    OrchestrationRun.status == previous_status,
                )
                .values(status="resumed", updated_at=utcnow())
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                raise IRValidationError(f"运行状态已变化，不能续跑: {run_id}")
            project_id = old.project_id
            goal = old.goal
            allow_render = bool(old.allow_render)
            phase = old.phase or "plan"
            approved_outline_id = old.approved_outline_id
            manual_ir_json = old.manual_ir_json
            pinned_effects_json = old.pinned_effects_json

        new_run_id: str | None = None
        try:
            manual_ir = (
                EditIR.model_validate_json(manual_ir_json)
                if manual_ir_json is not None
                else None
            )
            pinned_effects = (
                TypeAdapter(list[Effect]).validate_json(pinned_effects_json)
                if pinned_effects_json
                else None
            )
            new_run = self.create_orchestration_run(
                project_id,
                goal,
                allow_render=allow_render,
                manual_ir=manual_ir,
                pinned_effects=pinned_effects,
                phase=phase,
                approved_outline_id=approved_outline_id,
            )
            new_run_id = new_run["id"]
            job_id = self.jobs.submit(
                project_id,
                "orchestrate",
                lambda reporter: self.orchestrate_run(new_run_id, reporter),
                on_cancel=lambda: self.cancel_orchestration_run(new_run_id),
            )
        except Exception:
            self._compensate_resume(run_id, previous_status, new_run_id)
            raise
        return {
            "run_id": new_run_id,
            "project_id": project_id,
            "job_id": job_id,
            "resumed_from": run_id,
            "status": "queued",
        }

    def _compensate_resume(
        self,
        run_id: str,
        previous_status: str,
        new_run_id: str | None,
    ) -> None:
        try:
            with self.db.session() as session:
                if new_run_id is not None:
                    session.execute(
                        delete(OrchestrationRun).where(OrchestrationRun.id == new_run_id)
                    )
                session.execute(
                    update(OrchestrationRun)
                    .where(
                        OrchestrationRun.id == run_id,
                        OrchestrationRun.status == "resumed",
                    )
                    .values(status=previous_status, updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
        except Exception as exc:
            self.logger.warning(
                "resume_compensation_failed",
                run_id=run_id,
                error=str(exc),
            )

    def get_outline(self, run_id: str) -> dict:
        with self.db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            if run is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            project_id = run.project_id
            status = run.status
            phase = run.phase or "plan"
            approved_id = run.approved_outline_id
            if approved_id:
                record = session.get(OutlineApprovalRecord, approved_id)
                if record is None:
                    raise ResourceNotFound(f"大纲确认记录不存在: {approved_id}")
                chapters = json.loads(record.chapters_json)
                source = None
                approved = True
            else:
                chapters = []
                source = None
                approved = False

        if source is None:
            source = self._require_asset(project_id).src
        if not approved:
            plan = self.get_chapters(project_id)
            chapters = (plan or {}).get("chapters") or []
        return {
            "run_id": run_id,
            "project_id": project_id,
            "status": status,
            "phase": phase,
            "approved": approved,
            "outline_id": approved_id,
            "src": source,
            "chapters": chapters,
        }

    def approve_outline(
        self,
        run_id: str,
        chapters: list[dict],
        *,
        notes: str = "",
    ) -> dict:
        with self.db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            if run is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            if (run.phase or "plan") not in {"outline", "fast_outline"}:
                raise IRValidationError("只有 outline/fast_outline 阶段运行可以确认大纲")
            if run.status not in {"awaiting_approval", "succeeded", "partial"}:
                raise IRValidationError(f"运行状态不可确认大纲: {run.status}")
            previous_status = run.status
            project_id = run.project_id
            goal = run.goal
            allow_render = bool(run.allow_render)

        generated = self.get_chapters(project_id) or {}
        by_id = {
            str(item.get("id")): item for item in generated.get("chapters") or []
        }
        if not by_id:
            raise IRValidationError("没有可确认的章节")
        if not chapters:
            raise IRValidationError("至少选择一个章节")

        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        selected: list[Chapter] = []
        seen: set[str] = set()
        for raw in chapters:
            data = raw if isinstance(raw, dict) else raw.model_dump()
            chapter_id = str(data.get("id") or "")
            if chapter_id in seen:
                raise IRValidationError(f"章节重复: {chapter_id}")
            base = by_id.get(chapter_id)
            if base is None:
                raise IRValidationError(f"章节不存在: {chapter_id}")
            payload = dict(base)
            for key in ("start", "end"):
                if data.get(key) is None:
                    continue
                value = RationalTime.model_validate(data[key])
                if value.as_fraction() < 0 or value > facts.duration:
                    raise IRValidationError(
                        f"章节 {chapter_id} 的 {key} 超出素材时长"
                    )
                payload[key] = value.model_dump(mode="json")
            chapter = Chapter.model_validate(payload)
            if data.get("title") is not None:
                chapter = chapter.model_copy(
                    update={"title": str(data["title"])[:200]}
                )
            selected.append(chapter)
            seen.add(chapter_id)

        for previous, current in zip(selected, selected[1:]):
            if current.start < previous.end:
                raise IRValidationError(
                    f"章节顺序或范围重叠: {previous.id} → {current.id}"
                )

        source_hash = self._source_hash(project_id, asset)
        approval = OutlineApproval(
            project_id=project_id,
            run_id=run_id,
            src=asset.src,
            source_hash=source_hash,
            goal=goal,
            chapters=selected,
            notes=notes[:2000],
        )
        outline_id = f"outline_{uuid4().hex[:10]}"
        with self.db.session() as session:
            # CAS + 审批记录写入同一事务：双击/并发只能有一个成功。
            claimed = session.execute(
                update(OrchestrationRun)
                .where(
                    OrchestrationRun.id == run_id,
                    OrchestrationRun.status == previous_status,
                )
                .values(status="approved", updated_at=utcnow())
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                raise IRValidationError(f"运行状态已变化，不能确认大纲: {run_id}")
            session.add(
                OutlineApprovalRecord(
                    id=outline_id,
                    project_id=project_id,
                    run_id=run_id,
                    source_hash=source_hash,
                    goal=goal,
                    chapters_json=json.dumps(
                        [item.model_dump(mode="json") for item in selected],
                        ensure_ascii=False,
                    ),
                    notes=approval.notes,
                )
            )

        new_run_id: str | None = None
        try:
            new_run = self.create_orchestration_run(
                project_id,
                goal,
                allow_render=allow_render,
                phase="plan",
                approved_outline_id=outline_id,
            )
            new_run_id = new_run["id"]
            job_id = self.jobs.submit(
                project_id,
                "orchestrate",
                lambda reporter: self.orchestrate_run(new_run_id, reporter),
                on_cancel=lambda: self.cancel_orchestration_run(new_run_id),
            )
        except Exception:
            self._compensate_outline_approval(
                run_id,
                previous_status,
                outline_id,
                new_run_id,
            )
            raise
        return {
            "outline_id": outline_id,
            "approved_from": run_id,
            "run_id": new_run_id,
            "project_id": project_id,
            "job_id": job_id,
            "status": "queued",
            "chapters": [item.model_dump(mode="json") for item in selected],
        }

    def _compensate_outline_approval(
        self,
        run_id: str,
        previous_status: str,
        outline_id: str,
        new_run_id: str | None,
    ) -> None:
        try:
            with self.db.session() as session:
                session.execute(
                    delete(OutlineApprovalRecord).where(
                        OutlineApprovalRecord.id == outline_id
                    )
                )
                if new_run_id is not None:
                    session.execute(
                        delete(OrchestrationRun).where(OrchestrationRun.id == new_run_id)
                    )
                session.execute(
                    update(OrchestrationRun)
                    .where(
                        OrchestrationRun.id == run_id,
                        OrchestrationRun.status == "approved",
                    )
                    .values(status=previous_status, updated_at=utcnow())
                    .execution_options(synchronize_session=False)
                )
        except Exception as exc:
            self.logger.warning(
                "outline_approval_compensation_failed",
                run_id=run_id,
                error=str(exc),
            )

    def get_orchestration_run(self, run_id: str) -> dict:
        with self.db.session() as session:
            run = session.get(OrchestrationRun, run_id)
            if run is None:
                raise ResourceNotFound(f"编排运行不存在: {run_id}")
            payload = self._run_dict(run)
            if not payload["ledger"]:
                rows = session.scalars(
                    select(OrchestrationTaskOutcome)
                    .where(OrchestrationTaskOutcome.run_id == run_id)
                    .order_by(OrchestrationTaskOutcome.id)
                ).all()
                payload["ledger"] = [
                    json.loads(row.data_json) for row in rows
                ]
            return payload

    def list_orchestration_runs(self, project_id: str) -> list[dict]:
        self._require_project(project_id)
        with self.db.session() as session:
            runs = session.scalars(
                select(OrchestrationRun)
                .where(OrchestrationRun.project_id == project_id)
                .order_by(OrchestrationRun.created_at.desc())
            ).all()
            return [self._run_dict(run) for run in runs]

    def list_orchestration_run_summaries(self, project_id: str) -> list[dict]:
        """列表页只需要摘要；避免解析每次 run 的 plan/ledger 大 JSON。"""
        self._require_project(project_id)
        with self.db.session() as session:
            runs = session.scalars(
                select(OrchestrationRun)
                .where(OrchestrationRun.project_id == project_id)
                .order_by(OrchestrationRun.created_at.desc())
            ).all()
            return [self._run_summary_dict(run) for run in runs]

    def start_batch(
        self,
        project_ids: list[str],
        *,
        template_id: str | None = None,
        goal: str = "",
        allow_render: bool = False,
        phase: str = "plan",
    ) -> dict:
        if not project_ids:
            raise IRValidationError("批量任务至少需要一个项目")
        template = self.templates.get(template_id) if template_id else None
        if template is not None:
            batch_goal = template.goal_text(goal)
            allow_render = template.allow_render
            phase = template.phase
        else:
            batch_goal = goal.strip()
        if not batch_goal:
            raise IRValidationError("批量任务需要 goal 或 template")
        if phase not in {"outline", "fast_outline", "audio_first", "plan"}:
            raise IRValidationError(f"未知 phase: {phase!r}")
        runs: list[dict] = []
        for project_id in project_ids:
            self._require_project(project_id)
            self._require_asset(project_id)
            run = self.create_orchestration_run(
                project_id,
                batch_goal,
                allow_render=allow_render,
                phase=phase,
            )
            runs.append(
                {
                    "project_id": project_id,
                    "run_id": run["id"],
                    "status": "queued",
                }
            )
        owner = runs[0]["project_id"]
        job_id = self.jobs.submit(
            owner,
            "batch",
            lambda reporter: self._execute_batch(runs, reporter),
            on_cancel=lambda: [
                self.cancel_orchestration_run(item["run_id"]) for item in runs
            ],
        )
        return {
            "job_id": job_id,
            "template_id": template_id,
            "phase": phase,
            "allow_render": allow_render,
            "goal": batch_goal,
            "runs": runs,
        }

    def _execute_batch(self, runs: list[dict], reporter: JobReporter) -> dict:
        results: list[dict] = []
        total = max(1, len(runs))
        for index, item in enumerate(runs):
            token = getattr(reporter, "cancel_token", None)
            if token is not None:
                token.raise_if_cancelled()
            reporter.progress(
                5 + int(90 * index / total),
                f"批量任务 {index + 1}/{total}: {item['project_id']}",
            )
            try:
                result = self.orchestrate_run(item["run_id"], reporter)
                results.append(
                    {
                        "project_id": item["project_id"],
                        "run_id": item["run_id"],
                        "status": result.get("status"),
                        "artifacts": result.get("artifacts") or [],
                        "error": result.get("error"),
                    }
                )
            except RunCancelledError:
                raise
            except Exception as exc:
                results.append(
                    {
                        "project_id": item["project_id"],
                        "run_id": item["run_id"],
                        "status": "failed",
                        "artifacts": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        reporter.progress(100, "批量任务完成")
        failed = sum(1 for item in results if item["status"] == "failed")
        return {
            "runs": results,
            "succeeded": len(results) - failed,
            "failed": failed,
        }
