from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.timeline import build_timeline_memory
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.contracts.workspace import Workspace
from smartcut.orchestrator.context import (
    CONTEXT_DIGEST_LIMIT,
    AgentContext,
    summarize_context,
)
from smartcut.orchestrator.graph import TaskGraphExecutor
from smartcut.orchestrator.models import (
    AgentResult,
    OrchestrationPlan,
    OrchestrationResult,
    OrchestratorTask,
    TaskOutcome,
    TaskStatus,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.context import build_context, estimate_tokens
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _make_ir(ranges: list[tuple[str, str]], *, duration: str = "10") -> EditIR:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
        ),
    )
    return EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=facts.src,
                range=TimeRange(start=_rt(start), end=_rt(end)),
                tag=f"seg{index}",
            )
            for index, (start, end) in enumerate(ranges)
        ],
    )


def _ctx(tmp_path: Path) -> AgentContext:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return AgentContext(
        run_id="run_memory",
        project_id="p_memory",
        goal="memory",
        toolkit=cast(Any, object()),
        workspace=Workspace(root),
        settings=Settings(workspace_root=root),
    )


def _transcript(segments: int = 80) -> Transcript:
    items = []
    words = []
    for index in range(segments):
        start = _rt(str(index))
        end = _rt(str(index) + ".9")
        text = "这是一段用于测试上下文预算的中文转录内容" * 3
        items.append(
            TranscriptSegment(
                start=start,
                end=end,
                text=text,
                words=[Word(text=f"w{j}", start=start, end=end) for j in range(4)],
            )
        )
        words.extend(
            Word(text=f"w{j}", start=start, end=end) for j in range(4)
        )
    return Transcript(language="zh", words=words, segments=items)


def test_timeline_memory_tracks_coverage_duplicates_and_gaps() -> None:
    memory = build_timeline_memory(_make_ir([("0", "4"), ("2", "6")]))

    assert memory.output_duration.to_ffmpeg_seconds() == "8"
    assert memory.coverage_ratio == 0.6
    assert memory.duplicate_ratio == 0.25
    assert [
        (gap.start.to_ffmpeg_seconds(), gap.end.to_ffmpeg_seconds())
        for gap in memory.unused_gaps
    ] == [("6", "10")]
    assert [item.tag for item in memory.used] == ["seg0", "seg1"]
    assert memory.used[1].out_start.to_ffmpeg_seconds() == "4"


def test_set_plan_updates_timeline_and_slot_meta(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.begin_task(OrchestratorTask(id="plan", agent="plan"))
    try:
        ctx.set_plan(1, _make_ir([("0", "2")]))
    finally:
        ctx.end_task()

    assert ctx.shared.timeline is not None
    assert ctx.shared.meta["timeline"].source == "plan"
    assert ctx.shared.meta["ir"].source == "plan"

    ctx.set_candidates(
        {"candidate_count": 1, "candidates": [{"id": "c1"}], "cache_key": "abc"}
    )
    assert ctx.shared.meta["candidates"].source == "system"
    assert ctx.shared.meta["candidates"].cache_key == "abc"


def test_context_budget_truncates_transcript_by_tokens() -> None:
    transcript = _transcript()
    stats: dict[str, Any] = {}
    context = build_context(
        _make_ir([("0", "5")]).media_facts,
        transcript,
        "goal",
        max_tokens=1_500,
        stats=stats,
    )

    assert context["transcript"]["truncated"] is True
    assert len(context["transcript"]["segments"]) < len(transcript.segments)
    assert stats["budget_tokens"] == 1_500
    assert stats["transcript_truncated"] is True
    assert stats["sections"]["transcript"]["tokens"] <= 1_500
    assert stats["estimated_tokens"] > 0


def test_estimate_tokens_handles_cjk_and_ascii() -> None:
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0


def test_timeline_payload_is_compact_and_bounded() -> None:
    from smartcut.planner.context import timeline_payload

    memory = build_timeline_memory(_make_ir([("0", "1"), ("2", "3")]))
    payload = timeline_payload(memory, max_used=1, max_gaps=1)

    assert payload is not None
    assert payload["used_count"] == 2
    assert len(payload["used"]) == 1
    assert payload["output_seconds"] == 2.0
    assert payload["coverage_ratio"] == 0.2


class _DigestAgent:
    name = "analyze"
    description = "digest"

    def run(self, task: OrchestratorTask, ctx: AgentContext) -> AgentResult:
        return AgentResult(summary="ok", output={"ok": True})


def test_graph_records_bounded_context_digest(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.set_chapters(
        {
            "chapter_count": 500,
            "chapters": [{"id": f"ch_{index:04d}"} for index in range(500)],
        }
    )
    executor = TaskGraphExecutor({"analyze": _DigestAgent()}, max_parallel=1)
    outcomes, ok = executor.run(
        OrchestrationPlan(tasks=[OrchestratorTask(id="analyze", agent="analyze")]),
        ctx,
    )

    assert ok is True
    digest = outcomes[0].context_digest
    assert digest["agent"] == "analyze"
    assert digest["counts"]["chapters"] == 500
    assert digest["slots"]["chapters"] is True
    assert digest["slot_sources"]["chapters"]["source"] == "system"
    size = len(json.dumps(digest, ensure_ascii=False).encode("utf-8"))
    assert size <= CONTEXT_DIGEST_LIMIT


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_checkpoint_writes_context_digest(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("digest")["id"]
        services.ingest(project_id, str(synthetic_video))
        run = services.create_orchestration_run(project_id, "分析", allow_render=False)
        outcome = TaskOutcome(
            task_id="analyze",
            agent="analyze",
            status=TaskStatus.succeeded,
            summary="ok",
            context_digest={"agent": "analyze", "counts": {"chapters": 0}},
        )
        plan = OrchestrationPlan(
            tasks=[OrchestratorTask(id="analyze", agent="analyze")]
        )

        def fake_run(**kwargs) -> OrchestrationResult:
            kwargs["on_task_complete"]([outcome])
            return OrchestrationResult(
                run_id=kwargs["run_id"],
                project_id=kwargs["project_id"],
                goal=kwargs["goal"],
                status="succeeded",
                answer="ok",
                confidence=1.0,
                plan=plan,
                ledger=[outcome],
            )

        monkeypatch.setattr(services.orchestrator, "run", fake_run)
        services.orchestrate_run(run["id"])

        run_dir = services.workspaces.run_tmp(project_id, run["id"])
        payload = json.loads((run_dir / "context" / "analyze.json").read_text("utf-8"))
        assert payload["agent"] == "analyze"
        assert payload["counts"]["chapters"] == 0
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
