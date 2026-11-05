"""证据合成：measure + render + 固定常量 → VisualEvidence + 哈希。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smartcut.contracts.visual_evidence import (
    SCHEMA_VERSION,
    MeasureResult,
    RenderedImage,
    ViewRequest,
    VisualEvidence,
    compute_render_hash,
    evidence_id,
    measurements_hash,
    stable_hash,
    tile_map_hash,
    validate_meta,
)
from smartcut.perception.view.hash import semantic_hash, sha256_bytes
from smartcut.perception.view.measure import grid_rows, mean_confidence


@dataclass
class SynthesizedEvidence:
    evidence: VisualEvidence
    png_bytes: bytes
    rendered: RenderedImage


class ViewSynthesizer:
    def __init__(self, renderer) -> None:
        self.renderer = renderer

    def synthesize(
        self,
        measure: MeasureResult,
        request: ViewRequest,
        *,
        source_ref: dict[str, Any],
        image_ref: str,
        ffmpeg_version: str = "unknown",
        opencv_version: str = "unknown",
        ir_revision: int | None = None,
    ) -> SynthesizedEvidence:
        rendered = self.renderer.render(measure, request)
        return self._build(
            measure,
            request,
            rendered,
            source_ref=source_ref,
            image_ref=image_ref,
            ffmpeg_version=ffmpeg_version,
            opencv_version=opencv_version,
            ir_revision=ir_revision,
        )

    def synthesize_many(
        self,
        measure: MeasureResult,
        requests: list[ViewRequest],
        *,
        source_ref: dict[str, Any],
        image_refs: list[str],
        ffmpeg_version: str = "unknown",
        opencv_version: str = "unknown",
        ir_revision: int | None = None,
    ) -> list[SynthesizedEvidence]:
        """一次渲染调用输出多个网格/kind；渲染器可复用 context 与解码图片。"""
        if len(image_refs) != len(requests):
            raise ValueError("image_refs 必须与 requests 等长")
        render_many = getattr(self.renderer, "render_many", None)
        rendered_items = (
            render_many(measure, requests)
            if callable(render_many)
            else [self.renderer.render(measure, request) for request in requests]
        )
        return [
            self._build(
                measure,
                request,
                rendered,
                source_ref=source_ref,
                image_ref=image_ref,
                ffmpeg_version=ffmpeg_version,
                opencv_version=opencv_version,
                ir_revision=ir_revision,
            )
            for request, rendered, image_ref in zip(
                requests, rendered_items, image_refs
            )
        ]

    def _build(
        self,
        measure: MeasureResult,
        request: ViewRequest,
        rendered: RenderedImage,
        *,
        source_ref: dict[str, Any],
        image_ref: str,
        ffmpeg_version: str,
        opencv_version: str,
        ir_revision: int | None,
    ) -> SynthesizedEvidence:
        rows = grid_rows(len(measure.tile_map.tiles), request.grid_cols)
        constants = measure.constants
        meta = {
            "contract_version": SCHEMA_VERSION,
            "policy_version": request.policy_version,
            "ffmpeg_version": ffmpeg_version,
            "opencv_version": opencv_version,
            "fingerprint_version": constants.get(
                "fingerprint_version", "unknown"
            ),
            "fingerprint_engine": constants.get("fingerprint_engine", "unknown"),
            "motion_split_version": constants.get(
                "motion_split_version", "none"
            ),
            "measure_backend": constants.get("measure_backend", "cpu"),
            "fps_sample": constants.get("fps_sample", 0.0),
            "dedup_threshold": constants.get("dedupe_threshold", 0),
            "scene_threshold_pct": constants.get("scene_threshold_pct", 0.0),
            "color_convention": "bt709/limited",
            "yuv_to_rgb_matrix_id": "bt709",
            "tile_label_format": request.label_format,
            "grid_rows": rows,
            "grid_cols": request.grid_cols,
            "tile_w": request.tile_width,
            "tile_h": request.tile_height,
            "pad_px": 4,
            "shader_hash": rendered.shader_hash,
            "backend": rendered.backend,
            "device_tier": rendered.device_tier,
        }
        validate_meta(meta)
        media_hash = str(source_ref.get("media_sha256_16") or "")[:16]
        map_sha = tile_map_hash(measure.tile_map)
        measure_sha = measurements_hash(measure.measurements)
        image_sha = sha256_bytes(rendered.png_bytes)
        render_digest = compute_render_hash(
            schema_version=SCHEMA_VERSION,
            kind=request.kind,
            media_sha256_16=media_hash,
            tile_map_sha=map_sha,
            measurements_sha=measure_sha,
            image_bytes_sha=image_sha,
            meta=meta,
        )
        semantic = semantic_hash(
            rendered.png_bytes,
            grid_rows=rows,
            grid_cols=request.grid_cols,
        )
        evidence = VisualEvidence(
            id=evidence_id(request.content_id, request.kind, request.source_range),
            kind=request.kind,
            source_range=request.source_range,
            source_ref=source_ref,
            tile_map=measure.tile_map,
            measurements=measure.measurements,
            image_ref=image_ref,
            meta=meta,
            confidence=round(mean_confidence(measure.measurements), 6),
            render_hash=render_digest,
            semantic_hash=semantic,
            ir_revision=ir_revision,
        )
        return SynthesizedEvidence(
            evidence=evidence,
            png_bytes=rendered.png_bytes,
            rendered=rendered,
        )


def evidence_signature(evidence: VisualEvidence) -> dict:
    """用于缓存键：排除生成时间、后端等非语义字段。"""
    return {
        "kind": evidence.kind,
        "source_range": evidence.source_range,
        "tile_labels": [tile.label for tile in evidence.tile_map.tiles],
        "tile_pts": [tile.pts for tile in evidence.tile_map.tiles],
        "render_hash": stable_hash(
            {
                "tile_map": evidence.tile_map,
                "measurements": evidence.measurements,
            }
        ),
    }
