"""安全 ID 的唯一校验事实源。

所有会进入路径、数据库主键或任务图节点的字符串标识必须使用 ``SafeId``，
禁止在各契约中复制正则或校验函数。
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

SAFE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
SAFE_ID_RE = re.compile(SAFE_ID_PATTERN)


def is_safe_id(value: str) -> bool:
    return bool(SAFE_ID_RE.fullmatch(value))


def validate_safe_id(value: str) -> str:
    if not is_safe_id(value):
        raise ValueError("只允许字母、数字、下划线、连字符（1-64 字符）")
    return value


SafeId = Annotated[str, AfterValidator(validate_safe_id)]
