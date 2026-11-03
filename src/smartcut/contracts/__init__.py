"""全系统唯一契约事实源。

Pydantic 模型在这里定义；JSON Schema、前端类型、工具参数均从这些模型派生。
"""

from smartcut.contracts.audio import (
    AudioAsset,
    AudioAssetRef,
    AudioTrack,
    AudioTrackSpec,
)
from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.captions import CaptionCue
from smartcut.contracts.chapters import Chapter, ChapterPlan, OutlineApproval
from smartcut.contracts.effects import (
    BlurEffect,
    ColorEffect,
    Effect,
    EffectSpec,
    FadeEffect,
    FitEffect,
    SharpenEffect,
    VignetteEffect,
    ZoomEffect,
)
from smartcut.contracts.edit_ir import AudioSpec, EditIR, ExportSpec, TimeRange, TrimOp
from smartcut.contracts.overlay_assets import OverlayAssetRef
from smartcut.contracts.timeline_ops import (
    OverlaySpec,
    TransitionSpec,
    WatermarkRemovalSpec,
)
from smartcut.contracts.encoder import ENCODER_CODECS, EncoderProfile
from smartcut.contracts.faces import FaceAnchor, FaceReport
from smartcut.contracts.history import RunDigest
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.memory import MemoryChapter, MemoryShot, VideoMemoryIndex
from smartcut.contracts.patch import EditPatch, PatchOp
from smartcut.contracts.perception import FrameRef, PerceptionData
from smartcut.contracts.preference import EditPreference, PreferenceStats
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.review import EditReview, RenderReview, ReviewIssue
from smartcut.contracts.templates import EditTemplate
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime, fps_from_string
from smartcut.contracts.timeline import (
    TimelineMemory,
    UsedRange,
    build_timeline_memory,
)
from smartcut.contracts.tools import ToolCall, ToolResult
from smartcut.contracts.transcode import (
    AudioOutput,
    MetadataOptions,
    SubtitleOutput,
    TranscodeProfile,
    TranscodeRequest,
)
from smartcut.contracts.transcript import Transcript, TranscriptSegment, Word
from smartcut.contracts.vision import (
    ChapterVisualNote,
    ImageVisualNote,
    ImageVisualNotes,
    VisualNotes,
)

__all__ = [
    "AudioAsset",
    "AudioAssetRef",
    "AudioOutput",
    "AudioSpec",
    "AudioTrack",
    "AudioTrackSpec",
    "BlurEffect",
    "Candidate",
    "CandidateList",
    "CaptionCue",
    "Chapter",
    "ChapterPlan",
    "ChapterVisualNote",
    "ColorEffect",
    "ENCODER_CODECS",
    "Effect",
    "EditIR",
    "EditPatch",
    "EditPreference",
    "EditReview",
    "EditTemplate",
    "EncoderProfile",
    "EffectSpec",
    "ExportSpec",
    "FaceAnchor",
    "FadeEffect",
    "FitEffect",
    "FaceReport",
    "FrameRef",
    "ImageVisualNote",
    "ImageVisualNotes",
    "MediaFacts",
    "MemoryChapter",
    "MemoryShot",
    "MetadataOptions",
    "OutlineApproval",
    "OverlayAssetRef",
    "OverlaySpec",
    "PatchOp",
    "PerceptionData",
    "PreferenceStats",
    "QualityKind",
    "QualityMark",
    "QualityReport",
    "RationalTime",
    "RenderReview",
    "ReviewIssue",
    "RunDigest",
    "ShotBoundary",
    "SharpenEffect",
    "ShotList",
    "StreamFacts",
    "SubtitleOutput",
    "TimeRange",
    "TimelineMemory",
    "TranscodeProfile",
    "TranscodeRequest",
    "ToolCall",
    "ToolResult",
    "Transcript",
    "TransitionSpec",
    "TranscriptSegment",
    "TrimOp",
    "UsedRange",
    "VignetteEffect",
    "VideoMemoryIndex",
    "VisualNotes",
    "WatermarkRemovalSpec",
    "Word",
    "ZoomEffect",
    "build_timeline_memory",
    "fps_from_string",
]
