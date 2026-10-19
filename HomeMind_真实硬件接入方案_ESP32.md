# HomeMind 真实硬件接入方案（ESP32 + ESPHome + Home Assistant）

> 编制时间：2026-10-20 ｜ 版本：v1.0 ｜ 适用项目：`C:\Users\13141\Desktop\HomeMind`
> 目标：把 Mock 设备层换成真实物理设备，让 AI 说一句"把客厅灯关掉"，灯**真的**灭，状态**真的**回流到控制台。
> 本文只写方案，不含已落地的代码改动。所有选型理由都标注了是"事实"还是"我的判断"。

---

## 0. 结论先看

**推荐路线（三层）**：

```
ESP32 硬件  ──ESPHome 固件──▶  Home Assistant  ──REST API──▶  HomeMind
（灯/灯带/继电器）  （原生 API/本地）   （状态唯一真相）        （只改一个 handler）
```

| 决策点 | 选择 | 一句话理由 |
|---|---|---|
| 固件 | **ESPHome**（不自己写 C++、不用 Tasmota） | 声明式 YAML，改一行就重新编译，GPIO 乱接会编译期报错 |
| 协议 | **ESPHome 原生 API 给 HA，HomeMind 走 HA REST** | 状态由 HA 统一维护，断线能被感知 |
| 网关 | **Home Assistant** | 91,599 星、昨天还在推送，Python 生态，REST 接口十年没变过 |
| 电压 | **全程 5V / 3.3V，不碰 220V** | 触电风险 = 0，这一步就把最可怕的事排除了 |
| 控制屏 | **最后做** | 屏是给人看的，闭环先跑通 |

**硬件三档**：零焊接（买灯）→ 基础继电器 → 灯带调光 → 带屏面板。见第 3 节。

---

## 1. 为什么是这条路：三份证据

### 1.1 社区规模与活跃度（本次实测，非记忆）

`api.github.com` 实时查询，2026-10-19/20：

| 项目 | ★ | 最后推送 | 角色 |
|---|---|---|---|
| home-assistant/core | 91,599 | 2026-10-19 | 网关 |
| lvgl/lvgl | 24,971 | 2026-10-19 | 嵌入式 UI（带屏方案用） |
| arendst/Tasmota | 24,883 | 2026-10-19 | 备选固件 |
| wled/WLED | 18,876 | 2026-10-19 | 灯效固件（本项目不需要） |
| espressif/esp-idf | 19,284 | 2026-10-19 | 官方 SDK（ESPHome 的底座） |
| esphome/esphome | 11,917 | 2026-10-19 | 固件 |
| eclipse-mosquitto/mosquitto | 11,285 | 2026-10-15 | MQTT Broker（本项目不用） |
| project-chip/connectedhomeip | 8,974 | 2026-10-19 | Matter（**否决**，见 1.3） |
| al-one/hass-xiaomi-miot | 6,171 | 2026-10-04 | 米家设备接 HA（零焊接路线用） |
| espressif/esp-box | 1,328 | 2026-06-01 | 官方带屏开发板固件 |
| esphome/aioesphomeapi | 198 | 2026-10-19 | ESPHome 的 Python 客户端 |

**关键一条**：`aioesphomeapi` 只有 198 星，但它的描述是 "Python Client for ESPHome native API. **Used by Home Assistant**"。星少不等于弱——它是 HA 官方在用的那一份，等于有人替你做持续集成。

### 1.2 一条决定性的对比

搜"ESP32 带屏控制面板"能搜到一堆项目，星数是这样：

| 项目 | 硬件 | ★ | 最后推送 |
|---|---|---|---|
| albert-canfield/HA-panel-esphome | Guition ESP32-4848S040 4寸 | 53 | 2026-01-07 |
| thomstas/esphome-esp32s3-7inch-lvgl | 7寸 800×480 | 0 | 2026-03-28 |
| Anghostdy/esphome-lvgl-dashboard | Guition JC3248W535C 3.5寸 | 0 | 2026-01-31 |

**这些不能当依赖，只能抄作业。** 一个人的项目，你跟着它升级，它停更你就卡死。真正撑得住的是它脚下的 ESPHome（11,917）和 LVGL（24,971）。

这条原则写进选型标准：**依赖选万星级、天天在推的；界面抄低星项目，但抄的是 YAML 结构，不是版本号。**

### 1.3 被否决的三条路，及原因

| 方案 | 为什么否决 |
|---|---|
| **Matter** | 要跑 Matter SDK、管 Fabric 证书、走 commissioning，开发/QA 成本按有经验的说法是 MQTT 路线的 **3–4 倍**。对你的求职场景零加分——面试官问的是你的 Agent 架构，不是你有没有 CSA 认证。 |
| **浏览器直连 ESP32 HTTP** | ESPHome 的 `web_server` 在 **2026.7.0 有破坏性变更**：默认拒绝跨域浏览器请求，`enable_private_network_access` 默认改 `false`。curl 和脚本不受限，但你前端直接打设备 IP 会被拦。绕它要么配 `allowed_origins`，要么在中控加代理——两种都比直接用 HA 麻烦。 |
| **自研固件（Arduino/ESP-IDF 裸写）** | GPIO 配错在编译期不报错，烧进去才发现灯不亮/板子重启。ESPHome 用 YAML 声明式描述，引脚冲突、组件缺失在**编译阶段**就报。省下的调试时间够你把方案文档写完。 |

---

## 2. 总体架构

### 2.1 分层

```
┌─────────────────────────────────────────────────────────────┐
│  HomeMind Web 控制台（web/，浏览器）                          │
│    "把客厅灯调到 60%"                                        │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP / JWT
┌────────────────────────▼────────────────────────────────────┐
│  HomeMind 后端（FastAPI + LangGraph）                        │
│   意图解析 → 安全护栏 → ToolRegistry → _handle_device_control │
│                                    │                          │
│                        ┌───────────▼──────────────┐          │
│                        │  ha_client.py（新增）      │          │
│                        │  httpx → HA REST API      │          │
│                        └───────────┬──────────────┘          │
└────────────────────────────────────┼─────────────────────────┘
                                     │ POST /api/services/light/turn_on
                                     │ GET  /api/states/light.xxx
┌────────────────────────────────────▼─────────────────────────┐
│  Home Assistant（容器 / 主机）                                │
│   · 维护设备唯一真相（state）                                 │
│   · ESPHome 原生 API 长连接，设备离线能立刻发现                │
└────────────────────────────────────┬─────────────────────────┘
                                     │ 原生 API（加密，端口 6053）
┌────────────────────────────────────▼─────────────────────────┐
│  ESP32-S3（ESPHome 固件）                                     │
│   · GPIO → LEDC PWM / RMT → MOSFET / 灯带                    │
│   · 板端也可跑本地自动化（断网照用）                           │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 一次"关灯"的完整数据流

1. 用户说"把客厅灯关掉"
2. `intent.py` 判 `device_control`，护栏放行（非高危设备）
3. `ToolRegistry` 调 `device_control(action=turn_off, room=客厅, device_name=灯)`
4. `_handle_device_control` 找到 `device_id=living_room_light`，**不再改本地对象**，改调 HA
5. `ha_client` 发 `POST /api/services/light/turn_off`，body `{"entity_id": "light.living_room_light"}`
6. HA 通过原生 API 把命令下发给 ESP32
7. ESP32 拉低 GPIO，MOSFET 断流，灯灭
8. ESP32 回报新状态 → HA → **HomeMind 回读** `GET /api/states/light.living_room_light`，拿到 `state: "off"`
9. 状态写回 `DeviceState`，前端刷新，灯卡从"已开 80%"变成"已关"
10. `events.py` 记一条"我 关闭了 客厅灯"

**第 8 步是整个改造的灵魂**：本地对象不再是真相，设备回报才是。你项目里踩过"`result.ok` 只是工具被调用成功、不等于操作生效"，到硬件上这句话会变成更难看的版本——UI 显示灯开着，灯没亮。

### 2.3 为什么中间要插一个 HA，而不是直连设备

| 直连 ESP32 HTTP | 经过 HA |
|---|---|
| 每台设备一个 IP，写死在配置里 | 一台网关，`entity_id` 统一寻址 |
| 状态靠轮询（或自己解析 SSE） | `GET /api/states/<entity>` 直接拿，或 WebSocket 订阅变化 |
| 设备掉线不知道 | HA 长连接，掉线立刻 `unavailable` |
| 亮度 0–255，要跟项目里的 0–100 换算 | **HA 的 `brightness_pct` 就是 0–100，跟项目一致，一行转换都不用写** |
| 凭证 = 设备密码，明文 | 凭证 = 一个 long-lived token，可随时吊销 |
| 浏览器 CORS 被 2026.7 变更拦 | 浏览器只跟 HA/HomeMind 说话，不碰设备 |

**"不用换算"这条是意外之喜，也是真正省 bug 的地方**——0–255 和 0–100 写反，灯会在不该亮的时候微亮。

---

## 3. 材料清单（BOM）

> **价格说明**：以下都是 2026 年 10 月国内电商的**估算区间**，我没实际下单，仅供量级参考。实际以你买到的为准。

### 3.1 四档配置对比

| 档 | 目标 | 估算总价 | 能验证什么 | 动手量 |
|---|---|---|---|---|
| **A 零焊接** | 今晚就看到真实反应 | ¥50–120 | 开/关/调亮度/调色温 | 无 |
| **B 基础** | 自己搭第一块板 | ¥90–150 | 开/关，AI 说话灯真亮 | 接线 3 根 |
| **C 推荐** | 完整闭环 + 数值调节 | ¥130–200 | 开/关/亮度/颜色，对齐项目"数值调节" | 接线 4 根 |
| **D 带屏** | 墙面控制面板 | ¥180–320 | 触摸屏点控 + 状态显示 | 烧固件 + 3D 打印外壳 |

### 3.2 A 档：零焊接（最省事，适合先验证整条链路）

| 物料 | 规格 | 数量 | 估算 | 选它的理由 |
|---|---|---|---|---|
| Yeelight 智能灯泡 | YLDP 系列，E27，需支持**局域网控制** | 1 | ¥50–90 | 官方 LAN 协议，无需拆机刷固件；关掉云也能用 |
| （备选）米家设备 | 任意 Wi-Fi 设备 | 1 | ¥40–120 | 配 `hass-xiaomi-miot`（★6171）接入 |

**需要额外做的**：在 Yeelight App 里打开"局域网控制"开关，然后 HA 装 `yeelight` 集成。
**放弃理由**：不能体验"自己烧固件"的过程，面试也没法讲"我写了固件"。但对"先确认整条链路通不通"这件事，性价比最高。

### 3.3 B 档：基础版（继电器 + 5V 台灯）

| 物料 | 规格 | 数量 | 估算 | 选它的理由 |
|---|---|---|---|---|
| 主控 | **ESP32-S3-DevKitC-1，N16R8**（16MB Flash + 8MB Octal PSRAM） | 1 | ¥45–65 | 官方开发板，引脚丝印清晰，USB-C 双口（一个串口一个原生 USB）；N16R8 的 Flash/PSRAM 余量够以后加屏、加 LVGL |
| 继电器模块 | **5V，1 路，带光耦隔离 + 续流二极管** | 1 | ¥6–10 | 光耦把控制侧和负载侧电气隔离；续流二极管吸收线圈断电反峰，否则反峰会打回 GPIO |
| 台灯 | **USB 5V 供电**（关键：不要 220V 台灯） | 1 | ¥20–40 | 全程 5V，见 4.1.1 |
| 杜邦线 | 母对母，20cm | 1 包 | ¥5 | — |
| USB 电源 | 5V 2A，带数据线 | 1 | ¥15–25 | 供电和烧录共用一根线 |

**为什么不用"最便宜的裸继电器模块"**：那种没有光耦的模块，线圈断电瞬间会在控制侧产生几十伏的反向尖峰，运气好是 GPIO 莫名重启，运气不好是芯片报废。差价 3 块钱，不值得赌。

### 3.4 C 档：推荐版（WS2812 灯带，能调亮度能变色）

| 物料 | 规格 | 数量 | 估算 | 选它的理由 |
|---|---|---|---|---|
| 主控 | ESP32-S3-DevKitC-1 N16R8 | 1 | ¥45–65 | 同上 |
| 灯带 | **WS2812B，5V，1 米 60 灯，IP30 裸板** | 1 | ¥20–35 | 单线控制（一根数据线搞定全部灯珠），每个灯珠独立寻址；`esp32_rmt_led_strip` 是 ESPHome 官方组件 |
| 电源 | **5V 5A** 独立适配器 | 1 | ¥25–40 | 60 颗全白约 3.6A，5A 给到约 28% 余量。想更宽裕（或少亮灯时的电压跌落）选 5V 6A。**绝不能用开发板的 5V 引脚供电** |
| 电解电容 | 1000µF / 10V | 1 | ¥2 | 并在灯带电源两端，吸收上电涌流，防第一颗灯珠烧毁 |
| 电阻 | 330Ω / 0.25W | 1 | ¥0.5 | 串在数据线上，抑制反射振铃 |
| 接线端子 | 5.08mm 2P/3P | 若干 | ¥5 | 灯带裸线比杜邦线粗，直接插会接触不良 |

**这套比 B 档强在哪**：项目里灯是"可调档设备"（`value=80.0`），B 档的继电器只能开/关，等于把你代码里写好的亮度逻辑浪费了。C 档能真正跑通"调到 60%"。

### 3.5 D 档：带屏面板（最后做）

| 物料 | 规格 | 数量 | 估算 | 选它的理由 |
|---|---|---|---|---|
| 一体板 | **Guition ESP32-4848S040**（ESP32-S3，4.0" IPS 480×480 电容触摸，16MB Flash + 8MB PSRAM，**板载 1–3 路继电器**） | 1 | ¥130–180 | 社区带屏项目用得最多的板子（`albert-canfield/HA-panel-esphome` 就是这个），有现成 YAML 可抄；板载继电器意味着**面板本身就是开关** |
| 备选 | 乐鑫官方 **ESP32-S3-BOX-3**（2.4" 触摸屏 + 双麦 + 扬声器） | 1 | ¥280–350 | 官方出品，`espressif/esp-box` ★1328 有官方固件；带麦克风，以后能加语音 |
| 外壳 | 3D 打印 | 1 | ¥0–40 | 有打印机自己做，没有就淘宝代打 |

**为什么不推荐 7 寸**：7 寸 800×480 的 RGB 屏对 ESP32-S3 的 PSRAM 带宽压力大，社区项目里要专门"降低像素时钟减少抖动"（那是 `thomstas` 项目的一条 commit 信息）。4 寸 480×480 是这台芯片的舒适区。

### 3.6 网关主机（跑 Home Assistant）

| 选项 | 前提 | 估算 | 评价 |
|---|---|---|---|
| **你现有的 Windows 11 电脑 + Docker Desktop** | 已有 | ¥0 | **最现实**。但 Windows 的 Docker **不支持 `--network host`**，必须手动填设备 IP 添加集成（见 7.2） |
| 旧笔记本 / 迷你主机装 HAOS | 有一台闲置机 | ¥0–600 | 最省心：一键更新、有 add-ons（ESPHome 面板直接网页编译） |
| 树莓派 4/5 | 需另买 | ¥400–700 | 官方支持，但 2026 年性价比不如二手迷你主机 |
| Home Assistant Green | 官方整机 | 约 ¥700+ | 插电即用，零折腾，最贵 |

**我的判断**：先用你现成的 Windows + Docker 把闭环跑通。跑通之后如果觉得每天开电脑很烦，再花几百块买个迷你主机装 HAOS。不要一开始就为了"稳定"去买硬件——**你连闭环都没验证过，谈什么稳定**。

---

## 4. 安全与可靠：逐条给理由

这一节是你点名要的。每一条都写清"为什么"，而不是"必须这样"。

### 4.1 电气安全（优先级最高，出事是人身伤害不是 bug）

#### 4.1.1 全程 5V，从根上不碰 220V

**做法**：负载端只用 USB 5V 台灯 / 5V 灯带。继电器的"被控端"接的是 5V 回路，不是市电。

**理由**：
- 220V 触电的后果是人身伤害，不是"程序报错"。而 5V 接触人体是安全的（安全电压一般认为 ≤36V）。
- 一旦引入 220V，你就同时引入了：绝缘、接地、外壳、阻燃、走线间距、漏电保护这一整套**电工规范**。这些不是"注意一点"能解决的，是专业领域。
- 你的目标是验证"AI → 物理世界"这条链路，不是考电工证。**用 5V 能验证的东西，就不要用 220V 去验证。**

**如果以后确实要控 220V 的灯**：不要自己做。买现成的**智能插座**（几十块，出厂已过认证），让 HA 通过插座间接控制。你的代码不用改——插座在 HA 里同样是一个 `switch` 实体。

#### 4.1.2 GPIO 不能直接驱灯，必须过一层

**做法**：GPIO →（PWM/电平）→ MOSFET 或继电器 → 灯。

**理由**：
- ESP32-S3 单个 GPIO 的推荐持续输出电流是 **20mA 量级**（绝对最大值约 40mA）。一颗普通 LED 就要 10–20mA，一条灯带是**安培级**。直连的结局是 GPIO 烧毁，芯片内部 ESD 二极管击穿，整块板子报废。
- GPIO 输出是 3.3V 逻辑，而灯带/继电器是 5V 系统。3.3V 驱动 5V 的继电器线圈可能吸合不可靠（吸合了但接触电阻大，发热）。
- 用 MOSFET（灯带场景）或继电器（开关场景）做电流放大，是标准做法，不是可选项。

#### 4.1.3 上电瞬间继电器可能误动作 —— 官方明确警告过

这是最容易被忽略、也最吓人的一条。ESPHome 官方 `switch/gpio` 文档原文：

> "at reset time (before any of ESPHome's code runs) the relay GPIO pins may have pull-ups active, so the relay may be active before ESPHome can manually deactivate them."

翻译：**ESP32 复位期间（你的代码还没开始跑），GPIO 可能处于上拉状态，继电器会先吸合一下，然后才被代码关掉。**

后果：每次断电重启，灯会"闪一下"。如果你控的是门锁或燃气阀，这一下就是事故。

**三条应对**（按推荐顺序）：

1. **用低电平触发的继电器模块**（市面上多数是 `IN` 低电平有效），配合 ESPHome：
   ```yaml
   switch:
     - platform: gpio
       pin:
         number: GPIO5
         inverted: true        # 反相：让"关"对应 GPIO 高电平
   ```
   低电平触发 + 外部下拉电阻，上电瞬间继电器保持断开。

2. **`restore_mode: ALWAYS_OFF`**（ESPHome 默认就是这个）——启动时强制初始化为关，不恢复上次状态。宁可"重启后灯灭了要重新开"，不要"重启后灯自己亮了"。

3. **加硬件下拉电阻**（10kΩ 从 GPIO 到 GND），让引脚在复位期间被确定地拉低，不依赖芯片内部上拉的默认值。

**这条的本质**：软件无法控制"软件还没运行的那段时间"。任何依赖软件来保证的安全，都有这段真空期。所以这类保护必须落在**硬件**上。

#### 4.1.4 ESP32-S3 的引脚禁区（接错就崩，不是不亮）

ESP-IDF 官方文档（v5.3/v6.0 中文版）明确：

| GPIO | 状态 | 说明 |
|---|---|---|
| **26–32** | ⛔ 禁用 | SPI0/1，接内部 Flash。接东西 → 缓存崩溃 |
| **33–37** | ⛔ 禁用（N16R8） | Octal PSRAM 占用。**你的板子就是 R8，所以这几个也不能用** |
| 19 / 20 | ⚠️ 慎用 | 原生 USB D−/D+，用了就失去 USB-JTAG 调试 |
| 0 / 3 / 45 / 46 | ⚠️ 慎用 | Strapping 引脚，上电电平决定启动模式。GPIO0 被拉高会进下载模式（板子"变砖"假象） |
| 48 | ⚠️ 占用 | 多数板子的板载 RGB 灯 |
| 22–25 | ❌ 不存在 | S3 没有这几个脚 |

**安全可用白名单**：`GPIO 1–18、21、38–42、47`。

**建议直接钉死用这几个**：`GPIO 4、5、6、7、15、16、17、18、21`——这九个脚不碰 Flash、不碰 PSRAM、不碰 USB、不碰 strapping、不吃 ADC/Wi-Fi 冲突。

**理由**：`33–37` 在很多"引脚图"上画着、排针上也引出来了，看着能用。一旦接了东西，表现是随机重启、Wi-Fi 掉线、程序跑飞——**你绝对会先怀疑自己的代码，排查一整天之后才发现是接了不该接的脚**。把白名单写进文档，比事后 debug 便宜一百倍。

#### 4.1.5 必须共地

**做法**：ESP32 的 GND 和灯带电源的 GND 必须连在一起。

**理由**：数据信号是"相对于 GND 的电压差"。不共地，两边的 GND 电位漂移，ESP32 输出的 3.3V 在灯带看来可能是 2.1V 或 4.4V——表现为颜色乱闪、第一颗灯不亮、偶尔全亮。

这是新手第一大坑，因为它"有时好使"，让你以为代码有问题。

#### 4.1.6 灯带电流要算，不能拍脑袋

**计算**（WS2812B 官方数据，每个灯珠全白最大约 **60mA**）：

| 灯数 | 全白电流 | 电源建议 |
|---|---|---|
| 30 颗 | 1.8 A | 5V 3A |
| **60 颗** | **3.6 A** | **5V 5A**（余量约 28%；想宽裕选 6A） |
| 144 颗 | 8.6 A | 5V 10A |

**注意**：这里的 60mA/颗是"全白满亮度"的最坏值，实际日常用不到。但**电源要按最坏值选**——因为总有一天你会让它全白，而那一刻不该是重启循环。

**为什么必须独立电源**：
- ESP32 开发板通过 USB 供电时，USB 2.0 端口只给 500mA。60 颗灯全白要 3.6A，**是它的 7 倍**。
- 从开发板的 5V 引脚取电，等于让板载 LDO 和 USB 线缆扛 3.6A，结果是电压跌落 → ESP32 欠压复位 → 无限重启循环。
- 表现：灯带一亮，板子就重启。看起来很玄学，其实就是供电。

**为什么加 1000µF 电容**：上电瞬间所有电容同时充电，涌流可能达到稳态电流的数倍。没有这个大电容吸收，第一颗灯珠承受最大冲击，最容易先烧。

#### 4.1.7 数据线串 330Ω 电阻

**理由**：长导线是传输线，信号边沿会在末端反射，产生过冲（可能超过 5V）和振铃。330Ω 串阻做阻抗匹配，把反射压下去。1 米以内可能感觉不到，但"可能感觉不到"不等于"不存在"，一颗电阻 5 毛钱。

### 4.2 网络与凭证安全

| 风险 | 措施 | 理由 |
|---|---|---|
| HA token 泄露 | `HA_TOKEN` 只放 `.env`，`.env` 在 `.gitignore` 里 | long-lived token 等于**整个家的全权钥匙**，能开锁能开燃气阀。你项目已经有"AI 绝不读取含密钥文件"的约定，这条一致 |
| 设备暴露公网 | ESP32 和 HA 都只在内网，路由器不做端口映射 | ESP32 的 Web 服务鉴权是 basic（弱），暴露公网等于送人 |
| 浏览器直连设备 | 前端只跟 HomeMind 说话，不碰设备 IP | 见 1.3：ESPHome 2026.7.0 起的 CORS 变更会拦；即使不拦，也不该让浏览器持有设备凭证 |
| 家里来客人 | HA 单独建一个**只读**用户给访客 | HA 支持按用户配权限，不用共享主账号 |
| 误操作 | 门锁/燃气阀这类 `high_risk` 设备走二次确认 | 你项目 `FR-SEC-001` 已有，接硬件后从"演示"变"必需" |

### 4.3 系统可靠性

| 关注点 | 现状 | 加固做法 |
|---|---|---|
| **断网** | ESPHome 设备在板端跑，本地自动化不依赖云 | 关掉路由器做一次测试，灯应该照常响应 |
| **HA 宕机** | HomeMind 调不到 HA，报错 | `ha_client` 必须捕获超时，返回"设备网关无响应"，**不要假装成功**。这是你项目"假的判定标准"那条原则的硬件版 |
| **ESP32 掉线** | HA 立刻感知（原生 API 是长连接），实体变 `unavailable` | HomeMind 读状态时判断 `unavailable`，UI 显示"离线"而不是显示旧值 |
| **状态漂移** | 用户手动按了墙上的开关 | **所有操作后回读状态**。本地缓存必须随时可被设备真值覆盖 |
| **重复下发** | AI 重试/用户连点 | 你项目已有幂等键（`execution.py`），接硬件后这个机制开始真正有用 |

### 4.4 安全设计的总结一句话

**能用硬件保证的，不要用软件保证；能用隔离保证的，不要用代码保证。**

- 触电风险 → 用 5V 从物理上消除（4.1.1）
- 过流风险 → 用 MOSFET/继电器从物理上隔离（4.1.2）
- 上电误动作 → 用下拉电阻保证，不指望代码（4.1.3）
- 引脚冲突 → 用白名单避开，不指望记性（4.1.4）
- 误操作 → 用白名单 + 二次确认，不让模型自己决定（4.2）

---

## 5. 接线

### 5.1 C 档（灯带，推荐）

| ESP32-S3 引脚 | 接到 | 备注 |
|---|---|---|
| `GPIO 5` | 330Ω 电阻 → 灯带 `DIN` | 数据线，串阻必加 |
| `GND` | 灯带 `GND` + 电源 `GND` | **必须共地**，见 4.1.5 |
| — | 电源 `+5V` → 灯带 `+5V` | 不要从开发板取电 |
| — | 1000µF 电容并在灯带电源两端 | 注意极性，反接会炸 |

**接线顺序**（很重要）：**先接 GND，再接数据，最后接 5V**。带电插拔数据线会通过灯珠内部 ESD 二极管串电，烧第一颗灯。

### 5.2 B 档（继电器）

| ESP32-S3 引脚 | 接到 |
|---|---|
| `GPIO 5` | 继电器模块 `IN` |
| `GND` | 继电器 `GND` |
| `3V3` | 继电器 `VCC`（5V 继电器也可以用 5V 引脚，但 3.3V 更保险，见 4.1.2） |

继电器**常开触点（NO）**串在 5V 电源和台灯之间。**不要用常闭（NC）**——那样断电时灯是亮的。

### 5.3 上电前自检清单

- [ ] 没有任何线接到 GPIO 26–37（拿万用表或放大镜看丝印确认）
- [ ] 灯带电源和 ESP32 的 GND 已经连在一起
- [ ] 1000µF 电容极性正确
- [ ] 数据线串了 330Ω 电阻
- [ ] 没有任何 220V 线路出现在桌面上
- [ ] 用万用表量过 5V 电源输出确实是 5V（有些"5V"适配器实测 5.4V）

---

## 6. 固件（ESPHome YAML，可直接烧）

### 6.1 C 档：WS2812 灯带（对应项目里的 `living_room_light`）

```yaml
esphome:
  name: homemind-strip
  friendly_name: HomeMind Strip

esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf          # ESP32-S3 建议用 IDF，性能和稳定性都更好

# 网络与接口
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
  ap:                      # 连不上 Wi-Fi 时开热点，方便救砖
    ssid: "homemind-strip-fallback"

api:                       # 原生 API，给 HA 用，加密
  encryption:
    key: !secret api_key

ota:
  - platform: esphome
    password: !secret ota_password

logger:
  level: INFO

# 调试用：开网页界面，方便用 curl 直接验证
web_server:
  port: 80
  version: 2
  # 如果你要让浏览器页面直接调它，需要配 allowed_origins（见 10.1）
  # 正常情况下 HomeMind 走 HA，不需要开这个

# ---- 灯带 ----
light:
  - platform: esp32_rmt_led_strip
    id: strip
    name: "Living Room Light"   # ← HA 里会变成 light.living_room_light
    pin: GPIO5
    num_leds: 60
    chipset: ws2812
    channel_colors: GRB        # 必填。必须 GRB，写 RGB 会红绿颠倒
    restore_mode: ALWAYS_OFF   # 断电重启不自作主张亮起来（见 4.1.3）
    effects:
      - random:
      - pulse:
```

**注意**：`channel_colors` 是当前版本**必填**字段（GRB / BGR / GRBW 等，大小写不敏感）。老教程里写的 `rgb_order: GRB` 在新版会报 unknown key，别照抄旧配置。

### 6.2 B 档：继电器 + 台灯

```yaml
esphome:
  name: homemind-relay

esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

api:
  encryption:
    key: !secret api_key

ota:
  - platform: esphome
    password: !secret ota_password

logger:

switch:
  - platform: gpio
    id: relay1
    name: "Living Room Light"     # 对 HA 暴露为 switch.living_room_light
    pin:
      number: GPIO5
      inverted: true              # 低电平触发，见 4.1.3
    restore_mode: ALWAYS_OFF      # 上电强制断开
    # 想让它在 HA 里长得像"灯"而不是"开关"，再加一个 light 包装：
    # light:
    #   - platform: binary
    #     name: "Living Room Light"
    #     output: relay1
```

### 6.3 可调光版：PWM + MOSFET（不买灯带也能调亮度）

```yaml
output:
  - platform: ledc
    id: pwm_out
    pin: GPIO5
    frequency: 19531Hz     # 官方推荐：位深 12 位，4096 级，低亮度过渡平滑

light:
  - platform: monochromatic
    id: dimmable
    name: "Bedroom Light"
    output: pwm_out
    restore_mode: ALWAYS_OFF
```

**为什么选 19531Hz 而不是默认 1kHz**：ESPHome 官方给了推荐频率表——19531Hz 对应 12 位（4096 级）分辨率。频率太低（1kHz）在低亮度时会看到闪烁和台阶感，而且部分人耳能听到高频啸叫。S3 的 LEDC 只支持 14 位定时器，所以别贪 2200Hz 以上的高分辨率。

### 6.4 `secrets.yaml`（**不要提交到 git**）

```yaml
wifi_ssid: "你的WiFi名"
wifi_password: "你的WiFi密码"
api_key: "32字节base64"      # 生成：python -c "import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())"
ota_password: "另一个强密码"
```

---

## 7. Home Assistant 部署

### 7.1 你的环境怎么装（Windows 11 家庭版 + Docker Desktop）

官方有两种安装类型：**Home Assistant OS**（推荐，支持 add-ons）和 **Home Assistant Container**（Docker，无 add-ons，需手动更新）。

Windows 家庭版没有 Hyper-V，装 HAOS 虚拟机会比较绕。**最现实的路径是 Container**：

```bash
docker run -d --name homeassistant \
  --restart=unless-stopped \
  -v C:\ha-config:/config \
  -e TZ=Asia/Shanghai \
  -p 8123:8123 \
  ghcr.io/home-assistant/home-assistant:stable
```

打开 `http://localhost:8123`，走完初始化向导。

**代价要说清楚**：
- 没有 add-ons，所以网页版 ESPHome 编译面板用不了 → 用命令行 `pip install esphome` 或单独跑 `ghcr.io/esphome/esphome` 容器
- **Windows 的 Docker 不支持 `--network host`** → mDNS 自动发现失效，必须手动填设备 IP（见 7.2）

**如果你有闲置旧笔记本**：装 HAOS 会舒服很多（一键更新、有 add-ons、支持 mDNS）。这是唯一值得为它花硬件的理由。

### 7.2 把 ESP32 加进 HA

1. HA 界面 → **设置 → 设备与服务 → 添加集成 → ESPHome**
2. 填 `host`：设备的 IP（在路由器后台或 ESPHome 日志里看），端口 `6053`
3. 填加密 key（`secrets.yaml` 里的 `api_key`）
4. 设备就出现了，实体会是 `light.living_room_light` 或 `switch.living_room_light`

**entity_id 怎么来的**：ESPHome 的 `name` 转小写下划线。你在 YAML 里写 `name: "Living Room Light"`，HA 里就是 `light.living_room_light`——**跟你项目里 `device_id` 一字不差**。这个对齐不是巧合，是当初按智能家居惯例命名的红利。

### 7.3 生成 HomeMind 用的 token

HA 界面 → 左下角点自己的头像 → **安全 → 长期访问令牌 → 创建令牌**。
生成的 token 只显示一次，存进项目的 `.env`：

```
HM_HA_BASE_URL=http://127.0.0.1:8123
HM_HA_TOKEN=<粘贴>
HM_DEVICE_BACKEND=ha          # mock | ha
```

**这个 token 等于全家的钥匙**，绝不进 git。你项目已有"AI 绝不读取含密钥文件"的规矩，照办。

### 7.4 先手动验证一次（不要跳过）

```bash
# 1. 查状态
curl -s -H "Authorization: Bearer $HM_HA_TOKEN" \
  http://127.0.0.1:8123/api/states/light.living_room_light

# 2. 开灯，亮度 60%
curl -s -X POST -H "Authorization: Bearer $HM_HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id":"light.living_room_light","brightness_pct":60}' \
  http://127.0.0.1:8123/api/services/light/turn_on

# 3. 关灯
curl -s -X POST -H "Authorization: Bearer $HM_HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id":"light.living_room_light"}' \
  http://127.0.0.1:8123/api/services/light/turn_off
```

**这三条 curl 跑通之前，不要改 HomeMind 任何代码。** 否则你分不清是 HA 的问题还是你的问题——这是排查问题最基本的分层原则。

---

## 8. HomeMind 侧改造

### 8.1 接缝只有一处

`src/homemind/tools/device_server.py:181` 的 `_handle_device_control` 是唯一的改造点。

**不需要动的**：`build_device_tools()` 的 JSON Schema、`ToolRegistry`、`execution.py` 的治理逻辑（幂等键/记忆补全/增量解析）、`intent.py` 的意图判定、前端、测试。

这是项目开局就设计好的分层——工具 Schema 描述"能做什么"，handler 才管"怎么做"。换后端只动 handler。

### 8.2 先加配置项（跟项目现有 `config.py` 风格一致）

项目已有 `src/homemind/config.py`，全是 `@dataclass` + `field(default_factory=lambda: os.getenv(...))`。照这个风格加一段：

```python
@dataclass
class HardwareConfig:
    """真实硬件后端配置。

    backend=mock 时走内存假设备（评测/单测用，零依赖）；
    backend=ha 时走 Home Assistant（真实物理设备）。
    allowlist 是物理设备的白名单：表外的设备一律回落 Mock。
    AI 误开一次客厅灯无所谓，误开一次门锁是安全事件——
    白名单比事后道歉便宜。
    """

    backend: str = field(default_factory=lambda: os.getenv("HM_DEVICE_BACKEND", "mock"))
    ha_base_url: str = field(
        default_factory=lambda: os.getenv("HM_HA_BASE_URL", "http://127.0.0.1:8123"))
    ha_token: str = field(default_factory=lambda: os.getenv("HM_HA_TOKEN", ""))
    ha_timeout: float = field(default_factory=lambda: _get_float("HM_HA_TIMEOUT", 8.0))
    allowlist: tuple[str, ...] = (
        "living_room_light", "bedroom_light",
    )

    @property
    def enabled(self) -> bool:
        """真后端只有在配置齐全时才启用，否则静默回落 Mock。"""
        return self.backend == "ha" and bool(self.ha_token)
```

然后在 `Settings` 里加一行：

```python
    hardware: HardwareConfig = field(default_factory=HardwareConfig)
```

**注意 `enabled` 这个属性**：它跟项目里 `LLMConfig` / `RedisConfig` 的 `enabled` 是同一个模式——**配置不全时降级，而不是崩溃**。这样你在没接硬件的机器上照样能跑全部 133 条测试。

### 8.3 新增 `integrations/ha_client.py`（示意实现，未在真实 HA 上跑过）

```python
"""Home Assistant REST 客户端。

三条设计原则，每条都对应项目里踩过的坑：
1. 超时必须显式设置。不设的话 AI 对话会卡在半路，用户以为系统挂了。
2. 失败必须抛异常，绝不返回"假成功"。踩过 result.ok ≠ 操作生效。
3. 状态必须回读，不信任本地缓存。
"""
from __future__ import annotations

import httpx

from ..config import get_settings
from ..core.trace import get_logger

logger = get_logger("integrations.ha")


class HAClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        cfg = get_settings().hardware
        self.base_url = (base_url or cfg.ha_base_url).rstrip("/")
        self.token = token or cfg.ha_token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            timeout=httpx.Timeout(cfg.ha_timeout, connect=3.0),
        )

    async def call_service(self, domain: str, service: str, **data) -> list:
        """调 HA 服务。返回 HA 说"因此改变了状态"的实体列表。"""
        resp = await self._client.post(f"/api/services/{domain}/{service}", json=data)
        resp.raise_for_status()
        return resp.json()

    async def get_state(self, entity_id: str) -> dict:
        """读单个实体状态。设备离线时 state 为 'unavailable'。"""
        resp = await self._client.get(f"/api/states/{entity_id}")
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
```

### 8.4 `_handle_device_control` 的改造分支

```python
# 文件顶部需要：import httpx，from ..config import get_settings
async def _handle_device_control(args: dict[str, Any]) -> dict[str, Any]:
    family_id = _resolve_family(args)
    device = _find_device(args, family_id)       # 原有逻辑，不动
    action = args["action"]
    cfg = get_settings().hardware

    # 双闸：后端配好 且 设备在白名单里。任一不满足就走 Mock。
    if cfg.enabled and device.device_id in cfg.allowlist:
        from ..integrations.ha_client import get_ha_client
        ha = get_ha_client()

        # 1. 设备 → HA 实体：device_id 就是 entity_id 的后半段
        domain = "light" if device.device_type in ("light", "curtain") else "switch"
        entity_id = f"{domain}.{device.device_id}"

        # 2. 下发命令
        try:
            if action == "turn_on":
                payload = {"entity_id": entity_id}
                if args.get("value") is not None:
                    # HA 用 brightness_pct（0-100），跟项目里的 0-100 一致，无需换算
                    payload["brightness_pct"] = int(args["value"])
                await ha.call_service(domain, "turn_on", **payload)
            elif action == "turn_off":
                await ha.call_service(domain, "turn_off", entity_id=entity_id)
            elif action == "set_value":
                await ha.call_service(domain, "turn_on",
                                      entity_id=entity_id,
                                      brightness_pct=int(args["value"]))
            else:
                return {"success": False, "message": f"不支持的动作 {action}"}
        except httpx.HTTPError as exc:
            # 关键：网关不通就如实说，别改本地状态装成成功
            logger.warning("HA 调用失败 %s %s: %s", entity_id, action, exc)
            return {"success": False, "message": "设备网关无响应，操作未执行"}

        # 3. ★ 回读真实状态，本地缓存让位
        state = await ha.get_state(entity_id)
        if state.get("state") == "unavailable":
            return {"success": False, "message": f"{device.name} 已离线"}
        device.power = state["state"] == "on"
        if "brightness" in state.get("attributes", {}):
            device.value = round(state["attributes"]["brightness"] / 2.55, 1)

        return {"success": True, "device": device.name,
                "action": action, "state": state["state"]}

    # ---- Mock 分支（原逻辑，评测与单测继续跑）----
    ...
```

**三个刻意的地方**（都对应你项目里已有的教训）：

1. **`brightness / 2.55` 是反算**：HA 的 `brightness` 属性是 0–255，`brightness_pct` 是 0–100。写入用 pct，回读用 255，所以回读要除回去。这里写错会让 UI 显示"已开 255%"。
2. **`unavailable` 要单独处理**：不处理的话，UI 显示的是上次的 `on`，用户以为灯亮着。这跟"假数据"那条判定标准一个道理——**能不能被用户的操作改变？不能，它就是假的。**
3. **异常必须落到 `success: False`**：你项目里 `result.ok and data.get("success", True)` 那道判断，在硬件场景下才是真正救命的。

### 8.5 实体映射表（先手工，别自作聪明自动化）

| 项目 `device_id` | HA `entity_id` | 类型 | 风险 |
|---|---|---|---|
| `living_room_light` | `light.living_room_light` | 灯带 | 低 |
| `bedroom_light` | `light.bedroom_light` | 灯带/PWM | 低 |
| `living_room_ac` | 暂不接真机 | — | — |
| `front_door_lock` | **不接** | — | **高危，见 8.6** |

**建议写死一张映射表，不要"按名字模糊匹配"**。你项目里已经有模糊匹配（"客厅灯"→`living_room_light`），那是给人说话用的；**落地到物理设备这一步必须是白名单**。

### 8.6 为什么白名单要写在代码里，而不是存在数据库

`HardwareConfig.allowlist` 是一个**元组常量**，不是用户能改的配置项。理由：

- AI 误开一次客厅灯，你笑一下就过去了。AI "误开"一次门锁，那是安全事件。
- 你项目里 `high_risk` 的二次确认在 Mock 时代是演示，接真硬件后是**最后一道防线**。而防线前面的白名单，比防线本身更便宜。
- 白名单一旦进了数据库，就意味着有接口能改它——**那么"能不能控门锁"这个问题，就变成了一个 HTTP 请求能决定的事**。安全边界不该是数据，应该是代码。
- 想扩白名单？改代码、跑测试、提一次 commit。这个摩擦力是**故意**留的。

用户能改的东西（场景、习惯、成员）继续存库；**"AI 到底能碰哪些物理设备"这件事，只写在代码里**。

---

## 9. 联调验收清单（12 项，按顺序做）

| # | 验证项 | 通过标准 |
|---|---|---|
| 1 | ESP32 连上 Wi-Fi | ESPHome 日志出现 IP |
| 2 | HA 发现设备 | 集成里出现实体，状态可读 |
| 3 | curl 能开灯 | 灯真亮 |
| 4 | curl 能关灯 | 灯真灭 |
| 5 | curl 设 60% 亮度 | 灯明显变暗，`GET /api/states` 回 `brightness: 153` |
| 6 | 拔电源，10 秒后插回 | **灯不亮**（`restore_mode: ALWAYS_OFF` 生效） |
| 7 | 断网（关路由器） | 手动按墙开关，灯正常工作 |
| 8 | 拔掉 ESP32 电源 | HA 里实体变 `unavailable`；HomeMind UI 显示离线，不显示旧值 |
| 9 | HomeMind 说"关客厅灯" | 灯灭 + 消息中心留痕 + 状态卡变"已关" |
| 10 | HomeMind 说"把卧室灯调到 30%" | 灯真到 30%，UI 数值与设备一致 |
| 11 | HA 关掉，再说"开灯" | 提示"设备网关无响应"，**UI 状态不变**（不能被假成功骗） |
| 12 | 说"打开大门门锁" | 走二次确认，且**因不在白名单被拦下** |

第 6 和 11 两项最容易漏，但恰恰最能证明"这套东西是真的"。

---

## 10. 会咬人的坑（版本级，写下来免得重新踩）

### 10.1 ESPHome `web_server` 的 2026.7.0 破坏性变更

**事实**（官方文档原文）：
- 默认**拒绝跨域浏览器请求**，只允许同源，或 `allowed_origins` 里列出的来源
- `enable_private_network_access` 默认从 `true` 改成 `false`，启用时必须同时配 `allowed_origins`
- 不带 `Origin` 头的请求（curl、脚本、原生 API）**不受限制**

**影响**：你前端如果打算直接 `fetch('http://192.168.x.x/light/xxx/turn_on')`，会被拦。走 HA 就没有这个问题（浏览器只跟 HomeMind 和 HA 说话）。

### 10.2 `web_server` v1 将在 2027.1.0 移除

默认已是 v2。别照老教程写 `version: 1`。

### 10.3 `auth` 的 basic → digest

默认还是 `basic`，但官方说 **2027.1.0 会改成 `digest`**。届时 REST 客户端要用 `curl --digest`。反正你走 HA，用不上。

### 10.4 亮度两套刻度

| 场景 | 字段 | 范围 |
|---|---|---|
| ESPHome REST | `brightness` | **0–255** |
| HA 服务调用 | `brightness_pct` | **0–100** ← 跟项目一致 |
| HA 状态回读 | `attributes.brightness` | **0–255** ← 要除回去 |

这三个不要混。写反了的表现是"灯开着但暗得像没开"或"亮度调不上去"。

### 10.5 `channel_colors` vs `rgb_order`

ESPHome 新版用 `channel_colors: GRB`（必填）。旧配置写的 `rgb_order: GRB` 在部分版本会报 unknown key。看到这个报错就删掉 `rgb_order`。

### 10.6 Windows 的 Docker 没有 `--network host`

HA 容器只能走 bridge + 端口映射，mDNS 自动发现失效 → 手动填设备 IP。这一条不知道的话，你会以为是"ESPHome 集成坏了"。

### 10.7 实体名里的空格和中文

ESPHome REST 路径是 `/<domain>/<entity_name>/<action>`，`entity_name` 是 YAML 里写的名字，**含空格和中文要 URL 编码**。走 HA 没这个问题（用 entity_id）。

---

## 11. 成本与工期（估算，不是承诺）

| 阶段 | 内容 | 工期 | 花钱 |
|---|---|---|---|
| P0 | A 档（买 Yeelight）+ 装 HA + curl 跑通 | 半天 | ¥50–120 |
| P1 | C 档硬件 + 固件 + HA 接入 | 1–2 天 | ¥130–200 |
| P2 | `ha_client` + handler 改造 + 白名单 | 半天 | ¥0 |
| P3 | 补测试（Mock/HA 双分支）+ 端到端验收 | 半天 | ¥0 |
| P4 | 带屏面板（可选） | 2–3 天 | ¥180–320 |

**P0 半天就能看到"AI 说话，物理世界的灯真的灭"**。这个反馈比你想象的爽，也比写十篇文档有用。

---

## 12. 不做的事（诚实边界）

- **不接门锁、不接燃气阀**。Mock 里可以随便演，物理世界里一次误开就是事故。想接必须走"独立硬件 + 独立二次确认 + 独立白名单"三件套，不是这次的范围。
- **不接空调、加湿器、洗衣机**。它们需要红外/厂商 API/功率控制，属于另一个工程。
- **不做 220V 强电**。见 4.1.1。
- **不做 Matter**。见 1.3。
- **不保证 Windows 上的 HA 长期稳定**。Windows 更新、Docker Desktop 升级都可能让 HA 挂掉。要长期跑，换迷你主机装 HAOS。
- **本文的代码示意未经真实 HA 环境验证**。验证方法在第 9 节，第 3、4 条 curl 跑通之前不要改 HomeMind。

---

## 附：本文的事实 / 判断分离

**事实（有出处）**：
- 所有 ★ 数与推送日期：`api.github.com` 实时查询，2026-10-19/20
- ESP32-S3 引脚禁区、SPI0/1、Octal PSRAM 占用 33–37：ESP-IDF 官方文档（v5.3.1 / v6.0 中文版）
- `web_server` 的 CORS 变更、v1 移除时间、auth 类型变更：esphome.io 官方组件文档
- 继电器上电可能误动作：esphome.io `switch/gpio` 文档原文
- YAML 组件名与参数（`esp32_rmt_led_strip` / `channel_colors` / `ledc` / `19531Hz`）：esphome.io 官方文档
- HA 两种安装类型、Container 无 add-ons：home-assistant.io 安装文档
- REST 接口格式（`/api/services/<domain>/<service>`、`brightness_pct`、`GET /api/states/<entity>`）：HA 开发者文档
- WS2812B 单灯 60mA：常见规格书数据
- `_handle_device_control` 在 181 行：读本项目源码确认

**判断（无客观数据，可被推翻）**：
- 三档价格区间（未实际下单）
- "P0 半天"这类工期估算
- "继电器贵 3 块值得买光耦版"、"先 Docker 后换迷你主机"这类取舍
- "控制屏放最后做"的优先级排序
