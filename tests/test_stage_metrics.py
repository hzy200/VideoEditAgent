from __future__ import annotations

from datetime import datetime, timezone

from smartcut.orchestrator.models import TaskOutcome, TaskStatus
from smartcut.orchestrator.orchestrator import Orchestrator


def test_collect_metrics_aggregates_vlm_and_stage_metrics() -> None:
    now = datetime.now(timezone.utc)
    outcome = TaskOutcome(
        task_id="plan_auto",
        agent="plan",
        status=TaskStatus.succeeded,
        queue_wait_ms=5,
        duration_ms=20,
        output={
            "usage": {
                "requests": 1,
                "cache_hits": 0,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
            "metrics": {
                "vision": {
                    "images": 2,
                    "image_bytes": 4096,
                    "cache_hits": 1,
                },
                "stages": {"vision": 12, "plan": 8},
            },
            "decoded_frames": 10,
            "analyzed_frames": 6,
            "dropped_frames": 4,
        },
    )
    metrics = Orchestrator._collect_metrics([outcome], now, now)
    totals = metrics["totals"]
    assert totals["vlm_images"] == 2
    assert totals["vlm_image_bytes"] == 4096
    assert totals["vlm_cache_hits"] == 1
    assert totals["frames_extracted"] == 10
    assert totals["frames_analyzed"] == 6
    assert totals["frames_dropped"] == 4
    assert totals["stage_durations_ms"] == {"vision": 12, "plan": 8}
    assert totals["total_tokens"] == 120
    task = metrics["tasks"][0]
    assert task["vlm_images"] == 2
    assert task["stage_durations_ms"]["vision"] == 12
