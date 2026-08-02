[CmdletBinding()]
param(
    [switch]$ProbeOnly,
    [ValidateRange(0,300)][int]$StartupDelaySeconds = 30
)

$ErrorActionPreference = 'Stop'

try {
    Import-Module (Join-Path $PSScriptRoot 'QQBot.WslAutostart.psm1') -Force
    $result = Start-QQBotWslRuntimeHolder @PSBoundParameters
    $result | ConvertTo-Json -Compress
    exit [int]$result.exitCode
} catch {
    $reason = [string]$_.Exception.Data['QQBotReason']
    $exitCode = $_.Exception.Data['QQBotExitCode']
    if ([string]::IsNullOrWhiteSpace($reason)) { $reason = 'holder_failed' }
    if ($null -eq $exitCode) { $exitCode = 20 }

    [pscustomobject][ordered]@{
        ok = $false
        alreadyRunning = $false
        ready = $false
        exitCode = [int]$exitCode
        reason = $reason
    } | ConvertTo-Json -Compress
    exit [int]$exitCode
}
