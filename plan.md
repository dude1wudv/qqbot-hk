# Hermes QQ 群聊 / 好友机器人复刻执行计划

## 目标与边界

目标是在现有 `qqbot-hk` Hermes 部署上复刻 `Smart_Group_Bot` 的核心思路，而不是移植其 Telegram/aiogram 实现：

- QQ 好友继续使用 Hermes 原生 C2C 与 pairing；可开启有明确 UTC 截止时间的临时自动配对登记窗口。
- QQ 群仅开放给明确配置的群，使用 Hermes 原生 `GROUP_AT_MESSAGE_CREATE` 群 @ 入口。
- 以 Hermes 原生会话保存机器人可见的群聊上下文；同一群共享会话，不把好友私聊或其他群的历史带入。
- 增加专门的群聊人格、关键词自动回复、轻量内容审核、审计与群会话管理。
- 兼容常见 `/` 指令（如 `/help`、`/reset`、`/status`），自动剥离 QQ 客户端的 `@` 标签并做本地短路。
- 提供 QQ 平台友好的发消息排版格式化（Markdown 降级渲染、代码块美化、长消息分片分条）。
- 实现群聊记忆管理与自动摘要整理，平衡多人会话上下文一致性与模型 Token 消耗。
- 保留安全的工具调用；不向 QQ 用户暴露 terminal/file 等容器写入与执行工具。

### 官方能力边界与非伪装实现声明

根据 QQ 官方开放平台（`bot.q.qq.com`）与官方 SDK（`botpy`）现行技术规范：
1. **群聊无非 @ 消息事件（不支持真正的群聊不 @ 智能回复）**：
   - 官方网关对群聊仅开放 `GROUP_AT_MESSAGE_CREATE` 事件。官方在规范中对“群聊全量消息（私域）”和“群聊关键词消息”均明确标注为「暂不开放」。
   - 机器人网关物理上根本无法接收群内未 @ 机器人的闲聊，因此**无法做到像 Telegram 那样无 @ 旁听群聊并自主插嘴回复**。
   - **合理替代方案**：通过“被动会话内的多阶段交互与智能追问”（利用 5 分钟内同一 `msg_id` 最多 5 次回复）、管理员触发的定向广播、以及群成员 @ 机器人请求 `/summary` 整理群讨论等方式提供“主动感知”。
2. **主动推送能力已下线**：
   - 官方已于 2025 年 4 月 21 日起全面停止提供无事件上下文的主动消息推送能力；即便此前群主动消息也仅为每月 4 条。
   - 因此，**原计划中的每日定点主动群发定时任务（Cron）在 QQ 官方群聊上不可行**，强行调用会触发 API 错误（如 `22009 msg limit exceed` 或接口下线报错）。本计划将定时群发调整为实验性/私信频道专用，默认在 QQ 群上关闭。
3. **群管理能力不存在**：
   - 官方未开放群成员进退群事件、撤回消息、禁言、踢人等管理接口。“阻断”仅定义为：插件不把违规内容提交给 Agent，并在群内回复一条警告提示。
4. **被动回复时效限制**：
   - 群聊被动回复必须携带入站消息的 `msg_id`，且必须在 **5 分钟内** 完成，每条消息最多回复 5 次，必须配合递增的 `msg_seq`（1..5）。若模型推理或工具执行超时，回复将被平台拒绝。
## 现状依据与设计决策

### Smart_Group_Bot 中要复用的思路

- `bot/handlers/group.py:on_group_message`：先鉴权、记忆归档和规则处理，再进入 AI 决策/回复；关键词命中会短路 AI 管线。
- `bot/services/keyword_reply.py:match_keyword_reply`：确定性规则优先，避免不必要的模型调用。
- `bot/services/moderation.py:check_content`：静态规则与 LLM 审核分层，并把执行结果持久化。
- `bot/__main__.py:register_update_middlewares`：治理能力位于统一入站管线，而不是散落到业务 handler。
- SQLite/WAL、幂等键、后台任务清理和行为测试是可复用的可靠性做法。

### Hermes 已有能力

- `gateway/platforms/qqbot/adapter.py:QQBotAdapter` 已实现 QQ 官方 Bot API v2 的 C2C、群 @、引用/附件/STT、文本及媒体发送、ACL 和重连；无需 fork 或复制 QQ 适配器。
- `gateway/run.py:GatewayRunner._handle_message` 在 auth/pairing 和 Agent dispatch 前触发 `pre_gateway_dispatch`；插件可返回 `skip/rewrite/allow`，适合关键词和审核短路。
- Hermes 的 `SOUL.md` 是人格入口；`group_sessions_per_user=false` 可保持每个 QQ 群一个共享 Agent 会话。
- `platform_toolsets.qqbot` 可对 QQ 表面显式缩减工具集。
- `cron.jobs.create_job/update_job/remove_job` 支持 `no_agent` 脚本任务和 `qqbot:<chat_id>` 定向投递；适合固定文案群发，无需浪费 LLM 调用。
- `plugins.plugin_storage.plugin_db()` 提供 profile-scoped、WAL 模式的持久 SQLite，插件更新/删除不会误删运行状态。

### 核心决策

1. **不 fork Hermes，不移植 aiogram。** 新增一个 Hermes native plugin，挂在 `pre_gateway_dispatch`。
2. **群白名单只保留一个权威来源。** 使用 `/opt/qqbot-hk-deploy/secrets/qqbot.env` 中的 `QQ_GROUP_ALLOWED_USERS`；Hermes QQ adapter 在产生 `MessageEvent` 前已按此值 fail-closed。插件不再维护第二份群白名单。
3. **私聊 pairing，群聊 allowlist。** `dm_policy: pairing`；临时登记窗口只在 `pre_gateway_dispatch` 中批准 QQ 私聊发送者，过期后自动失效。不得设置 `QQ_ALLOW_ALL_USERS=true` 绕过 pairing。`group_policy: allowlist` 独立使用群 OpenID 白名单。
4. **同群共享记忆与发言人归一化。** 显式设置 `gateway.group_sessions_per_user: false`；群与好友的 session key 仍由 Hermes 分隔。在入站消息前缀标准化注入 `[成员:<member_openid_short>]`，让模型准确理解多人对话脉络。
5. **指令清洗与本地短路。** 在 `pre_gateway_dispatch` 钩子中剥离 `@机器人` 占位符；对 `/reset`、`/clear`、`/help`、`/status` 等常用指令在插件层就地短路处理，无需唤醒 Agent 模型。
6. **发消息格式优雅降级与智能分片。** 针对未获官方 Markdown 内邀的现状（`markdown_support: false`），设计纯文本排版美化器，将 Markdown 标题、代码块、列表、引用转换为 QQ 纯文本可读排版；单条消息超长时按段落拆分，利用同一 `msg_id` 的 `msg_seq` 连续发送。
7. **群记忆管理与摘要整理。** 实现基于交互轮数与空闲时长的滑动窗口记忆压缩；支持群成员使用 `/summary` 生成群内互动脉络，支持管理员使用 `/reset` 清理污染上下文。
8. **审核静态优先、语义可选。** 静态关键词/正则同步完成；只有未命中静态规则时才可进入 `ctx.llm.complete_structured(..., timeout=5)`。语义审核初始配置关闭。
9. **定时群发降级与受限。** 识别 QQ 官方主动推送限制，cron 仅作为非群聊场景（或未来协议演进）的基础设施，不作为 QQ 群日常依赖。
10. **最小权限工具集。** QQ 仅启用 `[web, vision, skills, todo]`；关闭终端、文件写入等高危工具。
## 目标文件布局

```text
qqbot-hk/
├── config/
│   ├── hermes-config.yaml                 # 原有配置，追加 QQ/插件/工具/时区设置
│   ├── SOUL.md                            # 可提交的人格与群聊隐私边界
│   └── scheduled-messages.yaml            # 计划任务配置（默认禁用群主动推送）
├── plugins/
│   └── smart_group_qq/
│       ├── plugin.yaml                    # hooks + settings schema
│       ├── __init__.py                    # register(ctx)、pre_gateway_dispatch 入站管线
│       ├── policy.py                      # 规则归一化、静态/语义审核决策
│       ├── commands.py                    # / 命令解析、清洗与本地短路（/help, /reset, /status）
│       ├── formatter.py                   # QQ 纯文本排版美化器与长消息分片
│       ├── memory.py                      # 群成员说话人标识、会话滑动窗口与摘要整理
│       └── store.py                       # 幂等 claim、审计、发送状态与群记忆存储
├── scripts/
│   ├── reconcile-smart-group-cron.py      # cron 声明式对账（受限/可选）
│   ├── install-server.sh                  # 安装 SOUL/plugin/schedules/reconciler
│   └── verify-server.sh                   # 配置、插件、探针验证
├── tests/
│   ├── test_smart_group_qq.py             # 核心策略、审核与指令测试
│   ├── test_formatter.py                  # 消息排版与分片测试
│   ├── test_memory.py                     # 记忆管理与说话人注入测试
│   └── test_smart_group_cron.py           # cron 对账逻辑测试
├── .env.example
└── README.md
```

## 实施步骤

### 1. 收紧 QQ 接入与工具边界

修改 `qqbot-hk/config/hermes-config.yaml`：

- 将 `platforms.qqbot.extra.dm_policy` 设为 `pairing`；自动配对必须显式配置 UTC 截止时间，且只适用于 QQ 私聊。
- 将 `platforms.qqbot.extra.group_policy` 从 `pairing` 改为 `allowlist`。
- 保持 `markdown_support: false`。
- 新增 `gateway.group_sessions_per_user: false`，使同一群的机器人可见对话共享上下文；不得设为 `true`，否则会按发言人切碎群记忆。
- 新增 `timezone: Asia/Hong_Kong`，使 cron 表达式有明确时区。
- 新增 `platform_toolsets.qqbot: [web, vision, skills, todo]`。
- 新增 `plugins.enabled: [smart_group_qq]` 及 `plugins.entries.smart_group_qq.settings`：
  - `keyword_replies`: 有稳定 `id`、`match` (`exact|contains|regex`)、`pattern`、`reply`、`enabled`；按声明顺序首个命中获胜。
  - `moderation.static_rules`: 有稳定 `id`、`pattern`、`match`、`notice`、`enabled`。
  - `moderation.semantic.enabled: false`、`timeout_seconds: 5`、`min_confidence: 0.92`、`notice`。
  - 所有 reply/notice 限长到 QQ adapter 单条消息上限以内；配置加载时拒绝空 id、重复 id、非法 regex 和未知 match 类型，插件整体 fail-open 但记录配置错误。
- 若开启语义审核，为 `plugins.entries.smart_group_qq.llm` 只授予当前 provider/model 的精确 allowlist；不允许 profile/agent 任意 override。

修改 `qqbot-hk/.env.example`：新增 `QQ_GROUP_ALLOWED_USERS=replace-with-comma-separated-group-openids`。不放真实 OpenID。

修改 `qqbot-hk/scripts/install-server.sh`：

- 把 `QQ_GROUP_ALLOWED_USERS` 加入 `qqbot.env` 必填校验，并原样写入 `/opt/data/.env`；空值直接中止，不能回退为 `*`。
- 不改变现有 `QQ_APP_ID`、`QQ_CLIENT_SECRET`、`SUB2API_API_KEY` 的秘密处理。

### 2. 固化人格与隐私约束

新增 `qqbot-hk/config/SOUL.md`，保持短、稳定、无部署秘密，至少包含：

- 中文群聊人格、简洁程度和称呼规则；好友聊天可更完整，群聊优先短答。
- 群聊是公开表面：禁止引用/总结/暗示好友私聊、其他群、服务器秘密、凭据或内部日志。
- 不声称拥有撤回、禁言、踢人、封禁等实际不存在的权限。
- 使用工具前先判断必要性；群里只回传结论和可公开来源，不回传内部命令、路径或调试输出。
- 关键词/审核由系统插件决定；Agent 不绕过、不与其争辩。

`install-server.sh` 使用原子替换把它安装为 `/opt/data/SOUL.md`，归属 `10000:10000`。部署后必须新建测试 session 或 `/reset`；已有 session 的已持久化 system prompt 不应被误当作已刷新。

### 3. 实现 `smart_group_qq` 核心治理与扩展插件

#### 3.1 插件定义与入站管线 (`__init__.py`, `plugin.yaml`)

- `plugin.yaml` 声明 `name: smart_group_qq`、`hooks: [pre_gateway_dispatch]`。
- `register(ctx)` 初始化 SQLite store、编译规则、绑定命令处理器与格式化器。
- `pre_gateway_dispatch` 执行流水线：
  1. **平台与会话类型过滤**：非 `qqbot` 或非 `group` 事件直接 `allow`（私聊行为不变）。
  2. **文本清洗与说话人规范化**：
     - 剥离 QQ 消息中的 `<@!openid>` 占位符和机器人名称前缀。
     - 在消息文本前注入说话人标识：`[群成员:{member_openid[:6]}]: {clean_text}`，以便 Agent 区分群成员。
  3. **命令识别与短路 (`commands.py`)**：
     - 识别清洗后的 `/help`、`/reset`、`/clear`、`/status`、`/summary`。
     - 命中内置命令直接就地执行并调用 QQ adapter 回复，返回 `{"action": "skip", "reason": "command_handled"}`，零 LLM 消耗。
  4. **内容审核流水线 (`policy.py`)**：
     - 静态规则匹配（敏感词/正则）：命中则发送警告，阻断消息进入 Agent。
     - 可选语义审核（LLM）：仅在开启时执行，超时/失败 fail-open。
  5. **关键词自动回复 (`policy.py`)**：
     - 命中关键词预设模板：发送固定回复，短路 Agent。
  6. **记忆更新与透传 (`memory.py`)**：
     - 记录会话轮次，检查记忆压缩触发条件。
     - 审核与规则放行后返回 `{"action": "rewrite", "text": normalized_text}`，交给 Hermes Agent 正常推理。

#### 3.2 `/` 命令兼容与指令系统 (`commands.py`)

- **前缀自适应解析**：兼容 `@机器人 /cmd`、`/cmd @机器人`、中英文斜杠（`/`、`／`）、无空格或多空格。
- **支持指令集**：
  - `/help`：返回机器人在本群的功能指南、可用工具、指令清单（卡片化纯文本排版）。
  - `/reset` 或 `/clear`：重置当前群聊共享记忆上下文（清理历史消息缓存），并向群内回复清爽的重置提示。
  - `/status`：展示机器人当前主模型、推理级别、白名单群状态、运行健康度。
  - `/summary`：调取本群近期对话（由 `memory.py` 归档），调用轻量模型生成当前讨论热点与知识摘要。
  - `/rules`：输出群内已启用的关键词和交互规则。

#### 3.3 消息格式优化与优雅降级 (`formatter.py`)

针对 QQ 客户端不支持未授权 Markdown 的限制，建立纯文本增强排版引擎：
- **视觉层级重构**：
  - 标题降级：`# 标题` → `【 标题 】`；`## 二级标题` → `📌 二级标题`；`### 三级` → `▫️ 三级`。
  - 强调转换：`**加粗文本**` → `「加粗文本」`。
  - 列表规整：`- 列表项` 或 `* 列表项` → `• 列表项`；数字列表 `1. ` 保持对齐。
  - 引用块美化：`> 引用内容` → `▎ 引用内容`。
  - 链接格式化：`[描述](URL)` → `描述 (URL)`。
  - 代码块美化：将 ```` ```python ... ``` ```` 封装为清晰带边框的纯文本代码卡片（如 `┌── [python] ──\n...\n└──`）。
- **超长消息分片投递（Chunking）**：
  - QQ 文本单条消息建议在 1000~1500 字符内，避免手机端折叠或被平台拒绝。
  - 消息超过阈值时，按段落和换行平滑切片。
  - 充分利用 QQ 官方协议特性：**一条被动消息（msg_id）在 5 分钟内最多回复 5 次**，分片时递增 `msg_seq=1, 2, ...` 进行连续有序发送。
- **Markdown 白名单兼容**：当未来 `markdown_support: true` 时，无缝直通 QQ 官方 Markdown 格式并进行合法性校验。

#### 3.4 群记忆管理与整理 (`memory.py`)

- **多人对话解耦**：群聊是多对一交互，Hermes 单群共享会话时容易产生主体混淆。通过给每条入站消息打上 `member_openid` 简短指纹标签，让模型清晰分辨说话人。
- **上下文滑动窗口与记忆整理（Compaction）**：
  - 在 SQLite 中记录群聊交互流水 `group_history`。
  - 设定滑动窗口（如保留最近 20 轮互动）。
  - 当群聊上下文超过设定长度，或群内出现较长空闲（如 30 分钟无新消息）后再次被唤醒时，自动启动异步记忆整理任务：
    - 提取历史对话中的事实、共识、用户偏好和进行中的任务。
    - 压缩为 200 字以内的「群背景摘要」。
    - 将摘要注入系统提示词开头，重置过往冗余聊天行，极大降低后续 token 开销并防止模型幻觉漂移。

#### 3.5 规则与审核引擎 (`policy.py`)

- 不可变 `Decision(action, rule_id, notice, confidence, source)`。
- `normalize_text()`：Unicode NFKC、折叠空白。
- `match_static_moderation()`：正则与关键词树匹配，审核优先级最高。
- `semantic_moderation()`：`ctx.llm.complete_structured`，JSON 规范 `{action, confidence, category}`。

#### 3.6 状态与审计存储 (`store.py`)

- WAL 模式持久 SQLite。
- `message_claims`：`(platform, message_id, action)` 幂等防重。
- `group_memories`：保存群专属长期摘要与记忆配置。
- `audit_events`：安全审计日志，严禁记录原消息与私密内容。

#### 3.7 针对“群聊主动回复（不@）”的合规实现边界

由于官方技术限制，机器人无法旁听无 @ 消息。为提升群内“主动感”，实现以下三个合规特性：
1. **被动多段推进（Follow-up Insights）**：在被 @ 触发后的 5 分钟窗口内，回答完成后，若有延伸知识点，递增 `msg_seq` 追加一条“💡 延伸提示”，形成自然追问。
2. **定时触发的被动激活**：若群友在当日有过互动并开启了提醒订阅，利用被动有效窗口或私聊通道下发通知。
3. **群活跃度看板**：群成员随时可通过 `@机器人 /summary` 让机器人主动汇报当前讨论焦点。
### 4. 实现声明式定时群发

新增 `qqbot-hk/config/scheduled-messages.yaml`：

```yaml
schedules:
  - id: daily-reminder
    enabled: false
    cron: "0 9 * * *"
    target: allowed_groups
    text: "待替换的固定群公告"
```

约束：

- `id` 唯一且只允许 `[a-z0-9_-]`；`target` 本轮只接受 `allowed_groups`。
- `text` 非空并受长度上限；不支持模板执行、shell、URL 抓取或 LLM 生成。
- 初始示例禁用，只有替换真实文案并审阅后才启用。

新增 `qqbot-hk/scripts/reconcile-smart-group-cron.py`：

- 从 `/opt/data/smart-group-schedules.yaml` 读取计划，从 `/opt/data/.env` 读取 `QQ_GROUP_ALLOWED_USERS`；绝不打印 OpenID 或消息正文。
- 对每个 enabled schedule × allowed group 生成稳定 job name：`smart-group-qq::<schedule-id>::<sha256(group_openid)[:12]>`。
- 在 `/opt/data/scripts/generated/` 为每个 job 原子生成一个只向 stdout 打印固定文案的 Python 脚本；脚本名同样只含 schedule id 和目标哈希，不含 group OpenID。
- 用 `cron.jobs.list_jobs(include_disabled=True)`、`create_job(..., no_agent=True, deliver="qqbot:<group_openid>", script=<relative generated path>)`、`update_job()` 对账。
- 只更新/删除 `smart-group-qq::` 前缀任务；禁用/删除声明时移除对应 owned job 和生成脚本，绝不触碰用户手工 cron。
- 相同输入重复运行结果不变；计划、文案、目标或 cron 改动走 update，不产生重复任务。

修改 `install-server.sh`：

1. 在启动容器前，把插件目录原子同步到 `/opt/data/plugins/smart_group_qq`，把 schedule YAML 和 reconciler 安装到 `/opt/data` 对应目录；安装树与 data 均归属 UID/GID 10000。
2. `docker compose up -d --remove-orphans` 后等待 health ready，再执行容器内 reconciler。
3. reconciler 失败必须使安装失败；不能留下“服务已启动但 cron 未更新”的成功输出。

### 5. 测试

新增 `qqbot-hk/tests/test_smart_group_qq.py`，覆盖可观察契约：

- 私聊和非 QQ 事件完全透传。
- 静态审核先于关键词回复；命中返回 skip 并仅发送一次提示。
- exact/contains/regex 顺序和 Unicode 归一化。
- 重复 QQ message id 不重复发送；failed/pending-stale 可重试。
- 关键词旁路发送失败时 allow；审核旁路发送失败时仍 skip。
- 语义审核阈值边界；关闭时零 LLM 调用；超时/异常/坏 JSON fail-open。
- SQLite 审计不含原消息和回复正文。
- fake adapter 接收到 chat_id、reply_to 和纯文本内容，真实 adapter 不被 monkeypatch 到源码。

新增 `qqbot-hk/tests/test_smart_group_cron.py`：

- 相同声明二次 reconcile 零新增。
- cron/text/群集合变化只产生预期 update/create/remove。
- 只删除 owned prefix；其他 Hermes cron 原样保留。
- 空白名单、重复 id、非法 cron、非法 target、超长/空正文均 fail-closed。
- 生成脚本 stdout 与配置文案逐字一致，文件名/日志不泄露 group OpenID。

新增 `qqbot-hk/tests/test_commands.py`：

- 各种 @ 标签格式与 `/help`、`/reset`、`/clear`、`/status` 提取识别。
- 本地命令就地短路返回 `skip`，不进入 Agent。
- 非命令或未识别命令正常透传或 rewrite。

新增 `qqbot-hk/tests/test_formatter.py`：

- Markdown 标题、粗体、列表、引用平滑降级为 QQ 纯文本可读排版。
- 超长文本基于段落智能切片，生成递增 `msg_seq` 的分片结果。
- 代码块纯文本边框封装正确。

新增 `qqbot-hk/tests/test_memory.py`：

- 多成员入站消息的 `member_openid` 标签注入与还原。
- 会话滑动窗口达到上限时触发记忆摘要生成。
- `/reset` 指令对群记忆的清理边界隔离（只清本群，不影响其他群与私聊）。
测试在固定 Hermes 镜像中运行，避免本仓库另造一套 Hermes stub API。最小命令：

```bash
docker run --rm \
  -v "$PWD/plugins:/opt/data/plugins:ro" \
  -v "$PWD/scripts:/work/scripts:ro" \
  -v "$PWD/tests:/work/tests:ro" \
  nousresearch/hermes-agent@sha256:76d5d17a201bb623268c02d43e397925e8f0127eb29b2e00fc48632d74945b05 \
  python -m unittest discover -s /work/tests -v
```

如镜像入口会干扰命令，执行实现时显式加 `--entrypoint python`，并以镜像实际模块路径为准；不能改为只测试复制出来的伪接口。

### 6. 扩展部署验证

修改 `qqbot-hk/scripts/verify-server.sh`，保留现有三模型真实探针，并新增：

- `/opt/data/SOUL.md`、plugin manifest/code、schedule YAML、reconciler 存在且归属 10000；不输出内容。
- `/opt/data/.env` 中 `QQ_GROUP_ALLOWED_USERS` 存在且非空、`QQ_SANDBOX=true`，并且未启用 `QQ_ALLOW_ALL_USERS`；部署态 `dm_policy=pairing`、`group_policy=allowlist`。只输出策略、布尔值和数量，不输出真实值。
- `hermes config check` 成功；plugin manager 报告 `smart_group_qq` enabled/loaded。
- 直接查询 `cron.jobs.list_jobs(include_disabled=True)`，确认 expected owned job 数 = enabled schedules × unique allowed groups，name 唯一、`no_agent=true`、delivery platform 为 qqbot；只输出计数和 schedule id。
- SQLite schema/`PRAGMA integrity_check` 成功；只输出状态和行数。

本地通过后，再按生产边界执行：

1. 根仓库和 `qqbot-hk` 分别确认 `git status --short` / `git log -1 --oneline`；在 `qqbot-hk` 提交并推送精确 SHA。
2. 通过 `ssh -o BatchMode=yes hk` 在 `/opt/qqbot-hk` 获取该 SHA；部署前备份 `/opt/qqbot-hk-deploy/hermes-data/{config.yaml,SOUL.md,plugins,cron}` 和 Compose。
3. 在 `/opt/qqbot-hk-deploy/secrets/qqbot.env` 配置真实 `QQ_GROUP_ALLOWED_USERS`；不写入 Git、命令行参数或日志。
4. 运行 `scripts/install-server.sh`，只重建/重启 `hermes-qqbot`；不触碰 Sub2API/Postgres/Redis/Caddy。
5. 运行 `scripts/verify-server.sh`。
6. 执行真实 QQ 验收矩阵：
   - QQ 开放平台“开发体验号码”名单中的新用户，在有效登记窗口内首次私聊会自动 pairing 并直接问答；窗口过期后的新用户恢复标准配对码。
   - 白名单群 @ 机器人可正常问答；连续两位群成员 @ 后由同一群 session 保持上下文。
   - 非白名单群 @ 无回复，gateway 日志显示 adapter ACL 拒绝但不泄露内容。
   - 静态审核样例：原消息不进入 Agent，群内收到一次提示；重放同 message id 不重复提示。
   - 关键词样例：收到一次固定回复，模型调用计数不增加。
   - 若决定开启语义审核：先在单一测试群开启，验证 allow/block/阈值/超时，再扩到全部白名单群。
   - `hermes cron run <owned-job-name>` 强制触发一条固定公告；目标群只收到一次，Hermes execution ledger 为 success。
7. 观察至少一个 QQ WebSocket reconnect 周期，确认 reconnect 后沙箱私聊边界、群白名单、plugin 和 cron 仍有效。

## 回滚

- 应用回滚到上一精确镜像/提交 SHA。
- 恢复部署前的 `config.yaml`、`SOUL.md` 和 plugin 目录备份；运行 install 使容器重新加载。
- 运行 reconciler 前先把所有 schedule 设为 disabled，或由回滚脚本仅删除 `smart-group-qq::` owned jobs；不得整体覆盖 `cron/jobs.json`。
- 保留 `plugin-data/smart_group_qq/data.db` 作为审计事实；除非明确授权，不在回滚中删除。
- 重新运行现有模型探针、config/doctor、容器 health，以及好友/白名单群最小 smoke test。

## Critical files

1. `qqbot-hk/config/hermes-config.yaml` — QQ ACL、共享群 session、工具集、插件策略和时区的权威非秘密配置。
2. `qqbot-hk/plugins/smart_group_qq/__init__.py` — 入站治理、清洗、命令短路与格式化流程。
3. `qqbot-hk/plugins/smart_group_qq/commands.py` — `/` 指令解析与本地短路执行。
4. `qqbot-hk/plugins/smart_group_qq/formatter.py` — QQ 纯文本排版美化与分片分条。
5. `qqbot-hk/plugins/smart_group_qq/memory.py` — 说话人标识、群记忆滑动窗口与摘要整理。
6. `qqbot-hk/plugins/smart_group_qq/policy.py` — 静态规则和可选语义审核的纯决策逻辑。
7. `qqbot-hk/scripts/reconcile-smart-group-cron.py` — 固定文案群发任务的幂等 create/update/remove 边界（受限/可选）。
8. `qqbot-hk/scripts/install-server.sh` — 秘密、SOUL、插件、schedule 和 cron 对账进入 `/opt/data` 的唯一生产安装路径。
