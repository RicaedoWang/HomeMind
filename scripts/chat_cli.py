"""交互式命令行：用聊天的方式自定义你的家居习惯。

运行：
    python scripts/chat_cli.py

能干什么：
    直接说话就能定制习惯——记忆会持久化到 data/homemind.db，
    关掉再打开还在，不用改任何代码。

    你> 我喜欢睡觉时空调26度        → 记住偏好
    你> 把空调打开                  → 自动按 26 度执行
    你> 我习惯晚上十一点关客厅灯     → 再记一条
    你> 忘掉聚会的事                → 主动遗忘

内置命令：
  /habits          查看已记住的全部习惯
  /forget 关键词    忘掉某条记忆（硬删除）
  /clean           扫描并合并重复记忆
  /devices         查看设备当前状态
    /role L1~L4      切换身份（L4管理员 / L3成人 / L2儿童 / L1访客）
    /mode            查看当前是规则模式还是真模型模式
    /help            帮助
    /quit            退出

试试切换身份：用 /role L2 变成儿童视角，家长会这类高权限记忆就召不到了。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr, sys.stdin):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from homemind.agent.graph import build_agent_graph  # noqa: E402
from homemind.agent.state import initial_state  # noqa: E402
from homemind.config import get_settings  # noqa: E402
from homemind.core.trace import new_trace_id, set_trace_id, setup_logging  # noqa: E402
from homemind.memory.long_term import LongTermMemory  # noqa: E402
from homemind.memory.semantic import SemanticMemory  # noqa: E402
from homemind.memory.service import MemoryService  # noqa: E402
from homemind.memory.short_term import ShortTermMemory  # noqa: E402
from homemind.tools.device_server import get_device_states  # noqa: E402
from homemind.tools.registry import default_registry  # noqa: E402

ROLE_DESC = {"L4": "管理员", "L3": "成人", "L2": "儿童", "L1": "访客"}
MEMBER_ID = "member_001"


def hr(title: str = "") -> None:
    print("\n" + "-" * 62)
    if title:
        print(f"  {title}")
        print("-" * 62)


def show_help() -> None:
    print("""
  直接说话 = 让 Agent 干活 / 记住你的习惯

  /habits          查看已记住的全部习惯
  /forget 关键词    忘掉含该关键词的记忆（硬删除）
  /devices         查看设备当前状态
  /role L1~L4      切换身份：L4管理员 L3成人 L2儿童 L1访客
  /mode            当前是规则模式还是真模型模式
  /help            这份帮助
  /quit            退出
""")


async def run_turn(graph, text: str, role: str, confirmed: bool = False) -> None:
    """跑一轮对话，打印记忆与工具相关的细节。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    state = initial_state(user_input=text, member_id=MEMBER_ID,
                          member_role=role, confirmed=confirmed, trace_id=trace_id)
    result = await graph.ainvoke(state)

    recalled = result.get("recalled_memories", [])
    if recalled:
        print(f"  [召回] {len(recalled)} 条：")
        for mem in recalled[:3]:
            print(f"      · {mem['memory_value']}  (分 {mem['score']})")

    for item in result.get("applied_memories", []):
        print(f"  [应用] 记忆「{item['memory_value']}」→ {item['applied_as']}")

    for item in result.get("tool_results", []):
        if item["ok"]:
            print(f"  [执行] {item['data'].get('message')}")
        else:
            print(f"  [失败] {item['tool_name']}：{item['error']}")

    for mem in result.get("written_memories", []):
        print(f"  [记住] {mem['memory_value']}")

    print(f"  助手：{result.get('final_answer')}")


async def main() -> None:
    settings = get_settings()
    setup_logging("ERROR")

    # 记忆持久化到文件：关掉再打开，习惯还在
    service = MemoryService(
        long_term=LongTermMemory(db_path="data/homemind.db"),
        semantic=SemanticMemory(),
        short_term=ShortTermMemory(),
        settings=settings,
    )
    graph = build_agent_graph(registry=default_registry(), memory=service)
    role = "L4"

    # 启动时自动合并存量重复记忆（历史脏数据：早期版本没有写入去重）
    dedup_removed = service.deduplicate(MEMBER_ID)

    mode = "规则模式（离线）" if settings.llm.use_mock else f"真模型（{settings.llm.model}）"
    hr("HomeMind 家居习惯定制")
    print(f"  模式：{mode}")
    print(f"  身份：{role} {ROLE_DESC[role]}    记忆库：data/homemind.db（持久化）")
    if dedup_removed:
        print(f"  已自动合并 {dedup_removed} 条重复记忆（历史数据清理）")
    print("  输入 /help 看命令，直接说话就能定制习惯。")

    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not text:
            continue

        # ---- 内置命令 ----
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("/quit", "/exit", "/q"):
                print("再见。")
                break
            if cmd == "/help":
                show_help()
                continue
            if cmd == "/mode":
                print(f"  当前：{mode}")
                continue

            if cmd == "/role":
                if arg.upper() in ROLE_DESC:
                    role = arg.upper()
                    print(f"  已切换身份：{role} {ROLE_DESC[role]}")
                    print("  （切到 L2 儿童再问敏感问题，你会发现有些记忆召不到了）")
                else:
                    print("  用法：/role L4  (L4管理员 L3成人 L2儿童 L1访客)")
                continue

            if cmd in ("/habits", "/mem"):
                records = service.long_term.list_all()
                if not records:
                    print("  还没记住任何习惯。试着说：我喜欢睡觉时空调26度")
                    continue
                hr(f"已记住的习惯（{len(records)} 条）")
                for r in records:
                    print(f"  · [{r.memory_type}] {r.memory_value}")
                    print(f"      key={r.memory_key}  v{r.version}  "
                          f"重要度={r.importance}  可见={r.visible_role}")
                continue

            if cmd == "/clean":
                removed = service.deduplicate(MEMBER_ID)
                print(f"  扫描完成：合并了 {removed} 条重复记忆"
                      if removed else "  没有发现重复记忆")
                continue

            if cmd == "/forget":
                if not arg:
                    print("  用法：/forget 聚会")
                    continue
                removed = service.forget_by_keyword(MEMBER_ID, arg)
                print(f"  已彻底删除 {removed} 条记忆" if removed
                      else f"  没找到含「{arg}」的记忆")
                continue

            if cmd == "/devices":
                hr("设备状态")
                for dev in get_device_states().values():
                    state_txt = "开" if dev.power else "关"
                    value_txt = f"  {dev.value}度" if dev.value is not None else ""
                    print(f"  · {dev.name}（{dev.room}）  电源={state_txt}{value_txt}")
                continue

            print(f"  未知命令：{cmd}   输入 /help 看可用命令")
            continue

        # ---- 正常对话 ----
        await run_turn(graph, text, role)


if __name__ == "__main__":
    asyncio.run(main())
