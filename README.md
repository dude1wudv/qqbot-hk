# QQBot HK

Nous Research Hermes Agent 的 QQ Bot 在 Hytron HK 上的独立部署仓库，模型通过现有 Sub2API 提供。

## 配置

生产秘密位于 `/opt/qqbot-hk-deploy/secrets`，不得提交：

- `qqbot.env`：`QQ_APP_ID`、`QQ_CLIENT_SECRET`、`QQ_SCHEDULE_GROUPS`。
- `sub2api-api-key`：Hermes 通用模型 key；`sub2api-deepseek-api-key`：仅绑定 DeepSeek 分组的主模型 key。
- `QQ_SCHEDULE_GROUPS` 是固定公告的显式目标列表，使用逗号分隔的 QQ 群 OpenID，不能使用数字群号、用户 OpenID、空值或 `*`。固定公告默认禁用。部署会移除旧 `QQ_GROUP_ALLOWED_USERS`，防止 Hermes 环境变量覆盖群专属通配配置。
- 开发体验阶段也使用 QQ 官方生产 API/Gateway；开放平台按“开发体验用户”名单限制可访问账号。

可提交占位模板见 `.env.example`。本机 `.env.local` 被 Git 忽略。

## 运行边界

部署使用以 Hermes Agent v0.21.2（`v2026.9.11`）固定摘要为基础的项目派生镜像。新版已原生在 gateway 启动阶段发现插件，项目只保留经 SHA/哨兵 fail-closed 的 QQ 语音、Sub2API Chat 推理字段、QQ 帮助菜单和群聊最终输出补丁；镜像构建会运行对应行为 smoke，不复制整个 adapter。容器不开放宿主机端口、不挂载 Docker socket，只加入 `sub2api_sub2api-network`。

- 私聊：使用 Hermes `dm_policy: pairing`。截至 `2026-09-05T08:00:00Z` 的临时登记窗口内，QQ 私聊发送者会在中央鉴权前自动写入 pairing 批准名单并继续处理；窗口结束后，新的未批准用户恢复标准配对码流程。QQ 开放平台仍必须先实际投递该用户消息。
- 群聊回复：允许已由 QQ 适配器和中央网关授权群的官方 `@机器人 + 问题`，也会在平台投递普通群消息时按规则积极参与开放问题。普通消息先免费过滤，再按同成员聚合；明确 @ 他人、管理命令和低价值闲聊不进入主 Agent。
- 同一群共享 Hermes 会话；私聊、其他群和当前群严格隔离。
- 回复默认采用 QQ 短聊风格：群聊通常 1～3 句、约 120 个汉字内，私聊通常约 300 个汉字内；用户明确要求或内容确有必要时再展开。发送前统一把常见 Markdown 降级为克制的纯文本，群聊和私聊均生效。
- QQ 工具集启用 `web`、`vision`、`skills`、`todo`、`terminal`、`file`；其中 `terminal` 提供 shell 能力，`tts`、`code`、`computer` 不暴露。文件写入受 `HERMES_WRITE_SAFE_ROOT=/opt/data:/tmp` 限制，Hermes credential/project env 路径仍由内置防护拦截。
- QQ 语音输入与输出均关闭：语音附件不进入 STT/模型，`send_voice` 在媒体上传前失败关闭，配置不包含 TTS provider 或音频密钥。普通文本与图片功能不受影响。
- `smart_group_qq` 插件提供静态审核、关键词短路、幂等审计、AI 结构化长期记忆、非 @ 消息旁听、群知识库/RAG，以及 `/help`、`/reset`、`/clear`、`/new`、`/compress`、`/status`、`/summary`、`/rules`、`/kb`、`/值日表`、`/gemini`、`/deepseek`、`/low`、`/medium`、`/high`、`/max`、`/我的记忆`、`/记住我`、`/纠正记忆`、`/停止记忆`、`/忘记我`。
- `/low`、`/medium`、`/high`、`/max`、`/deepseek`、`/gemini` 注册为 Hermes 原生命令层的 `quick_commands`，群聊和私聊都会在未知命令拦截前展开。群聊仅将 `/compress`、`/new`、`/commands` 在剥离 QQ @ 后透传给 Hermes，避免恢复命令被包装进群上下文，同时不暴露其他原生管理命令。私聊 `/help` 优先显示中文自定义命令菜单，Hermes 原生命令折叠为 `/commands` 入口；`/值日表` 仍仅群聊可用。
- `@机器人 /值日表` 按北京时间即时计算本周日到周六的轮值安排；2026 年 9 月 13 日开始，每周日轮换一次，开始前显示首轮预告。该功能不依赖主动群发或模型调用。
- `/summary` 使用模型生成本群摘要、话题、决定、待办和未决问题；每群独立持久化，`/reset` 只清理会话与记忆，不删除知识库。
- `/kb add 标题 | 正文` 添加资料；`/kb list`、`/kb search 关键词`、`/kb remove 文档ID`、`/kb clear confirm` 管理本群知识。支持缓存目录中的 TXT/Markdown/CSV/JSON/YAML/XML/TOML/DOCX，PDF 需镜像提供 `pypdf`。
- 审计表只保存动作元数据，不保存原消息或回复正文。
- 所有机器人已加入、且 QQ 平台允许投递的群均开放非 @ 按需参与。配置通过原生 `group_allow_from: ["*"]` 与 `group_allowed_chats: ["*"]` 分别授权适配器和中央网关；私聊仍要求 pairing，不启用 `QQ_ALLOW_ALL_USERS`。固定定时群发仍禁用。[QQ 官方现行文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)列出全量群消息与主动发送能力；群管理员仍需分别开启接收全部消息与允许主动发送。

### 成员专属记忆与非 @ 消息

- 成员身份记忆使用独立的 `group_members`、`member_memory_facts` 表，按 `(group_id, member_ref)` 隔离；不得把成员私有资料写入群级 `knowledge_documents`。`compaction_jobs` 保存异步压缩任务的游标和重试状态。
- 发布版本的 `member_ref`/`member_digest` 必须由部署态密钥派生，是稳定伪名而不是 QQ OpenID 的截断值；未注入部署态密钥的构建不得上线。原始 OpenID 不写入日志、提示词、指标或生成的 cron 文件。
- 默认只保存成员主动表达、明确确认或通过记忆命令授权的偏好、角色和项目事实；不推断健康、政治、宗教、性取向、财务等敏感属性。成员可查询、撤回和删除自己的记忆，删除必须覆盖画像、事实、来源关联和缓存召回。
- 自动提取的新事实必须附有本次输入中的原文证据，并绑定该成员本条消息的历史行；拒绝缺失/伪造证据及非有限置信度，不把同群后来另一人的消息误认作来源。
- `/纠正记忆：字段=新内容` 的本人确认优先于自动推断。同一归一化字段不因模型分类不同而重复，晚完成的旧消息提取不能覆盖更新来源。回答时先按当前问题相关性召回，再考虑本人确认和更新时间，不只机械取最后几条。
- 空白或敏感内容的记忆命令不会重新开启已停止的成员记忆。`/停止记忆` 和 `/纠正记忆` 会重置本群当前会话上下文，避免继续沿用旧画像。
- `/忘记我` 会清理本人原文、成员档案/事实、本群机器人回复缓存以及衍生摘要/压缩任务；他人成员原文和知识库保留。清理范围是机器人内部缓存，不是撤回 QQ 客户端消息。持久化失效版本防止清理前尚在运行的摘要、提取或回答再次写回旧内容，即使之后重新同意记忆也不恢复旧任务。
- 非 `@` 消息只在 QQ 开放平台实际投递 `GROUP_MESSAGE_CREATE` 时进入本群 ambient 历史和摘要链路。普通消息按同群同成员进行 2 秒尾沿防抖、第一条起最多等待 5 秒，最多 20 条/6000 字；规则高分直接参与，模糊候选才调用一次低推理分类。5 秒回复冷却从实际发送成功开始，冷却内追问继续聚合；官方 `@机器人` 不等待且会使旧参与判断失效。`@` 其他成员、非 @ 管理命令、纯表情/收尾闲聊保持静默。
- 近期原文、滚动摘要、当前成员事实和知识检索分别受 1600/800/600/1000 字预算约束，总插件背景不超过 4000 字；当前合并问题另有 6000 字上限。语音附件始终忽略；其他旁听附件只有通过门控后才交给原生 QQ 入站链处理。
- 全群开放时新群不需要手工登记 OpenID。若运维主动恢复限制策略，未授权群的 @、加群和允许通知事件仅记录 `group_access_pending` 接入元数据到服务器审计数据库，每群每小时最多一条；不保存正文、不自动授权。
- 群知识检索覆盖本群所有已存片段，采用流式 top-k 保持候选内存有界，不再只检查最新 2000 个片段。当前仍是词面检索，不能等同于 embedding 语义召回。

### 数据保留与删除

`group_history` 是用于摘要和最近上下文的受限原文缓存，不等同于永久聊天档案；当前配置分别使用 `memory.ambient_retention_days`、`memory.addressed_retention_days` 和 `member_memory.fact_retention_days` 控制 ambient、addressed、成员事实的保留期。审计和媒体缓存也必须分别配置保留期，并由定时维护任务执行过期删除。`/reset` 清理当前群会话/历史/群摘要但保留知识库；成员退出或执行删除命令时，还要删除其专属画像和事实。审计仅保留动作元数据，日志、指标和错误信息不得包含消息正文、OpenID、附件签名 URL、提示词或密钥。

QQ 当前“开发体验用户”机制使用生产 API/Gateway，并由开放平台限制体验范围。不要设置旧的 `QQ_SANDBOX` 路由；它会使新增开发体验用户的 C2C 事件无法到达当前网关。

## 模型

Hermes 默认通过专用 Sub2API DeepSeek 分组，以 OpenAI Chat Completions 协议调用 `deepseek/deepseek-v4.1-flash`，推理强度为 `medium`。每次请求显式携带 `reasoning_effort`；群内可用 `/low`、`/medium`、`/high`、`/max` 仅切换当前群会话的推理强度。Hermes 将原生图片内容块翻译为该协议；不再先调用 Gemini/Luna 视觉链，也不配置自动模型回退。`gemini-3.8-flash-high` 仍保留为群会话可手动切换的模型，并使用独立的通用 Sub2API key。

群内 @ 机器人发送 /gemini（兼容 / gemini）可将当前群会话切换到 `gemini-3.8-flash-high`；发送 /deepseek（兼容 / deepseek）可切回 `deepseek/deepseek-v4.1-flash`。两条命令都使用 Hermes 原生的会话级模型覆写，不修改其他群或全局默认模型。

Hermes 的会话上下文达到 `50000` token 阈值时自动尝试压缩，受原生冷却和无效压缩保护约束；该值是触发阈值，不是完整请求硬上限。压缩在原会话继续，不调用 `/reset`，并固定使用 `deepseek/deepseek-v4.1-flash`、`low` 推理强度。若连续无效压缩触发持久化 anti-thrash breaker，QQ 网关会在下一条普通消息进入模型前自动执行完整 `/new` 等价轮换，并继续处理当前消息；手动 `/compress`、`/new` 不被抢占。群记忆与知识库不随该 Hermes 会话轮换清空。群记忆独立在 40 条新消息且距上次刷新至少 300 秒时整理；或至少 4 条新消息的最老一条等待 1200 秒后整理，单条闲聊不会因空闲自动摘要。

## 部署

生产目录：

- 应用与 Compose：`/opt/qqbot-hk`
- 持久数据与秘密：`/opt/qqbot-hk-deploy`

部署前备份 Compose 以及 `/opt/qqbot-hk-deploy/hermes-data` 中的 `config.yaml`、`SOUL.md`、`plugins`、`cron`。安装脚本会先对 `plugin-data/smart_group_qq/data.db` 执行完整性检查与 SQLite 一致性快照，再开始重建：

```bash
bash /opt/qqbot-hk/scripts/install-server.sh
# 安装脚本已自动执行同一份完整验收；需要独立复查时可再次运行：
bash /opt/qqbot-hk/scripts/verify-server.sh
```

`install-server.sh` 会校验固定公告目标列表，保留配置模板的群专属通配授权，移除旧沙箱路由和包括私聊在内的全员放行开关，原子安装 SOUL/plugin/schedule/reconciler，构建固定摘要派生镜像并只重建 `hermes-qqbot`，等待健康、运行 cron 对账并通过完整验收后才清理旧插件副本。源码配置不含真实 OpenID 或 API key。

`verify-server.sh` 验证基础摘要/补丁标签、DeepSeek 的 OpenAI Chat Completions 路由及显式推理强度、语音输入/输出双重禁用与音频环境变量清理、两把 Sub2API key 的隔离路由、DeepSeek 文本/识图、Gemini 文本、50k 压缩触发与无效压缩自动换新、QQ 中间输出关闭、群聊最终输出补丁、2秒/5秒聚合与分段预算、QQ `terminal`/`file` 工具集与写入安全边界、容器健康、QQ 生产网关、私聊策略、插件、白名单、owned cron，以及 SQLite 和记忆/知识库表结构；不会输出 OpenID、消息正文或秘密。

部署后验证任意已加入的新群通过适配器与中央群授权，未批准私聊仍被拒绝或进入 pairing，两名成员共享本群上下文且不串群。群管理员打开“接收所有消息”并确认平台实际投递后，再验证：普通闲聊只旁听、明确求助按需回复、重复事件只处理一次、非 @ 管理命令不执行、近期旁听能作为后续 @ 的上下文。主动推送还要求群内允许主动发送，不能用修改本地配置绕过平台权限。

## 回滚

恢复上一个精确代码/镜像和部署前备份，再运行安装与验证。本版本 SQLite schema 为 `3`，增量新增事实证据列和群记忆失效版本表，不删除已有成员事实或知识库。旧版代码会拒绝更高 schema，回滚时必须使用对应升级前的一致性数据库快照，不能只切换旧镜像；不得通过删库初始化解决兼容问题。cron 回滚只删除 `smart-group-qq::` 前缀任务，不覆盖整个 Hermes cron。备份与原始生产数据始终留在服务器，按独立保留策略管理。
