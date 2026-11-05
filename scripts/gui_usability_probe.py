"""正常路径 GUI 可用性探针（不经过 pytest/monkeypatch）。

使用生产 MainWindow + DesktopController + Services + 真实 ffmpeg，
覆盖导入 → 源文件即时预览 → 基线时间线 → 手动编辑 → 保存 →
dry-run → 渲染 → 转码 → 媒体诊断/修复 → 删除项目。

用法：
    python scripts/gui_usability_probe.py
    SMARTCUT_PROBE_OFFSCREEN=1 python scripts/gui_usability_probe.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

if os.environ.get("SMARTCUT_PROBE_OFFSCREEN") == "1":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from smartcut.config import Settings
from smartcut.contracts.media_ops import RepairRequest
from smartcut.contracts.transcode import AudioOutput, TranscodeProfile, TranscodeRequest
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.main_window import MainWindow
from smartcut.desktop.settings_store import SettingsStore
from smartcut.perception.transcribe import NullTranscriber
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services

STAGES: list[tuple[str, bool, str]] = []


def stage(name: str, ok: bool, detail: str = "") -> None:
    STAGES.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" · {detail}" if detail else ""))


def wait(app: QApplication, predicate, timeout: float = 30.0, label: str = "") -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    print(f"[WAIT] {label or 'condition'} timeout={timeout}s")
    return False


def make_video(path: Path, ffmpeg: str) -> None:
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


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("ffmpeg/ffprobe not found")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="smartcut-usability-"))
    video = tmp / "source.mp4"
    make_video(video, ffmpeg)
    data_dir = tmp / "data"
    settings = Settings(
        workspace_root=data_dir / "workspace",
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
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
    started = time.perf_counter()
    try:
        project = services.create_project("usability")
        project_id = project["id"]
        store = SettingsStore(data_dir)
        window = MainWindow(controller, settings, store, data_dir)
        window.show()
        app.processEvents()

        opened: list[dict] = []
        controller.project_opened.connect(opened.append)
        controller.open_project(project_id)
        if not wait(app, lambda: bool(opened), 30, "open empty project"):
            return 1

        mode_history: list[str] = window.preview_panel.mode_history
        asset_events: list[dict] = []
        controller.asset_changed.connect(asset_events.append)
        opened_count = len(opened)
        controller.upload_media(video)
        if not wait(
            app,
            lambda: bool(asset_events) and len(opened) > opened_count,
            60,
            "upload + reopen",
        ):
            return 1
        app.processEvents()

        mode = window.preview_panel.mode_label.text()
        source_seen = any("源文件预览" in item for item in mode_history)
        stage(
            "导入后立即显示源文件预览",
            source_seen or "代理预览" in mode,
            f"final={mode!r} history={mode_history!r}",
        )
        stage(
            "源预览无需等待代理",
            source_seen,
            f"history={mode_history!r}",
        )

        loaded = wait(
            app,
            lambda: window.preview_panel.play_button.isEnabled() or "加载中" not in window.preview_panel.time_label.text(),
            15,
            "media duration",
        )
        stage(
            "预览时长已刷新",
            loaded and "00:00.000 / 00:00.000" not in window.preview_panel.time_label.text(),
            window.preview_panel.time_label.text(),
        )

        baseline = wait(
            app,
            lambda: window.timeline_panel.table.rowCount() > 0,
            20,
            "baseline timeline",
        )
        stage(
            "导入后时间线立即可编辑",
            baseline,
            f"rows={window.timeline_panel.table.rowCount()}",
        )

        if baseline:
            panel = window.timeline_panel
            widget = panel.timeline
            app.processEvents()
            initial_rows = panel.table.rowCount()
            panel.table.selectRow(0)
            first = panel.segments()[0]
            panel.set_position(
                (float(first["start"]) + float(first["end"])) / 2
            )
            panel.split_selected()
            app.processEvents()
            split_ok = panel.table.rowCount() == initial_rows + 1

            _ruler, _thumbs, _wave, lane = widget._lanes()
            y = int(lane.center().y())
            dx = max(12, int(lane.width() * 0.05))

            segment = panel.segments()[0]
            old_start = float(segment["start"])
            mid = (old_start + float(segment["end"])) / 2
            x = lane.left() + lane.width() * mid / max(widget.duration, 1e-6)
            QTest.mousePress(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(x), y)
            )
            QTest.mouseMove(widget, QPoint(int(x + dx), y))
            QTest.mouseRelease(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(x + dx), y)
            )
            app.processEvents()
            moved_start = float(panel.segments()[0]["start"])
            drag_ok = moved_start > old_start + 0.01
            window._undo()
            app.processEvents()
            drag_restored = (
                panel.table.rowCount() == initial_rows + 1
                and abs(float(panel.segments()[0]["start"]) - old_start) < 1e-6
            )

            segment = panel.segments()[0]
            old_end = float(segment["end"])
            right = lane.left() + lane.width() * old_end / max(widget.duration, 1e-6)
            QTest.mousePress(
                widget,
                Qt.MouseButton.LeftButton,
                pos=QPoint(int(right - 2), y),
            )
            QTest.mouseMove(widget, QPoint(int(right - dx), y))
            QTest.mouseRelease(
                widget,
                Qt.MouseButton.LeftButton,
                pos=QPoint(int(right - dx), y),
            )
            app.processEvents()
            trimmed_end = float(panel.segments()[0]["end"])
            trim_ok = trimmed_end < old_end - 0.01
            window._undo()
            app.processEvents()

            panel.table.selectRow(0)
            before_buttons = panel.table.rowCount()
            panel.delete_button.click()
            delete_ok = panel.table.rowCount() == before_buttons - 1
            window._undo()
            app.processEvents()
            undo_ok = panel.table.rowCount() == before_buttons
            panel.add_button.click()
            app.processEvents()
            add_ok = panel.table.rowCount() == before_buttons + 1
            window._undo()
            app.processEvents()
            panel.zoom_in_button.click()
            zoom_ok = panel.timeline.zoom > 1.0
            panel.fit_button.click()
            fit_ok = abs(panel.timeline.zoom - 1.0) < 1e-6

            panel.table.selectRow(0)
            segment = panel.segments()[0]
            panel.set_position(
                (float(segment["start"]) + float(segment["end"])) / 2
            )
            before_split = panel.table.rowCount()
            panel.split_button.click()
            split_button_ok = panel.table.rowCount() == before_split + 1
            window._undo()
            app.processEvents()
            split_undo_ok = panel.table.rowCount() == before_split
            window._redo()
            app.processEvents()
            split_redo_ok = panel.table.rowCount() == before_split + 1
            window._undo()
            app.processEvents()

            stage(
                "时间线鼠标拖动/边缘裁剪/按钮",
                drag_ok
                and drag_restored
                and trim_ok
                and delete_ok
                and undo_ok
                and add_ok
                and zoom_ok
                and fit_ok,
                f"drag={drag_ok} restore={drag_restored} trim={trim_ok} "
                f"delete={delete_ok} undo={undo_ok} add={add_ok} "
                f"zoom={zoom_ok} fit={fit_ok}",
            )
            stage(
                "时间线分割/撤销/重做",
                split_ok and split_button_ok and split_undo_ok and split_redo_ok,
                f"initial={split_ok} button={split_button_ok} "
                f"undo={split_undo_ok} redo={split_redo_ok}",
            )

            # 磁性吸附：把第一段起点拖到播放头附近，应吸附到 1.0s。
            panel.table.selectRow(0)
            panel.timeline.set_position(1.0)
            segment = panel.segments()[0]
            mid = (float(segment["start"]) + float(segment["end"])) / 2
            x = lane.left() + lane.width() * mid / max(widget.duration, 1e-6)
            target = lane.left() + lane.width() * 2.0 / max(
                widget.duration, 1e-6
            )
            QTest.mousePress(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(x), y)
            )
            QTest.mouseMove(widget, QPoint(int(target), y))
            QTest.mouseRelease(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(target), y)
            )
            app.processEvents()
            snapped_start = float(panel.segments()[0]["start"])
            stage(
                "磁性吸附到播放头",
                abs(snapped_start - 1.0) < 0.05,
                f"start={snapped_start:.3f}",
            )
            window._undo()
            app.processEvents()

            # 拖拽转场：片段边界顶部热区向右拖出 fade。
            panel.timeline.set_transitions([])
            boundary = panel.segments()[0]["end"]
            bx = lane.left() + lane.width() * float(boundary) / max(
                widget.duration, 1e-6
            )
            by = int(lane.top() + 4)
            QTest.mousePress(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(bx), by)
            )
            QTest.mouseMove(widget, QPoint(int(bx) + 50, by))
            QTest.mouseRelease(
                widget, Qt.MouseButton.LeftButton, pos=QPoint(int(bx) + 50, by)
            )
            app.processEvents()
            transitions = window._timing.get("transitions") or []
            stage(
                "拖拽转场",
                bool(transitions),
                f"count={len(transitions)}",
            )
        second_video = tmp / "second.mp4"
        make_video(second_video, ffmpeg)
        opened_before_second = len(opened)
        controller.add_media_asset(str(second_video))
        second_ok = wait(
            app,
            lambda: len(opened) > opened_before_second,
            60,
            "add second asset",
        )
        assets = services.list_media_assets(project_id)
        second_asset = next(
            (item for item in assets if not item.get("is_primary")), None
        )
        if second_ok and second_asset is not None:
            # open_project 的 revisions_changed/revision_loaded 可能晚于 project_opened。
            time.sleep(0.2)
            app.processEvents()
            window.timeline_panel.add_source_segment(second_asset, 0.5)
            app.processEvents()
        multi_bin_ok = (
            second_ok
            and second_asset is not None
            and len(assets) == 2
            and any(
                segment.get("src") == second_asset.get("src")
                for segment in window.timeline_panel.segments()
            )
        )
        stage(
            "多素材 bin + 拖入时间线",
            multi_bin_ok,
            f"opened={second_ok} assets={len(assets)} "
            f"second={bool(second_asset)} "
            f"rows={window.timeline_panel.table.rowCount()} "
            f"status={window.timeline_panel.status_label.text()!r}",
        )

        if second_asset is not None:
            window.project_panel.media_list.setCurrentRow(1)
            window.project_panel.pip_button.click()
            app.processEvents()
            pip_rows = [
                segment
                for segment in window.timeline_panel.segments()
                if int(segment.get("track") or 0) > 0
            ]
            stage(
                "画中画添加",
                bool(pip_rows),
                f"pip_clips={len(pip_rows)}",
            )


        preview_results: list[dict] = []
        controller.effects_preview_ready.connect(preview_results.append)
        window.timeline_panel.preview_button.click()
        preview_ok = wait(
            app, lambda: bool(preview_results), 180, "timeline preview"
        )
        stage(
            "时间线连续预览",
            preview_ok
            and bool(preview_results[-1].get("path"))
            and not preview_results[-1].get("error"),
            f"legacy={preview_results[-1].get('legacy') if preview_results else None} "
            f"mode={window.preview_panel.mode_label.text()!r}",
        )

        # 预览播放（QtMultimedia 不可用时只记录，不判失败）
        try:
            window.preview_panel.toggle_play()
            playing = wait(
                app,
                window.preview_panel.is_playing,
                3.0,
                "playback started",
            )
            detail = f"playing={playing}"
            if not playing and window.preview_panel.player is not None:
                detail += (
                    f" state={window.preview_panel.player.playbackState()}"
                    f" error={window.preview_panel.player.errorString()!r}"
                )
            window.preview_panel.toggle_play()
            stage("预览可播放/暂停", playing, detail)
        except Exception as exc:
            stage("预览可播放/暂停", False, f"{type(exc).__name__}: {exc}")

        revisions_before = len(services.list_revisions(project_id))
        window._on_save_revision()
        saved = wait(
            app,
            lambda: len(services.list_revisions(project_id)) > revisions_before,
            30,
            "save revision",
        )
        stage("手动编辑保存为新版本", saved)
        revisions = services.list_revisions(project_id)
        revision_no = max(int(item["revision"]) for item in revisions)

        dry_done: list[tuple[str, object]] = []
        controller.operation_done.connect(
            lambda op, result: dry_done.append((op, result))
        )
        window._on_dry_run(revision_no)
        dry_ok = wait(
            app,
            lambda: any(op == "dry_run" for op, _ in dry_done),
            30,
            "dry run",
        )
        stage("dry-run 生成 ToolCall 计划", dry_ok)

        jobs_done: list[dict] = []
        controller.job_finished.connect(
            lambda _job_id, job: jobs_done.append(job)
        )
        window._on_render(revision_no)
        render_ok = wait(
            app,
            lambda: any(
                job.get("kind") == "render"
                and job.get("status") in {"succeeded", "failed"}
                for job in jobs_done
            ),
            180,
            "render",
        )
        render_job = next(
            (job for job in jobs_done if job.get("kind") == "render"),
            {},
        )
        stage(
            "渲染 revision",
            render_ok and render_job.get("status") == "succeeded",
            f"status={render_job.get('status')}",
        )

        transcode_request = TranscodeRequest(
            profile=TranscodeProfile(
                container="mkv",
                video_codec="libx264",
                preset="veryfast",
                crf=30,
                fps="30",
                audio_tracks=[AudioOutput(codec="aac", bitrate="96k")],
            )
        )
        controller.transcode(transcode_request)
        transcode_ok = wait(
            app,
            lambda: any(
                job.get("kind") == "transcode"
                and job.get("status") in {"succeeded", "failed"}
                for job in jobs_done
            ),
            180,
            "transcode",
        )
        transcode_job = next(
            (job for job in jobs_done if job.get("kind") == "transcode"),
            {},
        )
        stage(
            "格式转换/压缩",
            transcode_ok and transcode_job.get("status") == "succeeded",
            f"status={transcode_job.get('status')}",
        )

        diagnoses: list[dict] = []
        controller.media_diagnosis.connect(diagnoses.append)
        controller.diagnose_media()
        diag_ok = wait(app, lambda: bool(diagnoses), 30, "media diagnose")
        stage(
            "媒体诊断",
            diag_ok and diagnoses[-1].get("probe_ok") is True,
            f"issues={len(diagnoses[-1].get('issues') or [])}" if diagnoses else "",
        )

        controller.repair_media(RepairRequest(strategy="auto"))
        repair_ok = wait(
            app,
            lambda: any(
                job.get("kind") == "media_repair"
                and job.get("status") in {"succeeded", "failed"}
                for job in jobs_done
            ),
            180,
            "media repair",
        )
        repair_job = next(
            (job for job in jobs_done if job.get("kind") == "media_repair"),
            {},
        )
        stage(
            "媒体修复",
            repair_ok and repair_job.get("status") == "succeeded",
            f"status={repair_job.get('status')}",
        )

        understand_results: list[dict] = []
        controller.operation_done.connect(
            lambda op, result: understand_results.append(result)
            if op == "understand"
            else None
        )
        revisions_before_understand = len(services.list_revisions(project_id))
        window.ai_panel.set_project_enabled(True)
        window.ai_panel.goal_edit.setPlainText("总结这个视频的主要内容")
        window.ai_panel.understand_only.setChecked(True)
        window.ai_panel.run_button.click()
        understand_ok = wait(
            app, lambda: bool(understand_results), 180, "understand"
        )
        revisions_after_understand = len(services.list_revisions(project_id))
        stage(
            "AI 仅理解并文字回答（不改时间线）",
            understand_ok
            and bool(understand_results[-1].get("answer"))
            and revisions_after_understand == revisions_before_understand,
            f"answer_chars={len(understand_results[-1].get('answer') or '') if understand_results else 0} "
            f"revisions={revisions_before_understand}->{revisions_after_understand}",
        )

        projects_seen: list[list[dict]] = []
        controller.projects_changed.connect(projects_seen.append)
        controller.delete_project(project_id)
        deleted = wait(
            app,
            lambda: bool(projects_seen)
            and not any(item.get("id") == project_id for item in projects_seen[-1]),
            30,
            "delete project",
        )
        stage(
            "删除项目并清理状态",
            deleted and window.current_project is None,
            f"current={window.current_project}",
        )

        elapsed = time.perf_counter() - started
        failed = [name for name, ok, _ in STAGES if not ok]
        print(f"\n正常路径 GUI 探针完成: {len(STAGES) - len(failed)}/{len(STAGES)} 通过, {elapsed:.2f}s")
        if failed:
            print("失败项: " + ", ".join(failed))
        return 0 if not failed else 1
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
