from __future__ import annotations

from PySide6.QtCore import QLockFile


def test_qlockfile_blocks_second_instance(tmp_path) -> None:
    lock_path = str(tmp_path / "smartcut.lock")
    first = QLockFile(lock_path)
    second = QLockFile(lock_path)
    try:
        assert first.tryLock(100) is True
        assert second.tryLock(10) is False
        first.unlock()
        assert second.tryLock(100) is True
    finally:
        second.unlock()
        first.unlock()
