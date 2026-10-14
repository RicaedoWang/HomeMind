"""FastAPI 服务入口（对齐 SRS 6.1 对外 API）。

V0.1 提供的接口：
    POST /api/v1/chat        对话主入口（支持 confirmed 二次确认）
    GET  /health             健康检查
    GET  /api/v1/tools       列出 MCP 工具清单
    GET  /api/v1/devices     查看设备状态（演示用）
    GET  /api/v1/schedule    查看已创建的提醒（演示用）
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from ..agent.graph import build_agent_graph
from ..agent.state import initial_state
from ..config import get_settings
from ..core.trace import get_logger, new_trace_id, set_trace_id, setup_logging
from ..memory.short_term import get_short_term_memory
from ..tools.calendar_server import get_schedules
from ..tools.device_server import get_device_states
from ..tools.registry import ToolRegistry, default_registry
from .auth import router as auth_router
from .console import mount_web, router as console_router

logger = get_logger("api")


class ChatRequest(BaseModel):
    """对话请求。"""

    text: str = Field(..., description="用户输入")
    session_id: str = Field(default="default", description="会话 ID")
    member_id: str = Field(default="member_001", description="家庭成员 ID")
    confirmed: bool = Field(default=False, description="是否已确认上一步的高危动作")


class ChatResponse(BaseModel):
    """对话响应。"""

    trace_id: str
    answer: str
    intents: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    requires_confirm: bool = False
    degraded: bool = False


# 全局单例
_GRAPH: Any = None
_REGISTRY: ToolRegistry | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化日志与 Agent 图。"""
    global _GRAPH, _REGISTRY
    settings = get_settings()
    setup_logging(settings.log_level)
    _REGISTRY = default_registry()
    _GRAPH = build_agent_graph(registry=_REGISTRY)
    logger.info("homemind api started, env=%s, mock_llm=%s",
                settings.env, settings.llm.use_mock)
    yield
    logger.info("homemind api stopped")


app = FastAPI(
    title="HomeMind 智能家居多智能体中枢",
    version="0.1.0",
    description="意图理解 → 任务规划 → 工具调用 → 应答生成的 Agent 服务",
    lifespan=lifespan,
)

# 认证（必须最先注册：控制台接口全部依赖它）
app.include_router(auth_router)

# 控制台 REST API（Web 界面数据源）
app.include_router(console_router)

# 托管前端静态站点：一条命令同时起 API 与 Web 控制台
mount_web(app)


@app.get("/health")
async def health() -> dict[str, Any]:
    """健康检查。

    memory_mode 走真实探测（health_check），不返回"我以为我在用 redis"——
    健康检查的意义就是暴露真实状态，不是复述配置。
    """
    memory = get_short_term_memory()
    return {
        "status": "ok",
        "env": get_settings().env,
        "mock_llm": get_settings().llm.use_mock,
        "memory_mode": await memory.health_check(),
    }


@app.get("/api/v1/tools")
async def list_tools() -> dict[str, Any]:
    """列出全部 MCP 工具（对应 MCP 的 list_tools）。"""
    registry = _REGISTRY or default_registry()
    return {"tools": registry.list_tools(), "count": len(registry.list_tools())}


@app.get("/api/v1/devices")
async def list_devices() -> dict[str, Any]:
    """查看设备状态（演示用）。"""
    return {"devices": [d.to_dict() for d in get_device_states().values()]}


@app.get("/api/v1/schedule")
async def list_schedule() -> dict[str, Any]:
    """查看已创建的提醒（演示用）。"""
    return {"schedules": get_schedules()}


@app.post("/api/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """对话主入口。

    流程：
    1. 生成 traceId 并注入上下文（全链路可追踪）
    2. 写入短期记忆（用户消息）
    3. 运行 LangGraph：意图 → 安全护栏 → 工具 → 应答
    4. 把助手应答写回短期记忆
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    logger.info("chat request: %s", req.text,
                extra={"extra_fields": {"event": "chat.request", "session": req.session_id}})

    state = initial_state(
        user_input=req.text,
        member_id=req.member_id,
        confirmed=req.confirmed,
        trace_id=trace_id,
    )

    memory = get_short_term_memory()
    await memory.append(req.session_id, "user", req.text)

    result = await _GRAPH.ainvoke(state)

    await memory.append(req.session_id, "assistant", result.get("final_answer", ""))

    return ChatResponse(
        trace_id=trace_id,
        answer=result.get("final_answer", ""),
        intents=result.get("intents", []),
        tasks=result.get("tasks", []),
        tool_results=result.get("tool_results", []),
        requires_confirm=result.get("requires_confirm", False),
        degraded=result.get("degraded", False),
    )
