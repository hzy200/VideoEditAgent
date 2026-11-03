"""DeepSeek（OpenAI 兼容）客户端。

统一承担：
- HTTP 调用与错误映射
- JSON Output 解析（容忍 Markdown 代码块）
- Pydantic 校验 + 有限修复重试
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from smartcut.config import Settings
from smartcut.errors import LLMResponseError
from smartcut.llm.singleflight import SingleFlight
from smartcut.logging import get_logger
from smartcut.resources import ResourceManager

T = TypeVar("T", bound=BaseModel)
Message = dict[str, Any]


class TextCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, value: str) -> None: ...


def extract_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("响应中找不到 JSON 对象") from None
        payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型输出的顶层必须是 JSON 对象")
    return payload


class DeepSeekClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        logger: Any | None = None,
        cache: TextCache | None = None,
        resources: ResourceManager | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or get_logger("smartcut.llm")
        self.cache = cache
        self.resources = resources or ResourceManager(settings)
        self._usage_lock = threading.Lock()
        self._flights = SingleFlight()
        self._usage: dict[str, int] = self._empty_usage()
        self._scoped_usage: dict[str, dict[str, int]] = {}
        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                timeout=settings.deepseek_timeout_seconds,
                transport=transport,
            )

    @property
    def available(self) -> bool:
        # 动态读取：桌面设置对话框修改 Key 后无需重建 Services。
        return bool(self.settings.deepseek_api_key)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "requests": 0,
            "cache_hits": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def usage_snapshot(self) -> dict[str, int]:
        with self._usage_lock:
            return dict(self._usage)

    def usage_for(self, scope: str | None) -> dict[str, int]:
        if not scope:
            return self.usage_snapshot()
        with self._usage_lock:
            return dict(self._scoped_usage.get(scope) or self._empty_usage())

    def _record_usage(
        self,
        usage: dict[str, Any] | None,
        *,
        cache_hit: bool,
        scope: str | None = None,
    ) -> None:
        with self._usage_lock:
            targets = [self._usage]
            if scope:
                scoped = self._scoped_usage.setdefault(scope, self._empty_usage())
                targets.append(scoped)
            for target in targets:
                if cache_hit:
                    target["cache_hits"] += 1
                    continue
                target["requests"] += 1
                if not isinstance(usage, dict):
                    continue
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    try:
                        target[key] += int(usage.get(key) or 0)
                    except (TypeError, ValueError):
                        continue

    def chat_json(
        self,
        messages: list[Message],
        model: type[T],
        *,
        validate: Callable[[T], None] | None = None,
        max_repairs: int = 2,
        temperature: float = 0.2,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> T:
        """要求模型输出 JSON，并用 Pydantic 校验；失败时带错误提示重试。"""
        conversation = list(messages)
        last_error: str | None = None

        for attempt in range(max_repairs + 1):
            content = self._chat(
                conversation,
                temperature=temperature,
                json_mode=True,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
            try:
                payload = extract_json_object(content)
                parsed = model.model_validate(payload)
                if validate is not None:
                    validate(parsed)
                return parsed
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                self.logger.warning(
                    "llm_json_repair",
                    attempt=attempt + 1,
                    max_repairs=max_repairs,
                    error=last_error[:500],
                )
                if attempt >= max_repairs:
                    break
                conversation = [
                    *conversation,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "上面的 JSON 未通过校验，错误如下：\n"
                            f"{last_error}\n"
                            "请只输出修正后的 JSON 对象，严格遵守系统消息中的 Schema 与规则。"
                        ),
                    },
                ]

        raise LLMResponseError(
            f"模型连续 {max_repairs + 1} 次未给出合法 JSON: {last_error}"
        )

    def chat_text(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.2,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        return self._chat(
            messages,
            temperature=temperature,
            json_mode=False,
            cancel_token=cancel_token,
            usage_scope=usage_scope,
            token_budget=token_budget,
        )

    def _chat(
        self,
        messages: list[Message],
        *,
        temperature: float,
        json_mode: bool,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        api_key = self.settings.deepseek_api_key
        if not api_key:
            raise LLMResponseError("未配置 DEEPSEEK_API_KEY")

        url = f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        cache_payload = {
            "url": url,
            "model": self.settings.deepseek_model,
            "messages": messages,
            "temperature": temperature,
            "json_mode": json_mode,
        }
        cache_key = (
            hashlib.sha256(
                json.dumps(
                    cache_payload,
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            if self.cache is not None
            else None
        )
        # single-flight 按 run 隔离，避免不同 run 共享同一请求却不计入各自 usage。
        flight_key = hashlib.sha256(
            json.dumps(
                {**cache_payload, "usage_scope": usage_scope},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if cache_key is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.logger.info("llm_cache_hit", key=cache_key)
                self._record_usage(
                    None,
                    cache_hit=True,
                    scope=usage_scope,
                )
                return cached

        def fetch() -> str:
            content = self._request_with_retries(
                url,
                headers,
                body,
                cancel_token=cancel_token,
                usage_scope=usage_scope,
                token_budget=token_budget,
            )
            if self.cache is not None and cache_key is not None:
                self.cache.put(cache_key, content)
            return content

        # 同一 prompt/model/run 的并发请求只发出一次；等待方可在取消时退出。
        return self._flights.run(
            flight_key,
            fetch,
            cancel_token=cancel_token,
        )

    def _budget_exhausted(
        self,
        scope: str | None = None,
        budget: int | None = None,
    ) -> bool:
        global_budget = int(self.settings.llm_token_budget or 0)
        scoped_budget = (
            int(budget)
            if budget is not None
            else int(self.settings.llm_token_budget_per_run or 0)
        )
        with self._usage_lock:
            if global_budget > 0 and self._usage["total_tokens"] >= global_budget:
                return True
            if scope and scoped_budget > 0:
                used = (self._scoped_usage.get(scope) or {}).get(
                    "total_tokens", 0
                )
                return used >= scoped_budget
        return False

    def _request_with_retries(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        *,
        cancel_token: Any | None = None,
        usage_scope: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        attempts = max(0, int(self.settings.deepseek_max_retries))
        retryable = {408, 409, 429, 500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            if self._budget_exhausted(usage_scope, token_budget):
                raise LLMResponseError(
                    "LLM token 预算已用尽"
                    f"（global={self.settings.llm_token_budget}, "
                    f"run={token_budget if token_budget is not None else self.settings.llm_token_budget_per_run}）"
                )
            try:
                with self.resources.llm_slot(cancel_token=cancel_token):
                    response = self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= attempts:
                    raise LLMResponseError(f"DeepSeek 请求失败: {exc}") from exc
                self._sleep_before_retry(attempt, cancel_token)
                continue

            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            if response.status_code in retryable and attempt < attempts:
                delay = self._retry_delay(response, attempt)
                self.logger.warning(
                    "llm_retry",
                    attempt=attempt + 1,
                    status=response.status_code,
                    delay=delay,
                )
                self._cancelable_sleep(delay, cancel_token)
                continue

            if response.status_code >= 400:
                raise LLMResponseError(
                    f"DeepSeek HTTP {response.status_code}: {response.text[:1000]}"
                )

            try:
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMResponseError(
                    f"DeepSeek 响应结构异常: {response.text[:1000]}"
                ) from exc

            if not isinstance(content, str) or not content.strip():
                raise LLMResponseError("DeepSeek 返回空 content")
            self._record_usage(
                payload.get("usage"),
                cache_hit=False,
                scope=usage_scope,
            )
            return content
        raise LLMResponseError(f"DeepSeek 请求失败: {last_error}")

    @staticmethod
    def _cancelable_sleep(seconds: float, cancel_token: Any | None) -> None:
        """退避等待期间按 100ms 轮询取消，避免最长等满 30s。"""
        if seconds <= 0:
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            return
        if cancel_token is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while True:
            cancel_token.raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _sleep_before_retry(
        self,
        attempt: int,
        cancel_token: Any | None = None,
    ) -> None:
        base = float(self.settings.deepseek_retry_base_seconds)
        if base > 0:
            self._cancelable_sleep(
                min(30.0, base * (2**attempt)),
                cancel_token,
            )

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(60.0, float(retry_after)))
            except ValueError:
                pass
        base = float(self.settings.deepseek_retry_base_seconds)
        return min(30.0, base * (2**attempt))
