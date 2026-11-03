"""长视频分段性能基准（可复用 runner + CLI）。"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

from smartcut.config import Settings
from smartcut.perception.transcribe import NullTranscriber, Transcriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def run_benchmark(
    settings: Settings,
    src: str | Path,
    *,
    transcriber: Transcriber | None = None,
    include_transcribe: bool = False,
    repeat: int = 1,
) -> dict:
    """按阶段执行感知/章节/清单，返回 wall time 与缓存命中。"""
    repeat = max(1, int(repeat))
    with tempfile.TemporaryDirectory(prefix="smartcut-bench-") as tmp:
        root = Path(tmp) / "workspace"
        bench_settings = settings.model_copy(update={"workspace_root": root})
        db = Database(bench_settings.db_path)
        db.init()
        jobs = JobService(db, 1)
        services = Services(
            bench_settings,
            db,
            jobs,
            transcriber=transcriber
            or (None if include_transcribe else NullTranscriber()),
        )
        stages: list[dict] = []
        try:
            project = services.create_project("benchmark")
            project_id = project["id"]

            def measure(name: str, fn) -> dict:
                started = time.perf_counter()
                result = fn()
                duration_ms = int((time.perf_counter() - started) * 1000)
                entry = {
                    "stage": name,
                    "duration_ms": duration_ms,
                    "cached": bool(
                        result.get("cached") if isinstance(result, dict) else False
                    ),
                }
                if isinstance(result, dict):
                    for key in (
                        "count",
                        "analyzed_frames",
                        "dropped_frames",
                        "engine",
                        "chapter_count",
                        "decoder",
                        "transcribe_workers",
                        "transcribe_device",
                    ):
                        if key in result:
                            entry[key] = result[key]
                stages.append(entry)
                return entry

            measure(
                "ingest",
                lambda: services.ingest(project_id, str(src)),
            )
            for run_index in range(repeat):
                prefix = f"run{run_index + 1}_"
                measure(
                    prefix + "frames",
                    lambda: services.extract_frames(project_id),
                )
                measure(
                    prefix + "scenes",
                    lambda: services.detect_scenes(project_id),
                )
                measure(
                    prefix + "quality",
                    lambda: services.analyze_quality(project_id),
                )
                measure(
                    prefix + "faces",
                    lambda: services.detect_faces(project_id),
                )
                measure(
                    prefix + "contact_sheet",
                    lambda: services.build_contact_sheet(project_id),
                )
                if include_transcribe:
                    measure(
                        prefix + "transcribe",
                        lambda: services.transcribe(project_id),
                    )
                measure(
                    prefix + "chapters",
                    lambda: services.build_chapters(project_id),
                )
                measure(
                    prefix + "manifest",
                    lambda: services.build_video_manifest(project_id),
                )
        finally:
            services.close()
            jobs.shutdown(wait=True)
            db.dispose()

    total_ms = sum(item["duration_ms"] for item in stages)
    by_stage: dict[str, int] = {}
    for item in stages:
        by_stage[item["stage"]] = by_stage.get(item["stage"], 0) + item["duration_ms"]
    cached = sum(1 for item in stages if item["cached"])
    return {
        "source": str(src),
        "repeat": repeat,
        "stages": stages,
        "by_stage_duration_ms": by_stage,
        "total_ms": total_ms,
        "cache_hits": cached,
        "stage_count": len(stages),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SmartCut 长视频阶段性能基准")
    parser.add_argument("--src", required=True, help="输入视频路径")
    parser.add_argument("--repeat", type=int, default=2, help="重复阶段次数")
    parser.add_argument(
        "--transcribe",
        action="store_true",
        help="包含转写阶段（需配置 whisper 依赖/模型）",
    )
    parser.add_argument("--process-pool", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--event-json", action="store_true", help="输出单行 JSON")
    args = parser.parse_args(argv)

    settings = Settings(
        perception_process_pool=args.process_pool,
        hardware_decoding="off",
    )
    transcriber = None
    if args.transcribe:
        from smartcut.perception.transcribe import build_transcriber

        transcriber = build_transcriber(settings)
    payload = run_benchmark(
        settings,
        args.src,
        transcriber=transcriber,
        include_transcribe=args.transcribe,
        repeat=args.repeat,
    )
    if args.event_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
