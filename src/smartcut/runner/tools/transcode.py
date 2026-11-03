"""独立转码/封装工具。

只生成 argv，不执行进程。输入输出必须是工作区相对路径。
转码 profile 与 EditIR 完全解耦。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from smartcut.contracts.transcode import TranscodeProfile
from smartcut.runner.tools.base import ToolContext


class TranscodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    profile: TranscodeProfile
    loudnorm_measured: dict[str, str] | None = None


def _at_least_even(value: int) -> int:
    return value if value % 2 == 0 else value + 1


def _video_filter(profile: TranscodeProfile) -> str | None:
    filters: list[str] = []
    if profile.hdr_tonemap:
        filters.extend(
            [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
                "format=yuv420p",
            ]
        )
    if profile.width is not None and profile.height is not None:
        width = _at_least_even(profile.width)
        height = _at_least_even(profile.height)
        if profile.scale_mode == "stretch":
            filters.append(f"scale={width}:{height}")
        elif profile.scale_mode == "crop":
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}"
            )
        else:
            filters.append(
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            )
    if not filters:
        return None
    return ",".join(filters)


def _video_codec_args(profile: TranscodeProfile) -> list[str]:
    codec = profile.video_codec
    if codec == "copy":
        return ["-c:v", "copy"]
    if codec == "libx264":
        args = ["-c:v", "libx264", "-preset", profile.preset]
        if profile.video_bitrate:
            args.extend(["-b:v", profile.video_bitrate])
        else:
            args.extend(["-crf", str(profile.crf if profile.crf is not None else 20)])
    elif codec == "libx265":
        args = ["-c:v", "libx265", "-preset", profile.preset]
        if profile.video_bitrate:
            args.extend(["-b:v", profile.video_bitrate])
        else:
            args.extend(
                [
                    "-crf",
                    str(profile.crf if profile.crf is not None else 20),
                    "-x265-params",
                    "log-level=error",
                ]
            )
    elif codec == "libsvtav1":
        args = ["-c:v", "libsvtav1", "-preset", profile.preset]
        if profile.video_bitrate:
            args.extend(["-b:v", profile.video_bitrate])
        else:
            args.extend(["-crf", str(profile.crf if profile.crf is not None else 30)])
    elif codec == "prores_ks":
        args = ["-c:v", "prores_ks", "-profile:v", "3"]
    else:  # pragma: no cover - contract 已枚举
        raise ValueError(f"未支持的视频编码: {codec}")

    if profile.max_bitrate:
        args.extend(["-maxrate", profile.max_bitrate])
        # 以 2x maxrate 作为 bufsize，避免瞬时峰值导致码率失控。
        text = profile.max_bitrate.rstrip("kKmM")
        suffix = profile.max_bitrate[len(text) :] or "k"
        try:
            args.extend(["-bufsize", f"{int(float(text) * 2)}{suffix}"])
        except ValueError:
            pass
    return args


def _audio_codec_args(
    profile: TranscodeProfile,
    loudnorm: str | None,
) -> list[str]:
    tracks = profile.audio_tracks
    if not tracks:
        return ["-c:a", "copy"]

    args: list[str] = []
    for index, track in enumerate(tracks):
        args.extend([f"-c:a:{index}", track.codec])
        if track.codec != "copy":
            if track.bitrate:
                args.extend([f"-b:a:{index}", track.bitrate])
            if track.channels is not None:
                args.extend([f"-ac:a:{index}", str(track.channels)])
        filters: list[str] = []
        if track.gain_db:
            filters.append(f"volume={track.gain_db}dB")
        if loudnorm:
            filters.append(loudnorm)
        if filters:
            args.extend([f"-filter:a:{index}", ",".join(filters)])
    return args


def _measured_loudnorm(
    profile: TranscodeProfile,
    measured: dict[str, str] | None,
) -> str | None:
    if profile.loudness_target is None:
        return None
    if not profile.loudnorm_two_pass or not measured:
        return (
            f"loudnorm=I={profile.loudness_target}:"
            f"TP={profile.true_peak}:LRA={profile.lra}"
        )
    keys = (
        "measured_I",
        "measured_TP",
        "measured_LRA",
        "measured_thresh",
        "offset",
    )
    parts = [
        f"I={profile.loudness_target}",
        f"TP={profile.true_peak}",
        f"LRA={profile.lra}",
    ]
    for key in keys:
        value = measured.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    parts.append("linear=true")
    return "loudnorm=" + ":".join(parts)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, TranscodeArgs)
    profile = args.profile
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})

    argv: list[str] = [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
    ]
    if profile.autorotate:
        argv.append("-autorotate")
    else:
        argv.append("-noautorotate")
    argv.extend(["-i", str(src)])

    input_count = 1
    audio_input_index: dict[int, int] = {}
    subtitle_input_index: dict[int, int] = {}
    for index, track in enumerate(profile.audio_tracks):
        if track.src is not None:
            audio_input_index[index] = input_count
            argv.extend(["-i", str(ctx.workspace.resolve_input(track.src))])
            input_count += 1
    for index, track in enumerate(profile.subtitle_tracks):
        if track.src is not None:
            subtitle_input_index[index] = input_count
            argv.extend(["-i", str(ctx.workspace.resolve_input(track.src))])
            input_count += 1

    # 视频始终只取第一条视频流；转码不是多源时间线。
    argv.extend(["-map", "0:v:0"])

    if profile.audio_tracks:
        for index, track in enumerate(profile.audio_tracks):
            if track.src is None:
                argv.extend(["-map", f"0:a:{track.stream_index}?"])
            else:
                source = audio_input_index[index]
                argv.extend(["-map", f"{source}:a:{track.stream_index}?"])
    else:
        argv.extend(["-map", "0:a:0?"])

    for index, track in enumerate(profile.subtitle_tracks):
        if track.src is None:
            argv.extend(["-map", f"0:s:{track.stream_index}?"])
        else:
            source = subtitle_input_index[index]
            argv.extend(["-map", f"{source}:s:{track.stream_index}?"])
    if not profile.subtitle_tracks:
        argv.append("-sn")

    video_filter = _video_filter(profile)
    if video_filter:
        argv.extend(["-vf", video_filter])
    if profile.video_codec != "copy":
        argv.extend(_video_codec_args(profile))
        pix_fmt = profile.effective_pix_fmt()
        if pix_fmt:
            argv.extend(["-pix_fmt", pix_fmt])
        if profile.fps:
            argv.extend(["-r", profile.fps])
        if profile.fps_mode != "passthrough":
            argv.extend(["-fps_mode", profile.fps_mode])
    else:
        argv.extend(["-c:v", "copy"])

    loudnorm = _measured_loudnorm(profile, args.loudnorm_measured)
    argv.extend(_audio_codec_args(profile, loudnorm))
    for index, track in enumerate(profile.audio_tracks):
        if track.language:
            argv.extend([f"-metadata:s:a:{index}", f"language={track.language}"])
        if track.title:
            argv.extend([f"-metadata:s:a:{index}", f"title={track.title}"])

    for index, track in enumerate(profile.subtitle_tracks):
        if track.codec != "copy":
            argv.extend([f"-c:s:{index}", track.codec])
        if track.language:
            argv.extend([f"-metadata:s:s:{index}", f"language={track.language}"])
        if track.title:
            argv.extend([f"-metadata:s:s:{index}", f"title={track.title}"])

    metadata = profile.metadata
    argv.extend(["-map_metadata", "0", "-map_chapters", "0"])
    for key in ("title", "artist", "album", "comment", "language"):
        value = getattr(metadata, key)
        if value:
            argv.extend(["-metadata", f"{key}={value}"])
    for key, value in metadata.extra.items():
        argv.extend(["-metadata", f"{key}={value}"])

    if profile.container in {"mp4", "mov"}:
        flags: list[str] = []
        if profile.faststart:
            flags.append("faststart")
        if metadata.extra:
            flags.append("use_metadata_tags")
        if flags:
            argv.extend(["-movflags", "+" + "+".join(flags)])
    argv.append(str(dst))
    return argv


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, TranscodeArgs)
    ctx.workspace.resolve_output(args.dst)


__all__ = ["TranscodeArgs", "build_argv", "prepare"]
