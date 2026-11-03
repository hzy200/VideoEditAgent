from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.time import RationalTime
from smartcut.perception import frames


def _ref(index: int, path: str | None = None) -> FrameRef:
    return FrameRef(
        index=index,
        t=RationalTime.from_seconds(str(index), rate=90_000),
        path=path or f"frames/{index}.jpg",
    )


def _pattern(path: Path, kind: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("L", (64, 36), 0)
    draw = ImageDraw.Draw(image)
    if kind in {"left-right", "left-right-copy"}:
        for x in range(64):
            draw.line((x, 0, x, 36), fill=x * 4)
    elif kind == "right-left":
        for x in range(64):
            draw.line((x, 0, x, 36), fill=(63 - x) * 4)
    else:
        for x in range(0, 64, 8):
            for y in range(0, 36, 8):
                if (x // 8 + y // 8) % 2 == 0:
                    draw.rectangle((x, y, x + 7, y + 7), fill=255)
    image.save(path, format="JPEG")


def test_accepts_structured_events(tmp_path: Path) -> None:
    refs = [_ref(index) for index in range(100)]
    selected = frames.select_analysis_frames(
        refs,
        [50.0],
        max_frames=8,
        risk_times=[10.0],
        topic_times=[30.0],
        chapter_bounds=[70.0],
        workspace_root=tmp_path,
        dedupe=False,
    )
    positions = [item.index for item in selected]
    assert len(positions) == 8
    assert positions == sorted(positions)
    assert {49, 50, 10, 30, 70} <= set(positions)


def test_keeps_priority_frames_when_budget_is_tight() -> None:
    refs = [_ref(index) for index in range(100)]
    selected = frames.select_analysis_frames(
        refs,
        [50.0],
        max_frames=5,
        risk_times=[10.0],
        topic_times=[30.0],
        chapter_bounds=[70.0],
        dedupe=False,
    )
    positions = [item.index for item in selected]
    assert len(positions) == 5
    assert 50 in positions
    assert 10 in positions
    assert 30 in positions


def test_perceptual_dedupe_skips_similar_frames(tmp_path: Path) -> None:
    kinds = ["left-right", "left-right-copy", "right-left", "checker"]
    refs = []
    for index, kind in enumerate(kinds):
        _pattern(tmp_path / "frames" / f"{index}.jpg", kind)
        refs.append(_ref(index))

    selected = frames.select_analysis_frames(
        refs,
        max_frames=4,
        dedupe=True,
        workspace_root=tmp_path,
        min_gap_seconds=0.0,
    )
    assert len(selected) == 3
    assert selected[0].index in {0, 1}
    assert {2, 3} <= {item.index for item in selected}


def test_no_dedupe_keeps_all_frames_within_budget(tmp_path: Path) -> None:
    for index in range(3):
        _pattern(tmp_path / "frames" / f"{index}.jpg", "left-right")
    refs = [_ref(index) for index in range(3)]

    selected = frames.select_analysis_frames(
        refs,
        max_frames=3,
        dedupe=False,
        workspace_root=tmp_path,
    )
    assert selected is refs
