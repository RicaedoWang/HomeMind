# HomeMind · 智能家居多智能体中枢

> 当前状态：**V0.5 已完成**。主链路（听懂 → 规划 → 安全护栏 → 调工具 → 应答）+ 三层记忆系统
> （写入 / 召回排序 / 冲突更新 / 遗忘 / 权限隔离）均已跑通，真大模型（DeepSeek）已接入并带降级，
> 27 个单元测试全绿（3~6s，确定性执行）。
> 配套文档：《HomeMind_软件需求规格说明书_SRS.md》《HomeMind_项目进度总结与下一步规划.md》

---

## 一、30 秒了解这个项目

一个面向智能家居场景的 Agent 服务：用户说一句自然语言，系统识别意图、规划任务、
调用 MCP 工具控制设备或创建日程，最后返回应答——全程带安全护栏和链路追踪。

```
用户输入
   ↓
recall_memory  记忆召回（语义 × 时效 × 重要度）—— 必须在最前，因为要影响后续参数
   ↓
parse_intent   意图识别 + 多意图拆分 + 时间解析
   ↓
safety_gate    安全护栏：高危动作拦截、创建类回显确认、低置信度澄清
   ↓            ├─ 可执行 → execute_tools
   ↓            └─ 待确认 → 挂起，回显给用户
execute_tools  MCP 工具调用（超时/重试/熔断/幂等/降级）
                 ↑ 记忆补全参数："把空调打开" + 记忆里的 26 度 → set_value 26
   ↓
respond        应答生成（含失败兜底话术 + 记忆应用说明）
   ↓
write_memory   记忆写入（把本轮对话里的偏好存下来）

全链路 traceId 贯穿，结构化 JSON 日志
```

### 记忆真的会改变行为（不是花架子）

```
你：我喜欢睡觉时空调26度        → 写入记忆 preference:空调温度
你：把空调打开                  → 用户没提温度
    [应用] 记忆「喜欢睡觉时空调26度」→ 参数改为 set_value 26度
    助手：已将客厅空调设置为26.0度（按你记下的偏好…）
你：我习惯空调开24度            → 同主题归一化 → 覆盖旧偏好（版本 v2）
你：把空调打开                  → 这次设成 24 度，行为跟着记忆变了
```

---

## 二、快速开始

### 2.1 建环境（conda，推荐）

conda 没写进系统 PATH，要用**全路径**调用（或改用 Anaconda Prompt）：

```bash
# 创建项目专属环境
D:\anaconda\Scripts\conda.exe create -n homemind python=3.11 -y

# 装依赖（不激活也能用，直接调环境里的 python）
D:\anaconda\envs\homemind\python.exe -m pip install -r requirements.txt

# 验证
D:\anaconda\envs\homemind\python.exe scripts/demo.py
```

激活后命令可以短一点（Anaconda Prompt 或配好 conda 的终端）：

```bash
conda activate homemind
pip install -r requirements.txt
python scripts/demo.py               # V0.1 主链路演示
python scripts/demo_memory.py        # V0.5 记忆系统能力演示
python scripts/demo_agent_memory.py  # V0.5 记忆接入 Agent（记忆改变行为）
pytest -q
```

### 💬 交互式定制你的家居习惯（推荐先玩这个）

```bash
python scripts/chat_cli.py
```

直接说话就能定制，**记忆持久化到 `data/homemind.db`，关掉再打开还在**：

```
你> 我喜欢睡觉时空调26度      → [记住] 喜欢睡觉时空调26度
你> /habits                   → 列出全部已记住的习惯
你> 把空调打开                → [应用] 记忆 → 自动设 26 度（你没提温度）
你> /role L2                 → 切到儿童视角，家长的高权限记忆就召不到了
你> 忘掉聚会的事              → 主动遗忘
```

内置命令：`/habits` 查看习惯 · `/forget 关键词` 遗忘 · `/devices` 设备状态 ·
`/role L1~L4` 切身份 · `/mode` 看模式 · `/help` 帮助 · `/quit` 退出

### 2.2 两个必踩的坑（已修，记一下原因）

**坑 1 · pip 源配错 → 所有包装不上**

报错长这样：`Could not find a version ... (from versions: none)`

检查：`pip config list`
配置文件在 `C:\Users\<你>\AppData\Roaming\pip\pip.ini`

```ini
[global]
index-url = https://pypi.tuna.tsinghua.edu.cn/simple   ← 正确
```

两个常见错误写法（都不通）：
- `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`（路径错误）
- `https://mirrors.tuna.tsinghua.edu.cn/simple`（域名不通）

**坑 2 · requirements.txt 里写中文注释 → pip 解析失败**

Windows 下 pip 用系统 locale（GBK）读 requirements 文件，
UTF-8 中文注释会让它把包名解析坏，报同样的 `from versions: none`。
所以本项目的 `requirements.txt` **刻意保持纯 ASCII**，注释用英文——别改成中文。

### 2.4 接真大模型（可选，三步）

默认跑的是**规则引擎**（正则+关键词），不联网、不花钱。
想让 Agent 真正"聪明"，接一个真模型：

```bash
# 1. 在项目根目录的 .env 里填 Key，并把 mock 关掉
HM_LLM_API_KEY=你的key
HM_USE_MOCK_LLM=false

# 2. 自检（确认 Key 有效、接口通、JSON 输出能被解析）
python scripts/check_llm.py

# 3. 跑真模型版链路
python scripts/demo_agent_memory.py
```

支持任何 OpenAI 兼容接口（DeepSeek / 通义 / OpenAI / 本地 vLLM），
换模型只改 `HM_LLM_BASE_URL` 和 `HM_LLM_MODEL`。

**模型挂了会怎样？** 自动降级——意图解析回退规则、应答生成回退模板。
用户拿到的是"笨一点但能用"的结果，不是一个 500 错误，服务永远在线。

**模型在链路里干什么？** 两件事：意图解析、应答润色。
注意应答**只做表达层润色**，执行结果是确定事实由工具给出，
不许模型自由发挥——否则它会编出"已经帮你关了窗"这种鬼话（幻觉）。

### 2.5 VSCode + Code Runner

用 VSCode **打开 `homemind` 目录本身**（不是上层目录），
`.vscode/settings.json` 已配好解释器与 Code Runner：

```json
"python.defaultInterpreterPath": "D:\\anaconda\\envs\\homemind\\python.exe",
"code-runner.executorMap": { "python": "D:\\anaconda\\envs\\homemind\\python.exe -u" }
```

注意：**Code Runner 无视 `defaultInterpreterPath`**，必须单独配 `executorMap`，
否则按 ▶ 会用 PATH 里的裸 python，直接报 `ModuleNotFoundError`。
改完 `Ctrl+Shift+P → Reload Window` 才生效。

启动 API 服务：

```bash
uvicorn homemind.api.main:app --reload --port 8000
# 打开 http://localhost:8000/docs 看接口文档
```

Docker 全套（含 Redis）：

```bash
docker compose up -d          # 最小：只起 redis + api
docker compose --profile full up -d   # 完整：再加 mysql + milvus（V0.5 记忆系统用）
```

---

## 三、目录结构

```
homemind/
├── src/homemind/
│   ├── config.py              全局配置（全环境变量驱动，改配置不改代码）
│   ├── core/
│   │   ├── trace.py           traceId + 结构化 JSON 日志 + 耗时 span
│   │   └── models.py          Intent / Task / ToolResult / DeviceState 数据模型
│   ├── agent/
│   │   ├── state.py           AgentState（LangGraph 状态定义）
│   │   ├── intent.py          意图识别：规则模式 + 大模型模式双模
│   │   ├── time_parser.py     时间语义解析（"明天早上九点" → ISO 时间戳）
│   │   └── graph.py           LangGraph 状态图：四个节点 + 条件边
│   ├── tools/
│   │   ├── registry.py        工具注册中心：Schema + 超时/重试/熔断/幂等
│   │   ├── device_server.py   设备控制 MCP Server（Mock 设备层）
│   │   └── calendar_server.py 日程提醒 MCP Server
│   ├── memory/
│   │   └── short_term.py      短期记忆（Redis，可降级内存）
│   └── api/main.py            FastAPI 接口
├── scripts/demo.py            命令行链路演示
├── scripts/smoke_api.py       API 冒烟测试
├── tests/test_pipeline.py     回归测试（12 用例）
├── sql/init.sql               数据库 DDL（V0.5 记忆系统预留）
├── docker-compose.yml         全套服务编排
└── Dockerfile                 应用镜像
```

---

## 四、核心设计亮点

### 1. 工具调用治理内建在注册中心

超时、重试、熔断、幂等、降级全部集中在 `tools/registry.py`，
业务 handler 只管干活，不用每个工具重复写重试逻辑。

```python
result = await registry.call("create_reminder", args, idempotency_key=key)
# 返回的一定是 ToolResult，绝不让异常穿透到用户
```

### 2. 安全护栏是独立节点，不是散落的 if

`safety_gate` 是图里的一等公民节点，保证"误操作率 = 0"这条验收标准
**有唯一的实现位置可审计**，而不是在各处 if 里碰运气。

### 3. 时间解析用规则而非全交给大模型

大模型解析"下周三下午三点"的错误率足以让用户抓狂。
所以采用 **规则为主 + 大模型兜底 + 解析不出就强制澄清**，绝不静默猜测。

### 4. 一切都可降级

| 依赖 | 降级方案 |
|---|---|
| Redis 不可用 | 自动切进程内存 |
| 大模型不可用 | 自动回退规则解析 |
| 工具调用失败 | 重试 → 熔断 → 用户可理解的兜底话术 |
| 时间解析失败 | 强制澄清，不猜测 |

### 5. 记忆召回不是简单 topK，而是三项加权

```
score = 0.6 × 语义相似度 + 0.2 × 时效衰减 + 0.2 × 重要度
time_decay = exp(-λ × Δt)      λ 默认 0.01/天
```

纯语义相似会把"三年前随口一提"和"昨天刚说"排在一起；
时效衰减让新鲜记忆优先，重要度让关键偏好不被埋没。
每条召回结果都返回三项分解值，分数**可解释、可调试**。

### 6. 权限在召回阶段就过滤

不是召回完再过滤——那样敏感内容已经进了模型上下文，等于泄露。
`visible_role` 在语义检索时就拦截（L4 管理员 / L3 成人 / L2 儿童 / L1 访客）。

### 7. 健康检查说实话

`/health` 的 `memory_mode` 是**真探测**的结果，不是复述配置：
```json
{"status":"ok","memory_mode":"memory(redis_unavailable:TimeoutError)"}
```

---

## 五、已实现 vs 路线图

| 版本 | 内容 | 状态 |
|---|---|---|
| **V0.1** | 意图识别、多意图拆分、时间解析、MCP 工具注册中心、安全护栏、链路追踪、短期记忆、FastAPI | ✅ 已完成 |
| **V0.5** | 记忆服务核心 + 接入 Agent 图：写入/召回排序/冲突更新/遗忘/权限隔离/记忆影响参数 | ✅ 已完成 |
| **V0.8** | Supervisor 多 Agent 协同、子 Agent 工具白名单、执行前冲突检测、RAG 知识库（本地检索版） | ✅ 已完成 |
| V0.9 | Milvus 真向量、视觉分析、模型分级路由、多租户隔离 | 规划中 |
| V1.0 | 评测体系、灰度发布、异常回退、指标监控 | 规划中 |

### V0.5 记忆系统现状

| 能力 | 实现 | 存储 |
|---|---|---|
| 短期记忆 | 会话上下文，滑动窗口 + TTL | Redis（降级内存） |
| 长期结构化记忆 | 偏好 / 成员画像 / 设备习惯 / 场景 | SQLite（表结构对齐 MySQL，切后端只改配置） |
| 语义记忆 | 向量化 + 相似度召回 | 内存（无 embedding 时用 bigram Jaccard 降级） |
| 召回排序 | 语义 × 时效 × 重要度 加权 | — |
| 冲突更新 | 同 key 版本 +1 + 变更留痕 | memory_history 表 |
| 遗忘 | 主动硬删除 / 自然衰减清理 | — |
| 权限 | 四级角色，召回阶段过滤 | visible_role 字段 |

演示：`python scripts/demo_memory.py`，会跑完写入 → 召回 → 冲突更新 → 权限隔离 → 遗忘 → 衰减六段。

---

## 五之二、V0.8 多 Agent 协同

```
START → recall_memory → parse_intent → supervisor → safety_gate
          ─┬─(有分派)→ execute_agents → respond → write_memory → END
           └─(无分派)→ respond → write_memory → END
```

三个子 Agent，各管一摊：

| Agent | 领域 | 工具白名单 |
|---|---|---|
| `device_agent` | 设备控制 | device_control / device_list / lock_control |
| `schedule_agent` | 日程提醒 | create_reminder / list_schedule |
| `knowledge_agent` | 知识问答 | 无（只回答，不碰设备） |

**四条设计原则（面试重点）**：

1. **Supervisor 只做决策，不碰工具。** 它干三件事：分派、冲突检测、并发编排。
   工具执行仍然统一在 `safety_gate` 之后——**多 Agent 的引入没有削弱安全**，
   护栏依旧只有唯一一处实现。
2. **最小权限。** 每个子 Agent 只能调用白名单内的工具。即使 Supervisor 分派错了，
   日程 Agent 也碰不到门锁。护栏挡"该不该做"，白名单挡"有没有资格做"。
3. **冲突在执行前拦。** "把客厅灯打开再关掉"两个操作单看都合法，合起来自相矛盾，
   执行了就是误操作。所以 `detect_conflicts()` 在 supervisor 阶段就把冲突任务挂起，
   转安全护栏要用户确认。
4. **失败隔离。** 子 Agent 的 `run()` 永不向外抛异常，崩了就转成 failed outcome，
   其他 Agent 照常返回结果，主链路不崩。

**治理逻辑只有一份**：主链路和子 Agent 共用 `agent/execution.py` 的执行原语
（增量解析、记忆补全、幂等键），避免出现"主链路有幂等、子 Agent 没有"这种裂缝。

演示：`python scripts/demo_multi_agent.py`，六段——单领域分派 / 多领域并发 /
知识问答 / 越权拦截 / 冲突检测 / 失败隔离。

---

## 五之三、评测体系（回答"你怎么保证改一个不坏一片"）

```bash
python scripts/run_eval.py                 # 跑全量（默认 mock，确定性、不烧 token）
python scripts/run_eval.py --save-baseline # 存为基线
python scripts/run_eval.py --real          # 真模型评测（结果不稳定，别当回归基线）
```

当前成绩（mock 模式，49 条用例）：**通过率 100%，安全红线 8/8 零误操作**。

| 指标 | 成绩 |
|---|---|
| 意图识别准确率 | 46/47 |
| 工具选择准确率 | 42/42 |
| Agent 分派准确率 | 6/6 |
| **安全红线** | **8/8（一票否决）** |

**四条设计原则**：

1. **红线一票否决。** 涉及误操作的用例任何一条失败，本次评测即判不合格，
   进程退出码非 0——可以直接挂 CI 卡住合入。安全不用平均分衡量，
   误操作率必须收敛到 0，不能是 0.98。
2. **可复现。** 每条用例跑之前重置 Mock 设备与日程。Mock 层是模块级全局字典，
   不重置的话前一条关了灯，后一条查到的就是"关"，结果根本没法复现。
3. **环境隔离。** 评测用独立的 `data/eval.db`，绝不碰用户的真实记忆库。
4. **区分已知缺口。** 隐式偏好（"我怕热"这类没有明确习惯句式的表达）当前抽不出来，
   标为 `known_gap` 单列：既不让它污染通过率，也不让它悄悄消失——
   **红线用例禁止标 known_gap**（代码里直接抛错，安全没有下不为例）。

**基线对比**能指名道姓说出哪条从通过变成了失败。实测：把
`has_device and has_action and not has_knowledge` 改回 `has_device and has_action`
（疑问词不再压过设备动作），评测立刻报出 `sec-08「空调怎么打开」` 红线失败、
退出码变 1——这正是"改一行崩一片"被拦下的样子。

---

## 六、关键踩坑记录

**LangGraph 不能用 lambda 包 async 节点。**
```python
# ❌ 错误：LangGraph 用 inspect.iscoroutinefunction 判断，lambda 会被当成同步节点
graph.add_node("parse", lambda s: parse_intent_node(s, parser))
# 报错：InvalidUpdateError: Expected dict, got <coroutine object>

# ✅ 正确：用 async def 闭包
async def _parse(state): return await parse_intent_node(state, parser)
graph.add_node("parse", _parse)
```

**门锁必须走专门的高危工具。**
如果把门锁当普通设备开关，安全护栏里的 `HIGH_RISK_TOOLS` 就永远命中不了——
护栏形同虚设。这个 bug 是靠实际跑一遍才暴露的。

**多意图不能因为一个任务待确认就全卡住。**
"关灯 + 建日程"里，关灯应该马上做，日程才需要确认。

---

## 七、面试怎么讲

> 我用 LangGraph 搭了一个智能家居 Agent 中枢，链路是
> 意图识别 → 安全护栏 → 工具执行 → 应答生成四个节点。
> 工具层用 MCP 协议标准化，注册中心内建了超时、重试、熔断和幂等治理。
> 安全护栏做成了独立节点，高危动作和创建类操作强制二次确认，保证误操作率为零。
> 时间解析我用规则为主、大模型兜底、解析不出来就强制澄清——
> 因为时间解析错误是这类产品最容易被投诉的点。
> 全链路 traceId 贯穿，依赖全部可降级：Redis 挂了走内存，模型挂了走规则。

**能被追问的点（都已在代码里落地）：**
1. 熔断怎么实现的？（连续失败 N 次开闸，30 秒后半开试探）
2. 幂等怎么保证？（写操作生成幂等键，命中缓存直接返回上次结果）
3. 增量调节"调低两度"怎么处理？（先 query 当前值，再算目标值 set_value）
4. 部分执行怎么做的？（安全网关按任务粒度判定，可执行的先跑）
