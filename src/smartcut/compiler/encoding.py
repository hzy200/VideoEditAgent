"""编码器参数映射：纯函数，禁止在这里启动进程。

实现已下沉到 ``contracts.encoder``，供 runner/compiler 共享；这里保留
导出以兼容既有调用点。
"""

from __future__ import annotations

from smartcut.contracts.encoder import EncoderProfile, video_encoder_args

__all__ = ["EncoderProfile", "video_encoder_args"]
