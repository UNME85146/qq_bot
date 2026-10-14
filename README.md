# QQ Bot

[English](#english) | [简体中文](#chinese) · [Operation manual](操作手册.md#english) | [操作手册](操作手册.md#chinese)

<a id="english"></a>
## English

A NoneBot2 and OneBot v11 QQ bot with permission-aware chat, structured information delivery, provider resilience, media workflows, reminders, memory, and audit tooling.

This is the private development repository. Runtime credentials, QQ identifiers, databases, logs, login state, and rollback packages remain outside Git. The public GitHub repository is a separate sanitized export, not a mirror of this tree.

## Highlights

- Stable bot-owned persona derived from validated, low-sensitivity aggregate metrics. Raw chat text, source QQ identifiers, free-text profiles, and group identifiers are not prompt inputs.
- Root and owner permissions, private/group allowlists, group mute controls, per-group FIFO chat queues, reminders, memory, image understanding, stickers, and audited administration commands.
- Group reply paths do not run project-level safety classifiers. Text, group images, image generation, explicit voice, search/evaluation, repeats, stickers, and pending enqueue go directly through their feature routes; private-chat safety and low-sensitivity long-term storage controls remain enabled.
- Normal short group model calls use at most 320 completion tokens, forward the configured reasoning effort, and stop at an 18-second default hard deadline. Dormant-session relation checks use 32 tokens and a 1.2-second deadline. Image understanding uses a separate breaker and 30-second deadline, so image failures cannot open the chat breaker.
- Technical explanation requests use a separate `single_message_long` mode: they bypass the normal 320-token group cap and send the complete formatted answer as one QQ message without the ordinary bubble/truncation limit. Non-technical chat remains short.
- Successful image understanding returns a short fallback plus a semantic sticker intent. Group and private routes send one matching safe local sticker without a text caption; only a missing match or media-send failure uses the short text. Plain incoming sticker media is analyzed first unless the user explicitly requests a sticker battle.
- Current group display names and explicit "do not use this phrase/name" preferences are persisted separately from historical style. Question-like pending rows expire after 30 days by default without being deleted or marked answered.
- Dedicated structured reply mode for help, market, and search results. Full information messages are attempted first and are summarized once only when OneBot explicitly rejects their length.
- Information-feature audits store the actual successfully sent bubble text, delivery status, and available OneBot message IDs in schema v4.
- A-share quotes use HiThink as the primary provider and AkShare only as the fallback, with closed/open/half-open circuit recovery. The public template keeps `QQ_BOT_HITHINK_API_KEY` empty; configure it only in an ignored `.env` file or service manager. AkShare fallback uses Tencent Securities for batch and name lookups, while a canonical `000001.SZ`-style code uses the Tencent quote fast path. Missing, non-positive, or suspended prices are unavailable rather than reported as current. Individual A-shares can be queried by code, Chinese short name, company name, or a full-width/half-width parenthesized alias. A-share and US-share reports return 20 sector messages with 10 stocks per sector, including name, code, previous close, current price, and percentage change. When a sector does not contain five actual gainers and five actual losers, the report explicitly labels the top/bottom five as relative leaders/laggards.
- Video parsing, news feeds, news subscriptions, and scheduled news delivery have been removed from the runtime and dashboard.
- Optional Codex Runway monitoring runs every six hours from bot startup, first sending after six hours. Reconnection preserves the startup anchor and missed intervals are not replayed in a burst. The daily local-rendered usage-ranking image starts at `17:15` in `Asia/Shanghai`; missed daily windows are skipped. SQLite schema v5 durably claims each scheduled slot before external work, preventing duplicate attempts after reconnects or ambiguous failures. Trusted X/Twitter text is translated completely into Chinese without application-level truncation; transport splitting occurs only after an explicit OneBot length rejection. Both monitors are disabled by default.
- OpenAI-compatible speech and image endpoints are optional. Speech is remote-model only: it supports either binary `/audio/speech` or Chat Completions audio selected by `speech.apiMode`; Chat Audio responses are size-, format-, Base64-, and transcript-validated. Explicit voice commands can be enabled while random voice replies remain disabled, and QQ record delivery is attempted once to avoid duplicate audio after an ambiguous timeout. The retired local MOSS TTS service is not part of the runtime.

## Requirements

- Python 3.12 or 3.13
- NoneBot2 with the OneBot v11 adapter
- LLBot/LuckyLilliaBot on Linux using the OneBot v11 reverse WebSocket
- `ffmpeg` for bounded GIF/WebP image preprocessing
- Optional market dependencies: AkShare (Tencent Securities fallback) and yfinance; HiThink uses the built-in HTTP client
## Quick Start

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[market]"
Copy-Item .env.example .env
Copy-Item config/config.example.json config/config.json
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[market]'
cp .env.example .env
cp config/config.example.json config/config.json
```

Configure placeholders in `config/config.json`:

- `BOT_QQ`, `ROOT_QQ`, `OWNER_QQ`, and `ALLOWED_GROUP_ID`
- `model.baseUrl`, `model.name`, and the API-key environment variable name
- OneBot reverse WebSocket host, port, and token environment variable
- Optional market providers, search provider, speech, image, Codex Runway, and usage-ranking settings; the template configures HiThink as the A-share primary provider and AkShare as the only fallback

Set secrets only in `.env` or your service manager. The supplied `.env.example` keeps every credential value, including `QQ_BOT_HITHINK_API_KEY`, empty. Do not put literal credentials in JSON templates, scripts, shell history, logs, or commits.

`config/persona_profile.example.json` contains numeric demonstration metrics only so the public example can be validated. For a real deployment, generate your own ignored `persona_profile.local.json`, point `persona.profilePath` to it, and keep source identifiers and raw history out of version control. Runtime has no automatic example-profile fallback.

Start the bot:

```powershell
.\.venv\Scripts\python bot.py
```

The default reverse WebSocket target is:

```text
ws://127.0.0.1:8081/onebot/v11/ws
```

## User Commands

Group information entry points:

```text
/help
#A股          #美股
#比亚迪       #牧原股份（牧原）
#chat 查一下 <query> [--page N]
#chat 帮我查一下 <query> [--page N]
#画图 <prompt>  #改图 <instruction>
```

Market reports send one message per sector. Search keeps each title, source, summary, and URL together and retains paging for larger result sets. If OneBot explicitly rejects a complete information message as too long, the bot performs one compact-summary retry instead of proactively truncating it.

Group image understanding skips project-level safety classification and calls the configured multimodal model directly. Triggered OneBot images first resolve a fresh `get_image` URL, accept only trusted QQ CDN HTTPS downloads up to 8 MiB, and inline JPEG/PNG; GIF/WebP are converted to a bounded PNG first frame with ffmpeg. A successful visual call produces a controlled semantic sticker intent and sends one matching safe local sticker without a text caption; the just-received asset is excluded. Missing matches and media-send failures use the generated short fallback. Private images retain classification and short refusal behavior. Private classification results are cached for five minutes, concurrent requests for the same prepared image share one SHA-256 cache key, and image model calls use the configured reasoning budget and independent vision deadline.

Owner/root private commands include:

```text
/help
/status
/memory
/memory clear
/audit last
/reload profile
/ping model
/allow ...
/mute ...
/voice ...
```

Only root users can manage `/owner ...`.

## Operations Tools

Read the current redacted provider snapshot without external requests:

```powershell
.\.venv\Scripts\python tools/check_provider_status.py --snapshot-only
```

Actively probe currently configured providers and require a healthy result:

```powershell
.\.venv\Scripts\python tools/check_provider_status.py --require-healthy
```

Inspect the Bot, LLBot, OneBot, and retired local-TTS status:

```powershell
.\.venv\Scripts\python tools/inspect_runtime_status.py --summary --limit 5
```

Inspect historical TTS retirement state:

```bash
.venv/bin/python tools/manage_tts_retirement.py status
```

`apply`, `rollback`, rehearsal, and deletion are mutating operations. Review the generated plan, preserve rollback material, and use a separately approved operation window.

## Verification

The private repository carries the full test suite. A public-export smoke check can still validate syntax and configuration loading:

```powershell
.\.venv\Scripts\python -m compileall app bot.py tools
$env:QQ_BOT_MODEL_API_KEY = "placeholder-for-import-check"
$env:QQ_BOT_HITHINK_API_KEY = "placeholder-for-import-check"
$env:QQ_BOT_CONFIG_PATH = "config/config.example.json"
.\.venv\Scripts\python -c "import bot; import nonebot; print(nonebot.get_driver().type)"
```

Passing local tests or probes does not prove live QQ delivery, provider success, or recovery behavior. Validate each enabled production path separately.

## Security

- Never commit `.env`, `config/config.json`, local persona profiles, databases, backups, logs, runtime artifacts, QR codes, LLBot cache/login state, tokens, private QQ/group IDs, or server credentials.
- Provider telemetry is intentionally redacted. Keep raw response bodies and secrets out of durable audit records.
- Public releases must contain only the approved export paths and must pass secret and private-identifier scans before push.
- Keep dependencies and OneBot/LLBot endpoints private by default; expose only the minimum required network surface.

## Ubuntu 24.04 production deployment and rollback

The active production path is Linux-only: the Bot runs as `qq-bot.service`; the `qq-pmhq` container starts Linux QQ and PMHQ, and the `llbot` container uses `--pmhq-port=13000 --pmhq-host=127.0.0.1`. PMHQ and OneBot stay on loopback. The current pinned components are LLBot 8.3.2, PMHQ 9.0.6, and Linux QQ 3.2.34-53644. Direct-mode and other retired connectors must not run alongside this QQ session.

1. **Freeze and back up.** Identify the private SSH target through the locally configured alias, inspect existing workloads, disk, ports, and service identities, then freeze a reviewed commit, dependency constraints, and SHA-256 manifest. Stop the Bot/LLBot pair before the final SQLite online backup; require `PRAGMA quick_check=ok` and the current schema. Keep QQ login state, database, profile, stickers, logs, and rollback material outside Git.
2. **Install the Bot.** Install Python 3.12/3.13, `ffmpeg`, `fontconfig`, and the dashboard dependencies. Place the source under a private runtime root such as `/opt/qq_bot`, create `.env` and `config/config.json` with mode `0600`, and configure the OneBot reverse WebSocket at `ws://127.0.0.1:8081/onebot/v11/ws`.
3. **Install Linux QQ, PMHQ, and LLBot.** Keep LLBot data, PMHQ configuration, and Linux QQ session data in separate private directories. Use `config/pmhq.example.json` as the non-secret template; write the existing LLBot Auth Token only into the private PMHQ configuration. Bind PMHQ to `127.0.0.1:13000`, LLBot WebUI to loopback, and OneBot reverse WebSocket to the Bot listener. Start PMHQ with its QQ session mount and then LLBot with explicit PMHQ parameters; keep component versions pinned. The PMHQ container provides Xvfb, so Windows/WSL is unnecessary. A stopped direct-mode container and private configuration backup provide rollback; never start both QQ login paths. Dashboard QR requests continue through the existing authenticated LLBot WebUI.
4. **Install the dashboard and tunnel.** Run `tools/run_dashboard.py` on `127.0.0.1:3011` behind the authenticated HTTPS tunnel. Use `scripts/server/qqbot-dashboard.service` as the service template and configure its LLBot WebUI token file. `RuntimeDirectory=qqbot-dashboard` creates a private directory owned by the dashboard user; QR responses from LLBot are cached at `/run/qqbot-dashboard/login-qrcode.png`, outside the connector's login directory. Validate public `/login=200`, anonymous `/api/status=401` and `/api/qq/qr=401`, then sign in and verify that the displayed status reflects LLBot's actual login state.
5. **Establish one QQ session.** Start `qq-bot.service`, `qq-pmhq`, and the LLBot PMHQ container, use the dashboard's **Refresh QR** action when the existing QR is missing or expired, and scan it from the QQ client. A successful runtime check requires the Bot listener, LLBot-to-OneBot WebSocket, LLBot online state, three consecutive `ready=true` probes, SQLite integrity, and a user-visible QQ login. A green Bot listener alone does not prove QQ delivery.
6. **Clean up and retain rollback.** Generate a fresh exact-target cleanup plan after acceptance. Remove retired connector staging, old release archives, unused caches, and unreferenced images only after verifying their paths and references. Preserve the current `.env`, configuration, database, logs, active LLBot login state, current image, virtual environment, service units, and the explicitly retained rollback package. If cleanup partially succeeds, verify the protected runtime before any recovery action. Publish only the secret-scanned private code and a separate sanitized public export.

<a id="chinese"></a>
## 简体中文

[English](#english) | [简体中文](#chinese)

本项目是基于 NoneBot2 与 OneBot v11 的 QQ Bot，包含权限、聊天、行情与信息服务、媒体、提醒、记忆和审计。以下为生产部署及回滚说明；功能详情、示例命令和安全约束可在上方英文版查看，实际配置和密钥均不进入 Git。

### 功能概览

- 角色画像只使用经过校验的低敏聚合指标；原始聊天、来源 QQ、自由文本画像和群号不直接成为提示词输入。
- root/owner 权限、私聊/群聊白名单、群禁言控制、逐群 FIFO 对话、提醒、记忆、表情包与审计命令可独立配置。普通群回复不经项目级安全分类；私聊安全与低敏长期存储边界仍生效。
- 普通群聊短回复最多 320 completion tokens、默认 18 秒硬截止；技术解释使用单条长消息模式。图片理解有独立熔断器和 30 秒截止，可按语义选用安全的本地表情包，缺失/发送失败才回退短文本。
- 结构化帮助、行情与搜索优先发送完整消息；只有 OneBot 明确拒绝长度后才进行一次精简重试。schema v5 中的信息审计保存实际成功发送的内容与可用消息 ID。
- A 股行情以同花顺 HiThink 为主、AkShare（腾讯证券路径）为唯一备用；缺价/停牌不会伪装成实时行情。美股依赖 yfinance。语音、生图及定时监控按配置启用，不把健康探针等同于 QQ 客户端可见交付。
- 可选 Runway 信息从启动起每六小时发送一次，截图在北京时间 17:15 发送；schema v5 的持久化 claim 阻止重连/不确定回执下的重复尝试。两个监控默认关闭。

### 环境与快速开始

需要 Python 3.12 或 3.13、NoneBot2、Linux 上的 LLBot/LuckyLilliaBot、OneBot v11 反向 WebSocket，以及 `ffmpeg` 图片预处理。可选行情依赖 AkShare 与 yfinance；同花顺使用内置 HTTP 客户端。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[market,dashboard]'
cp .env.example .env
cp config/config.example.json config/config.json
```

Windows PowerShell 可改用 `python -m venv .venv`、`.\.venv\Scripts\python -m pip install -e ".[market,dashboard]"`、`Copy-Item` 复制示例文件。将 `BOT_QQ`、`ROOT_QQ`、`OWNER_QQ`、`ALLOWED_GROUP_ID`、模型地址与名称替换为私有配置；示例 `.env.example` 中所有凭据均为空。实际历史画像须生成被忽略的 `persona_profile.local.json`，运行时不会自动退回示例画像。默认反向 WS 地址为 `ws://127.0.0.1:8081/onebot/v11/ws`。

### 使用、运维与验证

群信息命令包括 `/help`、`#A股`、`#美股`、股票名称/代码、`#chat 查一下 <关键词> [--page N]`、`#画图`、`#改图`。root/owner 私聊可用 `/status`、`/memory`、`/audit last`、`/reload profile`、`/ping model`、`/allow`、`/mute`、`/voice`；只有 root 能管理 `/owner`。

```bash
.venv/bin/python tools/check_provider_status.py --snapshot-only
.venv/bin/python tools/check_provider_status.py --require-healthy
.venv/bin/python tools/inspect_runtime_status.py --summary --limit 5
.venv/bin/python tools/manage_tts_retirement.py status
.venv/bin/python -m compileall -q app bot.py tools
```

完整自动化测试位于私有开发仓库，公开导出只提供示例配置的语法和导入冒烟测试。部署、回滚、演练与删除均会改变运行态，必须先审计划、留备份并做独立验收；Bot ready、提供方成功、QQ 可见投递是不同证据。不要提交 `.env`、真实配置/画像、数据库、日志、二维码、LLBot 登录缓存、Token、QQ/群号或回滚包。

## Ubuntu 24.04 生产部署与回滚

本节记录服务器迁移的实际流程，但主机名、账号、路径和密钥均改为示例。执行前逐项核对目标主机与占位符。当前生产运行 Bot、LLBot、鉴权后台及 HTTPS tunnel；旧 QQ connector 和 WSL 路径均停用，不允许两个 QQ 会话并行。

### 1. 冻结源码、备份与先决条件

1. 核对 SSH 主机身份、磁盘/内存、现有服务和端口。冻结经审阅的源码版本、依赖约束和 SHA-256 清单，放入独立 staging。停止 Bot/LLBot 后使用 SQLite online backup 做最终一致备份，核对 `PRAGMA quick_check` 和当前 schema。
2. 私下迁移经验证的数据库、低敏聚合画像与表情包归档。素材先在隔离目录解包，拒绝符号链接和路径穿越，逐文件对哈希与数量；原始聊天、画像来源信息、API Key、QQ 登录态和中间归档不能进入 Git 或公开导出。
3. 在真实 QQ 登录及应用验收完成前保留归档、活动服务的回滚单元和明确保留的 rollback package。

在 Ubuntu 中从可信来源安装 Python 3.12/3.13 venv、`ffmpeg`、`fontconfig` 和经验证的 `cloudflared`；限制 SSH，后台、LLBot WebUI 和 OneBot 端口不直连公网。

### 2. 安装 Bot 与私有配置

示例目录使用 `BOT_ROOT=/opt/qq_bot`，由服务用户持有。将冻结源码安装在 `BOT_ROOT` 后创建环境：

```bash
cd "$BOT_ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --constraint /path/to/reviewed-constraints.txt '.[market,dashboard]'
cp .env.example .env
cp config/config.example.json config/config.json
chmod 600 .env config/config.json
mkdir -p data/stickers logs runtime_artifacts/secrets
```

私下填写 `.env` 和 `config/config.json`：模型密钥、可选同花顺密钥、强 OneBot Token、QQ 权限、模型端点，以及 `onebot.host=127.0.0.1`、`onebot.port=8081`。画像置于私有 `0600` 文件并配置路径，一致数据库副本及已校验素材放在 `data/`；部署输出不打印密钥。

### 3. 配置 LLBot，控制端口仅限本机

在 `/opt/llbot/data` 等私有目录保存 LLBot 配置和 Auth/WebUI token，token 文件使用 `0600`。另建 PMHQ 私有配置和 Linux QQ 登录目录，参考 `config/pmhq.example.json`；PMHQ 使用现有 Auth Token，API 只监听 `127.0.0.1:13000`。`qq-pmhq` 容器运行 Linux QQ 与 Xvfb，`llbot` 容器以 `--pmhq-port=13000 --pmhq-host=127.0.0.1` 连接；WebUI 仅绑定 `127.0.0.1`，OneBot reverse WebSocket 指向 `ws://127.0.0.1:8081/onebot/v11/ws`，与 Bot 使用同一强 Token。固定组件版本与镜像摘要，保留独立 QQ 登录目录。旧直连容器作为回滚时必须停止并禁用自动启动，不能和 PMHQ QQ 同时登录。

### 4. 安装鉴权后台与 HTTPS tunnel

用交互式命令两次输入至少 16 字符的后台口令，生成私有 `0600` scrypt 哈希；口令不放在 shell 参数或 unit 文件里：

```bash
cd "$BOT_ROOT"
.venv/bin/python tools/dashboard_password.py "$BOT_ROOT/runtime_artifacts/secrets/dashboard-admin.hash"
```

由非特权用户在 `qqbot-dashboard.service` 运行 `tools/run_dashboard.py`，设置固定 HTTPS 来源 `QQ_BOT_DASHBOARD_ORIGIN`、哈希文件、LLBot WebUI token 文件和二维码路径。`QQ_BOT_ROOT`、`QQ_BOT_CONFIG_PATH`、`QQ_BOT_ENV_PATH` 可显式指定 Bot 根目录、私有配置和环境文件；未指定时默认使用服务工作目录下的 `config/config.json` 与 `.env`。使用 `PYTHON_DOTENV_DISABLED=1`、`UMask=0077`、`NoNewPrivileges=true`、`PrivateTmp=true`、`ProtectSystem=strict` 和 `MemoryMax=512M`。后台只绑定 `127.0.0.1:3011`，不给 Docker socket 或 sudo；Cookie 带 Secure/HttpOnly/SameSite Strict，写操作校验同源。

tunnel 使用私有凭据与两条 ingress 规则；示例域名和凭据路径须由操作者替换，真实值不入仓库：

```yaml
ingress:
  - hostname: qq.example.com
    service: http://127.0.0.1:3011
  - service: http_status:404
```

先运行 `cloudflared tunnel ingress validate`，再按权限安装 `qq-bot.service`、`qqbot-dashboard.service`、`qqbot-dashboard-tunnel.service`，并管理 `qq-pmhq` 与 `llbot` 两个容器。后台本地 `/login` 正常后才切公网路径；公网 HTTPS `/login` 应为 `200`，匿名 `/api/status`、`/api/qq/qr` 应为 `401`。登录后的配置页只返回脱敏模型信息和模型 ID；主模型区域维护全局 Base URL 与 API Key，语音和生图模型从同一个 `/models` 列表获取并选择，最长文本以字数输入并由后台换算为 Token。视频解析和新闻推送已经从配置模型、运行时代码与后台开关中删除。保存后的配置需要重启 `qq-bot.service` 才会生效。后台“重启 Bot”按钮只重启同一用户的 Bot 主进程并依赖 systemd 自动拉起，不会重启 LLBot。LLBot WebUI、OneBot `8081` 和后台应用 `3011` 均不能直接公网暴露。

### 5. 建立唯一 QQ 会话并验收

启动远端 `qq-bot.service`、`qq-pmhq` 和 PMHQ 模式的 `llbot` 容器，确认 `127.0.0.1:8081` 的反向 WS。登录后台，必要时请求 LLBot 生成新 QR，再由手机 QQ 扫码；仅用户侧确认能证明客户端登录。后台每 10 秒轮询：未报告在线时只尝试展示 180 秒内的**已有**二维码，缺失或过期使用“刷新二维码”调用 LLBot WebUI 生成新的二维码；在线时隐藏二维码。

恢复数据后以服务用户执行以下校验（不输出私有状态字段）：

```bash
cd "$BOT_ROOT"
.venv/bin/python tools/inspect_runtime_status.py --summary --require-ready --limit 5
.venv/bin/python -c 'import sqlite3; d=sqlite3.connect("file:data/bot.db?mode=ro", uri=True); assert d.execute("PRAGMA quick_check").fetchone()[0] == "ok"; assert d.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5; print("db=ok schema=5")'
systemctl show -p ActiveState -p NRestarts -p User qq-bot.service qqbot-dashboard.service qqbot-dashboard-tunnel.service
```

核对连续三次 `ready=true`、无意外重启、LLBot 在线状态与 WebUI 鉴权、数据库完整性、表情包数量与哈希，以及用户 QQ 客户端可见登录。若启用定时任务，启动后六小时汇总和北京时间 17:15 截图须分别等待自然到点验收；ready 不代表消息投递成功，也不应为了报告通过而合成 QQ 消息。

### 6. 清理、保留回滚并安全发布

客户端登录且独立核验完成后，冻结独立的 SHA-256 清理计划，明确路径与镜像。核对引用及备份后，仅移除已退役 connector、旧 release、无用 staging、pip/node-gyp/apt 下载、无引用旧镜像与项目测试/编译缓存；一次执行、写回执，独立复查目标不存在、受保护服务、SQLite `quick_check` 与三次 ready。若删除或写回执中途失败，只做只读恢复核验，不重放删除。保留活动 LLBot 镜像/运行文件、虚拟环境本体、数据/配置/日志、LLBot 登录目录及缓存根目录、tunnel 凭据和明确保留的回滚包；缓存文件须逐项判定过期和引用。

回滚先停止远端 Bot/LLBot，保持 QQ 单写者，再用经验证的备份恢复旧运行环境或网站，仅恢复对应 unit/路由；重新校验数据库、ready 与登录。回滚归档属于私有运维材料，不进 Git。私有 Gitee 代码先测试和扫密钥再推送，公开 GitHub 只接受独立扫描过的白名单脱敏导出。
