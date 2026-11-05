from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


def test_desktop_open_empty_project(tmp_path: Path, settings) -> None:
    """空项目（尚未导入素材）打开时不能因缺少 asset 而失败。"""
    from PySide6.QtWidgets import QApplication

    from smartcut.desktop.controller import DesktopController
    from smartcut.perception.transcribe import NullTranscriber
    from smartcut.server.db import Database
    from smartcut.server.jobs import JobService
    from smartcut.server.services import Services

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    controller = DesktopController(settings, services, jobs)
    try:
        project = services.create_project("empty")
        opened: list[dict] = []
        errors: list[str] = []
        controller.project_opened.connect(opened.append)
        controller.error.connect(errors.append)
        controller.open_project(project["id"])
        controller.pool.waitForDone(5000)
        app.processEvents()
        assert opened and opened[0]["id"] == project["id"]
        assert errors == []
    finally:
        controller.shutdown()
        services.close()
        jobs.shutdown(wait=True)
        db.dispose()


def test_job_poll_reads_sqlite_off_ui_thread(tmp_path: Path, settings) -> None:
    from PySide6.QtWidgets import QApplication

    from smartcut.desktop.controller import DesktopController
    from smartcut.perception.transcribe import NullTranscriber
    from smartcut.server.db import Database
    from smartcut.server.jobs import JobService
    from smartcut.server.services import Services

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    controller = DesktopController(settings, services, jobs)
    main_thread = threading.get_ident()

    class FakeJobs:
        def __init__(self) -> None:
            self.thread_id: int | None = None

        def get(self, job_id: str) -> dict:
            self.thread_id = threading.get_ident()
            return {
                "id": job_id,
                "project_id": "p_poll",
                "kind": "render",
                "status": "running",
                "progress": 10,
                "message": "执行中",
            }

    fake = FakeJobs()
    controller.jobs = fake  # type: ignore[assignment]
    controller._current_project = "p_poll"
    controller._watched = {
        "job_poll": {
            "kind": "render",
            "project_id": "p_poll",
            "run_id": None,
        }
    }
    progress: list[dict] = []
    controller.job_progress.connect(progress.append)
    try:
        controller._poll_jobs()
        controller.pool.waitForDone(5000)
        app.processEvents()
        assert fake.thread_id is not None
        assert fake.thread_id != main_thread
        assert progress and progress[0]["id"] == "job_poll"
    finally:
        controller.shutdown()
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_job_submit_runs_off_ui_thread(tmp_path: Path, settings) -> None:
    from PySide6.QtWidgets import QApplication

    from smartcut.desktop.controller import DesktopController
    from smartcut.perception.transcribe import NullTranscriber
    from smartcut.server.db import Database
    from smartcut.server.jobs import JobService
    from smartcut.server.services import Services

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    db = Database(settings.db_path)
    db.init()
    jobs = JobService(db, 1)
    services = Services(settings, db, jobs, transcriber=NullTranscriber())
    controller = DesktopController(settings, services, jobs)
    main_thread = threading.get_ident()

    class FakeJobs:
        def __init__(self) -> None:
            self.submit_thread: int | None = None

        def submit(self, project_id, kind, handler, *, on_cancel=None) -> str:
            self.submit_thread = threading.get_ident()
            return "job_submit_1"

        def list_jobs(self, project_id: str, *, limit: int = 200):
            return []

    fake = FakeJobs()
    controller.jobs = fake  # type: ignore[assignment]
    controller._current_project = "p_submit"
    try:
        controller.render(1)
        controller.pool.waitForDone(5000)
        app.processEvents()
        assert fake.submit_thread is not None
        assert fake.submit_thread != main_thread
        assert "job_submit_1" in controller._watched
    finally:
        controller.shutdown()
        services.close()
        jobs.shutdown(wait=True, timeout=5, cancel=True)
        db.dispose()


def test_controller_single_flight_guards(settings) -> None:
    """open_project/proxy/audio_waveforms 重复触发只允许一个在途任务。"""
    from PySide6.QtWidgets import QApplication

    from smartcut.desktop.controller import DesktopController

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    controller = DesktopController(settings, object(), object())  # type: ignore[arg-type]
    ops: list[str] = []
    controller._run = lambda op, fn: ops.append(op)  # type: ignore[assignment]
    project_id = "p_singleflight"
    controller._current_project = project_id
    try:
        controller.open_project(project_id)
        controller.open_project(project_id)
        assert ops == ["open_project"]
        assert controller._open_inflight == project_id

        # 失败回调必须清空单飞标记，允许用户重试。
        controller._on_task_failed("open_project", "boom")
        assert controller._open_inflight is None
        controller.open_project(project_id)
        assert ops.count("open_project") == 2
        controller._open_inflight = None

        controller.load_audio_waveforms()
        controller.load_audio_waveforms()
        assert ops.count("audio_waveforms") == 1
        controller._dispatch(
            "audio_waveforms", {"project_id": project_id, "data": {}}
        )
        controller.load_audio_waveforms()
        assert ops.count("audio_waveforms") == 2

        controller.build_proxy(project_id)
        controller.build_proxy(project_id)
        assert ops.count("proxy") == 1
        controller._dispatch(
            "proxy", {"project_id": project_id, "proxy": None}
        )
        controller.build_proxy(project_id)
        assert ops.count("proxy") == 2
    finally:
        controller.shutdown()


def test_desktop_app_smoke_offscreen(tmp_path: Path) -> None:
    """在 offscreen 平台构造完整主窗口，验证 GUI 装配不会崩溃。"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "smartcut.desktop.app",
            "--smoke-test",
            "--data-dir",
            str(tmp_path / "data"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
