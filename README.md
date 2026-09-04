# QQBot HK

Nous Research Hermes Agent 的 QQ Bot 在 Hytron HK 上的独立部署仓库，模型通过现有 Sub2API 提供。

## 配置

生产秘密位于 `/opt/qqbot-hk-deploy/secrets`，不得提交：

- `qqbot.env`：`QQ_APP_ID`、`QQ_CLIENT_SECRET`、`QQ_SANDBOX`、`QQ_GROUP_ALLOWED_USERS`。
- `sub2api-api-key`：Hermes 专用 Sub2API key。
- `QQ_GROUP_ALLOWED_USERS` 是逗号分隔的 QQ 群 OpenID；不能使用数字群号、用户 OpenID、空值或 `*`。
- 开发体验阶段保持 `QQ_SANDBOX=true`。机器人完成发布后才切换生产 API。

可提交占位模板见 `.env.example`。本机 `.env.local` 被 Git 忽略。

## 运行边界

部署固定使用 Hermes Agent v0.21.0 镜像摘要。容器不开放宿主机端口、不挂载 Docker socket，只加入 `sub2api_sub2api-network`。

- 私聊：`pairing`。首次私聊返回配对码，管理员在服务器批准后才能使用。
- 群聊回复：只接受 `QQ_GROUP_ALLOWED_USERS` 中群的 `@机器人 + 问题`。若 QQ 开放平台已为机器人投递普通群消息，插件会旁听 `GROUP_MESSAGE_CREATE`，但该事件只进入本群记忆/检索链路，绝不触发回复、命令或普通 Agent 会话。
- 同一群共享 Hermes 会话；私聊、其他群和当前群严格隔离。
- QQ 工具集限制为 `web`、`vision`、`skills`、`todo`，不暴露 terminal/file/code execution。
- `smart_group_qq` 插件提供静态审核、关键词短路、幂等审计、AI 结构化长期记忆、非 @ 消息旁听、群知识库/RAG，以及 `/help`、`/reset`、`/clear`、`/status`、`/summary`、`/rules`、`/kb`。
- `/summary` 使用模型生成本群摘要、话题、决定、待办和未决问题；每群独立持久化，`/reset` 只清理会话与记忆，不删除知识库。
- `/kb add 标题 | 正文` 添加资料；`/kb list`、`/kb search 关键词`、`/kb remove 文档ID`、`/kb clear confirm` 管理本群知识。支持缓存目录中的 TXT/Markdown/CSV/JSON/YAML/XML/TOML/DOCX，PDF 需镜像提供 `pypdf`。
- 审计表只保存动作元数据，不保存原消息或回复正文。
- QQ 群主动推送已受官方限制；`scheduled-messages.yaml` 中示例默认禁用。

官方 Hermes v0.21.0 声明了 `QQ_SANDBOX`，但其 QQBot 常量仍固定指向生产 API。本仓库只读挂载 `overrides/qqbot-constants.py` 使该开关选择官方沙箱或生产 API；上游修复并完成生产发布后再移除覆盖。

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

`install-server.sh` 会校验群白名单、把 OpenID 注入部署态配置、原子安装 SOUL/plugin/schedule/reconciler、只重建 `hermes-qqbot`、等待健康并运行 cron 对账。源码配置不含真实 OpenID。

`verify-server.sh` 验证三个文本模型、Gemini/Luna 识图、容器健康、QQ 网关连接、配置、插件加载、白名单计数、owned cron 数量，以及插件 SQLite 和记忆/知识库表结构；不会输出 OpenID、消息正文或秘密。

部署后用新群会话或 `/reset` 验证：白名单群 @ 可回复、非白名单群无回复、两名群成员共享上下文、关键词和审核各只发送一次。普通群消息能否被旁听取决于 QQ 开放平台对该机器人的消息事件权限/投递配置；代码不会绕过平台边界。平台确实投递时，再验证“非 @ 不回复，但下一次 @ 提问可引用其内容”。

## 回滚

恢复上一个精确代码/镜像和部署前备份，再运行安装与验证。cron 回滚只删除 `smart-group-qq::` 前缀任务，不覆盖整个 Hermes cron；保留 `/opt/data/plugin-data/smart_group_qq/data.db` 作为审计事实。
