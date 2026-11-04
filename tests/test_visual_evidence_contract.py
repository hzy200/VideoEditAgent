from __future__ import annotations

import pytest

from smartcut.contracts.time import RationalTime
from smartcut.contracts.visual_evidence import (
    Measurement,
    Tile,
    TileMap,
    ViewRequest,
    canonical_bytes,
    compute_render_hash,
    evidence_id,
    measurements_hash,
    stable_hash,
    stable_meta,
    tile_map_hash,
    validate_meta,
)


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _tile(index: int = 1) -> Tile:
    return Tile(
        index=index,
        label=f"T{index:02d}",
        t=_rt(str(index)),
        pts=index * 30,
        frame_no=index * 30,
        row=0,
        col=index - 1,
    )


def test_canonical_and_stable_hash_are_deterministic() -> None:
    payload = {"b": 1.23456789, "a": [RationalTime(value=1, rate=30)]}
    assert canonical_bytes(payload) == canonical_bytes(
        {"a": ["1/30"], "b": 1.234568}
    )
    assert stable_hash(payload) == stable_hash(payload)
    with pytest.raises(ValueError):
        canonical_bytes({"bad": float("nan")})


def test_tile_map_requires_contiguous_indices() -> None:
    with pytest.raises(ValueError):
        TileMap(
            map_id="tm_1",
            content_id="c_1",
            tiles=[_tile(2)],
        )
    tile_map = TileMap(map_id="tm_1", content_id="c_1", tiles=[_tile(1), _tile(2)])
    assert tile_map_hash(tile_map) == tile_map_hash(tile_map)


def test_render_hash_ignores_backend_but_includes_meta() -> None:
    meta = {
        "contract_version": "1.0",
        "policy_version": "views@1.0",
        "shader_hash": "cpu:pillow/1",
        "backend": "cpu",
        "device_tier": "cpu",
        "grid_rows": 1,
    }
    common = {
        "schema_version": "1.0",
        "kind": "contact_sheet",
        "media_sha256_16": "a" * 16,
        "tile_map_sha": "b" * 64,
        "measurements_sha": "c" * 64,
        "image_bytes_sha": "d" * 64,
    }
    first = compute_render_hash(meta=meta, **common)
    other_backend = compute_render_hash(
        meta={**meta, "backend": "qtgl", "device_tier": "gpu"},
        **common,
    )
    assert first == other_backend
    changed = compute_render_hash(
        meta={**meta, "shader_hash": "cpu:pillow/2"},
        **common,
    )
    assert changed != first
    assert stable_meta(meta) == {
        key: value
        for key, value in meta.items()
        if key not in {"backend", "device_tier"}
    }


def test_validate_meta_rejects_missing_constants() -> None:
    with pytest.raises(ValueError):
        validate_meta({"contract_version": "1.0"})


def test_evidence_id_is_deterministic() -> None:
    first = evidence_id("p_123", "contact_sheet", (_rt("0"), _rt("10")))
    second = evidence_id("p_123", "contact_sheet", (_rt("0"), _rt("10")))
    assert first == second
    assert first.startswith("ve_p_123_contact_sheet_")
    assert evidence_id("p_123", "contact_sheet", (_rt("0"), _rt("11"))) != first


def test_measurements_hash_changes_with_score() -> None:
    left = [Measurement(tile_index=1, score=0.5)]
    right = [Measurement(tile_index=1, score=0.6)]
    assert measurements_hash(left) != measurements_hash(right)


def test_view_request_defaults_are_bounded() -> None:
    request = ViewRequest(content_id="p", source_range=(_rt("0"), _rt("1")))
    assert request.top_k == 7
    assert request.grid_cols == 4
    assert request.label_format == "T%02d"
