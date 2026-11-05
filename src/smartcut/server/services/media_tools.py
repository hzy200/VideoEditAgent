"""媒体修复/转换服务：诊断、简单修复、音频-only 转换与自然语言意图路由。"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from smartcut.contracts.media import MediaFacts
from smartcut.contracts.media_ops import (
    MediaDiagnosis,
    MediaGoalDraft,
    MediaIssue,
    MediaOperationPlan,
    RepairRequest,
    default_repair_profile,
)
from smartcut.contracts.quality import QualityKind, QualityReport
from smartcut.contracts.tools import ToolCall
from smartcut.contracts.transcode import (
    AUDIO_CONTAINERS,
    AudioOutput,
    TranscodeProfile,
    TranscodeRequest,
    default_audio_codec,
)
from smartcut.errors import ResourceNotFound, RunCancelledError, ToolExecutionError
from smartcut.runner.timeouts import compute_tool_timeout
from smartcut.runner.verify import VerifyArgs, verify_facts
from smartcut.server.services.media_intent import parse_media_goal

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
_VIDEO_CONTAINERS = {"mp4", "mov", "mkv"}
_AV_SYNC_START_TOLERANCE = 0.15
_AV_SYNC_DURATION_TOLERANCE = 0.5


class MediaToolsMixin:
    # ── 原始导入（允许无法 probe 的损坏文件） ───────────────────────────
    def import_raw_media(self, project_id: str, src: str) -> dict:
        """把本地文件复制进工作区，但不注册为项目素材。

        损坏/无法 probe 的文件不能走 ingest；媒体工具需要原始路径做诊断与
        修复。源文件只读，副本写入 media/ 并以随机前缀避免覆盖。
        """
        self._require_project(project_id)
        workspace = self.workspaces.get(project_id)
        source = Path(src).expanduser()
        if not source.is_file():
            raise ResourceNotFound(f"文件不存在: {src}")
        name = Path(source.name).name or "input.bin"
        workspace.media_dir.mkdir(parents=True, exist_ok=True)
        destination = workspace.media_dir / f"{uuid4().hex[:8]}_{name}"
        shutil.copy2(source.resolve(), destination)
        relative = destination.relative_to(workspace.root).as_posix()
        return {
            "src": relative,
            "name": name,
            "size": destination.stat().st_size,
        }

    # ── 诊断 ──────────────────────────────────────────────────────────
    def _resolve_media_source(
        self,
        project_id: str,
        src: str | None,
    ) -> tuple[Any, str, Path]:
        workspace = self.workspaces.get(project_id)
        if src is None:
            asset = self._require_asset(project_id)
            src_rel = asset.src
        else:
            src_rel = src
        path = workspace.resolve_input(src_rel)
        return workspace, path.relative_to(workspace.root).as_posix(), path

    def diagnose_media(
        self,
        project_id: str,
        src: str | None = None,
    ) -> MediaDiagnosis:
        """基于 ffprobe（及已有质量缓存）诊断，不重新抽帧、不写 IR。"""
        self._require_project(project_id)
        try:
            workspace, src_rel, _ = self._resolve_media_source(project_id, src)
        except Exception as exc:
            return MediaDiagnosis(
                src=str(src or ""),
                probe_ok=False,
                issues=[
                    MediaIssue(
                        code="unopenable",
                        severity="error",
                        message=f"无法解析媒体源: {type(exc).__name__}: {exc}",
                    )
                ],
                recommended="remux",
                warnings=["源路径无效或文件不存在"],
            )

        executor = self._executor(workspace)
        try:
            facts = executor.probe(src_rel)
        except Exception as exc:
            return MediaDiagnosis(
                src=src_rel,
                probe_ok=False,
                issues=[
                    MediaIssue(
                        code="unopenable",
                        severity="error",
                        message=f"ffprobe 无法打开: {type(exc).__name__}: {exc}",
                    )
                ],
                recommended="remux",
                warnings=["将尝试容错重封装；若仍失败请检查文件是否完整"],
            )

        issues: list[MediaIssue] = []
        warnings: list[str] = []
        recommended: str = "auto"
        has_video = facts.video is not None
        has_audio = facts.audio is not None
        if has_video and not has_audio:
            issues.append(
                MediaIssue(
                    code="missing_audio",
                    severity="info",
                    message="源文件没有音频轨",
                )
            )
        if has_audio and not has_video:
            issues.append(
                MediaIssue(
                    code="audio_only",
                    severity="info",
                    message="源文件只有音频轨",
                )
            )

        offset = self._start_offset(facts)
        if offset is not None and abs(offset) > _AV_SYNC_START_TOLERANCE:
            issues.append(
                MediaIssue(
                    code="av_desync",
                    severity="warning",
                    message=(
                        "音画起始时间不一致: "
                        f"offset={offset:.3f}s"
                    ),
                    detail={"offset_seconds": round(offset, 6)},
                )
            )
            recommended = "audio_sync"

        duration_delta = self._duration_delta(facts)
        if (
            duration_delta is not None
            and duration_delta > _AV_SYNC_DURATION_TOLERANCE
        ):
            issues.append(
                MediaIssue(
                    code="av_duration_mismatch",
                    severity="warning",
                    message=f"音视频时长相差 {duration_delta:.3f}s",
                    detail={"delta_seconds": round(duration_delta, 6)},
                )
            )
            if recommended == "auto":
                recommended = "audio_sync"

        if facts.vfr:
            issues.append(
                MediaIssue(
                    code="vfr_timestamps",
                    severity="warning",
                    message="源文件为可变帧率，可能造成播放卡顿",
                )
            )
            if recommended == "auto":
                recommended = "reencode"

        if src is None:
            for mark, issue in self._quality_issues(project_id):
                issues.append(issue)
                if mark == "blurry" and recommended == "auto":
                    recommended = "sharpen"
            if any(item.code == "frozen" for item in issues):
                warnings.append("检测到静止/冻结画面；结构性修复不能恢复内容")

        return MediaDiagnosis(
            src=src_rel,
            probe_ok=True,
            facts=facts,
            issues=issues,
            recommended=recommended,  # type: ignore[arg-type]
            can_repair=True,
            warnings=warnings,
        )

    @staticmethod
    def _start_offset(facts: MediaFacts) -> float | None:
        if facts.video is None or facts.audio is None:
            return None
        if facts.video.start_time is None or facts.audio.start_time is None:
            return None
        return (
            facts.audio.start_time.to_float()
            - facts.video.start_time.to_float()
        )

    @staticmethod
    def _duration_delta(facts: MediaFacts) -> float | None:
        if facts.video is None or facts.audio is None:
            return None
        if facts.video.duration is None or facts.audio.duration is None:
            return None
        return abs(
            facts.video.duration.to_float() - facts.audio.duration.to_float()
        )

    def _quality_issues(
        self, project_id: str
    ) -> list[tuple[str, MediaIssue]]:
        try:
            payload = self.get_perception(project_id)
        except Exception:
            return []
        raw = payload.get("quality")
        if not raw:
            return []
        try:
            report = QualityReport.model_validate(raw)
        except Exception:
            return []
        counts = {kind: 0 for kind in QualityKind}
        for mark in report.marks:
            counts[mark.kind] += 1
        issues: list[tuple[str, MediaIssue]] = []
        frame_count = max(1, report.frame_count)
        if counts[QualityKind.blurry]:
            ratio = counts[QualityKind.blurry] / frame_count
            issues.append(
                (
                    "blurry",
                    MediaIssue(
                        code="blurry",
                        severity="warning",
                        message=f"{counts[QualityKind.blurry]} 帧偏模糊（约 {ratio:.0%}）",
                        detail={"ratio": round(ratio, 4)},
                    ),
                )
            )
        if counts[QualityKind.frozen]:
            issues.append(
                (
                    "frozen",
                    MediaIssue(
                        code="frozen",
                        severity="warning",
                        message=f"{counts[QualityKind.frozen]} 帧检测为静止/冻结",
                    ),
                )
            )
        return issues

    # ── 修复 ──────────────────────────────────────────────────────────
    def repair_media(
        self,
        project_id: str,
        request: RepairRequest,
        reporter: Any | None = None,
    ) -> dict:
        self._require_project(project_id)
        workspace, src_rel, src_path = self._resolve_media_source(
            project_id, request.src
        )
        cache = self._cache_for(project_id)
        cancel_token = getattr(reporter, "cancel_token", None)
        started = time.perf_counter()
        if reporter is not None:
            reporter.progress(5, "诊断媒体")

        diagnosis_src = request.src
        try:
            asset = self._require_asset(project_id)
        except Exception:
            asset = None
        if request.src is None or (asset is not None and request.src == asset.src):
            diagnosis_src = None
        diagnosis = self.diagnose_media(project_id, diagnosis_src)
        facts = diagnosis.facts
        has_video = True if facts is None else facts.video is not None
        warnings: list[str] = list(diagnosis.warnings)
        strategy = request.strategy
        if strategy == "auto":
            strategy = (
                diagnosis.recommended
                if diagnosis.recommended != "auto"
                else "remux"
            )
        if strategy == "audio_sync" and not (
            facts is not None and facts.video is not None and facts.audio is not None
        ):
            warnings.append("源文件缺少可比较的音视频起始时间，改为重编码")
            strategy = "reencode"
        if strategy == "sharpen" and not has_video:
            warnings.append("音频文件无法锐化，改为重封装")
            strategy = "remux"

        try:
            profile = self._repair_profile(
                request, facts, strategy, src_rel
            )
        except ValueError as exc:
            raise ToolExecutionError(f"修复参数无效: {exc}") from exc
        if strategy == "sharpen" and profile.media_type == "audio":
            warnings.append("音频输出无法锐化，改为重编码")
            strategy = "reencode"
        if strategy == "audio_sync" and profile.media_type == "audio":
            warnings.append("纯音频输出没有音画同步语义，改为重编码")
            strategy = "reencode"
        offset = self._repair_offset(request, facts, strategy)
        has_video = bool(facts and facts.video) and (
            profile.media_type == "video"
        )
        source_hash = cache.source_hash(src_path)
        key = cache.key(
            source_hash,
            "media_repair_v1",
            {
                "strategy": strategy,
                "profile": profile.model_dump(mode="json"),
                "audio_offset_seconds": round(offset, 6),
                "sharpen_strength": request.sharpen_strength,
                "has_video": has_video,
            },
        )
        if request.dst:
            dst_rel = request.dst
        else:
            stem = Path(src_rel).stem
            dst_rel = (
                f"repairs/{stem}_repaired_{key[:10]}.{profile.container}"
            )
        dst_path = workspace.resolve_output(
            dst_rel, reserved_inputs={src_path}
        )
        cached = cache.get(project_id, "repair", key, source_hash)
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
                    "media_repair_cache_copy_failed",
                    project_id=project_id,
                    key=key,
                    error=str(exc),
                )

        executor = self._executor(workspace, cancel_token)
        if reporter is not None:
            reporter.progress(15, f"准备修复: {strategy}")
        attempts = [strategy]
        if strategy == "remux":
            attempts.append("reencode")
        output_facts: MediaFacts | None = None
        used_strategy = strategy
        used_profile = profile
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            attempt_profile = (
                profile
                if attempt == strategy
                else self._repair_profile(request, facts, attempt, src_rel)
            )
            args = {
                "src": src_rel,
                "dst": dst_rel,
                "strategy": attempt,
                "profile": attempt_profile.model_dump(mode="json"),
                "audio_offset_seconds": offset if attempt == "audio_sync" else 0.0,
                "pad_duration_seconds": (
                    facts.duration.to_float()
                    if attempt == "audio_sync" and facts is not None
                    else None
                ),
                "sharpen_strength": request.sharpen_strength,
                "has_video": has_video,
                "error_tolerant": True,
            }
            timeout = compute_tool_timeout(
                "media_repair",
                duration_seconds=(
                    facts.duration.to_float() if facts is not None else 0.0
                ),
                width=facts.width if facts is not None else None,
                height=facts.height if facts is not None else None,
                settings=self.settings,
            )
            if reporter is not None:
                reporter.progress(
                    20 + int(50 * index / max(1, len(attempts))),
                    f"修复中: {attempt}",
                )
            try:
                result = self._execute_tool(
                    executor,
                    ToolCall(seq=index, tool="media_repair", args=args),
                    timeout_seconds=timeout,
                )
                if reporter is not None:
                    reporter.record_tool(index, result)
                output_facts = executor.probe(dst_rel)
                self._verify_repair(
                    output_facts, facts, attempt_profile, attempt
                )
                used_strategy = attempt
                used_profile = attempt_profile
                break
            except RunCancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if dst_path.exists():
                    dst_path.unlink(missing_ok=True)
                if index == len(attempts) - 1:
                    break
                warnings.append(
                    f"{attempt} 修复失败，回退 {attempts[index + 1]}: "
                    f"{type(exc).__name__}: {exc}"
                )

        if output_facts is None:
            raise ToolExecutionError(
                "媒体修复失败: "
                f"{type(last_error).__name__ if last_error else 'unknown'}: "
                f"{last_error}"
            )

        if reporter is not None:
            reporter.progress(90, "校验修复产物")
        after = self.diagnose_media(project_id, dst_rel)
        payload = {
            "output_path": dst_rel,
            "output_abs": str(dst_path),
            "media_type": used_profile.media_type,
            "container": used_profile.container,
            "requested_strategy": request.strategy,
            "used_strategy": used_strategy,
            "issues_before": [
                item.model_dump(mode="json") for item in diagnosis.issues
            ],
            "issues_after": [
                item.model_dump(mode="json") for item in after.issues
            ],
            "diagnosis_before": diagnosis.model_dump(mode="json"),
            "diagnosis_after": after.model_dump(mode="json"),
            "duration_seconds": output_facts.duration.to_ffmpeg_seconds(),
            "width": output_facts.width,
            "height": output_facts.height,
            "fps": output_facts.fps,
            "warnings": warnings,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        with self._cache_write_lock:
            cache_path = cache.cache_path(
                "repair", key, f".{used_profile.container}"
            )
            shutil.copy2(dst_path, cache_path)
            cache.put(
                project_id,
                "repair",
                key,
                cache_path,
                source_hash=source_hash,
                meta=payload,
            )
        if reporter is not None:
            reporter.progress(100, "修复完成")
        return {**payload, "cached": False}

    @staticmethod
    def _repair_offset(
        request: RepairRequest,
        facts: MediaFacts | None,
        strategy: str,
    ) -> float:
        """返回“音频需要延迟的秒数”：正=延后，负=提前（裁头）。"""
        if strategy != "audio_sync":
            return 0.0
        if request.audio_offset_seconds is not None:
            return float(request.audio_offset_seconds)
        if facts is None:
            return 0.0
        # diagnose 的 offset = audio.start - video.start；音频晚开始需要提前。
        offset = MediaToolsMixin._start_offset(facts)
        return float(-(offset or 0.0))

    @staticmethod
    def _profile_with(
        profile: TranscodeProfile, **updates: Any
    ) -> TranscodeProfile:
        values = profile.model_dump(mode="python")
        values.update(updates)
        return TranscodeProfile(**values)

    def _repair_profile(
        self,
        request: RepairRequest,
        facts: MediaFacts | None,
        strategy: str,
        src_rel: str,
    ) -> TranscodeProfile:
        profile = request.profile.model_copy(deep=True)
        has_video = (
            (True if facts is None else facts.video is not None)
            and request.profile.media_type != "audio"
        )
        if not has_video:
            container = profile.container
            if container not in AUDIO_CONTAINERS:
                suffix = Path(src_rel).suffix.lstrip(".").lower()
                container = (
                    suffix if suffix in AUDIO_CONTAINERS else "m4a"
                )
            values: dict[str, Any] = {
                "media_type": "audio",
                "container": container,
                "width": None,
                "height": None,
                "fps": None,
                "hdr_tonemap": False,
                "keep_10bit": False,
                "subtitle_tracks": [],
            }
            profile = self._profile_with(profile, **values)
            tracks = profile.audio_tracks
            if not tracks or tracks[0].codec == "copy":
                profile = self._profile_with(
                    profile,
                    audio_tracks=[
                        AudioOutput(
                            codec=default_audio_codec(container),
                            bitrate="160k",
                        )
                    ],
                )
            return profile

        container = profile.container
        if container in AUDIO_CONTAINERS:
            container = "mp4"
        values = {"media_type": "video", "container": container}
        if strategy in {"reencode", "sharpen"} and profile.video_codec == "copy":
            values["video_codec"] = "libx264"
        if strategy in {"reencode", "sharpen", "audio_sync"}:
            tracks = profile.audio_tracks
            if not tracks or tracks[0].codec == "copy":
                values["audio_tracks"] = [
                    AudioOutput(codec="aac", bitrate="160k")
                ]
        if strategy == "reencode" and not profile.fps and facts and facts.fps:
            values["fps"] = facts.fps
        return self._profile_with(profile, **values)

    def _verify_repair(
        self,
        output_facts: MediaFacts,
        source_facts: MediaFacts | None,
        profile: TranscodeProfile,
        strategy: str,
    ) -> None:
        if source_facts is None:
            return
        verify_args = VerifyArgs(
            src=output_facts.src,
            expected_duration=source_facts.duration,
            expect_audio=source_facts.has_audio,
            expected_fps=(
                None
                if profile.media_type == "audio"
                else (profile.fps or source_facts.fps)
            ),
            check_av_sync=(
                profile.media_type == "video"
                and source_facts.video is not None
                and source_facts.audio is not None
            ),
            max_av_sync_seconds=0.5,
            tolerance_frames=3,
        )
        if (
            source_facts.video is not None
            and profile.media_type == "video"
            and strategy in {"reencode", "sharpen"}
        ):
            verify_args.expected_width = profile.width or source_facts.video.width
            verify_args.expected_height = (
                profile.height or source_facts.video.height
            )
            verify_args.expected_video_codec = (
                source_facts.video.codec_name
                if profile.video_codec == "copy"
                else _VIDEO_CODEC_NAMES.get(profile.video_codec)
            )
            verify_args.expected_pix_fmt = (
                profile.effective_pix_fmt() or source_facts.video.pix_fmt
            )
        if source_facts.audio is not None and strategy in {
            "reencode",
            "audio_sync",
            "sharpen",
        }:
            track = profile.audio_tracks[0] if profile.audio_tracks else None
            if track is not None and track.codec != "copy":
                verify_args.expected_audio_codec = _AUDIO_CODEC_NAMES.get(
                    track.codec
                )
        verify_facts(output_facts, verify_args)

    # ── 自然语言意图 ──────────────────────────────────────────────────
    def interpret_media_goal(
        self,
        project_id: str,
        goal: str,
        *,
        src: str | None = None,
        use_llm: bool = True,
        cancel_token: Any | None = None,
    ) -> MediaOperationPlan | None:
        draft = parse_media_goal(goal)
        facts = self._source_media_facts(project_id, src)
        if (
            use_llm
            and self.planner_llm_available
            and (draft is None or draft.confidence < 0.6)
        ):
            refined = self._llm_media_draft(
                goal,
                project_id,
                facts,
                cancel_token=cancel_token,
            )
            if refined is not None and refined.operation is not None:
                if draft is None or refined.confidence >= draft.confidence:
                    draft = refined
        if draft is None or draft.operation is None:
            return None
        src_rel = src or self._project_source_rel(project_id)
        plan = self._plan_from_draft(draft, facts, src_rel)
        return plan

    def _source_media_facts(
        self, project_id: str, src: str | None
    ) -> MediaFacts | None:
        if src is None:
            return self._project_media_facts(project_id)
        try:
            return self.probe(project_id, src)
        except Exception:
            return None

    def _llm_media_draft(
        self,
        goal: str,
        project_id: str,
        facts: MediaFacts | None,
        *,
        cancel_token: Any | None,
    ) -> MediaGoalDraft | None:
        llm = getattr(getattr(self, "planner", None), "llm", None)
        if llm is None or not getattr(llm, "available", False):
            return None
        summary = self._facts_summary(facts)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是媒体修复/转换意图解析器。只输出一个 JSON 对象，"
                    "字段必须属于 MediaGoalDraft。operation 取 "
                    "diagnose/repair/convert/compress；不是媒体操作时 operation=null。"
                    "strategy 取 auto/remux/reencode/audio_sync/sharpen。"
                    "禁止输出路径、URL、shell、ffmpeg 命令或任何额外字段。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": goal, "media": summary},
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            return llm.chat_json(
                messages,
                MediaGoalDraft,
                temperature=0.0,
                cancel_token=cancel_token,
                usage_scope=f"media:{project_id}",
            )
        except Exception as exc:
            self.logger.warning(
                "media_intent_llm_failed",
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

    @staticmethod
    def _facts_summary(facts: MediaFacts | None) -> dict:
        if facts is None:
            return {}
        return {
            "has_video": facts.video is not None,
            "has_audio": facts.audio is not None,
            "duration_seconds": round(facts.duration.to_float(), 3),
            "fps": facts.fps,
            "vfr": facts.vfr,
            "format": facts.format_name,
            "video_codec": facts.video.codec_name if facts.video else None,
            "audio_codec": facts.audio.codec_name if facts.audio else None,
        }

    def _project_media_facts(self, project_id: str) -> MediaFacts | None:
        try:
            return self.get_media_facts(project_id)
        except Exception:
            src_rel = self._project_source_rel(project_id)
            if not src_rel:
                return None
            try:
                return self.probe(project_id, src_rel)
            except Exception:
                return None

    def _project_source_rel(self, project_id: str) -> str | None:
        try:
            return self._require_asset(project_id).src
        except Exception:
            try:
                assets = self.list_audio_assets(project_id)
            except Exception:
                return None
            return assets[0].get("path") if assets else None

    def _plan_from_draft(
        self,
        draft: MediaGoalDraft,
        facts: MediaFacts | None,
        src_rel: str | None,
    ) -> MediaOperationPlan:
        if draft.operation == "diagnose":
            return MediaOperationPlan(
                operation="diagnose",
                source=src_rel,
                reason=draft.reason,
                confidence=draft.confidence,
            )
        if draft.operation == "repair":
            profile = default_repair_profile()
            try:
                if draft.target_container in AUDIO_CONTAINERS:
                    codec = (
                        draft.target_audio_codec
                        or default_audio_codec(draft.target_container)
                    )
                    profile = self._profile_with(
                        profile,
                        media_type="audio",
                        container=draft.target_container,
                        video_codec="copy",
                        width=None,
                        height=None,
                        fps=None,
                        hdr_tonemap=False,
                        keep_10bit=False,
                        subtitle_tracks=[],
                        audio_tracks=[
                            AudioOutput(codec=codec, bitrate="160k")
                        ],
                    )
                elif draft.target_container is not None:
                    profile = self._profile_with(
                        profile, container=draft.target_container
                    )
                if (
                    draft.target_video_codec is not None
                    and profile.media_type == "video"
                ):
                    profile = self._profile_with(
                        profile, video_codec=draft.target_video_codec
                    )
                if (
                    draft.target_audio_codec is not None
                    and profile.media_type == "video"
                ):
                    tracks = list(profile.audio_tracks)
                    if tracks:
                        tracks[0] = tracks[0].model_copy(
                            update={"codec": draft.target_audio_codec}
                        )
                    else:
                        tracks = [
                            AudioOutput(codec=draft.target_audio_codec)
                        ]
                    profile = self._profile_with(
                        profile, audio_tracks=tracks
                    )
            except ValueError:
                # 目标编码/容器不兼容时退回安全默认值，仍允许诊断/修复。
                profile = default_repair_profile()
            request = RepairRequest(
                src=src_rel,
                strategy=draft.strategy,
                profile=profile,
                audio_offset_seconds=draft.audio_offset_seconds,
            )
            return MediaOperationPlan(
                operation="repair",
                source=src_rel,
                reason=draft.reason,
                confidence=draft.confidence,
                repair=request,
            )

        transcode = self._transcode_request_from_draft(
            draft, facts, src_rel
        )
        return MediaOperationPlan(
            operation=draft.operation,
            source=src_rel,
            reason=draft.reason,
            confidence=draft.confidence,
            transcode=transcode,
        )

    @staticmethod
    def _source_container(src_rel: str | None, facts: MediaFacts | None) -> str:
        suffix = Path(src_rel or "").suffix.lstrip(".").lower()
        if suffix in AUDIO_CONTAINERS | _VIDEO_CONTAINERS:
            return suffix
        if facts is not None and facts.video is None:
            return "m4a"
        return "mp4"

    def _transcode_request_from_draft(
        self,
        draft: MediaGoalDraft,
        facts: MediaFacts | None,
        src_rel: str | None,
    ) -> TranscodeRequest:
        has_video = True if facts is None else facts.video is not None
        has_audio = True if facts is None else facts.audio is not None
        container = draft.target_container or self._source_container(
            src_rel, facts
        )
        media_type = "audio" if container in AUDIO_CONTAINERS else "video"
        if not has_video:
            media_type = "audio"
            if container not in AUDIO_CONTAINERS:
                container = "m4a"
        if media_type == "audio" and not has_audio:
            raise ValueError("源文件没有音频轨，无法生成音频输出")
        audio_codec = draft.target_audio_codec or (
            default_audio_codec(container)
            if media_type == "audio"
            else "aac"
        )
        video_codec = (
            "copy"
            if media_type == "audio"
            else (draft.target_video_codec or "libx264")
        )
        width: int | None = None
        height: int | None = None
        if (
            draft.max_height
            and facts is not None
            and facts.video is not None
            and facts.video.width
            and facts.video.height
            and media_type == "video"
            and facts.video.height > draft.max_height
        ):
            height = int(draft.max_height)
            width = int(
                round(facts.video.width * height / facts.video.height)
            )
            width += width % 2
            height += height % 2
        def build(video: str, audio: str) -> TranscodeProfile:
            return TranscodeProfile(
                media_type=media_type,  # type: ignore[arg-type]
                container=container,  # type: ignore[arg-type]
                video_codec=video,  # type: ignore[arg-type]
                preset="medium",
                crf=(
                    draft.crf
                    if draft.crf is not None
                    else (28 if draft.operation == "compress" else 23)
                ),
                width=width,
                height=height,
                audio_tracks=[
                    AudioOutput(
                        codec=audio,  # type: ignore[arg-type]
                        bitrate=(
                            "128k" if media_type == "audio" else "160k"
                        ),
                    )
                ],
            )

        try:
            profile = build(video_codec, audio_codec)
        except ValueError:
            # 目标编码与容器不兼容时回退到安全组合，避免 AI 计划直接失败。
            video_codec = "copy" if media_type == "audio" else "libx264"
            audio_codec = (
                default_audio_codec(container)  # type: ignore[arg-type]
                if media_type == "audio"
                else "aac"
            )
            profile = build(video_codec, audio_codec)
        return TranscodeRequest(src=src_rel, profile=profile)

    # ── 统一入口 ──────────────────────────────────────────────────────
    def run_media_operation(
        self,
        project_id: str,
        goal: str,
        reporter: Any | None = None,
        *,
        src: str | None = None,
    ) -> dict:
        cancel_token = getattr(reporter, "cancel_token", None)
        plan = self.interpret_media_goal(
            project_id, goal, src=src, cancel_token=cancel_token
        )
        if plan is None:
            raise ValueError(
                "未识别为媒体修复/转换意图；请描述要修复的问题或目标格式"
            )
        if plan.operation == "diagnose":
            diagnosis = self.diagnose_media(project_id, src)
            return {
                "operation": "diagnose",
                "plan": plan.model_dump(mode="json"),
                "diagnosis": diagnosis.model_dump(mode="json"),
            }
        if plan.operation == "repair":
            assert plan.repair is not None
            result = self.repair_media(project_id, plan.repair, reporter)
            result["operation"] = "repair"
            result["plan"] = plan.model_dump(mode="json")
            return result
        assert plan.transcode is not None
        result = self.transcode(project_id, plan.transcode, reporter)
        result["operation"] = plan.operation
        result["plan"] = plan.model_dump(mode="json")
        return result


__all__ = ["MediaToolsMixin"]
