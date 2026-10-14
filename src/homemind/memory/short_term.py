"""短期记忆（对齐 FR-MEM-001）。

存储策略：
- 首选 Redis（分布式、TTL 自动过期、多实例共享）
- Redis 不可用时**自动降级为进程内内存**，保证单机开发不装 Redis 也能跑

滑动窗口 + TTL 双保险：
- 窗口限制单会话保留的最大轮数（默认 20 轮）
- TTL 保证冷会话自动清理（默认 24 小时）
"""

from __future__ import annotations

import time
from typing import Any

from ..config import get_settings
from ..core.trace import get_logger

logger = get_logger("memory.short_term")


class ShortTermMemory:
    """短期记忆：会话上下文的存储与读取。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._redis: Any = None
        self._fallback: dict[str, list[dict[str, Any]]] = {}
        self._mode = "memory"
        self._init_redis()

    def _init_redis(self) -> None:
        """尝试连接 Redis，失败则静默降级为内存模式。"""
        if not self._settings.redis.enabled:
            logger.info("redis disabled by config, using in-memory mode")
            return
        try:
            import redis.asyncio as aioredis  # 局部导入：没装 redis 包也能跑

            self._redis = aioredis.Redis(
                host=self._settings.redis.host,
                port=self._settings.redis.port,
                db=self._settings.redis.db,
                password=self._settings.redis.password or None,
                decode_responses=True,
                socket_connect_timeout=1.0,
            )
            self._mode = "redis"
            logger.info("short term memory using redis: %s:%s",
                        self._settings.redis.host, self._settings.redis.port)
        except ImportError:
            logger.warning("redis package not installed, fallback to in-memory mode")
        except Exception as exc:  # noqa: BLE001
            logger.warning("redis init failed (%s), fallback to in-memory mode", exc)

    @property
    def mode(self) -> str:
        """当前存储模式：redis / memory。

        注意：这只是"配置意图"，不代表 Redis 真的可用
        （初始化时只创建客户端，不建连）。要确认真实状态请用 health_check()。
        """
        return self._mode

    async def health_check(self) -> str:
        """实际探测一次 Redis 连通性，返回真实可用的存储模式。

        健康检查不能撒谎——只返回"我打算用 redis"而不验证，
        等线上真出问题时才发现早就降级了，那就晚了。
        """
        if self._redis is not None:
            try:
                await self._redis.ping()
                return "redis"
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis health check failed: %s", type(exc).__name__)
                self._redis = None
                self._mode = "memory"
                return f"memory(redis_unavailable:{type(exc).__name__})"
        return "memory"

    async def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息到会话上下文。"""
        record = {"role": role, "content": content, "ts": time.time()}
        if self._redis is not None:
            try:
                key = f"hm:session:{session_id}"
                await self._redis.rpush(key, self._encode(record))
                await self._redis.expire(key, self._settings.redis.ttl_seconds)
                await self._trim(session_id)
                return
            except Exception as exc:  # noqa: BLE001 - Redis 挂了不能影响主流程
                logger.warning("redis append failed (%s), fallback to memory", exc)
                self._redis = None
                self._mode = "memory"
        self._fallback.setdefault(session_id, []).append(record)
        # 内存模式也要裁剪，防止无限增长
        window = self._settings.memory.short_term_window * 2
        if len(self._fallback[session_id]) > window:
            self._fallback[session_id] = self._fallback[session_id][-window:]

    async def get(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """读取会话上下文，返回最近 limit 条（滑动窗口）。"""
        limit = limit or self._settings.memory.short_term_window * 2
        if self._redis is not None:
            try:
                raw_list = await self._redis.lrange(f"hm:session:{session_id}", -limit, -1)
                return [self._decode(raw) for raw in raw_list]
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis get failed (%s), fallback to memory", exc)
                self._redis = None
                self._mode = "memory"
        return self._fallback.get(session_id, [])[-limit:]

    async def clear(self, session_id: str) -> None:
        """清空会话上下文。"""
        if self._redis is not None:
            try:
                await self._redis.delete(f"hm:session:{session_id}")
            except Exception:  # noqa: BLE001
                pass
        self._fallback.pop(session_id, None)

    async def _trim(self, session_id: str) -> None:
        """裁剪超窗口的历史消息，只保留最近 N 条。"""
        if self._redis is None:
            return
        window = self._settings.memory.short_term_window * 2
        try:
            await self._redis.ltrim(f"hm:session:{session_id}", -window, -1)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _encode(record: dict[str, Any]) -> str:
        import json
        return json.dumps(record, ensure_ascii=False)

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        import json
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"role": "unknown", "content": raw, "ts": 0}


_memory: ShortTermMemory | None = None


def get_short_term_memory() -> ShortTermMemory:
    """获取短期记忆单例。"""
    global _memory
    if _memory is None:
        _memory = ShortTermMemory()
    return _memory
