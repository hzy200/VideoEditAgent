from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.config import Settings
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.contracts.vision import (
    ChapterVisualNote,
    ImageVisualNote,
    ImageVisualNotes,
)
from smartcut.perception.transcribe import NullTranscriber
from smartcut.planner.context import visual_notes_payload
from smartcut.planner.vision import VisionImage, build_image_note_messages
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services
from smartcut.server.services.visual import VisualMixin


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def test_visual_note_evidence_refs_are_bounded() -> None:
    note = ImageVisualNote(image_index=0, evidence_refs=["T01", "T02"])
    assert note.evidence_refs == ["T01", "T02"]
    with pytest.raises(ValueError):
        ImageVisualNote(image_index=0, evidence_refs=["T99", "T99"])
    with pytest.raises(ValueError):
        ChapterVisualNote(chapter_id="ch_1", evidence_refs=["frame-1"])


def test_visual_notes_payload_includes_evidence_refs() -> None:
    payload = visual_notes_payload(
        {
            "chapters": [
                {
                    "chapter_id": "ch_1",
                    "summary": "人物",
                    "evidence_refs": ["T02", "T03"],
                }
            ],
            "evidence_ids": ["ve_1"],
        }
    )
    assert payload["chapters"][0]["evidence_refs"] == ["T02", "T03"]
    assert payload["evidence_ids"] == ["ve_1"]


def test_image_note_messages_interleave_evidence_and_transcript() -> None:
    transcript = Transcript(
        language="zh",
        words=[],
        segments=[
            TranscriptSegment(
                id=0,
                text="大家好，这是重点",
                start=_rt("1.0"),
                end=_rt("3.0"),
                words=[],
            )
        ],
        degraded=False,
    )
    image = VisionImage(
        label="视觉证据视图",
        mime="image/jpeg",
        data_url="data:image/jpeg;base64,AAAA",
        sha256="a" * 64,
        size=4,
        evidence_id="ve_1",
        tile_metadata=(
            {"label": "T01", "t_seconds": 1.0, "pts": 30, "frame_no": 30},
            {
                "label": "T02",
                "t_seconds": 2.0,
                "pts": 60,
                "frame_no": 60,
                "d_hist": 0.42,
                "flow_mag": 2.1,
                "flow_dir_deg": 123.4,
                "face": 0.3,
                "score": 0.75,
                "risk": "blurry",
            },
        ),
    )
    messages = build_image_note_messages(
        "目标",
        [{"id": "ch_1", "title": "开场"}],
        [image],
        transcript=transcript,
    )
    text = messages[1]["content"][0]["text"]
    assert "证据时间线" in text
    assert "[T01|1.000s]" in text
    assert (
        "[T02|2.000s|d_hist 0.42|flow 2.1|dir 123|face 0.30|score 0.75|risk blurry]"
        in text
    )
    # 测量表只在时间线出现一次，避免与 tiles JSON 重复。
    assert text.count("d_hist 0.42") == 1
    # 未测量的 T01 不得补造测量值。
    assert "[T01|1.000s|" not in text
    assert "[语音] 大家好" in text
    assert "evidence_id" in text


class _EvidenceVision:
    available = True
    model = "fake-evidence-vlm"

    def __init__(self, refs: list[str]) -> None:
        self.refs = refs
        self.calls = 0
        self.messages: list[dict] = []

    def analyze(self, messages, schema):
        self.calls += 1
        self.messages.append(messages)
        count = sum(
            1 for part in messages[1]["content"] if part.get("type") == "image_url"
        )
        return ImageVisualNotes(
            images=[
                ImageVisualNote(
                    image_index=index,
                    summary="证据画面",
                    visual_tags=["人物"],
                    evidence_refs=list(self.refs),
                )
                for index in range(count)
            ]
        )

    def close(self) -> None:
        pass


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_visual_notes_use_evidence_and_keep_refs(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    services, jobs, db = _services(settings)
    fake = _EvidenceVision(["T01"])
    services.vision = fake
    try:
        project_id = services.create_project("evidence-vision")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None

        result = services.build_visual_notes(project_id, "目标", chapters)
        assert result is not None
        assert result["evidence_ids"]
        assert any(note["evidence_refs"] for note in result["chapters"])
        metadata = fake.messages[0][1]["content"][0]["text"]
        assert "T01" in metadata
        assert "证据时间线" in metadata
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_visual_notes_reject_unknown_evidence_label(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"vision_use_evidence": True, "view_evidence_enabled": True}
    )
    services, jobs, db = _services(settings)
    services.vision = _EvidenceVision(["T99"])
    try:
        project_id = services.create_project("evidence-bad-ref")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.build_chapters(project_id)
        chapters = services.get_chapters(project_id)
        assert chapters is not None
        with pytest.raises(ValueError, match="不存在的证据标签"):
            services.build_visual_notes(project_id, "禁止标签", chapters)
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_multiview_evidence_refs_use_union_and_reject_duplicates() -> None:
    """多视图白名单取并集；跨视图同名标签必须拒绝，避免引用歧义。"""
    validator = VisualMixin()

    def view(*labels: str) -> dict:
        return {
            "evidence": {
                "tile_map": {"tiles": [{"label": label} for label in labels]}
            }
        }

    views = [view("T01", "T02"), view("T03", "T04")]
    validator._validate_evidence_refs(
        None,
        [ImageVisualNote(image_index=0, evidence_refs=["T01", "T04"])],
        views=views,
    )
    with pytest.raises(ValueError, match="不存在的证据标签"):
        validator._validate_evidence_refs(
            None,
            [ImageVisualNote(image_index=0, evidence_refs=["T09"])],
            views=views,
        )
    duplicates = [view("T01"), view("T01")]
    with pytest.raises(ValueError, match="跨视图重复"):
        validator._validate_evidence_refs(
            None,
            [ImageVisualNote(image_index=0, evidence_refs=["T01"])],
            views=duplicates,
        )
