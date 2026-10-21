"""预先下载本地 embedding 模型。

## 为什么需要这个脚本

`memory/embedder.py` 是**懒加载**的：第一次真要算向量时才去加载模型。
如果模型还没下载过，那一次会同步等下载完成——实测在没设镜像的情况下
fastembed 会重试 3 次，**总共卡 70 秒**，然后放弃降级。

这 70 秒会落在**第一个真实用户的请求**上。把它挪到部署阶段做掉。

## 用法

```bat
:: 国内网络强烈建议先设镜像，否则大概率超时
set HF_ENDPOINT=https://hf-mirror.com
set HF_HUB_DISABLE_SYMLINKS=1

python scripts/download_embedding.py
```

模型约 90MB（BAAI/bge-small-zh-v1.5，512 维），下载后缓存在本机，
之后启动和推理都不再联网。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    model = os.getenv("HM_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    endpoint = os.getenv("HF_ENDPOINT")

    print("=" * 72)
    print("HomeMind · 预下载本地 embedding 模型")
    print("=" * 72)
    print(f"  模型      : {model}")
    print(f"  HF 镜像   : {endpoint or '（未设置，走官方源，国内大概率超时）'}")
    if not endpoint:
        print("  提示      : 建议先执行  set HF_ENDPOINT=https://hf-mirror.com")
    print()

    try:
        from fastembed import TextEmbedding
    except ImportError:
        print("❌ fastembed 没装。先执行：")
        print("   pip install fastembed")
        return 1

    started = time.perf_counter()
    try:
        model_obj = TextEmbedding(model_name=model)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 加载/下载失败（{type(exc).__name__}: {exc}）")
        print()
        print("常见原因与对策：")
        print("  1. 网络到 HuggingFace 超时 → 设 HF_ENDPOINT=https://hf-mirror.com")
        print("  2. Windows 无开发者模式导致软链接失败 → 设 HF_HUB_DISABLE_SYMLINKS=1")
        print("  3. 磁盘空间不足（需要约 100MB）")
        print()
        print("不下载也能跑：语义召回会自动降级为字符 bigram Jaccard。")
        return 1

    elapsed = time.perf_counter() - started
    print(f"✅ 模型就绪，耗时 {elapsed:.1f}s")

    # 真跑一次，确认模型真的能用（下载成功不等于推理能跑通）
    try:
        vec = list(model_obj.embed(["把客厅灯打开"]))[0]
        print(f"✅ 推理自检通过：维度 {len(vec)}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 模型加载了但推理失败（{type(exc).__name__}: {exc}）")
        return 1

    print()
    print("之后 `memory/embedder.py` 会直接命中本地缓存，启动和推理都不再联网。")
    print("关闭它：在 .env 里设 HM_EMBEDDING_ENABLED=false（语义召回退回字面匹配）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
