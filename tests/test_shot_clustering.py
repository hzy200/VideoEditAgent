from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

pytest.importorskip("cv2")

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.shots import ShotBoundary, ShotList
from smartcut.contracts.time import RationalTime
from smartcut.perception.cluster import cluster_shots


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _frame(workspace: Path, name: str, color: str, offset: int) -> FrameRef:
    path = workspace / "frames" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (320, 180), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20 + offset, 20, 140 + offset, 120), fill="white")
    image.save(path, format="JPEG", quality=90)
    return FrameRef(index=int(offset), t=_rt(str(offset)), path=f"frames/{name}")


def test_cluster_shots_merges_similar_frames(tmp_path: Path) -> None:
    frames = [
        _frame(tmp_path, "a.jpg", "#1a1a2e", 0),
        _frame(tmp_path, "b.jpg", "#1a1a2e", 2),
        _frame(tmp_path, "c.jpg", "#e94560", 40),
    ]
    # 帧时间修正为 0.5/1.5/2.5，避免与镜头边界重叠。
    frames[0] = frames[0].model_copy(update={"t": _rt("0.5")})
    frames[1] = frames[1].model_copy(update={"t": _rt("1.5")})
    frames[2] = frames[2].model_copy(update={"t": _rt("2.5")})
    shots = ShotList(
        src="media/a.mp4",
        shots=[
            ShotBoundary(t=_rt("1"), confidence=0.8),
            ShotBoundary(t=_rt("2"), confidence=0.8),
        ],
    )
    clusters = cluster_shots(
        shots,
        frames,
        tmp_path,
        3.0,
        similarity=0.9,
    )
    assert len(clusters) == 2
    assert clusters[0].shot_indices == (0, 1)
    assert clusters[1].shot_indices == (2,)
    assert clusters[0].representative is not None
