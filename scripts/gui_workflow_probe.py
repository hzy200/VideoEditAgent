"""GUI 端到端探针：真实 MainWindow + 合成素材，输出关键阶段耗时。

用法：
    QT_QPA_PLATFORM=offscreen python scripts/gui_workflow_probe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from smartcut.config import Settings
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.main_window import MainWindow
from smartcut.desktop.settings_store import SettingsStore
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def _make_video(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _wait(app: QApplication, predicate, timeout: float = 120.0) -> float:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        app.processEvents()
        if predicate():
            return time.perf_counter() - started
        time.sleep(0.01)
    raise TimeoutError("GUI 流程等待超时")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="smartcut-gui-probe-"))
    video = tmp / "source.mp4"
    _make_video(video)
    data_dir = tmp / "data"
    settings = Settings(
        workspace_root=data_dir / "workspace",
        ffmpeg_bin=shutil.which("ffmpeg") or "ffmpeg",
        ffprobe_bin=shutil.which("ffprobe") or "ffprobe",
        deepseek_api_key=None,
        hardware_encoding="off",
        hardware_decoding="off",
        vision_use_evidence=False,
        planner_offline_fallback=True,
    )
    settings.ensure_dirs()
    app = QApplication.instance() or QApplication([])
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    controller = DesktopController(settings, services, jobs)
    window = None
    timings: dict[str, float] = {}
    try:
        project = services.create_project("gui-probe")
        project_id = project["id"]
        started = time.perf_counter()
        services.ingest(project_id, str(video))
        services.extract_frames(project_id)
        services.build_scenes(project_id) if hasattr(services, "build_scenes") else services.detect_scenes(project_id)
        services.build_contact_sheet(project_id)
        services.build_chapters(project_id)
        timings["prebuild_s"] = time.perf_counter() - started

        started = time.perf_counter()
        store = SettingsStore(data_dir)
        timings["settings_store_s"] = time.perf_counter() - started

        started = time.perf_counter()
        window = MainWindow(controller, settings, store, data_dir)
        timings["window_init_s"] = time.perf_counter() - started

        started = time.perf_counter()
        window.show()
        app.processEvents()
        timings["window_show_s"] = time.perf_counter() - started
        timings["window_construct_s"] = (
            timings["settings_store_s"]
            + timings["window_init_s"]
            + timings["window_show_s"]
        )

        opened: list[dict] = []
        controller.project_opened.connect(opened.append)
        started = time.perf_counter()
        controller.open_project(project_id)
        _wait(app, lambda: bool(opened), timeout=60)
        timings["open_project_s"] = time.perf_counter() - started

        started = time.perf_counter()
        for index in range(window.tabs.count()):
            window.tabs.setCurrentIndex(index)
            app.processEvents()
        timings["tab_switch_s"] = time.perf_counter() - started

        started = time.perf_counter()
        if window.timeline_panel.table.rowCount() > 0:
            window.timeline_panel.table.selectRow(0)
            window.timeline_panel.split_selected()
            window.timeline_panel.undo()
            window.timeline_panel.redo()
        app.processEvents()
        timings["timeline_edit_s"] = time.perf_counter() - started

        candidates: list[dict] = []
        controller.candidates_changed.connect(candidates.append)
        started = time.perf_counter()
        controller.search_candidates("精彩")
        _wait(app, lambda: bool(candidates), timeout=60)
        timings["candidate_search_s"] = time.perf_counter() - started

        evidence: list[dict] = []
        controller.evidence_changed.connect(evidence.append)
        started = time.perf_counter()
        controller.build_evidence("contact_sheet")
        _wait(app, lambda: bool(evidence), timeout=120)
        timings["evidence_build_s"] = time.perf_counter() - started

        runs: list[dict] = []
        controller.run_finished.connect(runs.append)
        started = time.perf_counter()
        window._on_orchestrate("生成一个 1 秒的精彩片段", False, False)
        _wait(app, lambda: bool(runs), timeout=180)
        timings["orchestrate_no_render_s"] = time.perf_counter() - started
        timings["run_status"] = 1.0 if runs[-1].get("status") == "succeeded" else 0.0
        timings["artifacts"] = float(len(runs[-1].get("artifacts") or []))

        revisions = services.list_revisions(project_id) or []
        if revisions:
            revision_no = max(int(item["revision"]) for item in revisions)
            completed: list[dict] = []
            controller.job_finished.connect(
                lambda _job_id, job: completed.append(job)
            )
            started = time.perf_counter()
            window._on_render(revision_no)
            _wait(
                app,
                lambda: any(
                    job.get("kind") == "render"
                    and job.get("status") in {"succeeded", "failed", "cancelled"}
                    for job in completed
                ),
                timeout=180,
            )
            timings["render_s"] = time.perf_counter() - started
            output = (
                Path(window.current_project["workspace_dir"])
                / "renders"
                / f"r{revision_no}"
                / "output.mp4"
            )
            timings["render_output"] = 1.0 if output.is_file() else 0.0

        print("GUI probe timings:")
        for key, value in timings.items():
            print(f"  {key}: {value:.3f}")
        return 0
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
            app.processEvents()
        controller.shutdown()
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
