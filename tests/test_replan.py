from __future__ import annotations

from pathlib import Path
from typing import Any

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.workspace import Workspace
from smartcut.orchestrator.agents.replan import ReplanAgent
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import OrchestratorTask


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _ir(revision: int = 2) -> EditIR:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
        ),
    )
    return EditIR(
        project_id="p_replan",
        revision=revision,
        media_facts=facts,
        sequence=[
            TrimOp(src=facts.src, range=TimeRange(start=_rt("0"), end=_rt("2")))
        ],
    )


class FakeReplanToolkit:
    planner_available = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reviews: list[dict] = []

    def replan(
        self,
        project_id: str,
        revision_no: int,
        goal: str,
        review: dict | None = None,
        **_: Any,
    ) -> dict:
        self.reviews.append(review or {})
        if self.fail:
            raise RuntimeError("planner down")
        return {"revision": revision_no + 1, "ir": _ir(revision_no + 1).dump_ir()}


def _ctx(tmp_path: Path, toolkit: Any, *, score: float = 0.3) -> AgentContext:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_root=root, deepseek_api_key="test")
    ctx = AgentContext(
        run_id="run_replan",
        project_id="p_replan",
        goal="fix pacing",
        toolkit=toolkit,
        workspace=Workspace(root),
        settings=settings,
    )
    ctx.set_plan(2, _ir())
    ctx.set_review(
        {
            "score": score,
            "summary": "节奏偏慢",
            "issues": [
                {
                    "severity": "warning",
                    "message": "片段过长",
                    "segment_index": 0,
                    "suggestion": "缩短",
                }
            ],
        }
    )
    return ctx


def test_replan_agent_submits_new_revision(tmp_path: Path) -> None:
    toolkit = FakeReplanToolkit()
    ctx = _ctx(tmp_path, toolkit)

    result = ReplanAgent().run(OrchestratorTask(id="replan", agent="replan"), ctx)

    assert result.output["changed"] is True
    assert result.output["previous_revision"] == 2
    assert result.output["revision"] == 3
    assert ctx.shared.revision == 3
    assert ctx.shared.ir is not None and ctx.shared.ir.revision == 3
    assert toolkit.reviews[0]["issues"][0]["message"] == "片段过长"


def test_replan_agent_skips_high_score(tmp_path: Path) -> None:
    toolkit = FakeReplanToolkit()
    ctx = _ctx(tmp_path, toolkit, score=0.9)

    result = ReplanAgent().run(OrchestratorTask(id="replan", agent="replan"), ctx)

    assert result.output["changed"] is False
    assert result.output["reason"] == "score_ok"
    assert ctx.shared.revision == 2
    assert toolkit.reviews == []


def test_replan_agent_degrades_on_failure(tmp_path: Path) -> None:
    toolkit = FakeReplanToolkit(fail=True)
    ctx = _ctx(tmp_path, toolkit)

    result = ReplanAgent().run(OrchestratorTask(id="replan", agent="replan"), ctx)

    assert result.output["changed"] is False
    assert result.output["reason"] == "failed"
    assert ctx.shared.revision == 2
    assert result.warnings
