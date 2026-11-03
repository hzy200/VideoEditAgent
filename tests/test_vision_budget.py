from __future__ import annotations

import threading
import time
from pathlib import Path

from PIL import Image

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.contracts.vision import ImageVisualNote, ImageVisualNotes
from smartcut.llm.singleflight import SingleFlight
from smartcut.perception import frames as frames_mod
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.vision import chapters_needing_vision, select_images
from smartcut.runner.paths import Workspace
from smartcut.server.db import Artifact, Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


class FakeVision:
    available = True
    model = "fake-vlm"

    def __init__(self) -> None:
        self.calls = 0
        self.last_image_count = 0
        self.usage = {
            "requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def analyze(self, messages, schema):
        self.calls += 1
        assert schema is ImageVisualNotes
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        self.last_image_count = count
        self.usage["requests"] += 1
        self.usage["prompt_tokens"] += 10
        self.usage["total_tokens"] += 12
        return ImageVisualNotes(
            images=[
                ImageVisualNote(
                    image_index=index,
                    summary=f"画面 {index}",
                    visual_tags=["人物"],
                    cautions=[],
                )
                for index in range(count)
            ]
        )

    def usage_snapshot(self) -> dict[str, int]:
        return dict(self.usage)

    def close(self) -> None:
        pass


class FakePlanner:
    available = True

    def __init__(self) -> None:
        self.called = 0

    def propose(self, facts, transcript, goal, **kwargs) -> EditIR:
        self.called += 1
        return EditIR(
            project_id=kwargs["project_id"],
            revision=kwargs["revision"],
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(start=_rt("0.0"), end=_rt("1.0")),
                )
            ],
        )

    def close(self) -> None:
        pass


def _prepare(settings: Settings, video: Path, planner=None):
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
        planner=planner,
    )
    project = services.create_project("vision-budget")
    project_id = project["id"]
    services.ingest(project_id, str(video))
    extracted = services.extract_frames(project_id)
    services.build_contact_sheet(project_id)
    refs = frames_mod.read_manifest(
        services.workspaces.get(project_id).root / extracted["manifest"]
    )
    return services, db, jobs, project_id, refs


def test_text_first_skips_long_summary_chapters() -> None:
    chapters = [
        {"id": "ch_0001", "summary": "这是一段足够长的转写摘要，" * 4},
        {"id": "ch_0002", "summary": "太短"},
        {"id": "ch_0003"},
    ]
    selected = chapters_needing_vision(chapters, min_chars=40)
    assert [item["id"] for item in selected] == ["ch_0002", "ch_0003"]


def test_singleflight_coalesces_concurrent_calls() -> None:
    flight = SingleFlight()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def work() -> str:
        calls.append(1)
        started.set()
        release.wait(2.0)
        return "ok"

    results: list[str] = []

    def invoke() -> None:
        results.append(flight.run("same", work))

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert started.wait(1.0)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2.0)
    second.join(2.0)
    assert calls == [1]
    assert results == ["ok", "ok"]


def test_singleflight_propagates_and_allows_retry() -> None:
    flight = SingleFlight()
    started = threading.Event()
    release = threading.Event()

    def failing() -> None:
        started.set()
        release.wait(2.0)
        raise RuntimeError("boom")

    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            flight.run("fail", failing)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert started.wait(1.0)
    second.start()
    release.set()
    first.join(2.0)
    second.join(2.0)
    assert len(errors) == 2
    assert flight.run("fail", lambda: "retry-ok") == "retry-ok"


def test_text_first_reduces_images_to_contact_sheet(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, db, jobs, project_id, refs = _prepare(settings, synthetic_video)
    fake = FakeVision()
    services.vision = fake
    try:
        chapter = {
            "id": "ch_0001",
            "summary": "这段转写摘要已经足够长，不需要再发送关键帧图片。" * 2,
            "keyframes": [refs[0].path],
        }
        notes = services.build_visual_notes(
            project_id,
            "目标",
            {"chapters": [chapter]},
        )
        assert notes is not None
        assert fake.calls == 1
        assert fake.last_image_count == 1  # 仅全局 contact sheet
        metrics = services._take_vision_metrics()
        assert metrics["skipped_chapters"] == 1
        assert metrics["images"] == 1
        assert metrics["duration_ms"] >= 0
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_only_changed_image_is_requested_again(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, db, jobs, project_id, refs = _prepare(settings, synthetic_video)
    fake = FakeVision()
    services.vision = fake
    try:
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None
        keyframes = chapters["chapters"][0]["keyframes"]
        assert keyframes

        first = services.build_visual_notes(project_id, "目标", chapters)
        assert first is not None
        assert fake.calls == 1

        # 只删整图缓存，保留单图缓存。
        with services.db.session() as session:
            session.query(Artifact).filter_by(
                project_id=project_id,
                kind="vision_notes",
            ).delete()

        target = services.workspaces.get(project_id).root / keyframes[0]
        Image.new("RGB", (320, 180), (1, 2, 3)).save(target, format="JPEG")

        second = services.build_visual_notes(project_id, "目标", chapters)
        assert second is not None
        assert fake.calls == 2
        assert fake.last_image_count == 1
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


class DeepVision(FakeVision):
    def analyze(self, messages, schema):
        self.calls += 1
        assert schema is ImageVisualNotes
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        self.last_image_count = count
        self.usage["requests"] += 1
        if self.calls == 1:
            summary = ""
            cautions: list[str] = []
        else:
            summary = "复核画面细节"
            cautions = ["复核发现遮挡"]
        return ImageVisualNotes(
            images=[
                ImageVisualNote(
                    image_index=index,
                    summary=summary,
                    visual_tags=["人物"],
                    cautions=cautions,
                )
                for index in range(count)
            ]
        )


def test_chapter_tiles_reduce_vlm_image_count(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    (workspace.root / "cache").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (10, 20, 30)).save(
        workspace.root / "cache" / "sheet.jpg", format="JPEG"
    )
    paths = []
    for index, color in enumerate([(200, 0, 0), (0, 200, 0), (0, 0, 200)]):
        relative = f"frames/f{index}.jpg"
        path = workspace.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (320, 180), color).save(path, format="JPEG")
        paths.append(relative)

    settings = Settings(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        vision_base_url="https://vlm.example/v1",
        vision_model="vlm-test",
        vision_api_key="key",
        vision_max_images=7,
        vision_image_max_edge=256,
        vision_image_max_bytes=500_000,
        vision_total_max_bytes=1_000_000,
        vision_chapter_tiles=True,
    )
    chapters = [{"id": "ch_0001", "keyframes": paths}]
    tiled = select_images(
        workspace,
        {"contact_sheet": "cache/sheet.jpg"},
        chapters,
        settings,
    )
    assert len(tiled) == 2
    assert any("拼图" in image.label for image in tiled)

    untiled = select_images(
        workspace,
        {"contact_sheet": "cache/sheet.jpg"},
        chapters,
        settings.model_copy(update={"vision_chapter_tiles": False}),
    )
    assert len(untiled) == 3


def test_low_confidence_chapter_triggers_second_pass(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, db, jobs, project_id, _refs = _prepare(settings, synthetic_video)
    fake = DeepVision()
    services.vision = fake
    try:
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        notes = services.build_visual_notes(project_id, "目标", chapters)
        assert notes is not None
        assert fake.calls == 2
        metrics = services._take_vision_metrics()
        assert metrics["second_pass_chapters"] >= 1
        assert metrics["second_pass_images"] >= 1
        assert metrics["requests"] == 2
        assert any(
            "复核" in (item.get("summary") or "")
            for item in notes.get("chapters") or []
        )
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_plan_returns_vision_and_stage_metrics(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    planner = FakePlanner()
    services, db, jobs, project_id, _refs = _prepare(
        settings, synthetic_video, planner
    )
    fake = FakeVision()
    services.vision = fake
    try:
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        result = services.plan(project_id, "目标", chapters=chapters)
        assert planner.called == 1
        metrics = result["metrics"]
        assert metrics["vision"]["images"] >= 1
        assert metrics["vision"]["prompt_tokens"] == 10
        assert metrics["stages"]["vision"] >= 0
        assert metrics["stages"]["plan"] >= 0
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
