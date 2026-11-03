"""抽取 PCM WAV：转写默认 16k 单声道，分离可指定 44.1k 立体声。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.runner.tools.base import ToolContext


class ExtractAudioArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    channels: int = Field(default=1, ge=1, le=8)
    sample_rate: int = Field(default=16_000, ge=8_000, le=192_000)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ExtractAudioArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    return [
        ctx.settings.ffmpeg_bin,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-ac",
        str(args.channels),
        "-ar",
        str(args.sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
