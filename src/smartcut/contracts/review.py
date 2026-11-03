"""语义审片契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "error"] = "warning"
    message: str = Field(min_length=1, max_length=500)
    segment_index: int | None = Field(default=None, ge=0)
    suggestion: str | None = Field(default=None, max_length=500)


class EditReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(default=0.7, ge=0.0, le=1.0)
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)


class RenderReview(BaseModel):
    """成片审片结果：只报告问题，不修改 EditIR。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    output_path: str
    score: float = Field(default=0.7, ge=0.0, le=1.0)
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    visual_summary: str = ""
    source: Literal["llm", "rules", "hybrid"] = "rules"
    image_count: int = Field(default=0, ge=0)
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
