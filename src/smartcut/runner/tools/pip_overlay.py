"""画中画/叠加视频轨：主轨视频 + 多个带时间窗的 PiP 输入。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext


class PipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    source_start: RationalTime
    source_end: RationalTime
    timeline_start: RationalTime
    duration: RationalTime
    x: float = Field(default=0.68, ge=0.0, le=1.0)
    y: float = Field(default=0.05, ge=0.0, le=1.0)
    width: float = Field(default=0.28, gt=0.0, le=1.0)
    height: float = Field(default=0.28, gt=0.0, le=1.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    z: int = Field(default=0, ge=-16, le=16)


class PipOverlayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    pips: list[PipInput] = Field(min_length=1)
    width: int = Field(ge=2, le=16384)
    height: int = Field(ge=2, le=16384)
    fps: str | None = None
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, PipOverlayArgs)
    src = ctx.workspace.resolve_input(args.src)
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs={src})
    pips = sorted(args.pips, key=lambda item: (item.z, item.timeline_start.to_float()))

    argv = [ctx.settings.ffmpeg_bin, "-hide_banner", "-y", "-i", str(src)]
    for pip in pips:
        argv.extend(["-i", str(ctx.workspace.resolve_input(pip.src))])

    graph: list[str] = []
    current = "base"
    graph.append(f"[0:v]setpts=PTS-STARTPTS[{current}]")
    for index, pip in enumerate(pips, start=1):
        width = max(2, int(round(args.width * pip.width)))
        height = max(2, int(round(args.height * pip.height)))
        start = pip.source_start.to_ffmpeg_seconds()
        end = pip.source_end.to_ffmpeg_seconds()
        timeline_start = pip.timeline_start.to_ffmpeg_seconds()
        timeline_end = float(timeline_start) + float(
            pip.duration.to_ffmpeg_seconds()
        )
        x = max(0, int(round(args.width * pip.x)))
        y = max(0, int(round(args.height * pip.y)))
        pip_label = f"pip{index}"
        next_label = f"base{index}"
        graph.append(
            f"[{index}:v]trim=start={start}:end={end},"
            f"setpts=PTS-STARTPTS+{timeline_start}/TB,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"format=rgba,colorchannelmixer=aa={pip.opacity:.3f}"
            f"[{pip_label}]"
        )
        graph.append(
            f"[{current}][{pip_label}]overlay=x={x}:y={y}:"
            f"enable='between(t,{timeline_start},{timeline_end})':"
            f"eof_action=pass[{next_label}]"
        )
        current = next_label

    argv.extend(
        [
            "-filter_complex",
            ";".join(graph),
            "-map",
            f"[{current}]",
            "-map",
            "0:a:0?",
            "-c:v",
            args.video_codec,
            "-preset",
            args.preset,
            "-crf",
            str(args.crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
        ]
    )
    if args.fps:
        argv.extend(["-r", args.fps, "-fps_mode", "cfr"])
    argv.append(str(dst))
    return argv


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, PipOverlayArgs)
    ctx.workspace.resolve_output(args.dst)


__all__ = ["PipInput", "PipOverlayArgs", "build_argv", "prepare"]
