from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from smartcut.compiler import lower
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.time import RationalTime
from smartcut.contracts.tools import ToolCall
from smartcut.errors import SandboxError, VerificationError
from smartcut.runner.execute import ToolCallExecutor
from smartcut.runner.paths import Workspace


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _args(start: str, end: str, *, mode: str = "reencode") -> dict:
    return {
        "start": RationalTime.from_seconds(start, rate=90_000).model_dump(),
        "end": RationalTime.from_seconds(end, rate=90_000).model_dump(),
        "mode": mode,
    }


def _cut(src: str, dst: str, start: str, end: str, *, mode: str = "reencode") -> ToolCall:
    return ToolCall(
        seq=0,
        tool="cut",
        args={"src": src, "dst": dst, **_args(start, end, mode=mode)},
    )


def test_probe_synthetic(executor: ToolCallExecutor, ingested_source: str) -> None:
    facts = executor.probe(ingested_source)
    assert facts.fps == "30/1"
    assert facts.video is not None
    assert (facts.video.width, facts.video.height) == (320, 240)
    assert facts.has_audio
    assert 1.9 <= facts.duration.to_float() <= 2.1


def test_cut_is_frame_accurate_and_never_touches_input(
    executor: ToolCallExecutor, workspace: Workspace, ingested_source: str
) -> None:
    source_path = workspace.resolve_input(ingested_source)
    before = _sha256(source_path)

    executor.execute(_cut(ingested_source, "renders/t/cut.mp4", "0.5", "1.5"))

    facts = executor.probe("renders/t/cut.mp4")
    assert 0.9 <= facts.duration.to_float() <= 1.1
    assert facts.has_audio
    assert _sha256(source_path) == before


def test_cut_and_concat_full_toolchain(
    executor: ToolCallExecutor, workspace: Workspace, ingested_source: str
) -> None:
    facts = executor.probe(ingested_source)
    ir = EditIR(
        project_id="p_test",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=ingested_source,
                range=TimeRange(
                    start=RationalTime.from_seconds("0.2", rate=90_000),
                    end=RationalTime.from_seconds("0.9", rate=90_000),
                ),
            ),
            TrimOp(
                src=ingested_source,
                range=TimeRange(
                    start=RationalTime.from_seconds("1.1", rate=90_000),
                    end=RationalTime.from_seconds("1.8", rate=90_000),
                ),
            ),
        ],
    )
    plan = lower(ir)
    for call in plan.tool_calls:
        executor.execute(call)

    output = workspace.resolve_input(plan.output_path)
    assert output.exists()
    final = executor.probe(plan.output_path)
    assert abs(final.duration.to_float() - 1.4) <= 0.15
    assert final.has_audio


def test_multi_segment_render_keeps_av_sync(
    executor: ToolCallExecutor,
    ingested_source: str,
) -> None:
    facts = executor.probe(ingested_source)
    ir = EditIR(
        project_id="p_av",
        revision=1,
        media_facts=facts,
        sequence=[
            TrimOp(
                src=ingested_source,
                range=TimeRange(
                    start=RationalTime.from_seconds("0", rate=90_000),
                    end=RationalTime.from_seconds("0.8", rate=90_000),
                ),
            ),
            TrimOp(
                src=ingested_source,
                range=TimeRange(
                    start=RationalTime.from_seconds("1.0", rate=90_000),
                    end=RationalTime.from_seconds("1.8", rate=90_000),
                ),
            ),
        ],
    )
    plan = lower(ir)
    for call in plan.tool_calls:
        executor.execute(call)

    final = executor.probe(plan.output_path)
    assert abs(final.duration.to_float() - 1.6) <= 0.2
    assert final.video is not None and final.audio is not None
    if final.video.start_time is not None and final.audio.start_time is not None:
        diff = abs(
            final.video.start_time.to_float()
            - final.audio.start_time.to_float()
        )
        assert diff <= 0.5
    if final.video.duration is not None and final.audio.duration is not None:
        diff = abs(
            final.video.duration.to_float()
            - final.audio.duration.to_float()
        )
        assert diff <= 0.5


def test_copy_mode_produces_output(
    executor: ToolCallExecutor, ingested_source: str
) -> None:
    executor.execute(_cut(ingested_source, "renders/t/copy.mp4", "1", "2", mode="copy"))
    facts = executor.probe("renders/t/copy.mp4")
    assert 0.5 <= facts.duration.to_float() <= 1.1


def test_extract_audio(
    executor: ToolCallExecutor, workspace: Workspace, ingested_source: str
) -> None:
    executor.execute(
        ToolCall(
            seq=0,
            tool="extract_audio",
            args={"src": ingested_source, "dst": "tmp/audio.wav"},
        )
    )
    assert workspace.resolve_input("tmp/audio.wav").exists()
    facts = executor.probe("tmp/audio.wav")
    assert facts.has_audio
    assert facts.video is None


def test_sandbox_rejects_absolute_and_traversal(
    executor: ToolCallExecutor, workspace: Workspace, ingested_source: str
) -> None:
    with pytest.raises(SandboxError):
        executor.execute(
            _cut(ingested_source, str(workspace.root / "evil.mp4"), "0", "1")
        )
    with pytest.raises(SandboxError):
        executor.execute(_cut(ingested_source, "../evil.mp4", "0", "1"))


def test_sandbox_rejects_overwriting_input(
    executor: ToolCallExecutor, ingested_source: str
) -> None:
    with pytest.raises(SandboxError):
        executor.execute(_cut(ingested_source, ingested_source, "0", "1"))


def test_concat_rejects_overwriting_inputs(
    executor: ToolCallExecutor,
    workspace: Workspace,
    ingested_source: str,
) -> None:
    second = workspace.media_dir / "second.mp4"
    shutil.copy2(workspace.resolve_input(ingested_source), second)
    args = {
        "inputs": [ingested_source, "media/second.mp4"],
        "dst": ingested_source,
        "list_path": "tmp/concat.txt",
    }
    with pytest.raises(SandboxError):
        executor.execute(ToolCall(seq=0, tool="concat", args=args))

    args["dst"] = "tmp/out.mp4"
    args["list_path"] = ingested_source
    with pytest.raises(SandboxError):
        executor.execute(ToolCall(seq=0, tool="concat", args=args))


def test_verify_rejects_wrong_expectation(
    executor: ToolCallExecutor, ingested_source: str
) -> None:
    with pytest.raises(VerificationError):
        executor.execute(
            ToolCall(
                seq=0,
                tool="verify",
                args={
                    "src": ingested_source,
                    "expected_duration": {"value": 900_000, "rate": 90_000},
                    "expected_fps": "30/1",
                    "expect_audio": True,
                    "expected_width": 320,
                    "expected_height": 240,
                    "tolerance_frames": 2,
                },
            )
        )
