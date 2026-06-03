$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
& (Join-Path $Root ".venv\Scripts\python.exe") (Join-Path $Root "tools\inspect_runtime_status.py")
