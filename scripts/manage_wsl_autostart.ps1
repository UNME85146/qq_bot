[CmdletBinding()]
param(
    [ValidateSet('Plan','Status','Install','Uninstall')][string]$Mode = 'Plan',
    [switch]$Apply,
    [switch]$ReplaceExisting,
    [AllowEmptyString()][string]$InstallRoot
)

$ErrorActionPreference = 'Stop'

try {
    $utilityModule = Join-Path $PSHOME `
        'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'
    Import-Module $utilityModule -Global -ErrorAction Stop
    Import-Module (Join-Path $PSScriptRoot 'QQBot.WslAutostart.psm1') -Force
    $result = Invoke-QQBotWslAutostart @PSBoundParameters
    $result | ConvertTo-Json -Depth 8 -Compress
    if ($result.PSObject.Properties.Name -contains 'exitCode') {
        exit [int]$result.exitCode
    }
    exit 0
} catch {
    $category = [string]$_.Exception.Data['QQBotCategory']
    $reason = [string]$_.Exception.Data['QQBotReason']
    $exitCode = $_.Exception.Data['QQBotExitCode']
    if ([string]::IsNullOrWhiteSpace($category)) { $category = 'autostart_error' }
    if ([string]::IsNullOrWhiteSpace($reason)) { $reason = 'operation_failed' }
    if ($null -eq $exitCode) { $exitCode = 3 }

    [pscustomobject][ordered]@{
        ok = $false
        category = $category
        reason = $reason
    } | ConvertTo-Json -Compress
    exit [int]$exitCode
}
