"""本地 embedding 接入（替代 Milvus 那个位置的真正方案）。

## 定位

`semantic.py` 早就留好了钩子：`SemanticMemory(embed_fn=...)`，传一个
`text -> list[float]` 的函数进去，语义召回立刻从「字符 bigram Jaccard」升级成
「真向量 + 余弦相似度」。这个文件就是提供那个函数的实现。

**不接 Milvus 的理由**：向量库解决的是「百万级向量的近似最近邻 + 高并发」。
本项目 50 个家庭 × 3 成员 × 30 条记忆 ≈ 4,500 条向量，实测 numpy 暴力余弦
**0.13 ms**（384 维），比一次网络往返还快。上 Milvus 要额外跑
etcd + minio + milvus 三个容器、吃 4G+ 内存，换来的是负收益。

**也不用 sentence-transformers**：它会拖进 2GB 的 PyTorch。这个项目是
「不装任何东西也能跑完整链路」的设计，为了几百条记忆的相似度计算装一个深度学习
框架，是把开发体验和部署体积都卖了。用 ONNX Runtime 跑同一批模型，依赖小一个数量级。

## 三条硬规则

1. **懒加载**：第一次真正要算向量时才加载模型。模块 import 时不碰模型——
   否则 `import homemind` 就会卡几秒，测试和 CLI 全受影响。
2. **失败一次就封口**：加载失败、推理失败都记 `_failed = True`，
   之后直接返回 None 让上层降级，**不再重试**。否则每条记忆都要等一次超时。
3. **返回 None 是合法值**：`SemanticMemory` 已经把 `embed_fn` 抛异常和返回
   `None` 都处理成「这条走文本相似度」。这里不抛异常，只安静降级。

## 降级后果

拿不到向量时，召回退化成字符 bigram Jaccard——它只认字面重合，
「空调太冷」和「把温度调高点」一个字都不重合，就召不回来。
这是**质量下降但功能可用**，不是故障。
"""

from __future__ import annotations

import os
import threading

from ..core.trace import get_logger

logger = get_logger("memory.embedder")

# bge-small-zh-v1.5：384 维，中文短文本检索效果好，ONNX 量化版约 100MB。
# 换模型只改这个默认值或环境变量，上层代码一行不用动。
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class LocalEmbedder:
    """本地 ONNX embedding。可调用对象：text -> list[float] | None。

    设计成可调用对象而不是普通函数，是为了带上「加载/失败」状态——
    函数闭包塞不下这些还能被读到的状态。
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or os.getenv("HM_EMBEDDING_MODEL", DEFAULT_MODEL)
        self._model = None
        self._failed = False
        self._lock = threading.Lock()
        self._load_attempts = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def is_ready(self) -> bool:
        """模型是否已加载成功（不等于「可以调用」，调用会触发懒加载）。"""
        return self._model is not None

    @property
    def has_failed(self) -> bool:
        """是否已经确定用不了（装不上 / 下载失败 / 推理报错）。"""
        return self._failed

    def _ensure_model(self) -> bool:
        """懒加载模型。返回是否可用。

        用锁保护：FastAPI 是多线程的，两个请求同时触发加载会各建一份模型，
        白吃一份内存。`_load_attempts` 也顺便记一下，排查时有用。
        """
        if self._model is not None:
            return True
        if self._failed:
            return False

        with self._lock:
            # 双检：等锁期间可能已经被别的线程加载好了
            if self._model is not None:
                return True
            if self._failed:
                return False

            self._load_attempts += 1
            try:
                from fastembed import TextEmbedding  # 局部导入：没装包也要能 import 本模块
            except ImportError:
                self._failed = True
                logger.warning(
                    "fastembed not installed, semantic memory falls back to "
                    "character bigram Jaccard (pip install fastembed 可启用真向量)"
                )
                return False

            try:
                # 首次会下载模型到缓存目录；国内网络可能需要
                # HF_ENDPOINT=https://hf-mirror.com，见 .env.example
                self._model = TextEmbedding(model_name=self._model_name)
                logger.info("local embedding ready: %s", self._model_name)
                return True
            except Exception as exc:  # noqa: BLE001 - 模型加载失败不能让服务起不来
                self._failed = True
                logger.warning(
                    "failed to load embedding model %s (%s: %s), "
                    "semantic memory falls back to Jaccard",
                    self._model_name, type(exc).__name__, exc,
                )
                return False

    def __call__(self, text: str) -> list[float] | None:
        """把文本转成向量。失败返回 None，由上层降级。"""
        if not text or not text.strip():
            return None
        if not self._ensure_model():
            return None

        try:
            # fastembed 的 embed() 返回生成器，每个元素是 np.ndarray
            vectors = list(self._model.embed([text]))
            if not vectors:
                return None
            # 转成 list[float]：上层 cosine_similarity 用的是纯 Python 数学，
            # 塞一个 np.ndarray 进去也能算，但类型混用会在序列化时出问题。
            return [float(x) for x in vectors[0]]
        except Exception as exc:  # noqa: BLE001
            logger.warning("embedding inference failed (%s), fall back for this item",
                           type(exc).__name__)
            # 注意：这里**不设** _failed。单条推理失败（比如超长文本）不该
            # 把整个模型判死刑，下一条正常文本还能用。只有加载失败才封口。
            return None


_default_embedder: LocalEmbedder | None = None


def get_embedder() -> LocalEmbedder:
    """取全局单例。

    单例的理由很实在：一个模型实例占几百 MB 内存，每个 SemanticMemory 各建一个
    会翻几倍。而且 fastembed 的模型加载要读盘、初始化 ONNX session，
    重复建实例会让每次请求都慢一截。
    """
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = LocalEmbedder()
    return _default_embedder


def set_embedder(embedder: LocalEmbedder | None) -> None:
    """测试接缝：换掉单例（与 set_device_store / set_memory_service 同套路）。

    测试里必须换成假的，否则会去加载真模型——单测就可能卡几十秒甚至因
    没网而失败。**测试不许依赖外部资源**是这个项目的硬规矩。
    """
    global _default_embedder
    _default_embedder = embedder
