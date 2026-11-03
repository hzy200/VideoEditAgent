"""多轨音频解析：把 AudioTrackSpec 转成 mix_audio 的结构化参数。"""

from __future__ import annotations

from smartcut.contracts.audio import AudioAssetRef
from smartcut.contracts.edit_ir import EditIR
from smartcut.contracts.time import RationalTime, rt_to_dict
from smartcut.errors import IRValidationError


def resolve_mix_tracks(
    ir: EditIR,
    audio_assets: dict[str, AudioAssetRef] | None,
    total_duration: RationalTime,
    warnings: list[str],
) -> list[dict]:
    tracks = list(ir.audio.tracks or [])
    if not tracks:
        return []
    refs = audio_assets or {}
    any_solo = any(track.solo for track in tracks)
    resolved: list[dict] = []
    used_specs: list = []
    for track in tracks:
        if track.muted or (any_solo and not track.solo):
            continue
        ref = refs.get(track.asset_id)
        if ref is None:
            raise IRValidationError(f"音轨 {track.id} 引用了未注册音频资产: {track.asset_id}")
        if not ref.has_audio:
            warnings.append(f"音轨 {track.id} 引用的素材没有音频轨，已跳过")
            continue
        start = track.source_start
        timeline_start = track.timeline_start
        remaining = total_duration.as_fraction() - timeline_start.as_fraction()
        if remaining <= 0:
            warnings.append(f"音轨 {track.id} 起点超出成片时长，已跳过")
            continue

        segment_end = track.source_end or ref.duration
        segment_end = min(segment_end, ref.duration)
        if segment_end <= start:
            raise IRValidationError(f"音轨 {track.id} 的素材区间为空")

        if track.loop:
            # -stream_loop 会在输入层循环整份素材；这里直接铺满剩余时长。
            end = RationalTime.from_fraction(start.as_fraction() + remaining)
        else:
            if (segment_end.as_fraction() - start.as_fraction()) > remaining:
                segment_end = RationalTime.from_fraction(
                    start.as_fraction() + remaining
                )
                warnings.append(f"音轨 {track.id} 超出成片时长，已截断")
            end = segment_end

        duration = end.as_fraction() - start.as_fraction()
        fade_in = track.fade_in
        fade_out = track.fade_out
        if fade_in.as_fraction() > duration:
            fade_in = RationalTime.from_fraction(duration)
        if fade_out.as_fraction() > duration:
            fade_out = RationalTime.from_fraction(duration)
        resolved.append(
            {
                "src": ref.src,
                "start": rt_to_dict(start),
                "end": rt_to_dict(end),
                "timeline_start": rt_to_dict(timeline_start),
                "gain_db": track.gain_db,
                "fade_in": rt_to_dict(fade_in),
                "fade_out": rt_to_dict(fade_out),
                "loop": track.loop,
                "ducking": bool(track.ducking),
                "ducking_gain_db": track.ducking_gain_db,
                "ducking_ratio": track.ducking_ratio,
                "ducking_threshold": track.ducking_threshold,
                "ducking_attack_ms": track.ducking_attack_ms,
                "ducking_release_ms": track.ducking_release_ms,
                "ducking_source": None,
            }
        )
        used_specs.append(track)

    # 以第一条非闪避的主/人声轨作为 sidechain 源；找不到时禁用闪避。
    source = next(
        (
            item
            for item, spec in zip(resolved, used_specs)
            if not item["ducking"]
            and spec.role in {"main", "voice", "stem_vocals"}
        ),
        None,
    )
    if source is None:
        # 兼容未显式设置 role 的旧 IR：退回到第一条非闪避音轨。
        source = next(
            (item for item in resolved if not item["ducking"]),
            None,
        )
    if source is not None:
        for item in resolved:
            if item["ducking"]:
                item["ducking_source"] = {
                    "src": source["src"],
                    "start": source["start"],
                    "end": source["end"],
                }
    else:
        for item in resolved:
            if item["ducking"]:
                item["ducking"] = False
                warnings.append("未找到可用于 BGM ducking 的人声/主音轨，已禁用闪避")
    return resolved
