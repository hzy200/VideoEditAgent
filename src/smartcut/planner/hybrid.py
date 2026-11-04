"""混合规划器：LLM 可用则透传，否则退化为确定性规则规划。

对外协议与 ``DeepSeekPlanner`` 一致，且 ``available`` 恒为 True；
因此 Orchestrator 的 ``when: "plan"/"render"`` 任务在无 Key 时也会执行。
"""

from __future__ import annotations

from typing import Any

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.review import EditReview
from smartcut.llm.client import TextCache
from smartcut.planner.client import DeepSeekPlanner
from smartcut.planner.rules import RulePlanner
from smartcut.resources import ResourceManager


class HybridPlanner:
    def __init__(
        self,
        settings: Settings,
        *,
        cache: TextCache | None = None,
        resources: ResourceManager | None = None,
        llm: DeepSeekPlanner | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm or DeepSeekPlanner(
            settings,
            cache=cache,
            resources=resources,
        )
        self.rules = RulePlanner(settings)

    @property
    def available(self) -> bool:
        # 离线规则规划始终可用；Key 只决定是否使用 LLM。
        return True

    @property
    def llm_available(self) -> bool:
        return bool(self.llm.available)

    def close(self) -> None:
        self.llm.close()

    def usage_snapshot(self) -> dict[str, int]:
        return self.llm.usage_snapshot()

    def usage_for(self, scope: str | None) -> dict[str, int]:
        return self.llm.usage_for(scope)

    def propose(
        self,
        facts: MediaFacts,
        transcript: Any | None,
        goal: str,
        **kwargs: Any,
    ) -> EditIR:
        if self.llm.available:
            return self.llm.propose(facts, transcript, goal, **kwargs)
        return self.rules.propose(facts, transcript, goal, **kwargs)

    def replan(
        self,
        ir: EditIR,
        transcript: Any | None,
        goal: str,
        review: dict | None,
        **kwargs: Any,
    ) -> EditIR:
        if self.llm.available:
            return self.llm.replan(ir, transcript, goal, review, **kwargs)
        return self.rules.replan(ir, transcript, goal, review, **kwargs)

    def review(
        self,
        ir: EditIR,
        transcript: Any | None,
        goal: str,
        **kwargs: Any,
    ) -> EditReview:
        if self.llm.available:
            return self.llm.review(ir, transcript, goal, **kwargs)
        return self.rules.review(ir, transcript, goal, **kwargs)
