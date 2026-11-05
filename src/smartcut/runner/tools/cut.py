"""裁剪工具：默认帧精确重编码；copy 模式保留给关键帧对齐场景。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.filters import validate_video_filters


class CutArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    start: RationalTime
    end: RationalTime
    mode: Literal["reencode", "copy"] = "reencode"
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    video_track_timescale: int = Field(default=90_000, gt=0)
    fps_mode: Literal["cfr", "vfr", "passthrough"] = "passthrough"
    fps: str | None = Field(default=None, max_length=32)
    encoder: EncoderProfile | None = None
    # 逐片段特效（L2）使用；只接受 Compiler 生成的白名单滤镜。
    video_filters: list[str] = Field(default_factory=list)
    # 变速：输出时长 = 源区间时长 / speed；atempo 支持 0.5–2.0。
    speed: float = Field(default=1.0, ge=0.5, le=2.0, allow_inf_nan=False)
    has_audio: bool = True
    # 多源拼接时为无音轨素材生成静音，并统一采样率/声道。
    ensure_audio: bool = False
    audio_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    audio_channels: int | None = Field(default=None, ge=1, le=8)

    @model_validator(mode="after")
    def _ordered(self) -> "CutArgs":
        if self.end <= self.start:
            raise ValueError("cut.end 必须大于 cut.start")
        validate_video_filters(self.video_filters)
        if self.mode == "copy" and self.video_filters:
            raise ValueError("copy 模式不能应用视频滤镜，请使用 reencode")
        if self.mode == "copy" and self.speed != 1.0:
            raise ValueError("copy 模式不能变速，请使用 reencode")
        return self


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, CutArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    start = args.start.to_ffmpeg_seconds()
    duration = (args.end - args.start).to_ffmpeg_seconds()
    retimed = args.speed != 1.0

    argv = [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-ss",
        start,
    ]
    if retimed:
        # -t 放在 -i 之前，限制的是输入读取时长；setpts 之后再得到目标输出时长。
        argv.extend(["-t", duration, "-i", str(src)])
    else:
        argv.extend(["-i", str(src), "-t", duration])
    silent_audio = (
        not args.has_audio and args.ensure_audio and args.mode != "copy"
    )
    if silent_audio:
        rate = args.audio_sample_rate or 48000
        channels = args.audio_channels or 2
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r={rate}:cl={'stereo' if channels == 2 else 'mono'}",
            ]
        )
    argv.extend(["-map", "0:v:0"])
    argv.extend(["-map", "1:a:0" if silent_audio else "0:a:0?"])

    video_filters = list(args.video_filters)
    if retimed:
        video_filters.insert(0, f"setpts=PTS/{args.speed}")
    if video_filters:
        argv.extend(["-vf", ",".join(video_filters)])
    if retimed and args.has_audio:
        argv.extend(["-af", f"atempo={args.speed}"])
    if args.mode == "copy":
        argv.extend(
            [
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                "-movflags",
                "+faststart",
            ]
        )
    else:
        if args.encoder is not None:
            argv.extend(video_encoder_args(args.encoder))
        else:
            argv.extend(
                [
                    "-c:v",
                    args.video_codec,
                    "-preset",
                    args.preset,
                    "-crf",
                    str(args.crf),
                ]
            )
        if args.fps:
            argv.extend(["-r", args.fps])
        if args.fps_mode != "passthrough":
            argv.extend(["-fps_mode", args.fps_mode])
        argv.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                str(args.video_track_timescale),
                "-c:a",
                args.audio_codec,
                "-b:a",
                args.audio_bitrate,
            ]
        )
        if args.audio_sample_rate:
            argv.extend(["-ar", str(args.audio_sample_rate)])
        if args.audio_channels:
            argv.extend(["-ac", str(args.audio_channels)])
        if silent_audio:
            argv.append("-shortest")
        argv.extend(["-movflags", "+faststart"])
    argv.append(str(dst))
    return argv
