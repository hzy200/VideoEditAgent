"""增量编辑补丁：相对某个 base revision 的结构修改。

约束：
- 补丁只描述 sequence/effects/audio/export 的差异，不携带路径或命令；
- 应用后必须生成**新 revision**，不得原地改写旧 revision；
- index 语义以“当前步骤的 sequence”为准，多个 op 按数组顺序依次应用。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from smartcut.contracts.effects import Effect
from smartcut.contracts.edit_ir import AudioSpec, ExportSpec, TrimOp


class PatchOp(BaseModel):
    """单个结构操作：插入 / 替换 / 删除。"""

    model_config = ConfigDict(extra="forbid")

    op: Literal["insert", "replace", "delete"]
    index: int = Field(ge=0)
    count: int = Field(default=1, ge=1)
    trims: list[TrimOp] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> "PatchOp":
        if self.op == "delete":
            if self.trims:
                raise ValueError("delete 操作不允许携带 trims")
            if self.count < 1:
                raise ValueError("delete 的 count 必须 >= 1")
            return self
        if not self.trims:
            raise ValueError(f"{self.op} 操作必须提供 trims")
        if self.op == "insert" and self.count != 1:
            raise ValueError("insert 的 count 固定为 1")
        return self


class EditPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    ops: list[PatchOp] = Field(min_length=1)
    effects: list[Effect] | None = None
    audio: AudioSpec | None = None
    export: ExportSpec | None = None
    rationale: str = ""
