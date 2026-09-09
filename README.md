# QQBot HK

Nous Research Hermes Agent 的 QQ Bot 在 Hytron HK 上的独立部署仓库，模型通过现有 Sub2API 提供。

## 配置

生产秘密位于 `/opt/qqbot-hk-deploy/secrets`，不得提交：

- `qqbot.env`：`QQ_APP_ID`、`QQ_CLIENT_SECRET`、`QQ_GROUP_ALLOWED_USERS`。
- `sub2api-api-key`：Hermes 专用 Sub2API key。
- `QQ_GROUP_ALLOWED_USERS` 是逗号分隔的 QQ 群 OpenID；不能使用数字群号、用户 OpenID、空值或 `*`。
- 开发体验阶段也使用 QQ 官方生产 API/Gateway；开放平台按“开发体验用户”名单限制可访问账号。

可提交占位模板见 `.env.example`。本机 `.env.local` 被 Git 忽略。

## 运行边界

部署使用以 Hermes Agent v0.21.0 固定摘要为基础的项目派生镜像。构建时对真实 QQ adapter 做 SHA/哨兵 fail-closed 的最小语音补丁并运行行为 smoke；不复制整个 adapter。容器不开放宿主机端口、不挂载 Docker socket，只加入 `sub2api_sub2api-network`。

- 私聊：使用 Hermes `dm_policy: pairing`。截至 `2026-09-05T08:00:00Z` 的临时登记窗口内，QQ 私聊发送者会在中央鉴权前自动写入 pairing 批准名单并继续处理；窗口结束后，新的未批准用户恢复标准配对码流程。QQ 开放平台仍必须先实际投递该用户消息。
- 群聊回复：只接受 `QQ_GROUP_ALLOWED_USERS` 中群的 `@机器人 + 问题`。若 QQ 开放平台已为机器人投递普通群消息，插件会旁听 `GROUP_MESSAGE_CREATE`，但该事件只进入本群记忆/检索链路，绝不触发回复、命令或普通 Agent 会话。
- 同一群共享 Hermes 会话；私聊、其他群和当前群严格隔离。
- 回复默认采用 QQ 短聊风格：群聊通常 1～3 句、约 120 个汉字内，私聊通常约 300 个汉字内；用户明确要求或内容确有必要时再展开。发送前统一把常见 Markdown 降级为克制的纯文本，群聊和私聊均生效。
- QQ 工具集限制为 `web`、`vision`、`skills`、`tts`、`todo`，不暴露 terminal/file/code execution。
- QQ 语音只使用 `qwen-audio-3.0-asr-flash` 外部识别，不采用腾讯 `asr_refer_text`。语音输入默认返回一条 QQ 原生 MP3 语音；普通文本默认返回文本，只有明确语音/朗读/朗唱请求才调用 TTS。
- STT 地址与模型固定在 `platforms.qqbot.extra.stt`，TTS 地址/模型/音色固定在 `tts.openai`；真实 Sub2API key 仅由安装脚本写入权限 `0600` 的部署态 `.env`。
- `smart_group_qq` 插件提供静态审核、关键词短路、幂等审计、AI 结构化长期记忆、非 @ 消息旁听、群知识库/RAG，以及 `/help`、`/reset`、`/clear`、`/status`、`/summary`、`/rules`、`/kb`、`/值日表`、`/我的记忆`、`/记住我`、`/纠正记忆`、`/停止记忆`、`/忘记我`。
- `@机器人 /值日表` 按北京时间即时计算本周日到周六的轮值安排；2026 年 9 月 13 日开始，每周日轮换一次，开始前显示首轮预告。该功能不依赖主动群发或模型调用。
- `/summary` 使用模型生成本群摘要、话题、决定、待办和未决问题；每群独立持久化，`/reset` 只清理会话与记忆，不删除知识库。
- `/kb add 标题 | 正文` 添加资料；`/kb list`、`/kb search 关键词`、`/kb remove 文档ID`、`/kb clear confirm` 管理本群知识。支持缓存目录中的 TXT/Markdown/CSV/JSON/YAML/XML/TOML/DOCX，PDF 需镜像提供 `pypdf`。
- 审计表只保存动作元数据，不保存原消息或回复正文。
- 本部署尚未启用普通群消息主动参与或定时群发，`scheduled-messages.yaml` 默认禁用。[QQ 官方现行文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)已列出全量群消息与主动发送能力；应用权限、群开关和实际事件投递必须单独核验，不能把本项目的保守默认策略当成平台永久不支持。

### 成员专属记忆与非 @ 消息

- 成员身份记忆使用独立的 `group_members`、`member_memory_facts` 表，按 `(group_id, member_ref)` 隔离；不得把成员私有资料写入群级 `knowledge_documents`。`compaction_jobs` 保存异步压缩任务的游标和重试状态。
- 发布版本的 `member_ref`/`member_digest` 必须由部署态密钥派生，是稳定伪名而不是 QQ OpenID 的截断值；未注入部署态密钥的构建不得上线。原始 OpenID 不写入日志、提示词、指标或生成的 cron 文件。
- 默认只保存成员主动表达、明确确认或通过记忆命令授权的偏好、角色和项目事实；不推断健康、政治、宗教、性取向、财务等敏感属性。成员可查询、撤回和删除自己的记忆，删除必须覆盖画像、事实、来源关联和缓存召回。
- 自动提取的新事实必须附有本次输入中的原文证据，并绑定该成员本条消息的历史行；拒绝缺失/伪造证据及非有限置信度，不把同群后来另一人的消息误认作来源。
- `/纠正记忆：字段=新内容` 的本人确认优先于自动推断。同一归一化字段不因模型分类不同而重复，晚完成的旧消息提取不能覆盖更新来源。回答时先按当前问题相关性召回，再考虑本人确认和更新时间，不只机械取最后几条。
- 空白或敏感内容的记忆命令不会重新开启已停止的成员记忆。`/停止记忆` 和 `/纠正记忆` 会重置本群当前会话上下文，避免继续沿用旧画像。
- `/忘记我` 会清理本人原文、成员档案/事实、本群机器人回复缓存以及衍生摘要/压缩任务；他人成员原文和知识库保留。清理范围是机器人内部缓存，不是撤回 QQ 客户端消息。持久化失效版本防止清理前尚在运行的摘要、提取或回答再次写回旧内容，即使之后重新同意记忆也不恢复旧任务。
- 非 `@` 消息只在 QQ 开放平台实际投递 `GROUP_MESSAGE_CREATE` 时旁听，进入本群 ambient 历史和摘要链路，不触发回复、命令或普通 Agent 会话。定时任务只能整理已收到的消息、执行保留期清理或发送固定公告，不能补拉平台没有投递的历史消息。
- 收到 `@` 后，插件只召回当前群、当前消息之前最近的有限条非 `@` 消息，并按字符预算截断；不得把其他群、私聊或成员私有记忆带入公开群聊。
- 群知识检索覆盖本群所有已存片段，采用流式 top-k 保持候选内存有界，不再只检查最新 2000 个片段。当前仍是词面检索，不能等同于 embedding 语义召回。

### 数据保留与删除

`group_history` 是用于摘要和最近上下文的受限原文缓存，不等同于永久聊天档案；当前配置分别使用 `memory.ambient_retention_days`、`memory.addressed_retention_days` 和 `member_memory.fact_retention_days` 控制 ambient、addressed、成员事实的保留期。审计和媒体缓存也必须分别配置保留期，并由定时维护任务执行过期删除。`/reset` 清理当前群会话/历史/群摘要但保留知识库；成员退出或执行删除命令时，还要删除其专属画像和事实。审计仅保留动作元数据，日志、指标和错误信息不得包含消息正文、OpenID、附件签名 URL、提示词或密钥。

QQ 当前“开发体验用户”机制使用生产 API/Gateway，并由开放平台限制体验范围。不要设置旧的 `QQ_SANDBOX` 路由；它会使新增开发体验用户的 C2C 事件无法到达当前网关。

## 模型

三种模型通过现有 Sub2API 分组调用：

1. 主模型：`deepseek-v4-flash-0731`，推理强度 `low`。
2. 备用 1：`gemini-3.8-flash-high`，推理强度 `high`。
3. 备用 2：`gpt-5.6-luna`，推理强度 `medium`。

主模型和备用 1 使用 Chat Completions；备用 2 使用 Responses。DeepSeek 只处理文本；所有图片先由专用视觉链 `gemini-3.8-flash-high → gpt-5.6-luna` 转为可信度受限的文字描述，再交给主模型。图片也可作为群知识库资料导入。

群内 @ 机器人发送 /gemini（兼容 / gemini）可将当前群会话切换到 gemini-3.8-flash-high；发送 /deepseek（兼容 / deepseek）可切回 deepseek-v4-flash-0731。两条命令都使用 Hermes 原生的会话级模型覆写，不修改其他群或全局默认模型。

Hermes 的会话上下文自动压缩对上述三个模型统一使用 `200000` token 绝对阈值；压缩摘要固定调用 `deepseek-v4-flash-0731`，推理强度为 `low`。群记忆的 12 条消息摘要阈值与此独立。

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

`install-server.sh` 会校验群白名单，把群 OpenID 注入部署态配置，明确移除旧沙箱路由和全员放行开关，原子安装 SOUL/plugin/schedule/reconciler，构建固定摘要派生镜像并只重建 `hermes-qqbot`，等待健康、运行 cron 对账并通过完整验收后才清理旧插件副本。源码配置不含真实 OpenID 或 API key。

`verify-server.sh` 验证基础摘要/补丁标签、STT/TTS 实际配置解析、VOICE 类型、原生 MP3 被动回复锚点、Sub2API 音频路由、三个文本模型、Gemini/Luna 识图、容器健康、QQ 生产网关、私聊策略、插件、白名单、owned cron，以及 SQLite 和记忆/知识库表结构；不会输出 OpenID、消息正文或秘密。

部署后先由 QQ 开放平台“开发体验号码”名单中的新用户直接私聊：在上述登记窗口内，首条消息应自动完成 pairing 并直接进入问答；窗口结束后，新的未批准用户应收到标准配对码。群聊再用新群会话或 `/reset` 验证：白名单群 @ 可回复、非白名单群无回复、两名群成员共享上下文、关键词和审核各只发送一次。普通群消息能否被旁听取决于 QQ 开放平台对该机器人的消息事件权限/投递配置；代码不会绕过平台边界。平台确实投递时，再验证“非 @ 不回复，但下一次 @ 提问可引用其内容”。

## 回滚

恢复上一个精确代码/镜像和部署前备份，再运行安装与验证。本版本 SQLite schema 为 `3`，增量新增事实证据列和群记忆失效版本表，不删除已有成员事实或知识库。旧版代码会拒绝更高 schema，回滚时必须使用对应升级前的一致性数据库快照，不能只切换旧镜像；不得通过删库初始化解决兼容问题。cron 回滚只删除 `smart-group-qq::` 前缀任务，不覆盖整个 Hermes cron。备份与原始生产数据始终留在服务器，按独立保留策略管理。
