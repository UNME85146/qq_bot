# QQ Bot

A QQ chat bot built with NoneBot2, OneBot V11, SQLite, and an OpenAI-compatible chat model API.

This repository is a sanitized public export. It intentionally excludes private runtime configuration, databases, logs, QR codes, QQ login state, local design documents, acceptance records, and any real credentials or account IDs.

## Features

- Private chat and allowlisted group chat.
- Root/owner permission model with dynamic allowlist commands.
- Owner private commands including `/help`, `/status`, `/memory`, `/audit last`, `/ping model`, `/allow ...`, `/owner ...`, and `/mute ...`.
- Private-only scheduled reminders with `/remind`.
- Group threaded replies using `reply + @ + text` on the first bubble.
- Per-group outbound queue with at least 1.5 seconds between same-group reply tasks.
- Group mute switch, pending question tracking, and low-risk group context summaries.
- Model failure retry, classification, and breaker behavior.
- Shared model-context enhancement before model calls: long-term memory, group context, quoted message, semantic terms, sticker/image analysis, and reply-mode hints.
- Optional image understanding via the configured OpenAI-compatible model, with safe fallback when unsupported.
- Global sticker asset pool with content-hash deduplication, semantic analysis, matching, probability repeat behavior, and a 3500 asset cap.
- Text repeat only after the same low-risk short text appears consecutively in the same group.
- SQLite audit/runtime inspection, backup, export, and vacuum tools.

## Quick Start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
Copy-Item .env.example .env
Copy-Item config\config.example.json config\config.json
Copy-Item config\persona_profile.example.json config\persona_profile.local.json
```

Fill `.env` with private values:

```env
QQ_BOT_MODEL_API_KEY=
QQ_BOT_ONEBOT_TOKEN=
QQ_BOT_CONFIG_PATH=config/config.json
```

Edit `config/config.json` for your bot QQ, root/owner IDs, allowlists, model base URL, and model name. Do not commit private config files.

Start the bot:

```powershell
python bot.py
```

Inspect runtime state:

```powershell
python tools\inspect_runtime_status.py --limit 5
```

## NapCat

Configure NapCat OneBot V11 reverse WebSocket to point at the bot:

```text
ws://YOUR_SERVER_IP:8080/onebot/v11/ws
```

If NapCat and QQ_bot run on the same server, use:

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

## Management Commands

Owner/root private commands:

```text
/help
/status
/memory
/memory clear
/audit last
/reload profile
/ping model
/remind <natural-language reminder>
/remind list
/remind cancel <id>
/allow private add <qq>
/allow private remove <qq>
/allow private list
/allow group add <group_id>
/allow group remove <group_id>
/allow group list
/mute status
/mute clear
/mute clear <group_id>
```

Root-only commands:

```text
/owner add <qq>
/owner remove <qq>
/owner list
```

## Tools

```powershell
python tools\inspect_runtime_status.py --limit 5
python tools\tail_bot_state.py --once --limit 5
python tools\backup_db.py --db data\bot.db --backup-dir data\backups
python tools\export_audits.py --db data\bot.db --output audits.jsonl
python tools\vacuum_db.py --db data\bot.db --backup-dir data\backups
```

## Security

Never commit:

- `.env`
- `config/config.json`
- `config/persona_profile.local.json`
- SQLite databases and backups
- logs
- NapCat login state, QR codes, screenshots, or cache
- API keys, OneBot tokens, NapCat WebUI tokens, server passwords, private QQ IDs, or private group IDs

Use placeholders in public docs and templates.
