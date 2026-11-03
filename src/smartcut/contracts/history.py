"""跨运行决策摘要：把已完成 run 压缩成只读记忆。

约束：
- 只读旧 run 的账本，不修改任何历史记录；
- 只保留摘要（目标、revision、QA 分数、结构修正），不复制完整 ledger；
- 由 Services 按 project_id + 素材创建时间过滤，素材更换后旧经验不再注入。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId


class RunDigest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: SafeId
    goal: str = ""
    phase: str = "plan"
    status: str = ""
    revision: int | None = Field(default=None, ge=0)
    qa_score: float | None = Field(default=None, ge=0.0, le=1.0)
    qa_summary: str | None = None
    changes: list[str] = Field(default_factory=list)
    created_at: str | None = None
