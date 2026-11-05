"""自然语言媒体意图解析（纯函数，无 I/O）。

规则优先覆盖常见中文/英文说法；输出只含枚举与数值，不含路径、命令或
ffmpeg 参数。无法识别时返回 None，由调用方决定是否再请 LLM 或回退。
"""

from __future__ import annotations

import re

from smartcut.contracts.media_ops import MediaGoalDraft

_CONTAINERS = ("mp4", "mov", "mkv", "mp3", "m4a", "wav", "flac")
_VIDEO_CODECS = {
    "h264": "libx264",
    "h.264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "h.265": "libx265",
    "av1": "libsvtav1",
    "prores": "prores_ks",
}
_AUDIO_CODECS = {
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "flac": "flac",
    "pcm": "pcm_s16le",
}

_REPAIR_WORDS = (
    "修复",
    "修一下",
    "修好",
    "打不开",
    "无法播放",
    "不能播放",
    "播放不了",
    "损坏",
    "坏了",
    "卡顿",
    "画面卡",
    "音画不同步",
    "不同步",
    "对不上",
    "不对应",
    "音画不对",
    "偏移",
    "模糊",
    "糊",
    "发虚",
    "repair",
    "fix",
    "unplayable",
    "won't open",
    "wont open",
    "stutter",
    "lag",
    "freeze",
    "blurry",
    "out of sync",
    "desync",
)
_SYNC_WORDS = (
    "不同步",
    "对不上",
    "不对应",
    "音画不对",
    "偏移",
    "out of sync",
    "desync",
    "sync",
)
_STUTTER_WORDS = (
    "卡顿",
    "画面卡",
    "冻结",
    "静止",
    "stutter",
    "lag",
    "frozen",
    "vfr",
)
_BLUR_WORDS = ("模糊", "糊", "发虚", "blurry")
_UNOPENABLE_WORDS = (
    "打不开",
    "无法播放",
    "不能播放",
    "播放不了",
    "损坏",
    "坏了",
    "unplayable",
    "won't open",
    "wont open",
)
_CONVERT_WORDS = (
    "转换",
    "转格式",
    "转成",
    "转为",
    "转码",
    "封装",
    "remux",
    "convert",
    "transcode",
    "格式转换",
)
_COMPRESS_WORDS = (
    "压缩",
    "压到",
    "变小",
    "减小",
    "体积",
    "小一点",
    "码率低",
    "compress",
    "smaller",
    "reduce size",
)
_DIAGNOSE_WORDS = ("诊断", "检测", "检查", "diagnose", "check", "inspect")
_EXTRACT_AUDIO_WORDS = (
    "提取音频",
    "导出音频",
    "分离音频",
    "只要音频",
    "extract audio",
    "audio only",
)

_CONTAINER_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(_CONTAINERS) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
_RESOLUTION_RE = re.compile(r"(?<!\d)(\d{3,4})\s*[pP](?![a-z])")
_CRF_RE = re.compile(r"crf\s*[=:：]?\s*(\d{1,2})", re.IGNORECASE)


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _container(text: str) -> str | None:
    match = _CONTAINER_RE.search(text)
    return match.group(1).lower() if match else None


def _codec(text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    video = next(
        (value for key, value in _VIDEO_CODECS.items() if key in lowered),
        None,
    )
    audio = next(
        (value for key, value in _AUDIO_CODECS.items() if key in lowered),
        None,
    )
    return video, audio


def _max_height(text: str) -> int | None:
    match = _RESOLUTION_RE.search(text)
    if not match:
        return None
    value = int(match.group(1))
    return value if value in {360, 480, 720, 1080, 1440, 2160} else None


def _crf(text: str) -> int | None:
    match = _CRF_RE.search(text)
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 63 else None


def _strategy(text: str) -> str:
    if _has(text, _SYNC_WORDS):
        return "audio_sync"
    if _has(text, _STUTTER_WORDS):
        return "reencode"
    if _has(text, _BLUR_WORDS):
        return "sharpen"
    if _has(text, _UNOPENABLE_WORDS):
        return "remux"
    return "auto"


def parse_media_goal(text: str) -> MediaGoalDraft | None:
    """把用户提示解析为媒体操作草稿；无明确媒体意图时返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    container = _container(lowered)
    video_codec, audio_codec = _codec(lowered)
    max_height = _max_height(lowered)
    crf = _crf(lowered)
    strategy = _strategy(lowered)

    extract_audio = _has(lowered, _EXTRACT_AUDIO_WORDS)
    repair_words = _has(lowered, _REPAIR_WORDS)
    compress_words = _has(lowered, _COMPRESS_WORDS)
    convert_words = _has(lowered, _CONVERT_WORDS)
    diagnose_words = _has(lowered, _DIAGNOSE_WORDS)

    if extract_audio:
        operation = "convert"
        container = container or "m4a"
        confidence = 0.85
    elif repair_words:
        operation = "repair"
        confidence = 0.9 if (container or strategy != "auto") else 0.65
    elif compress_words:
        operation = "compress"
        confidence = 0.9 if (max_height or crf) else 0.7
    elif convert_words or (container and (video_codec or audio_codec)):
        operation = "convert"
        confidence = 0.85 if container else 0.6
    elif diagnose_words:
        operation = "diagnose"
        confidence = 0.6
    else:
        return None

    return MediaGoalDraft(
        operation=operation,  # type: ignore[arg-type]
        strategy=strategy,  # type: ignore[arg-type]
        target_container=container,  # type: ignore[arg-type]
        target_video_codec=video_codec,  # type: ignore[arg-type]
        target_audio_codec=audio_codec,  # type: ignore[arg-type]
        crf=crf,
        max_height=max_height,
        confidence=confidence,
        reason=raw[:200],
    )


__all__ = ["parse_media_goal"]
