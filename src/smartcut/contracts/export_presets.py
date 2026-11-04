"""平台导出预设：只描述输出画幅，不包含时间线语义。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExportPreset:
    name: str
    width: int | None
    height: int | None
    description: str


EXPORT_PRESETS: dict[str, ExportPreset] = {
    "mp4_h264": ExportPreset(
        "mp4_h264", None, None, "mp4_h264（通用 H.264，保持源尺寸）"
    ),
    "reels": ExportPreset(
        "reels", 1080, 1920, "reels（竖版 1080x1920）"
    ),
    "douyin": ExportPreset(
        "douyin", 1080, 1920, "douyin（竖版 1080x1920）"
    ),
    "youtube": ExportPreset(
        "youtube", 1920, 1080, "youtube（横版 1920x1080）"
    ),
    "custom": ExportPreset(
        "custom", None, None, "custom（自定义，保持源尺寸）"
    ),
}


def preset_target(name: str) -> tuple[int, int] | None:
    preset = EXPORT_PRESETS.get(name)
    if preset is None or preset.width is None or preset.height is None:
        return None
    return preset.width, preset.height
