"""集中配置。所有路径、二进制、模型参数都从这里注入，禁止散落魔法值。"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARTCUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # 工作区
    workspace_root: Path = Path("./workspace")
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    tool_timeout_seconds: float = 120.0
    mix_timeout_seconds: float = 600.0
    render_timeout_seconds: float = 900.0
    max_workers: int = 2
    # 0=不限制；默认 64 个活动/排队 Job，满则快速失败而不是无限堆积。
    max_pending_jobs: int = 64
    max_upload_mb: int = 4096

    # 资源并发上限（跨 Job/Agent 共享）
    ffmpeg_concurrency: int = 2
    cpu_concurrency: int = 2
    llm_concurrency: int = 4
    # 资源池等待上限；0=自动（至少覆盖最长工具超时，默认 1800s）
    resource_wait_timeout_seconds: float = 0.0

    # 长视频处理
    transcribe_chunk_seconds: float = 300.0
    transcribe_chunk_overlap_seconds: float = 1.0
    # 0=auto：CPU + clone 能力时最多 2 个 worker；GPU/未知默认 1
    transcribe_workers: int = 0
    transcribe_silence_rms: float = 0.002
    transcribe_silence_min_seconds: float = 0.5
    # whisper 隔离进程的单次请求超时（含模型加载）；超时直接杀进程。
    whisper_worker_timeout_seconds: float = 1800.0
    # worker 崩溃或超时后的受控重试次数；每次重试都会重启独立进程。
    whisper_worker_retries: int = 1
    perception_max_frames: int = 1200
    perception_min_frame_gap_seconds: float = 0.5
    # auto/on/off：长任务用 spawn 进程池做 OpenCV 逐帧分析
    perception_process_pool: str = "auto"
    perception_process_pool_min_frames: int = 240
    # 进程池整批 watchdog；超时 terminate 整个池，避免单帧卡死拖住任务。
    perception_process_pool_timeout_seconds: float = 600.0
    frame_thumb_width: int = 320
    frame_thumb_height: int = 180
    waveform_buckets: int = 2000
    chapter_target_seconds: float = 300.0

    # 渲染管线输入规范化：HDR tonemap 默认关闭，仅提示风险。
    render_hdr_tonemap: bool = False
    # 单音轨渲染的 loudnorm 两遍测量；多轨混音保持单遍。
    render_loudnorm_two_pass: bool = True

    # 字幕样式（预览与烧录共用）
    caption_font_name: str = "Microsoft YaHei"
    caption_font_size: int = 22
    caption_color: str = "#FFFFFF"
    caption_position: str = "bottom"
    # 可选字体目录（工作区相对路径），用于 ASS/烧录字幕。
    caption_fonts_dir: str | None = None

    # 候选片段与检索
    candidate_target_seconds: float = 30.0
    candidate_max: int = 300
    candidate_retrieval_limit: int = 60
    # 旧缓存缺 visual_hash 时，首次“找相似”自动重建一次候选（每素材一次）。
    candidate_visual_backfill: bool = True

    # Critic 局部重规划
    critic_max_segment_seconds: float = 90.0
    critic_quality_padding: float = 0.5

    # QA 之后的反思式重规划：分数低于阈值且存在 warning/error 时触发。
    replan_enabled: bool = True
    replan_score_threshold: float = Field(default=0.55, ge=0.0, le=1.0)

    # 偏好记忆作用域：source=仅当前素材（默认，更严格）；project=跨素材复用。
    preference_scope: str = "source"

    # 音频局部修复（去口癖）
    audio_filler_words: str = "um,uh,erm,嗯,呃,那个"
    audio_filler_padding: float = 0.05
    audio_min_segment_seconds: float = 0.4

    # 可选人声分离（不打包进主程序；配置外部 Demucs 命令或模块）
    demucs_command: str | None = None
    demucs_model: str = "htdemucs"
    demucs_timeout_seconds: float = 1800.0

    @property
    def audio_filler_set(self) -> set[str]:
        return {
            item.strip()
            for item in self.audio_filler_words.split(",")
            if item.strip()
        }

    # Orchestrator（Magnetic-One 风格）
    orchestrator_max_parallel: int = 4
    orchestrator_max_rounds: int = 64
    # 任务图默认走 workflow.json 规则图；仅显式开启才让 LLM 生成 DAG。
    orchestrator_use_llm: bool = False
    llm_decision_summary: bool = False

    # 编码与代理优先
    hardware_encoding: str = "auto"  # auto / off
    encoder_priority: str = "h264_nvenc,h264_qsv,h264_amf,libx264"
    proxy_height: int = 720
    # GPU 解码：auto/off；失败自动回退 CPU
    hardware_decoding: str = "auto"
    decode_hwaccel: str = "cuda"

    # 多模态视觉摘要（OpenAI 兼容 VLM；未配置则禁用）
    # remote=OpenAI 兼容；local=本地插件模块（可选 ONNX 后端）
    vision_provider: str = "remote"
    local_vision_module: str | None = None
    local_vision_model_path: str | None = None
    local_vision_backend: str = "module"  # module / onnx
    vision_base_url: str | None = None
    vision_model: str | None = None
    vision_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("VISION_API_KEY", "SMARTCUT_VISION_API_KEY"),
    )
    vision_timeout_seconds: float = 90.0
    vision_json_mode: bool = False
    vision_max_images: int = 7
    vision_image_max_edge: int = 1024
    vision_image_max_bytes: int = 4 * 1024 * 1024
    vision_total_max_bytes: int = 12 * 1024 * 1024
    vision_max_keyframes_per_chapter: int = 2
    vision_prompt_version: str = "v1"
    # 文本优先：章节摘要足够长时不再发送关键帧，只用 contact sheet/文本规划。
    vision_text_first: bool = True
    vision_text_min_chars: int = 40
    # 同章节多关键帧拼成 2x2 tile，减少图片数量与 token。
    vision_chapter_tiles: bool = True
    # 摘要为空或 cautions 达到阈值时补一次复核请求。
    vision_second_pass_cautions: int = 2

    # DeepSeek（OpenAI 兼容）
    deepseek_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "SMARTCUT_DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_BASE_URL", "SMARTCUT_DEEPSEEK_BASE_URL"),
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        validation_alias=AliasChoices("DEEPSEEK_MODEL", "SMARTCUT_DEEPSEEK_MODEL"),
    )
    deepseek_timeout_seconds: float = Field(
        default=120.0,
        validation_alias=AliasChoices("DEEPSEEK_TIMEOUT_SECONDS", "SMARTCUT_DEEPSEEK_TIMEOUT_SECONDS"),
    )
    deepseek_max_retries: int = Field(default=2, ge=0, le=8)
    deepseek_retry_base_seconds: float = Field(default=0.5, ge=0.0, le=30.0)
    # 0=不限制；按进程累计 token 计数，达到后拒绝新请求。
    llm_token_budget: int = Field(default=0, ge=0)
    # 单次 orchestration run 的 token 预算；0=不限制。
    llm_token_budget_per_run: int = Field(default=0, ge=0)
    # Planner 上下文预算：按估算 token 裁剪转写与候选，避免字符截断失真。
    planner_context_max_tokens: int = Field(default=24_000, ge=2_000)

    # 转写（optional extra）
    whisper_model: str = "base"
    whisper_model_path: str | None = None
    # auto：首次在后台转写线程中检测 CUDA，不在桌面主进程 import torch
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_gpu_compute_type: str = "float16"

    @field_validator("ffmpeg_bin", "ffprobe_bin")
    @classmethod
    def _non_empty_binary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("二进制路径不能为空")
        return value

    @property
    def encoder_priority_list(self) -> list[str]:
        values = [item.strip() for item in self.encoder_priority.split(",")]
        return [item for item in values if item]

    @property
    def vision_configured(self) -> bool:
        return bool(self.vision_base_url and self.vision_model)

    @property
    def effective_vision_key(self) -> str | None:
        if self.vision_api_key:
            return self.vision_api_key
        if (
            self.vision_base_url
            and self.deepseek_base_url
            and self.vision_base_url.rstrip("/") == self.deepseek_base_url.rstrip("/")
        ):
            return self.deepseek_api_key
        return None

    @property
    def projects_dir(self) -> Path:
        """每个任务/项目独立工作区的父目录。"""
        return self.workspace_root / "projects"

    @property
    def media_dir(self) -> Path:
        return self.workspace_root / "media"

    @property
    def proxy_dir(self) -> Path:
        return self.workspace_root / "proxy"

    @property
    def frames_dir(self) -> Path:
        return self.workspace_root / "frames"

    @property
    def renders_dir(self) -> Path:
        return self.workspace_root / "renders"

    @property
    def tmp_dir(self) -> Path:
        return self.workspace_root / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.workspace_root / "logs"

    @property
    def db_path(self) -> Path:
        return self.workspace_root / "smartcut.db"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "smartcut.jsonl"

    def ensure_dirs(self) -> None:
        for path in (
            self.workspace_root,
            self.projects_dir,
            self.media_dir,
            self.proxy_dir,
            self.frames_dir,
            self.renders_dir,
            self.tmp_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    """构造配置；测试通过关键字覆盖 workspace 与二进制路径。"""
    return Settings(**overrides)
