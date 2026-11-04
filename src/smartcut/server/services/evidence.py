"""视觉证据服务：窗口抽帧、CPU 测量/渲染、原子缓存与 metrics。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from smartcut.contracts.faces import FaceReport
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.shots import ShotList
from smartcut.contracts.time import RationalTime, seconds_from_mapping
from smartcut.contracts.tools import ToolCall
from smartcut.contracts.visual_evidence import ViewRequest
from smartcut.errors import ToolExecutionError
from smartcut.perception.view.budget import view_budget
from smartcut.perception.view.measure import TileInput, measure_tiles
from smartcut.perception.view.render_cpu import CpuEvidenceRenderer
from smartcut.perception.view.select import select_evidence_windows
from smartcut.perception.view.synthesizer import ViewSynthesizer
from smartcut.perception.view.views import (
    BuiltView,
    build_bbox_overlay,
    build_flow_strip,
    build_roi,
    build_temporal_diff,
    build_unchanged,
)

_SHOWINFO_RE = re.compile(
    r"n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*(-?[0-9.]+)"
)
SUPPORTED_KINDS = {
    "contact_sheet",
    "temporal_diff",
    "flow_strip",
    "bbox_overlay",
    "roi_crop",
    "codec_residual",
}
FLOW_KINDS = {"temporal_diff", "flow_strip"}


@dataclass
class _EvidenceContext:
    project_id: str
    workspace: Any
    cache: Any
    source_hash: str
    facts: Any
    frames: list[Any]
    frames_key: str
    perception: dict
    chapters: dict | None
    transcript: Any


class EvidenceMixin:
    def build_visual_evidence(
        self,
        project_id: str,
        *,
        kind: str = "contact_sheet",
        chapter_ids: list[str] | None = None,
        cancel_token: Any | None = None,
        ir_revision: int | None = None,
        tile_index: int | None = None,
    ) -> dict:
        """生成可复现的证据视图；失败降级为 warning，不阻断主流程。"""
        if not self.settings.view_evidence_enabled:
            return {
                "available": False,
                "warning": "视觉证据层已关闭（SMARTCUT_VIEW_EVIDENCE_ENABLED=false）",
            }
        if kind not in SUPPORTED_KINDS:
            return {
                "available": False,
                "warning": f"不支持的证据视图: {kind!r}",
            }
        asset = self._require_asset(project_id)
        ctx = self._evidence_context(project_id, asset)
        if not ctx.frames:
            return {"available": False, "warning": "尚未抽帧，无法生成证据"}
        backend_hint = (
            "qtgl"
            if str(self.settings.view_renderer).lower() != "cpu"
            and self.view_renderer is not None
            else "cpu"
        )
        budget = view_budget(self.settings, backend_hint)
        silence = self._silence_ranges(ctx.transcript)

        selection = select_evidence_windows(
            ctx.frames,
            duration=ctx.facts.duration,
            shots=self._safe_model(ShotList, ctx.perception.get("shots")),
            quality=self._safe_model(QualityReport, ctx.perception.get("quality")),
            chapters=ctx.chapters,
            chapter_ids=chapter_ids,
            transcript=ctx.transcript,
            faces=self._safe_model(FaceReport, ctx.perception.get("faces")),
            max_images=budget.max_images,
            max_windows=self.settings.view_max_windows,
            fps=self.settings.view_fps,
            padding_seconds=self.settings.view_window_padding_seconds,
            max_window_seconds=self.settings.view_window_max_seconds,
        )
        key = self._evidence_key(
            ctx,
            kind,
            chapter_ids,
            selection,
            tile_index=tile_index,
            backend=backend_hint,
        )
        cached = ctx.cache.get(project_id, "evidence", key, ctx.source_hash)
        if cached is not None:
            try:
                payload = json.loads(
                    ctx.cache.resolve(cached["path"]).joinpath("evidence.json").read_text(
                        encoding="utf-8"
                    )
                )
                metrics = dict(cached.get("meta") or {})
                metrics["cache_hits"] = 1
                self._set_evidence_metrics(metrics)
                return {
                    "available": True,
                    "cached": True,
                    "key": key,
                    "kind": kind,
                    "path": cached["path"],
                    "image_ref": payload.get("image_ref"),
                    "evidence": payload,
                    "selection": selection.as_dict(),
                    "silence": silence,
                    "metrics": metrics,
                }
            except (OSError, ValueError):
                pass

        final_dir = ctx.cache.cache_dir("evidence", key)
        temp_dir = final_dir.with_name(f"{key}.part-{uuid4().hex[:8]}")
        started = time.perf_counter()
        extract_ms = 0
        measure_ms = 0
        render_ms = 0
        try:
            if final_dir.exists():
                shutil.rmtree(final_dir, ignore_errors=True)
            temp_dir.mkdir(parents=True, exist_ok=True)
            extract_started = time.perf_counter()
            tiles = self._collect_tiles(
                ctx,
                selection,
                temp_dir,
                cancel_token=cancel_token,
                codecview=kind == "codec_residual",
            )
            extract_ms = int((time.perf_counter() - extract_started) * 1000)
            if not tiles:
                tiles = self._fallback_tiles(ctx, selection)
            if not tiles:
                raise ValueError("没有可用的证据帧")
            tiles = self._trim_bytes(tiles, self.settings.view_total_max_bytes)
            label_start = 1

            if kind == "roi_crop":
                built = self._build_roi_view(
                    ctx,
                    selection,
                    tiles,
                    tile_index or 1,
                    temp_dir,
                    cancel_token=cancel_token,
                )
                label_format = self.settings.view_label_format
                label_start = max(1, tile_index or 1)
            elif kind == "temporal_diff":
                built = build_temporal_diff(
                    tiles,
                    tile_width=budget.tile_width,
                    tile_height=budget.tile_height,
                )
                label_format = self.settings.view_label_format
            elif kind == "flow_strip":
                built = build_flow_strip(
                    tiles,
                    tile_width=budget.tile_width,
                    tile_height=budget.tile_height,
                )
                label_format = self.settings.view_label_format
            elif kind == "bbox_overlay":
                built = build_bbox_overlay(tiles)
                label_format = self.settings.view_label_format
            else:
                built = build_unchanged(tiles)
                label_format = self.settings.view_label_format

            measure_started = time.perf_counter()
            faces = self._safe_model(FaceReport, ctx.perception.get("faces"))
            measure = measure_tiles(
                built.tiles,
                content_id=project_id,
                time_base=str(
                    getattr(ctx.facts.video, "time_base", None) or "1/1"
                ),
                vfr=bool(ctx.facts.vfr),
                grid_cols=self.settings.view_grid_cols,
                label_format=label_format,
                label_start=label_start,
                dedupe_distance=(
                    -1
                    if kind in FLOW_KINDS
                    else self.settings.view_dedupe_hash_distance
                ),
                quality_times=self._quality_times(ctx.perception),
                face_anchors=(
                    list(faces.anchors)
                    if faces is not None
                    and kind in {"contact_sheet", "bbox_overlay"}
                    else None
                ),
                cancel_check=(
                    cancel_token.raise_if_cancelled if cancel_token is not None else None
                ),
                extra_metrics=built.extra_metrics or None,
                engine="cpu-flow" if kind in FLOW_KINDS else "cpu",
            )
            measure.constants.update(built.constants)
            measure.constants.update(
                {
                    "fps_sample": self.settings.view_fps,
                    "dedupe_threshold": self.settings.view_dedupe_hash_distance,
                    "scene_threshold_pct": 0.0,
                }
            )
            measure_ms = int((time.perf_counter() - measure_started) * 1000)

            render_started = time.perf_counter()
            start_t = built.tiles[0].t
            end_t = built.tiles[-1].t
            if end_t <= start_t:
                end_t = start_t + RationalTime(value=1, rate=start_t.rate)
            request = ViewRequest(
                kind=kind,
                content_id=project_id,
                source_range=(start_t, end_t),
                top_k=budget.max_images,
                grid_cols=self.settings.view_grid_cols,
                tile_width=budget.tile_width,
                tile_height=budget.tile_height,
                label_format=label_format,
                policy_version=self.settings.view_policy_version,
            )
            image_ref = ctx.cache.relative(final_dir / "view.png")
            synthesized, renderer_fallback = self._synthesize_evidence(
                measure,
                request,
                source_ref={
                    "content_id": project_id,
                    "media_sha256_16": ctx.source_hash[:16],
                },
                image_ref=image_ref,
                ffmpeg_version="unknown",
                opencv_version=self._opencv_version(),
                ir_revision=ir_revision,
            )
            render_ms = int((time.perf_counter() - render_started) * 1000)
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            (temp_dir / "view.png").write_bytes(synthesized.png_bytes)
            (temp_dir / "evidence.json").write_text(
                json.dumps(
                    synthesized.evidence.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temp_dir, final_dir)
            metrics = {
                "extract_ms": extract_ms,
                "measure_ms": measure_ms,
                "render_ms": render_ms,
                "tiles": len(measure.tile_map.tiles),
                "windows": len(selection.windows),
                "estimated_tiles": selection.estimated_tiles,
                "estimated_tokens": selection.estimated_tokens,
                "dropped_windows": selection.dropped_windows,
                "image_bytes": len(synthesized.png_bytes),
                "backend": synthesized.rendered.backend,
                "kind": kind,
                "renderer_fallback": renderer_fallback,
                "cache_hits": 0,
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
            ctx.cache.put(
                project_id,
                "evidence",
                key,
                final_dir,
                source_hash=ctx.source_hash,
                meta=metrics,
            )
            self._set_evidence_metrics(metrics)
            return {
                "available": True,
                "cached": False,
                "key": key,
                "kind": kind,
                "path": ctx.cache.relative(final_dir),
                "image_ref": image_ref,
                "evidence": synthesized.evidence.model_dump(mode="json"),
                "selection": selection.as_dict(),
                "silence": silence,
                "metrics": metrics,
            }
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self.logger.warning(
                "visual_evidence_failed",
                project_id=project_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "available": False,
                "warning": f"证据生成失败: {type(exc).__name__}: {exc}",
                "selection": selection.as_dict(),
            }

    # ── 内部 ────────────────────────────────────────────────────────
    def _synthesize_evidence(
        self,
        measure: Any,
        request: Any,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        configured = str(self.settings.view_renderer or "auto").lower()
        renderer = None if configured == "cpu" else self.view_renderer
        if renderer is None:
            renderer = CpuEvidenceRenderer()
        try:
            synthesized = ViewSynthesizer(renderer).synthesize(
                measure,
                request,
                **kwargs,
            )
            return synthesized, False
        except Exception as exc:
            if getattr(renderer, "backend", "cpu") == "cpu":
                raise
            self.logger.warning(
                "evidence_gpu_render_fallback",
                backend=getattr(renderer, "backend", "unknown"),
                error=f"{type(exc).__name__}: {exc}",
            )
            # 同一 Services 实例后续直接走 CPU，避免每次任务重复失败。
            self.view_renderer = None
            fallback = CpuEvidenceRenderer()
            synthesized = ViewSynthesizer(fallback).synthesize(
                measure,
                request,
                **kwargs,
            )
            return synthesized, True

    def _evidence_context(self, project_id: str, asset: Any) -> _EvidenceContext:
        cache = self._cache_for(project_id)
        source_hash = self._source_hash(project_id, asset)
        facts = MediaFacts.model_validate_json(asset.facts_json)
        frames_record = cache.latest(project_id, "frames", source_hash)
        frames = []
        frames_key = "no-frames"
        if frames_record is not None:
            frames_key = frames_record["cache_key"]
            try:
                from smartcut.perception import frames as frames_mod

                frames = frames_mod.read_manifest(
                    cache.resolve(frames_record["path"]) / "manifest.json"
                )
            except (OSError, ValueError):
                frames = []
        return _EvidenceContext(
            project_id=project_id,
            workspace=self.workspaces.get(project_id),
            cache=cache,
            source_hash=source_hash,
            facts=facts,
            frames=frames,
            frames_key=frames_key,
            perception=self.get_perception(project_id),
            chapters=self.get_chapters(project_id),
            transcript=self.get_transcript(project_id),
        )

    @staticmethod
    def _safe_model(model, payload):
        if not payload:
            return None
        try:
            return model.model_validate(payload)
        except Exception:
            return None

    def _evidence_key(
        self,
        ctx: _EvidenceContext,
        kind: str,
        chapter_ids,
        selection,
        *,
        tile_index: int | None = None,
        backend: str = "cpu",
    ) -> str:
        transcript_sig = (
            ctx.cache.key(ctx.transcript.model_dump_json())
            if ctx.transcript is not None
            else "none"
        )
        return ctx.cache.key(
            ctx.source_hash,
            "evidence_v2",
            kind,
            tile_index,
            backend,
            self.settings.view_policy_version,
            ctx.frames_key,
            transcript_sig,
            sorted(chapter_ids or []),
            selection.signature(),
            self.settings.view_fps,
            self.settings.view_max_images,
            self.settings.view_max_windows,
            self.settings.view_window_padding_seconds,
            self.settings.view_window_max_seconds,
            self.settings.view_tile_width,
            self.settings.view_tile_height,
            self.settings.view_grid_cols,
            self.settings.view_label_format,
            self.settings.view_dedupe_hash_distance,
            self.settings.view_renderer,
        )

    def _build_roi_view(
        self,
        ctx: _EvidenceContext,
        selection,
        tiles: list[TileInput],
        tile_index: int,
        temp_dir: Path,
        *,
        cancel_token: Any | None,
    ) -> BuiltView:
        index = max(1, min(int(tile_index), len(tiles)))
        target = tiles[index - 1]
        rect: tuple[float, float, float, float] = (0.3, 0.2, 0.4, 0.6)
        faces = self._safe_model(FaceReport, ctx.perception.get("faces"))
        if faces is not None and faces.anchors:
            anchor = min(
                faces.anchors,
                key=lambda item: abs(item.t.to_float() - target.t.to_float()),
            )
            rect = (anchor.x, anchor.y, anchor.w, anchor.h)
        if not selection.windows:
            return build_roi(target, rect, target=512)
        window = min(
            selection.windows,
            key=lambda item: abs(float(item.start) - target.t.to_float()),
        )
        high = self._collect_tiles(
            ctx,
            SimpleNamespace(windows=[window]),
            temp_dir,
            cancel_token=cancel_token,
            width=1920,
            frame_dir="roi_frames",
        )
        source = (
            min(
                high,
                key=lambda item: abs(item.t.to_float() - target.t.to_float()),
            )
            if high
            else target
        )
        return build_roi(source, rect, target=512)

    @staticmethod
    def _silence_ranges(transcript: Any, min_seconds: float = 1.0) -> list[dict]:
        segments = sorted(
            getattr(transcript, "segments", []) or [],
            key=lambda segment: segment.start.to_float(),
        )
        gaps: list[dict] = []
        for previous, current in zip(segments, segments[1:]):
            start = previous.end.to_float()
            end = current.start.to_float()
            if end - start >= min_seconds:
                gaps.append(
                    {
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "kind": "silence",
                    }
                )
        return gaps[:200]

    def _collect_tiles(
        self,
        ctx: _EvidenceContext,
        selection,
        temp_dir: Path,
        *,
        cancel_token: Any | None,
        width: int | None = None,
        codecview: bool = False,
        frame_dir: str = "frames",
    ) -> list[TileInput]:
        executor = self._executor(ctx.workspace, cancel_token)
        tiles: list[TileInput] = []
        source_start = getattr(ctx.facts.video, "start_time", None)
        origin = source_start.to_float() if source_start is not None else 0.0
        source_fps = ctx.facts.fps_fraction
        for index, window in enumerate(selection.windows):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            dir_rel = ctx.cache.relative(
                temp_dir / frame_dir / f"w{index:02d}"
            )
            try:
                result = executor.execute(
                    ToolCall(
                        seq=index,
                        tool="extract_evidence_frames",
                        args={
                            "src": ctx.facts.src,
                            "start": RationalTime.from_fraction(window.start).model_dump(),
                            "end": RationalTime.from_fraction(window.end).model_dump(),
                            "source_start": (
                                source_start.model_dump()
                                if source_start is not None
                                else RationalTime(value=0, rate=90_000).model_dump()
                            ),
                            "fps": window.fps,
                            "width": width
                            or min(
                                1920,
                                max(
                                    self.settings.view_tile_width * 2,
                                    ctx.facts.width or 640,
                                ),
                            ),
                            "dst_dir": dir_rel,
                            "codecview": codecview,
                        },
                    ),
                    timeout_seconds=self.settings.tool_timeout_seconds,
                )
            except ToolExecutionError:
                continue
            tiles.extend(
                self._parse_extracted(
                    ctx,
                    ctx.workspace.resolve(dir_rel),
                    result.stderr,
                    window,
                    origin=origin,
                    source_fps=source_fps,
                )
            )
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
        return tiles

    def _parse_extracted(
        self,
        ctx: _EvidenceContext,
        directory: Path,
        stderr: str,
        window,
        *,
        origin: float,
        source_fps: Fraction | None,
    ) -> list[TileInput]:
        files = sorted(directory.glob("frame_*.jpg"))
        if not files:
            return []
        info = [
            (int(match.group(1)), int(match.group(2)), float(match.group(3)))
            for match in _SHOWINFO_RE.finditer(stderr or "")
        ]
        tiles: list[TileInput] = []
        for index, path in enumerate(files):
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            if index < len(info):
                _n, pts, pts_time = info[index]
                seconds = max(0.0, pts_time - origin)
                frame_no = (
                    int(round(seconds * float(source_fps)))
                    if source_fps is not None and not ctx.facts.vfr
                    else None
                )
            else:
                pts = 0
                seconds = float(window.start) + index / max(window.fps, 0.25)
                frame_no = None
            tiles.append(
                TileInput(
                    t=RationalTime.from_seconds(f"{seconds:.6f}", rate=90_000),
                    pts=int(pts),
                    frame_no=frame_no,
                    image_bytes=payload,
                )
            )
        return tiles

    def _fallback_tiles(self, ctx: _EvidenceContext, selection) -> list[TileInput]:
        tiles: list[TileInput] = []
        for window in selection.windows:
            candidates = sorted(
                ctx.frames,
                key=lambda frame: abs(frame.t.to_float() - float(window.start)),
            )
            for frame in candidates[: max(1, window.tiles())]:
                try:
                    payload = (ctx.workspace.root / frame.path).read_bytes()
                except OSError:
                    continue
                tiles.append(
                    TileInput(
                        t=frame.t,
                        pts=0,
                        frame_no=frame.index if not ctx.facts.vfr else None,
                        image_bytes=payload,
                        frame_ref=frame.path,
                    )
                )
        return tiles

    @staticmethod
    def _trim_bytes(tiles: list[TileInput], limit: int) -> list[TileInput]:
        if limit <= 0:
            return tiles
        selected: list[TileInput] = []
        total = 0
        for tile in tiles:
            if selected and total + len(tile.image_bytes) > limit:
                break
            selected.append(tile)
            total += len(tile.image_bytes)
        return selected

    @staticmethod
    def _quality_times(perception: dict) -> list[Fraction]:
        marks = ((perception or {}).get("quality") or {}).get("marks") or []
        times: list[Fraction] = []
        for mark in marks:
            if mark.get("kind") not in {"black", "frozen", "blurry", "duplicate"}:
                continue
            seconds = seconds_from_mapping(mark.get("t"))
            if seconds is not None:
                times.append(Fraction(str(seconds)))
        return times

    @staticmethod
    def _opencv_version() -> str:
        try:
            import cv2

            return str(cv2.__version__)
        except Exception:
            return "unavailable"

    def _set_evidence_metrics(self, metrics: dict) -> None:
        self._evidence_metrics.value = metrics

    def _take_evidence_metrics(self) -> dict:
        value = getattr(self._evidence_metrics, "value", None)
        self._evidence_metrics.value = None
        return value or {}
