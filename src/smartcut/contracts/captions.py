"""字幕 cue 契约（时间已映射到输出时间线）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from smartcut.contracts.time import RationalTime


class CaptionCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: RationalTime
    end: RationalTime
    text: str

    @model_validator(mode="after")
    def _ordered(self) -> "CaptionCue":
        if self.end <= self.start:
            raise ValueError("caption end 必须大于 start")
        if not self.text.strip():
            raise ValueError("caption 文本不能为空")
        return self
