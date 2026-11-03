from __future__ import annotations

from pathlib import Path

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.orchestrator.agents.critic import CriticAgent
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.critic import repair_sequence
from smartcut.orchestrator.models import OrchestratorTask
from smartcut.runner.paths import Workspace


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def make_ir(start: str = "0", end: str = "300") -> EditIR:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt("300"),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
        audio=StreamFacts(codec_type="audio", channels=2),
    )
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt(start), end=_rt(end)),
                tag="main",
                confidence=0.8,
            )
        ],
    )


def make_perception() -> tuple[QualityReport, ShotList]:
    quality = QualityReport(
        src="media/a.mp4",
        sampled_fps=1.0,
        marks=[
            QualityMark(t=_rt("100"), kind=QualityKind.black, score=0.95),
            QualityMark(t=_rt("200"), kind=QualityKind.frozen, score=0.9),
        ],
    )
    shots = ShotList(
        src="media/a.mp4",
        shots=[
            ShotBoundary(t=_rt("50"), confidence=0.9),
            ShotBoundary(t=_rt("150"), confidence=0.9),
            ShotBoundary(t=_rt("250"), confidence=0.9),
        ],
    )
    return quality, shots


def test_repair_removes_bad_interval_and_splits_long_segments() -> None:
    ir = make_ir()
    quality, shots = make_perception()

    report = repair_sequence(
        ir,
        quality,
        shots,
        max_segment_seconds=90.0,
        padding=0.5,
    )

    assert report.changed is True
    assert len(report.ir.sequence) > 1
    assert report.changes
    assert all(op.tag == "main" and op.confidence == 0.8 for op in report.ir.sequence)
    for op in report.ir.sequence:
        start = op.range.start.to_float()
        end = op.range.end.to_float()
        assert not (start < 100.5 and end > 99.5)
        assert end - start <= 90.0 + 1e-6
    # 黑帧区间被移除，总时长缩短约 2 秒；frozen 不参与修剪。
    assert report.ir.total_duration().to_float() < ir.total_duration().to_float()
    assert abs(report.ir.total_duration().to_float() - 298.0) <= 0.1

    boundaries = {
        round(op.range.start.to_float(), 3) for op in report.ir.sequence
    } | {round(op.range.end.to_float(), 3) for op in report.ir.sequence}
    assert 50.0 in boundaries
    assert 150.0 in boundaries


def test_repair_keeps_clean_short_sequence() -> None:
    ir = make_ir("10", "60")
    quality, shots = make_perception()
    report = repair_sequence(ir, quality, shots, max_segment_seconds=90.0)
    assert report.changed is False
    assert report.ir is ir


class FakeCriticToolkit:
    planner_available = True

    def __init__(self, quality: QualityReport, shots: ShotList) -> None:
        self.quality = quality
        self.shots = shots
        self.submitted: EditIR | None = None

    def get_perception(self, project_id: str) -> dict:
        return {
            "shots": self.shots.model_dump(mode="json"),
            "quality": self.quality.model_dump(mode="json"),
        }

    def submit_revision(self, project_id: str, ir: EditIR) -> dict:
        self.submitted = ir
        stored = ir.model_copy(update={"revision": 2})
        return {"revision": 2, "ir": stored.dump_ir()}


def test_critic_agent_submits_new_revision(tmp_path: Path) -> None:
    quality, shots = make_perception()
    toolkit = FakeCriticToolkit(quality, shots)
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        workspace_root=workspace.root,
        deepseek_api_key=None,
        critic_max_segment_seconds=90.0,
        critic_quality_padding=0.5,
    )
    ctx = AgentContext(
        run_id="run_test",
        project_id="p_test",
        goal="cut",
        toolkit=toolkit,
        workspace=workspace,
        settings=settings,
    )
    ir = make_ir()
    ctx.set_plan(1, ir)

    result = CriticAgent().run(
        OrchestratorTask(id="critic", agent="critic", depends_on=["plan"]),
        ctx,
    )

    assert result.output["changed"] is True
    assert result.output["previous_revision"] == 1
    assert result.output["revision"] == 2
    assert ctx.shared.revision == 2
    assert toolkit.submitted is not None
    assert len(toolkit.submitted.sequence) > 1
    assert len(ctx.shared.ir.sequence) > 1
