# jobpilot · 求职驾驶舱

本地运行的求职自动化程序（按 GitHub 高星方案重写，替代旧 jobbot）。
覆盖 **智联招聘 / 猎聘 / BOSS直聘** 三平台：初始化向导、简历导入与 AI 优化、
AI 采集计划、自动采集、按岗位 AI 定制简历、程序投递。数据存 **SQLite**，一键导出 Excel。

- 界面：`http://127.0.0.1:8765`
- 数据库：`jobpilot/data/jobs.db`
- 导出目录：`exports/`（默认在项目同级；可用 `local_config.py` 覆盖）

## 一、技术路线（2026-11 实测校正）

> 早期版本这一节写的是"按 GitHub 高星方案"，其中部分结论未经源码核对，已修正。
> 下面是**实际克隆源码核对 + 本机实测**后的结论。

### 关键事实：三平台都不存在"复制 Cookie 就能纯 HTTP 采集"

| 平台 | 实测结果 | 结论 |
|---|---|---|
| BOSS | `joblist.json` 第 1 次请求成功，**第 2 次立刻 `code=37`**。`__zp_stoken__` 每请求轮换，由账号专属混淆 JS（`/web/passport/zp/security-js/<rotating>.js`，实测 315KB）生成，掺 canvas/WebGL 指纹，服务端验签而非重算 | 无法离线复刻 |
| 智联 | 带 Cookie 请求搜索页只返回 2KB SPA 空壳，数据靠页面 XHR + `x-zp-*` 签名头 | 需页面上下文 |
| 猎聘 | 真实接口在 `api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job`，直连返回 `{"flag":0}`；headless 访问被重定向到 `safe.liepin.com/v/intercept/blankpage` | 需页面签名，且对 headless 极敏感 |

开源项目的实际做法（源码核对）：
- `loks666/get_jobs`（9.1k★）：**Java 21 + Spring Boot + Patchright(Playwright)**，全仓 0 个 Python 文件，无任何签名/加密代码；智联靠 DOM 选择器抓取，猎聘/BOSS 靠 `page.onResponse` 被动拦截页面自己发出的 JSON
- `boss-agent-cli`：源码注释明写 `High-risk: search, recommend, greet, job_card → browser channel`
- `BossZhipin_reverse`：README 立场"不解算法、不伪造指纹，用真实浏览器替你发请求"

### 本项目据此确定的路线

| 能力 | 做法 | 状态 |
|---|---|---|
| 登录态 | **自动解密本机 Chrome Cookie 库**（AES-128-CBC + macOS Keychain 的 `Chrome Safe Storage`），用户无需 F12 复制 | 已实测：一次读出三平台 Cookie |
| 采集 | 会话内接口：真实窗口移到屏幕外，页面自己发请求续令牌，**不点击不滚动** | 三平台实测跑通（2026-11-22）：智联 329 条 / 猎聘 40 条 / BOSS 30 条 |
| 投递 | BOSS 走页面内 `friend/add.json`；智联/猎聘走浏览器点击（立即投递 → 选简历 → 提交；仅直聊的岗位跳过） | 智联、猎聘均已实测投递成功（2026-11-22） |
| AI 筛选/定制/跟进 | 纯本地，零触达 | 已跑通 |
| 风控 | 任何风控信号 → 冷却 60 分钟 + 整单刹停；当日 2 次 → 自动关停平台 | 已多次触发验证有效 |

**浏览器引擎用 patchright，不用 Selenium（2026-11-22 实测结论）**：猎聘/BOSS 的页面脚本会检查
`window.cdc_adoQpoasnfa76pfcZLmcfl_*`（ChromeDriver 注入的指纹），且它比任何页面脚本都早注入——
Selenium 侧无法在页面执行前清掉，结果是猎聘把页面清成 `about:blank`、BOSS 连续跳安全验证。
patchright（打补丁的 Playwright，`get_jobs` 9.1k★ 同款）不发这些变量，同一配置目录、同一登录态下
猎聘/BOSS 都能稳定出结果。业务层方法签名不变，`rt.d` 保留兼容壳，智联链路未改动。

**未验证的部分明确标注为"待验证"，不当作既成事实。**

## 二、安装与启动

```bash
./run.sh
```

自动建 `.venv`、装依赖（flask patchright selenium requests openpyxl pypdf pytest，清华源）、初始化数据库并打开页面。

**个人配置（可选）**：复制 `local_config.example.py` 为 `local_config.py`（已被 `.gitignore` 忽略），可改导出目录、各平台登录后显示的账号名；不配置也能用。

**两种模式：**

- 离线模式（默认）：`./run.sh`。不会访问任何招聘网站，可以浏览界面、导入简历、
  配置 AI、生成采集计划；点采集/投递会明确提示"离线模式未执行"。
- 在线模式（实测用）：`JOBPILOT_ALLOW_LIVE=1 ./run.sh`。才允许启动自动化 Chrome
  真实访问三平台。**每次实测请单平台小量。**

## 三、初始化向导（3 步）

前提：已有三平台账号 + 一个 OpenAI 兼容 AI 账号（DeepSeek/通义/智谱均可）。

1. **AI 账号**：Base URL / 模型 / API Key，保存并测试连接
2. **导入简历**：上传 PDF/DOCX/HTML/TXT 或粘贴全文 → AI 结构化（只搬运不编造）
3. **登录与计划**：检测三平台登录（登录发生在弹出的专用 Chrome 窗口，扫码一次长期有效）；
   AI 生成采集计划（默认不启用，去「采集计划」页勾选启用后保存）

## 四、页面说明（9 页签）

- **总览**：三平台「允许自动访问」开关与冷却倒计时、**采集选项…**（弹窗指定平台/关键词/城市/页数）、有效性检测/清除失效、运行日志（登录检测与打开窗口已收进「设置 → 账号连接」，任务前会自动预检）
- **AI 助手**：说目标 → AI 自己决定调哪些工具（见第五节）；实时时间线；写操作弹确认卡片
- **岗位库**：关键词/平台/状态筛选 + **AI 判定下拉多选**（强匹配/匹配/弱匹配/不匹配/待判，可叠加状态筛选）、**默认按 AI 判定优先排序**（同档再按星和规则分）、行级「读JD/定制/投递/不投/打开」、**勾选多行 → 投递选中**、投递全部待投、AI 判定列、当前岗位条
- **简历**：JSON 编辑（含结构化字段）+ A4 实时预览 + 导出 PDF
- **AI 优化**：**只看建议**（分节：原文问题/建议/理由，可复制，不改简历）与**生成优化版简历**（新版 + 逐条改动对照 + 防编造校验 + 预览；可「采用为新简历」（旧版自动存版本历史）/ 导出 PDF / 放弃）
- **采集计划**：AI 生成 + 手工增删改 + 启用开关；采集按启用行执行，单平台单次 ≤30 页预算自动分配
- **投递与跟进**：**今日投递清单**（按 AI 匹配度排序、批量定制、导出 Markdown）、**跟进看板**（已投递→已读→HR有回复→面试→Offer/婉拒）、**AI 回复助手**（贴 HR 原话 → 判意图 + 生成草稿，不代发）；机制 B/C/D 与投递记录折叠在底部
- **设置**：AI 接口（含 Key 完整性校验）、**账号连接**（一键读取浏览器 Cookie）、薪资底线、冷却时长、BOSS 日限、Cookie 备份/恢复、重跑向导
- **日志**：events 审计（风控触发/冷却/机制执行/参数变更/agent 确认）

## 五、AI Agent 架构（谁做决策）

**旧架构（程序主导）**：用户点按钮 → Flask 路由 → `_task_collect()` 里写死"采集→规则→打分"。
AI 只是流程中一个被调用的函数，顺序由代码决定。

**现架构（AI 主导）**：用户说目标 → 模型自己决定调哪个工具、什么顺序、要不要再来一轮。
程序退化为「工具箱 + 安全闸门」。

```
用户目标
   ↓
agent.py  ← SYSTEM_PROMPT（用户背景 + 工具纪律 + 输出要求）
   ↓ 循环：思考 → 选工具 → 执行 → 观察 → 再思考
gate()  ← 闸门：按风险级决定 放行 / 拒绝 / 挂起
   ↓
tools.py  ← 16 个工具
```

### 三级风险（账号安全的唯一防线）

| 级别 | 数量 | 闸门行为 | 工具 |
|---|---|---|---|
| `local` 零触达 | 13 | **无条件放行** | get_context, list_jobs, get_apply_list, score_jobs, tailor_resume, set_job_status, add_blacklist, update_pipeline, reply_assist, export_apply_list, review_plan, save_plan, run_rules |
| `read` 只读触达 | 2 | 在线闸 + 平台开关 + 冷却 + 配额；**被拒时把原因回传给模型让它改策略**，不崩 | search_jobs, read_jd |
| `write` 写触达 | 1 | **模型无权执行**，一律挂起等人工点「同意」 | apply_job |

未注册的工具名默认按 `write` 处理，防止拼错名字绕过闸门。

### 防跑飞

- 步数上限 25（可调 3–40）
- 连续 4 步无实质进展 → 主动收敛
- 消息历史超 40 条 → 保 system + 裁剪旧工具结果
- 单个工具结果回传上限 2200 字符
- Agent 运行期间与普通任务互斥（`_run_task` 会拒绝启动）

### 相关接口

`GET /api/agent/tools`（能力清单）、`POST /api/agent/run`、`GET /api/agent/events?since=N`、
`GET /api/agent/state`（含 pending 详情与岗位信息）、`POST /api/agent/confirm`、`POST /api/agent/stop`

## 六、风控与安全（内置，不可绕过）

- `risk.check(platform)` 是每个采集/投递/读JD/Cookie 入口的第一行；冷却期内一律拒绝。
- 任何**空白页、安全验证、验证码、频繁类文案**：立即 `trip` → 60 分钟冷却 + 终止整个任务，
  **不重试、不换 URL、不回退搜索页**（这是旧版封号的直接教训）。当日两次 → 平台开关自动归零，需人工恢复。
- **异常刹停分级**：采集遇到风控拦截/浏览器启动失败/离线闸 → 整个任务立即停止；普通异常（解析、网络抖动）→
  日志报 `!! 平台 采集异常，已跳过该平台继续其余`，不影响其余平台。投递单条异常 → 日志报异常、该条标"待复查"后继续；
  风控/冷却 → 剩余全部停止。每平台采完立刻跑规则+AI 判定，后续异常不丢已完成部分的判定。
- BOSS：总开关默认关；API 日配额默认 60 次、单轮 10 页、投递日限 10（首轮实测建议手动调到 2）。
  详情页只用**采集时入库的新鲜 securityId** 拼链接；跳回列表=标记"链接过期"，等重新采集。
- 猎聘：`投简历 → 立即投递`；无入口只点 `打招呼`；**任何情况下不输入追发消息**。
- 聊天类机制（黑名单学习/推荐清洗/已读不回复聊）：默认全关，同一天只允许运行一种，各有日上限。
- 服务器/代理 IP 会被平台直接识别，不要挂代理、不要部署到云服务器。
- **诚实声明**：自动化投递始终违反平台用户协议相关条款，存在封号风险。主渠道仍建议手机 App
  人工沟通，本程序作为批量兜底。重要账号建议先小量试运行。

## 七、数据与文件

| 文件 | 说明 |
|---|---|
| `data/jobs.db` | 唯一数据源（jobs/apps/blacklist/plan/settings/quota/cooldown/events…），启动自动建，每日滚动备份 `jobs.db.bak` |
| `data/cookies.json` | 三平台 Cookie 备份，**含登录凭证，勿外传** |
| `exports/jobpilot-岗位-*.xlsx` | 一键导出（岗位/投递记录/黑名单三表） |
| `exports/*.html/.pdf` | 简历与定制版导出 |

`jobs.status` 生命周期：`new →(规则判定)→ 待投/待定-薪资未标/保留-低于底线(总监级)/不投-*；投递→已投；失效→已关闭`。
去重按 URL 路径（BOSS 的 securityId 变化不会重复入库，但会刷新库存 token）。

## 八、常用接口（调试）

`GET /api/state` `GET /api/jobs` `POST /api/collect|apply|checkjobs|precheck` `POST /api/job/jd`
`POST /api/tailor` `GET/POST /api/plan|settings|ai/settings|resume` `POST /api/boss/toggle`
`POST /api/cookies/export|restore` `POST /api/chat/scan|cleanse|followup` `POST /api/export/xlsx` `GET /api/events`
`POST /api/agent/run|confirm|stop` `GET /api/agent/state|events|tools`
`GET /api/pipeline|applylist` `POST /api/pipeline|ai/reply|tailor/batch|applylist/export`
`GET /api/net` `POST /api/net/connect|channel|cookie|test|login|autorecord`

## 九、测试

```bash
cd jobpilot && ./.venv/bin/python -m pytest tests/ -q   # 72 项，全部离线
```

覆盖：BOSS API 解析/入库去重/风控触发冷却、规则引擎（20K硬过滤、珠中总监级例外）、
risk 状态机、AI 防编造校验、计划校验、Flask API、离线硬闸、
**Agent 闸门（local 放行 / read 拦截 / write 必挂起）、无进展收敛、步数上限、拒绝后回传原因**。

## 十、常见问题

**Q1 点采集提示"离线模式"？** 用 `JOBPILOT_ALLOW_LIVE=1 ./run.sh` 重启。
**Q2 BOSS 一直"开关关闭"？** 总览勾选「允许自动访问 BOSS」（自动保存）；若当日触发过两次风控，需手动再勾。
**Q3 智联检测时好时坏？** 探针已改为"必须停在 zhaopin.com + 头部出现账号名 + 慢页面自动重进一轮"；仍失败就到「设置 → 账号连接」重新登录一次。
**Q4 老 jobbot 的 Excel 还要吗？** jobpilot 不读写旧表；新程序数据在 SQLite，导出是另一份带日期的新文件。
**Q5 Cookie 备份有什么用？** 配置目录被清理/换电脑后点「恢复Cookie」免扫码；但服务端会话过期仍需重扫。
**Q6 简历只能在初始化时导入一次？** 不是。①「简历」页顶部可随时"上传文件/粘贴全文 → AI 重新结构化并覆盖"（有二次确认）；②下方 JSON 编辑器随时手工修改保存；③设置页还可重跑初始化向导。任何时刻的采集计划、AI 优化、按岗定制用的都是简历表里的最新版。
**Q7 AI 报 `401 Invalid API-key`？** 多半是 Key 存残了（界面回显的是 `sk-xxx…` 脱敏值，若误当真实值保存就会写坏）。到「设置」页重新**完整粘贴** Key 并点「保存并测试」。现在 Key 短于 16 字符会被当场拒绝，AI 助手页顶部也会红字提示"AI 接口当前不可用"。
**Q8 AI 助手点了没反应？** 先看页面顶部有无红字警告（AI 未配置/Key 无效）；再看「执行时间线」——每步的思考、工具调用、闸门拦截都会实时列出来。若显示"闸门拦下"，说明该平台在冷却或开关关闭，属正常保护。
**Q9 猎聘/BOSS 采不到数据？** 这两个平台的令牌由页面 JS 现场生成且随请求轮换（BOSS 第 2 次请求即 `code=37`），实测无法纯 HTTP 复刻；headless 会被猎聘踢到 `safe.liepin.com` 拦截页。当前只保证智联稳定；BOSS/猎聘请用「投递清单 + 手动投递」，或等账号恢复后做一次极低频验证。
**Q10 怎么让程序不碰某个平台？** 「设置 → 账号连接」把该平台开关关掉，或让风控自动关停（当日 2 次触发即自动置 0）。Agent 的闸门会读这个开关，`read`/`write` 工具一律拒绝。
