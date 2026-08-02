Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:QQBotWslAutostartModuleRoot = $PSScriptRoot

function New-QQBotWslAutostartError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][string]$Category,
        [Parameter(Mandatory)][string]$Reason,
        [Parameter(Mandatory)][int]$ExitCode
    )

    $exception = New-Object System.InvalidOperationException $Message
    $exception.Data['QQBotCategory'] = $Category
    $exception.Data['QQBotReason'] = $Reason
    $exception.Data['QQBotExitCode'] = $ExitCode
    return $exception
}

function Get-QQBotWslAutostartSpec {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$InstallRoot)

    if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        $InstallRoot = Join-Path $env:LOCALAPPDATA 'QQBot\wsl-autostart'
    }

    [pscustomobject][ordered]@{
        SchemaVersion = 1
        TaskName = 'QQBot-WSL-Autostart'
        Distro = 'Ubuntu-24.04'
        RuntimeRoot = '/opt/qq_bot'
        InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
        LauncherName = 'start_wsl_runtime_holder.ps1'
        ModuleName = 'QQBot.WslAutostart.psm1'
        DelaySeconds = 30
        ReadyPollSeconds = 5
        ReadyTimeoutSeconds = 300
        RestartCount = 3
        RestartIntervalSeconds = 60
        LogRetention = 10
    }
}

function Test-QQBotPathEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd('\'),
        [IO.Path]::GetFullPath($Right).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Get-QQBotScheduledTaskActionArguments {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $launcherPath = Join-Path $Spec.InstallRoot $Spec.LauncherName
    return "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File `"$launcherPath`""
}

function Get-QQBotCurrentIdentity {
    [CmdletBinding()]
    param()

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($identity.IsSystem) {
        throw (New-QQBotWslAutostartError `
            -Message 'system_identity_not_allowed' `
            -Category 'preflight_failure' `
            -Reason 'system_identity_not_allowed' `
            -ExitCode 3)
    }
    return $identity
}

function Test-QQBotIdentityReferenceMatches {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Reference,
        [Parameter(Mandatory)]$Identity
    )

    if ([string]::IsNullOrWhiteSpace($Reference)) { return $false }
    if ([string]::Equals(
            $Reference,
            [string]$Identity.Name,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        return $true
    }

    $identitySid = [string]$Identity.User.Value
    if ([string]::IsNullOrWhiteSpace($identitySid)) { return $false }
    if ([string]::Equals(
            $Reference,
            $identitySid,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        return $true
    }

    try {
        $account = New-Object Security.Principal.NTAccount -ArgumentList $Reference
        $resolvedSid = $account.Translate([Security.Principal.SecurityIdentifier])
        return [string]::Equals(
            [string]$resolvedSid.Value,
            $identitySid,
            [StringComparison]::OrdinalIgnoreCase
        )
    } catch {
        return $false
    }
}

function New-QQBotScheduledTaskDefinition {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $identity = Get-QQBotCurrentIdentity
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal `
        -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -Hidden `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount $Spec.RestartCount `
        -RestartInterval (New-TimeSpan -Seconds $Spec.RestartIntervalSeconds)
    $powershellPath = Join-Path $env:SystemRoot `
        'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction `
        -Execute $powershellPath `
        -Argument (Get-QQBotScheduledTaskActionArguments -Spec $Spec) `
        -WorkingDirectory $Spec.InstallRoot

    [pscustomobject][ordered]@{
        Trigger = $trigger
        Principal = $principal
        Settings = $settings
        Action = $action
    }
}

function Test-QQBotScheduledTaskObjectMatches {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Task,
        [Parameter(Mandatory)]$Spec
    )

    $identity = Get-QQBotCurrentIdentity
    $triggers = @($Task.Triggers)
    $actions = @($Task.Actions)
    if ($triggers.Count -ne 1 -or $actions.Count -ne 1) { return $false }
    $trigger = $triggers[0]
    $action = $actions[0]
    $expectedPowerShell = Join-Path $env:SystemRoot `
        'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedLauncher = Join-Path $Spec.InstallRoot $Spec.LauncherName

    if ([string]$trigger.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger') { return $false }
    if (-not $trigger.Enabled) { return $false }
    if (-not (Test-QQBotIdentityReferenceMatches `
            -Reference ([string]$trigger.UserId) -Identity $identity)) { return $false }
    if (-not (Test-QQBotIdentityReferenceMatches `
            -Reference ([string]$Task.Principal.UserId) -Identity $identity)) { return $false }
    if ([string]$Task.Principal.LogonType -ne 'Interactive') { return $false }
    if ([string]$Task.Principal.RunLevel -ne 'Limited') { return $false }
    if (-not (Test-QQBotPathEqual -Left ([string]$action.Execute) -Right $expectedPowerShell)) { return $false }
    if ([string]$action.Arguments -ne (Get-QQBotScheduledTaskActionArguments -Spec $Spec)) { return $false }
    if (-not (Test-QQBotPathEqual -Left ([string]$action.WorkingDirectory) -Right $Spec.InstallRoot)) { return $false }
    if (-not $Task.Settings.Hidden) { return $false }
    if (-not $Task.Settings.StartWhenAvailable) { return $false }
    if ($Task.Settings.DisallowStartIfOnBatteries) { return $false }
    if ($Task.Settings.StopIfGoingOnBatteries) { return $false }
    if ([string]$Task.Settings.ExecutionTimeLimit -ne 'PT0S') { return $false }
    if ([string]$Task.Settings.MultipleInstances -ne 'IgnoreNew') { return $false }
    if ([int]$Task.Settings.RestartCount -ne [int]$Spec.RestartCount) { return $false }
    if ([string]$Task.Settings.RestartInterval -ne 'PT1M') { return $false }
    if (-not (Test-QQBotPathEqual -Left $expectedLauncher -Right (Join-Path $Spec.InstallRoot $Spec.LauncherName))) { return $false }
    return $true
}

function Get-QQBotScheduledTaskState {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    try {
        $task = Get-ScheduledTask -TaskName $Spec.TaskName -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'CmdletizationQuery_NotFound_TaskName*') {
            throw (New-QQBotWslAutostartError `
                -Message 'task_query_failed' `
                -Category 'preflight_failure' `
                -Reason 'task_query_failed' `
                -ExitCode 3)
        }
        $task = $null
    }
    if ($null -eq $task) {
        return [pscustomobject][ordered]@{
            present = $false
            matches = $false
            state = 'absent'
            lastResultCategory = 'never_run'
        }
    }

    $state = switch ([string]$task.State) {
        'Running' { 'running' }
        'Ready' { 'ready' }
        'Disabled' { 'disabled' }
        default { 'other' }
    }
    $lastResultCategory = 'unknown'
    $taskInfo = Get-ScheduledTaskInfo -TaskName $Spec.TaskName -ErrorAction SilentlyContinue
    if ($null -ne $taskInfo) {
        $lastResultCategory = if ([int64]$taskInfo.LastTaskResult -eq 0) {
            'success'
        } elseif ([int64]$taskInfo.LastTaskResult -eq 267009) {
            'running'
        } elseif ([int64]$taskInfo.LastTaskResult -eq 267011) {
            'never_run'
        } else {
            'failure'
        }
    }

    [pscustomobject][ordered]@{
        present = $true
        matches = [bool](Test-QQBotScheduledTaskObjectMatches -Task $task -Spec $Spec)
        state = $state
        lastResultCategory = $lastResultCategory
    }
}

function Get-QQBotAutostartFileState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)][ValidateSet('Launcher','Module')][string]$Kind
    )

    $fileName = if ($Kind -eq 'Launcher') { $Spec.LauncherName } else { $Spec.ModuleName }
    $installedPath = Join-Path $Spec.InstallRoot $fileName
    $sourcePath = Join-Path $script:QQBotWslAutostartModuleRoot $fileName
    $present = Test-Path -LiteralPath $installedPath -PathType Any
    $sourcePresent = Test-Path -LiteralPath $sourcePath -PathType Any
    $safeRegular = $false
    $sourceSafeRegular = $false
    if ($present) {
        try {
            Assert-QQBotOwnedRegularPath -Path $installedPath
            $safeRegular = $true
        } catch {
            $safeRegular = $false
        }
    }
    if ($sourcePresent) {
        try {
            Assert-QQBotOwnedRegularPath -Path $sourcePath
            $sourceSafeRegular = $true
        } catch {
            $sourceSafeRegular = $false
        }
    }
    $hashMatches = $false
    if ($safeRegular -and $sourceSafeRegular) {
        $installedHash = (Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash
        $sourceHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash
        $hashMatches = $installedHash -eq $sourceHash
    }

    [pscustomobject][ordered]@{
        present = [bool]$present
        safeRegular = [bool]$safeRegular
        hashMatches = [bool]$hashMatches
    }
}

function Assert-QQBotOwnedRegularPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$AllowMissing
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Any)) {
        if ($AllowMissing) { return }
        throw (New-QQBotWslAutostartError `
            -Message 'owned_path_missing' `
            -Category 'preflight_failure' `
            -Reason 'owned_path_missing' `
            -ExitCode 3)
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw (New-QQBotWslAutostartError `
            -Message 'owned_path_reparse_point' `
            -Category 'preflight_failure' `
            -Reason 'owned_path_reparse_point' `
            -ExitCode 3)
    }
    if ($item.PSIsContainer) {
        throw (New-QQBotWslAutostartError `
            -Message 'owned_path_not_regular_file' `
            -Category 'preflight_failure' `
            -Reason 'owned_path_not_regular_file' `
            -ExitCode 3)
    }
}

function Assert-QQBotSafeDirectoryChain {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-Path -LiteralPath $current -PathType Any) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw (New-QQBotWslAutostartError `
                    -Message 'owned_parent_reparse_point' `
                    -Category 'preflight_failure' `
                    -Reason 'owned_parent_reparse_point' `
                    -ExitCode 3)
            }
            if (-not $item.PSIsContainer) {
                throw (New-QQBotWslAutostartError `
                    -Message 'owned_parent_not_directory' `
                    -Category 'preflight_failure' `
                    -Reason 'owned_parent_not_directory' `
                    -ExitCode 3)
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current.TrimEnd('\'))
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Copy-QQBotFileAtomically {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination
    )

    Assert-QQBotOwnedRegularPath -Path $Source
    Assert-QQBotOwnedRegularPath -Path $Destination -AllowMissing
    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Destination))
    Assert-QQBotSafeDirectoryChain -Path $directory
    $temporary = Join-Path $directory ('.qqbot-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary
        Assert-QQBotOwnedRegularPath -Path $temporary
        $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
        $temporaryHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash
        if ($sourceHash -ne $temporaryHash) { throw 'atomic_copy_hash_mismatch' }
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
        $installedHash = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
        if ($installedHash -ne $sourceHash) { throw 'atomic_install_hash_mismatch' }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Write-QQBotTextAtomically {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Content
    )

    $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($Path))
    Assert-QQBotSafeDirectoryChain -Path $directory
    Assert-QQBotOwnedRegularPath -Path $Path -AllowMissing
    $temporary = Join-Path $directory ('.qqbot-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $utf8 = New-Object Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllText($temporary, $Content, $utf8)
        Assert-QQBotOwnedRegularPath -Path $temporary
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Test-QQBotScheduledTaskMatches {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $state = Get-QQBotScheduledTaskState -Spec $Spec
    if (-not $state.present -or -not $state.matches) { return $false }
    $launcher = Get-QQBotAutostartFileState -Spec $Spec -Kind Launcher
    $module = Get-QQBotAutostartFileState -Spec $Spec -Kind Module
    return [bool]($launcher.present -and $launcher.hashMatches -and
        $module.present -and $module.hashMatches)
}

function Register-QQBotScheduledTask {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $definition = New-QQBotScheduledTaskDefinition -Spec $Spec
    Register-ScheduledTask `
        -TaskName $Spec.TaskName `
        -TaskPath '\' `
        -Action $definition.Action `
        -Trigger $definition.Trigger `
        -Principal $definition.Principal `
        -Settings $definition.Settings `
        -Description 'QQ Bot WSL logon runtime holder' `
        -Force | Out-Null
}

function Export-QQBotScheduledTaskXml {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    return Export-ScheduledTask -TaskName $Spec.TaskName -TaskPath '\' -ErrorAction Stop
}

function Unregister-QQBotScheduledTask {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    try {
        Unregister-ScheduledTask `
            -TaskName $Spec.TaskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
    } catch {
        if ($_.FullyQualifiedErrorId -notlike 'CmdletizationQuery_NotFound_TaskName*') {
            throw
        }
    }
}

function Restore-QQBotScheduledTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)][string]$Xml
    )

    Register-ScheduledTask `
        -TaskName $Spec.TaskName -TaskPath '\' -Xml $Xml -Force | Out-Null
}

function Test-QQBotWslDistroAvailable {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $wslPath = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) { return $false }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $distros = @(& $wslPath -l -q 2>&1 | ForEach-Object { ([string]$_).Trim() })
        $exitCode = [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return [bool]($exitCode -eq 0 -and $distros -contains $Spec.Distro)
}

function Test-QQBotWslRuntimeRoot {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $wslPath = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) { return $false }
    $command = "test -d '$($Spec.RuntimeRoot)' && " +
        "test -x '$($Spec.RuntimeRoot)/.venv/bin/python' && " +
        "test -f '$($Spec.RuntimeRoot)/tools/inspect_runtime_status.py'"
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $wslPath -d $Spec.Distro -- bash -lc $command 2>&1 | Out-Null
        $exitCode = [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode -eq 0
}

function Assert-QQBotWslAutostartMutationPreflight {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    if ($null -eq (Get-Module ScheduledTasks -ListAvailable | Select-Object -First 1)) {
        throw (New-QQBotWslAutostartError `
            -Message 'scheduled_tasks_unavailable' `
            -Category 'preflight_failure' `
            -Reason 'scheduled_tasks_unavailable' `
            -ExitCode 3)
    }
    [void](Get-QQBotCurrentIdentity)
    $defaultRoot = [IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA 'QQBot\wsl-autostart')
    )
    if (-not (Test-QQBotPathEqual -Left $Spec.InstallRoot -Right $defaultRoot)) {
        throw (New-QQBotWslAutostartError `
            -Message 'nondefault_install_root' `
            -Category 'preflight_failure' `
            -Reason 'nondefault_install_root' `
            -ExitCode 3)
    }

    $sourceLauncher = Join-Path $script:QQBotWslAutostartModuleRoot $Spec.LauncherName
    $sourceModule = Join-Path $script:QQBotWslAutostartModuleRoot $Spec.ModuleName
    Assert-QQBotOwnedRegularPath -Path $sourceLauncher
    Assert-QQBotOwnedRegularPath -Path $sourceModule
    Assert-QQBotSafeDirectoryChain -Path $Spec.InstallRoot
    foreach ($name in @($Spec.LauncherName, $Spec.ModuleName, 'install-manifest.json')) {
        $installedPath = Join-Path $Spec.InstallRoot $name
        if (Test-Path -LiteralPath $installedPath -PathType Any) {
            Assert-QQBotOwnedRegularPath -Path $installedPath
        }
    }

    if (-not (Test-QQBotWslDistroAvailable -Spec $Spec)) {
        throw (New-QQBotWslAutostartError `
            -Message 'wsl_target_unavailable' `
            -Category 'preflight_failure' `
            -Reason 'wsl_target_unavailable' `
            -ExitCode 3)
    }
    if (-not (Test-QQBotWslRuntimeRoot -Spec $Spec)) {
        throw (New-QQBotWslAutostartError `
            -Message 'wsl_runtime_root_unavailable' `
            -Category 'preflight_failure' `
            -Reason 'wsl_runtime_root_unavailable' `
            -ExitCode 3)
    }
}

function Get-QQBotTextSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Text)

    $utf8 = New-Object Text.UTF8Encoding($false)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($utf8.GetBytes($Text)))).Replace('-', '')
    } finally {
        $sha.Dispose()
    }
}

function New-QQBotAutostartBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)]$TaskState
    )

    $taskXml = $null
    $taskXmlSha256 = $null
    if ($TaskState.present) {
        $taskXml = [string](Export-QQBotScheduledTaskXml -Spec $Spec)
        $taskXmlSha256 = Get-QQBotTextSha256 -Text $taskXml
    }

    $launcherPath = Join-Path $Spec.InstallRoot $Spec.LauncherName
    $modulePath = Join-Path $Spec.InstallRoot $Spec.ModuleName
    $manifestPath = Join-Path $Spec.InstallRoot 'install-manifest.json'
    $launcherPresent = Test-Path -LiteralPath $launcherPath -PathType Any
    $modulePresent = Test-Path -LiteralPath $modulePath -PathType Any
    $manifestPresent = Test-Path -LiteralPath $manifestPath -PathType Any
    if ($launcherPresent) { Assert-QQBotOwnedRegularPath -Path $launcherPath }
    if ($modulePresent) { Assert-QQBotOwnedRegularPath -Path $modulePath }
    if ($manifestPresent) { Assert-QQBotOwnedRegularPath -Path $manifestPath }
    $launcherSha256 = if ($launcherPresent) {
        (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash
    } else { $null }
    $moduleSha256 = if ($modulePresent) {
        (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256).Hash
    } else { $null }
    $manifestSha256 = if ($manifestPresent) {
        (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
    } else { $null }

    Assert-QQBotSafeDirectoryChain -Path $Spec.InstallRoot
    [void](New-Item -ItemType Directory -Path $Spec.InstallRoot -Force)
    $backupRoot = Join-Path $Spec.InstallRoot 'backups'
    [void](New-Item -ItemType Directory -Path $backupRoot -Force)
    $backupName = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' +
        [Guid]::NewGuid().ToString('N')
    $backupDirectory = Join-Path $backupRoot $backupName
    [void](New-Item -ItemType Directory -Path $backupDirectory)
    Assert-QQBotSafeDirectoryChain -Path $backupDirectory

    if ($launcherPresent) {
        Copy-QQBotFileAtomically `
            -Source $launcherPath -Destination (Join-Path $backupDirectory $Spec.LauncherName)
    }
    if ($modulePresent) {
        Copy-QQBotFileAtomically `
            -Source $modulePath -Destination (Join-Path $backupDirectory $Spec.ModuleName)
    }
    if ($manifestPresent) {
        Copy-QQBotFileAtomically `
            -Source $manifestPath -Destination (Join-Path $backupDirectory 'install-manifest.json')
    }
    if ($TaskState.present) {
        Write-QQBotTextAtomically `
            -Path (Join-Path $backupDirectory 'task.xml') -Content $taskXml
    }

    $backupManifest = [pscustomobject][ordered]@{
        schemaVersion = 1
        createdAt = [DateTime]::UtcNow.ToString('o')
        taskPresent = [bool]$TaskState.present
        taskXmlSha256 = $taskXmlSha256
        launcherPresent = [bool]$launcherPresent
        launcherSha256 = $launcherSha256
        modulePresent = [bool]$modulePresent
        moduleSha256 = $moduleSha256
        installManifestPresent = [bool]$manifestPresent
        installManifestSha256 = $manifestSha256
    }
    Write-QQBotTextAtomically `
        -Path (Join-Path $backupDirectory 'backup-manifest.json') `
        -Content ($backupManifest | ConvertTo-Json -Depth 5 -Compress)

    [pscustomobject][ordered]@{
        Directory = $backupDirectory
        TaskPresent = [bool]$TaskState.present
        TaskXml = $taskXml
        TaskXmlSha256 = $taskXmlSha256
        LauncherPresent = [bool]$launcherPresent
        LauncherSha256 = $launcherSha256
        ModulePresent = [bool]$modulePresent
        ModuleSha256 = $moduleSha256
        InstallManifestPresent = [bool]$manifestPresent
        InstallManifestSha256 = $manifestSha256
    }
}

function Restore-QQBotAutostartBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)]$Backup
    )

    $errors = @()
    try {
        Unregister-QQBotScheduledTask -Spec $Spec
        if ($Backup.TaskPresent) {
            Restore-QQBotScheduledTask -Spec $Spec -Xml $Backup.TaskXml
        }
    } catch {
        $errors += 'task_restore_failed'
    }

    $filePairs = @(
        [pscustomobject]@{
            Present = $Backup.LauncherPresent
            Name = $Spec.LauncherName
            Hash = $Backup.LauncherSha256
        },
        [pscustomobject]@{
            Present = $Backup.ModulePresent
            Name = $Spec.ModuleName
            Hash = $Backup.ModuleSha256
        },
        [pscustomobject]@{
            Present = $Backup.InstallManifestPresent
            Name = 'install-manifest.json'
            Hash = $Backup.InstallManifestSha256
        }
    )
    foreach ($pair in $filePairs) {
        $destination = Join-Path $Spec.InstallRoot $pair.Name
        try {
            if ($pair.Present) {
                Copy-QQBotFileAtomically `
                    -Source (Join-Path $Backup.Directory $pair.Name) `
                    -Destination $destination
            } elseif (Test-Path -LiteralPath $destination -PathType Any) {
                Assert-QQBotOwnedRegularPath -Path $destination
                Remove-Item -LiteralPath $destination -Force
            }
        } catch {
            $errors += ('file_restore_failed_' + $pair.Name)
        }
    }

    try {
        if ($Backup.TaskPresent) {
            $restoredXml = [string](Export-QQBotScheduledTaskXml -Spec $Spec)
            if ((Get-QQBotTextSha256 -Text $restoredXml) -ne $Backup.TaskXmlSha256) {
                $errors += 'task_restore_hash_mismatch'
            }
        } else {
            $restoredState = Get-QQBotScheduledTaskState -Spec $Spec
            if ($restoredState.present) { $errors += 'task_restore_presence_mismatch' }
        }
    } catch {
        $errors += 'task_restore_verification_failed'
    }
    foreach ($pair in $filePairs) {
        $destination = Join-Path $Spec.InstallRoot $pair.Name
        if ($pair.Present) {
            if (-not (Test-Path -LiteralPath $destination -PathType Leaf)) {
                $errors += ('file_restore_missing_' + $pair.Name)
            } elseif ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $pair.Hash) {
                $errors += ('file_restore_hash_mismatch_' + $pair.Name)
            }
        } elseif (Test-Path -LiteralPath $destination -PathType Any) {
            $errors += ('file_restore_presence_mismatch_' + $pair.Name)
        }
    }

    return [pscustomobject][ordered]@{
        success = $errors.Count -eq 0
        errorCount = $errors.Count
    }
}

function Write-QQBotInstallManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $launcherPath = Join-Path $Spec.InstallRoot $Spec.LauncherName
    $modulePath = Join-Path $Spec.InstallRoot $Spec.ModuleName
    $manifest = [pscustomobject][ordered]@{
        schemaVersion = 1
        installedAt = [DateTime]::UtcNow.ToString('o')
        launcherSha256 = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash
        moduleSha256 = (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256).Hash
    }
    Write-QQBotTextAtomically `
        -Path (Join-Path $Spec.InstallRoot 'install-manifest.json') `
        -Content ($manifest | ConvertTo-Json -Compress)
}

function Get-QQBotInstallManifestState {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $path = Join-Path $Spec.InstallRoot 'install-manifest.json'
    if (-not (Test-Path -LiteralPath $path -PathType Any)) {
        return [pscustomobject][ordered]@{
            present = $false
            valid = $false
            filesOwned = $false
        }
    }
    try {
        Assert-QQBotOwnedRegularPath -Path $path
        $manifest = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        $valid = [int]$manifest.schemaVersion -eq 1 -and
            [string]$manifest.launcherSha256 -match '^[A-Fa-f0-9]{64}$' -and
            [string]$manifest.moduleSha256 -match '^[A-Fa-f0-9]{64}$'
        $launcherPath = Join-Path $Spec.InstallRoot $Spec.LauncherName
        $modulePath = Join-Path $Spec.InstallRoot $Spec.ModuleName
        $filesOwned = $valid -and
            (Test-Path -LiteralPath $launcherPath -PathType Leaf) -and
            (Test-Path -LiteralPath $modulePath -PathType Leaf) -and
            (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash -eq
                [string]$manifest.launcherSha256 -and
            (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256).Hash -eq
                [string]$manifest.moduleSha256
        return [pscustomobject][ordered]@{
            present = $true
            valid = [bool]$valid
            filesOwned = [bool]$filesOwned
            launcherSha256 = [string]$manifest.launcherSha256
            moduleSha256 = [string]$manifest.moduleSha256
        }
    } catch {
        return [pscustomobject][ordered]@{
            present = $true
            valid = $false
            filesOwned = $false
        }
    }
}

function Remove-QQBotOwnedInstalledFiles {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)]$ManifestState
    )

    if (-not $ManifestState.valid -or -not $ManifestState.filesOwned) {
        return $false
    }
    foreach ($name in @($Spec.LauncherName, $Spec.ModuleName, 'install-manifest.json')) {
        $path = Join-Path $Spec.InstallRoot $name
        if (Test-Path -LiteralPath $path -PathType Any) {
            Assert-QQBotOwnedRegularPath -Path $path
            Remove-Item -LiteralPath $path -Force
        }
    }
    return $true
}

function Install-QQBotWslAutostart {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [switch]$ReplaceExisting
    )

    Assert-QQBotWslAutostartMutationPreflight -Spec $Spec
    $taskState = Get-QQBotScheduledTaskState -Spec $Spec
    $launcherState = Get-QQBotAutostartFileState -Spec $Spec -Kind Launcher
    $moduleState = Get-QQBotAutostartFileState -Spec $Spec -Kind Module
    $manifestState = Get-QQBotInstallManifestState -Spec $Spec
    $allAbsent = -not $taskState.present -and
        -not $launcherState.present -and
        -not $moduleState.present -and
        -not $manifestState.present
    $allCurrent = $taskState.present -and $taskState.matches -and
        $launcherState.present -and $launcherState.hashMatches -and
        $moduleState.present -and $moduleState.hashMatches -and
        $manifestState.valid -and $manifestState.filesOwned
    if ($allCurrent) {
        return [pscustomobject][ordered]@{
            operation = 'install'
            ok = $true
            alreadyInstalled = $true
            mutated = $false
            taskMatches = $true
            exitCode = 0
        }
    }
    if (-not $allAbsent -and -not $ReplaceExisting) {
        if ($taskState.present -and -not $taskState.matches) {
            throw (New-QQBotWslAutostartError `
                -Message 'task_drift_requires_replace' `
                -Category 'drift_refusal' `
                -Reason 'task_drift_requires_replace' `
                -ExitCode 3)
        }
        throw (New-QQBotWslAutostartError `
            -Message 'installed_files_drift_requires_replace' `
            -Category 'drift_refusal' `
            -Reason 'installed_files_drift_requires_replace' `
            -ExitCode 3)
    }

    $backup = New-QQBotAutostartBackup -Spec $Spec -TaskState $taskState
    try {
        $sourceLauncher = Join-Path $script:QQBotWslAutostartModuleRoot $Spec.LauncherName
        $sourceModule = Join-Path $script:QQBotWslAutostartModuleRoot $Spec.ModuleName
        Copy-QQBotFileAtomically `
            -Source $sourceLauncher `
            -Destination (Join-Path $Spec.InstallRoot $Spec.LauncherName)
        Copy-QQBotFileAtomically `
            -Source $sourceModule `
            -Destination (Join-Path $Spec.InstallRoot $Spec.ModuleName)
        Write-QQBotInstallManifest -Spec $Spec
        Register-QQBotScheduledTask -Spec $Spec
        if (-not (Test-QQBotScheduledTaskMatches -Spec $Spec)) {
            throw 'post_register_verification_failed'
        }
        return [pscustomobject][ordered]@{
            operation = 'install'
            ok = $true
            alreadyInstalled = $false
            mutated = $true
            taskMatches = $true
            backupCreated = $true
            exitCode = 0
        }
    } catch {
        $rollback = Restore-QQBotAutostartBackup -Spec $Spec -Backup $backup
        if ($rollback.success) {
            return [pscustomobject][ordered]@{
                operation = 'install'
                ok = $false
                mutated = $true
                category = 'mutation_failed'
                reason = 'mutation_failed_rollback_completed'
                rollbackCompleted = $true
                exitCode = 4
            }
        }
        throw (New-QQBotWslAutostartError `
            -Message 'rollback_incomplete' `
            -Category 'rollback_incomplete' `
            -Reason 'rollback_incomplete' `
            -ExitCode 5)
    }
}

function Uninstall-QQBotWslAutostart {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [switch]$ReplaceExisting
    )

    Assert-QQBotWslAutostartMutationPreflight -Spec $Spec
    $taskState = Get-QQBotScheduledTaskState -Spec $Spec
    $launcherState = Get-QQBotAutostartFileState -Spec $Spec -Kind Launcher
    $moduleState = Get-QQBotAutostartFileState -Spec $Spec -Kind Module
    $manifestState = Get-QQBotInstallManifestState -Spec $Spec
    $allAbsent = -not $taskState.present -and
        -not $launcherState.present -and
        -not $moduleState.present -and
        -not $manifestState.present
    if ($allAbsent) {
        return [pscustomobject][ordered]@{
            operation = 'uninstall'
            ok = $true
            alreadyAbsent = $true
            mutated = $false
            exitCode = 0
        }
    }

    $allOwned = $taskState.present -and $taskState.matches -and
        $launcherState.present -and $moduleState.present -and
        $manifestState.valid -and $manifestState.filesOwned
    if (-not $allOwned -and -not $ReplaceExisting) {
        if ($taskState.present -and -not $taskState.matches) {
            throw (New-QQBotWslAutostartError `
                -Message 'task_drift_requires_replace' `
                -Category 'drift_refusal' `
                -Reason 'task_drift_requires_replace' `
                -ExitCode 3)
        }
        throw (New-QQBotWslAutostartError `
            -Message 'installed_files_drift_requires_replace' `
            -Category 'drift_refusal' `
            -Reason 'installed_files_drift_requires_replace' `
            -ExitCode 3)
    }

    $backup = New-QQBotAutostartBackup -Spec $Spec -TaskState $taskState
    try {
        if ($taskState.present) {
            Unregister-QQBotScheduledTask -Spec $Spec
        }
        $afterState = Get-QQBotScheduledTaskState -Spec $Spec
        if ($afterState.present) { throw 'post_unregister_verification_failed' }
        $filesRemoved = Remove-QQBotOwnedInstalledFiles `
            -Spec $Spec -ManifestState $manifestState
        return [pscustomobject][ordered]@{
            operation = 'uninstall'
            ok = $true
            alreadyAbsent = $false
            mutated = $true
            taskPresent = $false
            filesPreserved = -not $filesRemoved
            backupCreated = $true
            exitCode = 0
        }
    } catch {
        $rollback = Restore-QQBotAutostartBackup -Spec $Spec -Backup $backup
        if ($rollback.success) {
            return [pscustomobject][ordered]@{
                operation = 'uninstall'
                ok = $false
                mutated = $true
                category = 'mutation_failed'
                reason = 'mutation_failed_rollback_completed'
                rollbackCompleted = $true
                exitCode = 4
            }
        }
        throw (New-QQBotWslAutostartError `
            -Message 'rollback_incomplete' `
            -Category 'rollback_incomplete' `
            -Reason 'rollback_incomplete' `
            -ExitCode 5)
    }
}

function Get-QQBotWslAutostartPlan {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $task = Get-QQBotScheduledTaskState -Spec $Spec
    $launcher = Get-QQBotAutostartFileState -Spec $Spec -Kind Launcher
    $module = Get-QQBotAutostartFileState -Spec $Spec -Kind Module
    $refusalReasons = @()
    if ($task.present -and -not $task.matches) { $refusalReasons += 'task_drift' }
    if ($launcher.present -and -not $launcher.hashMatches) { $refusalReasons += 'launcher_drift' }
    if ($module.present -and -not $module.hashMatches) { $refusalReasons += 'module_drift' }

    [pscustomobject][ordered]@{
        operation = 'plan'
        ok = $true
        taskPresent = [bool]$task.present
        taskMatches = [bool]$task.matches
        launcherPresent = [bool]$launcher.present
        launcherHashMatches = [bool]$launcher.hashMatches
        modulePresent = [bool]$module.present
        moduleHashMatches = [bool]$module.hashMatches
        willMutate = $false
        refusalReasons = $refusalReasons
    }
}

function Get-QQBotWslAutostartStatus {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $task = Get-QQBotScheduledTaskState -Spec $Spec
    $launcher = Get-QQBotAutostartFileState -Spec $Spec -Kind Launcher
    $module = Get-QQBotAutostartFileState -Spec $Spec -Kind Module

    [pscustomobject][ordered]@{
        operation = 'status'
        ok = $true
        taskPresent = [bool]$task.present
        taskMatches = [bool]$task.matches
        taskState = [string]$task.state
        lastResultCategory = [string]$task.lastResultCategory
        launcherPresent = [bool]$launcher.present
        launcherHashMatches = [bool]$launcher.hashMatches
        modulePresent = [bool]$module.present
        moduleHashMatches = [bool]$module.hashMatches
        willMutate = $false
    }
}

function Get-QQBotWslHolderLinuxScript {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $attempts = [int]($Spec.ReadyTimeoutSeconds / $Spec.ReadyPollSeconds)
    $template = @'
set -euo pipefail
root='__RUNTIME_ROOT__'
attempts=__ATTEMPTS__
if ! cd -- "$root"; then
  printf 'ready=false reason=runtime_root_unavailable\n' >&2
  exit 20
fi
for attempt in $(seq 1 "$attempts"); do
  if test -x "$root/.venv/bin/python" \
     && test -f "$root/tools/inspect_runtime_status.py" \
     && $root/.venv/bin/python $root/tools/inspect_runtime_status.py --summary --require-ready --limit 5 >/dev/null 2>&1; then
    printf 'ready=true\n'
    if test "${QQBOT_PROBE_ONLY:-0}" = 1; then exit 0; fi
    exec sleep infinity
  fi
  sleep __POLL_SECONDS__
done
printf 'ready=false reason=timeout\n' >&2
exit 20
'@

    return $template.Replace('__RUNTIME_ROOT__', [string]$Spec.RuntimeRoot).
        Replace('__ATTEMPTS__', [string]$attempts).
        Replace('__POLL_SECONDS__', [string]$Spec.ReadyPollSeconds)
}

function Enter-QQBotWslHolderMutex {
    [CmdletBinding()]
    param()

    $mutex = New-Object System.Threading.Mutex($false, 'Local\QQBot-WSL-Autostart-Holder')
    try {
        $acquired = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $acquired = $true
    }

    [pscustomobject][ordered]@{
        acquired = [bool]$acquired
        mutex = $mutex
    }
}

function Exit-QQBotWslHolderMutex {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Handle)

    if ($null -eq $Handle.mutex) { return }
    try {
        if ($Handle.acquired) {
            $Handle.mutex.ReleaseMutex()
        }
    } finally {
        $Handle.mutex.Dispose()
    }
}

function New-QQBotWslHolderLog {
    [CmdletBinding()]
    param([Parameter(Mandatory)]$Spec)

    $logRoot = Join-Path $Spec.InstallRoot 'logs'
    [void](New-Item -ItemType Directory -Path $logRoot -Force)
    $existing = @(Get-ChildItem -LiteralPath $logRoot -Filter 'holder-*.jsonl' -File |
        Sort-Object LastWriteTimeUtc -Descending)
    if ($existing.Count -ge $Spec.LogRetention) {
        $existing | Select-Object -Skip ($Spec.LogRetention - 1) |
            Remove-Item -Force
    }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
    return Join-Path $logRoot "holder-$stamp-$PID.jsonl"
}

function Write-QQBotWslHolderLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$LogPath,
        [Parameter(Mandatory)][ValidateSet('waiting','exited')][string]$Phase,
        [Parameter(Mandatory)][bool]$Ready,
        [Parameter(Mandatory)][int]$ExitCode
    )

    $record = [pscustomobject][ordered]@{
        timestamp = [DateTime]::UtcNow.ToString('o')
        phase = $Phase
        ready = $Ready
        exitCode = $ExitCode
    }
    $line = $record | ConvertTo-Json -Compress
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, $utf8)
}

function Invoke-QQBotWslProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Spec,
        [Parameter(Mandatory)][string]$LinuxScript,
        [switch]$ProbeOnly
    )

    $wslPath = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslPath -PathType Leaf)) {
        return 20
    }
    $utf8 = New-Object Text.UTF8Encoding($false)
    $encoded = [Convert]::ToBase64String($utf8.GetBytes($LinuxScript))
    $probeFlag = if ($ProbeOnly) { '1' } else { '0' }
    $linuxCommand = "export QQBOT_PROBE_ONLY=$probeFlag; printf '%s' '$encoded' | base64 -d | bash"

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $wslPath -d $Spec.Distro -- bash -lc $linuxCommand 2>&1 | Out-Null
        $exitCode = [int]$LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode
}

function Start-QQBotWslRuntimeHolder {
    [CmdletBinding()]
    param(
        [switch]$ProbeOnly,
        [ValidateRange(0,300)][int]$StartupDelaySeconds = 30
    )

    $spec = Get-QQBotWslAutostartSpec
    if (-not $ProbeOnly -and $StartupDelaySeconds -ne $spec.DelaySeconds) {
        throw (New-QQBotWslAutostartError `
            -Message 'startup_delay_invalid' `
            -Category 'invalid_request' `
            -Reason 'startup_delay_invalid' `
            -ExitCode 2)
    }

    $handle = Enter-QQBotWslHolderMutex
    try {
        if (-not $handle.acquired) {
            return [pscustomobject][ordered]@{
                ok = $true
                alreadyRunning = $true
                ready = $false
                exitCode = 0
            }
        }

        $logPath = $null
        if (-not $ProbeOnly) {
            $logPath = New-QQBotWslHolderLog -Spec $spec
            Write-QQBotWslHolderLog -LogPath $logPath -Phase waiting -Ready $false -ExitCode 0
            Start-Sleep -Seconds $StartupDelaySeconds
        }

        $linuxScript = Get-QQBotWslHolderLinuxScript -Spec $spec
        try {
            $exitCode = Invoke-QQBotWslProcess `
                -Spec $spec -LinuxScript $linuxScript -ProbeOnly:$ProbeOnly
        } catch {
            $exitCode = 20
        }
        $ready = $exitCode -eq 0
        if (-not $ProbeOnly) {
            Write-QQBotWslHolderLog `
                -LogPath $logPath -Phase exited -Ready $ready -ExitCode $exitCode
        }

        return [pscustomobject][ordered]@{
            ok = [bool]$ready
            alreadyRunning = $false
            ready = [bool]$ready
            exitCode = [int]$exitCode
        }
    } finally {
        Exit-QQBotWslHolderMutex -Handle $handle
    }
}

function Invoke-QQBotWslAutostart {
    [CmdletBinding()]
    param(
        [ValidateSet('Plan','Status','Install','Uninstall')][string]$Mode = 'Plan',
        [switch]$Apply,
        [switch]$ReplaceExisting,
        [AllowEmptyString()][string]$InstallRoot
    )

    if ($Mode -in @('Install','Uninstall') -and -not $Apply) {
        throw (New-QQBotWslAutostartError `
            -Message "$Mode requires explicit -Apply" `
            -Category 'invalid_request' `
            -Reason 'apply_required' `
            -ExitCode 2)
    }

    $spec = Get-QQBotWslAutostartSpec -InstallRoot $InstallRoot
    if ($Mode -eq 'Plan') {
        return Get-QQBotWslAutostartPlan -Spec $spec
    }
    if ($Mode -eq 'Status') {
        return Get-QQBotWslAutostartStatus -Spec $spec
    }
    if ($Mode -eq 'Install') {
        return Install-QQBotWslAutostart -Spec $spec -ReplaceExisting:$ReplaceExisting
    }
    return Uninstall-QQBotWslAutostart -Spec $spec -ReplaceExisting:$ReplaceExisting
}

Export-ModuleMember -Function @(
    'Get-QQBotWslAutostartSpec',
    'Invoke-QQBotWslAutostart',
    'Start-QQBotWslRuntimeHolder'
)
