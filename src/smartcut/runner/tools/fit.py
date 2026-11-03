"""9:16 重构图：按 Compiler 计算的 crop 框裁切并缩放到目标分辨率。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.runner.tools.base import ToolContext


class FitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    crop_x: int = Field(ge=0)
    crop_y: int = Field(ge=0)
    crop_width: int = Field(gt=0)
    crop_height: int = Field(gt=0)
    target_width: int = Field(default=1080, ge=2)
    target_height: int = Field(default=1920, ge=2)
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    video_track_timescale: int = Field(default=90_000, gt=0)
    encoder: EncoderProfile | None = None

    @model_validator(mode="after")
    def _even(self) -> "FitArgs":
        for name in ("crop_x", "crop_y", "crop_width", "crop_height", "target_width", "target_height"):
            if getattr(self, name) % 2 != 0:
                raise ValueError(f"{name} 必须为偶数")
        return self


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, FitArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    video_filter = (
        f"crop={args.crop_width}:{args.crop_height}:{args.crop_x}:{args.crop_y},"
        f"scale={args.target_width}:{args.target_height},setsar=1"
    )
    encoder_args = (
        video_encoder_args(args.encoder)
        if args.encoder is not None
        else ["-c:v", args.video_codec, "-preset", args.preset, "-crf", str(args.crf)]
    )
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter,
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-video_track_timescale",
        str(args.video_track_timescale),
        "-c:a",
        args.audio_codec,
        "-b:a",
        args.audio_bitrate,
        "-movflags",
        "+faststart",
        str(dst),
    ]
