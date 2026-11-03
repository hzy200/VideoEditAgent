from __future__ import annotations

import time
from contextlib import nullcontext
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from smartcut.config import Settings
from smartcut.contracts.perception import FrameRef
from smartcut.contracts.time import RationalTime
from smartcut.errors import RunCancelledError, ToolExecutionError
from smartcut.perception import process_pool, quality
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _frames(tmp_path: Path, count: int = 6) -> list[FrameRef]:
    refs = []
    for index in range(count):
        path = tmp_path / "frames" / f"{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("L", (160, 90), 0)
        draw = ImageDraw.Draw(image)
        if index % 3 == 0:
            draw.rectangle((0, 0, 159, 89), fill=255)
        else:
            for x in range(160):
                draw.line((x, 0, x, 89), fill=(x + index * 20) % 255)
        image.convert("RGB").save(path, format="JPEG")
        refs.append(
            FrameRef(
                index=index,
                t=RationalTime.from_seconds(str(index), rate=90_000),
                path=path.relative_to(tmp_path).as_posix(),
            )
        )
    return refs


def test_process_pool_quality_matches_serial(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    refs = _frames(tmp_path)
    serial = quality.analyze_quality(
        refs,
        tmp_path,
        "src.mp4",
        max_workers=1,
    )
    parallel = quality.analyze_quality(
        refs,
        tmp_path,
        "src.mp4",
        max_workers=2,
        process_pool_enabled=True,
        process_workers=2,
        pool_slot=lambda count: nullcontext(),
    )
    assert parallel.engine in {"process", "thread_fallback"}
    if parallel.engine == "process":
        assert [(mark.t.to_float(), mark.kind, mark.score) for mark in parallel.marks] == [
            (mark.t.to_float(), mark.kind, mark.score) for mark in serial.marks
        ]
    else:
        assert [(mark.t.to_float(), mark.kind) for mark in parallel.marks] == [
            (mark.t.to_float(), mark.kind) for mark in serial.marks
        ]


def test_process_pool_map_can_be_interrupted(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    refs = _frames(tmp_path, 8)
    paths = [str(tmp_path / ref.path) for ref in refs]
    calls = {"count": 0}

    def cancel_check() -> None:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise RunCancelledError("stop")

    started = time.monotonic()
    with pytest.raises(RunCancelledError):
        process_pool.map_quality(paths, 2, cancel_check=cancel_check)
    assert time.monotonic() - started < 20.0


def test_process_pool_watchdog_terminates_hung_batch(tmp_path: Path) -> None:
    pytest.importorskip("cv2")
    refs = _frames(tmp_path, 8)
    paths = [str(tmp_path / ref.path) for ref in refs]
    with pytest.raises(ToolExecutionError):
        process_pool.map_quality(paths, 2, timeout_seconds=0.01)


def test_services_enables_process_pool_for_long_tasks(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    pytest.importorskip("cv2")
    settings = settings.model_copy(
        update={
            "perception_process_pool": "on",
            "cpu_concurrency": 2,
        }
    )
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    try:
        project = services.create_project("process-pool")
        project_id = project["id"]
        services.ingest(project_id, str(synthetic_video))
        services.extract_frames(project_id)
        services.detect_scenes(project_id)
        result = services.analyze_quality(project_id)
        assert result["available"] is True
        assert process_pool.should_use_process_pool(settings, 10) is True
        # 合成视频帧数很少时可能退化为单帧线程分析。
        assert result["engine"] in {"process", "thread", "thread_fallback"}
        if result["engine"] == "process":
            assert services.resources.snapshot()["cpu"]["active"] == 0
    finally:
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()
