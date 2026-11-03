from __future__ import annotations

from pathlib import Path

from smartcut.contracts.candidates import Candidate, CandidateList
from smartcut.contracts.edit_ir import TimeRange
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.time import RationalTime
from smartcut.perception import candidates as candidates_mod
from smartcut.perception.candidates import attach_visual_hashes
from smartcut.server.candidate_index import CandidateIndex
from smartcut.server.db import Database


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _candidate(candidate_id: str, start: str, end: str, visual_hash=None) -> Candidate:
    return Candidate(
        id=candidate_id,
        source_range=TimeRange(start=_rt(start), end=_rt(end)),
        visual_hash=visual_hash,
    )


def test_attach_visual_hashes_uses_nearest_frame(
    tmp_path: Path,
    monkeypatch,
) -> None:
    frames = [
        FrameRef(index=0, t=_rt("0"), path="frames/frame_0000.jpg"),
        FrameRef(index=1, t=_rt("2"), path="frames/frame_0002.jpg"),
    ]
    candidate_list = CandidateList(
        src="media/a.mp4",
        candidates=[_candidate("c1", "1.9", "3")],
    )
    calls: list[Path] = []

    def fake_dhash(path: Path) -> int:
        calls.append(path)
        return 12345

    monkeypatch.setattr(candidates_mod, "dhash", fake_dhash)

    result = attach_visual_hashes(candidate_list, frames, tmp_path)

    assert result.candidates[0].visual_hash == 12345
    assert result.candidates[0].keyframe == "frames/frame_0002.jpg"
    assert calls == [tmp_path / "frames/frame_0002.jpg"]


def test_candidate_index_visual_orders_by_hamming_distance(tmp_path: Path) -> None:
    db = Database(tmp_path / "smartcut.db")
    db.init()
    index = CandidateIndex(db)
    items = [
        _candidate("cand_0001", "0", "1", visual_hash=0b0000),
        _candidate("cand_0002", "1", "2", visual_hash=0b0001),
        _candidate("cand_0003", "2", "3", visual_hash=0b1111),
        _candidate("cand_0004", "3", "4", visual_hash=None),
    ]
    index.replace("p_test", "hash_a", [item.model_dump(mode="json") for item in items])

    found = index.visual("p_test", "hash_a", 0b0000, limit=10, max_distance=2)

    assert [item["id"] for item in found] == ["cand_0001", "cand_0002"]
