"""HomeMind 全局配置。

设计原则：
1. 全部配置从环境变量读取，改配置不改代码（对齐 NFR-MAIN-002）。
2. 所有外部依赖都可降级：Redis 挂了走内存、大模型挂了走规则解析。
   这样保证任何一台机器 clone 下来就能跑，不依赖云服务和数据库。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv_once() -> None:
    """加载项目根目录下的 .env（存在才加载）。

    这样配 API Key 只改 .env 就行，不用改代码、不用设系统环境变量。
    python-dotenv 没装时静默跳过——它是可选依赖，缺了不影响跑。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # 从项目根目录往上找两级，兼容从 scripts/ 子目录启动
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        candidate = os.path.join(here, ".env")
        if os.path.isfile(candidate):
            load_dotenv(candidate, override=False)
            return
        here = os.path.dirname(here)


_load_dotenv_once()


def _get_bool(key: str, default: bool) -> bool:
    """读取布尔型环境变量，兼容 true/1/yes/on。"""
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class LLMConfig:
    """大模型配置。USE_MOCK_LLM=True 时不调任何 API，走本地规则解析。"""

    use_mock: bool = field(default_factory=lambda: _get_bool("HM_USE_MOCK_LLM", True))
    provider: str = field(default_factory=lambda: os.getenv("HM_LLM_PROVIDER", "deepseek"))
    model: str = field(default_factory=lambda: os.getenv("HM_LLM_MODEL", "deepseek-chat"))
    api_key: str = field(default_factory=lambda: os.getenv("HM_LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("HM_LLM_BASE_URL", ""))
    timeout: int = field(default_factory=lambda: _get_int("HM_LLM_TIMEOUT", 30))
    temperature: float = field(default_factory=lambda: _get_float("HM_LLM_TEMPERATURE", 0.1))


@dataclass
class RedisConfig:
    """Redis 配置，用于短期记忆。连不上时自动降级为内存存储。"""

    host: str = field(default_factory=lambda: os.getenv("HM_REDIS_HOST", "localhost"))
    port: int = field(default_factory=lambda: _get_int("HM_REDIS_PORT", 6379))
    db: int = field(default_factory=lambda: _get_int("HM_REDIS_DB", 0))
    password: str = field(default_factory=lambda: os.getenv("HM_REDIS_PASSWORD", ""))
    ttl_seconds: int = field(default_factory=lambda: _get_int("HM_REDIS_TTL", 86400))
    enabled: bool = field(default_factory=lambda: _get_bool("HM_REDIS_ENABLED", True))


@dataclass
class EmbeddingConfig:
    """本地 embedding 配置（语义召回的真向量模式）。

    默认开启：模型走懒加载，第一次真要算向量时才加载；没装 fastembed 或模型下载
    失败都会自动降级为字符 bigram Jaccard，**不会让服务起不来**。

    关掉它的场景：不想要那 90MB 模型、或机器磁盘紧张。
    关掉后语义召回退回字面匹配，功能可用但认不出同义改写。
    """

    enabled: bool = field(default_factory=lambda: _get_bool("HM_EMBEDDING_ENABLED", True))
    model: str = field(
        default_factory=lambda: os.getenv("HM_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    )


@dataclass
class ToolConfig:
    """工具调用治理配置：超时、重试、熔断（对齐 FR-TOOL-003）。"""

    timeout_seconds: float = field(default_factory=lambda: _get_float("HM_TOOL_TIMEOUT", 5.0))
    max_retries: int = field(default_factory=lambda: _get_int("HM_TOOL_MAX_RETRIES", 2))
    circuit_breaker_threshold: int = field(
        default_factory=lambda: _get_int("HM_TOOL_CB_THRESHOLD", 3)
    )


@dataclass
class MemoryConfig:
    """记忆相关配置（对齐 FR-MEM-005 召回公式权重）。"""

    short_term_window: int = field(default_factory=lambda: _get_int("HM_MEM_WINDOW", 20))
    alpha_semantic: float = field(default_factory=lambda: _get_float("HM_MEM_ALPHA", 0.6))
    beta_time_decay: float = field(default_factory=lambda: _get_float("HM_MEM_BETA", 0.2))
    gamma_importance: float = field(default_factory=lambda: _get_float("HM_MEM_GAMMA", 0.2))
    decay_lambda: float = field(default_factory=lambda: _get_float("HM_MEM_LAMBDA", 0.01))


@dataclass
class SafetyConfig:
    """安全护栏配置（对齐 FR-SEC-001 危险动作二次确认）。"""

    require_confirm: bool = field(default_factory=lambda: _get_bool("HM_REQUIRE_CONFIRM", True))
    high_risk_devices: tuple[str, ...] = ("门锁", "燃气阀", "门禁")
    confidence_threshold: float = field(
        default_factory=lambda: _get_float("HM_CONFIDENCE_THRESHOLD", 0.7)
    )


@dataclass
class Settings:
    """聚合全部配置，全局单例通过 get_settings() 获取。"""

    env: str = field(default_factory=lambda: os.getenv("HM_ENV", "development"))
    log_level: str = field(default_factory=lambda: os.getenv("HM_LOG_LEVEL", "INFO"))
    llm: LLMConfig = field(default_factory=LLMConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    tool: ToolConfig = field(default_factory=ToolConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    """获取全局配置单例。"""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def reload_settings() -> Settings:
    """重新加载配置（测试用）。"""
    global _SETTINGS
    _SETTINGS = Settings()
    return _SETTINGS
