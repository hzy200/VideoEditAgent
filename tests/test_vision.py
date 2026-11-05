from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from PIL import Image

from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.time import RationalTime
from smartcut.contracts.vision import (
    ImageVisualNote,
    ImageVisualNotes,
    VisualNotes,
)
from smartcut.errors import LLMResponseError
from smartcut.llm.vision import VisionClient
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.context import build_context, visual_facts_payload
from smartcut.planner.vision import (
    VisionImage,
    build_image_note_messages,
    build_vision_messages,
    select_images,
)
from smartcut.runner.paths import Workspace
from smartcut.server.db import Artifact, Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), color).save(path, format="JPEG")


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        workspace_root=tmp_path / "workspace",
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        deepseek_api_key=None,
        hardware_encoding="off",
        vision_base_url="https://vlm.example/v1",
        vision_model="vlm-test",
        vision_api_key="vision-key",
        vision_max_images=3,
        vision_max_keyframes_per_chapter=2,
        vision_image_max_edge=256,
        vision_image_max_bytes=200_000,
        vision_total_max_bytes=500_000,
    )
    values.update(overrides)
    return Settings(**values)


def test_select_images_caps_contact_sheet_and_keyframes(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    _image(workspace.root / "cache" / "sheet.jpg", (10, 20, 30))
    for index, color in enumerate([(200, 0, 0), (0, 200, 0), (0, 0, 200)]):
        _image(workspace.root / "frames" / f"f{index}.jpg", color)

    chapters = [
        {"id": "ch_0001", "keyframes": ["frames/f0.jpg", "frames/f1.jpg", "frames/f2.jpg"]},
        {"id": "ch_0002", "keyframes": ["frames/f1.jpg", "frames/f2.jpg"]},
    ]
    settings = _settings(tmp_path)
    images = select_images(
        workspace,
        {"contact_sheet": "cache/sheet.jpg"},
        chapters,
        settings,
    )
    assert len(images) == 3
    assert images[0].label.startswith("全局")
    assert all(item.data_url.startswith("data:image/jpeg;base64,") for item in images)
    assert len({item.sha256 for item in images}) == 3

    messages = build_vision_messages("目标", chapters, images[:2])
    parts = messages[1]["content"]
    assert isinstance(parts, list)
    assert any(part.get("type") == "image_url" for part in parts)
    assert any(part.get("type") == "text" and "ch_0001" in part.get("text", "") for part in parts)


def test_vision_client_sends_image_parts_and_parses_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        calls.append({"url": str(request.url), "body": payload.decode("utf-8")})
        content = '{"chapters":[{"chapter_id":"ch_0001","summary":"人物在讲解",'
        content += '"visual_tags":["人物"],"cautions":[]}],"overall":"口播"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = VisionClient(settings, transport=httpx.MockTransport(handler))
    try:
        image = type(
            "Image",
            (),
            {
                "label": "章节 ch_0001 关键帧",
                "data_url": "data:image/jpeg;base64,AAAA",
                "sha256": "deadbeef",
                "size": 4,
                "mime": "image/jpeg",
                "chapter_id": "ch_0001",
            },
        )()
        messages = build_vision_messages(
            "目标",
            [{"id": "ch_0001", "title": "Intro", "start": {"value": 0, "rate": 90000}, "end": {"value": 10, "rate": 90000}}],
            [image],
        )
        notes = client.analyze(messages, VisualNotes)
        assert notes.chapters[0].summary == "人物在讲解"
        assert calls and "image_url" in calls[0]["body"]
        assert calls[0]["url"].endswith("/v1/chat/completions")
    finally:
        client.close()


def test_vision_http_failure_is_explicit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = VisionClient(
        settings,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="boom")),
    )
    try:
        with pytest.raises(LLMResponseError):
            client.analyze(
                [{"role": "user", "content": "hello"}],
                VisualNotes,
            )
    finally:
        client.close()


def test_visual_notes_enter_planner_context() -> None:
    facts = MediaFacts(
        src="media/a.mp4",
        duration=_rt("10"),
        fps="30/1",
        video=StreamFacts(codec_type="video", width=1280, height=720, avg_frame_rate="30/1"),
    )
    context = build_context(
        facts,
        None,
        "goal",
        visual_notes={
            "overall": "室内口播",
            "chapters": [
                {
                    "chapter_id": "ch_0001",
                    "summary": "人物近景",
                    "visual_tags": ["人物"],
                    "cautions": ["轻微模糊"],
                }
            ],
            "image_count": 2,
            "model": "vlm-test",
            "view_kinds": ["bbox_overlay", "roi_crop"],
            "visual_facts": [
                {
                    "chapter_id": "ch_0001",
                    "view_kinds": ["bbox_overlay", "roi_crop"],
                    "face_coverage": 0.42,
                    "mean_d_hist": 0.18,
                    "motion_level": "low",
                    "quality_risks": ["blurry"],
                }
            ],
        },
    )
    assert context["visual_notes"]["overall"] == "室内口播"
    assert context["visual_notes"]["chapters"][0]["chapter_id"] == "ch_0001"
    assert context["visual_notes"]["image_count"] == 2
    assert context["visual_notes"]["view_kinds"] == ["bbox_overlay", "roi_crop"]
    assert context["visual_facts"]["chapter_count"] == 1
    facts = context["visual_facts"]["chapters"][0]
    assert facts["chapter_id"] == "ch_0001"
    assert facts["face_coverage"] == 0.42
    assert facts["mean_d_hist"] == 0.18
    assert facts["motion_level"] == "low"
    assert facts["quality_risks"] == ["blurry"]


def test_visual_facts_payload_filters_and_limits() -> None:
    notes = {
        "view_kinds": ["contact_sheet"],
        "visual_facts": [
            {
                "chapter_id": f"ch_{index:04d}",
                "view_kinds": ["contact_sheet"],
                "face_coverage": 0.1,
                "mean_d_hist": 0.2,
                "motion_level": "low",
                "quality_risks": [],
            }
            for index in range(60)
        ],
    }
    payload = visual_facts_payload(notes, limit=20)
    assert payload is not None
    assert payload["chapter_count"] == 20
    assert payload["chapters"][0]["chapter_id"] == "ch_0000"

    selected = visual_facts_payload(
        notes,
        chapter_ids=["ch_0003", "ch_0007"],
    )
    assert selected is not None
    assert [item["chapter_id"] for item in selected["chapters"]] == [
        "ch_0003",
        "ch_0007",
    ]
    assert visual_facts_payload(None) is None


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
                    range=TimeRange(start=_rt("0.5"), end=_rt("1.5")),
                )
            ],
        )

    def close(self) -> None:
        pass


class FakeVision:
    available = True
    model = "fake-vlm"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, messages, schema):
        self.calls += 1
        assert schema is ImageVisualNotes
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
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

    def close(self) -> None:
        pass


def test_visual_notes_cache_before_images_and_per_image_reuse(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    fake = FakeVision()
    services.vision = fake
    try:
        project = services.create_project("vision-cache")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_contact_sheet(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None

        first = services.build_visual_notes(project_id, "目标", chapters)
        assert first is not None
        assert fake.calls == 1
        assert first["model"] == "fake-vlm"
        assert first["chapters"]

        # 只删整图缓存，保留单图缓存：不应再次调用 VLM。
        with services.db.session() as session:
            session.query(Artifact).filter_by(
                project_id=project_id,
                kind="vision_notes",
            ).delete()

        second = services.build_visual_notes(project_id, "目标", chapters)
        assert second is not None
        assert fake.calls == 1
        assert second["chapters"]

        messages = build_image_note_messages(
            "目标",
            chapters.get("chapters") or [],
            [
                VisionImage(
                    label="测试图",
                    mime="image/jpeg",
                    data_url="data:image/jpeg;base64,AAAA",
                    sha256="abc",
                    size=4,
                    chapter_id="ch_0001",
                )
            ],
        )
        assert "image_index" in messages[1]["content"][0]["text"]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_plan_degrades_when_vision_fails(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    planner = FakePlanner()
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
        planner=planner,
    )
    try:
        project = services.create_project("vision")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))

        def fail(*_args, **_kwargs):
            raise RuntimeError("vlm down")

        services.build_visual_notes = fail
        result = services.plan(
            project_id,
            "goal",
            perception={},
            chapters={
                "chapters": [
                    {
                        "id": "ch_0001",
                        "start": {"value": 0, "rate": 90000},
                        "end": {"value": 2_000_000, "rate": 90000},
                    }
                ]
            },
            candidates={"candidates": []},
        )
        assert result["revision"] == 1
        assert planner.called == 1
        assert result["warnings"]
        assert "视觉摘要失败" in result["warnings"][0]
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
