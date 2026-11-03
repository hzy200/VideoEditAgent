"""子智能体共享上下文与工具协议。

Orchestrator 只依赖这个 Protocol，不直接依赖 server.Services，
从而保持“编排层 → 业务工具”的单向依赖。
"""

from __future__ import annotations

import functools
import inspect
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from smartcut.config import Settings
from smartcut.contracts.chapters import OutlineApproval
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.effects import Effect
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.timeline import TimelineMemory, build_timeline_memory
from smartcut.contracts.transcript import Transcript
from smartcut.contracts.workspace import Workspace


class CancellableToolkit:
    """按 run 取消 token 包裹 AgentToolkit；取消后拒绝新的工具调用。

    运行中的同步调用无法强制中断，但可以保证 agent 在取消后不再发起
    新的缓存/DB/渲染写入。
    """

    def __init__(self, inner: AgentToolkit, cancel_token: Any) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_token", cancel_token)

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        token = object.__getattribute__(self, "_token")
        attribute = getattr(inner, name)
        if not callable(attribute):
            return attribute

        @functools.wraps(attribute)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            if token is not None:
                token.raise_if_cancelled()
            result = attribute(*args, **kwargs)
            if token is not None:
                token.raise_if_cancelled()
            return result

        return guarded


def accepts_keyword(callable_obj: Any, name: str) -> bool:
    """兼容尚未声明新参数的自定义 planner/toolkit 实现。"""
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    if name in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


class AgentToolkit(Protocol):
    settings: Settings
    planner_available: bool

    def project_workspace(self, project_id: str) -> Workspace: ...

    def get_media_facts(self, project_id: str) -> MediaFacts: ...

    def get_transcript(self, project_id: str) -> Transcript | None: ...

    def transcribe(
        self,
        project_id: str,
        reporter: Any | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict: ...

    def extract_frames(
        self,
        project_id: str,
        fps: float = 1.0,
        cancel_token: Any | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict: ...

    def detect_scenes(self, project_id: str, cancel_token: Any | None = None) -> dict: ...

    def analyze_quality(
        self,
        project_id: str,
        cancel_token: Any | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict: ...

    def detect_faces(
        self,
        project_id: str,
        cancel_token: Any | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict: ...

    def build_contact_sheet(
        self, project_id: str, cancel_token: Any | None = None
    ) -> dict: ...

    def get_perception(self, project_id: str) -> dict: ...

    def build_chapters(self, project_id: str, cancel_token: Any | None = None) -> dict: ...

    def get_chapters(self, project_id: str) -> dict | None: ...

    def build_candidates(
        self,
        project_id: str,
        cancel_token: Any | None = None,
        chapter_ids: list[str] | None = None,
    ) -> dict: ...

    def get_candidates(self, project_id: str) -> dict | None: ...

    def retrieve_candidates(
        self,
        project_id: str,
        query: str,
        *,
        limit: int | None = None,
        reference_candidate_id: str | None = None,
        max_distance: int = 10,
    ) -> dict: ...

    def plan(
        self,
        project_id: str,
        goal: str,
        perception: dict | None = None,
        chapters: dict | None = None,
        candidates: dict | None = None,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        memory: Any | None = None,
        preference: Any | None = None,
        history: list[dict] | None = None,
        chapter_ids: list[str] | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        context_stats: dict | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> dict: ...

    def submit_revision(self, project_id: str, ir: EditIR) -> dict: ...

    def replan(
        self,
        project_id: str,
        revision_no: int,
        goal: str,
        review: dict | None = None,
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
        pinned_effects: list[Effect] | None = None,
    ) -> dict: ...

    def review_edit(
        self,
        project_id: str,
        revision_no: int,
        goal: str,
        visual_notes: dict | None = None,
        timeline: Any | None = None,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> dict: ...

    def dry_run(self, project_id: str, revision_no: int) -> dict: ...

    def render(self, project_id: str, revision_no: int, reporter: Any | None = None) -> dict: ...

    def probe(self, project_id: str, relative_src: str) -> MediaFacts: ...

    def llm_usage(self, scope: str) -> dict: ...


class _FrozenDict(dict):
    """只读 dict 视图；保留 dict 身份，便于 json/模型序列化。"""

    @staticmethod
    def _deny(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("共享状态是只读快照，请通过 AgentContext setter 更新")

    __setitem__ = _deny
    __delitem__ = _deny
    clear = _deny
    pop = _deny
    popitem = _deny
    setdefault = _deny
    update = _deny


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        frozen = _FrozenDict()
        dict.__init__(
            frozen,
            {key: _freeze(item) for key, item in value.items()},
        )
        return frozen
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SlotMeta:
    """共享槽位的来源信息：谁写的、什么时候写的、对应哪个缓存键。"""

    source: str
    produced_at: str
    cache_key: str | None = None

    def dump(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "at": self.produced_at,
            "cache_key": self.cache_key,
        }


def _extract_cache_key(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for name in ("cache_key", "key"):
        item = value.get(name)
        if isinstance(item, str) and item:
            return item
    return None


@dataclass(frozen=True)
class SharedState:
    """编排层共享状态的只读快照；只能通过 AgentContext setter 替换。"""

    facts: MediaFacts | None = None
    transcript: Transcript | None = None
    transcript_summary: dict | None = None
    frames: dict | None = None
    shots: dict | None = None
    quality: dict | None = None
    faces: dict | None = None
    contact_sheet: dict | None = None
    chapters: dict | None = None
    candidates: dict | None = None
    revision: int | None = None
    ir: EditIR | None = None
    timeline: TimelineMemory | None = None
    review: dict | None = None
    tool_calls: tuple[dict, ...] | None = None
    render_result: dict | None = None
    verify_result: dict | None = None
    warnings: tuple[str, ...] = ()
    # 槽位名 -> 来源；只增不减，供回放与冲突排查。
    meta: dict[str, SlotMeta] = field(default_factory=dict)


@dataclass
class AgentContext:
    run_id: str
    project_id: str
    goal: str
    toolkit: AgentToolkit
    workspace: Workspace
    settings: Settings
    allow_render: bool = True
    manual_ir: EditIR | None = None
    pinned_effects: list[Effect] | None = None
    phase: str = "plan"
    approved_outline: OutlineApproval | None = None
    reporter: Any | None = None
    cancel_token: Any | None = None
    _shared: SharedState = field(default_factory=SharedState)
    lock: threading.RLock = field(default_factory=threading.RLock)
    # 当前 agent 必须按线程隔离：多个子智能体会并行写共享状态。
    _local: threading.local = field(default_factory=threading.local, repr=False)
    planned_agents: set[str] = field(default_factory=set)
    forbidden_agents: frozenset[str] = field(default_factory=frozenset)
    # 非空时，动态任务只能使用当前 phase policy 声明的 agent。
    allowed_dynamic_agents: frozenset[str] = field(default_factory=frozenset)
    dynamic_task_ids: set[str] = field(default_factory=set)
    chapter_ids: list[str] | None = None

    @property
    def shared(self) -> SharedState:
        """返回当前不可变快照；agent 不能直接赋值或追加 warning。"""
        with self.lock:
            return self._shared

    @property
    def current_agent(self) -> str | None:
        return getattr(self._local, "agent", None)

    @property
    def current_task_id(self) -> str | None:
        return getattr(self._local, "task_id", None)

    def begin_task(self, task: Any) -> None:
        """在 agent 线程内标记当前任务，供槽位来源标记使用。"""
        self._local.agent = getattr(task, "agent", None)
        self._local.task_id = getattr(task, "id", None)

    def end_task(self) -> None:
        self._local.agent = None
        self._local.task_id = None

    def _update_shared(self, **changes: Any) -> None:
        if self.cancel_token is not None:
            self.cancel_token.raise_if_cancelled()
        source = self.current_agent or "system"
        produced_at = datetime.now(timezone.utc).isoformat()
        with self.lock:
            meta = dict(self._shared.meta)
            for key, value in changes.items():
                meta[key] = SlotMeta(
                    source=source,
                    produced_at=produced_at,
                    cache_key=_extract_cache_key(value),
                )
            self._shared = replace(
                self._shared,
                meta=_freeze(meta),
                **{key: _freeze(value) for key, value in changes.items()},
            )

    def raise_if_cancelled(self) -> None:
        if self.cancel_token is not None:
            self.cancel_token.raise_if_cancelled()

    def has_agent(self, name: str) -> bool:
        with self.lock:
            return name in self.planned_agents

    def mark_agent(self, name: str) -> None:
        with self.lock:
            self.planned_agents.add(name)

    def mark_dynamic_task(self, task_id: str) -> None:
        with self.lock:
            self.dynamic_task_ids.add(task_id)

    def dynamic_tasks(self) -> list[str]:
        with self.lock:
            return sorted(self.dynamic_task_ids)

    def add_warning(self, message: str) -> None:
        if self.cancel_token is not None:
            self.cancel_token.raise_if_cancelled()
        with self.lock:
            if message not in self._shared.warnings:
                self._shared = replace(
                    self._shared,
                    warnings=(*self._shared.warnings, message),
                )

    def set_facts(self, facts: MediaFacts) -> None:
        self._update_shared(facts=facts)

    def set_transcript(self, transcript: Transcript | None, summary: dict) -> None:
        self._update_shared(
            transcript=transcript,
            transcript_summary=summary,
        )

    def set_frames(self, result: dict) -> None:
        self._update_shared(frames=result)

    def set_shots(self, result: dict) -> None:
        self._update_shared(shots=result)

    def set_quality(self, result: dict) -> None:
        self._update_shared(quality=result)

    def set_faces(self, result: dict) -> None:
        self._update_shared(faces=result)

    def set_contact_sheet(self, result: dict) -> None:
        self._update_shared(contact_sheet=result)

    def set_chapters(self, result: dict) -> None:
        self._update_shared(chapters=result)

    def set_candidates(self, result: dict) -> None:
        self._update_shared(candidates=result)

    def set_plan(self, revision: int, ir: EditIR) -> None:
        # 时间线账本随每次 revision 更新，Plan/Critic/Audio 都走同一入口。
        timeline = build_timeline_memory(ir, origin=self.current_agent or "plan")
        self._update_shared(revision=revision, ir=ir, timeline=timeline)

    def set_review(self, review: dict) -> None:
        self._update_shared(review=review)

    def set_tool_calls(self, tool_calls: list[dict]) -> None:
        self._update_shared(tool_calls=tuple(tool_calls))

    def set_render(self, result: dict) -> None:
        self._update_shared(render_result=result)

    def set_verify(self, result: dict) -> None:
        self._update_shared(verify_result=result)


# ── 上下文摘要（回放用） ────────────────────────────────────────────────
CONTEXT_DIGEST_LIMIT = 8192
_GOAL_PREVIEW_CHARS = 200
_SLOT_NAMES = (
    "facts",
    "transcript",
    "frames",
    "shots",
    "quality",
    "faces",
    "contact_sheet",
    "chapters",
    "candidates",
    "ir",
    "timeline",
    "review",
    "tool_calls",
    "render_result",
    "verify_result",
)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _count(value: Any, key: str, fallback: str | None = None) -> int:
    if not isinstance(value, dict):
        return 0
    count = _int(value.get(key))
    if count or not fallback:
        return count
    return _int(value.get(fallback))


def summarize_context(ctx: AgentContext, task: Any | None = None) -> dict[str, Any]:
    """生成 agent 输入的结构化摘要，用于运行目录中的可回放快照。

    只记录"看到了什么"的规模、来源与缓存键，不记录 prompt 明文或媒体内容。
    """
    shared = ctx.shared
    chapters = shared.chapters or {}
    candidates = shared.candidates or {}
    shots = shared.shots or {}
    quality = shared.quality or {}
    faces = shared.faces or {}
    frames = shared.frames or {}
    timeline = shared.timeline

    digest: dict[str, Any] = {
        "task_id": getattr(task, "id", None) or ctx.current_task_id,
        "agent": getattr(task, "agent", None) or ctx.current_agent,
        "phase": ctx.phase,
        "goal": (ctx.goal or "")[:_GOAL_PREVIEW_CHARS],
        "approved_outline": ctx.approved_outline is not None,
        "chapter_ids": list(ctx.chapter_ids or [])[:20],
        "slots": {name: getattr(shared, name) is not None for name in _SLOT_NAMES},
        "counts": {
            "chapters": _count(chapters, "chapter_count", "chapters"),
            "candidates": _count(candidates, "candidate_count", "candidates"),
            "shots": _count(shots, "shot_count", "shots"),
            "quality_marks": _count(quality, "mark_count", "marks"),
            "face_frames": _count(faces, "face_frame_count", "anchors"),
            "frames": _count(frames, "frame_count", "frames"),
        },
        "transcript": {
            "present": shared.transcript is not None,
            "degraded": bool((shared.transcript_summary or {}).get("degraded")),
            "segments": _int((shared.transcript_summary or {}).get("segments")),
            "words": _int((shared.transcript_summary or {}).get("words")),
        },
        "plan": {
            "revision": shared.revision,
            "segments": len(shared.ir.sequence) if shared.ir is not None else 0,
            "output_seconds": (
                shared.ir.total_duration().to_ffmpeg_seconds()
                if shared.ir is not None
                else None
            ),
        },
        "timeline": (
            {
                "coverage_ratio": round(timeline.coverage_ratio, 4),
                "duplicate_ratio": round(timeline.duplicate_ratio, 4),
                "output_seconds": timeline.output_duration.to_ffmpeg_seconds(),
                "used_count": len(timeline.used),
                "unused_gap_count": len(timeline.unused_gaps),
            }
            if timeline is not None
            else None
        ),
        "warnings": len(shared.warnings),
        "slot_sources": {
            name: meta.dump() for name, meta in shared.meta.items()
        },
        "dynamic_tasks": ctx.dynamic_tasks()[:20],
    }
    return _shrink_digest(digest)


def _shrink_digest(digest: dict[str, Any]) -> dict[str, Any]:
    """确保摘要不超过大小上限；优先丢弃可重建的信息。"""
    if _digest_size(digest) <= CONTEXT_DIGEST_LIMIT:
        return digest
    for key in ("dynamic_tasks", "slot_sources", "chapter_ids"):
        if key in digest:
            digest.pop(key, None)
            if _digest_size(digest) <= CONTEXT_DIGEST_LIMIT:
                return digest
    digest["goal"] = digest.get("goal", "")[:40]
    return digest


def _digest_size(digest: dict[str, Any]) -> int:
    return len(json.dumps(digest, ensure_ascii=False, default=str).encode("utf-8"))
