param(
    [string]$HostName = "your-server-ip",
    [string]$User = "your-server-user",
    [int]$Port = 22,
    [string]$Root = "/opt/qq_bot",
    [string]$ConnectorContainer = "llbot",
    [string]$PasswordEnv = "",
    [string]$SudoPasswordEnv = "",
    [switch]$SkipTests,
    [switch]$RestartConnector,
    [switch]$AllowDirty,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$argsList = @(
    "tools/deploy_server.py",
    "--host", $HostName,
    "--user", $User,
    "--port", "$Port",
    "--root", $Root,
    "--connector-container", $ConnectorContainer
)

if ($PasswordEnv) {
    $argsList += @("--password-env", $PasswordEnv)
}
if ($SudoPasswordEnv) {
    $argsList += @("--sudo-password-env", $SudoPasswordEnv)
}
if ($SkipTests) {
    $argsList += "--skip-tests"
}
if ($RestartConnector) {
    $argsList += "--restart-connector"
}
if ($AllowDirty) {
    $argsList += "--allow-dirty"
}
if ($DryRun) {
    $argsList += "--dry-run"
}

& $python @argsList
exit $LASTEXITCODE
