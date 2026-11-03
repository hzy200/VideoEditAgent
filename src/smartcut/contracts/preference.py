"""编辑偏好记忆：从用户显式编辑中聚合出的节奏约束。

约束：
- 只保留结构化统计（片段时长、保留率、标签），不存原始媒体或路径；
- 样本不足时不注入 Planner，避免用一两次操作误导规划；
- 素材更换后由服务层决定是否继续使用（当前按项目维度保留）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId


class PreferenceStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_size: int = Field(default=0, ge=0)
    segment_count: int = Field(default=0, ge=0)
    avg_segment_seconds: float = Field(default=0.0, ge=0.0)
    median_segment_seconds: float = Field(default=0.0, ge=0.0)
    max_segment_seconds: float = Field(default=0.0, ge=0.0)
    keep_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    top_tags: list[str] = Field(default_factory=list)


class EditPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: SafeId
    source_hash: str | None = None
    stats: PreferenceStats
    notes: list[str] = Field(default_factory=list)
    updated_at: str | None = None
