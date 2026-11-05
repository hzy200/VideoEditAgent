"""EditIR 语义校验。明确支持 P1 能力，拒绝未实现项而不是静默忽略。"""

from __future__ import annotations

from fractions import Fraction

from smartcut.compiler.loudness import parse_target_lufs
from smartcut.contracts.effects import enabled_segment_effects, validate_effects
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.export_presets import EXPORT_PRESETS, preset_target
from smartcut.errors import IRValidationError, UnsupportedFeature


def validate_ir(ir: EditIR) -> None:
    facts = ir.media_facts
    sources = {facts.src: facts, **ir.source_facts}
    if facts.duration.as_fraction() <= 0:
        raise IRValidationError("media_facts.duration 必须大于 0")
    if facts.video is None:
        raise IRValidationError("P1 只支持含视频轨道的素材")
    if len(sources) > 1 and ir.export.cut_mode == "copy":
        raise UnsupportedFeature("多素材时间线必须使用 reencode 模式")
    if len(sources) > 1:
        non_video = [src for src, item in sources.items() if item.video is None]
        if non_video:
            raise UnsupportedFeature(
                "多素材时间线目前只支持视频素材: "
                + ", ".join(sorted(non_video))
            )

    for index, op in enumerate(ir.sequence):
        op_facts = sources.get(op.src)
        if op_facts is None:
            raise IRValidationError(
                f"sequence[{index}].src 不在媒体事实中: {op.src!r}"
            )
        source_duration = op_facts.duration.as_fraction()
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

    if ir.pips:
        if ir.export.cut_mode == "copy":
            raise UnsupportedFeature("画中画需要重编码；请将 cut_mode 设为 reencode")
        total = ir.total_duration().as_fraction()
        pip_ids = [item.id for item in ir.pips]
        if len(pip_ids) != len(set(pip_ids)):
            raise IRValidationError("画中画 id 必须唯一")
        for index, pip in enumerate(ir.pips):
            pip_facts = sources.get(pip.src)
            if pip_facts is None or pip_facts.video is None:
                raise IRValidationError(
                    f"pips[{index}] 必须是已导入的视频素材"
                )
            if (
                pip.source_start.as_fraction() < 0
                or pip.source_end > pip_facts.duration
            ):
                raise IRValidationError(f"pips[{index}] 源区间越界")
            if (
                pip.timeline_start.as_fraction() < 0
                or pip.timeline_start.as_fraction()
                + pip.duration.as_fraction()
                > total + 1e-6
            ):
                raise IRValidationError(f"pips[{index}] 输出时间窗越界")

    for index, op in enumerate(ir.sequence):
        if not op.keyframes:
            continue
        times = [kf.t.as_fraction() for kf in op.keyframes]
        if times != sorted(times) or len(times) != len(set(times)):
            raise IRValidationError(
                f"sequence[{index}] 关键帧时间必须唯一且升序"
            )
        duration = op.output_duration().as_fraction()
        if any(t > duration for t in times):
            raise IRValidationError(
                f"sequence[{index}] 关键帧超出片段时长"
            )

    if ir.absolute_timeline():
        if ir.export.cut_mode == "copy":
            raise UnsupportedFeature("绝对输出时间线必须使用 reencode 模式")
        for index, op in enumerate(ir.sequence):
            if op.timeline_start is None:
                raise IRValidationError(
                    f"sequence[{index}] 缺少 timeline_start（绝对时间线必须全部提供）"
                )
            if op.timeline_start.as_fraction() < 0:
                raise IRValidationError(
                    f"sequence[{index}].timeline_start 不能为负"
                )
        transition_by_after = {item.after: item for item in ir.transitions}
        by_track: dict[int, list] = {}
        for index, op in enumerate(ir.sequence):
            by_track.setdefault(op.track, []).append((index, op))
        track0_ids = {op.id for op in ir.sequence if op.track == 0}
        track0_chained = any(
            op.track == 0 and op.id in transition_by_after
            for op in ir.sequence
        ) or any(
            transition.after in track0_ids for transition in ir.transitions
        )
        for track, items in by_track.items():
            ordered = sorted(
                items, key=lambda pair: pair[1].timeline_start.as_fraction()
            )
            previous_end = None
            previous_op = None
            for index, op in ordered:
                start = op.timeline_start.as_fraction()
                end = start + op.output_duration().as_fraction()
                if track == 0 and track0_chained and previous_end is not None:
                    transition = (
                        transition_by_after.get(previous_op.id)
                        if previous_op is not None
                        else None
                    )
                    expected = previous_end
                    if transition is not None:
                        expected -= transition.duration.as_fraction()
                    if abs(start - expected) > Fraction(1, 1000):
                        raise IRValidationError(
                            "track 0 使用转场时片段必须连续（含重叠）: "
                            f"sequence[{index}] start={float(start):.3f} "
                            f"expected={float(expected):.3f}"
                        )
                previous_end = end
                previous_op = op

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
    absolute_by_track: dict[int, list] = {}
    if ir.absolute_timeline():
        for op in ir.sequence:
            absolute_by_track.setdefault(op.track, []).append(op)
        for track, items in absolute_by_track.items():
            absolute_by_track[track] = sorted(
                items, key=lambda item: item.timeline_start.as_fraction()
            )
    for transition in ir.transitions:
        if transition.after in seen_after:
            raise IRValidationError(f"同一片段后不能有多个转场: {transition.after}")
        seen_after.add(transition.after)
        left = by_id.get(transition.after)
        if left is None:
            raise IRValidationError(
                f"转场引用了不存在的片段: {transition.after}"
            )
        if ir.absolute_timeline():
            items = absolute_by_track.get(left.track) or []
            index = items.index(left) if left in items else -1
            if index < 0 or index + 1 >= len(items):
                raise IRValidationError(
                    f"转场后没有同轨片段: {transition.after}"
                )
            right = items[index + 1]
        else:
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
    target = preset_target(ir.export.preset)
    if has_fit:
        output_width, output_height = 1080, 1920
    elif target is not None:
        output_width, output_height = target
    else:
        output_width, output_height = facts.width, facts.height
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

    if ir.export.preset not in EXPORT_PRESETS:
        raise UnsupportedFeature(f"未知导出预设: {ir.export.preset!r}")
    if ir.export.cut_mode == "copy" and preset_target(ir.export.preset):
        raise UnsupportedFeature(
            "平台导出预设需要裁切/缩放；请把 export.cut_mode 设为 reencode"
        )
    if ir.export.fps is not None:
        raise UnsupportedFeature("P1 不改帧率；fps 预设延后到 P2")
