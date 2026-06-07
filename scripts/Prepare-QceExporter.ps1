[CmdletBinding()]
param(
  [string]$Version = "latest",
  [ValidateSet("framework", "plugin", "windows-shell")]
  [string]$Asset = "framework",
  [string]$RuntimeDir,
  [string]$ZipPath,
  [switch]$Force,
  [switch]$SkipDownload,
  [switch]$NoExtract,
  [switch]$ProbeOnly,
  [string]$QceHost = "127.0.0.1",
  [int]$QcePort = 40653,
  [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeDir) {
  $RuntimeDir = Join-Path $Root "runtime_artifacts\qce"
}

$RepoApi = "https://api.github.com/repos/shuakami/qq-chat-exporter"
$UserAgent = "QQBot-QCE-Prepare/1.0"

function Test-QceEndpoint {
  param(
    [string]$HostName,
    [int]$Port,
    [int]$Timeout
  )
  $baseUrl = "http://${HostName}:$Port"
  $result = [ordered]@{
    baseUrl = $baseUrl
    listening = $false
    toolUrl = "$baseUrl/qce-v4-tool"
    error = $null
    statusCode = $null
    contentType = $null
  }
  try {
    $response = Invoke-WebRequest -Uri $result.toolUrl -UseBasicParsing -TimeoutSec $Timeout -Headers @{ "User-Agent" = $UserAgent }
    $result.statusCode = [int]$response.StatusCode
    $result.contentType = [string]$response.Headers["Content-Type"]
    $result.listening = $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
  } catch {
    $result.error = $_.Exception.Message
  }
  return $result
}

function Get-QceRelease {
  param([string]$RequestedVersion)
  $uri = if ($RequestedVersion -eq "latest") {
    "$RepoApi/releases/latest"
  } else {
    "$RepoApi/releases/tags/$RequestedVersion"
  }
  return Invoke-RestMethod -Uri $uri -Headers @{ "User-Agent" = $UserAgent }
}

function Select-QceAsset {
  param(
    [object]$Release,
    [string]$AssetKind
  )
  $pattern = switch ($AssetKind) {
    "framework" { "^NapCat-Framework-QCE-.*\.zip$" }
    "plugin" { "^napcat-plugin-qce\.zip$" }
    "windows-shell" { "^NapCat-QCE-Windows-x64-.*\.zip$" }
  }
  $matches = @($Release.assets | Where-Object { $_.name -match $pattern })
  if ($matches.Count -eq 0) {
    throw "No QCE release asset matched kind '$AssetKind' with pattern '$pattern'."
  }
  return $matches[0]
}

function Save-UrlToFile {
  param(
    [string]$Url,
    [string]$Destination,
    [int]$Timeout
  )
  $parent = Split-Path -Parent $Destination
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $partial = "$Destination.part"
  if (Test-Path -LiteralPath $partial) {
    Remove-Item -LiteralPath $partial -Force
  }

  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl) {
    $curlArgs = @(
      "-L",
      "--fail",
      "--retry", "5",
      "--retry-delay", "3",
      "--connect-timeout", [string]$Timeout,
      "--speed-time", "60",
      "--speed-limit", "1024",
      "-A", $UserAgent,
      "-o", $partial,
      $Url
    )
    & $curl.Source @curlArgs
    if ($LASTEXITCODE -ne 0) {
      throw "curl.exe failed with exit code $LASTEXITCODE while downloading $Url"
    }
  } else {
    Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -TimeoutSec $Timeout -Headers @{ "User-Agent" = $UserAgent }
  }
  Move-Item -LiteralPath $partial -Destination $Destination -Force
}

function Test-ZipFile {
  param([string]$Path)
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = $null
  try {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
    return $zip.Entries.Count
  } finally {
    if ($zip) {
      $zip.Dispose()
    }
  }
}

function Get-RelativeToolFiles {
  param([string]$Directory)
  if (-not (Test-Path -LiteralPath $Directory)) {
    return @()
  }
  $base = (Resolve-Path -LiteralPath $Directory).Path.TrimEnd("\")
  return @(
    Get-ChildItem -LiteralPath $Directory -Recurse -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match "\.(bat|cmd|ps1|exe)$" -or $_.Name -eq "package.json" } |
      Select-Object -First 80 |
      ForEach-Object {
        [ordered]@{
          path = $_.FullName.Substring($base.Length).TrimStart("\")
          sizeBytes = $_.Length
        }
      }
  )
}

$probe = Test-QceEndpoint -HostName $QceHost -Port $QcePort -Timeout ([Math]::Min($TimeoutSeconds, 10))
if ($ProbeOnly) {
  [ordered]@{
    mode = "probe-only"
    qce = $probe
    nextSteps = @(
      "If qce.listening=false, prepare or start QCE until http://127.0.0.1:40653/qce-v4-tool is reachable.",
      "After exporting STREAMING_JSONL, rerun tools/check_qce_exporter_status.py and tools/audit_human_like_goal.py."
    )
  } | ConvertTo-Json -Depth 8
  exit 0
}

$release = $null
$assetInfo = $null
$resolvedVersion = "local"
$downloadUrl = $null
$expectedSize = $null

if (-not $ZipPath) {
  $release = Get-QceRelease -RequestedVersion $Version
  $assetInfo = Select-QceAsset -Release $release -AssetKind $Asset
  $resolvedVersion = [string]$release.tag_name
  $downloadUrl = [string]$assetInfo.browser_download_url
  $expectedSize = [int64]$assetInfo.size
  $versionDir = Join-Path $RuntimeDir $resolvedVersion
  New-Item -ItemType Directory -Force -Path $versionDir | Out-Null
  $ZipPath = Join-Path $versionDir ([string]$assetInfo.name)
} else {
  $ZipPath = (Resolve-Path -LiteralPath $ZipPath).Path
  $versionDir = Split-Path -Parent $ZipPath
}

if ((Test-Path -LiteralPath $ZipPath) -and $Force) {
  Remove-Item -LiteralPath $ZipPath -Force
}

if (-not (Test-Path -LiteralPath $ZipPath)) {
  if ($SkipDownload) {
    throw "ZipPath does not exist and -SkipDownload was supplied: $ZipPath"
  }
  if (-not $downloadUrl) {
    throw "ZipPath was not provided and no download URL was resolved."
  }
  Save-UrlToFile -Url $downloadUrl -Destination $ZipPath -Timeout $TimeoutSeconds
}

$zipItem = Get-Item -LiteralPath $ZipPath
if ($expectedSize -and $zipItem.Length -ne $expectedSize) {
  throw "Downloaded asset size mismatch. Expected $expectedSize bytes, got $($zipItem.Length) bytes: $ZipPath"
}

$entryCount = Test-ZipFile -Path $ZipPath
$hash = Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256
$extractDir = Join-Path (Split-Path -Parent $ZipPath) ([System.IO.Path]::GetFileNameWithoutExtension($ZipPath))

if (-not $NoExtract) {
  New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $extractDir -Force
}

$toolFiles = Get-RelativeToolFiles -Directory $extractDir
$afterProbe = Test-QceEndpoint -HostName $QceHost -Port $QcePort -Timeout ([Math]::Min($TimeoutSeconds, 10))

[ordered]@{
  mode = "prepare"
  repository = "https://github.com/shuakami/qq-chat-exporter"
  version = $resolvedVersion
  assetKind = $Asset
  assetName = if ($assetInfo) { [string]$assetInfo.name } else { Split-Path -Leaf $ZipPath }
  assetUrl = $downloadUrl
  zipPath = $ZipPath
  zipSizeBytes = $zipItem.Length
  zipSha256 = $hash.Hash
  zipEntryCount = $entryCount
  extractDir = if (Test-Path -LiteralPath $extractDir) { $extractDir } else { $null }
  extractedToolFiles = $toolFiles
  qce = $afterProbe
  safety = [ordered]@{
    startsLoader = $false
    mutatesQqLoginState = $false
    readsSecurityJson = $false
  }
  nextSteps = @(
    "Inspect the extracted QCE files under extractDir. This script does not start any loader.",
    "Start QCE manually only when you are ready to attach it to the current QQ/NapCat session.",
    "Wait until http://127.0.0.1:40653/qce-v4-tool is reachable.",
    "Export the target group as STREAMING_JSONL/chunked-jsonl to %USERPROFILE%\\.qq-chat-exporter\\exports.",
    "Rerun tools/check_qce_exporter_status.py, tools/build_style_profile.py, and tools/audit_human_like_goal.py."
  )
} | ConvertTo-Json -Depth 10
