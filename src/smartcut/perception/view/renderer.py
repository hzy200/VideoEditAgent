"""证据渲染器协议：只接收 MeasureResult，不访问源素材或时间线。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from smartcut.contracts.visual_evidence import (
    MeasureResult,
    RenderedImage,
    ViewRequest,
)


@runtime_checkable
class EvidenceRenderer(Protocol):
    backend: str

    def render(
        self,
        measure: MeasureResult,
        request: ViewRequest,
    ) -> RenderedImage: ...

    def render_many(
        self,
        measure: MeasureResult,
        requests: list[ViewRequest],
    ) -> list[RenderedImage]: ...
