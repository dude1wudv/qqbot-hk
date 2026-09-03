# QQBot HK

Nous Research Hermes Agent 的 QQ Bot 在 Hytron HK 上的独立部署仓库，模型通过现有 Sub2API 提供。

## 配置

- `QQ_APP_ID`：Hermes QQ Bot 适配器使用的 AppID。
- `QQ_CLIENT_SECRET`：Hermes QQ Bot 适配器使用的 AppSecret。
- `QQ_SANDBOX`：开发体验阶段启用 QQ 官方沙箱 API；当前固定为 `true`。
- `SUB2API_API_KEY`：为 Hermes 单独签发的 Sub2API key；内部地址保存在非秘密配置中。
- 本机真实参数保存在 `.env.local`，该文件被 Git 忽略，禁止提交。
- 可提交的字段模板见 `.env.example`。

后续生产部署时，运行配置应存放在 HK 的 `/opt/qqbot-hk-deploy`，应用代码放在 `/opt/qqbot-hk`；秘密不得进入镜像、Git 历史或日志。

## 当前状态

部署基于官方 `Hermes Agent v0.21.0 (2026.8.31)` 镜像，并固定到镜像摘要。容器不开放宿主机或公网端口，只加入现有的 `sub2api_sub2api-network`。三种模型都通过 0.25 倍率的 `ChatGPT-Pro 20×【Luna 可用】` 分组（group 81）调用：

1. 主模型：`deepseek-v4-flash-0731`，推理强度 `low`。
2. 备用 1：`gemini-3.8-flash-high`，推理强度 `high`。
3. 备用 2：`gpt-5.6-luna`，推理强度 `medium`。

Hermes 仅展示这三个 Sub2API 模型。主模型和备用 1 使用 Chat Completions 协议，备用 2 使用 Responses 协议。

QQ 私聊和群聊均采用 `pairing` 策略。首次发送私聊消息后，需要在服务器上批准配对请求；未批准的 QQ 用户不能使用机器人。QQ 开放平台中的数字 QQ 号用于“开发体验号”资格；Hermes 运行时白名单使用入站事件提供的用户/群 OpenID，二者不可混用。

官方 Hermes Agent v0.21.0 镜像声明了 `QQ_SANDBOX`，但其 QQBot 常量仍固定指向生产 API。本仓库只读挂载 `overrides/qqbot-constants.py`，让该开关选择 QQ 官方沙箱 API；机器人发布后应关闭沙箱并移除此覆盖。

生产目录：

- 应用与 Compose：`/opt/qqbot-hk`
- 持久数据与秘密：`/opt/qqbot-hk-deploy`

服务不挂载 Docker socket，也不共享 HK 宿主机文件系统。Hermes 的终端工具只在自身容器内运行。
