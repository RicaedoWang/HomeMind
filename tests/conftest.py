"""测试全局配置。

必须在任何 homemind 模块导入之前设置环境变量：
config.py 的 load_dotenv(override=False) 不会覆盖已存在的环境变量，
所以在这里先设 mock=true，用户 .env 里的真模型配置就不会污染测试。

为什么测试必须走 mock：
1. **确定性**——LLM 每次返回都可能不同，回归测试要的是"同样输入必然同样结果"。
2. **不花钱**——每轮回归 22 个用例，走真模型就是在烧 token。
3. **不联网**——CI / 离线环境也能跑。

真模型的行为验证靠 scripts/check_llm.py 和 demo 脚本，不靠单测。
"""

import os

os.environ["HM_USE_MOCK_LLM"] = "true"
os.environ.setdefault("HM_REDIS_ENABLED", "false")
os.environ.setdefault("HM_LOG_LEVEL", "ERROR")
