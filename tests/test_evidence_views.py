from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("PIL")
pytest.importorskip("cv2")

from PIL import Image, ImageDraw

from smartcut.config import Settings
from smartcut.contracts.time import RationalTime
from smartcut.contracts.transcript import Transcript, TranscriptSegment
from smartcut.perception.transcribe import NullTranscriber
from smartcut.perception.view.budget import view_budget
from smartcut.perception.view.flow import (
    crop_roi,
    flow_between,
    flow_strip,
    temporal_composite,
)
from smartcut.perception.view.measure import TileInput
from smartcut.perception.view.views import (
    build_flow_strip,
    build_roi,
    build_temporal_diff,
)
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services
from smartcut.server.services.evidence import EvidenceMixin


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _image(offset: int) -> bytes:
    image = Image.new("RGB", (320, 180), (18, 24, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + offset, 40, 120 + offset, 110), fill=(240, 240, 240))
    draw.text((30, 130), f"frame {offset}", fill=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def test_flow_channels_and_strips() -> None:
    previous = _image(0)
    current = _image(30)
    flow = flow_between(previous, current)
    assert flow.flow_mag > 0
    assert flow.mag_cap > flow.mag_floor
    assert flow.diff_rgb and flow.hue_rgb
    assert flow.flow_x_gray and flow.flow_y_gray
    composite, result = temporal_composite(previous, current, 320, 180)
    assert result.flow_mag > 0
    assert composite[:2] == b"\xff\xd8"
    strip = flow_strip([flow, flow], 320, 180)
    assert strip[:2] == b"\xff\xd8"


def test_view_builders_produce_aligned_tiles() -> None:
    tiles = [
        TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image(0)),
        TileInput(t=_rt("1"), pts=30, frame_no=30, image_bytes=_image(20)),
        TileInput(t=_rt("2"), pts=60, frame_no=60, image_bytes=_image(40)),
    ]
    diff = build_temporal_diff(tiles, tile_width=320, tile_height=180)
    assert len(diff.tiles) == 2
    assert len(diff.extra_metrics) == 2
    assert diff.constants["flow_encoding"] == "hsv_hue_v1"

    strips = build_flow_strip(tiles, tile_width=320, tile_height=180)
    assert strips.tiles
    assert strips.extra_metrics

    roi = build_roi(tiles[1], (0.2, 0.2, 0.4, 0.4), target=256)
    assert len(roi.tiles) == 1
    assert roi.constants["roi_target"] == 256


def test_crop_roi_returns_source_pixels() -> None:
    payload, box = crop_roi(_image(0), (0.2, 0.2, 0.4, 0.4), target=256)
    assert payload[:2] == b"\xff\xd8"
    assert box[2] > box[0] and box[3] > box[1]


def test_view_budget_caps_cpu() -> None:
    settings = Settings(workspace_root=".", view_max_images=7)
    cpu = view_budget(settings, "cpu")
    gl = view_budget(settings, "qtgl")
    assert cpu.max_images <= 5
    assert gl.max_images == 7
    assert gl.tile_width >= cpu.tile_width


def test_silence_ranges_from_transcript_gaps() -> None:
    transcript = Transcript(
        language="zh",
        words=[],
        segments=[
            TranscriptSegment(id=0, text="a", start=_rt("0"), end=_rt("1"), words=[]),
            TranscriptSegment(id=1, text="b", start=_rt("3"), end=_rt("4"), words=[]),
        ],
        degraded=False,
    )
    gaps = EvidenceMixin._silence_ranges(transcript)
    assert gaps == [{"start": 1.0, "end": 3.0, "kind": "silence"}]


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_service_builds_all_evidence_kinds(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("evidence-kinds")["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        for kind, tile_index in (
            ("contact_sheet", None),
            ("temporal_diff", None),
            ("flow_strip", None),
            ("bbox_overlay", None),
            ("codec_residual", None),
            ("roi_crop", 2),
        ):
            result = services.build_visual_evidence(
                project_id,
                kind=kind,
                tile_index=tile_index,
            )
            assert result["available"] is True, (kind, result)
            assert result["kind"] == kind
            assert result["evidence"]["kind"] == kind
            assert result["evidence"]["tile_map"]["tiles"]
            directory = services.project_workspace(project_id).root / result["path"]
            assert (directory / "view.png").is_file()
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
