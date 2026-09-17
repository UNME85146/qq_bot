param(
    [ValidateSet("plan", "apply")]
    [string]$Action = "plan",
    [string]$Plan = ".deploy/wsl-plan.json",
    [string]$PlanHash = "",
    [string]$Distro = "Ubuntu-24.04",
    [string]$Root = "/opt/qq_bot",
    [string]$Service = "qq-bot.service",
    [string]$NapCatContainer = "napcat",
    [string]$VideoCacheHost = "/opt/napcat/cache/qq-bot-media",
    [string]$VideoCacheContainer = "/app/napcat/cache/qq-bot-media",
    [string]$PersonaProfile = "config/persona_profile.local.json"
)

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}

$arguments = @(
    "-m",
    "tools.deploy_wsl",
    $Action,
    "--plan", $Plan,
    "--distro", $Distro,
    "--root", $Root,
    "--service", $Service,
    "--napcat-container", $NapCatContainer,
    "--video-cache-host", $VideoCacheHost,
    "--video-cache-container", $VideoCacheContainer,
    "--persona-profile", $PersonaProfile
)
if ($Action -eq "apply") {
    if (-not $PlanHash) {
        throw "Apply requires -PlanHash from the immediately preceding plan."
    }
    $arguments += @("--plan-hash", $PlanHash)
}

& $python @arguments
exit $LASTEXITCODE
