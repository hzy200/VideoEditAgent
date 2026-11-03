from __future__ import annotations

from pathlib import Path

from smartcut.config import Settings
from smartcut.contracts.edit_ir import AudioSpec, EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.orchestrator.agents.audio import AudioAgent
from smartcut.orchestrator.agents.qa import SemanticQAAgent
from smartcut.orchestrator.audio import repair_audio
from smartcut.orchestrator.context import AgentContext
from smartcut.orchestrator.models import OrchestratorTask
from smartcut.perception.transcribe import NullTranscriber
from smartcut.runner.paths import Workspace
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def make_ir(*, remove_filler: bool = True, confidence: float = 0.9) -> EditIR:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
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
                range=TimeRange(start=_rt("0"), end=_rt("1.2")),
                tag="main",
                confidence=confidence,
            )
        ],
        audio=AudioSpec(remove_filler=remove_filler),
    )


def make_transcript() -> Transcript:
    words = [
        Word(text="hello", start=_rt("0.0"), end=_rt("0.5")),
        Word(text="um", start=_rt("0.5"), end=_rt("0.7")),
        Word(text="world", start=_rt("0.7"), end=_rt("1.2")),
    ]
    return Transcript(
        language="en",
        words=words,
        segments=[
            TranscriptSegment(
                id=0,
                text="hello um world",
                start=_rt("0"),
                end=_rt("1.2"),
                words=words,
            )
        ],
        degraded=False,
    )


def test_repair_audio_removes_filler_words() -> None:
    report = repair_audio(
        make_ir(),
        make_transcript(),
        filler_words={"um", "uh"},
        padding=0.05,
        min_segment_seconds=0.3,
    )
    assert report.changed is True
    assert report.removed_words == 1
    assert report.ir.audio.remove_filler is False
    assert len(report.ir.sequence) == 2
    for op in report.ir.sequence:
        assert not (op.range.start.to_float() < 0.75 and op.range.end.to_float() > 0.45)

    untouched = repair_audio(
        make_ir(remove_filler=False),
        make_transcript(),
        filler_words={"um"},
    )
    assert untouched.changed is False


class FakeAudioToolkit:
    planner_available = True

    def __init__(self) -> None:
        self.submitted: EditIR | None = None

    def get_transcript(self, project_id: str) -> Transcript:
        return make_transcript()

    def submit_revision(self, project_id: str, ir: EditIR) -> dict:
        self.submitted = ir
        stored = ir.model_copy(update={"revision": 2})
        return {"revision": 2, "ir": stored.dump_ir()}


def test_audio_agent_submits_revision(tmp_path: Path) -> None:
    toolkit = FakeAudioToolkit()
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_root=workspace.root, deepseek_api_key=None)
    ctx = AgentContext(
        run_id="run_test",
        project_id="p_test",
        goal="remove filler",
        toolkit=toolkit,
        workspace=workspace,
        settings=settings,
    )
    ctx.set_plan(1, make_ir())
    ctx.set_transcript(make_transcript(), {"degraded": False})

    result = AudioAgent().run(OrchestratorTask(id="audio", agent="audio"), ctx)

    assert result.output["changed"] is True
    assert ctx.shared.revision == 2
    assert toolkit.submitted is not None
    assert toolkit.submitted.audio.remove_filler is False


class FakeQAToolkit:
    planner_available = True

    def review_edit(self, project_id, revision_no, goal, visual_notes=None) -> dict:
        return {
            "score": 0.55,
            "summary": "节奏偏慢",
            "issues": [
                {
                    "severity": "warning",
                    "message": "片段过长",
                    "segment_index": 0,
                    "suggestion": "缩短",
                }
            ],
            "source": "rules",
        }


def test_semantic_qa_agent_records_review(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_root=workspace.root, deepseek_api_key=None)
    ctx = AgentContext(
        run_id="run_test",
        project_id="p_test",
        goal="cut",
        toolkit=FakeQAToolkit(),
        workspace=workspace,
        settings=settings,
    )
    ctx.set_plan(1, make_ir())

    result = SemanticQAAgent().run(OrchestratorTask(id="qa", agent="qa"), ctx)

    assert ctx.shared.review is not None
    assert ctx.shared.review["score"] == 0.55
    assert result.output["issue_count"] == 1
    assert result.warnings and "片段过长" in result.warnings[0]


def test_services_deterministic_review_flags_low_confidence(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("qa")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        ir = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(start=_rt("0.5"), end=_rt("1.5")),
                    confidence=0.3,
                )
            ],
        )
        revision = services.submit_revision(project_id, ir)["revision"]
        review = services.review_edit(project_id, revision, "goal")
        assert review["source"] == "rules"
        assert review["score"] < 0.9
        assert any("置信度" in item["message"] for item in review["issues"])
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
