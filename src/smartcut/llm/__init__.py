"""LLM 基础设施：OpenAI 兼容客户端、JSON 校验与修复重试。"""

from smartcut.llm.client import DeepSeekClient, extract_json_object

__all__ = ["DeepSeekClient", "extract_json_object"]
