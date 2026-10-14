"""工具注册中心（对齐 FR-TOOL-001 / FR-TOOL-003）。

这一层是整个系统"工具调用标准化"的地基，设计要点：

1. **Schema 驱动**：每个工具必须声明 JSON Schema，Schema 同时用于
   - MCP 工具的对外描述（list_tools）
   - OpenAI Function Calling 的模型侧签名（同一份 Schema 两处复用）
   - 参数合法性校验

2. **调用治理内建**：超时、重试、熔断、幂等、降级全在这层实现，
   业务 handler 只管干活，不用每个工具重复写重试逻辑。

3. **降级而非崩溃**：工具失败必须返回 ToolResult(ok=False)，
   由上层 Agent 决定降级话术，绝不让异常穿透到用户。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import get_settings
from ..core.models import ToolResult
from ..core.trace import get_logger, span

logger = get_logger("tools.registry")

# 工具处理函数签名：接收参数 dict，返回结果 dict
ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class ToolSpec:
    """工具规格声明。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: ToolHandler
    # 高危工具：执行前强制二次确认（对齐 FR-SEC-001）
    high_risk: bool = False
    # 写操作：需要幂等保护
    mutating: bool = False

    def to_function_schema(self) -> dict[str, Any]:
        """转成 OpenAI Function Calling 的工具签名。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_mcp_schema(self) -> dict[str, Any]:
        """转成 MCP 工具描述。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }


class ToolRegistry:
    """工具注册中心。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._settings = get_settings()
        # 熔断计数器：工具名 → 连续失败次数
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        # 幂等缓存：幂等键 → 上次结果
        self._idempotency: dict[str, dict[str, Any]] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册工具。同名覆盖（便于测试时替换实现）。"""
        self._tools[spec.name] = spec
        logger.info("tool registered: %s", spec.name,
                    extra={"extra_fields": {"event": "tool.register", "tool": spec.name}})

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_tools(self) -> list[dict[str, Any]]:
        """列出全部工具的 MCP 描述（对应 MCP 的 list_tools）。"""
        return [spec.to_mcp_schema() for spec in self._tools.values()]

    def list_function_schemas(self) -> list[dict[str, Any]]:
        """列出全部工具的 Function Calling 签名，喂给大模型。"""
        return [spec.to_function_schema() for spec in self._tools.values()]

    def is_high_risk(self, name: str) -> bool:
        spec = self._tools.get(name)
        return bool(spec and spec.high_risk)

    # ---------- 熔断 ----------

    def _is_circuit_open(self, name: str) -> bool:
        """判断熔断器是否打开。冷却 30s 后进入半开，允许一次试探。"""
        opened_at = self._opened_at.get(name)
        if opened_at is None:
            return False
        if time.monotonic() - opened_at > 30.0:
            self._opened_at.pop(name, None)
            self._failures[name] = 0
            return False
        return True

    def _record_failure(self, name: str) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        threshold = self._settings.tool.circuit_breaker_threshold
        if count >= threshold:
            self._opened_at[name] = time.monotonic()
            logger.warning("circuit opened: %s", name,
                           extra={"extra_fields": {"event": "tool.circuit_open", "tool": name}})

    def _record_success(self, name: str) -> None:
        self._failures[name] = 0
        self._opened_at.pop(name, None)

    # ---------- 调用 ----------

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> ToolResult:
        """调用工具，内建超时 / 重试 / 熔断 / 幂等治理。"""
        spec = self._tools.get(name)
        started = time.perf_counter()

        if spec is None:
            return ToolResult(
                tool_name=name, ok=False, error=f"unknown_tool: {name}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 熔断器打开：快速失败，不拖垮整条链路
        if self._is_circuit_open(name):
            return ToolResult(
                tool_name=name, ok=False, error="circuit_open", degraded=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 幂等：重复请求直接返回上次结果，防重复创建日程 / 重复触发设备
        if idempotency_key and spec.mutating:
            cache_key = f"{name}:{idempotency_key}"
            if cache_key in self._idempotency:
                logger.info("idempotent hit: %s", name,
                            extra={"extra_fields": {"event": "tool.idempotent", "tool": name}})
                return ToolResult(
                    tool_name=name, ok=True, data=dict(self._idempotency[cache_key]),
                    idempotent_hit=True,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )

        last_error: str | None = None
        max_attempts = self._settings.tool.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                with span(f"tool.call.{name}", logger, tool=name, attempt=attempt):
                    data = await asyncio.wait_for(
                        spec.handler(arguments),
                        timeout=self._settings.tool.timeout_seconds,
                    )
                self._record_success(name)
                if idempotency_key and spec.mutating:
                    self._idempotency[f"{name}:{idempotency_key}"] = dict(data)
                return ToolResult(
                    tool_name=name, ok=True, data=data,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            except asyncio.TimeoutError:
                last_error = f"timeout after {self._settings.tool.timeout_seconds}s"
                logger.warning("tool timeout: %s (attempt %s)", name, attempt)
            except Exception as exc:  # noqa: BLE001 - 工具异常必须兜住，不能穿透
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("tool error: %s (attempt %s) -> %s", name, attempt, last_error)

        self._record_failure(name)
        return ToolResult(
            tool_name=name, ok=False, error=last_error, degraded=True,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def default_registry() -> ToolRegistry:
    """构建默认工具注册中心，注册本期全部 MCP 工具。"""
    from .calendar_server import build_calendar_tools
    from .device_server import build_device_tools

    registry = ToolRegistry()
    for spec in build_device_tools() + build_calendar_tools():
        registry.register(spec)
    return registry


def make_idempotency_key(
    prefix: str,
    arguments: dict[str, Any],
    context: str = "",
) -> str:
    """生成幂等键。

    context 必须带上用户原始输入。原因：
    只按参数算键的话，"调低两度"和"把空调打开"可能算出相同的参数（都是 24 度），
    第二次会被误判成重复请求而拦截——那是两次不同的用户意图，不该拦。
    带上原始输入后：同一句指令重试 → 键相同 → 拦截（正确）；
    不同指令凑巧参数相同 → 键不同 → 正常执行（正确）。
    """
    args_sig = "|".join(f"{k}={arguments.get(k)}" for k in sorted(arguments))
    signature = f"{context}|{args_sig}"
    return f"{prefix}:{uuid.uuid5(uuid.NAMESPACE_OID, signature).hex[:12]}"
