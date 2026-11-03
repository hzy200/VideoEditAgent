"""可选本地视觉后端适配器。

模型与推理实现通过外部模块注入（例如 ONNX Runtime VLM 插件），
主程序不打包 torch/onnxruntime，也不在桌面主进程加载模型。
模块接口：

    def analyze(messages, schema, *, model_path=None) -> dict | BaseModel

未配置/模块不可用时 available=False，调用方自动降级为 text-only。
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from smartcut.config import Settings
from smartcut.errors import LLMResponseError

T = TypeVar("T", bound=BaseModel)


class LocalVisionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._module = None

    def _load_module(self):
        if self._module is None:
            name = self.settings.local_vision_module
            if not name:
                return None
            try:
                self._module = importlib.import_module(name)
            except Exception:
                return None
        return self._module

    @property
    def available(self) -> bool:
        if str(self.settings.vision_provider).lower() != "local":
            return False
        module = self._load_module()
        if module is None or not callable(getattr(module, "analyze", None)):
            return False
        if str(self.settings.local_vision_backend).lower() == "onnx":
            if importlib.util.find_spec("onnxruntime") is None:
                return False
            path = self.settings.local_vision_model_path
            if not path or not Path(path).expanduser().exists():
                return False
        return True

    @property
    def model(self) -> str | None:
        if not self.available:
            return None
        name = str(self.settings.local_vision_module or "local")
        backend = str(self.settings.local_vision_backend or "module")
        return f"local:{backend}:{name}"

    def close(self) -> None:
        close = getattr(self._module, "close", None) if self._module else None
        if callable(close):
            close()

    def usage_snapshot(self) -> dict[str, int]:
        return {
            "requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def usage_for(self, scope: str | None) -> dict[str, int]:
        return self.usage_snapshot()

    def analyze(
        self,
        messages: list[dict[str, Any]],
        schema: type[T],
        *,
        max_repairs: int = 1,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> T:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        module = self._load_module()
        if module is None or not self.available:
            raise LLMResponseError("本地视觉后端不可用")
        analyze = getattr(module, "analyze")
        path = self.settings.local_vision_model_path
        try:
            payload = analyze(messages, schema, model_path=path)
        except TypeError:
            payload = analyze(messages, schema)
        except Exception as exc:
            raise LLMResponseError(f"本地视觉推理失败: {exc}") from exc
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        try:
            if isinstance(payload, schema):
                return payload
            if isinstance(payload, BaseModel):
                payload = payload.model_dump(mode="json")
            return schema.model_validate(payload)
        except (ValidationError, ValueError) as exc:
            raise LLMResponseError(f"本地视觉输出不符合 Schema: {exc}") from exc
