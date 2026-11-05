"""渲染与转码：代理、编译、工具执行、回退与成片审阅。"""

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
from smartcut.contracts.effects import enabled_output_effects
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
    default_audio_codec,
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


def _rewrite_preview_value(value: Any, prefix: str, replacement: str) -> Any:
    if isinstance(value, str) and value.startswith(prefix):
        return replacement + value[len(prefix) :]
    if isinstance(value, list):
        return [_rewrite_preview_value(item, prefix, replacement) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_preview_value(item, prefix, replacement)
            for key, item in value.items()
        }
    return value



class RenderMixin:
    _VIDEO_CODEC_NAMES = {
        "libx264": "h264",
        "libx265": "hevc",
        "libsvtav1": "av1",
        "prores_ks": "prores",
    }

    _AUDIO_CODEC_NAMES = {
        "aac": "aac",
        "ac3": "ac3",
        "libopus": "opus",
        "libmp3lame": "mp3",
        "flac": "flac",
        "pcm_s16le": "pcm_s16le",
        "pcm_s24le": "pcm_s24le",
    }

    _CONTAINER_AUDIO = {
        "mp4": {"aac", "ac3", "opus"},
        "mov": {"aac", "ac3", "pcm_s16le", "pcm_s24le"},
        "mkv": None,
        "m4a": {"aac", "ac3", "opus"},
        "mp3": {"mp3"},
        "wav": {"pcm_s16le", "pcm_s24le"},
        "flac": {"flac"},
    }

    def create_proxy(self, project_id: str, height: int | None = None) -> dict:
        """生成桌面预览代理；已存在则复用，重新导入素材时由 _register_asset 失效。"""
        height = height or self.settings.proxy_height
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        if facts.video is None:
            return {"proxy": None, "cached": False, "reason": "素材没有视频轨"}

        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        profile = self._encoder_profile(workspace)
        codec = profile.codec if profile is not None else "default"
        hwaccel = self._decode_hwaccel(workspace, asset.src)
        decode_mode = "gpu" if hwaccel else "cpu"
        key = cache.key(source_hash, "proxy_v3", height, codec, decode_mode)
        cached = cache.get(project_id, "proxy", key, source_hash)
        if cached is not None:
            return {
                "proxy": cached["path"],
                "cached": True,
                "encoder": cached["meta"].get("encoder"),
                "decoder": cached["meta"].get("decoder", decode_mode),
            }

        target = cache.cache_path("proxy", key, ".mp4")
        dst = cache.relative(target)
        executor = self._executor(workspace)
        args: dict[str, Any] = {"src": asset.src, "dst": dst, "height": height}
        if profile is not None:
            args["encoder"] = profile.model_dump(mode="json")
        if hwaccel:
            args["hwaccel"] = hwaccel
        call = ToolCall(seq=0, tool="proxy", args=args)
        proxy_timeout = compute_tool_timeout(
            "proxy",
            duration_seconds=facts.duration.to_float(),
            width=facts.width,
            height=facts.height,
            settings=self.settings,
        )
        decode_warning = None
        if not hwaccel:
            _encoded, result, encode_warning = self._execute_with_fallback(
                call,
                executor,
                timeout_seconds=proxy_timeout,
            )
        else:
            try:
                result = self._execute_tool(
                    executor,
                    call,
                    timeout_seconds=proxy_timeout,
                )
                _encoded = call
                encode_warning = None
            except ToolExecutionError as exc:
                self.decode_selector.disable()
                target.unlink(missing_ok=True)
                decode_warning = f"{hwaccel} 解码失败，proxy 已回退 CPU"
                self.logger.warning(
                    "decode_fallback",
                    tool="proxy",
                    hwaccel=hwaccel,
                    error=str(exc)[:500],
                )
                call = ToolCall(
                    seq=0,
                    tool="proxy",
                    args={
                        key: value
                        for key, value in args.items()
                        if key != "hwaccel"
                    },
                )
                _encoded, result, encode_warning = self._execute_with_fallback(
                    call,
                    executor,
                    timeout_seconds=proxy_timeout,
                )
        actual_codec = result.encoder or "libx264"
        actual_decode = "gpu" if "hwaccel" in _encoded.args else "cpu"
        actual_key = cache.key(
            source_hash,
            "proxy_v3",
            height,
            actual_codec,
            actual_decode,
        )
        record = cache.put(
            project_id,
            "proxy",
            actual_key,
            target,
            source_hash=source_hash,
            meta={
                "height": height,
                "encoder": actual_codec,
                "decoder": actual_decode,
                "decode_fallback": bool(decode_warning),
            },
        )
        warnings = [
            item for item in (decode_warning, encode_warning) if item
        ]
        return {
            "proxy": record["path"],
            "cached": False,
            "result": result.model_dump(mode="json"),
            "encoder": actual_codec,
            "decoder": actual_decode,
            "warnings": warnings,
        }

    def render_effects_preview(
        self,
        project_id: str,
        ir: EditIR,
        *,
        max_height: int = 480,
        cancel_token: Any | None = None,
    ) -> dict:
        """渲染低分辨率全片特效预览；失败只返回 error，不抛给 GUI 弹窗。"""
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        height = max(2, int(max_height))
        height -= height % 2
        warnings: list[str] = []

        has_timeline_ops = (
            bool(ir.transitions or ir.overlays or ir.pips or ir.source_facts)
            or len(ir.sequence) > 1
            or any(op.speed != 1.0 for op in ir.sequence)
        )
        if not enabled_output_effects(ir.effects) and not has_timeline_ops:
            proxy = workspace.proxy_dir / "preview.mp4"
            path = proxy if proxy.exists() else workspace.resolve(asset.src)
            return {
                "path": str(path),
                "cached": True,
                "legacy": True,
                "key": None,
                "warnings": [],
            }

        signature = {
            "ir": ir.model_dump(mode="json", by_alias=True),
            "height": height,
        }
        key = cache.key(source_hash, "effects_preview_v1", signature)
        destination = workspace.proxy_dir / f"effects_{key[:16]}" / "output.mp4"
        if destination.exists():
            return {
                "path": str(destination),
                "cached": True,
                "legacy": False,
                "key": key,
                "warnings": [],
            }

        try:
            encoder = self._encoder_profile(workspace)
            plan = lower(
                ir,
                transcript=self.get_transcript(project_id),
                perception=self.get_perception(project_id),
                encoder=encoder,
                caption_style={
                    "font_name": self.settings.caption_font_name,
                    "font_size": self.settings.caption_font_size,
                    "color": self.settings.caption_color,
                    "position": self.settings.caption_position,
                },
                audio_assets=self._audio_asset_refs(project_id),
                overlay_assets=self._overlay_asset_refs(project_id),
                hdr_tonemap=self.settings.render_hdr_tonemap,
                keep_10bit=False,
                fonts_dir=self.settings.caption_fonts_dir,
                preview_height=height,
            )
            preview_dir = f"proxy/effects_{key[:16]}"
            prefix = f"renders/r{ir.revision}/"
            replacement = f"{preview_dir}/"
            calls = [
                ToolCall(
                    seq=call.seq,
                    tool=call.tool,
                    args=_rewrite_preview_value(
                        call.args, prefix, replacement
                    ),
                    description=call.description,
                )
                for call in plan.tool_calls
                if call.tool != "verify"
            ]
            output_rel = _rewrite_preview_value(
                plan.output_path, prefix, replacement
            )
            if plan.captions:
                srt_rel = f"{preview_dir}/captions.srt"
                atomic_write_text(
                    workspace.resolve_output(srt_rel),
                    format_srt(plan.captions),
                )

            executor = self._executor(workspace, cancel_token)
            results: dict[int, ToolResult] = {}

            def progress(_call: ToolCall) -> None:
                return None

            self._render_probe_calls(
                calls, executor, None, results, warnings, progress
            )
            self._render_cut_calls(
                [call for call in calls if call.tool == "cut"],
                executor=executor,
                cache=cache,
                workspace=workspace,
                project_id=project_id,
                source_hash=source_hash,
                facts=facts,
                revision_no=ir.revision,
                cancel_token=cancel_token,
                reporter=None,
                results_by_seq=results,
                warnings=warnings,
                progress=progress,
            )
            for call in calls:
                if call.tool in ("probe", "cut"):
                    continue
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                call_timeout = compute_tool_timeout(
                    call.tool,
                    duration_seconds=ir.total_duration().to_float(),
                    width=facts.width,
                    height=facts.height,
                    settings=self.settings,
                )
                self._execute_tool(
                    executor, call, timeout_seconds=call_timeout
                )
            if not workspace.resolve(output_rel).exists():
                raise ToolExecutionError("特效预览未产出文件")
            return {
                "path": str(workspace.resolve(output_rel)),
                "cached": False,
                "legacy": False,
                "key": key,
                "warnings": warnings,
            }
        except RunCancelledError:
            return {
                "path": None,
                "cached": False,
                "legacy": False,
                "key": key,
                "cancelled": True,
                "warnings": warnings,
            }
        except Exception as exc:
            self.logger.warning(
                "effects_preview_failed",
                project_id=project_id,
                error=str(exc)[:500],
            )
            return {
                "path": None,
                "cached": False,
                "legacy": False,
                "key": key,
                "error": f"{type(exc).__name__}: {exc}",
                "warnings": warnings,
            }

    def review_render(
        self,
        project_id: str,
        revision_no: int,
        *,
        goal: str = "",
        keyframe_count: int = 6,
        cancel_token: Any | None = None,
    ) -> dict:
        """审片工作流：检查成片画面/字幕/音频，只输出问题不改 IR。"""
        ir = self.get_revision(project_id, revision_no)
        asset = self._require_asset(project_id)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        output_rel = f"renders/r{revision_no}/output.mp4"
        workspace.resolve_input(output_rel)
        transcript = self.get_transcript(project_id)

        issues: list[ReviewIssue] = []
        warnings: list[str] = []
        checks: dict[str, bool] = {}
        artifacts = [output_rel]
        visual_summary = ""
        image_count = 0
        source = "rules"
        usage = self._empty_usage()

        actual = self.probe(project_id, output_rel)
        self._review_output_facts(ir, facts, actual, issues, checks)
        self._review_captions(ir, transcript, issues, checks)
        sheet_rel = self._review_frames(
            workspace,
            cache,
            source_hash,
            output_rel,
            actual.duration.to_float(),
            keyframe_count,
            issues,
            checks,
            warnings,
            artifacts,
        )
        if self.vision.available and sheet_rel:
            visual_summary, image_count, usage, source = self._review_visual(
                workspace,
                sheet_rel,
                goal,
                cancel_token,
                issues,
                warnings,
                source,
            )
        return self._render_review_payload(
            revision_no=revision_no,
            output_rel=output_rel,
            issues=issues,
            checks=checks,
            visual_summary=visual_summary,
            source=source,
            image_count=image_count,
            artifacts=artifacts,
            warnings=warnings,
            usage=usage,
        )

    def _review_output_facts(
        self,
        ir: EditIR,
        facts: MediaFacts,
        actual: MediaFacts,
        issues: list[ReviewIssue],
        checks: dict[str, bool],
    ) -> None:
        diff = abs(actual.duration.to_float() - ir.total_duration().to_float())
        checks["duration"] = diff <= 0.5
        if not checks["duration"]:
            issues.append(
                ReviewIssue(
                    severity="warning",
                    message=f"成片时长与 IR 相差 {diff:.3f}s",
                    suggestion="检查 trim/compile 时长映射",
                )
            )
        checks["audio"] = actual.has_audio == facts.has_audio
        if not checks["audio"]:
            issues.append(
                ReviewIssue(
                    severity="error",
                    message="成片音轨存在性与源素材不一致",
                )
            )
        checks["resolution"] = actual.width > 0 and actual.height > 0
        fit_vertical = any(
            effect.type == "fit" and effect.aspect == "9:16"
            for effect in ir.effects
        )
        if fit_vertical:
            checks["aspect"] = actual.height > actual.width
            if not checks["aspect"]:
                issues.append(
                    ReviewIssue(
                        severity="warning",
                        message="请求 9:16 竖版但成片不是竖屏比例",
                    )
                )

    def _review_captions(
        self,
        ir: EditIR,
        transcript: Transcript | None,
        issues: list[ReviewIssue],
        checks: dict[str, bool],
    ) -> None:
        cues = build_cues(ir, transcript)
        if ir.export.burn_captions:
            checks["subtitles"] = bool(cues)
            if not cues:
                issues.append(
                    ReviewIssue(
                        severity="warning",
                        message="请求烧录字幕但没有可用字幕 cue",
                        suggestion="检查转写或手动字幕",
                    )
                )
        previous_end = None
        for cue in cues:
            duration = cue.end.to_float() - cue.start.to_float()
            if duration > 8.0:
                issues.append(
                    ReviewIssue(
                        severity="info",
                        message=f"字幕停留 {duration:.1f}s，可能过长",
                    )
                )
            if previous_end is not None and cue.start.to_float() < previous_end - 0.01:
                issues.append(
                    ReviewIssue(
                        severity="warning",
                        message="字幕 cue 时间重叠",
                    )
                )
            previous_end = max(previous_end or 0.0, cue.end.to_float())

    def _review_frames(
        self,
        workspace: Any,
        cache: ArtifactCache,
        source_hash: str,
        output_rel: str,
        actual_duration: float,
        keyframe_count: int,
        issues: list[ReviewIssue],
        checks: dict[str, bool],
        warnings: list[str],
        artifacts: list[str],
    ) -> str | None:
        fps = 1.0
        frames = []
        sheet_rel = None
        try:
            duration = max(0.1, actual_duration)
            count = max(1, min(int(keyframe_count), 12))
            fps = min(30.0, max(0.1, count / duration))
            key = cache.key(
                source_hash,
                "render_review_frames_v1",
                output_rel,
                count,
            )
            frames_dir_rel = f"cache/review_frames/{key}"
            self._executor(workspace).execute(
                ToolCall(
                    seq=0,
                    tool="extract_frames",
                    args={
                        "src": output_rel,
                        "dst_dir": frames_dir_rel,
                        "fps": fps,
                        "width": 640,
                    },
                )
            )
            frames = frames_mod.list_frames(
                workspace.resolve(frames_dir_rel),
                workspace.root,
                fps,
            )
            if frames:
                sheet_path = cache.cache_path("review_sheet", key, ".jpg")
                with self.resources.cpu_slot():
                    sheet_rel = contactsheet_mod.build_contact_sheet(
                        frames,
                        workspace.root,
                        sheet_path,
                        columns=3,
                    )
                artifacts.append(sheet_rel)
                report = quality_mod.analyze_quality(
                    frames,
                    workspace.root,
                    output_rel,
                    sampled_fps=fps,
                    max_workers=min(self.settings.cpu_concurrency, len(frames)),
                    slot=self.resources.cpu_slot,
                )
                black = sum(
                    1 for mark in report.marks if mark.kind.value == "black"
                )
                blurry = sum(
                    1 for mark in report.marks if mark.kind.value == "blurry"
                )
                checks["picture"] = black == 0 and blurry == 0
                if black:
                    issues.append(
                        ReviewIssue(
                            severity="warning",
                            message=f"成片抽帧包含 {black} 个黑帧",
                        )
                    )
                if blurry:
                    issues.append(
                        ReviewIssue(
                            severity="info",
                            message=f"成片抽帧包含 {blurry} 个模糊帧",
                        )
                    )
        except Exception as exc:
            warnings.append(f"成片抽帧/画质检查失败: {type(exc).__name__}: {exc}")
        return sheet_rel

    def _review_visual(
        self,
        workspace: Any,
        sheet_rel: str,
        goal: str,
        cancel_token: Any | None,
        issues: list[ReviewIssue],
        warnings: list[str],
        source: str,
    ) -> tuple[str, int, dict[str, int], str]:
        visual_summary = ""
        image_count = 0
        before = self._usage_snapshot(self.vision)
        try:
            images = select_images(
                workspace,
                {"contact_sheet": sheet_rel},
                [],
                self.settings,
            )
            if images:
                notes = self._vision_analyze(
                    build_image_note_messages(
                        "审片：检查画面内容、遮挡与可看性问题。"
                        + (f"目标：{goal}" if goal else ""),
                        [],
                        images,
                    ),
                    ImageVisualNotes,
                    cancel_token=cancel_token,
                )
                image_count = len(images)
                visual_summary = "；".join(
                    item.summary for item in notes.images if item.summary
                )
                for note in notes.images:
                    for caution in note.cautions[:3]:
                        issues.append(
                            ReviewIssue(
                                severity="warning",
                                message=str(caution)[:500],
                                suggestion="人工复核该画面",
                            )
                        )
                source = "hybrid"
        except RunCancelledError:
            raise
        except Exception as exc:
            warnings.append(f"VLM 审片失败，已降级规则审片: {type(exc).__name__}: {exc}")
        usage = self._usage_delta(before, self._usage_snapshot(self.vision))
        return visual_summary, image_count, usage, source

    def _render_review_payload(
        self,
        *,
        revision_no: int,
        output_rel: str,
        issues: list[ReviewIssue],
        checks: dict[str, bool],
        visual_summary: str,
        source: str,
        image_count: int,
        artifacts: list[str],
        warnings: list[str],
        usage: dict[str, int],
    ) -> dict:
        errors = sum(1 for item in issues if item.severity == "error")
        warn_count = sum(1 for item in issues if item.severity == "warning")
        score = max(0.2, 0.9 - 0.12 * warn_count - 0.3 * errors)
        summary = (
            "成片审片通过"
            if not issues
            else f"发现 {len(issues)} 个问题，建议人工复核"
        )
        payload = RenderReview(
            revision=revision_no,
            output_path=output_rel,
            score=score,
            summary=summary,
            issues=issues,
            checks=checks,
            visual_summary=visual_summary,
            source=source,
            image_count=image_count,
            artifacts=artifacts,
            warnings=warnings,
        ).model_dump(mode="json")
        payload["usage"] = usage
        return payload

    @staticmethod
    def _deterministic_review(
        ir: EditIR,
        transcript: Transcript | None,
        perception: dict | None,
    ) -> dict:
        issues: list[ReviewIssue] = []
        duration = ir.total_duration().to_float()
        if duration < 1.0:
            issues.append(
                ReviewIssue(
                    severity="warning",
                    message=f"成片过短（{duration:.2f}s），可能不符合目标",
                )
            )
        for index, op in enumerate(ir.sequence):
            if op.range.duration().to_float() < 0.5:
                issues.append(
                    ReviewIssue(
                        severity="info",
                        message="片段短于 0.5s，可能产生闪烁",
                        segment_index=index,
                        suggestion="考虑与相邻片段合并或删除",
                    )
                )
            if op.confidence is not None and op.confidence < 0.5:
                issues.append(
                    ReviewIssue(
                        severity="warning",
                        message="片段置信度较低",
                        segment_index=index,
                        suggestion="人工复核该片段",
                    )
                )
        quality = (perception or {}).get("quality") or {}
        marks = quality.get("marks") or []
        overlapping = 0
        for mark in marks:
            t = mark.get("t") or {}
            try:
                seconds = float(t.get("value", 0)) / float(t.get("rate", 1))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if any(
                op.range.start.to_float() <= seconds < op.range.end.to_float()
                for op in ir.sequence
            ):
                overlapping += 1
        if overlapping:
            issues.append(
                ReviewIssue(
                    severity="warning",
                    message=f"保留区间内仍有 {overlapping} 个质量标记",
                    suggestion="检查黑帧/模糊/重复帧是否影响可看性",
                )
            )
        if ir.export.burn_captions and (transcript is None or not transcript.segments):
            issues.append(
                ReviewIssue(
                    severity="warning",
                    message="请求烧录字幕但没有可用转写",
                )
            )
        if ir.audio.remove_filler:
            issues.append(
                ReviewIssue(
                    severity="info",
                    message="remove_filler 尚未被 AudioAgent 处理",
                )
            )
        warnings_count = sum(1 for item in issues if item.severity == "warning")
        errors_count = sum(1 for item in issues if item.severity == "error")
        score = max(0.2, 0.9 - 0.1 * warnings_count - 0.3 * errors_count)
        review = EditReview(
            score=score,
            summary=(
                "规则审片通过"
                if not issues
                else f"发现 {len(issues)} 个问题，建议人工复核"
            ),
            issues=issues,
        )
        return review.model_dump(mode="json")

    def caption_preview(self, project_id: str, revision_no: int) -> dict:
        ir = self.get_revision(project_id, revision_no)
        transcript = self.get_transcript(project_id)
        cues = build_cues(ir, transcript)
        return {
            "revision": revision_no,
            "available": bool(cues),
            "captions": [
                {
                    "start": cue.start.to_float(),
                    "end": cue.end.to_float(),
                    "text": cue.text,
                }
                for cue in cues
            ],
            "srt": format_srt(cues) if cues else "",
            "vtt": format_vtt(cues) if cues else "",
        }

    def _transcode_audio_fallback(
        self,
        profile: TranscodeProfile,
        source_facts: MediaFacts,
        warnings: list[str],
    ) -> TranscodeProfile:
        if not source_facts.has_audio:
            return profile
        tracks = list(profile.audio_tracks)
        fallback = (
            default_audio_codec(profile.container)
            if profile.media_type == "audio"
            else "aac"
        )
        if not tracks:
            codec = "copy"
            source_codec = (source_facts.audio.codec_name or "").lower()
            allowed = self._CONTAINER_AUDIO.get(profile.container)
            if allowed is not None and source_codec not in allowed:
                codec = fallback
                warnings.append(
                    f"{profile.container} 不支持源音频 {source_codec or 'unknown'}，已转 {fallback}"
                )
            tracks = [AudioOutput(codec=codec)]
        else:
            allowed = self._CONTAINER_AUDIO.get(profile.container)
            source_codec = (source_facts.audio.codec_name or "").lower()
            for index, track in enumerate(tracks):
                if track.codec != "copy" or track.src is not None:
                    continue
                if allowed is not None and source_codec not in allowed:
                    tracks[index] = track.model_copy(update={"codec": fallback})
                    warnings.append(
                        f"音轨 {index} 源编码 {source_codec or 'unknown'} 不兼容 "
                        f"{profile.container}，已转 {fallback}"
                    )
        return profile.model_copy(update={"audio_tracks": tracks})

    def transcode(
        self,
        project_id: str,
        request: TranscodeRequest,
        reporter: JobReporter | None = None,
    ) -> dict:
        """按输出 profile 转码/封装工作区内的媒体；与 EditIR 解耦。"""
        self._require_project(project_id)
        # 显式 src 允许处理项目音频资产/工作区文件；仅默认源才依赖主素材。
        asset = self._require_asset(project_id) if request.src is None else None
        workspace = self.workspaces.get(project_id)
        cache = self._cache_for(project_id)
        cancel_token = getattr(reporter, "cancel_token", None)

        src_rel = request.src or asset.src
        src_path = workspace.resolve_input(src_rel)
        src_rel = src_path.relative_to(workspace.root).as_posix()
        executor = self._executor(workspace, cancel_token)
        source_facts = executor.probe(src_rel)
        source_hash = cache.source_hash(src_path)

        warnings: list[str] = []
        if request.profile.media_type == "video" and source_facts.video is None:
            raise ValueError(
                "源文件没有视频轨；请选择音频容器并使用 media_type='audio'"
            )
        if request.profile.media_type == "audio" and not source_facts.has_audio:
            raise ValueError("源文件没有音频轨，无法生成音频输出")
        profile = self._transcode_audio_fallback(profile=request.profile, source_facts=source_facts, warnings=warnings)
        if (
            source_facts.video is not None
            and "10" in (source_facts.video.pix_fmt or "")
            and not profile.keep_10bit
        ):
            warnings.append(
                f"源素材为 {source_facts.video.pix_fmt}，当前 profile 将降级为 "
                f"{profile.effective_pix_fmt() or '编码器默认'}"
            )
        if (
            source_facts.color_transfer
            and source_facts.color_transfer.lower() not in {"bt709", "sdr"}
            and not profile.hdr_tonemap
            and profile.video_codec != "copy"
        ):
            warnings.append(
                f"源素材色彩传输为 {source_facts.color_transfer}，未启用 HDR tonemap，"
                "输出可能出现偏色"
            )
        if (
            not profile.autorotate
            and source_facts.rotation not in (0, 180)
        ):
            # 重编码时 ffmpeg 默认 autorotate；copy 模式仅保留元数据。
            warnings.append("已禁用 autorotate，输出可能保持旋转元数据")

        key_parts: list[Any] = [source_hash, "transcode_v1"]
        key_parts.append(profile.model_dump(mode="json"))
        for track in profile.audio_tracks:
            if track.src:
                path = workspace.resolve_input(track.src)
                key_parts.append(("audio", cache.source_hash(path)))
        for track in profile.subtitle_tracks:
            if track.src:
                path = workspace.resolve_input(track.src)
                key_parts.append(("subtitle", cache.source_hash(path)))
        key = cache.key(*key_parts)

        if request.dst:
            dst_rel = request.dst
        else:
            dst_rel = (
                f"exports/{src_path.stem}_{key[:10]}.{profile.container}"
            )
        dst_path = workspace.resolve_output(dst_rel, reserved_inputs={src_path})
        cached = cache.get(project_id, "transcode", key, source_hash)
        if cached is not None:
            try:
                shutil.copy2(cache.resolve(cached["path"]), dst_path)
                return {
                    **cached["meta"],
                    "output_path": dst_rel,
                    "output_abs": str(dst_path),
                    "cached": True,
                    "warnings": warnings,
                }
            except OSError as exc:
                self.logger.warning(
                    "transcode_cache_copy_failed",
                    project_id=project_id,
                    key=key,
                    error=str(exc),
                )

        measured: dict[str, str] | None = None
        if (
            profile.loudness_target is not None
            and profile.loudnorm_two_pass
            and source_facts.has_audio
        ):
            try:
                measure_timeout = compute_tool_timeout(
                    "loudnorm_measure",
                    duration_seconds=source_facts.duration.to_float(),
                    width=source_facts.width,
                    height=source_facts.height,
                    settings=self.settings,
                )
                measured_result = self._execute_tool(
                    executor,
                    ToolCall(
                        seq=0,
                        tool="loudnorm_measure",
                        args={
                            "src": src_rel,
                            "target_lufs": profile.loudness_target,
                            "true_peak": profile.true_peak,
                            "lra": profile.lra,
                        },
                    ),
                    timeout_seconds=measure_timeout,
                )
                measured = parse_loudnorm_measure(
                    measured_result.stderr or measured_result.stdout
                )
            except Exception as exc:
                if isinstance(exc, RunCancelledError):
                    raise
                warnings.append(
                    f"loudnorm 第一遍测量失败，回退单遍: {type(exc).__name__}: {exc}"
                )

        args: dict[str, Any] = {
            "src": src_rel,
            "dst": dst_rel,
            "profile": profile.model_dump(mode="json"),
        }
        if measured:
            args["loudnorm_measured"] = measured
        if reporter is not None:
            reporter.progress(10, "转码中")
        timeout = compute_tool_timeout(
            "transcode",
            duration_seconds=source_facts.duration.to_float(),
            width=source_facts.width,
            height=source_facts.height,
            settings=self.settings,
        )
        result = self._execute_tool(
            executor,
            ToolCall(seq=0, tool="transcode", args=args),
            timeout_seconds=timeout,
        )
        if reporter is not None:
            reporter.record_tool(0, result)
            reporter.progress(80, "校验转码产物")

        actual = executor.probe(dst_rel)
        verify_args: dict[str, Any] = {
            "src": dst_rel,
            "expected_duration": source_facts.duration.model_dump(mode="json"),
            "expect_audio": bool(profile.audio_tracks) or source_facts.has_audio,
            "expected_fps": (
                None
                if profile.media_type == "audio"
                else (profile.fps or source_facts.fps)
            ),
            "check_av_sync": profile.media_type == "video",
            "max_av_sync_seconds": 0.5,
            "tolerance_frames": 2,
        }
        if source_facts.video is not None and profile.media_type == "video":
            verify_args["expected_width"] = (
                profile.width or source_facts.video.width
            )
            verify_args["expected_height"] = (
                profile.height or source_facts.video.height
            )
            verify_args["expected_video_codec"] = (
                source_facts.video.codec_name
                if profile.video_codec == "copy"
                else self._VIDEO_CODEC_NAMES.get(profile.video_codec)
            )
            verify_args["expected_pix_fmt"] = (
                profile.effective_pix_fmt() or source_facts.video.pix_fmt
            )
        if source_facts.audio is not None:
            first_audio = profile.audio_tracks[0] if profile.audio_tracks else None
            if first_audio is None or first_audio.src is None:
                if first_audio is not None and first_audio.codec == "copy":
                    expected_audio = source_facts.audio.codec_name
                elif first_audio is not None:
                    expected_audio = self._AUDIO_CODEC_NAMES.get(
                        first_audio.codec
                    )
                else:
                    expected_audio = default_audio_codec(profile.container)
                verify_args["expected_audio_codec"] = expected_audio
        verify_result = self._execute_tool(
            executor,
            ToolCall(seq=1, tool="verify", args=verify_args),
        )
        if reporter is not None:
            reporter.record_tool(1, verify_result)

        if profile.media_type == "audio":
            first_audio = profile.audio_tracks[0] if profile.audio_tracks else None
            actual_encoder = (
                first_audio.codec
                if first_audio is not None
                else default_audio_codec(profile.container)
            )
        else:
            actual_encoder = (
                source_facts.video.codec_name
                if profile.video_codec == "copy"
                and source_facts.video is not None
                else profile.video_codec
            )
        payload = {
            "output_path": dst_rel,
            "output_abs": str(dst_path),
            "media_type": profile.media_type,
            "container": profile.container,
            "video_codec": (
                None if profile.media_type == "audio" else profile.video_codec
            ),
            "actual_video_codec": actual.video.codec_name if actual.video else None,
            "audio_codec": verify_args.get("expected_audio_codec"),
            "width": actual.width,
            "height": actual.height,
            "fps": actual.fps,
            "duration_seconds": actual.duration.to_ffmpeg_seconds(),
            "encoder": actual_encoder,
            "loudnorm_two_pass": bool(measured),
            "warnings": warnings,
        }
        with self._cache_write_lock:
            cache_path = cache.cache_path("transcode", key, f".{profile.container}")
            shutil.copy2(dst_path, cache_path)
            cache.put(
                project_id,
                "transcode",
                key,
                cache_path,
                source_hash=source_hash,
                meta=payload,
            )
        if reporter is not None:
            reporter.progress(100, "转码完成")
        return {**payload, "cached": False}

    def _compile(
        self,
        project_id: str,
        revision_no: int,
        *,
        force_software: bool = False,
    ):
        ir = self.get_revision(project_id, revision_no)
        transcript = self.get_transcript(project_id)
        perception = self.get_perception(project_id)
        workspace = self.workspaces.get(project_id)
        encoder = self._encoder_profile(workspace, force_software=force_software)
        return ir, lower(
            ir,
            transcript=transcript,
            perception=perception,
            encoder=encoder,
            caption_style={
                "font_name": self.settings.caption_font_name,
                "font_size": self.settings.caption_font_size,
                "color": self.settings.caption_color,
                "position": self.settings.caption_position,
            },
            audio_assets=self._audio_asset_refs(project_id),
            overlay_assets=self._overlay_asset_refs(project_id),
            hdr_tonemap=self.settings.render_hdr_tonemap,
            keep_10bit=False,
            fonts_dir=self.settings.caption_fonts_dir,
        )

    def _encoder_profile(
        self,
        workspace: Workspace,
        *,
        force_software: bool = False,
    ) -> EncoderProfile:
        if force_software:
            return profile_from_policy(self.policy, "libx264")
        with self._encoder_lock:
            exclude = set(self._failed_encoders)
        executor = self._executor(workspace)
        profile = self.encoder_selector.resolve(executor.process, exclude=exclude)
        if profile.codec in exclude:
            return profile_from_policy(self.policy, "libx264")
        return profile

    @staticmethod
    def _execute_tool(
        executor: ToolCallExecutor,
        call: ToolCall,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """执行工具；兼容未声明 timeout_seconds 的测试/自定义 executor。"""
        if timeout_seconds is not None and accepts_keyword(
            executor.execute, "timeout_seconds"
        ):
            return executor.execute(call, timeout_seconds=timeout_seconds)
        return executor.execute(call)

    @staticmethod
    def _without_encoder(call: ToolCall) -> ToolCall:
        args = {key: value for key, value in call.args.items() if key != "encoder"}
        return ToolCall(
            seq=call.seq,
            tool=call.tool,
            args=args,
            description=call.description,
        )

    def _execute_with_fallback(
        self,
        call: ToolCall,
        executor: ToolCallExecutor,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[ToolCall, ToolResult, str | None]:
        """硬件编码失败时禁用该 codec，去 encoder 后重试当前调用。"""
        try:
            return (
                call,
                self._execute_tool(
                    executor,
                    call,
                    timeout_seconds=timeout_seconds,
                ),
                None,
            )
        except ToolExecutionError as exc:
            raw = call.args.get("encoder")
            if not isinstance(raw, dict):
                raise
            codec = str(raw.get("codec") or "")
            if not codec or codec == "libx264":
                raise
            with self._encoder_lock:
                self._failed_encoders.add(codec)
            fallback = self._without_encoder(call)
            warning = f"{codec} 编码失败，{call.tool} 已回退 libx264"
            self.logger.warning(
                "encoder_fallback",
                tool=call.tool,
                codec=codec,
                error=str(exc)[:500],
            )
            fallback_result = self._execute_tool(
                executor,
                fallback,
                timeout_seconds=timeout_seconds,
            )
            fallback_result = fallback_result.model_copy(
                update={"encoder": "libx264"}
            )
            return fallback, fallback_result, warning

    def _decode_hwaccel(self, workspace: Workspace, src: str) -> str | None:
        if not self.decode_selector.enabled:
            return None
        executor = self._executor(workspace)
        if self.decode_selector.available(executor.process, src):
            return self.decode_selector.backend
        return None

    def dry_run(self, project_id: str, revision_no: int) -> dict:
        _ir, plan = self._compile(project_id, revision_no)
        return plan.dump()

    def render(self, project_id: str, revision_no: int, reporter: JobReporter | None = None) -> dict:
        with self._render_lock(project_id, revision_no):
            return self._render_impl(
                project_id,
                revision_no,
                reporter,
                force_software=False,
            )

    def _render_impl(
        self,
        project_id: str,
        revision_no: int,
        reporter: JobReporter | None,
        *,
        force_software: bool,
    ) -> dict:
        ir, plan = self._compile(
            project_id,
            revision_no,
            force_software=force_software,
        )
        facts = ir.media_facts
        workspace = self.workspaces.get(project_id)
        self._write_render_captions(workspace, plan, ir.revision)

        asset = self._require_asset(project_id)
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        cancel_token = getattr(reporter, "cancel_token", None)
        executor = self._executor(workspace, cancel_token)
        calls = sorted(plan.tool_calls, key=lambda call: call.seq)
        total = max(len(calls), 1)
        results_by_seq: dict[int, ToolResult] = {}
        warnings = list(plan.warnings)

        def progress(call: ToolCall) -> None:
            if reporter is not None:
                reporter.progress(
                    5 + int(85 * call.seq / total),
                    f"{call.tool} 执行中",
                )

        # probe 先串行完成，后续所有工具都基于同一份事实。
        self._render_probe_calls(
            calls, executor, reporter, results_by_seq, warnings, progress
        )
        cache_hits, cache_misses = self._render_cut_calls(
            [call for call in calls if call.tool == "cut"],
            executor=executor,
            cache=cache,
            workspace=workspace,
            project_id=project_id,
            source_hash=source_hash,
            facts=facts,
            revision_no=revision_no,
            cancel_token=cancel_token,
            reporter=reporter,
            results_by_seq=results_by_seq,
            warnings=warnings,
            progress=progress,
        )

        for call in calls:
            if call.tool in ("probe", "cut"):
                continue
            progress(call)
            call_timeout = compute_tool_timeout(
                call.tool,
                duration_seconds=ir.total_duration().to_float(),
                width=facts.width,
                height=facts.height,
                settings=self.settings,
            )
            call = self._with_loudnorm_measurement(
                call, ir, facts, executor, warnings
            )
            try:
                _executed, result, warning = self._execute_with_fallback(
                    call,
                    executor,
                    timeout_seconds=call_timeout,
                )
            except (ToolExecutionError, VerificationError) as exc:
                hardware_used = any(
                    item.encoder and item.encoder != "libx264"
                    for item in results_by_seq.values()
                )
                if not force_software and hardware_used:
                    warnings.append(
                        f"硬件编码产物校验失败，已回退软件重渲: {type(exc).__name__}"
                    )
                    self.logger.warning(
                        "render_software_retry",
                        project_id=project_id,
                        revision=revision_no,
                        error=str(exc)[:500],
                    )
                    return self._render_impl(
                        project_id,
                        revision_no,
                        reporter,
                        force_software=True,
                    )
                raise
            if warning:
                warnings.append(warning)
            results_by_seq[call.seq] = result
            if reporter is not None:
                reporter.record_tool(call.seq, result)

        results = [results_by_seq[seq] for seq in sorted(results_by_seq)]
        if reporter is not None:
            reporter.progress(95, "归档渲染结果")
        return self._render_result(
            plan,
            results,
            warnings,
            cache_hits,
            cache_misses,
            workspace,
            actual_encoder=self._final_actual_encoder(
                plan,
                calls,
                results_by_seq,
            ),
        )

    @staticmethod
    def _final_actual_encoder(
        plan: Any,
        calls: list[ToolCall],
        results_by_seq: dict[int, ToolResult],
    ) -> str | None:
        """以最终输出步骤的实际编码器为准，避免被前面成功的硬件 cut 掩盖。"""
        for call in sorted(calls, key=lambda item: item.seq, reverse=True):
            dst = call.args.get("dst")
            if not isinstance(dst, str) or dst != plan.output_path:
                continue
            result = results_by_seq.get(call.seq)
            if result is not None and result.encoder:
                return str(result.encoder)
            # 最终步骤没有 encoder 参数时是 copy 透传，不是 libx264；
            # 硬件回退路径会显式写入 encoder=libx264。
            return "copy"
        return None

    def _write_render_captions(
        self,
        workspace: Any,
        plan: Any,
        revision: int,
    ) -> None:
        if not plan.captions:
            return
        render_dir = workspace.resolve(f"renders/r{revision}")
        render_dir.mkdir(parents=True, exist_ok=True)
        assert plan.caption_srt_path is not None
        assert plan.caption_vtt_path is not None
        atomic_write_text(
            workspace.resolve(plan.caption_srt_path),
            format_srt(plan.captions),
        )
        atomic_write_text(
            workspace.resolve(plan.caption_vtt_path),
            format_vtt(plan.captions),
        )

    def _render_probe_calls(
        self,
        calls: list[ToolCall],
        executor: Any,
        reporter: JobReporter | None,
        results_by_seq: dict[int, ToolResult],
        warnings: list[str],
        progress,
    ) -> None:
        for call in calls:
            if call.tool != "probe":
                continue
            progress(call)
            _executed, result, warning = self._execute_with_fallback(call, executor)
            if warning:
                warnings.append(warning)
            results_by_seq[call.seq] = result
            if reporter is not None:
                reporter.record_tool(call.seq, result)

    def _render_cut_calls(
        self,
        cut_calls: list[ToolCall],
        *,
        executor: Any,
        cache: ArtifactCache,
        workspace: Any,
        project_id: str,
        source_hash: str,
        facts: MediaFacts,
        revision_no: int,
        cancel_token: Any,
        reporter: JobReporter | None,
        results_by_seq: dict[int, ToolResult],
        warnings: list[str],
        progress,
    ) -> tuple[int, int]:
        cache_hits = 0
        cache_misses = 0
        if not cut_calls:
            return cache_hits, cache_misses
        # 先在主线程顺序创建输出目录并完成沙箱校验，避免并行线程
        # 同时解析/创建同一目录时触发 Windows 路径解析竞态。
        for call in cut_calls:
            workspace.resolve_output(str(call.args.get("dst") or ""))
        workers = max(
            1,
            min(self.settings.ffmpeg_concurrency, len(cut_calls)),
        )
        cut_jobs: list[tuple[ToolCall, float]] = []
        for call in cut_calls:
            cut_args = CutArgs.model_validate(call.args)
            cut_jobs.append(
                (
                    call,
                    compute_tool_timeout(
                        "cut",
                        duration_seconds=(cut_args.end - cut_args.start).to_float(),
                        width=facts.width,
                        height=facts.height,
                        settings=self.settings,
                    ),
                )
            )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"cut-r{revision_no}",
        ) as pool:
            futures = [
                pool.submit(
                    self._cut_with_cache,
                    executor,
                    cache,
                    workspace,
                    project_id,
                    source_hash,
                    call,
                    timeout_seconds=cut_timeout,
                )
                for call, cut_timeout in cut_jobs
            ]
            for future in as_completed(futures):
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                call, result, warning, hit = future.result()
                if warning:
                    warnings.append(warning)
                results_by_seq[call.seq] = result
                if hit:
                    cache_hits += 1
                else:
                    cache_misses += 1
                progress(call)
                if reporter is not None:
                    reporter.record_tool(call.seq, result)
        return cache_hits, cache_misses

    def _with_loudnorm_measurement(
        self,
        call: ToolCall,
        ir: EditIR,
        facts: MediaFacts,
        executor: Any,
        warnings: list[str],
    ) -> ToolCall:
        if not (
            self.settings.render_loudnorm_two_pass
            and call.tool == "postprocess"
            and call.args.get("target_lufs") is not None
        ):
            return call
        try:
            measured_result = self._execute_tool(
                executor,
                ToolCall(
                    seq=call.seq,
                    tool="loudnorm_measure",
                    args={
                        "src": str(call.args.get("src") or ""),
                        "target_lufs": float(call.args["target_lufs"]),
                        "true_peak": float(call.args.get("true_peak", -1.5)),
                        "lra": float(call.args.get("lra", 11.0)),
                    },
                ),
                timeout_seconds=compute_tool_timeout(
                    "loudnorm_measure",
                    duration_seconds=ir.total_duration().to_float(),
                    width=facts.width,
                    height=facts.height,
                    settings=self.settings,
                ),
            )
            measured = parse_loudnorm_measure(
                measured_result.stderr or measured_result.stdout
            )
            if measured:
                warnings.append("loudnorm 已使用两遍测量")
                return ToolCall(
                    seq=call.seq,
                    tool=call.tool,
                    args={**call.args, "loudnorm_measured": measured},
                    description=call.description,
                )
        except RunCancelledError:
            raise
        except Exception as exc:
            warnings.append(
                f"loudnorm 第一遍测量失败，回退单遍: "
                f"{type(exc).__name__}: {exc}"
            )
        return call

    def _render_result(
        self,
        plan: Any,
        results: list[ToolResult],
        warnings: list[str],
        cache_hits: int,
        cache_misses: int,
        workspace: Any,
        *,
        actual_encoder: str | None = None,
    ) -> dict:
        final_facts = None
        if results and results[-1].tool == "verify":
            final_facts = MediaFacts.model_validate_json(results[-1].stdout)
        encoders = sorted({item.encoder for item in results if item.encoder})
        if actual_encoder is None:
            actual_encoder = (
                encoders[0]
                if len(encoders) == 1
                else ("mixed" if encoders else "libx264")
            )
        output_abs = workspace.resolve(plan.output_path)
        artifacts = [plan.output_path]
        if plan.caption_srt_path:
            artifacts.append(plan.caption_srt_path)
        if plan.caption_vtt_path:
            artifacts.append(plan.caption_vtt_path)
        return {
            "output_path": plan.output_path,
            "output_abs": str(output_abs),
            "tool_calls": [call.model_dump(mode="json") for call in plan.tool_calls],
            "results": [result.model_dump(mode="json") for result in results],
            "facts": final_facts.model_dump(mode="json") if final_facts is not None else None,
            "caption_srt_path": plan.caption_srt_path,
            "caption_vtt_path": plan.caption_vtt_path,
            "captions": len(plan.captions),
            "warnings": warnings,
            "artifacts": artifacts,
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "actual_encoder": actual_encoder,
            "encoders_used": encoders,
        }

    @staticmethod
    def _cut_cache_key(cache: ArtifactCache, source_hash: str, call: ToolCall) -> str:
        args = CutArgs.model_validate(call.args)
        payload = args.model_dump(mode="json")
        payload.pop("dst", None)
        # 无滤镜/无变速保持旧键；逐片段特效或变速使用新版本。
        version = (
            "segment_v2"
            if (args.video_filters or args.speed != 1.0)
            else "segment_v1"
        )
        return cache.key(source_hash, version, payload)

    def _cut_with_cache(
        self,
        executor: ToolCallExecutor,
        cache: ArtifactCache,
        workspace: Workspace,
        project_id: str,
        source_hash: str,
        call: ToolCall,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[ToolCall, ToolResult, str | None, bool]:
        """执行或复用单个 cut；缓存键只由源与裁剪参数决定，不含输出路径。"""
        args = CutArgs.model_validate(call.args)
        key = self._cut_cache_key(cache, source_hash, call)
        record = cache.get(project_id, "segments", key, source_hash)
        dst_path = workspace.resolve_output(args.dst)

        if record is not None:
            try:
                shutil.copy2(cache.resolve(record["path"]), dst_path)
                codec = args.encoder.codec if args.encoder is not None else "libx264"
                return (
                    call,
                    ToolResult(
                        tool="cut",
                        argv=[],
                        exit_code=0,
                        duration_ms=0,
                        artifacts=[args.dst],
                        cached=True,
                        encoder=codec,
                    ),
                    None,
                    True,
                )
            except OSError as exc:
                self.logger.warning(
                    "segment_cache_copy_failed",
                    project_id=project_id,
                    key=key,
                    error=str(exc),
                )

        executed, result, warning = self._execute_with_fallback(
            call,
            executor,
            timeout_seconds=timeout_seconds,
        )
        result.cached = False
        actual_key = self._cut_cache_key(cache, source_hash, executed)
        executed_args = CutArgs.model_validate(executed.args)
        try:
            # 并行 cut 在各自线程执行；缓存索引写入串行化，避免 SQLite 并发写。
            with self._cache_write_lock:
                cache_path = cache.cache_path("segments", actual_key, ".mp4")
                shutil.copy2(dst_path, cache_path)
                cache.put(
                    project_id,
                    "segments",
                    actual_key,
                    cache_path,
                    source_hash=source_hash,
                    meta={
                        "start": executed_args.start.to_ffmpeg_seconds(),
                        "end": executed_args.end.to_ffmpeg_seconds(),
                        "encoder": result.encoder,
                    },
                )
        except OSError as exc:
            self.logger.warning(
                "segment_cache_store_failed",
                project_id=project_id,
                key=actual_key,
                error=str(exc),
            )
        return executed, result, warning, False
