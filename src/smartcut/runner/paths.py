"""Workspace 沙箱入口。

实现位于 ``smartcut.contracts.workspace``；这里保留 runner 侧导入路径，
避免既有调用点与打包配置漂移。
"""

from __future__ import annotations

from smartcut.contracts.workspace import Workspace

__all__ = ["Workspace"]
