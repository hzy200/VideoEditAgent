"""工具超时估算。

超时同时受两个因素约束：
- 配置的默认值（`tool_timeout_seconds` / `mix_timeout_seconds`）是下限；
- `render_timeout_seconds` 是渲染类工具的上限。

估算只用于避免长视频在固定 120s 处被误杀；不参与任何时间轴运算。
"""

from __future__ import annotations

from smartcut.config import Settings

# floor 用工具自身默认值；render_timeout_seconds 只作为缩放上限。
_TIMEOUT_FLOORS = {
    "mix_audio": "mix_timeout_seconds",
    "compose": "tool_timeout_seconds",
}

# 每 1 秒媒体时间对应的额外渲染秒数（1080p 基准，保守估计）。
_SCALED_TOOLS = {
    "cut": 2.0,
    "postprocess": 3.0,
    "extract_frames": 1.5,
    "extract_audio": 0.5,
    "concat": 0.5,
    "compose": 2.0,
    "mix_audio": 1.0,
    "proxy": 2.0,
    "transcode": 3.0,
    "loudnorm_measure": 0.5,
}


def tool_default_timeout(tool: str, settings: Settings) -> float:
    attr = _TIMEOUT_FLOORS.get(tool, "tool_timeout_seconds")
    return float(getattr(settings, attr))


def compute_tool_timeout(
    tool: str,
    *,
    duration_seconds: float = 0.0,
    width: int | None = None,
    height: int | None = None,
    settings: Settings,
) -> float:
    """按工具类型、媒体时长和分辨率估算超时，并限制在配置上限内。"""
    floor = tool_default_timeout(tool, settings)
    cap = max(floor, float(settings.render_timeout_seconds))
    factor = _SCALED_TOOLS.get(tool)
    if factor is None:
        return floor

    pixels = max(1, (width or 1920) * (height or 1080))
    pixel_scale = max(0.5, min(4.0, pixels / (1920 * 1080)))
    seconds = max(0.0, float(duration_seconds or 0.0))
    estimate = floor + seconds * pixel_scale * factor
    return max(floor, min(cap, round(estimate, 3)))
