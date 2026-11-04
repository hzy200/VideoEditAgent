"""Planner 提示词。约束与 Compiler 的硬规则保持一致。"""

from __future__ import annotations

import json

from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.media import MediaFacts
from smartcut.contracts.review import EditReview
from smartcut.contracts.transcript import Transcript
from smartcut.planner.context import (
    ChapterSelection,
    EditIRDraft,
    build_context,
    chapters_payload,
    perception_payload,
    timeline_payload,
    transcript_payload,
    visual_notes_payload,
)

SYSTEM_PROMPT = """你是视频粗剪决策器，只输出一个 JSON 对象，不要输出解释或 Markdown。

必须遵守：
1. 只输出 EditIR 草稿，字段为 sequence / effects / audio / export。
2. 绝对不要输出 ffmpeg 命令、文件路径、shell 片段，也不要拼接路径。
3. 不要做时长减法或计算输出时间；只给出每个保留片段的源区间。
4. 所有时间用有理数对象 {"value": 整数, "rate": 90000}，rate 固定 90000。
5. src 必须与给定 media_facts.src 完全一致。
6. 区间必须落在 [0, duration_seconds] 内，且 end > start。
7. effects 只能从 user 消息中的 effect_catalog 选择，target 必须是 "output"；
   同类型最多一个、总数最多 7 个；绝不输出滤镜表达式、坐标、路径或命令。
   只有用户明确要求风格/竖版/淡入淡出等效果时才添加；fit 仅在要求竖版/9:16 时添加。
   fade.fade_in/fade_out 使用有理数对象，两者之和不得超过成片时长。
8. audio.loudness 可以是 null 或 "-14 LUFS"；只有用户要求响度统一时才设置。
9. audio.bgm 必须为 null；remove_filler 可为 true/false。
10. export 使用 {"preset": "mp4_h264", "burn_captions": 布尔值, "cut_mode": "reencode"}；
    只有用户要求字幕/烧录时才把 burn_captions 设为 true。
11. 每个片段给出简短 tag、0-1 的 confidence 与不超过 30 字的 rationale
    （选段依据，纯文本，不得包含路径、命令或像素坐标）。
12. 如果转写降级，只能基于媒体事实/感知摘要给出保守的整段保留方案。
13. 可使用 perception.shots / quality_counts / face_frame_count 辅助选段与包装决策，
    但不要输出任何像素坐标、文件路径或命令。
14. chapters 是已选章节；所有 trim 必须落在这些章节的时间范围内，优先按章节结构选择，
    不要凭空扩展到未提供的章节。
15. candidates 是按目标检索出的候选片段，包含源时间、文本、质量标记与人脸信息。
    优先从候选中选择保留区间；候选只是建议，仍须满足上面的区间与章节约束。
    不要输出 candidate id，也不要输出坐标、路径或命令。
16. visual_notes 是视觉模型对 contact sheet/章节关键帧/视觉证据的摘要；只作为选段辅助，
    不要把它当作新的时间信息，也不要输出图片路径、坐标或视觉模型命令。
    visual_notes.chapters[].evidence_refs 是真实存在的 Txx 证据帧编号；rationale
    只能引用这些已存在标签，禁止编造标签或把标签换算成时间/切点。
17. 如果提供了人工确认的章节，只能在这些章节范围内选择，不要扩展到其他章节。
18. 用户明确要求“去口癖/去掉语气词/删除嗯呃”时，把 audio.remove_filler 设为 true；
    AudioAgent 会依据词级时间戳做局部修复。
19. timeline 是当前 revision 的已用区间统计（coverage/duplicate/unused_gaps），
    只作为重规划与查漏参考；不要在输出中复制其中的时间或统计字段。
20. memory 是分层索引：memory.chapters 给章节摘要与计数，memory.chapter_shots 给
    选中章节内的镜头切点（秒）。切点可作为选段边界参考，但不要输出坐标、路径或命令。
21. history 是同一项目最近完成的运行摘要，只用于避免重复已知问题；
    不得照抄其中的时间区间，也不要把历史目标当作本次目标。
22. preference 是用户历史剪辑的节奏统计（中位片段时长/保留率/常用标签），
    只作为节奏约束参考；不要照抄具体秒数，也不要为迎合统计破坏内容完整性。
23. 如果 pinned_effects 非空，effects 必须原样复制该数组，不要新增、删除或修改；
    系统会在校验后强制锁定这些特效。pinned_effects 为 null 时按普通规则选择。

输出必须能被以下 JSON Schema 校验：
"""

CHAPTER_SELECTION_PROMPT = """你是长视频章节选择器。只输出 JSON，不要解释或 Markdown。
根据用户目标与章节摘要，挑选最相关的章节。规则：
1. 只选择与目标直接相关的章节；不要为了凑数选择无关章节。
2. 最多选择 8 个章节；按相关性排序。
3. 如果目标需要全片概览，可以选择所有章节。
4. 只输出 chapter_ids 与 rationale，不要输出时间、路径或 ffmpeg 命令。
输出的 JSON 必须符合以下 Schema：
"""


def build_messages(
    facts: MediaFacts,
    transcript: Transcript | None,
    goal: str,
    perception: dict | None = None,
    chapters: dict | None = None,
    candidates: dict | None = None,
    visual_notes: dict | None = None,
    timeline=None,
    memory=None,
    preference=None,
    history: list[dict] | None = None,
    chapter_ids: list[str] | None = None,
    max_tokens: int | None = None,
    stats: dict | None = None,
    pinned_effects: list | None = None,
) -> list[dict[str, str]]:
    context = build_context(
        facts,
        transcript,
        goal,
        perception,
        chapters,
        candidates,
        visual_notes,
        timeline=timeline,
        memory=memory,
        preference=preference,
        chapter_ids=chapter_ids,
        max_tokens=max_tokens,
        stats=stats,
    )
    context["history"] = history or []
    context["pinned_effects"] = (
        [effect.model_dump(mode="json") for effect in pinned_effects]
        if pinned_effects is not None
        else None
    )
    schema = json.dumps(EditIRDraft.model_json_schema(by_alias=True), ensure_ascii=False)
    system = SYSTEM_PROMPT + schema
    user = json.dumps(context, ensure_ascii=False)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


REVIEW_SYSTEM_PROMPT = """你是视频粗剪审片器。只输出 JSON，不要解释或 Markdown。
根据目标、EditIR 片段、转写摘要、感知与视觉 notes 评估这次粗剪。
规则：
1. 只指出结构与内容问题，不要输出新的 ffmpeg 命令、路径或坐标。
2. 每个问题给出 severity（info/warning/error）、message、可选 segment_index、suggestion。
3. 不要因为“可以更好”就标 error；只有明显偏离目标或破坏可看性才标 error。
4. score 为 0-1，表示这次粗剪对目标的满足程度。
5. 输出必须符合以下 JSON Schema：
"""


def build_review_messages(
    ir: EditIR,
    transcript: Transcript | None,
    goal: str,
    perception: dict | None = None,
    visual_notes: dict | None = None,
    timeline=None,
) -> list[dict[str, str]]:
    schema = json.dumps(EditReview.model_json_schema(by_alias=True), ensure_ascii=False)
    payload = {
        "goal": goal,
        "duration_seconds": ir.total_duration().to_ffmpeg_seconds(),
        "timeline": timeline_payload(timeline),
        "sequence": [
            {
                "index": index,
                "start_seconds": op.range.start.to_ffmpeg_seconds(),
                "end_seconds": op.range.end.to_ffmpeg_seconds(),
                "tag": op.tag,
                "confidence": op.confidence,
                "rationale": op.rationale,
            }
            for index, op in enumerate(ir.sequence)
        ],
        "audio": ir.audio.model_dump(mode="json"),
        "export": ir.export.model_dump(mode="json"),
        "transcript": transcript_payload(transcript),
        "perception": perception_payload(perception),
        "visual_notes": visual_notes_payload(visual_notes),
    }
    return [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT + schema},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


REPLAN_SYSTEM_PROMPT = """你是视频粗剪的反思式重规划器。只输出一个 JSON 对象，不要解释或 Markdown。

输入包含上一版 sequence、审片问题与项目上下文。必须遵守：
1. 只输出 EditIR 草稿：sequence / effects / audio / export。
2. 绝对不要输出 ffmpeg 命令、文件路径、shell 片段或像素坐标。
3. 时间仍用 {"value": 整数, "rate": 90000}；src 必须与 media_facts.src 一致。
4. 优先修正 review_issues 指出的问题；未被点名的片段尽量保持原顺序与边界。
5. 不要把 suggestion 里的自然语言当成精确时间；需要时用媒体事实与候选片段重新定位。
6. 不要为了“更好”而整体重写；只有问题明确要求时才改变 effects/audio/export。
7. effects 仍只能从 effect_catalog 选择，target 必须是 "output"；不得输出滤镜表达式。
8. 如果 pinned_effects 非空，effects 必须原样复制该数组，不得新增/删除/修改。
9. 输出必须能被以下 JSON Schema 校验：
"""


def build_replan_messages(
    ir: EditIR,
    transcript: Transcript | None,
    goal: str,
    review: dict | None,
    *,
    perception: dict | None = None,
    chapters: dict | None = None,
    candidates: dict | None = None,
    visual_notes: dict | None = None,
    memory=None,
    preference=None,
    history: list[dict] | None = None,
    max_tokens: int | None = None,
    stats: dict | None = None,
    pinned_effects: list | None = None,
) -> list[dict[str, str]]:
    context = build_context(
        ir.media_facts,
        transcript,
        goal,
        perception,
        chapters,
        candidates,
        visual_notes,
        timeline=None,
        memory=memory,
        preference=preference,
        max_tokens=max_tokens,
        stats=stats,
    )
    payload = review or {}
    issues = []
    for item in (payload.get("issues") or [])[:8]:
        if not isinstance(item, dict):
            continue
        issues.append(
            {
                "severity": item.get("severity"),
                "message": str(item.get("message") or "")[:200],
                "segment_index": item.get("segment_index"),
                "suggestion": str(item.get("suggestion") or "")[:200] or None,
            }
        )
    context["previous_revision"] = ir.revision
    context["previous_sequence"] = [
        {
            "index": index,
            "start_seconds": op.range.start.to_ffmpeg_seconds(),
            "end_seconds": op.range.end.to_ffmpeg_seconds(),
            "tag": op.tag,
            "confidence": op.confidence,
            "rationale": op.rationale,
        }
        for index, op in enumerate(ir.sequence)
    ]
    context["review"] = {
        "score": payload.get("score"),
        "summary": str(payload.get("summary") or "")[:300],
        "issues": issues,
    }
    context["history"] = history or []
    context["pinned_effects"] = (
        [effect.model_dump(mode="json") for effect in pinned_effects]
        if pinned_effects is not None
        else None
    )
    schema = json.dumps(EditIRDraft.model_json_schema(by_alias=True), ensure_ascii=False)
    return [
        {"role": "system", "content": REPLAN_SYSTEM_PROMPT + schema},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]


def build_chapter_selection_messages(
    goal: str,
    chapters: dict,
) -> list[dict[str, str]]:
    schema = json.dumps(
        ChapterSelection.model_json_schema(by_alias=True),
        ensure_ascii=False,
    )
    system = CHAPTER_SELECTION_PROMPT + schema
    user = json.dumps(
        {"goal": goal, "chapters": chapters_payload(chapters)},
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
