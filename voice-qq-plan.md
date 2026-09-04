# QQBot HK 新一轮：Qwen 语音收发执行计划

## 已确认决策

- 接入边界：继续使用 QQ 官方 Bot API v2。好友私聊语音无需 `@`；群语音仍只有 `GROUP_AT_MESSAGE_CREATE`，必须 `@机器人`。不引入 NapCat/OneBot，不承担个人 QQ 风控。
- 识别模型：只用 `qwen-audio-3.0-asr-flash`。即使 QQ 附件携带 `asr_refer_text`，也不把腾讯 ASR 作为识别结果或故障回退。
- 回复策略：语音输入默认返回 QQ 原生语音；普通文本默认返回文本；明确要求“语音回复/朗读/朗唱/唱歌”时返回 QQ 原生语音。
- “唱歌”语义：使用 `qwen-audio-3.0-tts-plus` 做带节奏和情绪指令的朗唱，不承诺旋律、伴奏或真正歌曲生成。
- `qwen-audio-3.0-realtime-plus` 不进入本轮。QQ 是离散消息收发，Realtime 的持续双向 PCM/WebSocket 会话没有可用的 QQ 端到端交互面。

## 当前差距

1. Hermes v0.21.0 的 QQ adapter 已能下载/转码语音并调用 OpenAI 风格 `/audio/transcriptions`，也已能以 QQ `file_type=3` 发送原生语音；但它始终优先采用 QQ `asr_refer_text`，不满足“仅 Qwen”。
2. QQ adapter 把语音转写拼进 `event.text` 后没有保留 `MessageType.VOICE`；因此 Hermes 现有“语音输入自动 TTS”判断不会命中。
3. QQ 当前工具集只有 `[web, vision, skills, todo]`，模型无法针对文本意图调用 `text_to_speech`。
4. Hermes 的 OpenAI TTS 客户端已经支持自定义 `base_url`、`model`、`voice`、`speed` 和 `instructions`，但 Sub2API 尚无 OpenAI 兼容的 `/v1/audio/transcriptions` 与 `/v1/audio/speech`。
5. 阿里 Qwen 音频不是普通 Chat Completions：ASR 使用 DashScope multimodal-generation HTTP；TTS Plus 使用 DashScope `api-ws/v1/inference` 的 `run-task/continue-task/finish-task` 协议。不能把现有 `/v1/chat/completions` 直接改路径转发。
6. 生产账号 970 的模型映射和分组 81 已包含三个 Qwen 音频模型；实现必须仍按 group/model 调度，不能硬编码账号 970。分组 81 当前音频单价为空，会落到 Sub2API 内置默认价；上线前必须写入供应商实际 STT/TTS 单价，否则账务不准确。

## 目标调用链

```text
QQ 好友语音（无 @）/ QQ 群 @ 语音
  -> Hermes QQ adapter 下载 voice_wav_url，必要时 SILK -> WAV
  -> POST http://sub2api:8080/v1/audio/transcriptions
  -> Sub2API 鉴权、分组 81、模型映射、账号调度
  -> DashScope Qwen ASR HTTP
  -> {"text":"..."}
  -> Hermes Agent
  -> 语音输入：自动 TTS；文本明确语音意图：text_to_speech 工具
  -> POST http://sub2api:8080/v1/audio/speech
  -> Sub2API 调度 + DashScope Qwen TTS Plus WebSocket
  -> MP3
  -> QQ Bot API v2 file_type=3 原生语音消息
```

## 阶段 1：Sub2API 音频兼容层

### 1.1 固定公开契约

在 `sub2api-hk-src/backend/internal/server/routes/gateway.go` 注册以下受现有 API key、group assignment、request ID、错误保护和 body limit 中间件保护的路由：

- `POST /v1/audio/transcriptions`
  - `multipart/form-data`；必填 `file` 与 `model`。
  - 本轮只承诺 Hermes 实际发送的 WAV；原始文件上限 7 MiB，时长上限 300 秒，避免 Base64 后超过 Qwen 10 MiB 输入限制。
  - 成功返回 OpenAI 兼容 JSON：`{"text":"识别结果"}`。
- `POST /v1/audio/speech`
  - JSON 必填 `model`、`input`、`voice`；支持 `response_format=mp3`、`speed`、`instructions`。
  - 本轮只接受非流式 MP3；其他格式或 `stream=true` 明确返回 `400 invalid_request_error`，不做伪流式。
  - 成功返回 `audio/mpeg` 二进制。

两个端点只允许 `openai` 平台分组且请求模型必须通过账号 model mapping 命中。未知模型、错误平台、空文本、非法 voice/format、超限文件在账号调度和上游调用前失败。

### 1.2 新增 Qwen 原生传输

新增 `backend/internal/service/qwen_audio.go`，职责保持单一：

- 从已调度账号取得 API key，并读取账号非秘密配置 `extra.qwen_audio_http_base_url` 与 `extra.qwen_audio_ws_base_url`；只接受 HTTPS/WSS，禁止请求体控制上游 host，避免 SSRF。
- ASR：把 WAV 编码为 `data:audio/wav;base64,...`，请求 `services/aigc/multimodal-generation/generation`，传入映射后的模型、`format=wav`、实际采样率；从 `output.output.sentence.text` 或 `output.text` 解析结果。
- TTS：连接 `api-ws/v1/inference`，发送带唯一 `task_id` 的 `run-task`，在 `task-started` 后发送一次 `continue-task(input.text)` 与 `finish-task`；聚合 binary audio frame，直到 `task-finished`。
- 把 OpenAI `instructions` 映射为 Qwen 的单数 `instruction`，按 Qwen Audio TTS 的规则限制为 100 个计数单位（中日韩汉字计 2，其余字符计 1）；把 OpenAI `speed` 校验后映射为 Qwen `rate`。越界参数直接拒绝，不静默截断或钳位。
- 每个请求独立 WebSocket；本轮不做连接池。这样账号切换、失败隔离和密钥边界清楚，QQ 消息量也不值得先增加池化状态。
- HTTP、握手、首包、总请求分别设置有界 timeout；客户端取消必须关闭上游连接。
- 上游 4xx 原样归类为不可重试请求错误；网络错误、429、5xx 可进入现有同模型账号 failover；响应体与日志均不得包含音频 Base64、转写正文、TTS 正文或凭据。

新增 `backend/internal/handler/qwen_audio.go`：复用现有 billing eligibility、scheduler slot、model mapping、failover、usage record 与安全审计边界。TTS 文本进入现有 OpenAI 文本审核；ASR 音频不写正文审计，只记录模型、字节数、时长、状态和 request ID。

### 1.3 精确计费

- TTS：`utf8.RuneCountInString(input) / 1_000_000`，模式 `tts`。
- STT：从 WAV header 的 sample rate、channel、bits-per-sample、data length 计算小时数，模式 `stt`；损坏或无法确定时长的 WAV 在调用上游前拒绝，不能用 HTTP 耗时或请求体体积代替真实音频时长。
- 只有成功且拿到有效音频/转写结果才创建强唯一 usage event；失败请求不扣费。
- 上线前把分组 81 的 `audio_tts_price_per_million_chars` 与 `audio_stt_price_per_hour` 设置为供应商实际售卖单价并复核倍率；Realtime 单价不因本轮改动而启用。

### 1.4 Sub2API 测试

新增或扩展：

- `backend/internal/service/qwen_audio_test.go`：ASR Data URL、映射模型、双响应字段、TTS 事件顺序、binary frame 聚合、instruction 映射、超时/取消、4xx/429/5xx 分类、正文与密钥不出日志。
- `backend/internal/handler/qwen_audio_test.go`：multipart/JSON 校验、平台与模型门、账号 failover、失败不计费、成功 usage 元数据。
- `backend/internal/handler/qwen_audio_billing_test.go`：中文字符计数、WAV 精确时长、重复 client request ID 不合并两笔真实音频请求。
- `backend/internal/server/routes/gateway_test.go`：`/v1/audio/transcriptions` 与 `/v1/audio/speech` 的 middleware 和路由归属。

验证门：

```bash
cd sub2api-hk-src
make test
make secret-scan
```

随后在 HK 内网用 Hermes 专用 key 做两次最小真实探针：短 WAV ASR 返回非空 `text`；短中文 TTS 返回可被 `ffprobe` 识别的 MP3。只记录状态、模型、时长、字节数与 usage 增量，不输出正文或凭据。

## 阶段 2：Hermes QQ 语音策略补丁

### 2.1 使用可复现的派生镜像

不使用运行时 monkeypatch，也不复制 3000 多行 QQ adapter。新增：

- `qqbot-hk/Dockerfile`：`FROM` 仍固定当前 Hermes v0.21.0 digest，只执行最小源码补丁并产出项目自有镜像。
- `qqbot-hk/scripts/patch-hermes-audio.py`：对 pinned adapter 的预期片段和文件 SHA 做 fail-closed 校验后，完成两项改动：
  1. 增加 `QQ_STT_PREFER_BUILTIN` 开关；为 `false` 时忽略 `asr_refer_text`，始终走配置的外部 STT。
  2. 只要入站附件是 voice，就把最终 `MessageEvent.message_type` 保持为 `MessageType.VOICE`；识别失败也不能降为 TEXT。
- `qqbot-hk/scripts/verify-hermes-audio.py`：随 Dockerfile 复制到 `/opt/hermes/verify-hermes-audio.py`，在真实派生镜像内导入 adapter 并运行行为 smoke。
- `qqbot-hk/docker-compose.yml`：由固定上游 digest 改为构建并运行该派生镜像；仍不开放端口、不挂 Docker socket、不改变网络和资源限制。

补丁脚本不是宽松字符串替换：上游源码不匹配时镜像构建直接失败，避免 Hermes digest 更新后静默打错代码。后续上游原生支持两个行为时，删除补丁和派生镜像，恢复直接固定官方 digest。

### 2.2 配置 ASR/TTS

修改 `qqbot-hk/config/hermes-config.yaml`：

```yaml
voice:
  auto_tts: true

tts:
  provider: openai
  openai:
    base_url: http://sub2api:8080/v1
    model: qwen-audio-3.0-tts-plus
    voice: longanhuan_v3.6

platform_toolsets:
  qqbot:
    - web
    - vision
    - skills
    - tts
    - todo
```

修改 `qqbot-hk/scripts/install-server.sh`，继续只从现有 `sub2api-api-key` 秘密文件读取一次 key，并把以下变量写入权限 `0600` 的 `/opt/data/.env`：

```text
QQ_STT_PREFER_BUILTIN=false
QQ_STT_BASE_URL=http://sub2api:8080/v1
QQ_STT_MODEL=qwen-audio-3.0-asr-flash
QQ_STT_API_KEY=<与 SUB2API_API_KEY 相同的值>
VOICE_TOOLS_OPENAI_KEY=<与 SUB2API_API_KEY 相同的值>
```

不在 YAML、Compose、Git、命令行或日志中复制真实 key。`.env.example` 只增加无秘密的行为/端点示例和说明。

### 2.3 固定回复语义

修改 `qqbot-hk/config/SOUL.md`：

- 入站为语音时正常回答；`voice.auto_tts` 负责把最终回答转为语音，不要求 Agent 再调用一次 TTS，避免重复两条语音。
- 普通文本仍返回文本。只有用户明确要求语音、朗读、朗唱或唱歌时才调用 `text_to_speech`。
- “唱歌”先生成简短原创或公版风格文本，再以 `instructions` 指定“中文、有节奏、带情绪地朗唱，不加伴奏”；不得声称生成了旋律歌曲。
- 看到 `[Voice] [语音识别失败]` 时只回复“这段语音没识别清楚，请重发或改用文字”，不猜测内容。
- 不朗读凭据、内部日志、部署路径或其他会话内容；现有群聊隐私与工具限制继续优先。

普通文本意图依赖现有 `text_to_speech` 工具的 `[[audio_as_voice]]`/`MEDIA:` 交付链；QQ adapter 已实现 `send_voice(... MEDIA_TYPE_VOICE ...)`，无需在 `smart_group_qq` 再造一条发送路径。

### 2.4 QQ Bot 测试

新增：

- `qqbot-hk/tests/test_hermes_audio_patch.py`：固定输入片段、重复执行幂等、错误 SHA/缺失哨兵 fail-closed；验证关闭 built-in ASR 后仍传入外部 STT，voice 事件类型不丢失。
- 扩展 `qqbot-hk/tests/test_smart_group_qq.py`：语音文本仍经过当前群 ACL、审核、记忆与 session 隔离；私聊不被群插件改写。
- 镜像 smoke：构建派生镜像后导入真实 `QQBotAdapter`，用 fake HTTP/WebSocket 验证 `QQ_STT_PREFER_BUILTIN=false` 和 `MessageType.VOICE`，不测试复制的伪接口。
- 配置测试：QQ 工具集含 `tts` 但仍不含 terminal/file/code execution；TTS provider、模型、base URL 与 voice 精确匹配；秘密只存在部署态 `.env`。

本地验证门：

```bash
cd qqbot-hk
python -m unittest discover -s tests -v
docker compose build hermes-qqbot
docker compose run --rm --no-deps hermes-qqbot python /opt/hermes/verify-hermes-audio.py
```

## 阶段 3：部署与真实 QQ 验收

部署顺序不可倒置：

1. 在 `sub2api-hk-src` 提交、经代理推送精确 SHA；HK 获取该 SHA，只重建 `sub2api`，不重置 Postgres/Redis volume。
2. 配置账号的 Qwen 音频 HTTP/WSS endpoint 和分组 81 的实际音频单价；运行 ASR/TTS 内网探针并核验 usage。
3. 核验 Sub2API 镜像、容器 health、`127.0.0.1:8080/health`、Caddy SNI 与公网 health；音频业务调用仍从 QQ 容器走内网，不新增公网路由。
4. 在 `qqbot-hk` 提交、经代理推送精确 SHA；HK 构建固定 digest 的派生镜像，备份 Compose 与 `/opt/qqbot-hk-deploy/hermes-data/{config.yaml,SOUL.md,plugins}` 后只重建 `hermes-qqbot`。
5. 运行扩展后的 `scripts/verify-server.sh`：检查派生镜像标签/摘要、补丁标记、五个语音环境变量存在性、TTS 配置、`tts` 工具集、Sub2API 两个音频端点可达、容器健康；只输出布尔值和计数。
6. 使用新 QQ session 或 `/reset` 执行验收矩阵：
   - 已 pairing 的 QQ 好友直接发送语音，不 `@`：Sub2API 产生一笔 Qwen ASR usage，机器人返回一条 QQ 原生语音。
   - 白名单群 `@机器人` 后发送语音：同样识别并语音回复；不泄漏其他群/私聊上下文。
   - 群内未 `@` 语音：机器人收不到事件、无回复；这是官方边界，不记作失败。
   - 普通文本问题：只返回文本，不产生 TTS usage。
   - 文本“请用语音回复……”：返回一条原生语音，不重复发送文本附件。
   - 文本“唱一段……”：返回朗唱语音；回复措辞不声称存在旋律或伴奏。
   - ASR 上游故障/不可识别音频：不采用 `asr_refer_text`，不猜内容，返回固定重发提示。
   - TTS 上游故障：不暴露本地路径或上游错误；回落为一条简短文本失败提示，不重复扣费。
7. 观察至少一个 QQ WebSocket reconnect 周期，确认重连后 ASR/TTS、沙箱私聊边界、群 ACL 与工具限制仍有效。

## 回滚

- 先回滚 `qqbot-hk` 到上一精确提交和官方固定 digest，恢复部署前 `config.yaml`/`SOUL.md`，运行 install + verify；此时 QQ 回到原有文本回复与腾讯 ASR 优先行为。
- 再回滚 Sub2API 到上一精确 SHA。只有确认没有客户端调用 `/v1/audio/*` 后才能移除音频路由；不删除 usage 历史。
- 恢复分组 81 与账号 970 的部署前配置备份；不得清库、删除 key 或重置共享 volume。
- 回滚后复验原有三模型、QQ 好友文本、白名单群 @ 文本、容器 health 和公网 health。

## Critical files

1. `sub2api-hk-src/backend/internal/service/qwen_audio.go` — DashScope ASR HTTP 与 TTS WebSocket 适配、超时和响应解析。
2. `sub2api-hk-src/backend/internal/handler/qwen_audio.go` — OpenAI 音频契约、调度、审核、failover 与 usage 边界。
3. `sub2api-hk-src/backend/internal/server/routes/gateway.go` — `/v1/audio/transcriptions`、`/v1/audio/speech` 的唯一网关入口。
4. `qqbot-hk/scripts/patch-hermes-audio.py` — 仅 Qwen ASR 与 VOICE 类型保留的最小 pinned-source 补丁。
5. `qqbot-hk/config/hermes-config.yaml` — 自动语音回复、Qwen TTS 和 QQ `tts` 工具权限。
6. `qqbot-hk/scripts/install-server.sh` — 同一专用 Sub2API key 到 STT/TTS 环境变量的唯一秘密注入路径。
7. `qqbot-hk/config/SOUL.md` — 文本/语音/朗唱意图和识别失败的用户可见语义。
8. `qqbot-hk/scripts/verify-server.sh` — 部署态语音配置、镜像、端点与无秘密验证。
