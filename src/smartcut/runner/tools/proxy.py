"""预览代理：低码率 H.264/AAC，供桌面客户端与前端预览。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.runner.tools.base import ToolContext

HwAccel = Literal["cuda", "qsv", "d3d11va", "vaapi"]


class ProxyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    height: int = Field(default=720, ge=120, le=2160)
    crf: int = Field(default=23, ge=0, le=51)
    preset: str = "veryfast"
    encoder: EncoderProfile | None = None
    hwaccel: HwAccel | None = None


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ProxyArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    encoder_args = (
        video_encoder_args(args.encoder)
        if args.encoder is not None
        else ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf)]
    )
    hwaccel = ["-hwaccel", args.hwaccel] if args.hwaccel else []
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        *hwaccel,
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        f"scale=-2:{args.height}",
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dst),
    ]
