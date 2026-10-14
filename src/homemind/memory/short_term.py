"""短期记忆（对齐 FR-MEM-001）。

存储策略：
- 首选 Redis（分布式、TTL 自动过期、多实例共享）
- Redis 不可用时**自动降级为进程内内存**，保证单机开发不装 Redis 也能跑

滑动窗口 + 摘要压缩 + TTL 三层（FR-MEM-001）：
- 滑动窗口：单会话只保留最近 N 条原文（默认 20 条）
- 摘要压缩：超过 2×N 时，把最旧的溢出部分压缩成一条摘要存起来，
  原文只留最近 N 条——既控住 token 成本，又不让"聊过什么"彻底丢失
- TTL：冷会话自动清理（默认 24 小时）

摘要生成优先用 LLM；模型不可用 / mock 模式下降级为"逐条拼接"，
**降级路径测试也能确定性覆盖**，不会因为模型状态导致测试波动。
"""

from __future__ import annotations

import time
from typing import Any

from ..config import get_settings
from ..core.llm import get_llm_client
from ..core.trace import get_logger

logger = get_logger("memory.short_term")


class ShortTermMemory:
    """短期记忆：会话上下文的存储与读取。"""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._redis: Any = None
        self._fallback: dict[str, list[dict[str, Any]]] = {}
        self._summary_fallback: dict[str, str] = {}
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
        """追加一条消息到会话上下文，并在超窗时触发摘要压缩。"""
        record = {"role": role, "content": content, "ts": time.time()}
        if self._redis is not None:
            try:
                key = f"hm:session:{session_id}"
                await self._redis.rpush(key, self._encode(record))
                await self._redis.expire(key, self._settings.redis.ttl_seconds)
                await self._compress_if_needed(session_id)
                return
            except Exception as exc:  # noqa: BLE001 - Redis 挂了不能影响主流程
                logger.warning("redis append failed (%s), fallback to memory", exc)
                self._redis = None
                self._mode = "memory"
        self._fallback.setdefault(session_id, []).append(record)
        await self._compress_if_needed(session_id)

    async def get(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """读取会话上下文，返回最近 limit 条（滑动窗口内的原文）。"""
        limit = limit or self._settings.memory.short_term_window
        if self._redis is not None:
            try:
                raw_list = await self._redis.lrange(f"hm:session:{session_id}", -limit, -1)
                return [self._decode(raw) for raw in raw_list]
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis get failed (%s), fallback to memory", exc)
                self._redis = None
                self._mode = "memory"
        return self._fallback.get(session_id, [])[-limit:]

    async def get_context(self, session_id: str) -> dict[str, Any]:
        """取"可注入 prompt 的完整上下文"：更早对话的摘要 + 最近原文。

        这就是"滑动窗口 + 摘要压缩"模式的读取口：
        - summary：窗口之外被压缩掉的部分，一段话概括
        - messages：窗口内的原文，逐条可引用
        """
        return {
            "summary": await self._load_summary(session_id),
            "messages": await self.get(session_id),
        }

    async def clear(self, session_id: str) -> None:
        """清空会话上下文（原文 + 摘要一起清，否则摘要会变成幽灵记忆）。"""
        if self._redis is not None:
            try:
                await self._redis.delete(f"hm:session:{session_id}",
                                         self._summary_key(session_id))
            except Exception:  # noqa: BLE001
                pass
        self._fallback.pop(session_id, None)
        self._summary_fallback.pop(session_id, None)

    # ---------------- 摘要压缩 ----------------

    def _summary_key(self, session_id: str) -> str:
        return f"hm:summary:{session_id}"

    async def _compress_if_needed(self, session_id: str) -> None:
        """超过 2×窗口时，把最旧的溢出部分压成摘要，原文只留最近 N 条。"""
        keep = self._settings.memory.short_term_window
        threshold = keep * 2
        messages = await self.get(session_id, limit=10_000)
        if len(messages) <= threshold:
            return

        overflow = messages[: len(messages) - keep]
        old_summary = await self._load_summary(session_id)
        new_summary = await self._summarize(old_summary, overflow)

        if self._redis is not None:
            try:
                await self._redis.set(
                    self._summary_key(session_id), new_summary,
                    ex=self._settings.redis.ttl_seconds,
                )
                await self._redis.ltrim(f"hm:session:{session_id}", -keep, -1)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis summary store failed (%s), fallback to memory", exc)
                self._redis = None
                self._mode = "memory"
        self._summary_fallback[session_id] = new_summary
        self._fallback[session_id] = messages[-keep:]

    async def _summarize(self, old_summary: str, overflow: list[dict[str, Any]]) -> str:
        """把溢出消息压成一段话。LLM 优先，失败降级为逐条拼接（确定性可测）。"""
        lines = "\n".join(
            f"{m.get('role', 'user')}: {str(m.get('content', ''))[:120]}"
            for m in overflow
        )
        client = get_llm_client()
        if client.enabled:
            try:
                prompt = (
                    "把下面这段人与智能家居助手的对话压缩成一段话（200 字内），"
                    "保留用户做过的操作、表达过的偏好和重要结论，不要评论：\n\n"
                    + lines
                )
                result = await client.chat(prompt, system="你是对话摘要器，只输出摘要本身。",
                                           max_tokens=200)
                if result:
                    return result
            except Exception as exc:  # noqa: BLE001 - 摘要失败绝不能影响对话主流程
                logger.warning("llm summarize failed (%s), fallback to plain join", exc)
        # 降级：逐条拼接。溢出量上限 = 窗口大小（默认 20 条），全拼也不会失控；
        # 截断只裁单条长度，不裁条数——"信息不丢"是硬契约
        fallback = (old_summary + " " if old_summary else "") + "此前对话要点：" + "；".join(
            f"{m.get('role', 'user')}说{str(m.get('content', ''))[:60]}"
            for m in overflow
        )
        return fallback.strip()

    async def _load_summary(self, session_id: str) -> str | None:
        if self._redis is not None:
            try:
                return await self._redis.get(self._summary_key(session_id)) or None
            except Exception:  # noqa: BLE001
                pass
        return self._summary_fallback.get(session_id)

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
