"""大模型连通性自检。

运行：python scripts/check_llm.py

用途：填完 .env 里的 Key 后，先跑这个确认模型真的能通，
再切到模型模式跑主链路。避免"跑 demo 半天才发现 Key 是错的"。

会依次检查：
1. .env 是否被正确加载
2. Key 有没有填
3. 真实调用一次接口，看看能不能拿到回复
4. 结构化 JSON 输出能不能被正确解析
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from homemind.config import get_settings  # noqa: E402
from homemind.core.llm import get_llm_client  # noqa: E402
from homemind.core.trace import setup_logging  # noqa: E402


def mask(key: str) -> str:
    """脱敏显示 Key：只留前 6 位和后 4 位。"""
    if not key:
        return "(空)"
    if len(key) <= 12:
        return key[:4] + "*" * (len(key) - 4)
    return f"{key[:6]}{'*' * 8}{key[-4:]}"


async def main() -> None:
    setup_logging("ERROR")  # 自检时只报错误，保持输出干净
    settings = get_settings()
    client = get_llm_client()

    print("=" * 66)
    print("  HomeMind 大模型连通性自检")
    print("=" * 66)

    print(f"\n[1] 配置读取")
    print(f"    mock 模式   : {settings.llm.use_mock}")
    print(f"    模型        : {settings.llm.model}")
    print(f"    接口地址    : {settings.llm.base_url}")
    print(f"    API Key     : {mask(settings.llm.api_key)}")

    print(f"\n[2] 启用判断")
    if not settings.llm.api_key:
        print("    ✗ 未填写 API Key")
        print("\n    请在项目根目录的 .env 里填写：")
        print("        HM_LLM_API_KEY=你的key")
        print("        HM_USE_MOCK_LLM=false")
        print("\n    填完后重新运行本脚本。")
        return
    if settings.llm.use_mock:
        print("    ! Key 已填，但 HM_USE_MOCK_LLM=true，当前仍是规则模式")
        print("      想用真模型请把 .env 里的 HM_USE_MOCK_LLM 改成 false")
    else:
        print("    ✓ 已启用真模型模式")

    print(f"\n[3] 真实调用测试（说句话看它回不回）")
    reply = await client.chat("用一句话介绍你自己，不超过20个字。", max_tokens=100)
    if reply is None:
        print("    ✗ 调用失败：可能是 Key 无效、网络不通、或接口地址写错")
        print("      请检查 .env 里的 HM_LLM_API_KEY 与 HM_LLM_BASE_URL")
        return
    print(f"    ✓ 模型回复：{reply[:80]}")

    print(f"\n[4] 结构化输出测试（意图解析要靠它）")
    data = await client.chat_json(
        '把下面内容转成 JSON：{"ok": true, "n": 3}。只输出 JSON。',
        system="只输出 JSON，不要解释。",
    )
    if data is None:
        print("    ✗ JSON 解析失败（模型输出不规范）")
        print("      系统会自动降级到规则解析，不影响运行，但建议换个模型")
        return
    print(f"    ✓ 解析成功：{data}")

    print("\n" + "=" * 66)
    if settings.llm.use_mock:
        print("  结论：模型可用。把 HM_USE_MOCK_LLM 改成 false 即可启用真模型。")
    else:
        print("  结论：全部通过，可以跑 python scripts/demo_agent_memory.py 了")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
