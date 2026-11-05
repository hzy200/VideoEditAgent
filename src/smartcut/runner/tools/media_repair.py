"""媒体简单修复工具：重封装、CFR 重编码、音画同步、轻度锐化。

只生成 argv，不执行进程；输入输出必须是工作区相对路径。
修复不承诺恢复丢失内容，只处理容器/时间戳/起始偏移等结构性问题。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.media_ops import RepairStrategy
from smartcut.contracts.transcode import (
    AudioOutput,
    TranscodeProfile,
    default_audio_codec,
)
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.transcode import (
    audio_codec_args,
    common_metadata,
    container_flags,
    measured_loudnorm_filter,
    video_codec_args,
    video_filter,
)


class MediaRepairArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    strategy: RepairStrategy
    profile: TranscodeProfile
    audio_offset_seconds: float = Field(default=0.0, ge=-60.0, le=60.0)
    pad_duration_seconds: float | None = Field(
        default=None, ge=0.0, le=86_400.0
    )
    sharpen_strength: float = Field(default=0.5, ge=0.0, le=1.0)
    has_video: bool = True
    error_tolerant: bool = True
    loudnorm_measured: dict[str, str] | None = None


def _input_flags(args: MediaRepairArgs) -> list[str]:
    if not args.error_tolerant:
        return []
    return [
        "-err_detect",
        "ignore_err",
        "-fflags",
        "+genpts",
        "-analyzeduration",
        "100M",
        "-probesize",
        "100M",
    ]


def _maps(args: MediaRepairArgs) -> list[str]:
    maps: list[str] = []
    if args.has_video and args.profile.media_type == "video":
        maps.extend(["-map", "0:v:0?"])
    else:
        maps.append("-vn")
    maps.extend(["-map", "0:a:0?", "-sn"])
    return maps


def _video_filters(args: MediaRepairArgs) -> str | None:
    filters: list[str] = []
    base = video_filter(args.profile)
    if base:
        filters.append(base)
    if args.strategy in {"reencode", "sharpen"}:
        if args.profile.fps:
            filters.append(f"fps={args.profile.fps}")
        filters.append("setpts=PTS-STARTPTS")
    if args.strategy == "sharpen":
        strength = max(0.0, float(args.sharpen_strength))
        filters.append(f"unsharp=5:5:{strength:.2f}:5:5:0")
    return ",".join(filters) if filters else None


def _audio_filters(args: MediaRepairArgs) -> list[str]:
    if args.strategy == "remux":
        return []
    filters: list[str] = []
    offset = float(args.audio_offset_seconds or 0.0)
    if args.strategy == "audio_sync":
        if offset > 0.001:
            filters.append(f"adelay={int(round(offset * 1000))}:all=1")
        elif offset < -0.001:
            filters.append(
                f"atrim=start={abs(offset):.6f},asetpts=PTS-STARTPTS"
            )
    filters.append("aresample=async=1:first_pts=0")
    if (
        args.strategy == "audio_sync"
        and args.has_video
        and args.profile.media_type == "video"
        and args.pad_duration_seconds
    ):
        # 用有界 apad 对齐视频时长，避免无限补静音导致任务挂起。
        filters.append(
            f"apad=whole_dur={args.pad_duration_seconds:.3f}"
        )
    return filters


def _audio_profile_with_codec(
    profile: TranscodeProfile,
    *,
    needs_filter: bool,
) -> TranscodeProfile:
    if not needs_filter:
        return profile
    tracks = profile.audio_tracks
    if tracks and tracks[0].codec != "copy":
        return profile
    codec = (
        default_audio_codec(profile.container)
        if profile.media_type == "audio"
        else "aac"
    )
    return profile.model_copy(
        update={
            "audio_tracks": [
                AudioOutput(stream_index=0, codec=codec, bitrate="160k")
            ]
        }
    )


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, MediaRepairArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    profile = args.profile

    argv: list[str] = [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        *_input_flags(args),
        "-i",
        str(src),
        "-max_muxing_queue_size",
        "1024",
        *_maps(args),
    ]

    audio_filters = _audio_filters(args)
    loudnorm = measured_loudnorm_filter(profile, args.loudnorm_measured)

    if args.strategy == "remux":
        argv.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
    elif args.strategy == "audio_sync":
        if args.has_video and profile.media_type == "video":
            argv.extend(["-c:v", "copy"])
        if audio_filters:
            argv.extend(["-af", ",".join(audio_filters)])
        audio_profile = _audio_profile_with_codec(
            profile, needs_filter=True
        )
        argv.extend(audio_codec_args(audio_profile, loudnorm))
        if (
            args.has_video
            and profile.media_type == "video"
            and args.pad_duration_seconds
        ):
            argv.extend(["-shortest"])
    else:
        if args.has_video and profile.media_type == "video":
            if profile.video_codec == "copy":
                raise ValueError("重编码/锐化修复不能使用视频 copy")
            filters = _video_filters(args)
            if filters:
                argv.extend(["-vf", filters])
            argv.extend(video_codec_args(profile))
            pix_fmt = profile.effective_pix_fmt()
            if pix_fmt:
                argv.extend(["-pix_fmt", pix_fmt])
            if profile.fps:
                argv.extend(["-r", profile.fps])
            if profile.fps_mode != "passthrough":
                argv.extend(["-fps_mode", profile.fps_mode])
        if audio_filters:
            argv.extend(["-af", ",".join(audio_filters)])
        audio_profile = _audio_profile_with_codec(
            profile, needs_filter=bool(audio_filters)
        )
        argv.extend(audio_codec_args(audio_profile, loudnorm))

    argv.extend(common_metadata(profile))
    argv.extend(container_flags(profile))
    argv.append(str(dst))
    return argv


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, MediaRepairArgs)
    ctx.workspace.resolve_output(args.dst)


__all__ = ["MediaRepairArgs", "build_argv", "prepare"]
