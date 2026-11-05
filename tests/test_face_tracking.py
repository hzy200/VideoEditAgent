from __future__ import annotations

import statistics

from smartcut.contracts.faces import FaceAnchor
from smartcut.contracts.time import RationalTime
from smartcut.perception.faces import derive_tracks, track_anchors


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _anchor(t: str, x: float, y: float = 0.2) -> FaceAnchor:
    return FaceAnchor(t=_rt(t), x=x, y=y, w=0.2, h=0.2)


def test_track_anchors_keeps_one_track_and_smooths_jitter() -> None:
    anchors = [
        _anchor("0", 0.20, 0.20),
        _anchor("1", 0.26, 0.14),
        _anchor("2", 0.16, 0.26),
        _anchor("3", 0.23, 0.17),
    ]
    smoothed, tracks = track_anchors(anchors, smoothing_alpha=0.4)
    assert {anchor.track_id for anchor in smoothed} == {0}
    assert len(tracks) == 1
    assert tracks[0].frame_count == 4
    raw_variance = statistics.pvariance([anchor.x for anchor in anchors])
    smooth_variance = statistics.pvariance([anchor.x for anchor in smoothed])
    assert smooth_variance < raw_variance


def test_track_anchors_starts_new_track_after_gap() -> None:
    anchors = [_anchor("0", 0.2), _anchor("10", 0.21)]
    smoothed, tracks = track_anchors(anchors, max_gap_seconds=2.0)
    assert [anchor.track_id for anchor in smoothed] == [0, 1]
    assert [track.track_id for track in tracks] == [0, 1]
    assert tracks[0].start.to_float() == 0.0
    assert tracks[1].start.to_float() == 10.0


def test_derive_tracks_groups_by_track_id() -> None:
    anchors = [
        _anchor("0", 0.2).model_copy(update={"track_id": 0}),
        _anchor("1", 0.2).model_copy(update={"track_id": 0}),
        _anchor("2", 0.6).model_copy(update={"track_id": 1}),
    ]
    tracks = derive_tracks(anchors)
    assert [track.frame_count for track in tracks] == [2, 1]
    assert tracks[0].end.to_float() == 1.0
