"""大模型客户端（OpenAI 兼容协议）。

设计要点：

1. **一套代码接所有模型**：DeepSeek / 通义 / OpenAI / 本地 vLLM
   都是 OpenAI 兼容协议，所以只写一个客户端，换模型只改 base_url 和 model。

2. **失败一律返回 None，由调用方降级**：
   这是本项目"全依赖可降级"原则的最后一环——模型挂了不能让整个服务挂掉，
   意图解析降级回规则、应答生成降级回模板，用户拿到的是"笨一点但能用"的结果，
   而不是一个 500 错误。

3. **结构化输出做清洗**：模型爱用 ```json 包裹、爱在 JSON 前后加解释，
   这里统一剥掉，解析失败返回 None。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..config import get_settings
from ..core.trace import get_logger, span

logger = get_logger("core.llm")


class LLMClient:
    """OpenAI 兼容的大模型客户端。"""

    def __init__(self, settings: Any | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        """是否启用真模型：关闭 mock 且配了 key 才算启用。"""
        return (not self.settings.llm.use_mock) and bool(self.settings.llm.api_key)

    def _endpoint(self) -> str:
        base = (self.settings.llm.base_url or "https://api.deepseek.com/v1").rstrip("/")
        return f"{base}/chat/completions"

    async def chat(
        self,
        prompt: str,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int = 800,
    ) -> str | None:
        """普通对话，返回文本；失败返回 None。"""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        with span("llm.chat", logger, model=self.settings.llm.model):
            try:
                import httpx
            except ImportError:
                logger.warning("httpx not installed, llm disabled")
                return None

            payload: dict[str, Any] = {
                "model": self.settings.llm.model,
                "messages": messages,
                "temperature": temperature if temperature is not None
                else self.settings.llm.temperature,
                "max_tokens": max_tokens,
            }
            try:
                async with httpx.AsyncClient(timeout=self.settings.llm.timeout) as client:
                    resp = await client.post(
                        self._endpoint(),
                        headers={
                            "Authorization": f"Bearer {self.settings.llm.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except Exception as exc:  # noqa: BLE001 - 模型任何异常都要吞掉并降级
                logger.warning("llm chat failed: %s", type(exc).__name__)
                return None

    async def chat_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float | None = None,
    ) -> Any | None:
        """要求模型输出 JSON，并做清洗后解析；失败返回 None。"""
        raw = await self.chat(prompt, system=system, temperature=temperature,
                              max_tokens=1200)
        if raw is None:
            return None
        return self.parse_json(raw)

    @staticmethod
    def parse_json(raw: str) -> Any | None:
        """从模型输出里抠出 JSON。

        模型常见的三种污染都要处理：
        1. ```json ... ``` 代码块包裹
        2. JSON 前后带解释文字
        3. 尾随逗号
        """
        text = raw.strip()
        # 去掉代码块围栏
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # 截取第一个 { 或 [ 到最后一个 } 或 ]
        start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        try:
            return json.loads(text)
        except Exception:  # noqa: BLE001
            # 尝试修掉尾随逗号再解析一次
            try:
                return json.loads(re.sub(r",\s*([}\]])", r"\1", text))
            except Exception:  # noqa: BLE001
                logger.warning("llm output is not valid json")
                return None


_client: LLMClient | None = None


def get_llm_client(settings: Any | None = None) -> LLMClient:
    """获取 LLM 客户端单例。

    settings 可选：首次调用时可传入自定义配置（如测试里构造的 Settings），
    之后再传会被忽略（单例）。不传则自动读全局配置。
    """
    global _client
    if _client is None:
        _client = LLMClient(settings)
    return _client
