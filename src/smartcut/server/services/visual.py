"""视觉摘要：图片选择、缓存、VLM 调用与降级。"""

from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from smartcut.compiler import lower, validate_ir
from smartcut.compiler.audio_mix import resolve_mix_tracks
from smartcut.compiler.captions import build_cues, format_srt, format_vtt
from smartcut.compiler.loudness import parse_loudnorm_measure, parse_target_lufs
from smartcut.compiler.diff import diff_ir
from smartcut.config import Settings
from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.chapters import Chapter, ChapterPlan, OutlineApproval
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.faces import FaceReport
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.perception import PerceptionData
from smartcut.contracts.quality import QualityReport
from smartcut.contracts.review import EditReview, RenderReview, ReviewIssue
from smartcut.contracts.shots import ShotList
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.time import RationalTime, seconds_from_mapping
from smartcut.contracts.tools import ToolCall, ToolResult
from smartcut.contracts.transcode import (
    AudioOutput,
    TranscodeProfile,
    TranscodeRequest,
)
from smartcut.contracts.transcript import Transcript
from smartcut.contracts.vision import (
    ChapterVisualNote,
    ImageVisualNote,
    ImageVisualNotes,
    VisualNotes,
)
from smartcut.errors import (
    IRValidationError,
    PlannerUnavailable,
    ResourceNotFound,
    RunCancelledError,
    ToolExecutionError,
    UnsupportedFeature,
    VerificationError,
)
from smartcut.llm.singleflight import SingleFlight
from smartcut.llm.vision import build_vision_client
from smartcut.logging import get_logger
from smartcut.orchestrator import build_orchestrator
from smartcut.orchestrator.context import accepts_keyword
from smartcut.perception import candidates as candidates_mod
from smartcut.perception import chapters as chapters_mod
from smartcut.perception import chunking
from smartcut.perception import contactsheet as contactsheet_mod
from smartcut.perception import faces as faces_mod
from smartcut.perception import frames as frames_mod
from smartcut.perception import manifest as manifest_mod
from smartcut.perception import process_pool
from smartcut.perception import quality as quality_mod
from smartcut.perception import scenes as scenes_mod
from smartcut.perception.transcribe import Transcriber, build_transcriber
from smartcut.planner.client import DeepSeekPlanner
from smartcut.planner.context import filter_transcript_to_chapters
from smartcut.planner.vision import (
    VisionImage,
    build_image_note_messages,
    chapters_needing_vision,
    select_evidence_images,
    select_images,
)
from smartcut.policy import load_policy
from smartcut.resources import ResourceManager
from smartcut.runner.decoders import HardwareDecodeSelector
from smartcut.runner.encoders import EncoderSelector, profile_from_policy
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.separation import (
    available as demucs_available,
    build_separator,
)
from smartcut.runner.timeouts import compute_tool_timeout
from smartcut.runner.tools.cut import CutArgs
from smartcut.server.cache import ArtifactCache, ArtifactLLMCache
from smartcut.server.candidate_index import CandidateIndex
from smartcut.server.db import (
    Artifact,
    Database,
    Job,
    MediaAsset,
    OrchestrationRun,
    OrchestrationTaskOutcome,
    OutlineApprovalRecord,
    Project,
    ProjectAudioAsset,
    Revision,
    ToolCallRecord,
    utcnow,
)
from smartcut.server.jobs import JobReporter, JobService
from smartcut.server.templates import TemplateStore
from smartcut.workspace import WorkspaceManager

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_CHUNK = 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = _SAFE_FILENAME.sub("_", base).strip("._")
    return cleaned or "input.mp4"



@dataclass(frozen=True)
class _VisionContext:
    project_id: str
    goal: str
    workspace: Any
    cache: Any
    source_hash: str
    perception: dict
    cancel_token: Any
    usage_scope: str | None
    token_budget: int | None
    transcript: Any | None = None
    evidence: dict | None = None


class VisualMixin:
    def build_visual_notes(
        self,
        project_id: str,
        goal: str,
        chapters: dict | None,
        chapter_ids: list[str] | None = None,
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> dict | None:
        started = time.perf_counter()
        self._set_vision_metrics(
            {
                "images": 0,
                "image_bytes": 0,
                "requests": 0,
                "cache_hits": 0,
                "skipped_chapters": 0,
                "duration_ms": 0,
                "available": False,
            }
        )
        if not self.vision.available or not chapters:
            return None
        items = self._select_vision_chapters(chapters, chapter_ids)
        if not items:
            return None
        vision_items, skipped = self._text_first_chapters(items)
        ctx = self._vision_context(
            project_id,
            goal,
            cancel_token,
            usage_scope,
            token_budget,
            chapter_ids=chapter_ids,
        )
        key = self._vision_notes_key(ctx, vision_items, skipped)
        cached = self._cached_visual_notes(ctx, key, skipped, started)
        if cached is not None:
            return cached

        images = self._vision_images(ctx, vision_items)
        if not images:
            self._set_vision_unavailable(started, skipped, "no_images")
            return None

        image_notes, missing_count, usage = self._collect_image_notes(
            ctx, vision_items, images
        )
        chapter_notes = self._merge_chapter_notes(vision_items, images, image_notes)
        (
            second_pass_images,
            deep_images,
            second_pass_chapters,
            usage,
        ) = self._second_pass_notes(ctx, chapter_notes, vision_items, usage)
        overall = self._overall_summary(images, image_notes, chapter_notes)
        return self._finish_visual_notes(
            ctx=ctx,
            key=key,
            started=started,
            skipped=skipped,
            images=images,
            deep_images=deep_images,
            chapter_notes=chapter_notes,
            overall=overall,
            missing_count=missing_count,
            second_pass_images=second_pass_images,
            second_pass_chapters=second_pass_chapters,
            usage=usage,
        )

    def _select_vision_chapters(
        self,
        chapters: dict,
        chapter_ids: list[str] | None,
    ) -> list[dict]:
        items = list(chapters.get("chapters") or [])
        if chapter_ids:
            by_id = {item.get("id"): item for item in items}
            items = [by_id[item] for item in chapter_ids if item in by_id]
        return items

    def _text_first_chapters(self, items: list[dict]) -> tuple[list[dict], int]:
        if not self.settings.vision_text_first:
            return items, 0
        vision_items = chapters_needing_vision(
            items,
            min_chars=self.settings.vision_text_min_chars,
        )
        return vision_items, len(items) - len(vision_items)

    def _vision_context(
        self,
        project_id: str,
        goal: str,
        cancel_token: Any | None,
        usage_scope: str | None,
        token_budget: int | None,
        *,
        chapter_ids: list[str] | None = None,
    ) -> _VisionContext:
        asset = self._require_asset(project_id)
        evidence = None
        if self.settings.vision_use_evidence and self.settings.view_evidence_enabled:
            try:
                evidence = self.build_visual_evidence(
                    project_id,
                    chapter_ids=chapter_ids,
                    cancel_token=cancel_token,
                )
            except Exception as exc:
                self.logger.warning(
                    "visual_evidence_unavailable",
                    project_id=project_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                evidence = None
        return _VisionContext(
            project_id=project_id,
            goal=goal,
            workspace=self.workspaces.get(project_id),
            cache=self._cache_for(project_id),
            source_hash=self._source_hash(project_id, asset),
            perception=self.get_perception(project_id),
            cancel_token=cancel_token,
            usage_scope=usage_scope,
            token_budget=token_budget,
            transcript=self.get_transcript(project_id),
            evidence=evidence if (evidence or {}).get("available") else None,
        )

    def _vision_images(
        self,
        ctx: _VisionContext,
        vision_items: list[dict],
    ) -> list[VisionImage]:
        if ctx.evidence is not None:
            evidence_images = select_evidence_images(
                ctx.workspace,
                ctx.evidence,
                self.settings,
            )
            if evidence_images:
                return evidence_images
        return select_images(
            ctx.workspace,
            ctx.perception,
            vision_items,
            self.settings,
        )

    def _vision_notes_key(
        self,
        ctx: _VisionContext,
        vision_items: list[dict],
        skipped: int,
    ) -> str:
        image_paths = [
            (str(item.get("id")), [str(path) for path in item.get("keyframes") or []])
            for item in vision_items
        ]
        evidence = (ctx.evidence or {}).get("evidence") or {}
        evidence_sig = (
            evidence.get("id"),
            evidence.get("render_hash"),
            evidence.get("semantic_hash"),
        )
        transcript_sig = (
            ctx.cache.key(ctx.transcript.model_dump_json())
            if ctx.transcript is not None
            else "none"
        )
        return ctx.cache.key(
            ctx.source_hash,
            "vision_notes_v5",
            self.settings.vision_base_url,
            self.settings.vision_model,
            self.settings.vision_prompt_version,
            # 影响选帧/缩放的开关也必须进键，否则改配置会命中旧结论。
            self.settings.vision_text_first,
            self.settings.vision_text_min_chars,
            self.settings.vision_chapter_tiles,
            self.settings.vision_max_keyframes_per_chapter,
            self.settings.vision_image_max_edge,
            self.settings.vision_use_evidence,
            evidence_sig,
            transcript_sig,
            ctx.goal,
            image_paths,
            ctx.perception.get("contact_sheet"),
            skipped,
        )

    def _cached_visual_notes(
        self,
        ctx: _VisionContext,
        key: str,
        skipped: int,
        started: float,
    ) -> dict | None:
        cached = ctx.cache.get_json(
            ctx.project_id, "vision_notes", key, ctx.source_hash
        )
        if cached is None:
            return None
        self._set_vision_metrics(
            {
                "images": int(cached.get("image_count") or 0),
                "image_bytes": 0,
                "requests": 0,
                "cache_hits": 1,
                "skipped_chapters": skipped,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "available": True,
                "from_cache": True,
            }
        )
        return cached

    def _set_vision_unavailable(
        self,
        started: float,
        skipped: int,
        reason: str,
    ) -> None:
        self._set_vision_metrics(
            {
                "images": 0,
                "image_bytes": 0,
                "requests": 0,
                "cache_hits": 0,
                "skipped_chapters": skipped,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "available": False,
                "reason": reason,
            }
        )

    def _collect_image_notes(
        self,
        ctx: _VisionContext,
        vision_items: list[dict],
        images: list[VisionImage],
    ) -> tuple[dict[str, ImageVisualNote], int, dict[str, int]]:
        def image_key(image: VisionImage) -> str:
            return ctx.cache.key(
                "vision_image_v2",
                image.sha256,
                self.settings.vision_base_url,
                self.settings.vision_model,
                self.settings.vision_prompt_version,
                ctx.goal,
                image.chapter_id,
                image.label,
                image.evidence_id,
                image.tile_metadata,
                (
                    ctx.cache.key(ctx.transcript.model_dump_json())
                    if ctx.transcript is not None
                    else "none"
                ),
            )

        def compute() -> tuple[dict[str, ImageVisualNote], int]:
            image_notes: dict[str, ImageVisualNote] = {}
            missing: list[VisionImage] = []
            for image in images:
                cached_note = ctx.cache.get_json(
                    ctx.project_id,
                    "vision_image",
                    image_key(image),
                    ctx.source_hash,
                )
                if cached_note is not None:
                    image_notes[image.sha256] = ImageVisualNote.model_validate(
                        cached_note
                    )
                else:
                    missing.append(image)

            if missing:
                notes = self._vision_analyze(
                    build_image_note_messages(
                        ctx.goal,
                        vision_items,
                        missing,
                        transcript=ctx.transcript,
                    ),
                    ImageVisualNotes,
                    cancel_token=ctx.cancel_token,
                    usage_scope=ctx.usage_scope,
                    token_budget=ctx.token_budget,
                )
                self._validate_evidence_refs(ctx, notes.images)
                by_index = {item.image_index: item for item in notes.images}
                for index, image in enumerate(missing):
                    note = by_index.get(index)
                    if note is None:
                        continue
                    image_notes[image.sha256] = note
                    ctx.cache.put_json(
                        ctx.project_id,
                        "vision_image",
                        image_key(image),
                        ImageVisualNote(
                            image_index=0,
                            summary=note.summary,
                            visual_tags=note.visual_tags,
                            cautions=note.cautions,
                            evidence_refs=note.evidence_refs,
                        ).model_dump(mode="json"),
                        source_hash=ctx.source_hash,
                    )
            return image_notes, len(missing)

        # 同一图片批次 + prompt 的并发请求只执行一次。
        batch_key = ctx.cache.key(
            "vision_batch_v1",
            self.settings.vision_base_url,
            self.settings.vision_model,
            self.settings.vision_prompt_version,
            ctx.goal,
            [(image.sha256, image.chapter_id, image.label) for image in images],
        )
        usage_before = self._usage_snapshot(self.vision)
        image_notes, missing_count = self._vision_flights.run(batch_key, compute)
        usage = self._usage_delta(usage_before, self._usage_snapshot(self.vision))
        return image_notes, missing_count, usage

    def _merge_chapter_notes(
        self,
        vision_items: list[dict],
        images: list[VisionImage],
        image_notes: dict[str, ImageVisualNote],
    ) -> list[ChapterVisualNote]:
        chapter_notes: list[ChapterVisualNote] = []
        for chapter in vision_items:
            chapter_id = str(chapter.get("id") or "")
            parts = [
                note
                for image in images
                if (note := self._note_for_chapter(
                    image,
                    image_notes.get(image.sha256),
                    chapter,
                ))
                is not None
            ]
            if not parts:
                continue
            chapter_notes.append(
                ChapterVisualNote(
                    chapter_id=chapter_id,
                    summary="；".join(self._unique([item.summary for item in parts], 3)),
                    visual_tags=self._unique(
                        [tag for item in parts for tag in item.visual_tags],
                        5,
                    ),
                    cautions=self._unique(
                        [caution for item in parts for caution in item.cautions],
                        3,
                    ),
                    evidence_refs=self._unique(
                        [ref for item in parts for ref in item.evidence_refs],
                        8,
                    ),
                )
            )
        return chapter_notes

    def _note_for_chapter(
        self,
        image: VisionImage,
        note: ImageVisualNote | None,
        chapter: dict,
    ) -> ImageVisualNote | None:
        if note is None:
            return None
        chapter_id = str(chapter.get("id") or "")
        if image.chapter_id:
            return note if image.chapter_id == chapter_id else None
        if not image.evidence_id:
            return None
        start = seconds_from_mapping(chapter.get("start"))
        end = seconds_from_mapping(chapter.get("end"))
        if start is None or end is None:
            return note if note.evidence_refs else None
        labels = {
            str(tile.get("label"))
            for tile in image.tile_metadata
            if tile.get("t_seconds") is not None
            and start <= float(tile["t_seconds"]) < end
        }
        if not labels:
            return None
        refs = [ref for ref in note.evidence_refs if ref in labels]
        return note.model_copy(update={"evidence_refs": refs})

    def _allowed_evidence_labels(self, ctx: _VisionContext) -> set[str]:
        evidence = (ctx.evidence or {}).get("evidence") or {}
        tiles = ((evidence.get("tile_map") or {}).get("tiles")) or []
        return {str(tile.get("label")) for tile in tiles if tile.get("label")}

    def _validate_evidence_refs(
        self,
        ctx: _VisionContext,
        notes: list[ImageVisualNote],
    ) -> None:
        if ctx.evidence is None:
            return
        allowed = self._allowed_evidence_labels(ctx)
        for note in notes:
            if not note.evidence_refs:
                continue
            unknown = set(note.evidence_refs) - allowed
            if unknown:
                raise ValueError(
                    f"VLM 引用了不存在的证据标签: {sorted(unknown)}"
                )

    def _second_pass_notes(
        self,
        ctx: _VisionContext,
        chapter_notes: list[ChapterVisualNote],
        vision_items: list[dict],
        usage: dict[str, int],
    ) -> tuple[int, list[VisionImage], int, dict[str, int]]:
        second_pass_images = 0
        second_pass_chapters = 0
        deep_images: list[VisionImage] = []
        threshold = self.settings.vision_second_pass_cautions
        flagged_ids = {
            note.chapter_id
            for note in chapter_notes
            if not note.summary.strip() or len(note.cautions) >= threshold
        }
        if not flagged_ids:
            return second_pass_images, deep_images, second_pass_chapters, usage

        chapter_map = {str(item.get("id")): item for item in vision_items}
        flagged_items = [
            chapter_map[chapter_id]
            for chapter_id in flagged_ids
            if chapter_id in chapter_map
        ]
        deep_images = self._vision_images(ctx, flagged_items)
        flagged_images = [
            VisionImage(
                label=f"复核：{image.label}",
                mime=image.mime,
                data_url=image.data_url,
                sha256=image.sha256,
                size=image.size,
                chapter_id=image.chapter_id,
                evidence_id=image.evidence_id,
                tile_metadata=image.tile_metadata,
            )
            for image in deep_images
        ]
        if not flagged_images:
            return second_pass_images, deep_images, second_pass_chapters, usage

        def deep_key(image: VisionImage) -> str:
            return ctx.cache.key(
                "vision_image_deep_v2",
                image.sha256,
                self.settings.vision_base_url,
                self.settings.vision_model,
                self.settings.vision_prompt_version,
                ctx.goal,
                image.chapter_id,
                image.evidence_id,
                image.tile_metadata,
                (
                    ctx.cache.key(ctx.transcript.model_dump_json())
                    if ctx.transcript is not None
                    else "none"
                ),
            )

        def compute_deep() -> dict[str, ImageVisualNote]:
            deep_notes: dict[str, ImageVisualNote] = {}
            missing: list[VisionImage] = []
            for image in flagged_images:
                cached_note = ctx.cache.get_json(
                    ctx.project_id,
                    "vision_image_deep",
                    deep_key(image),
                    ctx.source_hash,
                )
                if cached_note is not None:
                    deep_notes[image.sha256] = ImageVisualNote.model_validate(
                        cached_note
                    )
                else:
                    missing.append(image)
            if missing:
                notes = self._vision_analyze(
                    build_image_note_messages(
                        ctx.goal + "；请复核低置信度章节的画面细节与风险",
                        flagged_items,
                        missing,
                        transcript=ctx.transcript,
                    ),
                    ImageVisualNotes,
                    cancel_token=ctx.cancel_token,
                    usage_scope=ctx.usage_scope,
                    token_budget=ctx.token_budget,
                )
                self._validate_evidence_refs(ctx, notes.images)
                by_index = {item.image_index: item for item in notes.images}
                for index, image in enumerate(missing):
                    note = by_index.get(index)
                    if note is None:
                        continue
                    deep_notes[image.sha256] = note
                    ctx.cache.put_json(
                        ctx.project_id,
                        "vision_image_deep",
                        deep_key(image),
                        ImageVisualNote(
                            image_index=0,
                            summary=note.summary,
                            visual_tags=note.visual_tags,
                            cautions=note.cautions,
                            evidence_refs=note.evidence_refs,
                        ).model_dump(mode="json"),
                        source_hash=ctx.source_hash,
                    )
            return deep_notes

        deep_batch_key = ctx.cache.key(
            "vision_batch_deep_v1",
            self.settings.vision_base_url,
            self.settings.vision_model,
            self.settings.vision_prompt_version,
            ctx.goal,
            [
                (
                    image.sha256,
                    image.chapter_id,
                    image.evidence_id,
                    image.tile_metadata,
                )
                for image in flagged_images
            ],
            (
                ctx.cache.key(ctx.transcript.model_dump_json())
                if ctx.transcript is not None
                else "none"
            ),
        )
        deep_before = self._usage_snapshot(self.vision)
        deep_notes = self._vision_flights.run(deep_batch_key, compute_deep)
        second_usage = self._usage_delta(
            deep_before,
            self._usage_snapshot(self.vision),
        )
        usage = self._usage_add(usage, second_usage)
        by_chapter: dict[str, list[ImageVisualNote]] = {}
        for note in chapter_notes:
            chapter = chapter_map.get(note.chapter_id)
            if chapter is None:
                continue
            for image in flagged_images:
                filtered = self._note_for_chapter(
                    image,
                    deep_notes.get(image.sha256),
                    chapter,
                )
                if filtered is not None:
                    by_chapter.setdefault(note.chapter_id, []).append(filtered)
        second_pass_chapters = len(by_chapter)
        for note in chapter_notes:
            parts = by_chapter.get(note.chapter_id)
            if not parts:
                continue
            note.summary = "；".join(
                self._unique([note.summary, *[part.summary for part in parts]], 3)
            )
            note.visual_tags = self._unique(
                [*note.visual_tags, *[tag for part in parts for tag in part.visual_tags]],
                5,
            )
            note.cautions = self._unique(
                [
                    *note.cautions,
                    *[item for part in parts for item in part.cautions],
                ],
                3,
            )
            note.evidence_refs = self._unique(
                [*note.evidence_refs, *[ref for part in parts for ref in part.evidence_refs]],
                8,
            )
        second_pass_images = len(flagged_images)
        return second_pass_images, deep_images, second_pass_chapters, usage

    def _overall_summary(
        self,
        images: list[VisionImage],
        image_notes: dict[str, ImageVisualNote],
        chapter_notes: list[ChapterVisualNote],
    ) -> str:
        overall_parts = [
            image_notes[image.sha256].summary
            for image in images
            if image.chapter_id is None and image.sha256 in image_notes
        ]
        return (
            "；".join(self._unique(overall_parts, 2))
            or "；".join(item.summary for item in chapter_notes[:3] if item.summary)
        )

    def _finish_visual_notes(
        self,
        *,
        ctx: _VisionContext,
        key: str,
        started: float,
        skipped: int,
        images: list[VisionImage],
        deep_images: list[VisionImage],
        chapter_notes: list[ChapterVisualNote],
        overall: str,
        missing_count: int,
        second_pass_images: int,
        second_pass_chapters: int,
        usage: dict[str, int],
    ) -> dict | None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if not chapter_notes and not overall:
            self._set_vision_metrics(
                {
                    "images": len(images),
                    "image_bytes": sum(image.size for image in images),
                    "requests": usage["requests"],
                    "cache_hits": usage["cache_hits"],
                    "skipped_chapters": skipped,
                    "duration_ms": duration_ms,
                    "available": False,
                    "reason": "empty_notes",
                }
            )
            return None

        total_images = len(images) + second_pass_images
        evidence = (ctx.evidence or {}).get("evidence") or {}
        evidence_ids = [str(evidence.get("id"))] if evidence.get("id") else []
        payload = VisualNotes(
            chapters=chapter_notes,
            overall=overall,
            model=self.vision.model,
            image_count=total_images,
            evidence_ids=evidence_ids,
        ).model_dump(mode="json")
        image_bytes = sum(image.size for image in images) + sum(
            image.size for image in deep_images
        )
        ctx.cache.put_json(
            ctx.project_id,
            "vision_notes",
            key,
            payload,
            source_hash=ctx.source_hash,
            meta={
                "images": total_images,
                "image_bytes": image_bytes,
                "skipped_chapters": skipped,
                "missing_images": missing_count,
                "evidence_used": bool(evidence_ids),
                "evidence_refs": sum(
                    len(note.evidence_refs) for note in chapter_notes
                ),
                "second_pass_images": second_pass_images,
                "second_pass_chapters": second_pass_chapters,
                "duration_ms": duration_ms,
                "model": self.vision.model,
            },
        )
        self._set_vision_metrics(
            {
                "images": total_images,
                "image_bytes": image_bytes,
                "requests": usage["requests"],
                "cache_hits": usage["cache_hits"],
                "missing_images": missing_count,
                "skipped_chapters": skipped,
                "evidence_used": bool(evidence_ids),
                "evidence_refs": sum(
                    len(note.evidence_refs) for note in chapter_notes
                ),
                "second_pass_images": second_pass_images,
                "second_pass_chapters": second_pass_chapters,
                "duration_ms": duration_ms,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "total_tokens": usage["total_tokens"],
                "available": True,
                "from_cache": False,
            }
        )
        return payload

    @staticmethod
    def _unique(values: list[str], limit: int) -> list[str]:
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    def _vision_analyze(
        self,
        messages: list[dict],
        schema: Any,
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> Any:
        """兼容未声明新参数的自定义/测试视觉后端。"""
        kwargs: dict[str, Any] = {}
        if cancel_token is not None and accepts_keyword(
            self.vision.analyze, "cancel_token"
        ):
            kwargs["cancel_token"] = cancel_token
        if usage_scope is not None and accepts_keyword(
            self.vision.analyze, "usage_scope"
        ):
            kwargs["usage_scope"] = usage_scope
        if token_budget is not None and accepts_keyword(
            self.vision.analyze, "token_budget"
        ):
            kwargs["token_budget"] = token_budget
        return self.vision.analyze(messages, schema, **kwargs)

    def _set_vision_metrics(self, metrics: dict) -> None:
        self._vision_metrics.value = metrics

    def _take_vision_metrics(self) -> dict:
        value = getattr(self._vision_metrics, "value", None)
        self._vision_metrics.value = None
        return value or {}
