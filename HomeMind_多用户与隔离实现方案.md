# HomeMind 多用户 / 登录验证 / 数据隔离 · 实现方案（待确认）

> 编写时间：2026-10-14 ｜ 状态：**待确认，尚未动手**
> 目标：深圳家（我 / 妈妈 / 爸爸）与青岛家（小明）**共用一套代码、数据完全隔离**；
> 补齐登录验证、操作审计与并发一致性四件套。
> 所有"为什么这么做"都写在对应小节，不是只列"做什么"。

---

## 0. 一句话目标与最终验收

同一个服务，四个账号登录进去看到**两个完全不同的家**；任何账号都拿不到别人家的数据；
每一次设备操作都能查到"谁、什么时候、从哪个入口、把什么从什么改成了什么"。

| 验收口径 | 判据 |
|---|---|
| 登录 | 无 token 调 `/api/console/state` → 401；错误密码 → 401；过期 token → 401 |
| 隔离 | 小明（青岛）看不到深圳家任何设备 / 任务 / 记忆 |
| 越权 | 小明拿着深圳家的 `device_id` 直接调控制接口 → **403**，且设备状态不变 |
| 审计 | 点一次开关 → `operation_log` 落一条含 before/after 的记录，"我的"页可见 |
| 一致性 | 同一条指令并发 10 次只落一次；场景执行中途失败自动回滚，不留半截状态 |

---

## 1. 概念模型：租户 = 家庭

```
家庭 tenant（数据隔离的最小单位）
├── family_sz  深圳家 · 城市 深圳
│   ├── 我     member_001  L4 owner   （管理员）
│   ├── 妈妈   member_002  L3 admin   （共同管理）
│   └── 爸爸   member_003  L3 member  （普通成员）
└── family_qd  青岛家 · 城市 青岛
    └── 小明   member_101  L4 owner
```

**为什么把"家庭"而不是"用户"当租户**：智能家居的数据天然是**家庭共享**的——
妈妈记的"空调 26 度"要能作用到爸爸说的"把空调打开"。按用户隔离会把一家人拆成三个孤岛，
那就不叫家了。真正的隔离边界在**家庭与家庭之间**（你和邻居），不在家庭成员之间。

家庭成员内部的差异用**权限分级**（L1 访客 → L4 管理员）解决，这一层项目里已经实现
（`role_level` + 召回阶段过滤），本方案沿用不动。

---

## 2. 登录验证方案

### 2.1 选型对比

| 方案 | 判断 | 说明 |
|---|---|---|
| 短信验证码 / OAuth 第三方 | ✗ | 演示项目引外部依赖和费用，收益为零 |
| 服务端 Session | △ | 简单，但要存 Session 存储，多副本要共享存储 |
| **JWT（HS256）+ Bearer Token** | ✓ | 无状态、易扩展、前端好存；`family_id/member_id/role` 直接装进 payload，业务层不用回查库 |

### 2.2 具体设计

| 项 | 设计 | 理由 |
|---|---|---|
| 依赖 | `PyJWT`（新增 1 个） | JWT 签名别自己手写密码学；多一个依赖可以接受 |
| 密码哈希 | `hashlib.pbkdf2_hmac('sha256', 200_000 轮)`，存 `iterations$salt$hash` | **零新依赖**（标准库），且明确不是明文。生产应换 bcrypt/argon2，方案里如实注明 |
| Token 载荷 | `family_id / member_id / role / name / city / exp` | 业务层要的上下文一次带齐，不用回查 |
| Token 位置 | `Authorization: Bearer <token>` | 与现有 REST 接口风格一致 |
| 密钥 | `.env` 里的 `HM_AUTH_SECRET`，**不进仓库**；缺省时开发环境自动生成并告警 | 延续"密钥不入库"这条已有红线 |
| 有效期 | 默认 8 小时（`HM_AUTH_TTL_HOURS` 可配） | 家庭控制台，别一天一登 |

### 2.3 三个接口

| 接口 | 作用 |
|---|---|
| `POST /api/auth/login` | `{username, password}` → `{token, user:{...}, family:{...}}` |
| `GET  /api/auth/me` | 返回当前身份（前端刷新后恢复登录态） |
| `POST /api/auth/logout` | 服务端记一次登出审计，前端清 token |

**关键约束：`member_id` 不再由请求体传入。** 现在 `/api/v1/chat`、`/api/console/*`
都接受调用方自己填 `member_id`——这等于"谁都能自称是爸爸"。改造后一律从 token 取，
请求体里传了也忽略。这是本方案里**最实质的一次安全加固**。

---

## 3. 数据隔离：三层键 `family → member → role`

### 3.1 现状盘点（诚实版）

| 层 | 现状 | 本方案动作 |
|---|---|---|
| 记忆库 `memory_struct` | 有 `member_id`，`UNIQUE(member_id, memory_key)` | **加 `family_id`**，唯一键改三元组 |
| MySQL DDL `sql/init.sql` | **缺 `UNIQUE(member_id, memory_key)`**（隐患：换 MySQL 会变重复行） | 补齐唯一约束 + 加 `family_id` |
| 短期记忆 | key 是 `hm:session:{session_id}`，**不含家庭/成员** | 改为 `hm:{family_id}:{member_id}:{session_id}` |
| 设备层 | 全局 `_DEVICES` 字典，**不分家庭** | 改 `Dict[family_id, Dict[device_id, DeviceState]]` |
| 越权校验 | 无 | 所有按 id 取资源的接口加 `family_id` 断言 → 403 |

### 3.2 SQLite 迁移（注意：不能原地改唯一约束）

SQLite 不支持 `ALTER TABLE` 改唯一约束，必须**建新表 → 搬数据 → 改名**：

```sql
CREATE TABLE memory_struct_v2 (
    memory_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id     TEXT    NOT NULL DEFAULT 'family_sz',
    member_id     TEXT    NOT NULL,
    memory_type   TEXT    NOT NULL,
    memory_key    TEXT    NOT NULL,
    memory_value  TEXT    NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    importance    REAL    NOT NULL DEFAULT 0.50,
    visible_role  TEXT    NOT NULL DEFAULT 'L3',
    created_at    TEXT, updated_at TEXT, last_access_at TEXT,
    UNIQUE(family_id, member_id, memory_key)      -- ← 关键：三元组
);
INSERT INTO memory_struct_v2
  SELECT memory_id,'family_sz',member_id,memory_type,memory_key,memory_value,
         version,importance,visible_role,created_at,updated_at,last_access_at
  FROM memory_struct;
DROP TABLE memory_struct;
ALTER TABLE memory_struct_v2 RENAME TO memory_struct;
```

> ⚠️ 执行前**先备份 `data/homemind.db`**（那是你真实的记忆库，不是演示数据）。

### 3.3 新增表

```sql
-- 账号（登录用，与"家庭成员"分离：一个成员可以没有登录账号，比如小孩）
CREATE TABLE user_account (
    user_id       TEXT PRIMARY KEY,
    family_id     TEXT NOT NULL,
    member_id     TEXT NOT NULL,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,          -- PBKDF2: iterations$salt$hash
    role          TEXT NOT NULL DEFAULT 'L3',
    city          TEXT,                   -- 演示用的城市（见 §4 诚实说明）
    created_at    TEXT
);
```

---

## 4. 两个家庭的种子数据（深圳 vs 青岛）

| 维度 | 深圳家 `family_sz` | 青岛家 `family_qd` |
|---|---|---|
| 成员 | 我(L4) / 妈妈(L3) / 爸爸(L3) | 小明(L4) |
| 城市 | 深圳 | 青岛 |
| 设备 | 客厅灯 / 客厅空调 / 客厅窗帘 / 主卧灯 / 大门门锁 / 洗衣机 / 智能晾衣机 / 扫地机器人 / 加湿器 / 智能浇花器 | 客厅灯 / 客厅地暖 / 新风系统 / 厨房油烟机 / 阳台晾衣机 / 入户门锁 |
| 室内环境 | 24.5 °C · 湿度 62% · 室外晴 25°C · AQI 43 优 | 18.2 °C · 湿度 45% · 室外海雾 12°C · AQI 72 良 |
| 今日任务 | 给阳台绿萝浇花（逾期）/ 晾晒衣服 / 给猫加粮 / 确认门锁反锁 | 更换新风滤芯 / 地暖除霜提醒 / 收阳台衣服 |
| 记忆示例 | `preference:空调温度 = 26度` | `preference:地暖温度 = 20度` |
| 场景 | 回家 / 离家 / 观影 / 睡眠 / 起床 | 回家 / 离家 / 睡眠 / 通风（海边湿度大，场景不同） |

四个演示账号（登录页做成一键填充，方便你当场演示）：

| 用户名 | 家庭 | 角色 | 城市 |
|---|---|---|---|
| `wo` | 深圳家 | L4 管理员 | 深圳 |
| `mama` | 深圳家 | L3 共同管理 | 深圳 |
| `baba` | 深圳家 | L3 成员 | 深圳 |
| `xiaoming` | 青岛家 | L4 管理员 | 青岛 |

> **诚实说明**：登录时服务端会记录**真实的客户端 IP** 到操作日志，
> 但"深圳 / 青岛"这个城市字段来自**账号档案的 seed 数据**，**不做真实 GeoIP 定位**
> （引 GeoIP 库对演示无意义）。面试时照这个口径说，别吹成"按 IP 自动识别家庭"。

---

## 5. 多端并发操作：怎么记录（`operation_log`）

### 5.1 表结构

| 字段 | 说明 |
|---|---|
| `family_id / member_id` | 谁家的谁 |
| `source` | `web` / `ai` / `schedule` / `automation` —— **区分是人点的还是 AI 干的** |
| `tool_name / target_type / target_id` | `device_control` / `device` / `living_room_light` |
| `action` | `turn_on` / `turn_off` / `set_value` |
| `before_state / after_state` | JSON 快照 —— **能回答"灯原来是什么状态"** |
| `trace_id` | 与全链路日志对得上（已有 traceId 基础设施） |
| `idempotency_key` | 与幂等缓存对得上 |
| `ok / error / created_at` | 成功与否、失败原因、时间 |

### 5.2 写入位置：**只有一处**

写入统一放在工具注册中心的 `call()` 里（对 `mutating=True` 的工具生效），
**不允许各接口自己写日志**——沿用项目已有的铁律"治理逻辑只有一处实现"。
散落在各处的日志最后一定会漏，漏的地方就是你被问住的地方。

### 5.3 查询与展示

- `GET /api/console/operations?limit=20` → "我的"页新增**操作日志**卡片。
- 能当场回答："昨晚 21:07 客厅灯是被谁关的？"——这是**现在答不上来**的问题。

---

## 6. 一致性四件套

| 问题 | 现在 | 方案 |
|---|---|---|
| 重复请求 | 幂等缓存是**进程内字典**，多副本即失效 | 抽成 `IdempotencyStore` 接口：Redis 实现（`SETNX` + TTL）+ 内存实现降级。单进程下行为不变 |
| 设备并发写 | 内存字典裸赋值，**最后写入者赢** | 按 `device_id` 粒度 `asyncio.Lock` 串行化（单进程足够，**不上分布式锁那是过度设计**） |
| 场景半截状态 | 无事务、无补偿：中间失败就"灯关了窗帘没关" | dry-run 出计划 → 逐项执行 → 任一项失败则对**已成功项执行反向补偿** → 如实返回 partial 状态 |
| 前端一致性 | 每次操作后拉全量 state，天然收敛 | 保留（够用）；上规模再换 SSE 推增量 |

---

## 7. 实施步骤与验收（S1–S6）

| 步骤 | 内容 | 预估 | 验收标准 |
|---|---|---|---|
| **S1** | 认证：PyJWT + `AuthConfig` + `user_account` 表 + 4 个种子账号 + 3 个接口 + 依赖注入 | 半天 | 无/错/过期 token 均 401；`test_auth.py` ≥ 6 条 |
| **S2** | 租户隔离：`family_id` 贯穿 + 迁移 SQL + 设备分桶 + 越权 403 | 半天–1 天 | 小明看不到深圳设备；拿别家 `device_id` 控制 → 403 且状态不变；**两家可各存一条"空调温度"** |
| **S3** | 双家庭种子数据（深圳 / 青岛） | 半天 | 登小明 → 青岛家环境与设备；登我 → 深圳家 |
| **S4** | `operation_log` 审计 | 半天 | 点一次开关落一条带 before/after 的记录，"我的"页可见 |
| **S5** | 一致性：幂等 Redis 化 / 设备锁 / 场景补偿 | 1 天 | 并发 10 次只落一次；造一台失败设备 → 场景自动回滚 |
| **S6** | 前端：登录页 + 家庭信息展示 + 操作日志页 | 半天 | 四个账号一键切换演示 |

**总计约 3 天。** 顺序不能乱：S1 是 S2 的前提（没有身份就没有"越权"这个概念），
S2 是 S3 的前提（没有租户键就没地方挂两个家的数据）。

---

## 8. 风险与诚实边界

| 风险 | 应对 |
|---|---|
| 迁移会动**真实记忆库** `data/homemind.db` | 执行前备份；迁移脚本先 dry-run 打印影响行数 |
| 加鉴权后现有 58 个测试会大面积 401 | 测试侧提供**固定 token 依赖覆盖**（`app.dependency_overrides`），保持"测试确定性、不联网、不烧钱"这条原则不被破坏 |
| 密码哈希用 PBKDF2 而非 bcrypt | 演示够用；文档与注释里注明**生产应换 bcrypt / argon2** |
| 多副本一致性 | **本方案不做**：单进程内存态足够。多副本才需要 Redis 幂等 + 分布式锁，这条边界要主动说清 |

**明确不做**：真实短信 / OAuth 第三方登录、真实 GeoIP 定位、分布式锁、多副本部署。
这些不是"以后再说"，是**当前阶段不划算**，面试时主动点出边界反而加分。

---

## 9. 实施记录

### S1 · 登录验证 ✅（2026-10-14 完成）

**交付**：`src/homemind/auth/`（passwords / tokens / store / deps）+ `src/homemind/api/auth.py`
+ 控制台接口全部受保护 + 前端登录门禁。依赖新增 `PyJWT>=2.8.0`。

**测试用例（`tests/test_auth.py`，11 条全绿）**

| # | 用例 | 验证的契约 |
|---|---|---|
| 1 | `test_login_returns_family_scoped_identity` | token 里真带着 family_id / member_id / role / city |
| 2 | `test_qingdao_family_is_a_different_tenant` | 小明 → `family_qd` / 青岛 |
| 3 | `test_wrong_password_rejected` | 错误密码 → 401 |
| 4 | `test_unknown_user_same_error_message` | 账号不存在与密码错误**同一句话**（防账号枚举） |
| 5 | `test_protected_route_requires_token` | 无 token 访问控制台 → 401 |
| 6 | `test_tampered_token_rejected` | 改签名 → 401 且提示一致 |
| 7 | `test_expired_token_rejected` | 过期 token → 401 |
| 8 | `test_password_not_stored_in_plaintext` | 库里无明文但仍可校验 |
| 9 | **`test_identity_comes_from_token_not_body`** | **请求体伪造 `member_id=member_001`，完成人仍是登录者 `member_003`** |
| 10 | `test_rate_limit_after_repeated_failures` | 连错 5 次后正确密码也被限流 |
| 11 | `test_logout_and_me` | /me 回显身份、/logout 返回 ok |

**实测（起服务 curl，非单测）**：无 token 401 → 登录拿 token → 带 token 200（viewer 回显）
→ 账号清单 4 个 → 错误密码 401 → 篡改 token 401。
**前端 jsdom 冒烟**：未登录弹门禁 + 4 个账号按钮 → 点「小明」→ 门禁消失、token 落盘、10 张设备卡渲染、「我的」页显示"小明 · 青岛的家 · L4 · member_101"，**零运行时错误**。

**本步已规避的风险**

| 风险 | 规避方式 |
|---|---|
| 改认证弄脏真实记忆库 | 账号独立建库 `data/homemind_accounts.db`，与记忆库物理隔离 |
| 密钥误提交 | 优先读 `HM_AUTH_SECRET`；开发环境落 `data/.auth_secret`（`data/` 已 gitignore）；生产缺配置直接报错 |
| 重启后登录态全废 | 开发密钥持久化到文件，不每次随机生成 |
| 暴力破解 | 同用户名 60 秒 5 次失败即限流 |
| 时序攻击 / 账号枚举 | `hmac.compare_digest` + 不存在用户也跑一次哈希 + 统一错误文案 |
| **加鉴权后 58 个测试全 401** | 控制台测试用 `app.dependency_overrides` 注入固定身份（认证单独由 `test_auth.py` 覆盖），"测试确定性"原则未被破坏 |
| **网页因 401 白屏** | 提前做了前端登录门禁（原计划 S6），保证每一步结束应用都还能用 |

**本步发现的 bug（已修）**：`AccountStore.get_password_hash` 漏设 `row_factory`，
返回元组导致 `TypeError`；修法是把所有连接收口到 `_connect()`，杜绝复发。

### S1.5 · 用户实测反馈的两个 bug 修复 ✅（2026-10-14 晚）

| # | 用户报告 | 根因 | 修复 |
|---|---|---|---|
| 1 | 退出登录换账号（我→爸爸），数据还是"我"的 | 前端 `refresh()` 只在任务/场景页重渲染，「我的」页身份卡是旧渲染缓存；退出时对话面板也没清 | 非首页一律重渲染；退出/换号即清对话面板 |
| 2 | AI 对话没有记忆，问"我刚刚干了什么?"答不上 | `/api/console/assistant` 每轮全新 state，从不写短期记忆也不注入历史 | 接入"**滑动窗口 + 摘要压缩**"模式（见下） |

**多轮记忆实现（按用户指定的模式）**

- 会话按登录者隔离：`console:{member_id}`，请求体传的 session_id 不可信、忽略
- 每轮：写 user 消息 → 取"窗口内原文 + 窗口外摘要"注入 `AgentState.chat_history / chat_summary` → 图跑完把应答写回
- 短期记忆新增 `_compress_if_needed`：超过 2×窗口时，溢出部分压成摘要（LLM 优先，mock/失败降级为逐条拼接，**确定性可测**），原文裁到窗口大小
- `memory_query` 意图分支修通：真模型模式下把记忆清单作为确定事实喂给模型、结合"最近对话"作答（mock 保持纯模板，测试确定性不变）

**测试用例（`tests/test_chat_memory.py` 6 条，总数 75 全绿）**

| 用例 | 契约 |
|---|---|
| `test_window_compression_keeps_summary` | 超窗后原文 ≤ 窗口、摘要非空、**最早的溢出消息出现在摘要里（信息不丢）** |
| `test_no_summary_below_threshold` | 窗口内不触发压缩 |
| `test_sessions_isolated_per_member` | 两个会话互不可见 |
| `test_logout_clears_member_session` | 登出清空该成员会话 |
| `test_assistant_persists_conversation_history` | 连问两轮 → 记忆里 user/assistant 交替四条 |
| `test_history_reaches_the_graph_state` | 历史真的进入 AgentState 并跑图不崩 |

**真模型四轮实测**（这是单测覆盖不了的部分）：

```
① 打开窗帘          → 客厅窗帘已经打开了。（动作卡：已打开客厅窗帘）
② 我刚刚干了什么?    → 你刚刚让我打开了客厅窗帘，已经打开了。 ← 修复前答"我还没记住你的任何习惯"
③ 我习惯空调开24度   → 记下了
④ 我有哪些习惯?      → 你目前记下的习惯是：喜欢空调开24度
```
