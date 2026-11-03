"""OpenAI 兼容视觉模型客户端。

复用 DeepSeekClient 的 HTTP/缓存能力，但使用独立的 base_url/model/key；
未配置时完全禁用，不影响纯文本规划。
"""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from smartcut.config import Settings
from smartcut.errors import LLMResponseError
from smartcut.llm.client import (
    DeepSeekClient,
    Message,
    TextCache,
    extract_json_object,
)
from smartcut.resources import ResourceManager

T = TypeVar("T", bound=BaseModel)


class VisionClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        cache: TextCache | None = None,
        resources: ResourceManager | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._cache = cache
        self._resources = resources
        self._client: DeepSeekClient | None = None
        self._identity: tuple[str | None, str | None, str | None] | None = None
        if self.available:
            self._ensure_client()

    def _identity_value(self) -> tuple[str | None, str | None, str | None]:
        return (
            self.settings.vision_base_url,
            self.settings.vision_model,
            self.settings.effective_vision_key,
        )

    def _ensure_client(self) -> DeepSeekClient:
        identity = self._identity_value()
        if self._client is not None and self._identity == identity:
            return self._client
        if self._client is not None:
            self._client.close()
        self._identity = identity
        vision_settings = self.settings.model_copy(
            update={
                "deepseek_base_url": self.settings.vision_base_url,
                "deepseek_model": self.settings.vision_model,
                "deepseek_api_key": self.settings.effective_vision_key,
                "deepseek_timeout_seconds": self.settings.vision_timeout_seconds,
            }
        )
        self._client = DeepSeekClient(
            vision_settings,
            transport=self._transport,
            cache=self._cache,
            resources=self._resources,
        )
        return self._client

    @property
    def available(self) -> bool:
        return self.settings.vision_configured and bool(self.settings.effective_vision_key)

    @property
    def model(self) -> str | None:
        return self.settings.vision_model

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def usage_snapshot(self) -> dict[str, int]:
        if self._client is None:
            return {
                "requests": 0,
                "cache_hits": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        return self._client.usage_snapshot()

    def usage_for(self, scope: str | None) -> dict[str, int]:
        if self._client is None:
            return {
                "requests": 0,
                "cache_hits": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        return self._client.usage_for(scope)

    def analyze(
        self,
        messages: list[Message],
        schema: type[T],
        *,
        max_repairs: int = 1,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> T:
        if not self.available:
            raise LLMResponseError("未配置视觉模型")
        client = self._ensure_client()
        if self.settings.vision_json_mode:
            return client.chat_json(
                messages,
                schema,
                max_repairs=max_repairs,
                temperature=0.1,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )

        conversation = list(messages)
        last_error = ""
        for attempt in range(max_repairs + 1):
            content = client.chat_text(
                conversation,
                temperature=0.1,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
            try:
                payload = extract_json_object(content)
                return schema.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                if attempt >= max_repairs:
                    break
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "上面的 JSON 未通过校验，错误如下：\n"
                            f"{last_error}\n请只输出修正后的 JSON 对象。"
                        ),
                    },
                ]
        raise LLMResponseError(f"视觉模型未给出合法 JSON: {last_error}")


def build_vision_client(
    settings: Settings,
    *,
    cache: TextCache | None = None,
    resources: ResourceManager | None = None,
    transport: httpx.BaseTransport | None = None,
):
    """按 vision_provider 选择远程或本地视觉后端。"""
    if str(settings.vision_provider).lower() == "local":
        from smartcut.llm.local_vision import LocalVisionClient

        return LocalVisionClient(settings)
    return VisionClient(
        settings,
        transport=transport,
        cache=cache,
        resources=resources,
    )


__all__: list[Any] = ["VisionClient", "build_vision_client"]
