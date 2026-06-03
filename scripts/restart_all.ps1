$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "stop_all.ps1")
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot "start_all.ps1")
