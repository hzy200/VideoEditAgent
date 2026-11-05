"""绝对输出时间线合成：黑底画布 + 多轨 clip 绝对定位 + 音频混音。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.base import ToolContext


class TimelineKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    t: RationalTime
    x: float
    y: float
    width: float
    height: float
    opacity: float


class TimelineTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    after: str
    type: str = "fade"
    duration: RationalTime
    audio_curve: str = "tri"


class TimelineClipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    src: str
    timeline_start: RationalTime
    duration: RationalTime
    track: int = Field(default=0, ge=0, le=15)
    x: float = Field(default=0.0, ge=0.0, le=1.0)
    y: float = Field(default=0.0, ge=0.0, le=1.0)
    width: float = Field(default=1.0, gt=0.0, le=1.0)
    height: float = Field(default=1.0, gt=0.0, le=1.0)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    keyframes: list[TimelineKeyframe] = Field(default_factory=list)
    has_audio: bool = True


class TimelineComposeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clips: list[TimelineClipInput] = Field(min_length=1)
    transitions: list[TimelineTransition] = Field(default_factory=list)
    base: str | None = None
    dst: str
    width: int = Field(ge=2, le=16384)
    height: int = Field(ge=2, le=16384)
    total_duration: RationalTime
    fps: str | None = None
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"


def _ffmpeg_time(value: RationalTime) -> float:
    return float(value.to_ffmpeg_seconds())


def _linear(points: list[tuple[float, float]], var: str = "t") -> str | None:
    cleaned = sorted(
        {round(time, 6): value for time, value in points}.items()
    )
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return f"{cleaned[0][1]:.6f}"
    expression = f"{cleaned[-1][1]:.6f}"
    for index in range(len(cleaned) - 2, -1, -1):
        t0, v0 = cleaned[index]
        t1, v1 = cleaned[index + 1]
        span = max(1e-6, t1 - t0)
        expression = (
            f"if(lt({var},{t1:.6f}),"
            f"{v0:.6f}+({v1:.6f}-{v0:.6f})*({var}-{t0:.6f})/{span:.6f},"
            f"{expression})"
        )
    t0, v0 = cleaned[0]
    return f"if(lt({var},{t0:.6f}),{v0:.6f},{expression})"


def _clip_keyframe_points(
    clip: "TimelineClipInput", args: "TimelineComposeArgs"
) -> dict[str, list[tuple[float, float]]] | None:
    if not clip.keyframes:
        return None
    start = _ffmpeg_time(clip.timeline_start)
    duration = _ffmpeg_time(clip.duration)
    points = {
        "x": [(start, clip.x * args.width)],
        "y": [(start, clip.y * args.height)],
        "w": [(start, clip.width * args.width)],
        "h": [(start, clip.height * args.height)],
        "a": [(start, clip.opacity * 255.0)],
    }
    for keyframe in sorted(
        clip.keyframes, key=lambda item: _ffmpeg_time(item.t)
    ):
        time = start + min(duration, _ffmpeg_time(keyframe.t))
        points["x"].append((time, keyframe.x * args.width))
        points["y"].append((time, keyframe.y * args.height))
        points["w"].append((time, keyframe.width * args.width))
        points["h"].append((time, keyframe.height * args.height))
        points["a"].append((time, keyframe.opacity * 255.0))
    last = {key: values[-1][1] for key, values in points.items()}
    end = start + duration
    for key, value in last.items():
        points[key].append((end, value))
    return points


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, TimelineComposeArgs)
    clips = sorted(
        args.clips,
        key=lambda item: (item.track, _ffmpeg_time(item.timeline_start)),
    )
    srcs = [ctx.workspace.resolve_input(item.src) for item in clips]
    base_path = ctx.workspace.resolve_input(args.base) if args.base else None
    reserved = set(srcs) | ({base_path} if base_path else set())
    dst = ctx.workspace.resolve_output(args.dst, reserved_inputs=reserved)

    argv = [ctx.settings.ffmpeg_bin, "-hide_banner", "-y"]
    if base_path is not None:
        argv.extend(["-i", str(base_path)])
    for path in srcs:
        argv.extend(["-i", str(path)])
    offset = 1 if base_path is not None else 0

    if base_path is not None:
        parts = ["[0:v]setpts=PTS-STARTPTS[base]"]
    else:
        parts = [
            f"color=c=black:s={args.width}x{args.height}:r={args.fps or 30}:"
            f"d={_ffmpeg_time(args.total_duration)}[base]"
        ]
    track_sequences: dict[int, list[int]] = {}
    for index, clip in enumerate(clips):
        track_sequences.setdefault(clip.track, []).append(index)
    after_map = {item.after: item for item in args.transitions}
    current = "base"
    for index, clip in enumerate(clips):
        start = _ffmpeg_time(clip.timeline_start)
        end = start + _ffmpeg_time(clip.duration)
        input_index = index + offset
        points = _clip_keyframe_points(clip, args)
        position = track_sequences[clip.track].index(index)
        previous_id = (
            clips[track_sequences[clip.track][position - 1]].id
            if position > 0
            else ""
        )
        next_id = (
            clips[track_sequences[clip.track][position + 1]].id
            if position + 1 < len(track_sequences[clip.track])
            else ""
        )
        fade_filters = ""
        if previous_id and previous_id in after_map:
            fade = after_map[previous_id]
            duration = min(
                _ffmpeg_time(fade.duration), max(0.05, end - start)
            )
            fade_filters += (
                f",fade=t=in:st={start}:d={duration}:alpha=1"
            )
        if clip.id and clip.id in after_map and next_id:
            fade = after_map[clip.id]
            duration = min(
                _ffmpeg_time(fade.duration), max(0.05, end - start)
            )
            fade_filters += (
                f",fade=t=out:st={end - duration}:d={duration}:alpha=1"
            )
        if points is not None:
            width_expr = _linear(points["w"]) or str(args.width)
            height_expr = _linear(points["h"]) or str(args.height)
            alpha_expr = _linear(points["a"], "T") or str(
                int(clip.opacity * 255)
            )
            parts.append(
                f"[{input_index}:v]setpts=PTS-STARTPTS+{start}/TB,"
                f"scale=w='{width_expr}':h='{height_expr}':eval=frame,"
                f"setsar=1,format=rgba,"
                f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expr}'"
                f"{fade_filters}[c{index}]"
            )
            x_expr = _linear(points["x"]) or str(int(args.width * clip.x))
            y_expr = _linear(points["y"]) or str(int(args.height * clip.y))
            x_arg = f"x='{x_expr}'"
            y_arg = f"y='{y_expr}'"
        else:
            width = max(2, int(round(args.width * clip.width)))
            height = max(2, int(round(args.height * clip.height)))
            x = max(0, int(round(args.width * clip.x)))
            y = max(0, int(round(args.height * clip.y)))
            parts.append(
                f"[{input_index}:v]setpts=PTS-STARTPTS+{start}/TB,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"format=rgba,colorchannelmixer=aa={clip.opacity:.3f}"
                f"{fade_filters}[c{index}]"
            )
            x_arg = f"x={x}"
            y_arg = f"y={y}"
        next_label = f"base{index}"
        parts.append(
            f"[{current}][c{index}]overlay={x_arg}:{y_arg}:"
            f"enable='between(t,{start},{end})':eof_action=pass[{next_label}]"
        )
        current = next_label

    audio_clips = [clip for clip in clips if clip.has_audio]
    if base_path is not None:
        parts.append(
            f"[0:a]apad,atrim=0:{_ffmpeg_time(args.total_duration)}[b_a]"
        )
        mix_labels = ["[b_a]"]
        for index, clip in enumerate(clips):
            if not clip.has_audio:
                continue
            start_ms = int(round(_ffmpeg_time(clip.timeline_start) * 1000))
            input_index = index + offset
            parts.append(
                f"[{input_index}:a]adelay={start_ms}|{start_ms},apad,"
                f"atrim=0:{_ffmpeg_time(args.total_duration)}[a{index}]"
            )
            mix_labels.append(f"[a{index}]")
        if len(mix_labels) == 1:
            parts.append(
                f"[b_a]atrim=0:{_ffmpeg_time(args.total_duration)}[aout]"
            )
        else:
            parts.append(
                f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:"
                f"duration=longest:dropout_transition=0,"
                f"atrim=0:{_ffmpeg_time(args.total_duration)}[aout]"
            )
        audio_map = "[aout]"
    elif audio_clips:
        for index, clip in enumerate(clips):
            if not clip.has_audio:
                continue
            start_ms = int(round(_ffmpeg_time(clip.timeline_start) * 1000))
            parts.append(
                f"[{index}:a]adelay={start_ms}|{start_ms},apad,"
                f"atrim=0:{_ffmpeg_time(args.total_duration)}[a{index}]"
            )
        labels = "".join(
            f"[a{index}]" for index, clip in enumerate(clips) if clip.has_audio
        )
        parts.append(
            f"{labels}amix=inputs={len(audio_clips)}:"
            f"duration=longest:dropout_transition=0,"
            f"atrim=0:{_ffmpeg_time(args.total_duration)}[aout]"
        )
        audio_map = "[aout]"
    else:
        audio_map = None

    argv.extend(["-filter_complex", ";".join(parts), "-map", f"[{current}]"])
    if audio_map is not None:
        argv.extend(["-map", audio_map])
    argv.extend(
        [
            "-c:v",
            args.video_codec,
            "-preset",
            args.preset,
            "-crf",
            str(args.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    )
    if args.fps:
        argv.extend(["-r", args.fps, "-fps_mode", "cfr"])
    if audio_map is not None:
        argv.extend(["-c:a", args.audio_codec, "-b:a", args.audio_bitrate])
    else:
        argv.append("-an")
    argv.append(str(dst))
    return argv


def prepare(args: BaseModel, ctx: ToolContext) -> None:
    assert isinstance(args, TimelineComposeArgs)
    ctx.workspace.resolve_output(args.dst)


__all__ = [
    "TimelineClipInput",
    "TimelineComposeArgs",
    "build_argv",
    "prepare",
]
