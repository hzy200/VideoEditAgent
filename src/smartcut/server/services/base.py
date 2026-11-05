"""服务基类：依赖装配、共享查询与账本辅助。"""

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

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

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
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.overlay_assets import OverlayAssetRef
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
from smartcut.perception.screen_text import build_screen_text_extractor
from smartcut.perception.transcribe import Transcriber, build_transcriber
from smartcut.perception.view.measure_gpu import build_view_measurer
from smartcut.planner.client import DeepSeekPlanner
from smartcut.planner.context import filter_transcript_to_chapters
from smartcut.planner.hybrid import HybridPlanner
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
    ProjectOverlayAsset,
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



class ServiceBase:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        jobs: JobService,
        *,
        logger: Any | None = None,
        transcriber: Transcriber | None = None,
        planner: DeepSeekPlanner | None = None,
        view_renderer: Any | None = None,
        view_measurer: Any | None = None,
        screen_text_extractor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.jobs = jobs
        self.view_renderer = view_renderer
        self.view_measurer = view_measurer or build_view_measurer(settings)
        self.screen_text_extractor = (
            screen_text_extractor or build_screen_text_extractor(settings)
        )
        self.logger = logger or get_logger("smartcut.services")
        self.workspaces = WorkspaceManager(settings.workspace_root)
        self.resources = ResourceManager(settings)
        self.templates = TemplateStore(self.workspaces.root)
        # LLM 响应缓存是全局的；媒体 artifact 缓存按项目工作区隔离。
        self.cache = ArtifactCache(db, self.workspaces.root)
        self._project_caches: dict[str, ArtifactCache] = {}
        self._project_caches_lock = threading.RLock()
        self.candidate_index = CandidateIndex(db)
        self.policy = load_policy()
        self.encoder_selector = EncoderSelector(settings, self.policy)
        self.decode_selector = HardwareDecodeSelector(settings)
        self._failed_encoders: set[str] = set()
        self._encoder_lock = threading.RLock()
        # 并行 cut 的缓存写入串行化，避免 SQLite 并发写。
        self._cache_write_lock = threading.RLock()
        # 同一 (project, revision) 的渲染串行化，避免两个 ffmpeg 同时写
        # 同一 output.mp4 / seg_*.mp4 / concat.txt。
        self._render_locks: dict[tuple[str, int], threading.RLock] = {}
        self._render_locks_guard = threading.Lock()
        self._perception_cache: dict[str, tuple[tuple, dict]] = {}
        self._perception_lock = threading.RLock()
        self._visual_backfill_attempted: set[tuple[str, str]] = set()
        self._visual_backfill_lock = threading.RLock()
        self._vision_flights = SingleFlight()
        self._vision_metrics = threading.local()
        self._evidence_metrics = threading.local()
        self.vision = build_vision_client(
            settings,
            cache=ArtifactLLMCache(self.cache),
            resources=self.resources,
        )
        self.transcriber = transcriber or build_transcriber(settings)
        if planner is not None:
            self.planner = planner
        elif settings.planner_offline_fallback:
            self.planner = HybridPlanner(
                settings,
                cache=ArtifactLLMCache(self.cache),
                resources=self.resources,
            )
        else:
            self.planner = DeepSeekPlanner(
                settings,
                cache=ArtifactLLMCache(self.cache),
                resources=self.resources,
            )
        self.orchestrator = build_orchestrator(
            self,
            settings,
            llm=getattr(self.planner, "llm", None),
            logger=self.logger,
        )
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        """进程重启后，把不可能再继续的 Job/Run 标记为 interrupted。"""
        now = utcnow()
        try:
            with self.db.session() as session:
                jobs = session.execute(
                    update(Job)
                    .where(Job.status.in_(("queued", "running")))
                    .values(
                        status="interrupted",
                        message="进程重启，任务中断",
                        error="进程重启，任务未完成",
                        updated_at=now,
                    )
                ).rowcount
                runs = session.execute(
                    update(OrchestrationRun)
                    .where(OrchestrationRun.status.in_(("queued", "running")))
                    .values(
                        status="interrupted",
                        error="进程重启，运行中断",
                        updated_at=now,
                    )
                ).rowcount
            if jobs or runs:
                self.logger.warning(
                    "recovered_interrupted",
                    jobs=jobs,
                    runs=runs,
                )
        except Exception as exc:
            self.logger.warning(
                "recover_interrupted_failed",
                error=str(exc),
            )

    def _render_lock(self, project_id: str, revision_no: int) -> threading.RLock:
        key = (project_id, int(revision_no))
        with self._render_locks_guard:
            lock = self._render_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._render_locks[key] = lock
            return lock

    def _new_transcriber(self):
        """为每个转写 Job 取独占实例，避免跨 Job 共享 CTranslate2 模型。"""
        clone = getattr(self.transcriber, "clone", None)
        if callable(clone):
            try:
                return clone()
            except Exception as exc:
                self.logger.warning(
                    "transcriber_clone_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
        return self.transcriber

    def close(self) -> None:
        self.planner.close()
        self.vision.close()

    @property
    def planner_available(self) -> bool:
        return bool(getattr(self.planner, "available", False))

    @property
    def planner_llm_available(self) -> bool:
        """当前 Planner 是否真的会调用 LLM；用于标注 source=llm/rule。"""
        value = getattr(self.planner, "llm_available", None)
        if value is None:
            return self.planner_available
        return bool(value)

    @staticmethod
    def _empty_llm_usage() -> dict[str, int]:
        return {
            "requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def llm_usage(self, scope: str) -> dict:
        def usage_for(client) -> dict[str, int]:
            getter = getattr(client, "usage_for", None)
            if callable(getter):
                return getter(scope)
            return self._empty_llm_usage()

        planner = usage_for(self.planner)
        vision = usage_for(self.vision)
        total = self._empty_llm_usage()
        for key in total:
            total[key] = planner.get(key, 0) + vision.get(key, 0)
        return {
            "planner": planner,
            "vision": vision,
            "total": total,
            "budget": int(self.settings.llm_token_budget_per_run or 0),
        }

    def project_workspace(self, project_id: str) -> Workspace:
        self._require_project(project_id)
        return self.workspaces.get(project_id)

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    @classmethod
    def _usage_snapshot(cls, target) -> dict[str, int]:
        getter = getattr(target, "usage_snapshot", None)
        if not callable(getter):
            return cls._empty_usage()
        try:
            return {**cls._empty_usage(), **getter()}
        except Exception:
            return cls._empty_usage()

    @classmethod
    def _usage_delta(cls, before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
        return {
            key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
            for key in cls._empty_usage()
        }

    @classmethod
    def _usage_add(cls, left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        return {
            key: int(left.get(key, 0)) + int(right.get(key, 0))
            for key in cls._empty_usage()
        }

    def _executor(
        self,
        workspace: Workspace,
        cancel_token=None,
    ) -> ToolCallExecutor:
        return ToolCallExecutor(
            self.settings,
            workspace,
            self.logger,
            resources=self.resources,
            cancel_token=cancel_token,
        )

    def _cache_for(self, project_id: str) -> ArtifactCache:
        with self._project_caches_lock:
            cache = self._project_caches.get(project_id)
            if cache is None:
                workspace = self.workspaces.get(project_id)
                cache = ArtifactCache(self.db, workspace.root)
                self._project_caches[project_id] = cache
            return cache

    def _source_hash(self, project_id: str, asset: MediaAsset) -> str:
        stored = getattr(asset, "source_hash", None)
        if stored:
            return stored
        workspace = self.workspaces.get(project_id)
        path = workspace.resolve_input(asset.src)
        value = self._cache_for(project_id).source_hash(path)
        asset.source_hash = value
        with self.db.session() as session:
            row = session.get(MediaAsset, asset.id)
            if row is not None:
                row.source_hash = value
        return value

    def _require_project(self, project_id: str) -> Project:
        with self.db.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ResourceNotFound(f"项目不存在: {project_id}")
            return project

    def _require_asset(self, project_id: str) -> MediaAsset:
        self._require_project(project_id)
        with self.db.session() as session:
            asset = self._latest_asset(session, project_id)
            if asset is None:
                raise ResourceNotFound(f"项目尚未导入素材: {project_id}")
            return asset

    @staticmethod
    def _latest_asset(session, project_id: str) -> MediaAsset | None:
        return session.scalars(
            select(MediaAsset)
            .where(MediaAsset.project_id == project_id)
            .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
            .limit(1)
        ).first()

    @staticmethod
    def _next_revision_no(session, project_id: str) -> int:
        current = session.scalar(
            select(func.max(Revision.revision_no)).where(Revision.project_id == project_id)
        )
        return (current or 0) + 1

    def _peek_revision_no(self, project_id: str) -> int:
        """只用于给 Planner 提示的预览号，最终编号由 _save_revision 分配。"""
        with self.db.session() as session:
            return self._next_revision_no(session, project_id)

    def _save_revision(
        self,
        project_id: str,
        ir: EditIR,
        *,
        attempts: int = 6,
    ) -> tuple[int, EditIR]:
        """原子分配 revision 号并写入；唯一键冲突时重新计算重试。"""
        last_error: IntegrityError | None = None
        for _ in range(max(1, attempts)):
            try:
                with self.db.session() as session:
                    revision_no = self._next_revision_no(session, project_id)
                    stored = ir.model_copy(
                        update={
                            "project_id": project_id,
                            "revision": revision_no,
                        }
                    )
                    session.add(
                        Revision(
                            project_id=project_id,
                            revision_no=revision_no,
                            ir_json=stored.model_dump_json(by_alias=True),
                        )
                    )
                return revision_no, stored
            except IntegrityError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise RuntimeError("revision 分配失败")

    @staticmethod
    def _run_summary_dict(run: OrchestrationRun) -> dict:
        return {
            "id": run.id,
            "project_id": run.project_id,
            "goal": run.goal,
            "status": run.status,
            "allow_render": bool(run.allow_render),
            "phase": run.phase or "plan",
            "approved_outline_id": run.approved_outline_id,
            "pinned_effect_count": (
                len(json.loads(run.pinned_effects_json))
                if run.pinned_effects_json
                else 0
            ),
            "confidence": run.confidence,
            "artifact_count": (
                len(json.loads(run.artifacts_json)) if run.artifacts_json else 0
            ),
            "warning_count": (
                len(json.loads(run.warnings_json)) if run.warnings_json else 0
            ),
            "error": run.error,
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
        }

    @staticmethod
    def _run_dict(run: OrchestrationRun) -> dict:
        return {
            "id": run.id,
            "project_id": run.project_id,
            "goal": run.goal,
            "status": run.status,
            "allow_render": bool(run.allow_render),
            "phase": run.phase or "plan",
            "approved_outline_id": run.approved_outline_id,
            "pinned_effects": (
                json.loads(run.pinned_effects_json)
                if run.pinned_effects_json
                else None
            ),
            "plan": json.loads(run.plan_json) if run.plan_json else None,
            "ledger": json.loads(run.ledger_json) if run.ledger_json else [],
            "metrics": json.loads(run.metrics_json) if run.metrics_json else {},
            "answer": run.answer,
            "confidence": run.confidence,
            "artifacts": json.loads(run.artifacts_json) if run.artifacts_json else [],
            "warnings": json.loads(run.warnings_json) if run.warnings_json else [],
            "error": run.error,
            "created_at": _iso(run.created_at),
            "updated_at": _iso(run.updated_at),
        }

    @staticmethod
    def _audio_asset_dict(asset: ProjectAudioAsset) -> dict:
        return {
            "id": asset.id,
            "name": asset.name,
            "src": asset.src,
            "source_hash": asset.source_hash,
            "facts": json.loads(asset.facts_json),
            "created_at": _iso(asset.created_at),
        }

    @staticmethod
    def _overlay_asset_dict(asset: ProjectOverlayAsset) -> dict:
        return {
            "id": asset.id,
            "name": asset.name,
            "src": asset.src,
            "mime": asset.mime,
            "width": asset.width,
            "height": asset.height,
            "source_hash": asset.source_hash,
            "created_at": _iso(asset.created_at),
        }

    def _overlay_asset_refs(self, project_id: str) -> dict[str, OverlayAssetRef]:
        with self.db.session() as session:
            rows = session.scalars(
                select(ProjectOverlayAsset).where(
                    ProjectOverlayAsset.project_id == project_id
                )
            ).all()
        return {
            row.id: OverlayAssetRef(
                id=row.id,
                name=row.name,
                src=row.src,
                width=row.width,
                height=row.height,
                source_hash=row.source_hash,
            )
            for row in rows
        }

    def _audio_asset_refs(self, project_id: str) -> dict[str, AudioAssetRef]:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        refs: dict[str, AudioAssetRef] = {
            "main": AudioAssetRef(
                id="main",
                name="原声",
                src=asset.src,
                duration=facts.duration,
                channels=(facts.audio.channels if facts.audio and facts.audio.channels else 2),
                sample_rate=(
                    facts.audio.sample_rate
                    if facts.audio and facts.audio.sample_rate
                    else 48_000
                ),
                has_audio=facts.has_audio,
                source_hash=asset.source_hash,
            )
        }
        with self.db.session() as session:
            rows = session.scalars(
                select(ProjectAudioAsset).where(
                    ProjectAudioAsset.project_id == project_id
                )
            ).all()
            for row in rows:
                row_facts = MediaFacts.model_validate_json(row.facts_json)
                refs[row.id] = AudioAssetRef(
                    id=row.id,
                    name=row.name,
                    src=row.src,
                    duration=row_facts.duration,
                    channels=(
                        row_facts.audio.channels
                        if row_facts.audio and row_facts.audio.channels
                        else 2
                    ),
                    sample_rate=(
                        row_facts.audio.sample_rate
                        if row_facts.audio and row_facts.audio.sample_rate
                        else 48_000
                    ),
                    has_audio=row_facts.has_audio,
                    source_hash=row.source_hash,
                )
        return refs

    @staticmethod
    def _asset_dict(asset: MediaAsset | None) -> dict | None:
        if asset is None:
            return None
        return {
            "id": asset.id,
            "src": asset.src,
            "source_hash": getattr(asset, "source_hash", None),
            "facts": json.loads(asset.facts_json),
            "transcript": json.loads(asset.transcript_json) if asset.transcript_json else None,
            "created_at": _iso(asset.created_at),
        }
