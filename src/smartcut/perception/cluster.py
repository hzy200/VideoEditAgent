"""镜头视觉聚类：同一页 PPT/录屏/静止画面不因内部切点被拆成多章。

只做候选分组，不写时间线；签名使用 HSV 直方图相关性，CV 缺失时退化为
每个镜头独立成簇（不阻断章节生成）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smartcut.contracts.perception import FrameRef
from smartcut.contracts.shots import ShotList

CLUSTER_VERSION = "hsv-hist@1.0"


@dataclass(frozen=True)
class VisualCluster:
    start: float
    end: float
    shot_indices: tuple[int, ...]
    representative: str | None


def _cv():
    try:
        import cv2
        import numpy as np
    except ImportError:  # pragma: no cover - vision optional
        return None, None
    return cv2, np


def _signature(path: Path, cv2, np) -> Any:
    image = cv2.imread(str(path))
    if image is None:
        return None
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    return cv2.normalize(histogram, histogram).flatten()


def _similarity(left: Any, right: Any, cv2) -> float:
    if left is None or right is None:
        return 0.0
    return float(cv2.compareHist(left, right, cv2.HISTCMP_CORREL))


def _boundaries(
    shots: ShotList,
    duration_seconds: float,
) -> list[float]:
    values = [0.0]
    for shot in shots.shots:
        moment = shot.t.to_float()
        if 0.0 < moment < duration_seconds:
            values.append(moment)
    values.append(max(0.0, duration_seconds))
    ordered: list[float] = []
    for value in values:
        if not ordered or value > ordered[-1] + 0.05:
            ordered.append(value)
    return ordered


def cluster_shots(
    shots: ShotList | None,
    frames: list[FrameRef],
    workspace_root: Path,
    duration_seconds: float,
    *,
    similarity: float = 0.92,
) -> list[VisualCluster]:
    if shots is None or not shots.shots or duration_seconds <= 0:
        return []
    boundaries = _boundaries(shots, duration_seconds)
    if len(boundaries) < 2:
        return []

    representatives: list[FrameRef | None] = []
    for start, end in zip(boundaries, boundaries[1:]):
        candidates = [
            frame
            for frame in frames
            if start <= frame.t.to_float() < end
        ]
        representatives.append(
            min(
                candidates,
                key=lambda frame: abs(
                    frame.t.to_float() - (start + end) / 2
                ),
                default=None,
            )
            if candidates
            else None
        )

    cv2, np = _cv()
    signatures = []
    for frame in representatives:
        if cv2 is None or frame is None:
            signatures.append(None)
        else:
            signatures.append(
                _signature(workspace_root / frame.path, cv2, np)
            )

    clusters: list[VisualCluster] = []
    cluster_start = boundaries[0]
    cluster_shots = [0]
    previous = signatures[0] if signatures else None
    previous_rep = representatives[0] if representatives else None
    for index in range(1, len(boundaries) - 1):
        current = signatures[index] if index < len(signatures) else None
        same = (
            cv2 is not None
            and previous is not None
            and current is not None
            and _similarity(previous, current, cv2) >= similarity
        )
        if same:
            cluster_shots.append(index)
        else:
            clusters.append(
                VisualCluster(
                    start=cluster_start,
                    end=boundaries[index],
                    shot_indices=tuple(cluster_shots),
                    representative=(
                        previous_rep.path if previous_rep is not None else None
                    ),
                )
            )
            cluster_start = boundaries[index]
            cluster_shots = [index]
        previous = current
        previous_rep = (
            representatives[index]
            if index < len(representatives)
            else previous_rep
        )
    clusters.append(
        VisualCluster(
            start=cluster_start,
            end=boundaries[-1],
            shot_indices=tuple(cluster_shots),
            representative=(
                previous_rep.path if previous_rep is not None else None
            ),
        )
    )
    return clusters
