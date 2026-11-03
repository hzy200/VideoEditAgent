"""小型文件原子写：同目录临时文件 + os.replace。"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


def atomic_write_text(
    path: Path,
    value: str,
    *,
    encoding: str = "utf-8",
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.part-{uuid4().hex[:8]}")
    try:
        temp.write_text(value, encoding=encoding)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
