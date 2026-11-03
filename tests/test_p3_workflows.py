from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path
from typing import Any, cast

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.vision import VisualNotes
from smartcut.llm.vision import build_vision_client
from smartcut.orchestrator.planner import TaskPlanner
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(value: str) -> RationalTime:
    return RationalTime.from_seconds(value, rate=90_000)


def _facts(has_audio: bool = True) -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=320,
            height=240,
            avg_frame_rate="30/1",
        ),
        audio=StreamFacts(codec_type="audio") if has_audio else None,
    )


class _FakeToolkit:
    planner_available = False


def test_audio_first_planner_orders_transcribe_before_frames(tmp_path: Path) -> None:
    settings = Settings(workspace_root=tmp_path / "ws", deepseek_api_key=None)
    planner = TaskPlanner(cast(Any, _FakeToolkit()), settings)
    plan = planner._rule_plan(
        _facts(),
        allow_render=False,
        manual_ir=None,
        phase="audio_first",
    )
    by_agent = {task.agent: task for task in plan.tasks}
    assert "transcribe" in by_agent
    assert "transcribe" in by_agent["frames"].depends_on
    assert "candidates" in by_agent
    assert "音频优先" in plan.rationale


def test_render_review_reports_issues_without_touching_ir(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("render-review")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        workspace = services.project_workspace(project_id)
        output = workspace.root / "renders/r1/output.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(synthetic_video, output)

        ir = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(start=_rt("0"), end=facts.duration),
                )
            ],
        )
        services.submit_revision(project_id, ir)

        review = services.review_render(project_id, 1, goal="审片", keyframe_count=3)
        assert review["revision"] == 1
        assert review["output_path"] == "renders/r1/output.mp4"
        assert review["checks"]["duration"] is True
        assert review["checks"]["audio"] is True
        assert review["checks"]["resolution"] is True
        assert isinstance(review["issues"], list)
        assert 0.0 <= review["score"] <= 1.0
        assert "renders/r1/output.mp4" in review["artifacts"]
        assert any(item.startswith("cache/review_sheet/") for item in review["artifacts"])
        assert review["source"] == "rules"
        assert len(services.list_revisions(project_id)) == 1
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_local_vision_plugin_is_optional(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = types.ModuleType("fake_local_vlm")

    def analyze(messages, schema, *, model_path=None):
        assert messages
        assert schema is VisualNotes
        return {"overall": "本地视觉", "chapters": [], "model": "fake", "image_count": 0}

    module.analyze = analyze
    monkeypatch.setitem(sys.modules, "fake_local_vlm", module)

    settings = Settings(
        workspace_root=tmp_path / "ws",
        vision_provider="local",
        local_vision_module="fake_local_vlm",
        local_vision_backend="module",
    )
    client = build_vision_client(settings)
    assert client.available is True
    assert str(client.model).startswith("local:module:")
    notes = client.analyze([{"role": "user", "content": "x"}], VisualNotes)
    assert notes.overall == "本地视觉"
    assert client.usage_snapshot()["requests"] == 0

    onnx = build_vision_client(
        settings.model_copy(
            update={
                "local_vision_backend": "onnx",
                "local_vision_model_path": str(tmp_path / "missing.onnx"),
            }
        )
    )
    assert onnx.available is False
