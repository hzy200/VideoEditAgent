from __future__ import annotations

from typing import Any, cast

from smartcut.config import Settings
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.orchestrator.agents.analyze import AnalyzeAgent
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import OrchestratorTask
from smartcut.runner.paths import Workspace


class _Toolkit:
    planner_available = True

    def __init__(self, facts: MediaFacts) -> None:
        self.facts = facts

    def get_media_facts(self, project_id: str) -> MediaFacts:
        return self.facts


def test_analyze_warns_for_vfr_rotation_and_hdr(tmp_path) -> None:
    facts = MediaFacts(
        src="media/source.mp4",
        duration=RationalTime.from_seconds("10", rate=90_000),
        fps="30000/1001",
        vfr=True,
        rotation=90,
        color_transfer="smpte2084",
        video=StreamFacts(
            codec_type="video",
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            avg_frame_rate="30000/1001",
            r_frame_rate="30/1",
        ),
        audio=StreamFacts(codec_type="audio", channels=2),
    )
    ctx = AgentContext(
        run_id="run_analyze",
        project_id="p_analyze",
        goal="warnings",
        toolkit=cast(Any, _Toolkit(facts)),
        workspace=Workspace(tmp_path / "ws"),
        settings=Settings(workspace_root=tmp_path / "ws"),
    )
    result = AnalyzeAgent().run(
        OrchestratorTask(id="analyze", agent="analyze"),
        ctx,
    )
    warnings = " ".join(result.warnings)
    assert "VFR" in warnings
    assert "rotation=90" in warnings
    assert "smpte2084" in warnings
    assert ctx.shared.facts is facts
