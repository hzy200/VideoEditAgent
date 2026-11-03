"""任务工作区管理。

约束：每个项目/任务拥有独立工作区；用户上传的素材必须复制进工作区，
后续所有 ffmpeg 操作只作用于工作区内的副本，绝不直接操作源文件。
"""

from __future__ import annotations

import threading
from pathlib import Path

from smartcut.contracts.ids import is_safe_id
from smartcut.contracts.workspace import Workspace
from smartcut.errors import SandboxError

_WORKSPACE_DIRS = (
    "media",
    "audio",
    "overlays",
    "proxy",
    "frames",
    "renders",
    "tmp",
    "logs",
)


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._deleted: set[str] = set()
        self._deleted_lock = threading.Lock()

    def _safe_component(self, value: str, label: str) -> str:
        if not is_safe_id(value):
            raise SandboxError(f"非法{label}: {value!r}")
        return value

    def mark_deleted(self, project_id: str) -> None:
        """删除后立墓碑，阻止迟到线程用 get/create 重建工作区。"""
        self._safe_component(project_id, "project_id")
        with self._deleted_lock:
            self._deleted.add(project_id)

    def _ensure_not_deleted(self, project_id: str) -> None:
        with self._deleted_lock:
            deleted = project_id in self._deleted
        if deleted:
            raise SandboxError(f"项目工作区已删除: {project_id!r}")

    def project_root(self, project_id: str) -> Path:
        self._safe_component(project_id, "project_id")
        root = (self.projects_root / project_id).resolve()
        if not root.is_relative_to(self.projects_root):
            raise SandboxError(f"项目工作区越界: {project_id!r}")
        return root

    def create(self, project_id: str) -> Workspace:
        self._ensure_not_deleted(project_id)
        root = self.project_root(project_id)
        for name in _WORKSPACE_DIRS:
            (root / name).mkdir(parents=True, exist_ok=True)
        return Workspace(root)

    def get(self, project_id: str) -> Workspace:
        return self.create(project_id)

    def run_tmp(self, project_id: str, run_id: str) -> Path:
        self._safe_component(run_id, "run_id")
        path = self.get(project_id).tmp_dir / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path
