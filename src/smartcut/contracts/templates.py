"""编辑模板：复用目标与输出选项，不包含具体项目/时间线。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId

Phase = Literal["outline", "fast_outline", "audio_first", "plan"]


class EditTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: SafeId | None = None
    name: str = Field(min_length=1, max_length=200)
    goal: str = Field(default="", max_length=4000)
    allow_render: bool = False
    phase: Phase = "plan"
    aspect: Literal["9:16"] | None = None
    burn_captions: bool = False
    loudness: str | None = None
    remove_filler: bool = False
    notes: str = Field(default="", max_length=2000)
    created_at: str | None = None

    def goal_text(self, extra_goal: str = "") -> str:
        parts = [self.goal.strip()]
        if extra_goal.strip():
            parts.append(extra_goal.strip())
        extras: list[str] = []
        if self.aspect:
            extras.append(f"输出 {self.aspect} 竖版")
        if self.burn_captions:
            extras.append("烧录字幕")
        if self.loudness:
            extras.append(f"响度归一 {self.loudness}")
        if self.remove_filler:
            extras.append("去口癖、删除语气词")
        text = "；".join(item for item in parts if item)
        if extras:
            text = (text + "；要求：" if text else "要求：") + "，".join(extras)
        return text
