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
- QQ 工具集限制为 `web`、`vision`、`skills`、`tts`、`todo`，不暴露 terminal/file/code execution。
- QQ 语音只使用 `qwen-audio-3.0-asr-flash` 外部识别，不采用腾讯 `asr_refer_text`。语音输入默认返回一条 QQ 原生 MP3 语音；普通文本默认返回文本，只有明确语音/朗读/朗唱请求才调用 TTS。
- STT 地址与模型固定在 `platforms.qqbot.extra.stt`，TTS 地址/模型/音色固定在 `tts.openai`；真实 Sub2API key 仅由安装脚本写入权限 `0600` 的部署态 `.env`。
- `smart_group_qq` 插件提供静态审核、关键词短路、幂等审计、AI 结构化长期记忆、非 @ 消息旁听、群知识库/RAG，以及 `/help`、`/reset`、`/clear`、`/status`、`/summary`、`/rules`、`/kb`、`/值日表`。
- `@机器人 /值日表` 按北京时间即时计算本周日到周六的轮值安排；2026 年 9 月 13 日开始，每周日轮换一次，开始前显示首轮预告。该功能不依赖主动群发或模型调用。
- `/summary` 使用模型生成本群摘要、话题、决定、待办和未决问题；每群独立持久化，`/reset` 只清理会话与记忆，不删除知识库。
- `/kb add 标题 | 正文` 添加资料；`/kb list`、`/kb search 关键词`、`/kb remove 文档ID`、`/kb clear confirm` 管理本群知识。支持缓存目录中的 TXT/Markdown/CSV/JSON/YAML/XML/TOML/DOCX，PDF 需镜像提供 `pypdf`。
- 审计表只保存动作元数据，不保存原消息或回复正文。
- QQ 群主动推送已受官方限制；`scheduled-messages.yaml` 中示例默认禁用。

QQ 当前“开发体验用户”机制使用生产 API/Gateway，并由开放平台限制体验范围。不要设置旧的 `QQ_SANDBOX` 路由；它会使新增开发体验用户的 C2C 事件无法到达当前网关。

## 模型

三种模型通过现有 Sub2API 分组调用：

1. 主模型：`deepseek-v4-flash-0731`，推理强度 `low`。
2. 备用 1：`gemini-3.8-flash-high`，推理强度 `high`。
3. 备用 2：`gpt-5.6-luna`，推理强度 `medium`。

主模型和备用 1 使用 Chat Completions；备用 2 使用 Responses。DeepSeek 只处理文本；所有图片先由专用视觉链 `gemini-3.8-flash-high → gpt-5.6-luna` 转为可信度受限的文字描述，再交给主模型。图片也可作为群知识库资料导入。

## 部署

生产目录：

- 应用与 Compose：`/opt/qqbot-hk`
- 持久数据与秘密：`/opt/qqbot-hk-deploy`

部署前备份 Compose 以及 `/opt/qqbot-hk-deploy/hermes-data` 中的 `config.yaml`、`SOUL.md`、`plugins`、`cron`。随后：

```bash
bash /opt/qqbot-hk/scripts/install-server.sh
bash /opt/qqbot-hk/scripts/verify-server.sh
```

`install-server.sh` 会校验群白名单，把群 OpenID 注入部署态配置，明确移除旧沙箱路由和全员放行开关，原子安装 SOUL/plugin/schedule/reconciler，构建固定摘要派生镜像并只重建 `hermes-qqbot`，等待健康后运行 cron 对账。源码配置不含真实 OpenID 或 API key。

`verify-server.sh` 验证基础摘要/补丁标签、STT/TTS 实际配置解析、VOICE 类型、原生 MP3 被动回复锚点、Sub2API 音频路由、三个文本模型、Gemini/Luna 识图、容器健康、QQ 生产网关、私聊策略、插件、白名单、owned cron，以及 SQLite 和记忆/知识库表结构；不会输出 OpenID、消息正文或秘密。

部署后先由 QQ 开放平台“开发体验号码”名单中的新用户直接私聊：在上述登记窗口内，首条消息应自动完成 pairing 并直接进入问答；窗口结束后，新的未批准用户应收到标准配对码。群聊再用新群会话或 `/reset` 验证：白名单群 @ 可回复、非白名单群无回复、两名群成员共享上下文、关键词和审核各只发送一次。普通群消息能否被旁听取决于 QQ 开放平台对该机器人的消息事件权限/投递配置；代码不会绕过平台边界。平台确实投递时，再验证“非 @ 不回复，但下一次 @ 提问可引用其内容”。

## 回滚

恢复上一个精确代码/镜像和部署前备份，再运行安装与验证。cron 回滚只删除 `smart-group-qq::` 前缀任务，不覆盖整个 Hermes cron；保留 `/opt/data/plugin-data/smart_group_qq/data.db` 作为审计事实。
