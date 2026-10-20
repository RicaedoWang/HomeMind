"""LangGraph 状态图：意图 → 安全护栏 → 工具执行 → 应答。

图结构（对齐 FR-AGT-001、FR-SEC-001、FR-TOOL-003）：

    START → parse_intent → safety_gate ─┬─(需确认)→ respond → END
                                        └─(可执行)→ execute_tools → respond → END

为什么把 safety_gate 独立成一个节点，而不是塞进 execute：
安全校验必须是**一等公民**而不是散落在各处的 if，
这样才能保证"误操作率 = 0"这条验收标准有唯一的实现位置可审计。
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, StateGraph

from ..core.llm import get_llm_client
from ..core.models import IntentType, TaskStatus
from ..core.tenancy import DEFAULT_FAMILY_ID
from ..core.trace import get_logger, span
from ..memory.service import MemoryService, get_memory_service
from ..tools.registry import ToolRegistry
from .execution import apply_memory_to_arguments as _apply_memory_to_arguments
from .execution import execute_task
from .intent import IntentParser
from .state import AgentState
from .time_parser import describe_now, is_time_question

logger = get_logger("agent.graph")

# 高危动作：门锁、燃气阀等，执行前必须二次确认
HIGH_RISK_TOOLS = {"lock_control"}


# ---------------- 节点 0：记忆召回 ----------------

async def recall_memory_node(state: AgentState, service: MemoryService) -> dict[str, Any]:
    """进入主链路前先召回相关记忆（FR-MEM-005）。

    为什么放在最前面：召回结果要能影响后续的任务参数
    （"把空调调好"需要用到记忆里存的 26 度），
    所以必须在任务规划之前就拿到手。
    """
    with span("node.recall_memory", logger, trace_id=state.get("trace_id", "-")):
        recalled = service.recall(
            query=state["user_input"],
            member_id=state.get("member_id"),
            viewer_role=state.get("member_role", "L4"),
            top_k=5,
            family_id=state.get("family_id") or DEFAULT_FAMILY_ID,
        )
        return {"recalled_memories": recalled}


# ---------------- 节点 5：记忆写入 ----------------

# 注：记忆补参与增量调节的实现已抽到 execution.py，与子 Agent 共用同一份治理逻辑。

async def write_memory_node(state: AgentState, service: MemoryService) -> dict[str, Any]:
    """应答生成后，把本轮对话里的记忆点写入记忆库（FR-MEM-004）。"""
    with span("node.write_memory", logger, trace_id=state.get("trace_id", "-")):
        # 查询类输入不该被记住——"看下我的习惯"本身不是一条习惯
        types = {i.get("type") for i in state.get("intents", [])}
        if IntentType.MEMORY_QUERY.value in types:
            return {"written_memories": []}
        written = service.write(
            member_id=state.get("member_id", "member_001"),
            text=state["user_input"],
            visible_role=state.get("member_role", "L3"),
            family_id=state.get("family_id") or DEFAULT_FAMILY_ID,
        )
        return {"written_memories": written}


# ---------------- 节点：意图识别与任务规划 ----------------

def _intent_to_task(intent_dict: dict[str, Any]) -> dict[str, Any] | None:
    """把单个意图转成可执行任务（工具调用参数）。

    这是"自然语言 → 工具调用"的关键映射层（对齐 JD 职责 8 业务抽象能力）。
    """
    intent_type = intent_dict.get("type")
    entities = intent_dict.get("entities", {})
    task_id = f"task_{uuid.uuid4().hex[:8]}"

    if intent_type == IntentType.DEVICE_CONTROL.value:
        action = entities.get("action")

        # 门锁走专门的高危工具，不能直接当普通设备开关——
        # 否则安全护栏（HIGH_RISK_TOOLS）形同虚设。
        if entities.get("device_name") == "门锁":
            return {
                "task_id": task_id, "intent": intent_dict,
                "tool_name": "lock_control",
                "arguments": {
                    "action": "unlock" if action == "turn_on" else "lock",
                    "device_name": "门锁",
                },
                "status": TaskStatus.PENDING.value,
                "requires_confirm": True,  # 高危动作强制确认（FR-SEC-001）
            }

        if action in ("delta_up", "delta_down"):
            # 增量调节需要两步：先查当前值，再设新值（执行节点里解析）
            tool_name = "device_control"
            arguments = {
                "action": action,
                "device_name": entities.get("device_name"),
                "room": entities.get("room"),
                "delta": entities.get("delta", 1.0),
            }
        elif action == "set_value":
            tool_name = "device_control"
            arguments = {"action": "set_value", "device_name": entities.get("device_name"),
                         "room": entities.get("room"), "value": entities.get("value")}
        elif action in ("turn_on", "turn_off", "query"):
            tool_name = "device_control"
            arguments = {"action": action, "device_name": entities.get("device_name"),
                         "room": entities.get("room")}
        else:
            return None
        return {"task_id": task_id, "intent": intent_dict, "tool_name": tool_name,
                "arguments": arguments, "status": TaskStatus.PENDING.value,
                "requires_confirm": False}

    if intent_type == IntentType.SCHEDULE.value:
        if entities.get("needs_clarify"):
            # 时间解析不出来：不猜，直接生成澄清任务（对齐 FR-INT-004）
            return {"task_id": task_id, "intent": intent_dict, "tool_name": None,
                    "arguments": {}, "status": TaskStatus.AWAITING_CONFIRM.value,
                    "requires_confirm": True,
                    "clarify": "没听清具体时间，请告诉我提醒的时间"}
        arguments = {"content": entities.get("content"),
                     "scheduled_at": entities.get("scheduled_at")}
        return {"task_id": task_id, "intent": intent_dict, "tool_name": "create_reminder",
                "arguments": arguments, "status": TaskStatus.PENDING.value,
                "requires_confirm": True}  # 创建日程一律回显确认，防误创建

    # memory_write / knowledge / chat：本期不调工具，直接应答
    # 记忆写入由 write_memory 节点统一处理，不在这里生成任务
    return None


async def parse_intent_node(state: AgentState, parser: IntentParser) -> dict[str, Any]:
    """节点 1：意图识别 + 任务规划。

    family_id 必须传下去：用户自建的设备（咖啡机、加湿器…）只有本家庭认识，
    不传的话规则层只会拿内置词表去匹配，"打开咖啡机"直接判成闲聊。
    """
    with span("node.parse_intent", logger, trace_id=state.get("trace_id", "-")):
        intents = await parser.parse(state["user_input"], state.get("family_id"))
        tasks = [t for t in (_intent_to_task(i.to_dict()) for i in intents) if t]
        return {
            "intents": [i.to_dict() for i in intents],
            "tasks": tasks,
        }


# ---------------- 节点：安全护栏 ----------------

async def safety_gate_node(state: AgentState) -> dict[str, Any]:
    """节点 2：安全校验，决定是否需要二次确认。

    规则：
    1. 用户显式确认过（第二轮带 confirmed=True）→ 放行
    2. 命中高危工具（门锁等）→ 拦截确认
    3. 创建类任务（日程/提醒）→ 回显确认，防误创建
    4. 意图置信度过低 → 转澄清，防幻觉执行
    """
    with span("node.safety_gate", logger, trace_id=state.get("trace_id", "-")):
        if state.get("confirmed"):
            return {"requires_confirm": False, "confirm_prompt": ""}

        threshold = 0.7
        pending: list[str] = []

        for task in state.get("tasks", []):
            tool_name = task.get("tool_name")
            confidence = task.get("intent", {}).get("confidence", 1.0)

            if tool_name in HIGH_RISK_TOOLS:
                pending.append(f"【高危】即将执行 {tool_name}，参数 {task.get('arguments')}")
            elif task.get("requires_confirm"):
                pending.append(f"即将创建：{task.get('arguments', {}).get('content')}"
                               f"（{task.get('arguments', {}).get('scheduled_at')}）")
            elif confidence < threshold:
                pending.append(f"我不太确定你的意思是「{task.get('intent', {}).get('raw_text')}」，"
                               f"能再说清楚一点吗？")

        # 多 Agent 冲突：同一设备被要求做相反动作（V0.8）
        # 单看都合法，合起来自相矛盾，必须先跟用户对清楚
        for conflict in state.get("conflicts", []):
            pending.append(f"【冲突】{conflict.get('detail')}")

        # 意图整体置信度过低：直接澄清
        if not pending and state.get("intents"):
            lowest = min(i.get("confidence", 1.0) for i in state["intents"])
            if lowest < 0.5:
                pending.append("我没太听懂，能换种说法吗？")

        if pending:
            return {"requires_confirm": True, "confirm_prompt": "；".join(pending)}
        return {"requires_confirm": False, "confirm_prompt": ""}


# ---------------- 节点：工具执行 ----------------

async def execute_tools_node(state: AgentState, registry: ToolRegistry) -> dict[str, Any]:
    """节点 3：执行工具调用，带治理与降级。"""
    results: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    degraded = False

    with span("node.execute_tools", logger, trace_id=state.get("trace_id", "-")):
        for task in state.get("tasks", []):
            tool_name = task.get("tool_name")
            if not tool_name:
                continue
            # 澄清类任务（如时间缺失）不执行
            if task.get("status") == TaskStatus.AWAITING_CONFIRM.value:
                continue
            # 需确认但用户尚未确认 → 挂起，不执行（FR-SEC-001）
            # 注意：不能因为某个任务待确认就把整批任务都卡住，
            # 否则"关灯+建日程"这种组合里，能马上做的关灯也被耽误了。
            if task.get("requires_confirm") and not state.get("confirmed"):
                continue

            # 执行原语（含增量解析、记忆补全、幂等键）与子 Agent 共用同一份实现
            result_dict, applied_mem, task_degraded = await execute_task(registry, task, state)
            if applied_mem:
                applied.append(applied_mem)
                logger.info("memory applied to tool args: %s", applied_mem["applied_as"],
                            extra={"extra_fields": {"event": "memory.apply",
                                                    "key": applied_mem["memory_key"]}})
            if task_degraded:
                degraded = True
            results.append(result_dict)

    return {"tool_results": results, "degraded": degraded, "applied_memories": applied}


# ---------------- 节点：应答生成 ----------------

def _fallback_message(tool_name: str, error: str | None) -> str:
    """工具失败时的兜底话术（对齐 FR-TOOL-003：失败必须有用户可理解的提示）。"""
    if error == "circuit_open":
        return f"{tool_name} 暂时不可用，我已跳过这一步，稍后可以再试"
    if error and "timeout" in error:
        return f"{tool_name} 响应超时，我没能完成这一步，要不要再试一次？"
    return f"{tool_name} 执行失败了（{error or '未知原因'}），我没敢乱动，你说下一步怎么办？"


async def _llm_generate_answer(state: AgentState, facts: list[str]) -> str | None:
    """用大模型生成自然语言应答。失败返回 None，由调用方回退模板。

    为什么不把整段应答都交给模型自由发挥：
    工具已经执行完了，结果是确定事实；模型的任务是把事实说得自然，
    而不是重新编一遍——让它自由发挥就会 hallucinate 出"已经帮你关了窗"这种鬼话。
    所以 prompt 里明确列出执行结果，只让它做表达层润色。
    """
    client = get_llm_client()
    if not client.enabled:
        return None

    memory_lines = "\n".join(
        f"- {m['memory_value']}" for m in state.get("recalled_memories", [])[:5]
    ) or "（这个家庭暂无记忆）"
    applied_lines = "\n".join(
        f"- 依据记忆「{m['memory_value']}」把参数调整为 {m['applied_as']}"
        for m in state.get("applied_memories", [])
    )
    fact_lines = "\n".join(f"- {f}" for f in facts) if facts else "（本轮没有执行任何设备操作）"

    # 多轮上下文：滑动窗口内的原文 + 窗口外被压缩的摘要。
    # 有了它，"我刚刚干了什么"才答得上来；没有它，模型对上一轮一无所知。
    history = state.get("chat_history") or []
    history_lines = "\n".join(
        f"{m.get('role', 'user')}: {str(m.get('content', ''))[:160]}" for m in history[-10:]
    ) or "（这是本次会话的第一句）"
    summary = (state.get("chat_summary") or "").strip()

    system = (
        "你是智能家居助手。说话简短自然，用中文，一两句说完。\n"
        "严格要求：只能基于下面给出的信息回答，不得编造任何没发生过的操作；"
        "如果执行失败，如实说明并给一句建议。"
    )
    prompt = f"""{describe_now()}

用户说：{state['user_input']}

最近对话（旧→新）：
{history_lines}
{('更早对话摘要：' + summary + chr(10)) if summary else ''}
这个家庭的记忆：
{memory_lines}
{('本次应用了记忆：' + chr(10) + applied_lines) if applied_lines else ''}

刚刚实际执行的结果：
{fact_lines}

请回答用户：
- 若执行了操作，把结果用自然的话告诉他；用到了记忆就顺带说"按你之前的习惯"。
- 若用户在询问记忆或习惯，根据"这个家庭的记忆"如实回答。
- 若用户问"刚才/刚刚做了什么"，根据"最近对话"和"刚刚实际执行的结果"如实概括。
- 若用户问现在几点、今天几号、星期几，**直接根据上面第一行的"现在"回答**，
  不要说你不知道当前时间（模型没有系统时钟，这个时间是服务端算好喂给你的）。
- **"最近对话"里出现的"即将创建…""确认请回复「确认」"只是询问确认，什么都没执行。**
  只有"刚刚实际执行的结果"里明确列出的才算完成。历史里提过不等于已经做了，
  没执行就直说没执行（例如"这句我没执行任何操作"），**绝不能因为历史里提过就宣称"已创建""已完成"**。
- 若用户只是闲聊或提问，结合对话上下文正常回应，不要硬扯设备操作。"""

    result = await client.chat(prompt, system=system, max_tokens=200)
    return result or None


async def respond_node(state: AgentState, service: MemoryService) -> dict[str, Any]:
    """节点 4：生成最终应答。

    两种模式：
    - 规则模式（默认）：模板拼装，确定性、可回归测试。
    - 大模型模式（关掉 mock 且配了 key）：模型把执行结果润色成自然语言，
      失败自动回退模板——模型挂了用户照样拿到结果。
    """
    with span("node.respond", logger, trace_id=state.get("trace_id", "-")):
        # 记忆查询：清单本身必须准确（不能让模型自由发挥），
        # 但"我刚刚干了什么"这类问法也会落进这个意图——它问的是对话历史不是记忆。
        # 所以：清单作为确定事实喂给模型，让它结合"最近对话"作答；
        # 模型不可用（mock / 挂了）时回退纯模板，测试确定性不受影响。
        if any(i.get("type") == IntentType.MEMORY_QUERY.value
               for i in state.get("intents", [])):
            records = service.long_term.list_by_member(
                state.get("member_id", "member_001"),
                viewer_role=state.get("member_role", "L4"),
                family_id=state.get("family_id") or DEFAULT_FAMILY_ID,
            )
            mem_facts = [f"记忆清单第{i}条：{r.memory_value}"
                         for i, r in enumerate(records, 1)]
            client = get_llm_client()
            if client.enabled:
                generated = await _llm_generate_answer(state, mem_facts)
                if generated:
                    return {"final_answer": generated}
            if not records:
                return {"final_answer":
                        "我还没记住你的任何习惯。可以说「我喜欢…」或「我习惯…」教我。"}
            lines = "\n".join(
                f"  {idx}. {r.memory_value}" for idx, r in enumerate(records, 1)
            )
            return {"final_answer": f"你让我记住的习惯共 {len(records)} 条：\n{lines}"}

        # 记忆写入：用户说"我习惯…""我喜欢…"这类陈述，必须明确告诉他记住了什么。
        # 曾经这里没处理 memory_write，用户刚说完习惯，回的却是闲聊模板
        # "我在听。可以说「我喜欢…」" —— 他刚说完，你让他再说一遍？
        # 用 extract_memories 预览（它和 write_memory_node 用同一个抽取器），
        # 所以应答里说的"记住了 X"和实际入库的内容**必然一致**，不会撒谎。
        if any(i.get("type") == IntentType.MEMORY_WRITE.value
               for i in state.get("intents", [])):
            extracted = service.extract_memories(state["user_input"])
            if extracted:
                items = "；".join(value for _, _, value in extracted)
                return {"final_answer": f"记住了：{items}。之后我会照着做。"}
            return {"final_answer":
                    "这句我没听出要记的习惯。可以说「我习惯…」「我喜欢…」，我来记。"}

        parts: list[str] = []
        for item in state.get("tool_results", []):
            if item.get("ok"):
                message = item.get("data", {}).get("message")
                if message:
                    if item.get("idempotent_hit"):
                        message += "（检测到重复请求，未重复执行）"
                    parts.append(message)
            else:
                parts.append(_fallback_message(item.get("tool_name", "tool"),
                                               item.get("error")))

        # 告诉用户"我用了你之前说的偏好"——记忆生效必须让用户感知到，
        # 否则他只会觉得 Agent 莫名其妙自作主张
        for mem in state.get("applied_memories", []):
            parts.append(f"（按你记下的偏好「{mem['memory_value']}」，已应用为 {mem['applied_as']}）")

        # 挂起待确认的任务：能做的先做完，剩下的回显给用户确认
        if state.get("requires_confirm") and not state.get("confirmed"):
            prompt = state.get("confirm_prompt", "")
            if not parts:
                return {"final_answer": f"{prompt}（确认请回复「确认」）"}
            parts.append(f"另外——{prompt}（确认请回复「确认」）")

        if not parts:
            # 没有工具执行：可能是闲聊、提问或查询。先试模型（带记忆上下文），
            # 失败再兜底——"好的我记下了"对提问的用户是答非所问。
            recalled = state.get("recalled_memories", [])
            generated = await _llm_generate_answer(state, [])
            if generated:
                return {"final_answer": generated}
            # 规则模式（模型不可用）下问"现在几点"必须有答案：
            # 模型有 prompt 里的时间可用，规则模式没有，全靠这一句模板兜底。
            # 放在闲聊模板之前，否则用户会收到"我在听，可以说我喜欢…"这种鬼话。
            if is_time_question(state.get("user_input", "")):
                return {"final_answer": f"{describe_now()}。需要我帮你安排点什么吗？"}
            if recalled:
                mems = "；".join(m["memory_value"] for m in recalled[:3])
                return {"final_answer": f"我记得你之前说过：{mems}。需要我做什么？"}
            return {"final_answer": "我在听。可以说「我喜欢…」让我记住习惯，或直接下指令。"}

        template_answer = "；".join(parts)
        # 到这里说明没有待确认项，可以让模型润色；失败就用模板，服务照样可用
        generated = await _llm_generate_answer(state, parts)
        return {"final_answer": generated or template_answer}


# ---------------- 图的组装 ----------------

def _route_after_safety(state: AgentState) -> str:
    """条件边：存在可立即执行的任务就执行，全部待确认才直接应答。"""
    if state.get("confirmed"):
        return "execute"
    executable = [
        t for t in state.get("tasks", [])
        if t.get("tool_name") and not t.get("requires_confirm")
    ]
    return "execute" if executable else "respond"


def build_agent_graph(
    parser: IntentParser | None = None,
    registry: ToolRegistry | None = None,
    memory: MemoryService | None = None,
):
    """构建并编译 LangGraph 状态图。

    依赖注入 parser / registry / memory，便于测试时替换为 Mock 实现。
    """
    from ..tools.registry import default_registry

    _parser = parser or IntentParser()
    _registry = registry or default_registry()
    _memory = memory or get_memory_service()

    # 注意：这里必须用 async def 闭包，不能用 lambda。
    # LangGraph 通过 inspect.iscoroutinefunction 判断节点是否异步，
    # 用 lambda 包一层会让它误判为同步节点，导致协程没被 await 就直接当返回值。
    async def _parse_node(state: AgentState) -> dict[str, Any]:
        return await parse_intent_node(state, _parser)

    async def _execute_node(state: AgentState) -> dict[str, Any]:
        return await execute_tools_node(state, _registry)

    async def _recall_node(state: AgentState) -> dict[str, Any]:
        return await recall_memory_node(state, _memory)

    async def _write_node(state: AgentState) -> dict[str, Any]:
        return await write_memory_node(state, _memory)

    graph = StateGraph(AgentState)
    async def _respond_node(state: AgentState) -> dict[str, Any]:
        return await respond_node(state, _memory)

    graph.add_node("recall_memory", _recall_node)
    graph.add_node("parse_intent", _parse_node)
    graph.add_node("safety_gate", safety_gate_node)
    graph.add_node("execute_tools", _execute_node)
    graph.add_node("respond", _respond_node)
    graph.add_node("write_memory", _write_node)

    # 记忆在最前（要影响参数），写入在最后（要记录本轮对话）
    graph.set_entry_point("recall_memory")
    graph.add_edge("recall_memory", "parse_intent")
    graph.add_edge("parse_intent", "safety_gate")
    graph.add_conditional_edges(
        "safety_gate",
        _route_after_safety,
        {"execute": "execute_tools", "respond": "respond"},
    )
    graph.add_edge("execute_tools", "respond")
    graph.add_edge("respond", "write_memory")
    graph.add_edge("write_memory", END)

    return graph.compile()
