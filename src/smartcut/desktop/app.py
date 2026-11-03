"""SmartCut 桌面客户端入口。

开发态：
    python -m smartcut.desktop.app

打包后：
    SmartCut.exe

仅 DeepSeek 走网络；ffmpeg、转写模型、SQLite、工作区全部本地。
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from smartcut.config import Settings, load_settings
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.main_window import MainWindow
from smartcut.desktop.paths import (
    app_data_dir,
    bundled_binary,
    bundled_separator,
    bundled_whisper_dir,
)
from smartcut.desktop.settings_store import SettingsStore
from smartcut.logging import configure_logging
from smartcut.runner.paths import Workspace
from smartcut.runner.selfcheck import (
    probe_binary,
    probe_separator,
    write_test_tone,
)
from smartcut.server.db import Database
from smartcut.server.jobs import JobService
from smartcut.server.services import Services


def main(argv: list[str] | None = None) -> int:
    # 打包后 whisper worker 使用 spawn 重新进入 exe；必须在创建 QApplication
    # 与任何子进程之前调用，否则子进程会尝试启动完整桌面应用。
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="SmartCut 桌面客户端")
    parser.add_argument("--smoke-test", action="store_true", help="构建窗口后立即退出")
    parser.add_argument("--self-check", action="store_true", help="验证内嵌 ffmpeg/whisper")
    parser.add_argument("--data-dir", type=str, default=None, help="覆盖应用数据目录")
    parser.add_argument("--offscreen", action="store_true", help="使用 Qt offscreen 平台")
    args, _ = parser.parse_known_args(argv)

    if args.smoke_test or args.self_check or args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    app = QApplication([sys.argv[0]])
    app.setApplicationName("SmartCut")
    app.setOrganizationName("SmartCut")
    app.setStyle("Fusion")

    data_dir = app_data_dir(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    lock: QLockFile | None = None
    if not args.smoke_test and not args.self_check:
        lock = QLockFile(str(data_dir / "smartcut.lock"))
        if not lock.tryLock(100):
            QMessageBox.warning(None, "SmartCut", "SmartCut 已在运行。")
            return 1

    controller: DesktopController | None = None
    services: Services | None = None
    jobs: JobService | None = None
    db: Database | None = None
    try:
        store = SettingsStore(data_dir)
        config = store.load()
        base = load_settings()
        if not store.path.exists():
            config.deepseek_base_url = base.deepseek_base_url
            config.deepseek_model = base.deepseek_model
            config.whisper_model = base.whisper_model or "base"

        api_key = store.get_api_key() or base.deepseek_api_key
        whisper_dir = bundled_whisper_dir(config.whisper_model)
        settings = Settings(
            workspace_root=data_dir / "workspace",
            ffmpeg_bin=bundled_binary("ffmpeg"),
            ffprobe_bin=bundled_binary("ffprobe"),
            max_workers=base.max_workers,
            max_pending_jobs=base.max_pending_jobs,
            deepseek_api_key=api_key,
            deepseek_base_url=config.deepseek_base_url,
            deepseek_model=config.deepseek_model,
            orchestrator_max_parallel=config.orchestrator_max_parallel,
            orchestrator_use_llm=config.orchestrator_use_llm,
            llm_decision_summary=config.llm_decision_summary,
            hardware_encoding=config.hardware_encoding or base.hardware_encoding,
            encoder_priority=base.encoder_priority,
            vision_base_url=config.vision_base_url or base.vision_base_url,
            vision_model=config.vision_model or base.vision_model,
            vision_api_key=store.get_vision_api_key() or base.vision_api_key,
            demucs_command=(
                config.demucs_command
                or bundled_separator()
                or base.demucs_command
            ),
            demucs_model=config.demucs_model or base.demucs_model,
            perception_max_frames=config.perception_max_frames,
            transcribe_workers=config.transcribe_workers,
            transcribe_silence_rms=config.transcribe_silence_rms,
            transcribe_silence_min_seconds=config.transcribe_silence_min_seconds,
            audio_filler_words=config.audio_filler_words,
            caption_font_name=base.caption_font_name,
            caption_font_size=config.caption_font_size,
            caption_color=config.caption_color,
            caption_position=config.caption_position,
            whisper_model=config.whisper_model,
            whisper_model_path=str(whisper_dir) if whisper_dir else None,
            whisper_device=base.whisper_device,
            whisper_compute_type=base.whisper_compute_type,
        )
        settings.ensure_dirs()
        configure_logging(settings.log_file)

        if args.self_check:
            return _run_self_check(settings, data_dir)

        db = Database(settings.db_path)
        db.init()
        jobs = JobService(
            db,
            settings.max_workers,
            max_pending=settings.max_pending_jobs,
        )
        services = Services(settings, db, jobs)
        controller = DesktopController(settings, services, jobs)
        window = MainWindow(controller, settings, store, data_dir)
        window.show()

        if args.smoke_test:
            QTimer.singleShot(400, app.quit)

        return app.exec()
    except Exception:
        traceback.print_exc()
        if args.self_check:
            (data_dir / "selfcheck.json").write_text(
                json.dumps(
                    {"ok": False, "error": traceback.format_exc()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        elif not args.smoke_test:
            QMessageBox.critical(None, "SmartCut 启动失败", traceback.format_exc())
        return 1
    finally:
        if controller is not None:
            controller.shutdown()
        if jobs is not None:
            # 先停止/取消任务并限时等待，再关闭 HTTP 客户端与 DB，
            # 避免运行中的作业命中已关闭的 client 或已 dispose 的引擎。
            jobs.shutdown(wait=True, timeout=15.0, cancel=True)
        if services is not None:
            services.close()
        if db is not None:
            db.dispose()
        if lock is not None:
            lock.unlock()


def _run_self_check(settings: Settings, data_dir: Path) -> int:
    checks: dict[str, dict] = {}
    workspace = Workspace(data_dir / "selfcheck-workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    for name, path in (
        ("ffmpeg", settings.ffmpeg_bin),
        ("ffprobe", settings.ffprobe_bin),
    ):
        checks[name] = probe_binary(settings, workspace, path, name=name)

    try:
        import cv2

        cascade = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        checks["opencv"] = {
            "ok": cascade.exists(),
            "version": cv2.__version__,
            "haar_cascade": str(cascade),
        }
    except Exception as exc:
        checks["opencv"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        import scenedetect

        checks["scenedetect"] = {"ok": True, "version": scenedetect.__version__}
    except Exception as exc:
        checks["scenedetect"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        from smartcut.runner.separation import available, resolve_command

        command = resolve_command(settings)
        detail: dict = {
            "ok": True,
            "available": command is not None,
            "model": settings.demucs_model,
            "command": command,
        }
        is_worker = any(
            "separation_worker" in part or "SmartCut-Separator" in part
            for part in (command or [])
        )
        if command is not None and is_worker:
            detail["worker"] = probe_separator(command)
        checks["demucs"] = detail
    except Exception as exc:
        checks["demucs"] = {
            "ok": True,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    try:
        from smartcut.policy import load_policy
        from smartcut.runner.encoders import EncoderSelector
        from smartcut.runner.execute import ProcessRunner

        selector = EncoderSelector(settings, load_policy())
        checks["encoders"] = {
            "ok": True,
            **selector.summary(ProcessRunner(settings, workspace)),
        }
    except Exception as exc:
        checks["encoders"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    model_path = Path(settings.whisper_model_path) if settings.whisper_model_path else None
    model_ok = bool(model_path and (model_path / "model.bin").exists())
    checks["whisper_model"] = {
        "path": str(model_path) if model_path else None,
        "ok": model_ok,
    }
    if model_ok:
        try:
            from smartcut.perception.transcribe import FasterWhisperTranscriber

            sample = write_test_tone(data_dir / "selfcheck.wav")
            transcriber = FasterWhisperTranscriber(
                model_size=str(model_path),
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
            transcript = transcriber.transcribe(sample)
            checks["whisper_transcribe"] = {
                "ok": True,
                "segments": len(transcript.segments),
                "words": len(transcript.words),
            }
        except Exception as exc:
            checks["whisper_transcribe"] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        checks["whisper_transcribe"] = {"ok": False, "error": "模型路径不存在"}

    ok = all(item.get("ok") for item in checks.values())
    (data_dir / "selfcheck.json").write_text(
        json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
