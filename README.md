# QQ Bot

[English](#english) | [简体中文](#chinese) · [Operation manual](操作手册.md#english) | [操作手册](操作手册.md#chinese)

<a id="english"></a>
## English

A NoneBot2 and OneBot v11 QQ bot with permission-aware chat, structured information delivery, provider resilience, media workflows, reminders, memory, and audit tooling.

This GitHub repository is a sanitized public export. The private development tree, tests, local design and acceptance documents, credentials, QQ identifiers, runtime databases, logs, login state, and rollback packages are not mirrored here.

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
- Automatic Douyin/Bilibili download, categorized news commands, news subscriptions, and scheduled news delivery are disabled. Historical provider and maintenance modules remain available for rollback and audit work but are not routed from group messages.
- Optional Codex Runway monitoring runs every six hours from bot startup, first sending after six hours. Reconnection preserves the startup anchor and missed intervals are not replayed in a burst. The daily local-rendered usage-ranking image starts at `17:15` in `Asia/Shanghai`; missed daily windows are skipped. SQLite schema v5 durably claims each scheduled slot before external work, preventing duplicate attempts after reconnects or ambiguous failures. Trusted X/Twitter text is translated completely into Chinese without application-level truncation; transport splitting occurs only after an explicit OneBot length rejection. Both monitors are disabled by default.
- OpenAI-compatible speech and image endpoints are optional. Speech supports either binary `/audio/speech` or Chat Completions audio selected by `speech.apiMode`; Chat Audio responses are size-, format-, Base64-, and transcript-validated. Explicit voice commands can be enabled while random voice replies remain disabled, and QQ record delivery is attempted once to avoid duplicate audio after an ambiguous timeout. The historical local TTS service is not shipped.

## Requirements

- Python 3.12 or 3.13
- NoneBot2 with the OneBot v11 adapter
- A OneBot implementation such as NapCat using reverse WebSocket
- `ffmpeg` for bounded GIF/WebP image preprocessing and historical media tooling
- Optional market dependencies: AkShare (Tencent Securities fallback) and yfinance; HiThink uses the built-in HTTP client
- Optional video dependencies: yt-dlp and socksio

## Quick Start

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[market,video]"
Copy-Item .env.example .env
Copy-Item config/config.example.json config/config.json
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[market,video]'
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

Only root users can manage `/owner ...` and execute the `/video upload plan|apply ...` acceptance flow.

## Operations Tools

Read the current redacted provider snapshot without external requests:

```powershell
.\.venv\Scripts\python tools/check_provider_status.py --snapshot-only
```

Actively probe currently configured providers and require a healthy result:

```powershell
.\.venv\Scripts\python tools/check_provider_status.py --require-healthy
```

Preflight video-provider egress:

```powershell
.\.venv\Scripts\python tools/check_video_egress.py --require-ok
```

Inspect bot, NapCat, OneBot, and historical TTS status:

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

Passing local tests or probes does not prove live QQ delivery, provider success, video upload, or recovery behavior. Validate each enabled production path separately.

## Security

- Never commit `.env`, `config/config.json`, local persona profiles, databases, backups, logs, runtime artifacts, QR codes, NapCat cache/login state, tokens, private QQ/group IDs, or server credentials.
- Provider and video telemetry is intentionally redacted. Keep raw response bodies and secrets out of durable audit records.
- Public releases must contain only the approved export paths and must pass secret and private-identifier scans before push.
- Keep dependencies and OneBot/NapCat endpoints private by default; expose only the minimum required network surface.

## Ubuntu 24.04 production deployment and rollback

1. **Freeze and back up.** Identify the SSH host, existing workloads, disk, ports, and exact services to replace. Freeze a reviewed commit, dependency constraints, and SHA-256 manifest. Stop the source Bot/NapCat before making a final SQLite `Connection.backup()` snapshot; require `PRAGMA quick_check=ok` and schema 5. Back up the retired website, its units, and database. Transfer the private database, validated 30-day low-sensitivity persona, and hash-checked sticker archive without copying QQ login cache or raw chats into Git.
2. **Install private runtime.** Install Python 3.12 venv support, Docker, `ffmpeg`, `fontconfig`, and a verified `cloudflared` binary. Place the frozen source under a private runtime root and install the reviewed `.[market,video,dashboard]` dependencies with pinned constraints. Create `.env` and `config/config.json` from examples, set `0600`, populate real values only there, configure OneBot reverse WS on `127.0.0.1:8081` and HTTP API root on loopback `3100`. Restore DB, profile, and checked stickers into their private runtime locations.
3. **Prepare pinned NapCat.** Pull an architecture-checked, digest-pinned image. Before host-network startup configure WebUI `127.0.0.1:6099`, OneBot HTTP `127.0.0.1:3100` with a matching token, and reverse WS `ws://127.0.0.1:8081/onebot/v11/ws`. Use separate `config`, `QQ`, and `cache` mounts. The production limits are 3 GiB RAM, 4 GiB RAM+swap, 2 CPUs, 512 PIDs, and `10m` x 3 JSON logs. Never expose these control ports or reuse another machine's login cache.
4. **Install dashboard and tunnel.** Run `.venv/bin/python tools/dashboard_password.py <private-hash-file>` interactively with a password of at least 16 characters. Configure `qqbot-dashboard.service` with the exact HTTPS origin, private password hash, NapCat root, and separate `0600` OneBot/WebUI token copies; its process binds only `127.0.0.1:3011`, without Docker or sudo access. Use a Cloudflare Tunnel ingress rule from the operator's hostname to `http://127.0.0.1:3011` followed by `http_status:404`; validate ingress. Start the dashboard first, then stop/disable only the retired site/tunnel and enable the new tunnel. Check public `/login=200`, anonymous `/api/status=401` and `/api/qq/qr=401`.
5. **Establish one QQ session.** Before promoting the remote QQ login, disable and stop the retired WSL `qq-bot.service`, change its NapCat container to `restart=no` and stop it, remove the matching Windows WSL autostart task/holder, then terminate the old distro. A Windows-only autostart removal is insufficient: manually booting WSL can otherwise start a second QQ session. Verify the internal settings after any controlled WSL boot. Start the remote `qq-bot.service` and pinned container, then sign in on the HTTPS dashboard and scan a fresh QR. The page polls every 10 seconds and shows an existing QR at most 180 seconds old when QQ is not online. It does **not** generate another QR automatically; use **Refresh QR** if missing/expired. Online QQ hides the QR. Run `tools/inspect_runtime_status.py --summary --require-ready --limit 5`, verify three consecutive ready probes, DB integrity, container/image identity, loopback ports, sticker hashes, and the user-visible QQ login. Scheduled six-hour summaries and the 17:15 Beijing screenshot need their own natural-time acceptance.
6. **Clean up and retain rollback.** Generate a separate SHA-256-bound, exact-target cleanup plan after acceptance; delete retired site/staging, unused caches, apt downloads, and unreferenced old images once. Verify the receipt, target absence, current image, Bot/QQ readiness, and DB again. Do not prune current NapCat `config/QQ/cache`, credentials, logs, database, venv, or retained rollback archives. If deletion partly succeeds, verify recovery read-only rather than replaying. For rollback, first stop the remote QQ writer, restore the verified prior data/units/route, and recheck login. Publish only secret-scanned private code and a separate sanitized public export.

<a id="chinese"></a>
## 简体中文

[English](#english) | [简体中文](#chinese)

本公开仓库是 QQ Bot 的脱敏导出，采用 NoneBot2 和 OneBot v11，提供权限管理、聊天、行情/信息服务、媒体、提醒、记忆与审计。私有开发树、真实标识、密钥、数据库、日志、登录态及回滚包不会镜像到这里。以下为完整部署流程。

### 功能概览

- 角色画像只使用经过校验的低敏聚合指标；原始聊天、来源 QQ、自由文本画像和群号不直接成为提示词输入。
- root/owner 权限、私聊/群聊白名单、群禁言控制、逐群 FIFO 对话、提醒、记忆、表情包与审计命令可独立配置。普通群回复不经项目级安全分类；私聊安全与低敏长期存储边界仍生效。
- 普通群聊短回复最多 320 completion tokens、默认 18 秒硬截止；技术解释使用单条长消息模式。图片理解有独立熔断器和 30 秒截止，可按语义选用安全的本地表情包，缺失/发送失败才回退短文本。
- 结构化帮助、行情与搜索优先发送完整消息；只有 OneBot 明确拒绝长度后才进行一次精简重试。schema v5 中的信息审计保存实际成功发送的内容与可用消息 ID。
- A 股行情以同花顺 HiThink 为主、AkShare（腾讯证券路径）为唯一备用；缺价/停牌不会伪装成实时行情。美股依赖 yfinance。语音、生图、视频及定时监控按配置启用，不把健康探针等同于 QQ 客户端可见交付。
- 可选 Runway 信息从启动起每六小时发送一次，截图在北京时间 17:15 发送；schema v5 的持久化 claim 阻止重连/不确定回执下的重复尝试。两个监控默认关闭。

### 环境与快速开始

需要 Python 3.12 或 3.13、NoneBot2、OneBot v11 实现（如 NapCat），图片预处理/视频工具需要 `ffmpeg`。可选行情依赖 AkShare 与 yfinance，视频依赖 yt-dlp/socksio；同花顺使用内置 HTTP 客户端。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[market,video,dashboard]'
cp .env.example .env
cp config/config.example.json config/config.json
```

Windows PowerShell 可改用 `python -m venv .venv`、`.\.venv\Scripts\python -m pip install -e ".[market,video,dashboard]"`、`Copy-Item` 复制示例文件。将 `BOT_QQ`、`ROOT_QQ`、`OWNER_QQ`、`ALLOWED_GROUP_ID`、模型地址与名称替换为私有配置；示例 `.env.example` 中所有凭据均为空。实际历史画像须生成被忽略的 `persona_profile.local.json`，运行时不会自动退回示例画像。默认反向 WS 地址为 `ws://127.0.0.1:8081/onebot/v11/ws`。

### 使用、运维与验证

群信息命令包括 `/help`、`#A股`、`#美股`、股票名称/代码、`#chat 查一下 <关键词> [--page N]`、`#画图`、`#改图`。root/owner 私聊可用 `/status`、`/memory`、`/audit last`、`/reload profile`、`/ping model`、`/allow`、`/mute`、`/voice`；只有 root 能管理 `/owner` 与受控 `/video upload plan|apply`。

```bash
.venv/bin/python tools/check_provider_status.py --snapshot-only
.venv/bin/python tools/check_provider_status.py --require-healthy
.venv/bin/python tools/check_video_egress.py --require-ok
.venv/bin/python tools/inspect_runtime_status.py --summary --limit 5
.venv/bin/python tools/manage_tts_retirement.py status
.venv/bin/python -m compileall -q app bot.py tools
```

完整自动化测试位于私有开发仓库，公开导出只提供示例配置的语法和导入冒烟测试。部署、回滚、演练与删除均会改变运行态，必须先审计划、留备份并做独立验收；Bot ready、提供方成功、QQ 可见投递是不同证据。不要提交 `.env`、真实配置/画像、数据库、日志、二维码、NapCat 登录缓存、Token、QQ/群号或回滚包。

## Ubuntu 24.04 生产部署与回滚

本节是公开安全的部署流程，所有路径与域名均为示例；真实主机名、QQ 标识、Token、口令哈希、数据库、素材和登录缓存不能进入仓库。Bot、NapCat、鉴权后台与 HTTPS tunnel 位于同一生产服务器。可保留已停止的旧环境供回滚，但任何时刻只能有一份 QQ 会话。

### 1. 冻结源码、备份与先决条件

1. 核对 SSH 身份、容量、现有服务与端口；记录将被替换的旧网页/tunnel，不影响无关工作负载。
2. 在 staging 中冻结经审阅的源码版本、依赖约束与 SHA-256 清单。源端停 Bot/NapCat 后以 `sqlite3.Connection.backup()` 生成一致数据库副本，并检查 `PRAGMA quick_check` 与 schema；不得直接归档在线数据库目录或迁移 QQ 登录缓存。
3. 私下传输备份数据库、已校验低敏画像与表情包归档。素材先在隔离目录解包，拒绝符号链接/路径穿越，逐文件核对数量与哈希后再放入运行目录；原始聊天、Token 与画像来源不进入公开仓库。
4. 切换前备份旧网站文件、unit 与数据库；直到真实 QQ 登录和部署后验收通过才考虑清理停用源环境。

从可信渠道安装 Ubuntu Python 3.12 venv、Docker、`ffmpeg`、`fontconfig` 和经验证的 `cloudflared`。使用前检查 NapCat 固定版本的架构与摘要，动态 `latest` 标签不构成版本锁；后台、WebUI 和 OneBot 原生端口不暴露到公网。

### 2. 安装 Bot 与私有配置

示例目录为 `BOT_ROOT=/opt/qq_bot`、`NAPCAT_ROOT=/opt/napcat`，由独立的非特权服务用户持有。安装冻结源码并准备环境：

```bash
cd "$BOT_ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --constraint /path/to/reviewed-constraints.txt '.[market,video,dashboard]'
cp .env.example .env
cp config/config.example.json config/config.json
chmod 600 .env config/config.json
mkdir -p data/stickers logs runtime_artifacts/secrets
```

私下配置 `.env` 和 `config/config.json`：QQ 白名单、模型端点/密钥、可选同花顺密钥与一致的强 OneBot Token。反向 WS 监听 `127.0.0.1:8081`，`onebot.apiRoot` 指向本机 HTTP 端口（此示例为 `3100`）。校验后的画像置于 `0600` 文件并设置路径，恢复 SQLite 副本到 `data/`，核对表情包哈希；日志和命令输出不能打印密钥。

### 3. 先配置 NapCat，再启用 host 网络

在 `NAPCAT_ROOT` 建立私有 `config`、`QQ`、`cache` 挂载目录；`--network host` 启动前先配置固定镜像。WebUI 绑定 `127.0.0.1:6099`，带 Token 的 OneBot HTTP 绑定 `127.0.0.1:3100`，反向 WS 指向 `ws://127.0.0.1:8081/onebot/v11/ws`。后台用户只读独立的 `0600` WebUI/OneBot Token 副本，不访问 NapCat UID 目录或 Docker socket；不复制其他机器登录态。

容器使用固定镜像 ID、三个挂载目录、重启策略、3 GiB 内存、4 GiB 内存加 swap、2 CPU、512 PID 和 `10m` × 3 日志上限。启动后核对实际镜像、挂载与 loopback 监听。清理时保护 NapCat 的 `QQ`、`config` 和缓存**根目录**；缓存文件需单独核实过期/未引用及 QQ 登录状态后才能删除。

### 4. 安装后台与 HTTPS tunnel

交互式输入至少 16 字符口令两次，生成私有 `0600` scrypt 哈希，不把密码放在命令参数中：

```bash
cd "$BOT_ROOT"
.venv/bin/python tools/dashboard_password.py "$BOT_ROOT/runtime_artifacts/secrets/dashboard-admin.hash"
```

用非特权服务用户在 `qqbot-dashboard.service` 运行 `tools/run_dashboard.py`。设置精确 HTTPS 来源 `QQ_BOT_DASHBOARD_ORIGIN`、私有哈希文件、NapCat 根目录、`QQ_BOT_DASHBOARD_ONEBOT_HTTP_PORT=3100` 和独立的 OneBot/WebUI Token 文件。使用 `PYTHON_DOTENV_DISABLED=1`、`UMask=0077`、`NoNewPrivileges=true`、`PrivateTmp=true`、`ProtectSystem=strict`、`MemoryMax=512M`。后台仅监听 `127.0.0.1:3011` 且要求 HTTPS 来源鉴权，不授予 Docker socket 或 sudo。

只将后台经私有 Cloudflare Tunnel 凭据和已校验的 ingress 对外提供，示例域名须替换为操作者控制的地址：

```yaml
ingress:
  - hostname: qq.example.com
    service: http://127.0.0.1:3011
  - service: http_status:404
```

切换前运行 `cloudflared tunnel ingress validate`，按已审阅路径安装 `qq-bot.service`、`qqbot-dashboard.service` 与 `qqbot-dashboard-tunnel.service`。先启动后台并验证 loopback `/login`，再只停用旧网站及其 tunnel、保留归档和 unit 备份、启用新 tunnel。HTTPS `/login` 应为 `200`，匿名 `/api/status` 和 `/api/qq/qr` 应为 `401`。后台 `3011`、WebUI `6099`、OneBot HTTP `3100` 与反向 WS `8081` 仅在 loopback 监听。

### 5. 建立唯一 QQ 登录并验证真实链路

远端 QQ 登录前，旧 WSL 中先 `systemctl disable --now qq-bot.service`、将旧 NapCat 容器改为 `restart=no` 并停止，移除 Windows 对应的 WSL 自启任务和 holder，再终止旧发行版。**仅停 Windows 任务不够**：手工启动 WSL 时 systemd/Docker 仍可能拉起第二份 QQ。受控启动 WSL 后应复核内层状态，日常不要为监测远端而启动它。然后启动远端 Bot 和固定镜像 NapCat，确认反向 WS 连通，再登录后台、扫码。网页每 10 秒轮询；QQ 未被报告在线时只请求 180 秒内的**已有**二维码，缺失或过期不会自动生成，需点“刷新二维码”；在线时隐藏二维码并拒绝刷新请求。

```bash
cd "$BOT_ROOT"
.venv/bin/python tools/inspect_runtime_status.py --summary --require-ready --limit 5
.venv/bin/python -c 'import sqlite3; d=sqlite3.connect("file:data/bot.db?mode=ro", uri=True); assert d.execute("PRAGMA quick_check").fetchone()[0] == "ok"; assert d.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 5; print("db=ok schema=5")'
systemctl show -p ActiveState -p NRestarts -p User qq-bot.service qqbot-dashboard.service qqbot-dashboard-tunnel.service
```

核对连续三次 `ready=true`、Docker 镜像/容器身份、WebUI/OneBot 的 loopback、HTTPS 鉴权、SQLite 完整性、素材哈希和用户 QQ 客户端实际登录。若启用定时任务，首个六小时汇总与北京时间 17:15 截图须分别等待自然到点验收；健康探针不能证明 QQ 投递，也不要合成 QQ 消息充数。

### 6. 有界清理与回滚保留

用户登录后再准备独立的 SHA-256 清理计划，列出精确路径与镜像引用，保护活动服务/镜像、数据库、凭据、NapCat 登录目录/缓存根目录和保留的回滚包。只删除无用 staging、退役网站/旧 unit、pip/node-gyp/apt 下载、无引用镜像以及项目测试/构建缓存；缓存文件逐项核实过期和引用。计划一次执行，独立复核目标缺失、保护项、数据库及三次 ready；删除或写回执不完整时只做只读恢复核验，不重放。

回滚须先停止远端 Bot/NapCat，保证 QQ 单会话，再从经验证的归档恢复旧运行环境/网站，只恢复相应 unit 和路由，重验数据库与登录。回滚归档不入 Git；私有代码先测试/扫密钥，公开 GitHub 只接收独立白名单脱敏导出，示例凭据保持空值。
