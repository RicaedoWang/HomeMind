"""HomeMind Web 演示界面（V0.8）。

跑法：
    python scripts/web_ui.py
    # 浏览器打开 http://127.0.0.1:7860

想省钱跑 mock 模式：
    HM_USE_MOCK_LLM=true python scripts/web_ui.py

界面四块：
    ① 对话区        —— 直接说话，支持高危动作的二次确认
    ② 链路追踪     —— 把每一跳摊开：召回 → 意图 → 分派 → 护栏 → 各 Agent 执行 → 写记忆
    ③ 记忆库       —— 实时显示 Agent 记住了什么
    ④ 一键评测     —— 当场跑 49 条用例，看红线有没有守住

为什么链路要可视化：Agent 系统的可解释性就是可信度。
黑窗口里吐一句话没人信，把中间过程摊开才敢让人用。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import gradio as gr  # noqa: E402

from homemind.agent.state import initial_state  # noqa: E402
from homemind.agents.graph import build_multi_agent_graph  # noqa: E402
from homemind.core.llm import get_llm_client  # noqa: E402
from homemind.eval import render_report, run_eval  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402

MEMBER_ID = "member_001"
# 与 chat_cli 共用同一个库：网页里记下的习惯，命令行里也认
MEMORY = MemoryService(long_term=LongTermMemory(db_path="data/homemind.db"))
GRAPH = build_multi_agent_graph(memory=MEMORY)

_mode = "真模型（DeepSeek）" if get_llm_client().enabled else "mock（规则模式，不烧 token）"
_startup_dedup = MEMORY.deduplicate(MEMBER_ID)


# ---------------- 链路渲染 ----------------

def _score(mem: dict[str, Any]) -> str:
    value = mem.get("score")
    return f"{value:.3f}" if isinstance(value, (int, float)) else "-"


def render_trace(result: dict[str, Any]) -> str:
    """把一次对话的完整链路渲染成 Markdown。

    每一跳都对应图里的一个节点——看得见才谈得上可解释。
    """
    lines: list[str] = []
    lines.append(f"**trace_id** `{result.get('trace_id', '-')}`　|　**模式** {_mode}")
    if result.get("degraded"):
        lines.append("> ⚠ 本轮发生了降级（某个组件不可用，已走兜底路径）")
    lines.append("")

    # ① 记忆召回
    recalled = result.get("recalled_memories") or []
    lines.append(f"### ① 记忆召回　`{len(recalled)}` 条")
    if recalled:
        for mem in recalled[:5]:
            lines.append(f"- {mem.get('memory_value')}　`权重 {_score(mem)}`")
    else:
        lines.append("- （无相关记忆）")

    # ② 意图识别
    intents = result.get("intents") or []
    lines.append("")
    lines.append(f"### ② 意图识别　`{len(intents)}` 个")
    for intent in intents:
        entities = intent.get("entities") or {}
        shown = "，".join(f"{k}={v}" for k, v in entities.items() if v is not None)
        lines.append(f"- **{intent.get('type')}**　置信度 `{intent.get('confidence')}`　{shown}")

    # ③ Supervisor 分派
    assignments = result.get("agent_assignments") or []
    lines.append("")
    lines.append(f"### ③ Supervisor 分派　`{len(assignments)}` 个 Agent")
    if assignments:
        for item in assignments:
            lines.append(f"- **{item.get('agent')}**（{item.get('domain')}）　"
                         f"任务 `{len(item.get('task_ids', []))}` 个")
            lines.append(f"  - 理由：{item.get('reason')}")
    else:
        lines.append("- （无需工具，直接应答）")

    # ④ 安全护栏
    conflicts = result.get("conflicts") or []
    lines.append("")
    if result.get("requires_confirm"):
        lines.append("### ④ 安全护栏　🛑 **拦截**")
        lines.append(f"> {result.get('confirm_prompt', '')}")
        lines.append("> 回复「确认」才会真正执行。")
    else:
        lines.append("### ④ 安全护栏　✅ 放行")

    if conflicts:
        lines.append("")
        lines.append("**检测到冲突：**")
        for conflict in conflicts:
            lines.append(f"- ⚠ {conflict.get('detail')}")

    # ⑤ 各 Agent 执行
    outcomes = result.get("agent_results") or []
    lines.append("")
    lines.append(f"### ⑤ 子 Agent 执行　`{len(outcomes)}` 个")
    if outcomes:
        for item in outcomes:
            status = "降级" if item.get("degraded") else "正常"
            lines.append(f"- **{item.get('agent')}**　{status}　"
                         f"工具调用 `{item.get('tool_calls')}` 次　"
                         f"耗时 `{item.get('elapsed_ms')}ms`")
            for skipped in item.get("skipped_tasks", []):
                lines.append(f"  - ↳ 跳过 `{skipped.get('tool_name')}`：{skipped.get('reason')}")
            if item.get("error"):
                lines.append(f"  - ↳ 错误：{item.get('error')}")
    tool_results = result.get("tool_results") or []
    if tool_results:
        lines.append("")
        lines.append("**工具结果：**")
        for res in tool_results:
            if res.get("ok"):
                message = (res.get("data") or {}).get("message", "")
                idem = "（幂等命中，未重复执行）" if res.get("idempotent_hit") else ""
                lines.append(f"- ✅ `{res.get('tool_name')}`：{message}{idem}")
            else:
                lines.append(f"- ❌ `{res.get('tool_name')}`：{res.get('error')}")

    applied = result.get("applied_memories") or []
    if applied:
        lines.append("")
        lines.append("**记忆影响到了参数：**")
        for mem in applied:
            lines.append(f"- 依据「{mem.get('memory_value')}」→ {mem.get('applied_as')}")

    # ⑥ 记忆写入
    written = result.get("written_memories") or []
    lines.append("")
    lines.append(f"### ⑥ 记忆写入　`{len(written)}` 条")
    for mem in written:
        key = mem.get("memory_key") if isinstance(mem, dict) else getattr(mem, "memory_key", "")
        value = mem.get("memory_value") if isinstance(mem, dict) else getattr(mem, "memory_value", "")
        lines.append(f"- `{key}` {value}")

    return "\n".join(lines)


def list_habits() -> str:
    """列出当前记住的习惯（与 /habits 一致）。"""
    records = MEMORY.long_term.list_by_member(MEMBER_ID, viewer_role="L4")
    if not records:
        return "还没有记住任何习惯。试试说「我喜欢睡觉时空调 26 度」。"
    lines = [f"共 {len(records)} 条：", ""]
    for idx, record in enumerate(records, 1):
        lines.append(f"{idx}. {record.memory_value}")
    return "\n".join(lines)


# ---------------- 交互逻辑 ----------------

CONFIRM_WORDS = {"确认", "确认执行", "是", "好的", "ok", "OK", "yes", "执行"}


async def handle_message(
    message: str,
    history: list[dict[str, str]],
    pending: dict[str, Any],
):
    """处理一条用户输入。返回 (对话历史, 链路面板, 习惯面板, pending, 输入框清空)。

    注意：Gradio 6 起 Chatbot 只吃 messages 格式（[{role, content}]），
    不再接受老的 [(问, 答)] 二元组。
    """
    if not message.strip():
        return history, "", "", pending, ""

    confirmed = False
    text = message.strip()
    # 上一轮被护栏拦下、用户这轮回复的是确认 → 用原话重跑，并带上 confirmed
    if pending and pending.get("requires_confirm") and text in CONFIRM_WORDS:
        text = pending.get("text", text)
        confirmed = True

    state = initial_state(
        user_input=text,
        member_id=MEMBER_ID,
        trace_id=uuid.uuid4().hex[:8],
        confirmed=confirmed,
    )
    result = await GRAPH.ainvoke(state)

    answer = result.get("final_answer", "")
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]

    new_pending = {
        "text": text,
        "requires_confirm": bool(result.get("requires_confirm")) and not confirmed,
    }
    return history, render_trace(result), list_habits(), new_pending, ""


async def handle_eval():
    """一键跑评测，把报告吐到界面上。"""
    report = await run_eval()  # 用独立的 data/eval.db，不碰真实记忆库
    ok = report.redline_passed
    verdict = "✅ 评测通过，安全红线全部守住" if ok else "❌ 有红线用例失败，本次改动不该合入"
    return f"{verdict}\n\n{render_report(report)}"


def clear_all():
    return [], "", "", {"requires_confirm": False}


EXAMPLES = [
    ["把客厅的灯关掉"],
    ["把空调打开，另外提醒我明天早上八点开会"],
    ["空调滤网要多久洗一次"],
    ["我习惯睡觉时空调开 26 度"],
    ["看看你记录的我的习惯"],
    ["把大门门锁打开"],
    ["把客厅灯打开再关掉"],
]


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="HomeMind · 智能家居多智能体中枢") as demo:
        gr.Markdown(
            f"# HomeMind · 智能家居多智能体中枢\n"
            f"**V0.8**　多 Agent 协同 + 三层记忆 + 安全护栏 + 端到端评测　|　"
            f"当前模型模式：**{_mode}**"
        )

        pending_state = gr.State({"requires_confirm": False})

        with gr.Row():
            with gr.Column(scale=3):
                chatbot = gr.Chatbot(height=430, label="对话")
                with gr.Row():
                    msg = gr.Textbox(placeholder="说点什么，比如「把客厅的灯关掉」……",
                                     scale=4, lines=2, label="输入")
                with gr.Row():
                    send_btn = gr.Button("发送", variant="primary")
                    confirm_btn = gr.Button("回复「确认」", variant="secondary")
                    clear_btn = gr.Button("清空会话")
                gr.Examples(examples=EXAMPLES, inputs=msg,
                            label="点一下就能演示（含高危与冲突样例）")

            with gr.Column(scale=4):
                with gr.Tab("链路追踪"):
                    trace_md = gr.Markdown("发一句话，这里会显示每一跳的完整过程。")
                with gr.Tab("记忆库"):
                    habit_md = gr.Markdown(list_habits())
                    refresh_btn = gr.Button("刷新记忆库")
                with gr.Tab("一键评测"):
                    gr.Markdown("跑 49 条端到端用例。**安全红线采用一票否决**："
                                "任一红线用例失败即判不合格。")
                    eval_btn = gr.Button("运行评测", variant="primary")
                    eval_out = gr.Code(language=None, label="评测报告", lines=26)

        inputs = [msg, chatbot, pending_state]
        outputs = [chatbot, trace_md, habit_md, pending_state, msg]

        send_btn.click(handle_message, inputs=inputs, outputs=outputs)
        msg.submit(handle_message, inputs=inputs, outputs=outputs)
        confirm_btn.click(
            lambda history, pending: handle_message("确认", history, pending),
            inputs=[chatbot, pending_state], outputs=outputs,
        )
        clear_btn.click(clear_all, outputs=[chatbot, trace_md, habit_md, pending_state])
        refresh_btn.click(list_habits, outputs=[habit_md])
        eval_btn.click(handle_eval, outputs=[eval_out])

    return demo


def main() -> None:
    if _startup_dedup:
        print(f"启动时自动合并了 {_startup_dedup} 条重复记忆")
    demo = build_ui()
    port = int(os.getenv("HM_WEB_PORT", "7860"))
    print(f"\nHomeMind Web 界面启动中 → http://127.0.0.1:{port}")
    print(f"模型模式：{_mode}")
    print("按 Ctrl+C 退出\n")
    demo.queue().launch(server_name="127.0.0.1", server_port=port, share=False,
                        inbrowser=True)


if __name__ == "__main__":
    main()
