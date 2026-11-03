"""子智能体协议。所有智能体必须无状态、线程安全、可并行执行。"""

from __future__ import annotations

from typing import Protocol

from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import AgentResult, OrchestratorTask


class SubAgent(Protocol):
    name: str
    description: str

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult: ...
