# QQ Bot

一个基于 NoneBot2、OneBot V11 和 OpenAI-compatible 模型接口的 QQ 聊天机器人示例项目。它支持私聊、白名单群聊、owner/root 管理命令、长期记忆、群上下文、群静默、群聊线程化引用回复、模型失败治理、运行状态检查和数据库维护工具。

本仓库是公开脱敏版本，只包含代码、配置模板、README 和操作手册。不包含任何真实账号、API Key、Token、服务器密码、数据库、日志、二维码、QQ 登录态、设计文档或验收记录。

## 功能概览

- 私聊和白名单群聊回复。
- root / owner 权限分层。
- `/help`、`/status`、`/memory`、`/audit last`、`/ping model` 等私聊管理命令。
- `/allow private|group ...` 在线维护白名单。
- `/owner add|remove|list` root 专用 owner 管理。
- 群聊引用原消息并 @提问者的线程化回复。
- 群级回复队列，避免短时间连发。
- 群静默开关，静默期间仍可保存群消息和低敏群上下文。
- 模型失败重试、失败分类和熔断。
- 图片理解降级支持，复用 OpenAI-compatible 多模态接口。
- SQLite 存储、审计、运行状态脚本、备份和导出工具。

## 快速开始

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .[dev]
Copy-Item .env.example .env
Copy-Item config\config.example.json config\config.json
Copy-Item config\persona_profile.example.json config\persona_profile.local.json
```

填写 `.env`：

```env
QQ_BOT_MODEL_API_KEY=
QQ_BOT_ONEBOT_TOKEN=
QQ_BOT_CONFIG_PATH=config/config.json
```

把模型 API Key 和 OneBot Token 填到等号后面；不要提交 `.env`。

填写 `config/config.json` 中的：

- `qq.selfId`
- `qq.rootUserIds`
- `qq.ownerUserIds`
- `qq.allowedPrivateUserIds`
- `qq.allowedGroupIds`
- `model.baseUrl`
- `model.name`

启动：

```powershell
python bot.py
```

检查状态：

```powershell
python tools\inspect_runtime_status.py --limit 5
```

## NapCat

NapCat 需要配置 OneBot V11 反向 WebSocket：

```text
ws://YOUR_SERVER_IP:8080/onebot/v11/ws
```

如果 NapCat 和 QQ_bot 在同一台机器，可以使用：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

更多部署、登录、配置、命令和排障说明见 [操作手册.md](操作手册.md)。

## 配置和隐私

真实私有文件不要提交：

- `.env`
- `config/config.json`
- `config/persona_profile.local.json`
- `data/*.db`
- `data/backups/`
- `logs/`
- `runtime_artifacts/`
- NapCat 登录态、二维码和缓存

公开模板文件：

- `.env.example`
- `config/config.example.json`
- `config/persona_profile.example.json`

## 常用命令

Owner/root 私聊：

```text
/help
/status
/memory
/memory clear
/audit last
/reload profile
/ping model
/allow private add <qq>
/allow private remove <qq>
/allow private list
/allow group add <group_id>
/allow group remove <group_id>
/allow group list
```

Root 专用：

```text
/owner add <qq>
/owner remove <qq>
/owner list
```

## 工具

```powershell
python tools\inspect_runtime_status.py --limit 5
python tools\tail_bot_state.py --once --limit 5
python tools\backup_db.py --db data\bot.db --backup-dir data\backups
python tools\export_audits.py --db data\bot.db --output audits.jsonl
python tools\vacuum_db.py --db data\bot.db --backup-dir data\backups
```

## 安全提醒

发布、提交或推送前，请确认仓库中没有真实账号、密钥、Token、密码、服务器地址、数据库、日志、二维码或 QQ 登录态。

可以先做一次文本扫描：

```powershell
rg -n "real-password|real-token|real-server|真实服务器|真实密钥" .
```
