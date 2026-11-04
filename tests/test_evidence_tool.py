from __future__ import annotations

from pathlib import Path

from smartcut.contracts.time import RationalTime
from smartcut.contracts.tools import ToolCall
from smartcut.runner.tools.base import ToolContext
from smartcut.runner.tools.evidence_frames import (
    EvidenceWindowsArgs,
    build_argv,
)


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def test_evidence_args_validate_order() -> None:
    try:
        EvidenceWindowsArgs(
            src="media/a.mp4",
            start=_rt("2"),
            end=_rt("1"),
            dst_dir="evidence/tmp",
        )
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("end <= start 必须被拒绝")


def test_evidence_argv_uses_window_trim_and_showinfo(
    settings,
    workspace,
    ingested_source: str,
) -> None:
    args = EvidenceWindowsArgs(
        src=ingested_source,
        start=_rt("1"),
        end=_rt("3"),
        fps=2.0,
        width=640,
        dst_dir="evidence/w0",
    )
    argv = build_argv(args, ToolContext(settings=settings, workspace=workspace))
    assert "-copyts" in argv
    assert "showinfo" in " ".join(argv)
    assert "trim=start=" in " ".join(argv)
    assert argv[-1].endswith("frame_%06d.jpg")


def test_evidence_tool_extracts_pts_frames(
    executor,
    workspace,
    ingested_source: str,
) -> None:
    result = executor.execute(
        ToolCall(
            seq=0,
            tool="extract_evidence_frames",
            args={
                "src": ingested_source,
                "start": _rt("0").model_dump(),
                "end": _rt("1.5").model_dump(),
                "fps": 2.0,
                "width": 640,
                "dst_dir": "evidence/w0",
            },
        )
    )
    directory = workspace.root / "evidence/w0"
    frames = sorted(directory.glob("frame_*.jpg"))
    assert frames, result.stderr[-1000:]
    assert "pts_time:" in result.stderr
    assert result.exit_code == 0
