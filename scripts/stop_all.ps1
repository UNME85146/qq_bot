$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $PSScriptRoot
$ConfigPath = Join-Path $Root "config\config.json"
$Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
$BotQq = if ($env:QQ_BOT_SELF_ID) { $env:QQ_BOT_SELF_ID } else { [string]$Config.qq.selfId }

Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*QQ_bot*.venv*bot.py*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq "node.exe" -and ($_.CommandLine -match "NapCat\.Shell\.Windows\.Node|napcat\.mjs|$BotQq") } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Write-Host "Stopped QQ_bot and NapCat matching processes."
