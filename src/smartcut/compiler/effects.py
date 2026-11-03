"""特效校验与 ffmpeg 滤镜生成（纯函数）。

模型只输出枚举与数值；滤镜字符串只在本模块生成，Runner 再做一次白名单校验。
"""

from __future__ import annotations

from smartcut.contracts.effects import (
    BlurEffect,
    ColorEffect,
    Effect,
    FadeEffect,
    SharpenEffect,
    VignetteEffect,
    ZoomEffect,
    enabled_output_effects,
    enabled_segment_effects,
)
from smartcut.contracts.time import RationalTime, fps_from_string
from smartcut.errors import UnsupportedFeature


def effect_signature(effects: list[Effect]) -> list[dict]:
    """稳定的缓存/预览键片段。"""
    return [
        effect.model_dump(mode="json")
        for effect in enabled_output_effects(effects)
    ]


def _fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _color_filter(effect: ColorEffect) -> list[str]:
    filters: list[str] = []
    defaults = (
        effect.brightness == 0.0
        and effect.contrast == 1.0
        and effect.saturation == 1.0
        and effect.gamma == 1.0
    )
    if not defaults:
        filters.append(
            "eq=brightness={}:contrast={}:saturation={}:gamma={}".format(
                _fmt(effect.brightness),
                _fmt(effect.contrast),
                _fmt(effect.saturation),
                _fmt(effect.gamma),
            )
        )
    if effect.temperature:
        # 中性 6500K；±100 映射到约 3500K–9500K。
        kelvin = 6500 + int(effect.temperature) * 30
        filters.append(f"colortemperature=temperature={kelvin}")
    return filters


def _zoom_filter(
    effect: ZoomEffect,
    *,
    fps: str | None,
    width: int,
    height: int,
    total_duration: RationalTime,
) -> list[str]:
    if effect.start_scale == 1.0 and effect.end_scale == 1.0:
        return []
    if not fps:
        raise UnsupportedFeature("zoom 特效需要素材帧率，当前无法确定")
    try:
        fps_fraction = fps_from_string(fps)
    except ValueError as exc:
        raise UnsupportedFeature(f"zoom 特效的帧率不可用: {fps!r}") from exc
    frames = max(1, int(total_duration.as_fraction() * fps_fraction))
    expression = (
        f"{_fmt(effect.start_scale)}+"
        f"({_fmt(effect.end_scale)}-{_fmt(effect.start_scale)})*on/{frames}"
    )
    return [
        "zoompan="
        f"z={expression}:"
        "x=iw/2-(iw/zoom/2):"
        "y=ih/2-(ih/zoom/2):"
        f"d=1:s={width}x{height}:fps={fps}"
    ]


def _fade_filters(
    effect: FadeEffect,
    *,
    total_duration: RationalTime,
) -> list[str]:
    filters: list[str] = []
    if effect.fade_in.as_fraction() > 0:
        filters.append(
            f"fade=t=in:st=0:d={effect.fade_in.to_ffmpeg_seconds()}"
        )
    if effect.fade_out.as_fraction() > 0:
        start = total_duration - effect.fade_out
        filters.append(
            f"fade=t=out:st={start.to_ffmpeg_seconds()}:"
            f"d={effect.fade_out.to_ffmpeg_seconds()}"
        )
    return filters


def build_output_filters(
    effects: list[Effect],
    *,
    fps: str | None,
    width: int,
    height: int,
    total_duration: RationalTime,
) -> tuple[list[str], list[str]]:
    """返回 (fit 之前, fit 之后) 两段滤镜。

    顺序：color → blur → sharpen → vignette → [fit/scale] → zoom → fade → 字幕。
    """
    pre: list[str] = []
    post: list[str] = []
    by_type = {effect.type: effect for effect in enabled_output_effects(effects)}
    for kind in ("color", "blur", "sharpen", "vignette"):
        effect = by_type.get(kind)
        if effect is None:
            continue
        if isinstance(effect, ColorEffect):
            pre.extend(_color_filter(effect))
        elif isinstance(effect, BlurEffect):
            if effect.radius > 0:
                pre.append(f"gblur=sigma={_fmt(effect.radius)}")
        elif isinstance(effect, SharpenEffect):
            if effect.amount > 0:
                pre.append(f"unsharp=5:5:{_fmt(effect.amount)}")
        elif isinstance(effect, VignetteEffect):
            if effect.angle > 0:
                pre.append(f"vignette=angle={_fmt(effect.angle)}")
    zoom = by_type.get("zoom")
    if isinstance(zoom, ZoomEffect):
        post.extend(
            _zoom_filter(
                zoom,
                fps=fps,
                width=width,
                height=height,
                total_duration=total_duration,
            )
        )
    fade = by_type.get("fade")
    if isinstance(fade, FadeEffect):
        post.extend(_fade_filters(fade, total_duration=total_duration))
    return pre, post


def build_segment_filters(
    effects: list[Effect],
    *,
    segment_id: str | None,
    duration: RationalTime,
    fps: str | None,
    width: int,
    height: int,
) -> list[str]:
    """逐片段滤镜；在该片段的 cut 阶段应用，不进入全局 postprocess。"""
    if not segment_id:
        return []
    selected = [
        effect
        for effect in enabled_segment_effects(effects)
        if effect.segment_id == segment_id
    ]
    by_type = {effect.type: effect for effect in selected}
    filters: list[str] = []
    for kind in ("color", "blur", "sharpen", "vignette"):
        effect = by_type.get(kind)
        if isinstance(effect, ColorEffect):
            filters.extend(_color_filter(effect))
        elif isinstance(effect, BlurEffect):
            if effect.radius > 0:
                filters.append(f"gblur=sigma={_fmt(effect.radius)}")
        elif isinstance(effect, SharpenEffect):
            if effect.amount > 0:
                filters.append(f"unsharp=5:5:{_fmt(effect.amount)}")
        elif isinstance(effect, VignetteEffect):
            if effect.angle > 0:
                filters.append(f"vignette=angle={_fmt(effect.angle)}")
    zoom = by_type.get("zoom")
    if isinstance(zoom, ZoomEffect):
        filters.extend(
            _zoom_filter(
                zoom,
                fps=fps,
                width=width,
                height=height,
                total_duration=duration,
            )
        )
    fade = by_type.get("fade")
    if isinstance(fade, FadeEffect):
        filters.extend(_fade_filters(fade, total_duration=duration))
    return filters
