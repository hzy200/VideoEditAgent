"""EditIR 语义校验。明确支持 P1 能力，拒绝未实现项而不是静默忽略。"""

from __future__ import annotations

from smartcut.compiler.loudness import parse_target_lufs
from smartcut.contracts.effects import enabled_segment_effects, validate_effects
from smartcut.contracts.edit_ir import EditIR
from smartcut.errors import IRValidationError, UnsupportedFeature


def validate_ir(ir: EditIR) -> None:
    facts = ir.media_facts
    if facts.duration.as_fraction() <= 0:
        raise IRValidationError("media_facts.duration 必须大于 0")
    if facts.video is None:
        raise IRValidationError("P1 只支持含视频轨道的素材")

    source_duration = facts.duration.as_fraction()
    for index, op in enumerate(ir.sequence):
        start = op.range.start.as_fraction()
        end = op.range.end.as_fraction()
        if start < 0:
            raise IRValidationError(f"sequence[{index}] 起点为负: {start}")
        if end <= start:
            raise IRValidationError(f"sequence[{index}] 区间为空: [{start}, {end}]")
        if end > source_duration:
            raise IRValidationError(
                f"sequence[{index}] 越界: end={end} > 源时长 {source_duration}"
            )

    segment_durations = {
        op.id: op.range.duration() for op in ir.sequence if op.id
    }
    validate_effects(
        ir.effects,
        total_duration=ir.total_duration(),
        segment_durations=segment_durations,
    )
    if ir.export.cut_mode == "copy" and enabled_segment_effects(ir.effects):
        raise UnsupportedFeature(
            "逐片段特效需要重编码；请把 export.cut_mode 设为 reencode"
        )

    if ir.export.cut_mode == "copy" and any(
        op.speed != 1.0 for op in ir.sequence
    ):
        raise UnsupportedFeature("变速需要重编码；请把 export.cut_mode 设为 reencode")
    if ir.export.cut_mode == "copy" and (
        ir.transitions or ir.overlays or ir.removals
    ):
        raise UnsupportedFeature(
            "转场/水印/去水印需要重编码；请把 export.cut_mode 设为 reencode"
        )

    by_id = {op.id: op for op in ir.sequence if op.id}
    seen_after: set[str] = set()
    last_id = ir.sequence[-1].id
    for transition in ir.transitions:
        if transition.after in seen_after:
            raise IRValidationError(f"同一片段后不能有多个转场: {transition.after}")
        seen_after.add(transition.after)
        left = by_id.get(transition.after)
        if left is None:
            raise IRValidationError(
                f"转场引用了不存在的片段: {transition.after}"
            )
        if transition.after == last_id:
            raise IRValidationError("最后一个片段之后不能有转场")
        index = ir.sequence.index(left)
        right = ir.sequence[index + 1]
        limit = min(
            left.output_duration().as_fraction(),
            right.output_duration().as_fraction(),
        )
        if transition.duration.as_fraction() > limit:
            raise IRValidationError(
                f"转场时长 {transition.duration.to_ffmpeg_seconds()}s "
                f"超过相邻片段可重叠上限 {float(limit):.3f}s"
            )

    total_duration = ir.total_duration()
    if total_duration.as_fraction() <= 0:
        raise IRValidationError("转场重叠导致成片时长不大于 0")
    for overlay in ir.overlays:
        if overlay.start >= total_duration:
            raise IRValidationError("水印起点超出成片时长")
        if overlay.end is not None and overlay.end > total_duration:
            raise IRValidationError("水印终点超出成片时长")

    has_fit = any(
        effect.type == "fit" and effect.enabled for effect in ir.effects
    )
    output_width = 1080 if has_fit else facts.width
    output_height = 1920 if has_fit else facts.height
    seen_removals: set[str] = set()
    for removal in ir.removals:
        if removal.id in seen_removals:
            raise IRValidationError(f"去水印区域 id 重复: {removal.id}")
        seen_removals.add(removal.id)
        if removal.start >= total_duration:
            raise IRValidationError("去水印起点超出成片时长")
        if removal.end is not None and removal.end > total_duration:
            raise IRValidationError("去水印终点超出成片时长")
        if (
            removal.x + removal.width >= output_width
            or removal.y + removal.height >= output_height
        ):
            raise IRValidationError(
                f"去水印区域超出画面 {output_width}x{output_height}: "
                f"{removal.x},{removal.y},{removal.width},{removal.height}"
            )

    if ir.audio.loudness is not None:
        try:
            parse_target_lufs(ir.audio.loudness)
        except ValueError as exc:
            raise UnsupportedFeature(str(exc)) from exc
    if ir.audio.bgm is not None:
        raise UnsupportedFeature("P1 不支持 BGM 混音")

    if ir.export.preset != "mp4_h264":
        raise UnsupportedFeature(
            f"P1 只支持 mp4_h264 预设，收到 {ir.export.preset!r}"
        )
    if ir.export.fps is not None:
        raise UnsupportedFeature("P1 不改帧率；fps 预设延后到 P2")
