"""感知域：抽帧、镜头、质量、人脸、章节、候选与素材清单。"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

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
from smartcut.contracts.encoder import EncoderProfile
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
from smartcut.perception import memory as memory_mod
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
from smartcut.server.candidate_index import CandidateIndex, search_terms
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



class PerceptionMixin:
    def extract_frames(
        self,
        project_id: str,
        fps: float = 1.0,
        cancel_token=None,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if facts.video is None:
            return {"src": asset.src, "available": False, "warning": "素材没有视频轨"}

        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        thumb_width = self.settings.frame_thumb_width
        thumb_height = self.settings.frame_thumb_height
        hwaccel = self._decode_hwaccel(workspace, asset.src)
        decode_mode = "gpu" if hwaccel else "cpu"

        def frame_key(mode: str) -> str:
            return cache.key(
                source_hash,
                "frames_v3",
                fps,
                640,
                thumb_width,
                thumb_height,
                mode,
            )

        key = frame_key(decode_mode)
        cached = cache.get(project_id, "frames", key, source_hash)
        if cached is None and hwaccel:
            fallback = cache.get(
                project_id,
                "frames",
                frame_key("cpu"),
                source_hash,
            )
            if fallback is not None:
                cached = fallback
                key = frame_key("cpu")
                decode_mode = "cpu"
                hwaccel = None
        if cached is not None:
            frames_dir = cache.resolve(cached["path"])
            frames = frames_mod.read_manifest(frames_dir / "manifest.json")
            if frames:
                scoped = self._scope_frames(
                    frames,
                    cache,
                    project_id,
                    source_hash,
                    chapter_ids,
                )
                return {
                    "src": asset.src,
                    "available": True,
                    "count": len(frames),
                    "scoped_count": len(scoped),
                    "chapter_ids": chapter_ids or [],
                    "thumbnail_count": sum(1 for frame in frames if frame.thumbnail),
                    "sampled_fps": fps,
                    "manifest": f"{cached['path']}/manifest.json",
                    "cached": True,
                    "duration_ms": 0,
                    "decoder": decode_mode,
                    "decode_fallback": bool(cached["meta"].get("decode_fallback")),
                    "warnings": (
                        [
                            f"{self.decode_selector.backend} 解码失败，"
                            "extract_frames 已回退 CPU"
                        ]
                        if cached["meta"].get("decode_fallback")
                        else []
                    ),
                }

        final_frames_rel = f"cache/frames/{key}"
        frames_dir_rel = f"{final_frames_rel}.part-{uuid4().hex[:8]}"
        thumbs_dir_rel = f"{frames_dir_rel}/thumbs"
        executor = self._executor(workspace, cancel_token)
        decode_warning = None
        started = time.perf_counter()
        args: dict[str, Any] = {
            "src": asset.src,
            "dst_dir": frames_dir_rel,
            "fps": fps,
            "width": 640,
            "thumbs_dir": thumbs_dir_rel,
            "thumb_width": thumb_width,
            "thumb_height": thumb_height,
        }
        if hwaccel:
            args["hwaccel"] = hwaccel
        frames_timeout = compute_tool_timeout(
            "extract_frames",
            duration_seconds=facts.duration.to_float(),
            width=facts.width,
            height=facts.height,
            settings=self.settings,
        )
        try:
            try:
                tool_result = self._execute_tool(
                    executor,
                    ToolCall(seq=0, tool="extract_frames", args=args),
                    timeout_seconds=frames_timeout,
                )
            except ToolExecutionError as exc:
                if not hwaccel:
                    raise
                self.decode_selector.disable()
                decode_warning = f"{hwaccel} 解码失败，extract_frames 已回退 CPU"
                self.logger.warning(
                    "decode_fallback",
                    tool="extract_frames",
                    hwaccel=hwaccel,
                    error=str(exc)[:500],
                )
                decode_mode = "cpu"
                shutil.rmtree(
                    workspace.resolve(frames_dir_rel),
                    ignore_errors=True,
                )
                key = frame_key("cpu")
                final_frames_rel = f"cache/frames/{key}"
                frames_dir_rel = f"{final_frames_rel}.part-{uuid4().hex[:8]}"
                thumbs_dir_rel = f"{frames_dir_rel}/thumbs"
                args.pop("hwaccel", None)
                args["dst_dir"] = frames_dir_rel
                args["thumbs_dir"] = thumbs_dir_rel
                tool_result = self._execute_tool(
                    executor,
                    ToolCall(seq=0, tool="extract_frames", args=args),
                    timeout_seconds=frames_timeout,
                )
        except BaseException:
            # 取消/失败时丢弃半成品目录，避免后续误读。
            shutil.rmtree(
                workspace.resolve(frames_dir_rel),
                ignore_errors=True,
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        temp_dir = workspace.resolve(frames_dir_rel)
        frames_dir = workspace.resolve(final_frames_rel)
        try:
            if frames_dir.is_dir():
                shutil.rmtree(frames_dir, ignore_errors=True)
            elif frames_dir.exists():
                frames_dir.unlink()
            os.replace(temp_dir, frames_dir)
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        frames = frames_mod.list_frames(
            frames_dir,
            workspace.root,
            fps,
            thumbnail_dir=workspace.resolve(f"{final_frames_rel}/thumbs"),
        )
        frames_mod.write_manifest(frames_dir / "manifest.json", frames)
        cache.put(
            project_id,
            "frames",
            key,
            frames_dir,
            source_hash=source_hash,
            meta={
                "count": len(frames),
                "sampled_fps": fps,
                "thumbnails": sum(1 for frame in frames if frame.thumbnail),
                "thumb_width": thumb_width,
                "thumb_height": thumb_height,
                "duration_ms": duration_ms,
                "tool_duration_ms": tool_result.duration_ms,
                "decoder": decode_mode,
                "decode_fallback": bool(decode_warning),
            },
        )
        scoped = self._scope_frames(
            frames,
            cache,
            project_id,
            source_hash,
            chapter_ids,
        )
        return {
            "src": asset.src,
            "available": True,
            "count": len(frames),
            "scoped_count": len(scoped),
            "chapter_ids": chapter_ids or [],
            "thumbnail_count": sum(1 for frame in frames if frame.thumbnail),
            "sampled_fps": fps,
            "manifest": f"{final_frames_rel}/manifest.json",
            "cached": False,
            "decoder": decode_mode,
            "decode_fallback": bool(decode_warning),
            "warnings": [decode_warning] if decode_warning else [],
            "duration_ms": duration_ms,
        }

    def detect_scenes(self, project_id: str, cancel_token=None) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if facts.video is None:
            return {"src": asset.src, "available": False, "warning": "素材没有视频轨"}

        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        frames_key = frames_record["cache_key"] if frames_record else "no-frames"
        key = cache.key(source_hash, frames_key, "scenes_v1")
        cached = cache.get_json(project_id, "scenes", key, source_hash)
        if cached is not None:
            shots = ShotList.model_validate(cached)
            return self._scenes_summary(asset.src, shots, cached=True)

        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        proxy_record = cache.latest(project_id, "proxy", source_hash)
        if proxy_record is not None:
            video = cache.resolve(proxy_record["path"])
        else:
            video = workspace.resolve_input(asset.src)
        with self.resources.cpu_slot():
            shots = scenes_mod.detect_scenes(video, asset.src)
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        cache.put_json(
            project_id,
            "scenes",
            key,
            shots.model_dump(mode="json"),
            source_hash=source_hash,
        )
        return self._scenes_summary(asset.src, shots, cached=False)

    @staticmethod
    def _scenes_summary(src: str, shots: ShotList, *, cached: bool) -> dict:
        return {
            "src": src,
            "available": True,
            "shot_count": len(shots.shots),
            "shots": [shot.model_dump(mode="json") for shot in shots.shots],
            "cached": cached,
        }

    @staticmethod
    def _chapter_ranges_from_cache(
        cache,
        project_id: str,
        source_hash: str,
        chapter_ids: list[str] | None,
    ) -> list[tuple[float, float]]:
        if not chapter_ids:
            return []
        record = cache.latest(project_id, "chapters", source_hash)
        if record is None:
            return []
        try:
            plan = ChapterPlan.model_validate(
                json.loads(cache.resolve(record["path"]).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            return []
        wanted = set(chapter_ids)
        return [
            (chapter.start.to_float(), chapter.end.to_float())
            for chapter in plan.chapters
            if chapter.id in wanted
        ]

    def _scope_frames(
        self,
        frames,
        cache,
        project_id: str,
        source_hash: str,
        chapter_ids: list[str] | None,
    ):
        ranges = self._chapter_ranges_from_cache(
            cache,
            project_id,
            source_hash,
            chapter_ids,
        )
        if not ranges:
            return frames
        return [
            frame
            for frame in frames
            if any(start <= frame.t.to_float() < end for start, end in ranges)
        ]

    def _select_analysis_frames(
        self,
        frames,
        cache,
        project_id: str,
        source_hash: str,
        max_frames: int = 0,
    ):
        scenes_record = cache.latest(project_id, "scenes", source_hash)
        scene_times: list[float] = []
        if scenes_record is not None:
            try:
                shots = ShotList.model_validate(
                    json.loads(
                        cache.resolve(scenes_record["path"]).read_text(encoding="utf-8")
                    )
                )
                scene_times = [shot.t.to_float() for shot in shots.shots]
            except (OSError, ValueError):
                scene_times = []

        risk_times: list[float] = []
        quality_record = cache.latest(project_id, "quality", source_hash)
        if quality_record is not None:
            try:
                report = QualityReport.model_validate(
                    json.loads(
                        cache.resolve(quality_record["path"]).read_text(
                            encoding="utf-8"
                        )
                    )
                )
                risk_times = [mark.t.to_float() for mark in report.marks]
            except (OSError, ValueError):
                risk_times = []

        topic_times: list[float] = []
        transcript = self.get_transcript(project_id)
        if transcript is not None:
            previous: float | None = None
            for segment in transcript.segments:
                start = segment.start.to_float()
                if previous is None or start - previous >= 2.0:
                    topic_times.append(start)
                previous = start

        chapter_bounds: list[float] = []
        chapters_record = cache.latest(project_id, "chapters", source_hash)
        if chapters_record is not None:
            try:
                plan = ChapterPlan.model_validate(
                    json.loads(
                        cache.resolve(chapters_record["path"]).read_text(
                            encoding="utf-8"
                        )
                    )
                )
                for chapter in plan.chapters:
                    chapter_bounds.append(chapter.start.to_float())
                    chapter_bounds.append(chapter.end.to_float())
            except (OSError, ValueError):
                chapter_bounds = []

        return frames_mod.select_analysis_frames(
            frames,
            scene_times,
            max_frames=max_frames,
            risk_times=risk_times,
            topic_times=topic_times,
            chapter_bounds=chapter_bounds,
            min_gap_seconds=self.settings.perception_min_frame_gap_seconds,
            dedupe=True,
            workspace_root=cache.workspace_root,
        )

    def analyze_quality(
        self,
        project_id: str,
        cancel_token=None,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        asset = self._require_asset(project_id)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            return {"src": asset.src, "available": False, "warning": "尚未抽帧"}
        scenes_record = cache.latest(project_id, "scenes", source_hash)
        kind = "quality_chapter" if chapter_ids else "quality"
        key = cache.key(
            frames_record["cache_key"],
            scenes_record["cache_key"] if scenes_record else None,
            "quality_chapter_v1" if chapter_ids else "quality_v2",
            self.settings.perception_max_frames,
            sorted(chapter_ids or []),
        )
        cached_record = cache.get(project_id, kind, key, source_hash)
        if cached_record is not None:
            report = QualityReport.model_validate(
                json.loads(
                    cache.resolve(cached_record["path"]).read_text(encoding="utf-8")
                )
            )
            meta = cached_record.get("meta") or {}
            return self._quality_summary(
                asset.src,
                report,
                cached=True,
                total_frames=int(meta.get("total_frames", 0)),
                duration_ms=0,
            )

        frames = frames_mod.read_manifest(
            cache.resolve(frames_record["path"]) / "manifest.json"
        )
        frames = self._scope_frames(
            frames,
            cache,
            project_id,
            source_hash,
            chapter_ids,
        )
        total_frames = len(frames)
        frames = self._select_analysis_frames(
            frames,
            cache,
            project_id,
            source_hash,
            max_frames=self.settings.perception_max_frames,
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        started = time.perf_counter()
        workers = min(self.settings.cpu_concurrency, len(frames) or 1)
        report = quality_mod.analyze_quality(
            frames,
            workspace.root,
            asset.src,
            sampled_fps=1.0,
            max_workers=workers,
            slot=(
                partial(self.resources.cpu_slot, cancel_token=cancel_token)
                if cancel_token is not None
                else self.resources.cpu_slot
            ),
            process_pool_enabled=process_pool.should_use_process_pool(
                self.settings,
                len(frames),
            ),
            process_workers=workers,
            pool_slot=(
                partial(self.resources.cpu_slots, cancel_token=cancel_token)
                if cancel_token is not None
                else self.resources.cpu_slots
            ),
            cancel_check=(
                cancel_token.raise_if_cancelled
                if cancel_token is not None
                else None
            ),
            pool_timeout_seconds=self.settings.perception_process_pool_timeout_seconds,
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        duration_ms = int((time.perf_counter() - started) * 1000)
        cache.put_json(
            project_id,
            kind,
            key,
            report.model_dump(mode="json"),
            source_hash=source_hash,
            meta={
                "analyzed_frames": len(frames),
                "total_frames": total_frames,
                "chapter_ids": chapter_ids or [],
            },
        )
        return self._quality_summary(
            asset.src,
            report,
            cached=False,
            total_frames=total_frames,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _quality_summary(
        src: str,
        report: QualityReport,
        *,
        cached: bool,
        total_frames: int = 0,
        duration_ms: int = 0,
    ) -> dict:
        by_kind: dict[str, int] = {}
        for mark in report.marks:
            by_kind[mark.kind.value] = by_kind.get(mark.kind.value, 0) + 1
        return {
            "src": src,
            "available": True,
            "mark_count": len(report.marks),
            "by_kind": by_kind,
            "report": report.model_dump(mode="json"),
            "analyzed_frames": report.frame_count,
            "dropped_frames": max(0, total_frames - report.frame_count),
            "engine": report.engine,
            "duration_ms": duration_ms,
            "cached": cached,
        }

    def detect_faces(
        self,
        project_id: str,
        cancel_token=None,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        asset = self._require_asset(project_id)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            return {"src": asset.src, "available": False, "warning": "尚未抽帧"}
        scenes_record = cache.latest(project_id, "scenes", source_hash)
        kind = "faces_chapter" if chapter_ids else "faces"
        key = cache.key(
            frames_record["cache_key"],
            scenes_record["cache_key"] if scenes_record else None,
            "faces_chapter_v1" if chapter_ids else "faces_v2",
            self.settings.perception_max_frames,
            sorted(chapter_ids or []),
        )
        cached_record = cache.get(project_id, kind, key, source_hash)
        if cached_record is not None:
            report = FaceReport.model_validate(
                json.loads(
                    cache.resolve(cached_record["path"]).read_text(encoding="utf-8")
                )
            )
            meta = cached_record.get("meta") or {}
            return self._faces_summary(
                asset.src,
                report,
                cached=True,
                total_frames=int(meta.get("total_frames", 0)),
                analyzed_frames=int(meta.get("analyzed_frames", 0)),
                duration_ms=0,
            )

        frames = frames_mod.read_manifest(
            cache.resolve(frames_record["path"]) / "manifest.json"
        )
        frames = self._scope_frames(
            frames,
            cache,
            project_id,
            source_hash,
            chapter_ids,
        )
        total_frames = len(frames)
        frames = self._select_analysis_frames(
            frames,
            cache,
            project_id,
            source_hash,
            max_frames=self.settings.perception_max_frames,
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        started = time.perf_counter()
        workers = min(self.settings.cpu_concurrency, len(frames) or 1)
        report = faces_mod.detect_faces(
            frames,
            workspace.root,
            asset.src,
            sampled_fps=1.0,
            max_workers=workers,
            slot=(
                partial(self.resources.cpu_slot, cancel_token=cancel_token)
                if cancel_token is not None
                else self.resources.cpu_slot
            ),
            process_pool_enabled=process_pool.should_use_process_pool(
                self.settings,
                len(frames),
            ),
            process_workers=workers,
            pool_slot=(
                partial(self.resources.cpu_slots, cancel_token=cancel_token)
                if cancel_token is not None
                else self.resources.cpu_slots
            ),
            cancel_check=(
                cancel_token.raise_if_cancelled
                if cancel_token is not None
                else None
            ),
            pool_timeout_seconds=self.settings.perception_process_pool_timeout_seconds,
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        duration_ms = int((time.perf_counter() - started) * 1000)
        cache.put_json(
            project_id,
            kind,
            key,
            report.model_dump(mode="json"),
            source_hash=source_hash,
            meta={
                "analyzed_frames": len(frames),
                "total_frames": total_frames,
                "chapter_ids": chapter_ids or [],
            },
        )
        return self._faces_summary(
            asset.src,
            report,
            cached=False,
            total_frames=total_frames,
            analyzed_frames=len(frames),
            duration_ms=duration_ms,
        )

    @staticmethod
    def _faces_summary(
        src: str,
        report: FaceReport,
        *,
        cached: bool,
        total_frames: int = 0,
        analyzed_frames: int = 0,
        duration_ms: int = 0,
    ) -> dict:
        return {
            "src": src,
            "available": True,
            "face_frame_count": len(report.anchors),
            "report": report.model_dump(mode="json"),
            "analyzed_frames": analyzed_frames,
            "dropped_frames": max(0, total_frames - analyzed_frames),
            "engine": report.engine,
            "duration_ms": duration_ms,
            "cached": cached,
        }

    def build_contact_sheet(self, project_id: str, cancel_token=None) -> dict:
        asset = self._require_asset(project_id)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            return {"src": asset.src, "available": False, "warning": "尚未抽帧"}
        key = cache.key(
            frames_record["cache_key"], "contactsheet_v2", 4, 320, 180
        )
        cached = cache.get(project_id, "contactsheet", key, source_hash)
        if cached is not None:
            return {
                "src": asset.src,
                "available": True,
                "path": cached["path"],
                "count": cached["meta"].get("count", 0),
                "image_bytes": cached["meta"].get("image_bytes", 0),
                "cached": True,
                "duration_ms": 0,
            }

        frames = frames_mod.read_manifest(
            cache.resolve(frames_record["path"]) / "manifest.json"
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        target = cache.cache_path("contactsheet", key, ".jpg")
        started = time.perf_counter()
        with self.resources.cpu_slot():
            contactsheet_mod.build_contact_sheet(
                frames,
                workspace.root,
                target,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)
        image_bytes = target.stat().st_size if target.exists() else 0
        record = cache.put(
            project_id,
            "contactsheet",
            key,
            target,
            source_hash=source_hash,
            meta={
                "count": len(frames),
                "image_bytes": image_bytes,
                "thumbnails_used": sum(1 for frame in frames if frame.thumbnail),
                "duration_ms": duration_ms,
            },
        )
        return {
            "src": asset.src,
            "available": True,
            "path": record["path"],
            "count": len(frames),
            "image_bytes": image_bytes,
            "cached": False,
            "duration_ms": duration_ms,
        }

    def build_chapters(self, project_id: str, cancel_token=None) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            return {"src": asset.src, "available": False, "warning": "尚未抽帧"}

        transcript = self.get_transcript(project_id)
        transcript_sig = cache.key(
            transcript.model_dump_json() if transcript is not None else "none"
        )
        key = cache.key(
            source_hash,
            frames_record["cache_key"],
            "chapters_v1",
            self.settings.chapter_target_seconds,
            transcript_sig,
        )
        cached = cache.get_json(project_id, "chapters", key, source_hash)
        if cached is not None:
            return self._chapters_summary(ChapterPlan.model_validate(cached), cached=True)

        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        perception = self.get_perception(project_id)
        shots = (
            ShotList.model_validate(perception["shots"])
            if perception.get("shots")
            else None
        )
        quality = (
            QualityReport.model_validate(perception["quality"])
            if perception.get("quality")
            else None
        )
        faces = (
            FaceReport.model_validate(perception["faces"])
            if perception.get("faces")
            else None
        )
        frames = frames_mod.read_manifest(
            cache.resolve(frames_record["path"]) / "manifest.json"
        )
        plan = chapters_mod.build_chapters(
            facts,
            shots,
            transcript,
            quality,
            faces,
            frames,
            target_seconds=self.settings.chapter_target_seconds,
        )
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        cache.put_json(
            project_id,
            "chapters",
            key,
            plan.model_dump(mode="json"),
            source_hash=source_hash,
        )
        return self._chapters_summary(plan, cached=False)

    @staticmethod
    def _chapters_summary(plan: ChapterPlan, *, cached: bool) -> dict:
        return {
            "src": plan.src,
            "available": bool(plan.chapters),
            "chapter_count": len(plan.chapters),
            "chapters": [chapter.model_dump(mode="json") for chapter in plan.chapters],
            "rationale": plan.rationale,
            "cached": cached,
        }

    def get_chapters(self, project_id: str) -> dict | None:
        asset = self._require_asset(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        record = cache.latest(project_id, "chapters", source_hash)
        if record is None:
            return None
        try:
            payload = json.loads(
                cache.resolve(record["path"]).read_text(encoding="utf-8")
            )
            return ChapterPlan.model_validate(payload).model_dump(mode="json")
        except (OSError, ValueError):
            return None

    def build_candidates(
        self,
        project_id: str,
        cancel_token=None,
        chapter_ids: list[str] | None = None,
    ) -> dict:
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if facts.duration.to_float() <= 0:
            return {
                "src": asset.src,
                "available": False,
                "warning": "素材时长为 0",
            }
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        if frames_record is None:
            return {"src": asset.src, "available": False, "warning": "尚未抽帧"}

        transcript = self.get_transcript(project_id)
        perception = self.get_perception(project_id)
        shots = (
            ShotList.model_validate(perception["shots"])
            if perception.get("shots")
            else None
        )
        quality = (
            QualityReport.model_validate(perception["quality"])
            if perception.get("quality")
            else None
        )
        faces = (
            FaceReport.model_validate(perception["faces"])
            if perception.get("faces")
            else None
        )
        chapters = self.get_chapters(project_id)
        if chapter_ids and chapters:
            wanted = set(chapter_ids)
            chapters = {
                **chapters,
                "chapters": [
                    chapter
                    for chapter in chapters.get("chapters") or []
                    if chapter.get("id") in wanted
                ],
            }
            transcript = filter_transcript_to_chapters(transcript, chapters)
        transcript_sig = cache.key(
            transcript.model_dump_json() if transcript is not None else "none"
        )
        chapters_sig = cache.key(
            json.dumps(chapters, sort_keys=True, ensure_ascii=False)
            if chapters is not None
            else "none"
        )
        key = cache.key(
            source_hash,
            frames_record["cache_key"],
            "candidates_v2",
            "dhash_v1",
            self.settings.candidate_target_seconds,
            self.settings.candidate_max,
            transcript_sig,
            chapters_sig,
        )
        cached = cache.get_json(project_id, "candidates", key, source_hash)
        if cached is not None:
            candidate_list = CandidateList.model_validate(cached)
            cache_hit = True
        else:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            candidate_list = candidates_mod.generate_candidates(
                facts,
                transcript,
                shots,
                quality,
                faces,
                chapters,
                target_seconds=self.settings.candidate_target_seconds,
                max_candidates=self.settings.candidate_max,
            )
            frame_refs = frames_mod.read_manifest(
                cache.resolve(frames_record["path"]) / "manifest.json"
            )
            candidate_list = candidates_mod.attach_visual_hashes(
                candidate_list,
                frame_refs,
                workspace.root,
                max_items=self.settings.candidate_max,
            )
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            cache.put_json(
                project_id,
                "candidates",
                key,
                candidate_list.model_dump(mode="json"),
                source_hash=source_hash,
            )
            cache_hit = False

        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        # 每次构建都刷新 FTS 索引，适配数据库重建或索引表被清理的情况。
        self.candidate_index.replace(
            project_id,
            source_hash,
            [item.model_dump(mode="json") for item in candidate_list.candidates],
        )
        return {
            "src": asset.src,
            "available": bool(candidate_list.candidates),
            "candidate_count": len(candidate_list.candidates),
            "candidates": [
                item.model_dump(mode="json") for item in candidate_list.candidates
            ],
            "cached": cache_hit,
        }

    def get_candidates(self, project_id: str) -> dict | None:
        asset = self._require_asset(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        record = cache.latest(project_id, "candidates", source_hash)
        if record is None:
            return None
        try:
            payload = json.loads(
                cache.resolve(record["path"]).read_text(encoding="utf-8")
            )
            return CandidateList.model_validate(payload).model_dump(mode="json")
        except (OSError, ValueError):
            return None

    def retrieve_candidates(
        self,
        project_id: str,
        query: str,
        *,
        limit: int | None = None,
        reference_candidate_id: str | None = None,
        max_distance: int = 10,
    ) -> dict:
        asset = self._require_asset(project_id)
        source_hash = self._source_hash(project_id, asset)
        limit = limit or self.settings.candidate_retrieval_limit

        routes: list[str] = []
        fts_items = [
            Candidate.model_validate(item).model_dump(mode="json")
            for item in self.candidate_index.search(
                project_id,
                source_hash,
                query,
                limit=limit,
            )
        ]
        if fts_items:
            routes.append("fts")

        stored = self.get_candidates(project_id)
        pool = stored["candidates"] if stored is not None else []

        reference = self._candidate_by_id(pool, reference_candidate_id)
        if (
            reference_candidate_id
            and reference is not None
            and reference.get("visual_hash") is None
            and self.settings.candidate_visual_backfill
        ):
            # 旧缓存没有 visual_hash：首次“找相似”自动重建一次。
            self._ensure_visual_hashes(project_id, source_hash)
            stored = self.get_candidates(project_id)
            pool = stored["candidates"] if stored is not None else []
            reference = self._candidate_by_id(pool, reference_candidate_id)

        visual_items: list[dict] = []
        if reference_candidate_id and reference is not None:
            reference_hash = reference.get("visual_hash")
            if isinstance(reference_hash, int):
                visual_items = [
                    Candidate.model_validate(item).model_dump(mode="json")
                    for item in self.candidate_index.visual(
                        project_id,
                        source_hash,
                        reference_hash,
                        limit=limit,
                        max_distance=max(0, min(int(max_distance), 64)),
                    )
                    if str(item.get("id")) != str(reference_candidate_id)
                ]
                if visual_items:
                    routes.append("visual")

        tag_items: list[dict] = []
        terms = {term.lower() for term in search_terms(query)}
        if pool and terms:
            tag_items = [
                item
                for item in pool
                if terms
                & {str(tag).lower() for tag in (item.get("tags") or [])}
            ]
            tag_items.sort(key=self._candidate_rank_key, reverse=True)
            tag_items = tag_items[:limit]
            if tag_items:
                routes.append("tag")

        merged = self._merge_candidates(visual_items, fts_items, tag_items)
        if len(merged) < limit and pool:
            ranked = sorted(pool, key=self._candidate_rank_key, reverse=True)
            before = len(merged)
            merged = self._merge_candidates(merged, ranked[:limit])
            if len(merged) > before:
                routes.append("top")
        items = merged[:limit]
        return {
            "query": query,
            "source": "+".join(routes) if items else "empty",
            "count": len(items),
            "candidates": items,
        }

    @staticmethod
    def _candidate_rank_key(item: dict) -> tuple[float, str]:
        score = float(item.get("score") or 0.0)
        penalty = 0.15 * len(item.get("quality_flags") or [])
        bonus = 0.05 if item.get("shot_boundary") else 0.0
        return (score - penalty + bonus, str(item.get("id") or ""))

    @staticmethod
    def _candidate_by_id(pool: list[dict], candidate_id: str | None) -> dict | None:
        if not candidate_id:
            return None
        target = str(candidate_id)
        return next(
            (item for item in pool if str(item.get("id")) == target),
            None,
        )

    def _ensure_visual_hashes(self, project_id: str, source_hash: str) -> None:
        """对旧候选缓存做一次哈希回填；每素材最多尝试一次，失败不影响检索。"""
        key = (project_id, source_hash)
        with self._visual_backfill_lock:
            if key in self._visual_backfill_attempted:
                return
            self._visual_backfill_attempted.add(key)
        try:
            self.build_candidates(project_id)
        except Exception as exc:
            self.logger.warning(
                "candidate_visual_backfill_failed",
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _merge_candidates(*groups: list[dict]) -> list[dict]:
        merged: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                candidate_id = str(item.get("id") or "")
                if candidate_id and candidate_id in seen:
                    continue
                if candidate_id:
                    seen.add(candidate_id)
                merged.append(item)
        return merged

    def build_video_manifest(
        self,
        project_id: str,
        *,
        chapter_ids: list[str] | None = None,
        refresh: bool = False,
    ) -> dict:
        """组装并缓存 VideoManifest；只引用阶段缓存键与摘要。"""
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        records = {
            "frames": cache.latest(project_id, "frames", source_hash),
            "scenes": cache.latest(project_id, "scenes", source_hash),
            "quality": cache.latest(project_id, "quality", source_hash),
            "faces": cache.latest(project_id, "faces", source_hash),
            "contact_sheet": cache.latest(project_id, "contactsheet", source_hash),
            "chapters": cache.latest(project_id, "chapters", source_hash),
            "candidates": cache.latest(project_id, "candidates", source_hash),
            "vision_notes": cache.latest(project_id, "vision_notes", source_hash),
        }
        chunk_keys = sorted(
            {
                record["cache_key"]
                for record in cache.list_records(
                    project_id,
                    "transcript_chunk",
                    source_hash,
                )
            }
        )
        stage_keys = {
            name: record["cache_key"] if record is not None else ""
            for name, record in records.items()
        }
        key = cache.key(
            source_hash,
            "manifest_v1",
            stage_keys,
            chunk_keys,
            sorted(chapter_ids or []),
        )
        if not refresh:
            cached = cache.get_json(project_id, "manifest", key, source_hash)
            if cached is not None:
                try:
                    manifest = VideoManifest.model_validate(cached)
                    return {
                        "manifest": manifest.model_dump(mode="json"),
                        "cache_key": key,
                        "cached": True,
                    }
                except ValueError:
                    pass

        shots = self._read_cached_model(cache, records["scenes"], ShotList)
        quality = self._read_cached_model(cache, records["quality"], QualityReport)
        faces = self._read_cached_model(cache, records["faces"], FaceReport)
        chapters_payload = self.get_chapters(project_id)
        chapters = (
            ChapterPlan.model_validate(chapters_payload)
            if chapters_payload is not None
            else None
        )
        candidates = self.get_candidates(project_id)
        transcript = self.get_transcript(project_id)

        visual_notes: list[ChapterVisualNote] = []
        notes_payload = cache.get_json(
            project_id,
            "vision_notes",
            records["vision_notes"]["cache_key"],
            source_hash,
        ) if records["vision_notes"] is not None else None
        if isinstance(notes_payload, dict):
            for item in notes_payload.get("chapters") or []:
                try:
                    visual_notes.append(ChapterVisualNote.model_validate(item))
                except ValueError:
                    continue

        chapters, quality, faces, visual_notes = self._scope_manifest(
            chapters,
            quality,
            faces,
            visual_notes,
            chapter_ids,
        )
        manifest = manifest_mod.build_video_manifest(
            facts=facts,
            source_hash=source_hash,
            shots=shots,
            chapters=chapters,
            transcript=transcript,
            quality=quality,
            faces=faces,
            candidates=candidates,
            visual_notes=visual_notes,
            cache_keys=stage_keys,
            chunk_keys=chunk_keys,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
        payload = manifest.model_dump(mode="json")
        cache.put_json(
            project_id,
            "manifest",
            key,
            payload,
            source_hash=source_hash,
            meta={"stages": sorted(stage_keys), "chapter_scope": chapter_ids or []},
        )
        return {"manifest": payload, "cache_key": key, "cached": False}

    def build_video_memory(
        self,
        project_id: str,
        *,
        chapter_ids: list[str] | None = None,
        refresh: bool = False,
    ) -> dict:
        """组装分层记忆索引；按 source_hash + 阶段缓存键寻址。"""
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        records = {
            "frames": cache.latest(project_id, "frames", source_hash),
            "scenes": cache.latest(project_id, "scenes", source_hash),
            "chapters": cache.latest(project_id, "chapters", source_hash),
            "candidates": cache.latest(project_id, "candidates", source_hash),
            "vision_notes": cache.latest(project_id, "vision_notes", source_hash),
        }
        stage_keys = {
            name: record["cache_key"] if record is not None else ""
            for name, record in records.items()
        }
        key = cache.key(
            source_hash,
            "memory_v1",
            stage_keys,
            sorted(chapter_ids or []),
        )
        if not refresh:
            cached = cache.get_json(project_id, "memory", key, source_hash)
            if cached is not None:
                return {"memory": cached, "cache_key": key, "cached": True}

        shots = self._read_cached_model(cache, records["scenes"], ShotList)
        chapters_payload = self.get_chapters(project_id)
        candidates = self.get_candidates(project_id)
        transcript = self.get_transcript(project_id)
        notes_payload = (
            cache.get_json(
                project_id,
                "vision_notes",
                records["vision_notes"]["cache_key"],
                source_hash,
            )
            if records["vision_notes"] is not None
            else None
        )
        index = memory_mod.build_memory_index(
            facts=facts,
            source_hash=source_hash,
            chapters=chapters_payload,
            shots=shots.model_dump(mode="json") if shots is not None else None,
            candidates=candidates,
            visual_notes=notes_payload if isinstance(notes_payload, dict) else None,
            transcript_segments=len(transcript.segments) if transcript is not None else 0,
            cache_keys=stage_keys,
        )
        payload = index.model_dump(mode="json")
        cache.put_json(
            project_id,
            "memory",
            key,
            payload,
            source_hash=source_hash,
            meta={"counts": index.counts, "chapter_scope": chapter_ids or []},
        )
        return {"memory": payload, "cache_key": key, "cached": False}

    @staticmethod
    def _read_cached_model(cache, record, model):
        if record is None:
            return None
        try:
            return model.model_validate(
                json.loads(cache.resolve(record["path"]).read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            return None

    @staticmethod
    def _scope_manifest(
        chapters: ChapterPlan | None,
        quality: QualityReport | None,
        faces: FaceReport | None,
        notes: list[ChapterVisualNote],
        chapter_ids: list[str] | None,
    ):
        if not chapter_ids:
            return chapters, quality, faces, notes
        wanted = set(chapter_ids)
        selected = [
            chapter for chapter in (chapters.chapters if chapters else [])
            if chapter.id in wanted
        ]
        scoped_chapters = (
            ChapterPlan(
                src=chapters.src,
                chapters=selected,
                rationale=chapters.rationale,
                origin=chapters.origin,
            )
            if chapters is not None
            else None
        )
        ranges = [
            (chapter.start.to_float(), chapter.end.to_float())
            for chapter in selected
        ]

        def inside(value: float) -> bool:
            return any(start <= value < end for start, end in ranges)

        scoped_quality = quality
        if quality is not None:
            scoped_quality = QualityReport(
                src=quality.src,
                frame_count=quality.frame_count,
                sampled_fps=quality.sampled_fps,
                marks=[mark for mark in quality.marks if inside(mark.t.to_float())],
                engine=quality.engine,
            )
        scoped_faces = faces
        if faces is not None:
            scoped_faces = FaceReport(
                src=faces.src,
                frame_width=faces.frame_width,
                frame_height=faces.frame_height,
                sampled_fps=faces.sampled_fps,
                anchors=[
                    anchor for anchor in faces.anchors
                    if inside(anchor.t.to_float())
                ],
                engine=faces.engine,
            )
        scoped_notes = [note for note in notes if note.chapter_id in wanted]
        return scoped_chapters, scoped_quality, scoped_faces, scoped_notes

    def get_perception(self, project_id: str) -> dict:
        asset = self._require_asset(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        frames_record = cache.latest(project_id, "frames", source_hash)
        scenes_record = cache.latest(project_id, "scenes", source_hash)
        quality_record = cache.latest(project_id, "quality", source_hash)
        faces_record = cache.latest(project_id, "faces", source_hash)
        contact_record = cache.latest(project_id, "contactsheet", source_hash)
        chapter_record = cache.latest(project_id, "chapters", source_hash)
        signature = (
            source_hash,
            frames_record["cache_key"] if frames_record else None,
            scenes_record["cache_key"] if scenes_record else None,
            quality_record["cache_key"] if quality_record else None,
            faces_record["cache_key"] if faces_record else None,
            contact_record["cache_key"] if contact_record else None,
            chapter_record["cache_key"] if chapter_record else None,
        )
        with self._perception_lock:
            cached = self._perception_cache.get(project_id)
        if cached is not None and cached[0] == signature:
            return cached[1]

        frames = []
        if frames_record is not None:
            frames = frames_mod.read_manifest(
                cache.resolve(frames_record["path"]) / "manifest.json"
            )
        shots = None
        if scenes_record is not None:
            try:
                shots = ShotList.model_validate(
                    json.loads(
                        cache.resolve(scenes_record["path"]).read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValueError):
                shots = None
        quality = None
        if quality_record is not None:
            try:
                quality = QualityReport.model_validate(
                    json.loads(
                        cache.resolve(quality_record["path"]).read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValueError):
                quality = None
        faces = None
        if faces_record is not None:
            try:
                faces = FaceReport.model_validate(
                    json.loads(
                        cache.resolve(faces_record["path"]).read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValueError):
                faces = None

        warnings: list[str] = []
        if not frames:
            warnings.append("尚未抽帧")
        if shots is None:
            warnings.append("尚未进行镜头切分")
        if quality is None:
            warnings.append("尚未进行质量分析")
        if faces is None:
            warnings.append("尚未进行人脸检测")
        chapters: list[Chapter] = []
        if chapter_record is not None:
            try:
                plan = ChapterPlan.model_validate(
                    json.loads(
                        cache.resolve(chapter_record["path"]).read_text(encoding="utf-8")
                    )
                )
                chapters = plan.chapters
            except (OSError, ValueError):
                chapters = []
        data = PerceptionData(
            src=asset.src,
            frame_manifest=frames,
            sampled_fps=1.0,
            shots=shots,
            quality=quality,
            faces=faces,
            contact_sheet=contact_record["path"] if contact_record else None,
            chapters=chapters,
            available=bool(frames or shots or quality or faces),
            warnings=warnings,
        )
        payload = data.model_dump(mode="json")
        with self._perception_lock:
            self._perception_cache[project_id] = (signature, payload)
        return payload

    def get_media_facts(self, project_id: str) -> MediaFacts:
        return MediaFacts.model_validate_json(self._require_asset(project_id).facts_json)

    def get_transcript(self, project_id: str) -> Transcript | None:
        asset = self._require_asset(project_id)
        return (
            Transcript.model_validate_json(asset.transcript_json)
            if asset.transcript_json
            else None
        )

    def probe(self, project_id: str, relative_src: str) -> MediaFacts:
        workspace = self.workspaces.get(project_id)
        return self._executor(workspace).probe(relative_src)
