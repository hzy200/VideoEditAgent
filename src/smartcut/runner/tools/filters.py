"""视频滤镜参数的边界校验。

Compiler 只生成白名单滤镜；Runner 在工具边界再校验一次，防止其他调用方注入
任意 filtergraph（``;`` / ``|`` / 中括号 / 换行等）。
"""

from __future__ import annotations

import re

_FILTER_RE = re.compile(
    r"^(eq|colortemperature|gblur|unsharp|vignette|zoompan|fade|scale)="
    r"[A-Za-z0-9_=:.,+\-*/()]+$"
)
_MAX_FILTERS = 32


def validate_video_filters(filters: list[str] | None) -> list[str]:
    values = filters or []
    if len(values) > _MAX_FILTERS:
        raise ValueError(f"视频滤镜最多 {_MAX_FILTERS} 个")
    for item in values:
        if not isinstance(item, str) or not _FILTER_RE.fullmatch(item):
            raise ValueError(f"不允许的视频滤镜: {item!r}")
    return values
