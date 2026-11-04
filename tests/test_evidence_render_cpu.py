from __future__ import annotations

from io import BytesIO

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

from smartcut.contracts.time import RationalTime
from smartcut.contracts.visual_evidence import ViewRequest
from smartcut.perception.view.hash import compare_semantic, semantic_hash
from smartcut.perception.view.measure import TileInput, measure_tiles
from smartcut.perception.view.render_cpu import CpuEvidenceRenderer
from smartcut.perception.view.synthesizer import ViewSynthesizer


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _image(color: tuple[int, int, int], text: str) -> bytes:
    image = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 120, 80), fill=(255, 255, 255))
    draw.text((30, 100), text, fill=(0, 0, 0))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _tiles() -> list[TileInput]:
    return [
        TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=_image((20, 20, 80), "A")),
        TileInput(t=_rt("2"), pts=60, frame_no=60, image_bytes=_image((80, 20, 20), "B")),
        TileInput(t=_rt("4"), pts=120, frame_no=120, image_bytes=_image((20, 80, 20), "C")),
    ]


def _request() -> ViewRequest:
    return ViewRequest(
        content_id="p_evidence",
        source_range=(_rt("0"), _rt("4")),
        grid_cols=2,
        tile_width=320,
        tile_height=180,
    )


def test_measure_and_cpu_render_are_deterministic() -> None:
    measure = measure_tiles(
        _tiles(),
        content_id="p_evidence",
        time_base="1/30",
        grid_cols=2,
        dedupe_distance=4,
    )
    assert [tile.label for tile in measure.tile_map.tiles] == ["T01", "T02", "T03"]
    assert len(measure.measurements) == 3
    renderer = CpuEvidenceRenderer()
    first = renderer.render(measure, _request())
    second = renderer.render(measure, _request())
    assert first.png_bytes == second.png_bytes
    assert first.backend == "cpu"
    assert semantic_hash(first.png_bytes, grid_rows=2, grid_cols=2) == semantic_hash(
        second.png_bytes, grid_rows=2, grid_cols=2
    )
    comparison = compare_semantic(
        first.png_bytes,
        second.png_bytes,
        grid_rows=2,
        grid_cols=2,
    )
    assert comparison["within_tolerance"] is True
    assert comparison["max_distance"] == 0


def test_duplicate_tiles_are_removed_and_reindexed() -> None:
    duplicate = _image((20, 20, 80), "A")
    tiles = [
        TileInput(t=_rt("0"), pts=0, frame_no=0, image_bytes=duplicate),
        TileInput(t=_rt("2"), pts=60, frame_no=60, image_bytes=duplicate),
    ]
    measure = measure_tiles(
        tiles,
        content_id="p_evidence",
        time_base="1/30",
        grid_cols=2,
    )
    assert len(measure.tile_map.tiles) == 1
    assert measure.tile_map.tiles[0].label == "T01"


def test_synthesizer_hashes_and_meta() -> None:
    measure = measure_tiles(
        _tiles(),
        content_id="p_evidence",
        time_base="1/30",
        grid_cols=2,
    )
    measure.constants.update(
        {"fps_sample": 2.0, "dedupe_threshold": 4, "scene_threshold_pct": 0.0}
    )
    synthesizer = ViewSynthesizer(CpuEvidenceRenderer())
    first = synthesizer.synthesize(
        measure,
        _request(),
        source_ref={"content_id": "p_evidence", "media_sha256_16": "a" * 16},
        image_ref="cache/evidence/x/view.png",
    )
    second = synthesizer.synthesize(
        measure,
        _request(),
        source_ref={"content_id": "p_evidence", "media_sha256_16": "a" * 16},
        image_ref="cache/evidence/x/view.png",
    )
    assert first.evidence.render_hash == second.evidence.render_hash
    assert first.evidence.semantic_hash == second.evidence.semantic_hash
    assert first.evidence.time_role == "candidate_only"
    assert first.evidence.meta["backend"] == "cpu"
    assert first.evidence.id.startswith("ve_p_evidence_contact_sheet_")
