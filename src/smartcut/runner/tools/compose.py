"""转场合成：xfade / acrossfade 多输入 filter_complex。

输入是已按顺序切好的片段；相邻片段之间可以没有转场（concat），
也可以有重叠转场（xfade/acrossfade）。输出时长 = 片段时长和 - 转场重叠和。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args
from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext


class ComposeTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal[
        "fade",
        "fadeblack",
        "fadewhite",
        "wipeleft",
        "wiperight",
        "slideleft",
        "slideright",
        "circleopen",
        "dissolve",
    ] = "fade"
    duration: RationalTime


class ComposeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: list[str] = Field(min_length=2)
    durations: list[RationalTime]
    transitions: list[ComposeTransition]
    dst: str
    has_audio: bool = True
    fps: str | None = None
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    video_track_timescale: int = Field(default=90_000, gt=0)
    fps_mode: Literal["cfr", "vfr", "passthrough"] = "passthrough"
    encoder: EncoderProfile | None = None

    @model_validator(mode="after")
    def _shape(self) -> "ComposeArgs":
        if len(self.durations) != len(self.inputs):
            raise ValueError("durations 必须与 inputs 数量一致")
        if len(self.transitions) != len(self.inputs) - 1:
            raise ValueError("transitions 数量必须为 inputs - 1")
        return self


def _to_seconds(value: Fraction) -> str:
    return RationalTime.from_fraction(value).to_ffmpeg_seconds()


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, ComposeArgs)
    inputs = [ctx.workspace.resolve_input(item) for item in args.inputs]
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs=set(inputs))

    argv = [ctx.settings.ffmpeg_bin, "-hide_banner", "-y"]
    for path in inputs:
        argv.extend(["-i", str(path)])

    parts: list[str] = []
    video_label = "v0"
    video_chain = "setpts=PTS-STARTPTS"
    if args.fps:
        video_chain += f",fps={args.fps}"
    parts.append(f"[0:v]{video_chain}[v0]")
    # cut 产物已从 0 开始；额外 asetpts 会与 xfade 组合时产生空音频，
    # 因此音频直接引用输入流。
    audio_label = "0:a"

    cumulative = args.durations[0].as_fraction()
    for index in range(1, len(inputs)):
        transition = args.transitions[index - 1]
        overlap = transition.duration.as_fraction()
        next_video = f"v{index}"
        index_chain = "setpts=PTS-STARTPTS"
        if args.fps:
            index_chain += f",fps={args.fps}"
        parts.append(f"[{index}:v]{index_chain}[vsrc{index}]")
        if overlap > 0:
            offset = cumulative - overlap
            if offset < 0:
                raise ValueError("转场重叠超过前序输出时长")
            parts.append(
                f"[{video_label}][vsrc{index}]"
                f"xfade=transition={transition.type}:"
                f"duration={_to_seconds(overlap)}:offset={_to_seconds(offset)}"
                f"[{next_video}]"
            )
        else:
            parts.append(
                f"[{video_label}][vsrc{index}]concat=n=2:v=1:a=0[{next_video}]"
            )
        video_label = next_video

        if args.has_audio:
            next_audio = f"a{index}"
            if overlap > 0:
                parts.append(
                    f"[{audio_label}][{index}:a]"
                    f"acrossfade=d={_to_seconds(overlap)}:c1=tri:c2=tri"
                    f"[{next_audio}]"
                )
            else:
                parts.append(
                    f"[{audio_label}][{index}:a]concat=n=2:v=0:a=1[{next_audio}]"
                )
            audio_label = next_audio

        cumulative += args.durations[index].as_fraction() - overlap

    argv.extend(["-filter_complex", ";".join(parts), "-map", f"[{video_label}]"])
    if args.has_audio:
        argv.extend(["-map", f"[{audio_label}]"])

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
    if args.fps_mode != "passthrough":
        argv.extend(["-fps_mode", args.fps_mode])
    argv.extend(
        [
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            str(args.video_track_timescale),
        ]
    )
    if args.has_audio:
        argv.extend(["-c:a", args.audio_codec, "-b:a", args.audio_bitrate])
    argv.extend(["-movflags", "+faststart", str(dst)])
    return argv
