from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from smartcut.contracts.media_ops import RepairRequest
from smartcut.contracts.transcode import TranscodeProfile, TranscodeRequest
from smartcut.desktop.controller import DesktopController
from smartcut.desktop.main_window import MainWindow
from smartcut.desktop.settings_store import SettingsStore


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_media_buttons_route_to_controller(
    app,
    tmp_path: Path,
    settings,
    monkeypatch,
) -> None:
    # 只验证 GUI→Controller 路由；不启动真实 Job/DB 线程，避免 Windows
    # 下 PySide6 解释器退出阶段的已知清理问题。
    controller = DesktopController(settings, object(), object())  # type: ignore[arg-type]
    store = SettingsStore(tmp_path / "data")
    calls: list[tuple] = []
    # 构造窗口时会自动 refresh_projects；测试不依赖真实线程/DB 查询。
    monkeypatch.setattr(controller, "refresh_projects", lambda: None)
    monkeypatch.setattr(
        controller, "diagnose_media", lambda src=None: calls.append(("diagnose", src))
    )
    monkeypatch.setattr(
        controller, "repair_media", lambda request: calls.append(("repair", request))
    )
    monkeypatch.setattr(
        controller, "transcode", lambda request: calls.append(("transcode", request))
    )
    monkeypatch.setattr(
        controller,
        "interpret_media_goal",
        lambda goal, src=None: calls.append(("interpret", goal, src)),
    )
    monkeypatch.setattr(
        controller,
        "import_media_for_tools",
        lambda path: calls.append(("import", path)),
    )
    monkeypatch.setattr(
        controller,
        "understand_video",
        lambda question: calls.append(("understand", question)),
    )
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: ("C:/tmp/broken.mp4", "")),
    )
    class FakeRepairDialog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def apply_plan(self, plan) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def request(self) -> RepairRequest:
            return RepairRequest(strategy="auto")

    class FakeExportDialog:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def apply_plan(self, plan) -> None:
            pass

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

        def request(self) -> TranscodeRequest:
            return TranscodeRequest(
                profile=TranscodeProfile(container="mkv")
            )

    monkeypatch.setattr(
        "smartcut.desktop.main_window.RepairDialog", FakeRepairDialog
    )
    monkeypatch.setattr(
        "smartcut.desktop.main_window.ExportDialog", FakeExportDialog
    )
    window = MainWindow(controller, settings, store, tmp_path / "data")
    try:
        window.current_project = {"id": "p_demo", "workspace_dir": str(tmp_path)}
        window.media_tools_panel.set_project_enabled(True)
        assert any(
            window.tabs.tabText(index) == "包装"
            for index in range(window.tabs.count())
        )
        window.media_tools_panel.import_button.click()
        assert calls[-1] == ("import", "C:/tmp/broken.mp4")
        window._on_media_source_imported(
            {"src": "media/broken.mp4", "name": "broken.mp4"}
        )
        assert (
            window.media_tools_panel.selected_source()
            == "media/broken.mp4"
        )

        previews: list[tuple[str, str]] = []
        monkeypatch.setattr(
            window.preview_panel,
            "load_media",
            lambda path, mode="": previews.append((str(path), mode)),
        )
        source = tmp_path / "source.mp4"
        source.write_bytes(b"x")
        window.current_project = {
            "id": "p_demo",
            "workspace_dir": str(tmp_path),
            "asset": {"src": "source.mp4"},
        }
        window._showing_output = False
        window._load_source_preview(window.current_project)
        assert previews and "源文件预览" in previews[-1][1]

        loaded: list[int] = []
        monkeypatch.setattr(
            controller, "load_revision", lambda number: loaded.append(number)
        )
        window.current_ir = {"revision": 1}
        window.unsaved = False
        window.inspector_panel.set_revisions(
            [{"revision": 0}, {"revision": 1}]
        )
        window.inspector_panel.select_revision(1)
        window.inspector_panel.revision_combo.setCurrentIndex(
            window.inspector_panel.revision_combo.findData(0)
        )
        assert loaded == [0]

        calls.clear()
        window.media_tools_panel.diagnose_button.click()
        window.media_tools_panel.set_project_enabled(True)
        window.media_tools_panel.repair_button.click()
        window.media_tools_panel.set_project_enabled(True)
        window.media_tools_panel.convert_button.click()
        assert [call[0] for call in calls] == [
            "diagnose",
            "repair",
            "transcode",
        ]

        window._on_ai_media_goal("修复音画不同步")
        assert calls[-1] == (
            "interpret",
            "修复音画不同步",
            "media/broken.mp4",
        )

        calls.clear()
        window._on_media_plan_ready({"operation": "convert"})
        assert [call[0] for call in calls] == ["transcode"]

        window.current_ir = {"revision": 1}
        window.timeline_panel.set_position(0.0)
        window._on_add_pip(
            {
                "src": "media/source.mp4",
                "name": "source",
                "facts": {
                    "duration": {"value": 90_000, "rate": 90_000}
                },
            }
        )
        pip_segments = [
            segment
            for segment in window.timeline_panel.segments()
            if int(segment.get("track") or 0) > 0
        ]
        assert len(pip_segments) == 1
        assert pip_segments[0]["src"] == "media/source.mp4"

        calls.clear()
        window.ai_panel.set_project_enabled(True)
        window.ai_panel.goal_edit.setPlainText("总结视频内容")
        window.ai_panel.understand_only.setChecked(True)
        window.ai_panel.run_button.click()
        assert calls == [("understand", "总结视频内容")]
        assert window.ai_panel.run_button.text() == "理解并回答"
        window.ai_panel.set_understanding(
            {"answer": "规则回答", "llm_used": False}
        )
        assert "规则回答" in window.ai_panel.answer.toPlainText()
    finally:
        window.close()
        app.processEvents()
        controller.shutdown()
