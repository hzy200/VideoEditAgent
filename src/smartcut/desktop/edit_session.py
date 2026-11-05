"""桌面编辑会话：统一时间线/特效/音频/字幕/导出的撤销与未保存状态。

所有面板的编辑先写入草稿快照，再统一比较、撤销与保存；保存时才落成新 revision。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class EditSnapshot:
    sequence: list[dict] = field(default_factory=list)
    effects: list[dict] = field(default_factory=list)
    speeds: dict = field(default_factory=dict)
    transitions: list[dict] = field(default_factory=list)
    overlays: list[dict] = field(default_factory=list)
    removals: list[dict] = field(default_factory=list)
    audio: dict = field(default_factory=dict)
    track_states: dict = field(default_factory=dict)
    subtitles: list[dict] = field(default_factory=list)
    subtitles_dirty: bool = False
    export: dict = field(default_factory=dict)


class EditSession:
    """基于完整快照的撤销栈；快照只包含可编辑的 IR 字段。"""

    def __init__(self, max_history: int = 100) -> None:
        self._max_history = max(1, int(max_history))
        self._undo: list[EditSnapshot] = []
        self._redo: list[EditSnapshot] = []
        self._current: EditSnapshot | None = None
        self._saved: EditSnapshot | None = None

    def reset(self, snapshot: EditSnapshot | None) -> None:
        value = deepcopy(snapshot) if snapshot is not None else None
        self._undo.clear()
        self._redo.clear()
        self._current = value
        self._saved = deepcopy(value)

    def capture(self, snapshot: EditSnapshot) -> bool:
        """记录一次编辑；与当前草稿相同则返回 False。"""
        value = deepcopy(snapshot)
        if self._current is None:
            self.reset(value)
            return False
        if value == self._current:
            return False
        self._undo.append(self._current)
        if len(self._undo) > self._max_history:
            self._undo.pop(0)
        self._redo.clear()
        self._current = value
        return True

    def undo(self) -> EditSnapshot | None:
        if not self._undo:
            return None
        if self._current is not None:
            self._redo.append(self._current)
        self._current = self._undo.pop()
        return deepcopy(self._current)

    def redo(self) -> EditSnapshot | None:
        if not self._redo:
            return None
        if self._current is not None:
            self._undo.append(self._current)
        self._current = self._redo.pop()
        return deepcopy(self._current)

    def mark_saved(self) -> None:
        self._saved = deepcopy(self._current)

    @property
    def dirty(self) -> bool:
        return self._current != self._saved

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)
