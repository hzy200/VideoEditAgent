from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from smartcut.config import Settings
from smartcut.contracts.screen_text import ScreenTextEvidence
from smartcut.contracts.time import RationalTime
from smartcut.contracts.vision import ImageVisualNote, ImageVisualNotes
from smartcut.perception.screen_text import (
    aggregate_screen_text,
    detect_text_regions,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _text_image() -> bytes:
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 560, 120), fill="black")
    draw.text((60, 60), "CHAPTER 01 INTRODUCTION", fill="white")
    draw.rectangle((40, 200, 300, 260), fill="black")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_detect_text_regions_finds_text_like_boxes() -> None:
    pytest.importorskip("cv2")
    regions = detect_text_regions(_text_image())
    assert regions
    for x, y, w, h in regions:
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        assert w > 0 and h > 0
        assert x + w <= 1.001 and y + h <= 1.001


def test_aggregate_screen_text_dedup_and_stability() -> None:
    frames = [
        type("Frame", (), {"t": _rt("0"), "path": "f0.jpg"})(),
        type("Frame", (), {"t": _rt("1"), "path": "f1.jpg"})(),
        type("Frame", (), {"t": _rt("2"), "path": "f2.jpg"})(),
    ]
    results = [
        {
            "engine": "fake",
            "items": [
                {"text": "第一章 标题", "bbox": (0.1, 0.1, 0.5, 0.2), "confidence": 0.9}
            ],
            "error": None,
        }
        for _ in frames
    ]
    items = aggregate_screen_text(frames, results, min_confidence=0.5)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ScreenTextEvidence)
    assert item.text == "第一章 标题"
    assert item.stability == 1.0
    assert len(item.sha256) == 64
    assert items[0].t.to_float() == 0.0


class _FakeOcr:
    version = "ocr@test"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, paths, **kwargs):
        self.calls += 1
        return [
            {
                "engine": "fake",
                "items": [
                    {
                        "text": "第一章 标题",
                        "bbox": (0.1, 0.1, 0.5, 0.2),
                        "confidence": 0.9,
                    }
                ],
                "error": None,
            }
            for _ in paths
        ]


class _FakeVision:
    available = True
    model = "fake-vlm"

    def analyze(self, messages, schema):
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        return ImageVisualNotes(
            images=[
                ImageVisualNote(image_index=index, summary=f"画面 {index}")
                for index in range(count)
            ]
        )

    def close(self) -> None:
        pass


def test_screen_text_cache_and_visual_notes_injection(
    settings: Settings,
    synthetic_video: Path,
    monkeypatch,
) -> None:
    settings = settings.model_copy(
        update={
            "ocr_enabled": True,
            "vision_use_evidence": True,
            "view_evidence_enabled": True,
            "vision_base_url": "https://vlm.example/v1",
            "vision_model": "vlm-test",
            "vision_api_key": "vision-key",
        }
    )
    import smartcut.server.services.screen_text as screen_service

    monkeypatch.setattr(
        screen_service,
        "detect_text_regions",
        lambda *_args, **_kwargs: [],
    )

    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    ocr = _FakeOcr()
    services = Services(
        settings,
        db,
        jobs,
        transcriber=NullTranscriber(),
        screen_text_extractor=ocr,
    )
    services.vision = _FakeVision()
    try:
        project_id = services.create_project("screen-text")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None

        first = services.build_screen_text(project_id)
        assert first["available"] is True
        assert first["item_count"] == 1
        assert first["cached"] is False
        calls = ocr.calls

        second = services.build_screen_text(project_id)
        assert second["cached"] is True
        assert ocr.calls == calls

        notes = services.build_visual_notes(project_id, "目标", chapters)
        assert notes is not None
        assert notes["screen_text"]
        assert any(note["screen_text"] for note in notes["chapters"])
        assert ocr.calls == calls
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
