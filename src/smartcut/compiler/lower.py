"""IR lowering：纯函数，不做任何隐式调整。

P1 管线：cut → concat → fit → captions(burn) → loudnorm → verify。
字幕时间由 trim 结构推导；fit crop 由感知人脸锚点与源尺寸推导。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from smartcut.compiler.audio_mix import resolve_mix_tracks
from smartcut.compiler.captions import build_cues
from smartcut.compiler.effects import (
    build_output_filters,
    build_segment_filters,
)
from smartcut.compiler.fit import compute_fit_crop
from smartcut.compiler.loudness import parse_target_lufs
from smartcut.compiler.validate import validate_ir
from smartcut.contracts.audio import AudioAssetRef
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.overlay_assets import OverlayAssetRef
from smartcut.contracts.time import RationalTime, rt_to_dict
from smartcut.contracts.tools import ToolCall
from smartcut.contracts.transcript import Transcript
from smartcut.errors import IRValidationError


@dataclass(frozen=True)
class CompiledPlan:
    tool_calls: list[ToolCall] = field(default_factory=list)
    output_path: str = ""
    expected_duration: RationalTime | None = None
    expected_fps: str | None = None
    expect_audio: bool = False
    width: int | None = None
    height: int | None = None
    captions: list[CaptionCue] = field(default_factory=list)
    caption_srt_path: str | None = None
    caption_vtt_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def dump(self) -> dict:
        return {
            "tool_calls": [call.model_dump(mode="json") for call in self.tool_calls],
            "output_path": self.output_path,
            "expected_duration": (
                self.expected_duration.model_dump(mode="json")
                if self.expected_duration is not None
                else None
            ),
            "expected_fps": self.expected_fps,
            "expect_audio": self.expect_audio,
            "width": self.width,
            "height": self.height,
            "captions": [cue.model_dump(mode="json") for cue in self.captions],
            "caption_srt_path": self.caption_srt_path,
            "caption_vtt_path": self.caption_vtt_path,
            "warnings": self.warnings,
        }


def _ass_colour(value: str | None) -> str:
    text = str(value or "#FFFFFF").strip().lstrip("#")
    if len(text) != 6:
        return "&H00FFFFFF"
    try:
        int(text, 16)
    except ValueError:
        return "&H00FFFFFF"
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue}{green}{red}".upper().replace("&H00", "&H00")


def _caption_args(caption_style: dict | None) -> dict:
    style = caption_style or {}
    position = str(style.get("position") or "bottom")
    alignment = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
    return {
        "font_name": str(style.get("font_name") or "Microsoft YaHei"),
        "font_size": int(style.get("font_size") or 22),
        "primary_colour": _ass_colour(style.get("color")),
        "alignment": alignment,
    }


def _faces_from_perception(perception: dict | None) -> FaceReport | None:
    if not perception:
        return None
    payload = perception.get("faces")
    if not payload:
        return None
    try:
        return FaceReport.model_validate(payload)
    except Exception:
        return None


def lower(
    ir: EditIR,
    *,
    transcript: Transcript | None = None,
    perception: dict | None = None,
    encoder: EncoderProfile | None = None,
    caption_style: dict | None = None,
    audio_assets: dict[str, AudioAssetRef] | None = None,
    overlay_assets: dict[str, OverlayAssetRef] | None = None,
    hdr_tonemap: bool = False,
    keep_10bit: bool = False,
    fonts_dir: str | None = None,
    preview_height: int | None = None,
) -> CompiledPlan:
    validate_ir(ir)

    facts = ir.media_facts
    base_dir = f"renders/r{ir.revision}"
    output_path = f"{base_dir}/output.mp4"
    warnings: list[str] = []
    total_duration = ir.total_duration()
    copy_mode = ir.export.cut_mode == "copy"
    if facts.vfr:
        warnings.append(
            "源素材为 VFR；渲染将统一为 CFR"
            if not copy_mode
            else "copy 模式保留源 VFR/时间基，可能影响精确切点"
        )
    if facts.video.pix_fmt and "10" in facts.video.pix_fmt and not keep_10bit:
        warnings.append(
            f"源素材为 {facts.video.pix_fmt}，渲染管线输出 8bit yuv420p（已降级）"
        )
    if facts.color_transfer and facts.color_transfer.lower() not in {"bt709", "sdr"}:
        warnings.append(
            f"源素材色彩传输为 {facts.color_transfer}，"
            + ("将执行 HDR→SDR tonemap" if hdr_tonemap else "未启用 tonemap，输出可能偏色")
        )
    use_mixer = ir.audio.tracks is not None
    mix_tracks = resolve_mix_tracks(ir, audio_assets, total_duration, warnings)

    fit_effect = next(
        (
            effect
            for effect in ir.effects
            if effect.type == "fit" and effect.enabled
        ),
        None,
    )
    faces = _faces_from_perception(perception)
    fit_crop = None
    if fit_effect is not None:
        anchor = fit_effect.anchor or "face_bbox"
        use_faces = anchor == "face_bbox"
        fit_crop = compute_fit_crop(
            facts.video.width,
            facts.video.height,
            faces,
            use_faces=use_faces,
        )
        if use_faces and (faces is None or not faces.anchors):
            warnings.append("未检测到人脸，9:16 使用中心裁切")

    post_width = fit_crop.target_width if fit_crop is not None else facts.video.width
    post_height = fit_crop.target_height if fit_crop is not None else facts.video.height
    effect_pre, effect_post = build_output_filters(
        ir.effects,
        fps=facts.fps,
        width=post_width,
        height=post_height,
        total_duration=total_duration,
    )
    output_width, output_height = post_width, post_height
    preview_scale: str | None = None
    if preview_height is not None and preview_height > 0:
        height = max(2, int(preview_height))
        height -= height % 2
        width = max(2, round(post_width * height / post_height))
        width -= width % 2
        output_width, output_height = width, height
        if (width, height) != (post_width, post_height):
            preview_scale = f"scale={width}:{height}"
    overlays_payload: list[dict] = []
    for overlay in ir.overlays:
        item = overlay.model_dump(mode="json")
        item.pop("asset_id", None)
        item.pop("scale", None)
        if overlay.kind == "image":
            ref = (overlay_assets or {}).get(overlay.asset_id or "")
            if ref is None:
                raise IRValidationError(
                    f"图片水印资产不存在: {overlay.asset_id}"
                )
            width = max(2, round(output_width * overlay.scale))
            width -= width % 2
            item["src"] = ref.src
            item["width"] = width
        overlays_payload.append(item)
    removals_payload = []
    for removal in ir.removals:
        item = removal.model_dump(mode="json")
        item.pop("id", None)
        removals_payload.append(item)

    post_filters = list(effect_post)
    if preview_scale is not None:
        post_filters.append(preview_scale)
    effects_present = bool(effect_pre or post_filters)
    segment_filters = [
        build_segment_filters(
            ir.effects,
            segment_id=op.id,
            duration=op.output_duration(),
            fps=facts.fps,
            width=facts.video.width,
            height=facts.video.height,
        )
        for op in ir.sequence
    ]
    any_segment_filters = any(segment_filters)
    enabled_effects = [effect for effect in ir.effects if effect.enabled]
    if enabled_effects and not effects_present and not any_segment_filters:
        warnings.append("特效参数均为默认值，未产生视频滤镜")

    cues = build_cues(ir, transcript)
    burn_captions = ir.export.burn_captions and bool(cues)
    if ir.export.burn_captions and not cues:
        warnings.append("请求烧录字幕，但没有可用手动字幕/转写时间戳")

    target_lufs: float | None = None
    if ir.audio.loudness is not None:
        if use_mixer or facts.has_audio:
            target_lufs = parse_target_lufs(ir.audio.loudness)
        else:
            warnings.append("请求响度归一，但素材没有音频轨，已跳过")

    audio_adjusted = bool(ir.audio.volume_db or ir.audio.pitch_semitones)
    if audio_adjusted and not use_mixer and not facts.has_audio:
        warnings.append("请求音量/音调调整，但素材没有音频轨，已跳过")
        audio_adjusted = False

    post_steps: list[str] = []
    if fit_crop is not None:
        post_steps.append("fit")
    if effects_present:
        post_steps.append("effects")
    if ir.removals:
        post_steps.append("removals")
    if ir.overlays:
        post_steps.append("overlays")
    if burn_captions:
        post_steps.append("subtitles")
    if not use_mixer:
        if audio_adjusted:
            post_steps.append("audio")
        if target_lufs is not None:
            post_steps.append("loudnorm")
    needs_post = bool(post_steps)

    calls: list[ToolCall] = [
        ToolCall(seq=0, tool="probe", args={"src": facts.src}, description="确认源素材事实"),
    ]
    encoder_args = (
        {"encoder": encoder.model_dump(mode="json")} if encoder is not None else {}
    )

    single_segment = len(ir.sequence) == 1
    transition_by_after = {item.after: item for item in ir.transitions}
    segment_paths: list[str] = []
    for index, op in enumerate(ir.sequence):
        if single_segment and not needs_post and not use_mixer:
            dst = output_path
        else:
            dst = f"{base_dir}/seg_{index:03d}.mp4"
        cut_args: dict = {
            "src": facts.src,
            "dst": dst,
            "start": rt_to_dict(op.range.start),
            "end": rt_to_dict(op.range.end),
            "mode": ir.export.cut_mode,
            "video_codec": "libx264",
            "preset": "veryfast",
            "crf": 18,
            "audio_codec": "aac",
            "audio_bitrate": "192k",
            "video_track_timescale": 90_000,
            "fps_mode": (
                "cfr"
                if (
                    not copy_mode
                    or segment_filters[index]
                    or op.speed != 1.0
                )
                else "passthrough"
            ),
            **encoder_args,
        }
        if op.speed != 1.0:
            cut_args["speed"] = op.speed
            cut_args["has_audio"] = facts.has_audio
        if segment_filters[index]:
            cut_args["video_filters"] = segment_filters[index]
        calls.append(
            ToolCall(
                seq=len(calls),
                tool="cut",
                args=cut_args,
                description=op.tag or f"保留片段 {index + 1}",
            )
        )
        segment_paths.append(dst)

    if single_segment:
        current = segment_paths[0]
    else:
        assembled = (
            output_path
            if not needs_post and not use_mixer
            else f"{base_dir}/assembled.mp4"
        )
        if ir.transitions:
            zero = RationalTime(value=0, rate=90_000)
            compose_transitions = []
            for index, op in enumerate(ir.sequence[:-1]):
                spec = transition_by_after.get(op.id) if op.id else None
                compose_transitions.append(
                    {
                        "type": spec.type if spec is not None else "fade",
                        "duration": rt_to_dict(
                            spec.duration if spec is not None else zero
                        ),
                    }
                )
            calls.append(
                ToolCall(
                    seq=len(calls),
                    tool="compose",
                    args={
                        "inputs": segment_paths,
                        "durations": [
                            rt_to_dict(op.output_duration())
                            for op in ir.sequence
                        ],
                        "transitions": compose_transitions,
                        "dst": assembled,
                        "has_audio": facts.has_audio,
                        "fps": facts.fps,
                        "video_codec": "libx264",
                        "preset": "veryfast",
                        "crf": 18,
                        "audio_codec": "aac",
                        "audio_bitrate": "192k",
                        "video_track_timescale": 90_000,
                        "fps_mode": "cfr",
                        **encoder_args,
                    },
                    description=f"转场合成：{len(ir.transitions)} 个转场",
                )
            )
        else:
            calls.append(
                ToolCall(
                    seq=len(calls),
                    tool="concat",
                    args={
                        "inputs": segment_paths,
                        "dst": assembled,
                        "list_path": f"{base_dir}/concat.txt",
                    },
                    description="按 IR 顺序拼接片段",
                )
            )
        current = assembled

    if ir.audio.remove_filler:
        warnings.append("remove_filler 需由 AudioAgent 局部修复；Compiler 不会静默删减音频")

    if post_steps:
        post_dst = f"{base_dir}/visual.mp4" if use_mixer else output_path
        post_args: dict = {
            "src": current,
            "dst": post_dst,
            "fps_mode": (
                "cfr" if (not copy_mode or effects_present) else "passthrough"
            ),
            "hdr_tonemap": bool(hdr_tonemap),
            **encoder_args,
        }
        if effect_pre:
            post_args["video_filters_pre"] = effect_pre
        if post_filters:
            post_args["video_filters_post"] = post_filters
        if overlays_payload:
            post_args["overlays"] = overlays_payload
        if removals_payload:
            post_args["removals"] = removals_payload
        if fit_crop is not None:
            post_args.update(
                {
                    "crop_x": fit_crop.crop_x,
                    "crop_y": fit_crop.crop_y,
                    "crop_width": fit_crop.crop_width,
                    "crop_height": fit_crop.crop_height,
                    "target_width": fit_crop.target_width,
                    "target_height": fit_crop.target_height,
                }
            )
        if burn_captions:
            post_args["srt"] = f"{base_dir}/captions.srt"
            post_args.update(_caption_args(caption_style))
        if fonts_dir:
            post_args["fonts_dir"] = fonts_dir
        if audio_adjusted:
            post_args["volume_db"] = ir.audio.volume_db
            post_args["pitch_semitones"] = ir.audio.pitch_semitones
        if target_lufs is not None:
            post_args["target_lufs"] = target_lufs
        calls.append(
            ToolCall(
                seq=len(calls),
                tool="postprocess",
                args=post_args,
                description="单次后处理：" + " + ".join(post_steps),
            )
        )
        current = post_dst

    if use_mixer:
        mix_args: dict = {
            "src": current,
            "dst": output_path,
            "tracks": mix_tracks,
            "output_duration": rt_to_dict(total_duration),
            "volume_db": ir.audio.volume_db,
            "pitch_semitones": ir.audio.pitch_semitones,
            "audio_codec": "aac",
            "audio_bitrate": "192k",
        }
        if target_lufs is not None:
            mix_args["target_lufs"] = target_lufs
        calls.append(
            ToolCall(
                seq=len(calls),
                tool="mix_audio",
                args=mix_args,
                description=f"多轨混音：{len(mix_tracks)} 条活动音轨",
            )
        )
        current = output_path

    expected_duration = total_duration
    expected_width = output_width
    expected_height = output_height
    copy_mode = ir.export.cut_mode == "copy"
    video_filtered = bool(
        fit_crop is not None
        or burn_captions
        or effects_present
        or hdr_tonemap
        or any_segment_filters
        or bool(ir.overlays)
        or bool(ir.removals)
    )
    expected_video_codec = (
        (facts.video.codec_name or "h264")
        if copy_mode and not video_filtered
        else "h264"
    )
    expected_audio_codec = (
        (facts.audio.codec_name or "aac")
        if copy_mode and facts.audio is not None
        else ("aac" if (facts.has_audio or use_mixer) else None)
    )
    expected_pix_fmt = (
        (facts.video.pix_fmt or "yuv420p")
        if copy_mode and not video_filtered
        else "yuv420p"
    )
    expected_frame_count = None
    fps_fraction = facts.fps_fraction
    if fps_fraction is not None and not facts.vfr:
        expected_frame_count = int(
            round(expected_duration.as_fraction() * fps_fraction)
        )
    verify_args: dict = {
        "src": output_path,
        "expected_duration": rt_to_dict(expected_duration),
        "expected_fps": facts.fps,
        "expect_audio": facts.has_audio or use_mixer,
        "expected_width": expected_width,
        "expected_height": expected_height,
        "expected_video_codec": expected_video_codec,
        "expected_pix_fmt": expected_pix_fmt,
        "max_av_sync_seconds": 0.5,
        # copy 模式只能落在关键帧，时长/音画可能有一帧以上漂移。
        "tolerance_frames": 5 if copy_mode else 2,
    }
    if expected_audio_codec is not None:
        verify_args["expected_audio_codec"] = expected_audio_codec
    if expected_frame_count is not None:
        verify_args["expected_frame_count"] = expected_frame_count
    calls.append(
        ToolCall(
            seq=len(calls),
            tool="verify",
            args=verify_args,
            description="ffprobe 断言成片事实",
        )
    )

    caption_srt = f"{base_dir}/captions.srt" if cues else None
    caption_vtt = f"{base_dir}/captions.vtt" if cues else None
    return CompiledPlan(
        tool_calls=calls,
        output_path=output_path,
        expected_duration=expected_duration,
        expected_fps=facts.fps,
        expect_audio=facts.has_audio or use_mixer,
        width=expected_width,
        height=expected_height,
        captions=cues,
        caption_srt_path=caption_srt,
        caption_vtt_path=caption_vtt,
        warnings=warnings,
    )
