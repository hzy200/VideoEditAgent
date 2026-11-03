"""多轨音频混音：复用视频流，单次 filter_complex 输出 AAC。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from smartcut.contracts.time import RationalTime
from smartcut.runner.tools.audio_filters import audio_filter_chain
from smartcut.runner.tools.base import ToolContext


def _zero() -> RationalTime:
    return RationalTime.from_seconds("0", rate=90_000)


class DuckingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    start: RationalTime
    end: RationalTime


class MixTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    start: RationalTime
    end: RationalTime
    timeline_start: RationalTime = Field(default_factory=_zero)
    gain_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)
    loop: bool = False
    fade_in: RationalTime = Field(default_factory=_zero)
    fade_out: RationalTime = Field(default_factory=_zero)
    ducking: bool = False
    ducking_gain_db: float = Field(default=0.0, ge=-24.0, le=12.0, allow_inf_nan=False)
    ducking_ratio: float = Field(default=4.0, ge=1.0, le=20.0, allow_inf_nan=False)
    ducking_threshold: float = Field(default=0.05, gt=0.0, le=1.0, allow_inf_nan=False)
    ducking_attack_ms: int = Field(default=20, ge=1, le=2000)
    ducking_release_ms: int = Field(default=300, ge=10, le=9000)
    ducking_source: DuckingSource | None = None


class MixAudioArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    src: str
    dst: str
    tracks: list[MixTrack] = Field(default_factory=list)
    output_duration: RationalTime
    volume_db: float = Field(default=0.0, ge=-60.0, le=12.0, allow_inf_nan=False)
    pitch_semitones: float = Field(
        default=0.0, ge=-12.0, le=12.0, allow_inf_nan=False
    )
    target_lufs: float | None = Field(default=None, ge=-70.0, le=0.0)
    true_peak: float = Field(default=-1.5, ge=-9.0, le=0.0)
    lra: float = Field(default=11.0, ge=1.0, le=50.0)
    loudnorm_measured: dict[str, str] | None = None
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"


def build_argv(args: BaseModel, ctx: ToolContext) -> list[str]:
    assert isinstance(args, MixAudioArgs)
    video = ctx.workspace.resolve_input(args.src)
    track_paths = [ctx.workspace.resolve_input(track.src) for track in args.tracks]
    sidechain_paths = [
        ctx.workspace.resolve_input(track.ducking_source.src)
        for track in args.tracks
        if track.ducking and track.ducking_source is not None
    ]
    dst = ctx.workspace.resolve_output(
        args.dst,
        reserved_inputs={video, *track_paths, *sidechain_paths},
    )

    argv = [ctx.settings.ffmpeg_bin, "-hide_banner", "-y", "-i", str(video)]
    for track, path in zip(args.tracks, track_paths):
        if track.loop:
            argv.extend(["-stream_loop", "-1"])
        argv.extend(["-i", str(path)])
    if not args.tracks:
        argv.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])

    sidechain_input_index: dict[int, int] = {}
    next_input = 1 + len(args.tracks)
    for index, track in enumerate(args.tracks, start=1):
        if track.ducking and track.ducking_source is not None:
            argv.extend(
                [
                    "-i",
                    str(ctx.workspace.resolve_input(track.ducking_source.src)),
                ]
            )
            sidechain_input_index[index] = next_input
            next_input += 1

    filters: list[str] = []
    labels: list[str] = []
    for index, track in enumerate(args.tracks, start=1):
        start = track.start.to_ffmpeg_seconds()
        end = track.end.to_ffmpeg_seconds()
        chain = [
            f"[{index}:a]atrim=start={start}:end={end}",
            "asetpts=PTS-STARTPTS",
        ]
        if track.gain_db:
            chain.append(f"volume={track.gain_db}dB")
        duration = track.end.as_fraction() - track.start.as_fraction()
        if track.fade_in.as_fraction() > 0:
            chain.append(f"afade=t=in:st=0:d={track.fade_in.to_ffmpeg_seconds()}")
        if track.fade_out.as_fraction() > 0:
            fade_out = min(track.fade_out.as_fraction(), duration)
            fade_start = max(0, duration - fade_out)
            chain.append(
                f"afade=t=out:st={float(fade_start):.6f}:d={float(fade_out):.6f}"
            )
        delay_ms = int(round(track.timeline_start.to_float() * 1000))
        if delay_ms > 0:
            chain.append(f"adelay={delay_ms}:all=1")
        ducked = track.ducking and index in sidechain_input_index
        label = f"[m{index}]" if ducked else f"[a{index}]"
        filters.append(",".join(chain) + label)
        if ducked:
            source = track.ducking_source
            assert source is not None
            source_index = sidechain_input_index[index]
            filters.append(
                f"[{source_index}:a]atrim=start={source.start.to_ffmpeg_seconds()}:"
                f"end={source.end.to_ffmpeg_seconds()},"
                f"asetpts=PTS-STARTPTS[sc{index}]"
            )
            filters.append(
                f"{label}[sc{index}]sidechaincompress="
                f"threshold={track.ducking_threshold}:"
                f"ratio={track.ducking_ratio}:"
                f"attack={track.ducking_attack_ms}:"
                f"release={track.ducking_release_ms}[duck{index}]"
            )
            if track.ducking_gain_db:
                filters.append(
                    f"[duck{index}]volume={track.ducking_gain_db}dB[a{index}]"
                )
                label = f"[a{index}]"
            else:
                label = f"[duck{index}]"
        labels.append(label)

    if not args.tracks:
        output = args.output_duration.to_ffmpeg_seconds()
        filters.append(
            f"[1:a]atrim=duration={output},asetpts=PTS-STARTPTS[amix]"
        )
        mix_label = "[amix]"
    elif len(labels) > 1:
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:normalize=0[amix]"
        )
        mix_label = "[amix]"
    else:
        mix_label = labels[0]

    output = args.output_duration.to_ffmpeg_seconds()
    master = [
        f"{mix_label}apad=whole_dur={output}",
        f"atrim=end={output}",
    ]
    master.extend(
        audio_filter_chain(
            volume_db=args.volume_db,
            pitch_semitones=args.pitch_semitones,
            target_lufs=args.target_lufs,
            true_peak=args.true_peak,
            lra=args.lra,
            measured=args.loudnorm_measured,
        )
    )
    filters.append(",".join(master) + "[mix]")

    argv.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[mix]",
            "-c:v",
            "copy",
            "-c:a",
            args.audio_codec,
            "-b:a",
            args.audio_bitrate,
            "-t",
            args.output_duration.to_ffmpeg_seconds(),
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )
    return argv
