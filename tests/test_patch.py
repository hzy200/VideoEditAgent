from __future__ import annotations

from pathlib import Path

import pytest

from smartcut.compiler import apply_patch
from smartcut.config import Settings
from smartcut.contracts.edit_ir import EditIR, TimeRange, TrimOp
from smartcut.contracts.media import MediaFacts, StreamFacts
from smartcut.contracts.patch import EditPatch, PatchOp
from smartcut.contracts.time import RationalTime
from smartcut.errors import IRValidationError
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _rt(seconds: str) -> RationalTime:
    return RationalTime.from_seconds(seconds, rate=90_000)


def _facts(duration: str = "10") -> MediaFacts:
    return MediaFacts(
        src="media/a.mp4",
        duration=_rt(duration),
        fps="30/1",
        video=StreamFacts(
            codec_type="video",
            width=1280,
            height=720,
            avg_frame_rate="30/1",
        ),
    )


def _trim(start: str, end: str, tag: str) -> TrimOp:
    return TrimOp(
        src="media/a.mp4",
        range=TimeRange(start=_rt(start), end=_rt(end)),
        tag=tag,
    )


def _ir() -> EditIR:
    return EditIR(
        project_id="p_patch",
        revision=2,
        media_facts=_facts(),
        sequence=[
            _trim("0", "1", "a"),
            _trim("2", "3", "b"),
            _trim("4", "5", "c"),
        ],
    )


def test_patch_insert_replace_delete_keep_order() -> None:
    patched = apply_patch(
        _ir(),
        EditPatch(
            base_revision=2,
            ops=[
                PatchOp(op="insert", index=1, trims=[_trim("6", "7", "x")]),
                PatchOp(op="replace", index=2, count=1, trims=[_trim("8", "9", "y")]),
                PatchOp(op="delete", index=0),
            ],
        ),
    )

    assert [item.tag for item in patched.sequence] == ["x", "y", "c"]
    assert patched.revision == 2
    assert patched.project_id == "p_patch"


def test_patch_rejects_wrong_base_and_empty_sequence() -> None:
    with pytest.raises(IRValidationError, match="base_revision"):
        apply_patch(_ir(), EditPatch(base_revision=1, ops=[PatchOp(op="delete", index=0)]))

    with pytest.raises(IRValidationError, match="不能删除全部片段"):
        apply_patch(
            _ir(),
            EditPatch(base_revision=2, ops=[PatchOp(op="delete", index=0, count=3)]),
        )

    with pytest.raises(IRValidationError, match="越界"):
        apply_patch(
            _ir(),
            EditPatch(base_revision=2, ops=[PatchOp(op="delete", index=2, count=2)]),
        )


def _services(settings: Settings) -> tuple[Services, JobService, Database]:
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    return Services(settings, db, jobs, transcriber=NullTranscriber()), jobs, db


def test_service_applies_patch_as_new_revision(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    services, jobs, db = _services(settings)
    try:
        project_id = services.create_project("patch")["id"]
        services.ingest(project_id, str(synthetic_video))
        facts = services.get_media_facts(project_id)
        base_ir = EditIR(
            project_id=project_id,
            revision=0,
            media_facts=facts,
            sequence=[
                TrimOp(
                    src=facts.src,
                    range=TimeRange(start=_rt("0"), end=_rt("1")),
                    tag="a",
                )
            ],
        )
        created = services.submit_revision(project_id, base_ir)
        assert created["revision"] == 1

        result = services.apply_patch(
            project_id,
            1,
            EditPatch(
                base_revision=1,
                ops=[
                    PatchOp(
                        op="insert",
                        index=1,
                        trims=[
                            TrimOp(
                                src=facts.src,
                                range=TimeRange(start=_rt("1"), end=_rt("2")),
                                tag="b",
                            )
                        ],
                    )
                ],
                rationale="追加尾段",
            ),
        )

        assert result["revision"] == 2
        assert result["base_revision"] == 1
        assert [item["tag"] for item in result["ir"]["sequence"]] == ["a", "b"]
        revisions = services.list_revisions(project_id)
        assert [item["revision"] for item in revisions] == [1, 2]
        assert len(revisions[0]["ir"]["sequence"]) == 1
    finally:
        services.close()
        jobs.shutdown()
        db.dispose()
