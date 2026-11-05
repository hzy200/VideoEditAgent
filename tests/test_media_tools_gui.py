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
    finally:
        window.close()
        app.processEvents()
        controller.shutdown()
