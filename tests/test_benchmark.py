from __future__ import annotations

from pathlib import Path

from smartcut.benchmark import run_benchmark
from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber


def test_benchmark_records_stage_timings_and_cache(
    settings: Settings,
    synthetic_video: Path,
) -> None:
    settings = settings.model_copy(
        update={"perception_process_pool": "off", "hardware_decoding": "off"}
    )
    result = run_benchmark(
        settings,
        synthetic_video,
        transcriber=NullTranscriber(),
        repeat=2,
    )
    stages = {item["stage"]: item for item in result["stages"]}
    assert "ingest" in stages
    assert "run1_frames" in stages
    assert "run2_frames" in stages
    assert "run2_chapters" in stages
    assert stages["run2_frames"]["cached"] is True
    assert stages["run1_quality"]["engine"] in {"thread", "process", "thread_fallback"}
    assert result["total_ms"] >= 0
    assert result["stage_count"] == len(result["stages"])
