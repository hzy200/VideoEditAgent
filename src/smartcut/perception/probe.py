"""ffprobe JSON → MediaFacts 的纯函数解析。

这里不启动子进程；Runner 负责执行 ffprobe 并把 stdout 交给本模块。
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime, fps_from_string


def _to_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float_text(value: Any) -> str | None:
    if value in (None, "", "N/A"):
        return None
    return str(value)


def _duration(value: Any) -> RationalTime | None:
    text = _to_float_text(value)
    if text is None:
        return None
    try:
        return RationalTime.from_seconds(text, rate=90_000)
    except ValueError:
        return None


def _rotation(stream: dict[str, Any]) -> int:
    for entry in stream.get("side_data_list") or []:
        if "rotation" in entry:
            return int(entry["rotation"])
    tags = stream.get("tags") or {}
    if "rotate" in tags:
        try:
            return int(tags["rotate"])
        except (TypeError, ValueError):
            return 0
    return 0


def _stream_facts(stream: dict[str, Any]) -> StreamFacts:
    return StreamFacts(
        index=_to_int(stream.get("index")) or 0,
        codec_type=str(stream.get("codec_type", "unknown")),
        codec_name=stream.get("codec_name"),
        codec_long_name=stream.get("codec_long_name"),
        width=_to_int(stream.get("width")),
        height=_to_int(stream.get("height")),
        avg_frame_rate=stream.get("avg_frame_rate"),
        r_frame_rate=stream.get("r_frame_rate"),
        pix_fmt=stream.get("pix_fmt"),
        sample_rate=_to_int(stream.get("sample_rate")),
        channels=_to_int(stream.get("channels")),
        channel_layout=stream.get("channel_layout"),
        duration=_duration(stream.get("duration")),
        start_time=_duration(stream.get("start_time")),
        bit_rate=_to_int(stream.get("bit_rate")),
        nb_frames=_to_int(stream.get("nb_frames")),
        rotation=_rotation(stream),
        color_transfer=stream.get("color_transfer"),
        time_base=stream.get("time_base"),
    )


def _pick_fps(video: dict[str, Any] | None) -> tuple[str | None, bool]:
    if not video:
        return None, False
    avg = video.get("avg_frame_rate")
    real = video.get("r_frame_rate")
    candidates = [value for value in (avg, real) if value and value != "0/0"]
    if not candidates:
        return None, False
    fps_text = candidates[0]
    try:
        fps_from_string(fps_text)
    except ValueError:
        return None, False
    vfr = False
    if avg and real and avg != "0/0" and real != "0/0":
        try:
            avg_fraction = fps_from_string(avg)
            real_fraction = fps_from_string(real)
            vfr = abs(avg_fraction - real_fraction) > Fraction(1, 100)
        except ValueError:
            vfr = False
    return fps_text, vfr


def parse_ffprobe(payload: dict[str, Any], src: str) -> MediaFacts:
    """把 ``ffprobe -print_format json`` 的输出转换成契约对象。"""
    streams = payload.get("streams") or []
    format_info = payload.get("format") or {}

    video_raw = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and (stream.get("disposition") or {}).get("attached_pic", 0) == 0
        ),
        None,
    )
    audio_raw = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)

    video = _stream_facts(video_raw) if video_raw else None
    audio = _stream_facts(audio_raw) if audio_raw else None

    container_duration = _duration(format_info.get("duration"))
    if container_duration is None:
        container_duration = video.duration if video else None
    if container_duration is None:
        container_duration = audio.duration if audio else None
    if container_duration is None:
        # 单帧图片没有时长；仍可用于读取宽高（水印资产）。
        if video is not None:
            container_duration = RationalTime(value=0, rate=90_000)
        else:
            raise ValueError(f"{src}: ffprobe 输出缺少可用时长")

    fps, vfr = _pick_fps(video_raw)
    rotation = video.rotation if video else 0

    return MediaFacts(
        src=src,
        format_name=format_info.get("format_name"),
        duration=container_duration,
        size_bytes=_to_int(format_info.get("size")),
        video=video,
        audio=audio,
        fps=fps,
        vfr=vfr,
        rotation=rotation,
        color_transfer=video.color_transfer if video else None,
    )
