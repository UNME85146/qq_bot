$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BotLog = Join-Path $Root ("logs\bot-start-all-{0}.out.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$BotErr = $BotLog -replace "\.out\.log$", ".err.log"
$ConfigPath = Join-Path $Root "config\config.json"
$Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
$BotQq = if ($env:QQ_BOT_SELF_ID) { $env:QQ_BOT_SELF_ID } else { [string]$Config.qq.selfId }
if (-not $env:NAPCAT_ROOT) {
  throw "NAPCAT_ROOT is required, for example: `$env:NAPCAT_ROOT='F:\path\to\NapCat.Shell.Windows.Node'"
}
$NapCatRoot = $env:NAPCAT_ROOT
$NapCatLog = Join-Path $Root ("logs\napcat-start-all-{0}.out.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$NapCatErr = $NapCatLog -replace "\.out\.log$", ".err.log"

Start-Process -FilePath (Join-Path $Root ".venv\Scripts\python.exe") -ArgumentList "bot.py" -WorkingDirectory $Root -RedirectStandardOutput $BotLog -RedirectStandardError $BotErr -WindowStyle Hidden
Start-Sleep -Seconds 3
Start-Process -FilePath (Join-Path $NapCatRoot "node.exe") -ArgumentList "./index.js --qq $BotQq" -WorkingDirectory $NapCatRoot -RedirectStandardOutput $NapCatLog -RedirectStandardError $NapCatErr -WindowStyle Hidden

Write-Host "QQ_bot log: $BotLog"
Write-Host "NapCat log: $NapCatLog"
