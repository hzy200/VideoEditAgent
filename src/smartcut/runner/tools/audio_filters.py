"""共享音频滤镜链：音量、保持时长变调与响度归一。"""

from __future__ import annotations


def audio_filter_chain(
    *,
    volume_db: float = 0.0,
    pitch_semitones: float = 0.0,
    target_lufs: float | None = None,
    true_peak: float = -1.5,
    lra: float = 11.0,
    measured: dict[str, str] | None = None,
) -> list[str]:
    filters: list[str] = []
    if pitch_semitones:
        # asetrate 改变音高与速度，再以 atempo 反向抵消速度，保持时长不变。
        factor = 2 ** (pitch_semitones / 12.0)
        filters.extend(
            [
                "aresample=48000",
                f"asetrate={48000.0 * factor:.6f}",
                "aresample=48000",
                f"atempo={1.0 / factor:.6f}",
            ]
        )
    if volume_db:
        filters.append(f"volume={volume_db}dB")
    if target_lufs is not None:
        if measured:
            parts = [
                f"I={target_lufs}",
                f"TP={true_peak}",
                f"LRA={lra}",
            ]
            for key in (
                "measured_I",
                "measured_TP",
                "measured_LRA",
                "measured_thresh",
                "offset",
            ):
                value = measured.get(key)
                if value not in (None, ""):
                    parts.append(f"{key}={value}")
            parts.append("linear=true")
            filters.append("loudnorm=" + ":".join(parts))
        else:
            filters.append(f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}")
    return filters
