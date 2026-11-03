"""从 Pydantic 模型导出 JSON Schema，作为单一事实源的下游产物。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from smartcut.contracts.audio import AudioAssetRef, AudioTrackSpec
from smartcut.contracts.candidates import CandidateList
from smartcut.contracts.chapters import ChapterPlan, OutlineApproval
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import (
    BlurEffect,
    ColorEffect,
    FadeEffect,
    FitEffect,
    SharpenEffect,
    VignetteEffect,
    ZoomEffect,
)
from smartcut.contracts.encoder import EncoderProfile
from smartcut.contracts.patch import EditPatch
from smartcut.contracts.manifest import VideoManifest
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.memory import VideoMemoryIndex
from smartcut.contracts.review import EditReview, RenderReview
from smartcut.contracts.shots import ShotList
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.timeline import TimelineMemory
from smartcut.contracts.overlay_assets import OverlayAssetRef
from smartcut.contracts.timeline_ops import (
    OverlaySpec,
    TransitionSpec,
    WatermarkRemovalSpec,
)
from smartcut.contracts.tools import ToolCall
from smartcut.contracts.transcode import TranscodeProfile, TranscodeRequest
from smartcut.contracts.transcript import Transcript
from smartcut.contracts.vision import ImageVisualNotes, VisualNotes

MODELS = {
    "audio_asset": AudioAssetRef,
    "audio_track": AudioTrackSpec,
    "candidate_list": CandidateList,
    "chapter_plan": ChapterPlan,
    "edit_ir": EditIR,
    "edit_patch": EditPatch,
    "edit_review": EditReview,
    "effect_blur": BlurEffect,
    "effect_color": ColorEffect,
    "effect_fade": FadeEffect,
    "effect_fit": FitEffect,
    "effect_sharpen": SharpenEffect,
    "effect_vignette": VignetteEffect,
    "effect_zoom": ZoomEffect,
    "edit_template": EditTemplate,
    "encoder_profile": EncoderProfile,
    "image_visual_notes": ImageVisualNotes,
    "media_facts": MediaFacts,
    "outline_approval": OutlineApproval,
    "overlay_asset": OverlayAssetRef,
    "overlay_spec": OverlaySpec,
    "render_review": RenderReview,
    "transition_spec": TransitionSpec,
    "watermark_removal": WatermarkRemovalSpec,
    "transcript": Transcript,
    "shot_list": ShotList,
    "timeline_memory": TimelineMemory,
    "tool_call": ToolCall,
    "transcode_profile": TranscodeProfile,
    "transcode_request": TranscodeRequest,
    "video_manifest": VideoManifest,
    "video_memory": VideoMemoryIndex,
    "visual_notes": VisualNotes,
}


def build_schemas() -> dict[str, dict]:
    return {name: model.model_json_schema(by_alias=True) for name, model in MODELS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 SmartCut 契约 JSON Schema")
    parser.add_argument("--json-schema", action="store_true", help="输出到 stdout")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出目录；每个契约写一个 .json 文件（默认 stdout 输出 edit_ir）",
    )
    args = parser.parse_args(argv)

    schemas = build_schemas()
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, schema in schemas.items():
            (args.out / f"{name}.schema.json").write_text(
                json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return 0

    payload = schemas["edit_ir"] if not args.json_schema else schemas
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
