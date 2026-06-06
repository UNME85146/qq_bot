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
- Direct sticker requests such as "send a sticker", "change sticker", or "send another one" use the local sticker pool before model chat; the model is not allowed to fake media actions in text.
- Text repeat only after the same low-risk short text appears consecutively in the same group.
- Optional local MOSS-TTS-Nano voice replies through an independent HTTP TTS service. When enabled, 8-12 of every 80 eligible short model replies are randomly sent as QQ voice records; successful voice replies do not also send text, while TTS or record failures fall back to text.
- Explicit read-aloud requests are supported in private chat and in group chat only when the bot is mentioned. Generic requests such as "send a voice reply" do not read the user's raw message; they generate a short model reply and then force a QQ voice record. Follow-up phrases like "change one" can continue the previous voice action in private chat.
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
/voice status
/voice on
/voice off
/voice private on|off
/voice group on|off
/voice profile list
/voice profile set <profile_id>
/voice gender male|female|neutral
/voice language <code>
```

Root-only commands:

```text
/owner add <qq>
/owner remove <qq>
/owner list
```

## Optional Voice Replies

Voice replies use a separate local MOSS-TTS-Nano adapter service. The bot process only calls the stable local HTTP API and does not install MOSS, PyTorch, ONNX Runtime, or audio dependencies into the bot virtual environment.

Default public-safe service values:

```text
Bot root: /opt/qq_bot
TTS root: /opt/moss_tts_nano
Endpoint: http://127.0.0.1:18100/tts
Service: qq-bot-tts.service
Output cache: /opt/qq_bot/data/tts/cache
```

Install and start the adapter on Linux:

```bash
cd /opt/qq_bot
bash scripts/server/install_moss_tts_service.sh
sudo systemctl start qq-bot-tts.service
curl http://127.0.0.1:18100/health
```

Set `tts.enabled=true` plus `tts.privateEnabled` or `tts.groupEnabled` in private config, or use owner/root private commands:

```text
/voice status
/voice on|off
/voice private on|off
/voice group on|off
/voice profile list
/voice profile set <profile_id>
/voice gender male|female|neutral
/voice language <code>
```

`/voice profile list` shows configured local profiles with copyable `id=<profile_id>` values and a safe MOSS built-in voice summary in the form `voice | profile_id=<profile_id> | display_name | group`. It does not expose private `promptAudioPath` values or large `prompt_audio_codes`. Public templates contain only placeholder profiles; add your own enabled profiles to private `config/config.json`, then switch with `/voice profile set <profile_id>`.

There is no `/voice age` command because the current public integration does not rely on a stable age control exposed by the MOSS-TTS-Nano service.

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
