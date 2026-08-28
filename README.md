# QQ Bot

A NoneBot2 and OneBot v11 QQ bot with permission-aware chat, structured information delivery, provider resilience, media workflows, reminders, memory, and audit tooling.

This GitHub repository is a sanitized public export. The private development tree, tests, local design and acceptance documents, credentials, QQ identifiers, runtime databases, logs, login state, and rollback packages are not mirrored here.

## Highlights

- Stable bot-owned persona derived from validated, low-sensitivity aggregate metrics. Raw chat text, source QQ identifiers, free-text profiles, and group identifiers are not prompt inputs.
- Root and owner permissions, private/group allowlists, group mute controls, per-group FIFO chat queues, reminders, memory, image understanding, stickers, and audited administration commands.
- Group reply paths do not run project-level safety classifiers. Text, group images, image generation, explicit voice, search/evaluation, repeats, stickers, and pending enqueue go directly through their feature routes; private-chat safety and low-sensitivity long-term storage controls remain enabled.
- Normal short group model calls use at most 320 completion tokens, forward the configured reasoning effort, and stop at an 18-second default hard deadline. Dormant-session relation checks use 32 tokens and a 1.2-second deadline. Image understanding uses a separate breaker and 30-second deadline, so image failures cannot open the chat breaker.
- Current group display names and explicit "do not use this phrase/name" preferences are persisted separately from historical style. Question-like pending rows expire after 30 days by default without being deleted or marked answered.
- Dedicated structured reply mode for help, market, and search results. Full information messages are attempted first and are summarized once only when OneBot explicitly rejects their length.
- Information-feature audits store the actual successfully sent bubble text, delivery status, and available OneBot message IDs in schema v4.
- A-share provider fallback with closed/open/half-open circuit recovery. A-share and US-share reports return 20 sector messages with 10 stocks per sector, including name, code, previous close, current price, and percentage change. When a sector does not contain five actual gainers and five actual losers, the report explicitly labels the top/bottom five as relative leaders/laggards.
- Automatic Douyin/Bilibili download, categorized news commands, news subscriptions, and scheduled news delivery are disabled. Historical provider and maintenance modules remain available for rollback and audit work but are not routed from group messages.
- Optional four-hour Codex Runway summaries and a daily local-rendered usage-ranking image are disabled by default and require an explicitly configured recipient and credential file.
- OpenAI-compatible speech and image endpoints are optional. The historical local TTS service is not shipped; existing deployments can manage retirement and rollback packages with the transactional tool in `tools/`.

## Requirements

- Python 3.12 or 3.13
- NoneBot2 with the OneBot v11 adapter
- A OneBot implementation such as NapCat using reverse WebSocket
- `ffmpeg` for bounded GIF/WebP image preprocessing and historical media tooling
- Optional market dependencies: AkShare and yfinance
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
- Optional market providers, search provider, speech, image, Codex Runway, and usage-ranking settings

Set secrets only in `.env` or your service manager. Do not put literal credentials in JSON templates, scripts, shell history, logs, or commits.

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
#chat 查一下 <query> [--page N]
#画图 <prompt>  #改图 <instruction>
```

Market reports send one message per sector. Search keeps each title, source, summary, and URL together and retains paging for larger result sets. If OneBot explicitly rejects a complete information message as too long, the bot performs one compact-summary retry instead of proactively truncating it.

Group image understanding skips project-level safety classification and calls the configured multimodal model directly. Triggered OneBot images first resolve a fresh `get_image` URL, accept only trusted QQ CDN HTTPS downloads up to 8 MiB, and inline JPEG/PNG; GIF/WebP are converted to a bounded PNG first frame with ffmpeg. Private images retain classification and short refusal behavior. Private classification results are cached for five minutes, concurrent requests for the same prepared image share one SHA-256 cache key, and image model calls use the configured low reasoning budget and independent vision deadline.

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
$env:QQ_BOT_CONFIG_PATH = "config/config.example.json"
.\.venv\Scripts\python -c "import bot; import nonebot; print(nonebot.get_driver().type)"
```

Passing local tests or probes does not prove live QQ delivery, provider success, video upload, or recovery behavior. Validate each enabled production path separately.

## Security

- Never commit `.env`, `config/config.json`, local persona profiles, databases, backups, logs, runtime artifacts, QR codes, NapCat cache/login state, tokens, private QQ/group IDs, or server credentials.
- Provider and video telemetry is intentionally redacted. Keep raw response bodies and secrets out of durable audit records.
- Public releases must contain only the approved export paths and must pass secret and private-identifier scans before push.
- Keep dependencies and OneBot/NapCat endpoints private by default; expose only the minimum required network surface.
