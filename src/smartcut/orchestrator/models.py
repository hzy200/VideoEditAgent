"""Orchestrator 任务图与结果契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.ids import SafeId


class OrchestratorTask(BaseModel):
    """任务图节点。agent 必须是已注册的子智能体。"""

    model_config = ConfigDict(extra="forbid")

    id: SafeId
    agent: SafeId
    depends_on: list[str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    max_retries: int = Field(default=0, ge=0, le=3)
    allow_failed_deps: bool = False




class OrchestrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str = ""
    tasks: list[OrchestratorTask] = Field(min_length=1)


class TaskStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class AgentResult(BaseModel):
    """子智能体的标准返回值；可携带动态追加任务。"""

    model_config = ConfigDict(extra="forbid")

    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    new_tasks: list[OrchestratorTask] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class TaskOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    agent: str
    description: str | None = None
    status: TaskStatus
    attempts: int = 1
    queue_wait_ms: int = 0
    duration_ms: int = 0
    retries: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # agent 启动时看到的结构化输入摘要；用于运行目录回放，不含 prompt 明文。
    context_digest: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class OrchestrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    project_id: str
    goal: str
    status: str
    answer: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    plan: OrchestrationPlan
    ledger: list[TaskOutcome] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def dump(self) -> dict:
        return self.model_dump(mode="json")
