"""知识问答 Agent：设备说明书 / 使用常识（F13 RAG 的最小可用版）。

检索策略：内置文档片段 + 字符 bigram Jaccard 相似度。
为什么不上真向量库：本地没 embedding 服务，硬上 Milvus 会引入一个必挂的外部依赖。
先把检索链路跑通（召回 → 拼上下文 → 生成 → 降级），V0.8 后续换 embedding 只是替换一行。

诚实原则：检索不到就说检索不到，不让它瞎编说明书内容。
"""

from __future__ import annotations

from typing import Any

from ..core.llm import get_llm_client
from ..memory.semantic import jaccard_similarity
from .base import AgentOutcome, BaseAgent

# 内置知识片段：（标题, 正文）。真实项目里这一层换成向量库 + 文档切片。
DOCS: list[tuple[str, str]] = [
    ("空调滤网清洗", "空调滤网建议每两周清洗一次：断电后打开前面板，取出滤网用清水冲洗，"
                "晾干后再装回。长期不洗会导致制冷变慢、耗电上升。"),
    ("空调省电温度", "夏季制冷建议设定 26 度，配合风扇使用体感可再降 1-2 度；"
              "每调低 1 度耗电量约增加 7%-10%。"),
    ("洗衣机图标含义", "洗衣机面板上的雪花图标表示冷水洗，波浪线加数字表示转速档位，"
                "桶形图标带锁表示童锁已开启，长按 3 秒可解除。"),
    ("门锁换电池", "智能门锁电量低时会发出滴滴提示音，更换 4 节 5 号碱性电池即可；"
             "更换过程约 2 分钟，期间建议用机械钥匙开门。"),
    ("灯光色温选择", "客厅建议 4000K 中性光，卧室睡前建议 2700K 暖光，"
              "暖光有助于褪黑素分泌，改善入睡。"),
]


def retrieve(query: str, top_k: int = 2) -> list[tuple[str, str, float]]:
    """按 Jaccard 相似度检索最相关的文档片段。"""
    scored = [(title, body, jaccard_similarity(query, title + body)) for title, body in DOCS]
    scored.sort(key=lambda x: x[2], reverse=True)
    return [s for s in scored[:top_k] if s[2] > 0.0]


class KnowledgeAgent(BaseAgent):
    name = "knowledge_agent"
    domain = "知识问答"
    description = "设备说明书与家居常识问答（本地检索 + 大模型生成）"
    allowed_tools = set()  # 只回答，不操作任何设备
    handled_intents = {"knowledge"}

    async def handle(
        self,
        state: dict[str, Any],
        tasks: list[dict[str, Any]],
        registry: Any,
        outcome: AgentOutcome,
    ) -> AgentOutcome:
        query = state.get("user_input", "")
        hits = retrieve(query, top_k=2)

        if not hits:
            outcome.answer = ("这个我在说明书库里没查到。可以换个说法再问，"
                              "或者告诉我设备型号，我再帮你查。")
            outcome.ok = True
            return outcome

        context = "\n".join(f"【{t}】{b}" for t, b, _ in hits)

        client = get_llm_client()
        if not client.enabled:
            # 没有模型时，如实把资料原文给出——这比编造强，也比沉默强
            outcome.answer = f"资料库里找到这段说明：{hits[0][1]}"
            outcome.degraded = True
            return outcome

        system = ("你是智能家居助手，只能依据下面给出的说明书片段回答，"
                  "不许补充片段里没有的信息。回答控制在三句话内，中文。")
        prompt = f"说明书片段：\n{context}\n\n用户问：{query}\n请回答："
        answer = await client.chat(prompt, system=system, max_tokens=250)

        if not answer:
            outcome.answer = f"资料库里找到这段说明：{hits[0][1]}"
            outcome.degraded = True
        else:
            outcome.answer = answer
        return outcome
