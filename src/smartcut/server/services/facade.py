"""Services 门面：组合领域 mixin，保留既有公开 API。"""

from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from smartcut.compiler import apply_patch, lower, validate_ir
from smartcut.compiler.audio_mix import resolve_mix_tracks
from smartcut.compiler.captions import build_cues, format_srt, format_vtt
from smartcut.compiler.loudness import parse_loudnorm_measure, parse_target_lufs
from smartcut.compiler.diff import diff_ir
from smartcut.config import Settings
from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.chapters import Chapter, ChapterPlan, OutlineApproval
from smartcut.contracts.edit_ir import EditIR, assign_trim_ids
from smartcut.contracts.effects import Effect
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.overlay_assets import OverlayAssetRef
from smartcut.contracts.patch import EditPatch
from smartcut.contracts.perception import PerceptionData
from smartcut.contracts.preference import EditPreference, PreferenceStats
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
from smartcut.server.cache import ArtifactCache, ArtifactLLMCache
from smartcut.server.candidate_index import CandidateIndex
from smartcut.server.db import (
    Artifact,
    Database,
    EditFeedback,
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


def _plan_signature(ir: EditIR) -> tuple:
    """用于判断重规划是否真的改动了时间线（忽略 revision 编号）。"""
    return (
        ir.sequence,
        ir.effects,
        ir.transitions,
        ir.overlays,
        ir.removals,
        ir.audio,
        ir.subtitles,
        ir.export,
    )


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or "input.mp4"


from smartcut.server.services.base import ServiceBase
from smartcut.server.services.evidence import EvidenceMixin
from smartcut.server.services.orchestration import OrchestrationMixin
from smartcut.server.services.perception import PerceptionMixin
from smartcut.server.services.rendering import RenderMixin
from smartcut.server.services.screen_text import ScreenTextMixin
from smartcut.server.services.visual import VisualMixin


class Services(
    EvidenceMixin,
    VisualMixin,
    RenderMixin,
    ScreenTextMixin,
    PerceptionMixin,
    OrchestrationMixin,
    ServiceBase,
):
    def create_project(self, name: str) -> dict:
        project_id = f"p_{uuid4().hex[:10]}"
        workspace = self.workspaces.create(project_id)
        with self.db.session() as session:
            session.add(
                Project(
                    id=project_id,
                    name=name.strip()[:200] or "未命名项目",
                    workspace_dir=str(workspace.root),
                )
            )
        return self.get_project(project_id)

    def list_projects(self) -> list[dict]:
        with self.db.session() as session:
            projects = session.scalars(
                select(Project).order_by(Project.created_at.desc())
            ).all()
            return [
                {
                    "id": project.id,
                    "name": project.name,
                    "created_at": _iso(project.created_at),
                    "workspace_dir": project.workspace_dir,
                }
                for project in projects
            ]

    def get_project(self, project_id: str) -> dict:
        with self.db.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ResourceNotFound(f"项目不存在: {project_id}")
            asset = self._latest_asset(session, project_id)
            revisions = session.scalars(
                select(Revision)
                .where(Revision.project_id == project_id)
                .order_by(Revision.revision_no)
            ).all()
            audio_assets = session.scalars(
                select(ProjectAudioAsset)
                .where(ProjectAudioAsset.project_id == project_id)
                .order_by(
                    ProjectAudioAsset.created_at.asc(),
                    ProjectAudioAsset.id.asc(),
                )
            ).all()
            overlay_assets = session.scalars(
                select(ProjectOverlayAsset)
                .where(ProjectOverlayAsset.project_id == project_id)
                .order_by(ProjectOverlayAsset.created_at.asc())
            ).all()
            return {
                "id": project.id,
                "name": project.name,
                "workspace_dir": project.workspace_dir,
                "created_at": _iso(project.created_at),
                "asset": self._asset_dict(asset),
                "revisions": [
                    {"revision": item.revision_no, "created_at": _iso(item.created_at)}
                    for item in revisions
                ],
                "audio_assets": [
                    self._audio_asset_dict(item) for item in audio_assets
                ],
                "overlay_assets": [
                    self._overlay_asset_dict(item) for item in overlay_assets
                ],
            }

    def list_audio_assets(self, project_id: str) -> list[dict]:
        self._require_project(project_id)
        with self.db.session() as session:
            rows = session.scalars(
                select(ProjectAudioAsset)
                .where(ProjectAudioAsset.project_id == project_id)
                .order_by(ProjectAudioAsset.created_at.asc(), ProjectAudioAsset.id.asc())
            ).all()
            return [self._audio_asset_dict(row) for row in rows]

    def import_overlay_asset(
        self,
        project_id: str,
        src: str,
        name: str | None = None,
    ) -> dict:
        """导入图片/Logo 水印资产；只复制进工作区，IR 只引用 asset_id。"""
        self._require_project(project_id)
        workspace = self.workspaces.get(project_id)
        source = Path(src).expanduser()
        if not source.is_file():
            raise ResourceNotFound(f"图片文件不存在: {src}")
        suffix = source.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            raise IRValidationError("水印图片只支持 PNG/JPG/WebP/BMP")
        destination = workspace.overlays_dir / f"{uuid4().hex[:8]}_{_safe_filename(source.name)}"
        shutil.copy2(source.resolve(), destination)
        try:
            relative_src = destination.relative_to(workspace.root).as_posix()
            facts = self._executor(workspace).probe(relative_src)
            if facts.video is None or facts.width <= 0 or facts.height <= 0:
                raise IRValidationError("无法读取图片尺寸")
            source_hash = self._cache_for(project_id).source_hash(destination)
            display_name = (name or source.stem)[:200]
            with self.db.session() as session:
                existing = session.scalar(
                    select(ProjectOverlayAsset).where(
                        ProjectOverlayAsset.project_id == project_id,
                        ProjectOverlayAsset.source_hash == source_hash,
                        ProjectOverlayAsset.name == display_name,
                    )
                )
                if existing is not None:
                    existing.src = relative_src
                    existing.width = facts.width
                    existing.height = facts.height
                    session.flush()
                    row = existing
                else:
                    row = ProjectOverlayAsset(
                        id=f"o_{uuid4().hex[:10]}",
                        project_id=project_id,
                        name=display_name,
                        src=relative_src,
                        mime=f"image/{suffix.lstrip('.')}",
                        width=facts.width,
                        height=facts.height,
                        source_hash=source_hash,
                    )
                    session.add(row)
                    session.flush()
                self._cache_for(project_id).remember_hash(destination, source_hash)
                return self._overlay_asset_dict(row)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def list_overlay_assets(self, project_id: str) -> list[dict]:
        self._require_project(project_id)
        with self.db.session() as session:
            rows = session.scalars(
                select(ProjectOverlayAsset)
                .where(ProjectOverlayAsset.project_id == project_id)
                .order_by(ProjectOverlayAsset.created_at.asc())
            ).all()
            return [self._overlay_asset_dict(row) for row in rows]

    def delete_overlay_asset(self, project_id: str, asset_id: str) -> dict:
        self._require_project(project_id)
        with self.db.session() as session:
            row = session.scalar(
                select(ProjectOverlayAsset).where(
                    ProjectOverlayAsset.project_id == project_id,
                    ProjectOverlayAsset.id == asset_id,
                )
            )
            if row is None:
                raise ResourceNotFound(f"水印资产不存在: {asset_id}")
            session.delete(row)
            src = row.src
        workspace = self.workspaces.get(project_id)
        workspace.resolve_input(src).unlink(missing_ok=True)
        return {"deleted": asset_id}

    def import_audio_asset(self, project_id: str, src: str, name: str | None = None) -> dict:
        self._require_project(project_id)
        workspace = self.workspaces.get(project_id)
        source = Path(src).expanduser()
        if not source.is_file():
            raise ResourceNotFound(f"音频文件不存在: {src}")
        safe_name = _safe_filename(name or source.name)
        destination = workspace.audio_dir / f"{uuid4().hex[:8]}_{safe_name}"
        shutil.copy2(source.resolve(), destination)
        try:
            return self._register_audio_asset(
                project_id,
                workspace,
                destination,
                display_name=name or source.stem,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _register_audio_asset(
        self,
        project_id: str,
        workspace: Workspace,
        path: Path,
        *,
        display_name: str,
        source_hash: str | None = None,
    ) -> dict:
        relative_src = path.relative_to(workspace.root).as_posix()
        executor = self._executor(workspace)
        facts = executor.probe(relative_src)
        if not facts.has_audio:
            raise IRValidationError(f"文件没有音频轨: {display_name}")
        if source_hash is None:
            source_hash = self._cache_for(project_id).source_hash(path)
        with self.db.session() as session:
            existing = session.scalar(
                select(ProjectAudioAsset).where(
                    ProjectAudioAsset.project_id == project_id,
                    ProjectAudioAsset.source_hash == source_hash,
                    ProjectAudioAsset.name == display_name,
                )
            )
            if existing is not None:
                existing.src = relative_src
                existing.facts_json = facts.model_dump_json()
                session.flush()
                row = existing
            else:
                row = ProjectAudioAsset(
                    id=f"a_{uuid4().hex[:10]}",
                    project_id=project_id,
                    name=display_name[:200],
                    src=relative_src,
                    facts_json=facts.model_dump_json(),
                    source_hash=source_hash,
                )
                session.add(row)
                session.flush()
            self._cache_for(project_id).remember_hash(path, source_hash)
            return self._audio_asset_dict(row)

    @property
    def separation_available(self) -> bool:
        return demucs_available(self.settings)

    def separate_audio(
        self,
        project_id: str,
        reporter: JobReporter | None = None,
        model: str | None = None,
        stems: int = 2,
    ) -> dict:
        if stems not in {2, 4}:
            raise IRValidationError(f"只支持 2/4 stems，收到 {stems}")
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if not facts.has_audio:
            raise IRValidationError("素材没有音频轨，无法分离")
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        selected_model = model or self.settings.demucs_model
        key = cache.key(source_hash, "demucs_v1", selected_model, stems)
        cached = cache.get_json(project_id, "stems", key, source_hash)
        if cached is not None:
            return {**cached, "cached": True}
        if not self.separation_available:
            raise UnsupportedFeature(
                "未配置 Demucs：安装 demucs 或设置 SMARTCUT_DEMUCS_COMMAND"
            )

        cancel_token = getattr(reporter, "cancel_token", None)

        def progress(percent: int, message: str) -> None:
            if reporter is not None:
                reporter.progress(percent, message)

        progress(2, "准备分离音频…")
        audio_key = cache.key(source_hash, "demucs_audio_v1")
        wav_path = cache.cache_path("separation_audio", audio_key, ".wav")
        if not wav_path.exists():
            executor = self._executor(workspace, cancel_token)
            executor.execute(
                ToolCall(
                    seq=0,
                    tool="extract_audio",
                    args={
                        "src": asset.src,
                        "dst": cache.relative(wav_path),
                        "channels": 2,
                        "sample_rate": 44100,
                    },
                )
            )
        progress(4, "Demucs 分离中…")
        raw_dir = workspace.tmp_dir / "separation" / f"demucs_{uuid4().hex[:8]}"
        raw_dir.mkdir(parents=True, exist_ok=True)
        separator = build_separator(
            self.settings,
            workspace,
            resources=self.resources,
            logger=self.logger,
        )
        try:
            result = separator.separate(
                wav_path,
                raw_dir,
                model=selected_model,
                stems=stems,
                cancel_token=cancel_token,
                progress=progress,
            )
            stems_dir = cache.cache_dir("stems", key)
            stem_paths: dict[str, Path] = {}
            for name, path in result.stems.items():
                destination = stems_dir / f"{name}.wav"
                if path.resolve() != destination.resolve():
                    shutil.copy2(path, destination)
                stem_paths[name] = destination
        finally:
            shutil.rmtree(raw_dir, ignore_errors=True)

        display = {
            "vocals": "人声",
            "instrumental": "伴奏",
            "drums": "鼓",
            "bass": "贝斯",
            "other": "其他",
        }
        assets: list[dict] = []
        for name, path in stem_paths.items():
            label = display.get(name, name)
            assets.append(
                self._register_audio_asset(
                    project_id,
                    workspace,
                    path,
                    display_name=label,
                    source_hash=cache.source_hash(path),
                )
            )
        payload = {
            "model": selected_model,
            "stems_requested": stems,
            "source_hash": source_hash,
            "stems": assets,
            "command": result.command,
            "duration_ms": result.duration_ms,
            "metadata": result.metadata,
            "cached": False,
        }
        cache.put_json(
            project_id,
            "stems",
            key,
            payload,
            source_hash=source_hash,
        )
        progress(95, "分离完成，已注册音轨素材")
        return payload

    def rename_project(self, project_id: str, name: str) -> dict:
        cleaned = name.strip()[:200]
        if not cleaned:
            raise IRValidationError("项目名称不能为空")
        with self.db.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise ResourceNotFound(f"项目不存在: {project_id}")
            project.name = cleaned
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> dict:
        with self.jobs.project_lifecycle(project_id, for_delete=True):
            return self._delete_project_locked(project_id)

    def prune_cache(self, project_id: str, *, dry_run: bool = False) -> dict:
        """淘汰项目可重算缓存到 ``cache_max_mb`` 上限；有活动任务则跳过。"""
        self._require_project(project_id)
        cache = self._cache_for(project_id)
        return self.jobs.prune_project_cache(
            project_id,
            cache,
            max_bytes=int(self.settings.cache_max_mb) * 1024 * 1024,
            dry_run=dry_run,
        )

    def _delete_project_locked(self, project_id: str) -> dict:
        self._require_project(project_id)
        with self.db.session() as session:
            active = session.scalar(
                select(func.count(Job.id)).where(
                    Job.project_id == project_id,
                    Job.status.in_(("queued", "running")),
                )
            )
            if active:
                raise IRValidationError("项目仍有排队/运行中的任务，请先取消")
        # 已取消但线程尚未收敛的任务：限时等待，超时则拒绝删除，
        # 避免后台线程在 rmtree 之后继续写 revisions/artifacts/缓存。
        if not self.jobs.cancel_project(project_id):
            raise IRValidationError("项目仍有任务未停止，请稍后重试")
        with self.db.session() as session:
            job_ids = session.scalars(
                select(Job.id).where(Job.project_id == project_id)
            ).all()
            if job_ids:
                session.execute(
                    delete(ToolCallRecord).where(ToolCallRecord.job_id.in_(job_ids))
                )
            session.execute(delete(Job).where(Job.project_id == project_id))
            session.execute(
                delete(OrchestrationTaskOutcome).where(
                    OrchestrationTaskOutcome.project_id == project_id
                )
            )
            session.execute(
                delete(OutlineApprovalRecord).where(
                    OutlineApprovalRecord.project_id == project_id
                )
            )
            session.execute(
                delete(OrchestrationRun).where(OrchestrationRun.project_id == project_id)
            )
            session.execute(
                delete(EditFeedback).where(EditFeedback.project_id == project_id)
            )
            session.execute(delete(Revision).where(Revision.project_id == project_id))
            session.execute(delete(MediaAsset).where(MediaAsset.project_id == project_id))
            session.execute(
                delete(ProjectAudioAsset).where(
                    ProjectAudioAsset.project_id == project_id
                )
            )
            session.execute(
                delete(ProjectOverlayAsset).where(
                    ProjectOverlayAsset.project_id == project_id
                )
            )
            session.execute(delete(Artifact).where(Artifact.project_id == project_id))
            session.execute(delete(Project).where(Project.id == project_id))

        self.candidate_index.clear_project(project_id)
        with self._project_caches_lock:
            self._project_caches.pop(project_id, None)
        self._perception_cache.pop(project_id, None)
        # 立墓碑后再删目录；使用 project_root 而不是 get，避免 get 重建目录。
        self.workspaces.mark_deleted(project_id)
        root = self.workspaces.project_root(project_id)
        if root.exists() and root.is_relative_to(self.workspaces.root):
            shutil.rmtree(root, ignore_errors=True)
        return {"deleted": project_id}

    def artifacts(self, project_id: str) -> list[str]:
        workspace = self.workspaces.get(project_id)
        result: list[str] = []
        for base in (workspace.renders_dir, workspace.root / "exports"):
            if not base.exists():
                continue
            result.extend(
                path.relative_to(workspace.root).as_posix()
                for path in base.rglob("*")
                if path.is_file()
            )
        return sorted(result)

    def ingest(self, project_id: str, src: str) -> dict:
        self._require_project(project_id)
        workspace = self.workspaces.get(project_id)
        source = Path(src).expanduser()
        if not source.is_file():
            raise ResourceNotFound(f"源文件不存在: {src}")
        source = source.resolve()

        filename = _safe_filename(source.name)
        destination = workspace.media_dir / filename
        if destination.exists():
            destination = (
                workspace.media_dir / f"{destination.stem}_{uuid4().hex[:6]}{destination.suffix}"
            )
        # 只读源文件 → 工作区副本；之后的 probe/ffmpeg 只接触副本。
        shutil.copy2(source, destination)
        return self._register_asset(project_id, workspace, destination)

    def ingest_upload(
        self,
        project_id: str,
        filename: str,
        stream: BinaryIO,
    ) -> dict:
        self._require_project(project_id)
        workspace = self.workspaces.get(project_id)
        safe_name = _safe_filename(filename or "upload.mp4")
        destination = workspace.media_dir / f"{uuid4().hex[:8]}_{safe_name}"
        limit = self.settings.max_upload_mb * 1024 * 1024

        written = 0
        try:
            with destination.open("wb") as output:
                while True:
                    chunk = stream.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise IRValidationError(
                            f"上传超过限制 {self.settings.max_upload_mb} MB"
                        )
                    output.write(chunk)
            if written == 0:
                raise IRValidationError("上传文件为空")
            return self._register_asset(project_id, workspace, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def _register_asset(self, project_id: str, workspace: Workspace, destination: Path) -> dict:
        relative_src = destination.relative_to(workspace.root).as_posix()
        executor = self._executor(workspace)
        facts = executor.probe(relative_src)
        source_hash = self._cache_for(project_id).source_hash(destination)

        # 新素材导入后，旧的预览代理失效。
        (workspace.proxy_dir / "preview.mp4").unlink(missing_ok=True)

        with self.db.session() as session:
            asset = self._latest_asset(session, project_id)
            if asset is None:
                asset = MediaAsset(
                    id=f"m_{uuid4().hex[:10]}",
                    project_id=project_id,
                    src=relative_src,
                    facts_json=facts.model_dump_json(),
                    source_hash=source_hash,
                )
                session.add(asset)
            else:
                asset.src = relative_src
                asset.facts_json = facts.model_dump_json()
                asset.transcript_json = None
                asset.source_hash = source_hash
            session.flush()
            asset_id = asset.id

        # 媒体缓存按项目工作区隔离；源哈希随素材记录持久化，重启后无需重算。
        self._cache_for(project_id).remember_hash(destination, source_hash)
        return {
            "asset_id": asset_id,
            "src": relative_src,
            "workspace_dir": str(workspace.root),
            "facts": facts.model_dump(mode="json"),
            "source_hash": source_hash,
        }

    def transcribe(
        self,
        project_id: str,
        reporter: JobReporter | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        cancel_token = getattr(reporter, "cancel_token", None)
        # 每个 Job 独占一个 transcriber；跨 Job 不共享 CTranslate2 模型。
        transcriber = self._new_transcriber()
        identity = getattr(transcriber, "cache_identity", None) or getattr(
            transcriber, "name", type(transcriber).__name__
        )

        cache_hits = 0
        stats: dict[str, Any] = {}
        chunks: list[chunking.AudioChunk] = []
        if not facts.has_audio:
            transcript = Transcript(degraded=True, note="源素材没有音频轨")
        elif getattr(transcriber, "degraded", False):
            transcript = transcriber.transcribe(workspace.resolve_input(asset.src))
        else:
            audio_key = cache.key(source_hash, "audio_v1")
            audio_path = cache.cache_path("audio", audio_key, ".wav")
            audio_rel = cache.relative(audio_path)
            if not audio_path.exists():
                executor = self._executor(workspace, cancel_token)
                audio_timeout = compute_tool_timeout(
                    "extract_audio",
                    duration_seconds=facts.duration.to_float(),
                    width=facts.width,
                    height=facts.height,
                    settings=self.settings,
                )
                result = self._execute_tool(
                    executor,
                    ToolCall(
                        seq=0,
                        tool="extract_audio",
                        args={"src": asset.src, "dst": audio_rel},
                    ),
                    timeout_seconds=audio_timeout,
                )
                if reporter is not None:
                    reporter.record_tool(0, result)
                cache.put(
                    project_id,
                    "audio",
                    audio_key,
                    audio_path,
                    source_hash=source_hash,
                )
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            if reporter is not None:
                reporter.progress(10, "音频已就绪，分块转写中")

            chunks_dir = cache.cache_dir("audio_chunks", audio_key)
            chunks = chunking.split_wav(
                audio_path,
                chunks_dir,
                chunk_seconds=self.settings.transcribe_chunk_seconds,
                overlap_seconds=self.settings.transcribe_chunk_overlap_seconds,
            )
            stats = self._transcribe_chunks(
                project_id,
                cache,
                chunks,
                source_hash,
                audio_key,
                identity,
                transcriber=transcriber,
                cancel_token=cancel_token,
                reporter=reporter,
            )
            transcript = stats.pop("transcript")
            cache_hits += stats["cache_hits"]
            transcript_key = cache.key(
                source_hash, audio_key, "transcript_v1", identity
            )
            cache.put_json(
                project_id,
                "transcript",
                transcript_key,
                transcript.model_dump(mode="json"),
                source_hash=source_hash,
            )

        with self.db.session() as session:
            stored = session.get(MediaAsset, asset.id)
            if stored is None:
                raise ResourceNotFound("素材记录已消失")
            stored.transcript_json = transcript.model_dump_json()

        return {
            "degraded": transcript.degraded,
            "language": transcript.language,
            "segments": len(transcript.segments),
            "words": len(transcript.words),
            "note": transcript.note,
            "chunks": len(chunks),
            "cache_hits": cache_hits,
            "cache_misses": stats.get("cache_misses", 0),
            "silent_chunks": stats.get("silent_chunks", 0),
            "transcribe_workers": stats.get("workers", 1),
            "transcribe_device": self._transcribe_device(transcriber),
            "chapter_ids": chapter_ids or [],
        }

    def _worker_transcribers(self, workers: int, transcriber=None) -> list:
        target = transcriber or self.transcriber
        if workers <= 1:
            return [target]
        clone = getattr(target, "clone", None)
        if callable(clone):
            return [target, *[clone() for _ in range(workers - 1)]]
        if getattr(target, "thread_safe", False):
            return [target] * workers
        return [target]

    def _transcribe_device(self, transcriber=None) -> str:
        target = transcriber or self.transcriber
        resolved = getattr(target, "resolved_device", None)
        if isinstance(resolved, str) and resolved:
            return resolved
        return str(self.settings.whisper_device)

    def _effective_transcribe_workers(self, uncached: int, transcriber=None) -> int:
        target = transcriber or self.transcriber
        if uncached <= 1:
            return 1
        configured = int(self.settings.transcribe_workers)
        clone = getattr(target, "clone", None)
        if configured > 0:
            requested = configured
        elif callable(clone) and self._transcribe_device(target).lower() == "cpu":
            requested = 2
        else:
            requested = 1
        if requested > 1 and not (
            callable(clone) or getattr(target, "thread_safe", False)
        ):
            requested = 1
        return max(
            1,
            min(requested, max(1, self.settings.cpu_concurrency), uncached),
        )

    def _transcribe_one(self, transcriber, chunk: chunking.AudioChunk, cancel_token):
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        with self.resources.cpu_slot(cancel_token=cancel_token):
            if getattr(transcriber, "supports_cancel_token", False):
                return transcriber.transcribe(
                    chunk.path,
                    cancel_token=cancel_token,
                )
            return transcriber.transcribe(chunk.path)

    def _close_transcriber(self, transcriber) -> None:
        closer = getattr(transcriber, "close", None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as exc:
            self.logger.debug(
                "transcriber_close_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _transcribe_chunks(
        self,
        project_id: str,
        cache: ArtifactCache,
        chunks: list[chunking.AudioChunk],
        source_hash: str,
        audio_key: str,
        identity: str,
        *,
        transcriber,
        cancel_token=None,
        reporter: JobReporter | None = None,
    ) -> dict:
        results: dict[int, Transcript] = {}
        pending: list[tuple[chunking.AudioChunk, str]] = []
        cache_hits = 0
        silent_chunks = 0
        total = max(len(chunks), 1)
        completed = 0

        def progress() -> None:
            nonlocal completed
            completed += 1
            if reporter is not None:
                reporter.progress(
                    10 + int(80 * completed / total),
                    f"转写 {completed}/{len(chunks)} 块",
                )

        for chunk in chunks:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            chunk_key = cache.key(
                source_hash,
                audio_key,
                "chunk_v1",
                chunk.index,
                round(chunk.start, 3),
                round(chunk.end, 3),
                identity,
            )
            cached = cache.get_json(
                project_id,
                "transcript_chunk",
                chunk_key,
                source_hash,
            )
            if cached is not None:
                results[chunk.index] = Transcript.model_validate(cached)
                cache_hits += 1
                progress()
                continue
            if chunking.is_silent_chunk(
                chunk,
                threshold=self.settings.transcribe_silence_rms,
                min_seconds=self.settings.transcribe_silence_min_seconds,
            ):
                silent = Transcript(
                    language=None,
                    words=[],
                    segments=[],
                    degraded=False,
                    note="静音块，已跳过硬解码",
                )
                results[chunk.index] = silent
                silent_chunks += 1
                cache.put_json(
                    project_id,
                    "transcript_chunk",
                    chunk_key,
                    silent.model_dump(mode="json"),
                    source_hash=source_hash,
                )
                progress()
                continue
            pending.append((chunk, chunk_key))

        workers = self._effective_transcribe_workers(len(pending), transcriber)
        if pending:
            pool_transcribers = self._worker_transcribers(workers, transcriber)
            if len(pool_transcribers) < workers:
                workers = len(pool_transcribers)
            self.logger.info(
                "transcribe_parallel",
                chunks=len(chunks),
                pending=len(pending),
                workers=workers,
            )
            pool = ThreadPoolExecutor(
                max_workers=max(1, workers),
                thread_name_prefix="transcribe",
            )
            try:
                futures = {
                    pool.submit(
                        self._transcribe_one,
                        pool_transcribers[index % len(pool_transcribers)],
                        chunk,
                        cancel_token,
                    ): (chunk, chunk_key)
                    for index, (chunk, chunk_key) in enumerate(pending)
                }
                for future in as_completed(futures):
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    chunk, chunk_key = futures[future]
                    chunk_transcript = future.result()
                    cache.put_json(
                        project_id,
                        "transcript_chunk",
                        chunk_key,
                        chunk_transcript.model_dump(mode="json"),
                        source_hash=source_hash,
                    )
                    results[chunk.index] = chunk_transcript
                    progress()
            except BaseException:
                # 取消时不要等待所有已排队分块；未开始的直接取消，
                # 正在跑的分块会在下一轮检查点自行退出。
                pool.shutdown(wait=False, cancel_futures=True)
                for item in pool_transcribers:
                    self._close_transcriber(item)
                raise
            else:
                pool.shutdown(wait=True)
                for item in pool_transcribers:
                    self._close_transcriber(item)

        chunk_results = [
            (chunk, results[chunk.index])
            for chunk in chunks
            if chunk.index in results
        ]
        return {
            "transcript": chunking.merge_transcripts(chunk_results),
            "cache_hits": cache_hits,
            "cache_misses": len(pending),
            "silent_chunks": silent_chunks,
            "workers": workers,
        }

    def latest_proxy(self, project_id: str) -> dict | None:
        asset = self._require_asset(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        record = cache.latest(project_id, "proxy", source_hash)
        if record is None:
            return None
        return {
            "proxy": record["path"],
            "cached": True,
            "encoder": record["meta"].get("encoder"),
        }

    def build_waveform(
        self,
        project_id: str,
        buckets: int | None = None,
        cancel_token=None,
    ) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if not facts.has_audio:
            return {"src": asset.src, "available": False, "warning": "素材没有音频轨"}
        buckets = max(64, min(int(buckets or self.settings.waveform_buckets), 8000))
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        key = cache.key(source_hash, "waveform_v1", buckets)
        cached = cache.get_json(project_id, "waveform", key, source_hash)
        if cached is not None:
            return {**cached, "cached": True}

        audio_key = cache.key(source_hash, "audio_v1")
        audio_path = cache.cache_path("audio", audio_key, ".wav")
        if not audio_path.exists():
            executor = self._executor(workspace, cancel_token)
            self._execute_tool(
                executor,
                ToolCall(
                    seq=0,
                    tool="extract_audio",
                    args={"src": asset.src, "dst": cache.relative(audio_path)},
                ),
                timeout_seconds=compute_tool_timeout(
                    "extract_audio",
                    duration_seconds=facts.duration.to_float(),
                    width=facts.width,
                    height=facts.height,
                    settings=self.settings,
                ),
            )
            cache.put(
                project_id,
                "audio",
                audio_key,
                audio_path,
                source_hash=source_hash,
            )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        import array
        import wave

        peaks: list[float] = []
        try:
            with wave.open(str(audio_path), "rb") as handle:
                if (
                    handle.getframerate() != 16_000
                    or handle.getnchannels() != 1
                    or handle.getsampwidth() != 2
                ):
                    return {
                        "src": asset.src,
                        "available": False,
                        "warning": "音频不是 16k mono PCM",
                    }
                total = handle.getnframes()
                width = max(1, math.ceil(total / buckets))
                position = 0
                while position < total:
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    handle.setpos(position)
                    raw = handle.readframes(min(width, total - position))
                    if not raw:
                        peaks.append(0.0)
                    else:
                        try:
                            import audioop

                            peak = abs(audioop.max(raw, 2))
                        except Exception:
                            samples = array.array("h")
                            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
                            peak = max((abs(value) for value in samples), default=0)
                        peaks.append(min(1.0, peak / 32768.0))
                    position += width
        except (wave.Error, OSError, ValueError) as exc:
            return {
                "src": asset.src,
                "available": False,
                "warning": f"波形读取失败: {type(exc).__name__}: {exc}",
            }

        payload = {
            "src": asset.src,
            "available": True,
            "peaks": peaks,
            "buckets": len(peaks),
            "duration": facts.duration.to_ffmpeg_seconds(),
        }
        cache.put_json(
            project_id,
            "waveform",
            key,
            payload,
            source_hash=source_hash,
        )
        return {**payload, "cached": False}

    def build_audio_waveforms(
        self,
        project_id: str,
        buckets: int | None = None,
        cancel_token=None,
    ) -> dict:
        """主素材 + 所有项目音频资产的波形；各自内容寻址缓存。"""
        buckets = max(64, min(int(buckets or self.settings.waveform_buckets), 8000))
        main = self.build_waveform(project_id, buckets, cancel_token)
        payload = {
            "main": main.get("peaks") or [],
            "assets": {},
        }
        with self.db.session() as session:
            rows = session.scalars(
                select(ProjectAudioAsset)
                .where(ProjectAudioAsset.project_id == project_id)
                .order_by(ProjectAudioAsset.created_at.asc())
            ).all()
        for row in rows:
            result = self._asset_waveform(project_id, row, buckets, cancel_token)
            payload["assets"][row.id] = result.get("peaks") or []
        return payload

    def _asset_waveform(
        self,
        project_id: str,
        asset: ProjectAudioAsset,
        buckets: int,
        cancel_token,
    ) -> dict:
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if not facts.has_audio:
            return {"src": asset.src, "available": False, "peaks": []}
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = asset.source_hash or cache.source_hash(
            workspace.resolve_input(asset.src)
        )
        key = cache.key(source_hash, "waveform_asset_v1", buckets)
        cached = cache.get_json(project_id, "waveform", key, source_hash)
        if cached is not None:
            return {**cached, "cached": True}

        audio_key = cache.key(source_hash, "audio_asset_v1")
        audio_path = cache.cache_path("audio_asset", audio_key, ".wav")
        if not audio_path.exists():
            executor = self._executor(workspace, cancel_token)
            self._execute_tool(
                executor,
                ToolCall(
                    seq=0,
                    tool="extract_audio",
                    args={"src": asset.src, "dst": cache.relative(audio_path)},
                ),
                timeout_seconds=compute_tool_timeout(
                    "extract_audio",
                    duration_seconds=facts.duration.to_float(),
                    width=facts.width,
                    height=facts.height,
                    settings=self.settings,
                ),
            )
            cache.put(
                project_id,
                "audio_asset",
                audio_key,
                audio_path,
                source_hash=source_hash,
            )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()

        import array
        import wave

        peaks: list[float] = []
        try:
            with wave.open(str(audio_path), "rb") as handle:
                total = handle.getnframes()
                width = max(1, math.ceil(total / buckets))
                position = 0
                while position < total:
                    if cancel_token is not None:
                        cancel_token.raise_if_cancelled()
                    handle.setpos(position)
                    raw = handle.readframes(min(width, total - position))
                    if not raw:
                        peaks.append(0.0)
                    else:
                        try:
                            import audioop

                            peak = abs(audioop.max(raw, 2))
                        except Exception:
                            samples = array.array("h")
                            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
                            peak = max((abs(value) for value in samples), default=0)
                        peaks.append(min(1.0, peak / 32768.0))
                    position += width
        except (wave.Error, OSError, ValueError) as exc:
            return {
                "src": asset.src,
                "available": False,
                "peaks": [],
                "warning": f"波形读取失败: {type(exc).__name__}: {exc}",
            }
        payload = {
            "src": asset.src,
            "available": True,
            "peaks": peaks,
            "buckets": len(peaks),
            "duration": facts.duration.to_ffmpeg_seconds(),
        }
        cache.put_json(
            project_id,
            "waveform",
            key,
            payload,
            source_hash=source_hash,
        )
        return {**payload, "cached": False}

    def build_preview_mix(self, project_id: str, ir: EditIR) -> dict:
        """生成低码率混音预览；参数与资产哈希共同构成缓存键。"""
        asset = self._require_asset(project_id)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        refs = self._audio_asset_refs(project_id)
        fake_warnings: list[str] = []
        total_duration = ir.total_duration()

        tracks_spec = ir.audio.tracks
        adjustments = bool(
            ir.audio.volume_db
            or ir.audio.pitch_semitones
            or ir.audio.loudness is not None
        )
        if tracks_spec is None and not adjustments:
            proxy = workspace.proxy_dir / "preview.mp4"
            path = proxy if proxy.exists() else workspace.resolve(asset.src)
            return {"path": str(path), "cached": True, "legacy": True, "key": None}
        if tracks_spec is None:
            tracks_spec = [
                AudioTrackSpec(
                    id="main",
                    role="main",
                    asset_id="main",
                    source_end=refs["main"].duration if "main" in refs else None,
                )
            ]
            ir = ir.model_copy(
                update={
                    "audio": ir.audio.model_copy(update={"tracks": tracks_spec})
                }
            )
        mix_tracks = resolve_mix_tracks(
            ir, refs, total_duration, fake_warnings
        )
        target_lufs = None
        if ir.audio.loudness is not None:
            target_lufs = parse_target_lufs(ir.audio.loudness)
        proxy = workspace.proxy_dir / "preview.mp4"
        video_src = (
            proxy.relative_to(workspace.root).as_posix()
            if proxy.exists()
            else asset.src
        )
        main_hash = self._source_hash(project_id, asset)
        signature = {
            "video": video_src,
            "main_hash": main_hash,
            "tracks": mix_tracks,
            "duration": total_duration.model_dump(),
            "volume_db": ir.audio.volume_db,
            "pitch_semitones": ir.audio.pitch_semitones,
            "target_lufs": target_lufs,
            "assets": {
                ref_id: ref.source_hash
                for ref_id, ref in refs.items()
                if ref_id == "main" or any(
                    track["src"] == ref.src for track in mix_tracks
                )
            },
        }
        key = cache.key(signature, "preview_mix_v1")
        destination = workspace.proxy_dir / f"mix_{key[:16]}.mp4"
        if destination.exists():
            return {
                "path": str(destination),
                "cached": True,
                "legacy": False,
                "key": key,
            }
        args: dict = {
            "src": video_src,
            "dst": destination.relative_to(workspace.root).as_posix(),
            "tracks": mix_tracks,
            "output_duration": total_duration.model_dump(),
            "volume_db": ir.audio.volume_db,
            "pitch_semitones": ir.audio.pitch_semitones,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
        }
        if target_lufs is not None:
            args["target_lufs"] = target_lufs
        executor = self._executor(workspace)
        self._execute_tool(
            executor,
            ToolCall(seq=0, tool="mix_audio", args=args),
            timeout_seconds=compute_tool_timeout(
                "mix_audio",
                duration_seconds=ir.total_duration().to_float(),
                width=ir.media_facts.width,
                height=ir.media_facts.height,
                settings=self.settings,
            ),
        )
        return {
            "path": str(destination),
            "cached": False,
            "legacy": False,
            "key": key,
            "warnings": fake_warnings,
        }

    def submit_revision(
        self,
        project_id: str,
        ir: EditIR,
        *,
        origin: str = "system",
    ) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if ir.media_facts.src != asset.src:
            raise IRValidationError(
                f"IR 素材 {ir.media_facts.src!r} 与项目素材 {asset.src!r} 不一致"
            )

        sequence = assign_trim_ids(ir.sequence)
        normalized = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=facts,
            sequence=sequence,
            effects=ir.effects,
            transitions=ir.transitions,
            overlays=ir.overlays,
            removals=ir.removals,
            audio=ir.audio,
            subtitles=ir.subtitles,
            export=ir.export,
        )
        validate_ir(normalized)

        stored = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=facts,
            sequence=sequence,
            effects=ir.effects,
            transitions=ir.transitions,
            overlays=ir.overlays,
            removals=ir.removals,
            audio=ir.audio,
            subtitles=ir.subtitles,
            export=ir.export,
        )
        revision_no, stored = self._save_revision(project_id, stored)
        if origin == "user":
            self._record_feedback(project_id, "manual_revision", stored)
        return {"revision": revision_no, "ir": stored.dump_ir()}

    def apply_patch(
        self,
        project_id: str,
        revision_no: int,
        patch: EditPatch,
    ) -> dict:
        """在指定 revision 上应用增量补丁，生成新 revision（不改旧版本）。"""
        base = self.get_revision(project_id, revision_no)
        patched = apply_patch(base, patch)
        sequence = assign_trim_ids(patched.sequence)
        patched = patched.model_copy(update={"sequence": sequence})
        validate_ir(patched)
        stored = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=patched.media_facts,
            sequence=sequence,
            effects=patched.effects,
            transitions=patched.transitions,
            overlays=patched.overlays,
            removals=patched.removals,
            audio=patched.audio,
            subtitles=patched.subtitles,
            export=patched.export,
        )
        new_no, stored = self._save_revision(project_id, stored)
        self._record_feedback(project_id, "patch", stored)
        return {
            "revision": new_no,
            "base_revision": revision_no,
            "rationale": patch.rationale,
            "ir": stored.dump_ir(),
        }

    def _record_feedback(self, project_id: str, kind: str, ir: EditIR) -> None:
        """记录用户编辑的结构化节奏统计；失败不得影响保存。"""
        durations = [op.range.duration().to_float() for op in ir.sequence]
        self._write_feedback(
            project_id,
            kind,
            {
                "source_duration": ir.media_facts.duration.to_float(),
                "output_duration": float(sum(durations)),
                "segments": [
                    {"duration": round(duration, 3), "tag": op.tag}
                    for duration, op in zip(durations, ir.sequence)
                ][:200],
            },
        )

    def record_segment_reject(
        self,
        project_id: str,
        segments: list[dict],
    ) -> dict:
        """记录用户保存前删除的片段，供偏好统计排除被拒标签。"""
        self._require_project(project_id)
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        cleaned: list[dict] = []
        for segment in segments[:200]:
            try:
                start = float(segment.get("start"))
                end = float(segment.get("end"))
            except (TypeError, ValueError):
                continue
            duration = max(0.0, end - start)
            if duration <= 0:
                continue
            tag = segment.get("tag")
            cleaned.append(
                {
                    "duration": round(duration, 3),
                    "tag": str(tag)[:24] if tag else None,
                }
            )
        if not cleaned:
            return {"recorded": 0}
        self._write_feedback(
            project_id,
            "segment_reject",
            {
                "source_duration": facts.duration.to_float(),
                "output_duration": float(
                    sum(item["duration"] for item in cleaned)
                ),
                "segments": cleaned,
                "rejected": True,
            },
        )
        return {"recorded": len(cleaned)}

    def _write_feedback(
        self,
        project_id: str,
        kind: str,
        payload: dict,
    ) -> None:
        try:
            source_hash = self._source_hash(
                project_id, self._require_asset(project_id)
            )
            with self.db.session() as session:
                session.add(
                    EditFeedback(
                        project_id=project_id,
                        kind=kind,
                        source_hash=source_hash,
                        payload_json=json.dumps(payload, ensure_ascii=False),
                    )
                )
        except Exception as exc:
            self.logger.warning(
                "edit_feedback_write_failed",
                project_id=project_id,
                kind=kind,
                error=str(exc),
            )

    def get_edit_preference(self, project_id: str) -> dict | None:
        """聚合最近用户编辑的节奏统计；样本不足时返回 None。"""
        asset = self._require_asset(project_id)
        current_hash = self._source_hash(project_id, asset)
        filters = [EditFeedback.project_id == project_id]
        if self.settings.preference_scope != "project":
            filters.append(EditFeedback.source_hash == current_hash)
        with self.db.session() as session:
            rows = session.scalars(
                select(EditFeedback)
                .where(
                    *filters,
                    EditFeedback.kind.in_(("manual_revision", "patch")),
                )
                .order_by(
                    EditFeedback.created_at.desc(), EditFeedback.id.desc()
                ).limit(50)
            ).all()
            reject_rows = session.scalars(
                select(EditFeedback).where(
                    *filters,
                    EditFeedback.kind == "segment_reject",
                )
            ).all()
        if len(rows) < 2:
            return None

        rejected_tags: set[str] = set()
        for row in reject_rows:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError):
                continue
            for item in payload.get("segments") or []:
                tag = item.get("tag")
                if isinstance(tag, str) and tag.strip():
                    rejected_tags.add(tag.strip()[:24])

        durations: list[float] = []
        keep_ratios: list[float] = []
        tags: Counter[str] = Counter()
        for row in rows:
            try:
                payload = json.loads(row.payload_json)
            except (TypeError, ValueError):
                continue
            source_duration = float(payload.get("source_duration") or 0.0)
            output_duration = float(payload.get("output_duration") or 0.0)
            if source_duration > 0:
                keep_ratios.append(
                    max(0.0, min(1.0, output_duration / source_duration))
                )
            for item in payload.get("segments") or []:
                try:
                    durations.append(max(0.0, float(item.get("duration") or 0.0)))
                except (TypeError, ValueError):
                    continue
                tag = item.get("tag")
                if (
                    isinstance(tag, str)
                    and tag.strip()
                    and tag.strip()[:24] not in rejected_tags
                ):
                    tags[tag.strip()[:24]] += 1
        if not durations or not keep_ratios:
            return None

        stats = PreferenceStats(
            sample_size=len(rows),
            segment_count=len(durations),
            avg_segment_seconds=round(sum(durations) / len(durations), 3),
            median_segment_seconds=round(statistics.median(durations), 3),
            max_segment_seconds=round(max(durations), 3),
            keep_ratio=round(statistics.median(keep_ratios), 4),
            top_tags=[tag for tag, _ in tags.most_common(5)],
        )
        notes = [
            f"历史用户剪辑 {stats.sample_size} 次，片段中位 "
            f"{stats.median_segment_seconds:.1f}s，"
            f"保留率中位 {stats.keep_ratio:.0%}"
        ]
        if rejected_tags:
            notes.append(
                "已排除被拒标签：" + "、".join(sorted(rejected_tags)[:5])
            )
        preference = EditPreference(
            project_id=project_id,
            source_hash=current_hash,
            stats=stats,
            notes=notes,
            updated_at=_iso(rows[0].created_at),
        )
        return preference.model_dump(mode="json")

    def replan(
        self,
        project_id: str,
        revision_no: int,
        goal: str,
        review: dict | None = None,
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> dict:
        """按审片问题重规划并生成新 revision；不修改 base revision。"""
        if not self.planner_available:
            raise PlannerUnavailable("未配置 DEEPSEEK_API_KEY")
        replanner = getattr(self.planner, "replan", None)
        if not callable(replanner):
            raise UnsupportedFeature("当前 Planner 不支持反思式重规划")
        base = self.get_revision(project_id, revision_no)
        transcript = self.get_transcript(project_id)
        perception = self.get_perception(project_id)
        chapters = self.get_chapters(project_id)
        candidates = self.retrieve_candidates(project_id, goal)
        try:
            memory = self.build_video_memory(project_id)["memory"]
        except Exception:
            memory = None
        try:
            preference = self.get_edit_preference(project_id)
        except Exception:
            preference = None
        try:
            history = self.recent_run_digests(project_id)
        except Exception:
            history = []
        new_no = self._peek_revision_no(project_id)

        context_stats: dict = {}
        kwargs: dict[str, Any] = {
            "project_id": project_id,
            "revision": new_no,
            "perception": perception,
            "chapters": chapters,
            "candidates": candidates,
            "memory": memory,
            "preference": preference,
            "history": history,
            "context_stats": context_stats,
        }
        if cancel_token is not None and accepts_keyword(replanner, "cancel_token"):
            kwargs["cancel_token"] = cancel_token
        if usage_scope is not None and accepts_keyword(replanner, "usage_scope"):
            kwargs["usage_scope"] = usage_scope
        if token_budget is not None and accepts_keyword(replanner, "token_budget"):
            kwargs["token_budget"] = token_budget
        if pinned_effects is not None:
            if not accepts_keyword(replanner, "pinned_effects"):
                raise UnsupportedFeature("当前 Planner 不支持锁定特效")
            kwargs["pinned_effects"] = pinned_effects

        before = self._usage_snapshot(self.planner)
        ir = replanner(base, transcript, goal, review or {}, **kwargs)
        usage = self._usage_delta(before, self._usage_snapshot(self.planner))
        validate_ir(ir)
        source = "llm" if self.planner_llm_available else "rule"
        if _plan_signature(ir) == _plan_signature(base):
            # 离线规则规划不会改写时间线；不得制造一个重复 revision。
            return {
                "revision": revision_no,
                "previous_revision": revision_no,
                "changed": False,
                "ir": base.dump_ir(),
                "usage": usage,
                "metrics": {"context": context_stats},
                "warnings": [],
                "source": source,
            }
        stored = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=ir.media_facts,
            sequence=ir.sequence,
            effects=ir.effects,
            transitions=ir.transitions,
            overlays=ir.overlays,
            removals=ir.removals,
            audio=ir.audio,
            subtitles=ir.subtitles,
            export=ir.export,
        )
        new_no, stored = self._save_revision(project_id, stored)
        return {
            "revision": new_no,
            "previous_revision": revision_no,
            "changed": True,
            "ir": stored.dump_ir(),
            "usage": usage,
            "metrics": {"context": context_stats},
            "warnings": [],
            "source": source,
        }

    def plan(
        self,
        project_id: str,
        goal: str,
        perception: dict | None = None,
        chapters: dict | None = None,
        candidates: dict | None = None,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        chapter_ids: list[str] | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> dict:
        if not self.planner_available:
            raise PlannerUnavailable(
                "未配置 DEEPSEEK_API_KEY；请使用 POST /revisions 提交手工 IR"
            )
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        transcript = (
            Transcript.model_validate_json(asset.transcript_json)
            if asset.transcript_json
            else None
        )
        if perception is None:
            perception = self.get_perception(project_id)
        if chapters is None:
            chapters = self.get_chapters(project_id)
        if candidates is None:
            candidates = self.retrieve_candidates(project_id, goal)
        # Planner 只读压缩后的 manifest 摘要，避免各处重复拼 JSON。
        manifest_payload = self.build_video_manifest(
            project_id,
            chapter_ids=chapter_ids,
        )["manifest"]
        perception = {
            **(perception or {}),
            "manifest": manifest_mod.manifest_for_planner(manifest_payload),
        }

        warnings: list[str] = []
        memory = None
        try:
            memory = self.build_video_memory(
                project_id,
                chapter_ids=chapter_ids,
            )["memory"]
        except Exception as exc:
            warnings.append(
                f"记忆索引不可用，规划不使用分层索引: "
                f"{type(exc).__name__}: {exc}"
            )
        history: list[dict] = []
        try:
            history = self.recent_run_digests(project_id)
        except Exception as exc:
            warnings.append(
                f"历史运行摘要不可用: {type(exc).__name__}: {exc}"
            )
        preference = None
        try:
            preference = self.get_edit_preference(project_id)
        except Exception as exc:
            warnings.append(
                f"偏好统计不可用: {type(exc).__name__}: {exc}"
            )
        self._vision_metrics.value = None
        vision_before = self._usage_snapshot(self.vision)
        if visual_notes is None:
            try:
                visual_notes = self.build_visual_notes(
                    project_id,
                    goal,
                    chapters,
                    chapter_ids,
                    cancel_token=cancel_token,
                    usage_scope=usage_scope,
                    token_budget=token_budget,
                )
            except RunCancelledError:
                raise
            except Exception as exc:
                warnings.append(f"视觉摘要失败，已回退文本规划: {type(exc).__name__}: {exc}")
        vision_usage = self._usage_delta(
            vision_before,
            self._usage_snapshot(self.vision),
        )
        vision_metrics = self._take_vision_metrics()
        if not vision_metrics:
            vision_metrics = {
                "images": 0,
                "image_bytes": 0,
                "requests": 0,
                "cache_hits": 0,
                "duration_ms": 0,
                "available": False,
            }
        vision_metrics["requests"] = int(vision_usage.get("requests", 0))
        vision_metrics["cache_hits"] = max(
            int(vision_metrics.get("cache_hits", 0)),
            int(vision_usage.get("cache_hits", 0)),
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            vision_metrics[key] = int(vision_usage.get(key, 0))
        vision_metrics.setdefault("image_bytes", 0)
        vision_metrics.setdefault("images", 0)

        revision_no = self._peek_revision_no(project_id)

        plan_started = time.perf_counter()
        plan_before = self._usage_snapshot(self.planner)
        context_stats: dict = {}
        propose_kwargs = {
            "project_id": project_id,
            "revision": revision_no,
            "perception": perception,
            "chapters": chapters,
            "candidates": candidates,
            "visual_notes": visual_notes,
            "chapter_ids": chapter_ids,
        }
        if timeline is not None and accepts_keyword(self.planner.propose, "timeline"):
            propose_kwargs["timeline"] = timeline
        if memory is not None and accepts_keyword(self.planner.propose, "memory"):
            propose_kwargs["memory"] = memory
        if history and accepts_keyword(self.planner.propose, "history"):
            propose_kwargs["history"] = history
        if preference is not None and accepts_keyword(
            self.planner.propose, "preference"
        ):
            propose_kwargs["preference"] = preference
        if accepts_keyword(self.planner.propose, "context_stats"):
            propose_kwargs["context_stats"] = context_stats
        if accepts_keyword(self.planner.propose, "cancel_token"):
            propose_kwargs["cancel_token"] = cancel_token
        if usage_scope is not None and accepts_keyword(
            self.planner.propose, "usage_scope"
        ):
            propose_kwargs["usage_scope"] = usage_scope
        if token_budget is not None and accepts_keyword(
            self.planner.propose, "token_budget"
        ):
            propose_kwargs["token_budget"] = token_budget
        if pinned_effects is not None:
            if not accepts_keyword(self.planner.propose, "pinned_effects"):
                raise UnsupportedFeature("当前 Planner 不支持锁定特效")
            propose_kwargs["pinned_effects"] = pinned_effects
        ir = self.planner.propose(
            facts,
            transcript,
            goal,
            **propose_kwargs,
        )
        plan_duration_ms = int((time.perf_counter() - plan_started) * 1000)
        plan_usage = self._usage_delta(
            plan_before,
            self._usage_snapshot(self.planner),
        )
        validate_ir(ir)

        revision_no, stored = self._save_revision(project_id, ir)
        return {
            "revision": revision_no,
            "ir": stored.dump_ir(),
            "source": "llm" if self.planner_llm_available else "rule",
            "transcript_degraded": transcript.degraded if transcript is not None else True,
            "visual_notes_used": bool(visual_notes),
            "memory_used": bool(memory),
            "history_runs": len(history),
            "preference_used": preference is not None,
            "warnings": warnings,
            "usage": self._usage_add(plan_usage, vision_usage),
            "metrics": {
                "vision": vision_metrics,
                "context": context_stats,
                "stages": {
                    "vision": int(vision_metrics.get("duration_ms") or 0),
                    "plan": plan_duration_ms,
                },
            },
        }

    def review_edit(
        self,
        project_id: str,
        revision_no: int,
        goal: str,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> dict:
        """语义审片：有 LLM 时调用模型，失败或无 Key 时用确定性规则。"""
        ir = self.get_revision(project_id, revision_no)
        transcript = self.get_transcript(project_id)
        perception = self.get_perception(project_id)
        warnings: list[str] = []
        if self.planner_available and self.planner_llm_available:
            before = self._usage_snapshot(self.planner)
            try:
                review_kwargs = {
                    "perception": perception,
                    "visual_notes": visual_notes,
                }
                if timeline is not None and accepts_keyword(
                    self.planner.review, "timeline"
                ):
                    review_kwargs["timeline"] = timeline
                if accepts_keyword(self.planner.review, "cancel_token"):
                    review_kwargs["cancel_token"] = cancel_token
                if usage_scope is not None and accepts_keyword(
                    self.planner.review, "usage_scope"
                ):
                    review_kwargs["usage_scope"] = usage_scope
                if token_budget is not None and accepts_keyword(
                    self.planner.review, "token_budget"
                ):
                    review_kwargs["token_budget"] = token_budget
                review = self.planner.review(
                    ir,
                    transcript,
                    goal,
                    **review_kwargs,
                )
                payload = review.model_dump(mode="json")
                payload["source"] = "llm"
                payload["usage"] = self._usage_delta(
                    before,
                    self._usage_snapshot(self.planner),
                )
                return payload
            except RunCancelledError:
                raise
            except Exception as exc:
                warnings.append(
                    f"LLM 审片失败，使用规则审片: {type(exc).__name__}: {exc}"
                )

        payload = self._deterministic_review(ir, transcript, perception)
        payload["source"] = "rules"
        payload["warnings"] = warnings
        payload["usage"] = self._empty_usage()
        return payload

    def list_revisions(self, project_id: str) -> list[dict]:
        self._require_project(project_id)
        with self.db.session() as session:
            revisions = session.scalars(
                select(Revision)
                .where(Revision.project_id == project_id)
                .order_by(Revision.revision_no)
            ).all()
            return [
                {
                    "revision": item.revision_no,
                    "created_at": _iso(item.created_at),
                    "ir": json.loads(item.ir_json),
                }
                for item in revisions
            ]

    def list_templates(self) -> list[dict]:
        return self.templates.list()

    def create_template(self, template: EditTemplate) -> dict:
        return self.templates.create(template)

    def delete_template(self, template_id: str) -> dict:
        return self.templates.delete(template_id)

    def apply_template(
        self,
        project_id: str,
        template_id: str,
        *,
        extra_goal: str = "",
    ) -> dict:
        self._require_project(project_id)
        self._require_asset(project_id)
        template = self.templates.get(template_id)
        goal = template.goal_text(extra_goal)
        if not goal:
            raise IRValidationError("模板/附加目标为空")
        run = self.create_orchestration_run(
            project_id,
            goal,
            allow_render=template.allow_render,
            phase=template.phase,
        )
        run_id = run["id"]
        job_id = self.jobs.submit(
            project_id,
            "orchestrate",
            lambda reporter: self.orchestrate_run(run_id, reporter),
            on_cancel=lambda: self.cancel_orchestration_run(run_id),
        )
        return {
            "run_id": run_id,
            "project_id": project_id,
            "template_id": template_id,
            "job_id": job_id,
            "status": "queued",
        }

    def get_revision(self, project_id: str, revision_no: int) -> EditIR:
        with self.db.session() as session:
            revision = session.scalar(
                select(Revision).where(
                    Revision.project_id == project_id,
                    Revision.revision_no == revision_no,
                )
            )
            if revision is None:
                raise ResourceNotFound(f"版本不存在: {project_id}#{revision_no}")
            return EditIR.model_validate_json(revision.ir_json)

    def revision_diff(
        self,
        project_id: str,
        revision_no: int,
        base_revision_no: int,
    ) -> dict:
        """比较两个 revision 的 sequence；纯结构 diff，不包含渲染结果。"""
        if revision_no == base_revision_no:
            raise IRValidationError("base_revision 必须与 revision 不同")
        target = self.get_revision(project_id, revision_no)
        base = self.get_revision(project_id, base_revision_no)
        return diff_ir(base, target)

    def read_logs(self, limit: int = 300) -> list[str]:
        limit = max(1, min(int(limit), 2000))
        path = self.settings.log_file
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return list(deque(handle, maxlen=limit))
        except OSError:
            return []

    def diagnostics(self, limit: int = 300) -> dict:
        info = {
            "workspace_root": str(self.settings.workspace_root),
            "db_path": str(self.settings.db_path),
            "log_file": str(self.settings.log_file),
            "policy_agents_version": self.policy.agents.version,
            "hardware_encoding": self.settings.hardware_encoding,
            "encoder_priority": self.settings.encoder_priority,
            "vision_configured": self.vision.available,
            "demucs_available": self.separation_available,
            "demucs_model": self.settings.demucs_model,
            "transcribe_workers": self.settings.transcribe_workers,
            "perception_max_frames": self.settings.perception_max_frames,
            "candidate_index": self.candidate_index.status(),
            "active_jobs": len(getattr(self.jobs, "_futures", {})),
        }
        return {"info": info, "logs": self.read_logs(limit)}
