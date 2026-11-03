from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from smartcut.compiler import lower
from smartcut.compiler.loudness import parse_loudnorm_measure
from smartcut.contracts.edit_ir import AudioSpec, EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace
from smartcut.runner.tools.audio_filters import audio_filter_chain


def _make_loud_video(tmp_path: Path, ffmpeg_bin: str) -> Path:
    output = tmp_path / "loud.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30:duration=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    return output


def test_parse_loudnorm_measure_extracts_fields() -> None:
    text = (
        'prefix\n{"input_i" : "-18.20", "input_tp" : "-1.00", '
        '"input_lra" : "7.00", "input_thresh" : "-28.00", '
        '"target_offset" : "0.10"}\n'
    )
    assert parse_loudnorm_measure(text) == {
        "measured_I": "-18.20",
        "measured_TP": "-1.00",
        "measured_LRA": "7.00",
        "measured_thresh": "-28.00",
        "offset": "0.10",
    }


def test_audio_filter_chain_uses_two_pass_measurement() -> None:
    filters = audio_filter_chain(
        target_lufs=-14.0,
        measured={
            "measured_I": "-18.2",
            "measured_TP": "-1.0",
            "measured_LRA": "7.0",
            "measured_thresh": "-28.0",
            "offset": "0.1",
        },
    )
    assert filters
    assert "measured_I=-18.2" in filters[-1]
    assert "linear=true" in filters[-1]


def test_loudnorm_moves_integrated_loudness_near_minus_14(
    tmp_path: Path,
    ffmpeg_bin: str,
    executor: ToolCallExecutor,
    workspace: Workspace,
) -> None:
    source = _make_loud_video(tmp_path, ffmpeg_bin)
    destination = workspace.media_dir / "loud.mp4"
    shutil.copy2(source, destination)
    relative = destination.relative_to(workspace.root).as_posix()
    facts = executor.probe(relative)

    ir = EditIR(
        project_id="p_loud",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=relative,
                range=TimeRange(
                    start=RationalTime.from_seconds("0", rate=90_000),
                    end=RationalTime.from_seconds("5", rate=90_000),
                ),
            )
        ],
        audio=AudioSpec(loudness="-14 LUFS"),
    )
    plan = lower(ir)
    for call in plan.tool_calls:
        executor.execute(call)

    output = workspace.resolve(plan.output_path)
    assert output.exists()
    result = subprocess.run(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-i",
            str(output),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    stderr = result.stderr
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    assert start != -1 and end > start
    payload = json.loads(stderr[start : end + 1])
    integrated = float(payload["input_i"])
    assert -17.0 <= integrated <= -11.0, integrated
