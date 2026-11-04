from __future__ import annotations

from smartcut.contracts.chapters import Chapter, ChapterPlan
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.quality import QualityKind, QualityMark, QualityReport
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.perception.view.select import select_evidence_windows


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _frames(count: int = 60) -> list[FrameRef]:
    return [
        FrameRef(index=index, t=_rt(str(index)), path=f"frames/{index}.jpg")
        for index in range(count)
    ]


def test_evidence_selection_respects_hard_image_budget() -> None:
    shots = ShotList(
        src="media/a.mp4",
        shots=[
            ShotBoundary(t=_rt(str(index * 10)), confidence=0.9)
            for index in range(1, 6)
        ],
    )
    selection = select_evidence_windows(
        _frames(60),
        duration=_rt("60"),
        shots=shots,
        max_images=3,
        max_windows=4,
        fps=2.0,
        padding_seconds=2.0,
        max_window_seconds=8.0,
    )
    assert selection.windows
    assert selection.estimated_tiles <= 3
    assert sum(window.tiles() for window in selection.windows) <= 3
    assert selection.estimated_tokens == selection.estimated_tiles * 1024


def test_evidence_selection_merges_nearby_seeds_and_keeps_reasons() -> None:
    shots = ShotList(
        src="media/a.mp4",
        shots=[ShotBoundary(t=_rt("10"), confidence=0.9)],
    )
    quality = QualityReport(
        src="media/a.mp4",
        marks=[QualityMark(t=_rt("10.5"), kind=QualityKind.black, score=0.9)],
    )
    selection = select_evidence_windows(
        _frames(30),
        duration=_rt("30"),
        shots=shots,
        quality=quality,
        max_images=7,
        max_windows=2,
        fps=2.0,
        padding_seconds=2.0,
    )
    assert len(selection.windows) >= 1
    first = selection.windows[0]
    assert "scene" in first.reasons or "quality:black" in first.reasons
    assert first.start <= RationalTime.from_seconds("10", rate=90_000).as_fraction()


def test_evidence_selection_without_seeds_covers_duration() -> None:
    selection = select_evidence_windows(
        _frames(20),
        duration=_rt("20"),
        max_images=7,
        max_windows=4,
        fps=1.0,
        padding_seconds=1.0,
    )
    assert selection.windows
    assert all(window.reasons == ("coverage",) for window in selection.windows)
    assert selection.seed_count >= 1


def test_evidence_selection_respects_chapter_ids() -> None:
    chapters = ChapterPlan(
        src="media/a.mp4",
        chapters=[
            Chapter(id="ch_1", start=_rt("0"), end=_rt("10"), title="开头"),
            Chapter(id="ch_2", start=_rt("20"), end=_rt("30"), title="重点"),
        ],
    )
    selection = select_evidence_windows(
        _frames(30),
        duration=_rt("30"),
        chapters=chapters,
        chapter_ids=["ch_2"],
        max_images=7,
        max_windows=2,
        padding_seconds=1.0,
    )
    spans = [(float(window.start), float(window.end)) for window in selection.windows]
    assert any(start <= 20.5 <= end for start, end in spans)
    assert all(end <= 30.0 for _, end in spans)
