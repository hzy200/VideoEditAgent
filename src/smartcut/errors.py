"""领域异常。server 层负责把它们映射为 HTTP 状态码。"""

from __future__ import annotations


class SmartCutError(Exception):
    """所有领域异常的基类。"""


class ResourceNotFound(SmartCutError):
    """项目、素材、版本或任务不存在。"""


class IRValidationError(SmartCutError):
    """EditIR 语义不合法（越界、零长、跨素材等）。"""


class UnsupportedFeature(SmartCutError):
    """当前 P0 阶段明确不支持的能力。"""


class SandboxError(SmartCutError):
    """路径越出 workspace、试图覆盖输入等安全问题。"""


class ToolExecutionError(SmartCutError):
    """子进程失败、超时或退出码非零。"""


class ResourceTimeout(SmartCutError):
    """等待 ffmpeg/CPU/LLM 配额超时。"""


class RunCancelledError(SmartCutError):
    """任务被用户取消。"""


class VerificationError(SmartCutError):
    """成片 ffprobe 事实与期望不符。"""


class PerceptionUnavailable(SmartCutError):
    """OpenCV/PySceneDetect 等可选感知依赖不可用。"""


class LLMResponseError(SmartCutError):
    """LLM 网络错误、响应结构异常或多次修复后仍不合格。"""


class PlannerUnavailable(SmartCutError):
    """未配置 API Key 或缺少 planner 依赖。"""


class PlannerResponseError(LLMResponseError):
    """模型多次重试后仍无法给出合法 EditIR。"""
