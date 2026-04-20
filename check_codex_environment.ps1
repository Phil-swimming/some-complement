[CmdletBinding()]
param(
  [string]$UserHome = "",
  [switch]$NoBootstrap,
  [switch]$RequirePersistent,
  [switch]$Build
)

$ErrorActionPreference = "Stop"

function Resolve-RepoRoot {
  return [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
}

function Resolve-UserHomeFromRepo {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  $repoUserMatch = [regex]::Match($RepoRoot, '^(?<root>[A-Za-z]:\\Users\\[^\\]+)\\')
  if ($repoUserMatch.Success -and (Test-Path $repoUserMatch.Groups["root"].Value)) {
    return [System.IO.Path]::GetFullPath($repoUserMatch.Groups["root"].Value)
  }

  if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE) -and (Test-Path $env:USERPROFILE)) {
    return [System.IO.Path]::GetFullPath($env:USERPROFILE)
  }

  throw "Unable to resolve UserHome. Pass -UserHome explicitly."
}

function Get-ExpectedEnvironment {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $systemDrive = [System.IO.Path]::GetPathRoot($UserHome)
  if ([string]::IsNullOrWhiteSpace($systemDrive)) {
    $systemDrive = "C:\"
  }
  $systemDrive = $systemDrive.TrimEnd("\")

  $windowsRoot = Join-Path $systemDrive "Windows"
  if (-not (Test-Path $windowsRoot) -and (Test-Path "C:\Windows")) {
    $windowsRoot = "C:\Windows"
    $systemDrive = "C:"
  }

  $localAppData = Join-Path $UserHome "AppData\Local"
  $homeDrive = [System.IO.Path]::GetPathRoot($UserHome)
  if (-not [string]::IsNullOrWhiteSpace($homeDrive)) {
    $homeDrive = $homeDrive.TrimEnd("\")
  }

  $homePath = $UserHome
  if (-not [string]::IsNullOrWhiteSpace($homeDrive) -and $UserHome.StartsWith($homeDrive, [System.StringComparison]::OrdinalIgnoreCase)) {
    $homePath = $UserHome.Substring($homeDrive.Length)
  }
  if ([string]::IsNullOrWhiteSpace($homePath)) {
    $homePath = "\"
  }

  return [ordered]@{
    SystemDrive = $systemDrive
    SystemRoot = $windowsRoot
    windir = $windowsRoot
    ComSpec = (Join-Path $windowsRoot "System32\cmd.exe")
    USERPROFILE = $UserHome
    HOME = $UserHome
    HOMEDRIVE = $homeDrive
    HOMEPATH = $homePath
    APPDATA = (Join-Path $UserHome "AppData\Roaming")
    LOCALAPPDATA = $localAppData
    TEMP = (Join-Path $localAppData "Temp")
    TMP = (Join-Path $localAppData "Temp")
    CODEX_CLI_PATH = (Join-Path $UserHome "AppData\Local\OpenAI\Codex\bin\codex.exe")
  }
}

function Set-ProcessEnvironment {
  param(
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$Values,
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  foreach ($name in $Values.Keys) {
    $value = [string]$Values[$name]
    if (-not [string]::IsNullOrWhiteSpace($value)) {
      Set-Item -Path "Env:$name" -Value $value
    }
  }

  $prepend = @(
    (Join-Path $UserHome "AppData\Local\OpenAI\Codex\cli-tools"),
    (Join-Path $UserHome "AppData\Local\OpenAI\Codex\python-tools"),
    (Join-Path $UserHome "AppData\Local\OpenAI\Codex\bin")
  ) | Where-Object { Test-Path $_ }

  $entries = New-Object 'System.Collections.Generic.List[string]'
  foreach ($entry in @($prepend + ($env:PATH -split ";"))) {
    if ([string]::IsNullOrWhiteSpace($entry)) {
      continue
    }

    $normalized = $entry.Trim()
    $alreadyPresent = $false
    foreach ($existing in $entries) {
      if ($existing.TrimEnd("\").ToLowerInvariant() -eq $normalized.TrimEnd("\").ToLowerInvariant()) {
        $alreadyPresent = $true
        break
      }
    }

    if (-not $alreadyPresent) {
      $entries.Add($normalized) | Out-Null
    }
  }

  $env:PATH = $entries -join ";"
}

function Test-UserEnvironment {
  param(
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$Expected
  )

  $rows = @()
  foreach ($name in $Expected.Keys) {
    $expectedValue = [string]$Expected[$name]
    $actualValue = [Environment]::GetEnvironmentVariable($name, "User")
    $rows += [pscustomobject]@{
      Name = $name
      Status = if ($actualValue -eq $expectedValue) { "OK" } else { "FIX" }
      UserValue = $actualValue
      Expected = $expectedValue
    }
  }

  return $rows
}

function Invoke-Check {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [scriptblock]$ScriptBlock
  )

  try {
    $global:LASTEXITCODE = 0
    $output = & $ScriptBlock 2>&1
    $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    $text = ($output | Select-Object -First 4) -join " "

    [pscustomobject]@{
      Name = $Name
      Status = if ($exitCode -eq 0) { "OK" } else { "FAIL" }
      ExitCode = $exitCode
      Detail = $text
    }
  } catch {
    [pscustomobject]@{
      Name = $Name
      Status = "FAIL"
      ExitCode = -1
      Detail = $_.Exception.Message
    }
  }
}

$repoRoot = Resolve-RepoRoot
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($UserHome)) {
  $UserHome = Resolve-UserHomeFromRepo -RepoRoot $repoRoot
} else {
  $UserHome = [System.IO.Path]::GetFullPath($UserHome)
}

$targetUserName = Split-Path $UserHome -Leaf
$enforcePersistent = $RequirePersistent -or $currentIdentity.EndsWith("\" + $targetUserName, [System.StringComparison]::OrdinalIgnoreCase)

$expected = Get-ExpectedEnvironment -UserHome $UserHome
$rawMissing = @(
  "SystemRoot",
  "windir",
  "ComSpec",
  "USERPROFILE",
  "HOME",
  "APPDATA",
  "LOCALAPPDATA",
  "CODEX_CLI_PATH"
) | Where-Object { [string]::IsNullOrWhiteSpace((Get-Item -Path "Env:$_" -ErrorAction SilentlyContinue).Value) }

if (-not $NoBootstrap) {
  Set-ProcessEnvironment -Values $expected -UserHome $UserHome
}

Write-Host "Repo root : $repoRoot"
Write-Host "User home : $UserHome"
Write-Host "Identity  : $currentIdentity"
Write-Host "Bootstrap : $(if ($NoBootstrap) { 'off' } else { 'process only' })"
Write-Host "Persistent env gate : $(if ($enforcePersistent) { 'required' } else { 'reported only' })"
Write-Host "Raw missing process env : $(if ($rawMissing.Count -gt 0) { $rawMissing -join ', ' } else { 'none' })"
Write-Host ""

Write-Host "Persistent user environment:"
$envRows = Test-UserEnvironment -Expected $expected
$envRows |
  Select-Object Name, Status, UserValue |
  Format-Table -AutoSize

Write-Host ""
Write-Host "Command checks:"
$checks = New-Object 'System.Collections.Generic.List[object]'
$checks.Add((Invoke-Check "codex" { codex --version })) | Out-Null
$checks.Add((Invoke-Check "rg" { rg --version })) | Out-Null
$checks.Add((Invoke-Check "git" { git --version })) | Out-Null
$checks.Add((Invoke-Check "node-crypto" { node -e "console.log(process.version); console.log(require('crypto').randomBytes(4).toString('hex'))" })) | Out-Null
$checks.Add((Invoke-Check "npm" { npm --version })) | Out-Null
$checks.Add((Invoke-Check "python" { python --version })) | Out-Null
$checks.Add((Invoke-Check "pip" { pip --version })) | Out-Null
$checks.Add((Invoke-Check "code" { code --version })) | Out-Null
$checks.Add((Invoke-Check "node-red" { node-red --version })) | Out-Null
$checks.Add((Invoke-Check "uv4" { Get-Command UV4.exe -ErrorAction Stop | Select-Object -ExpandProperty Source })) | Out-Null

if ($Build) {
  $checks.Add((Invoke-Check "keil-build" { & (Join-Path $repoRoot "tools\build_keil.ps1") })) | Out-Null
}

$checks |
  Select-Object Name, Status, ExitCode, Detail |
  Format-Table -AutoSize

$failedEnv = @($envRows | Where-Object { $_.Status -ne "OK" })
$failedChecks = @($checks | Where-Object { $_.Status -ne "OK" })

if ($failedEnv.Count -gt 0 -and -not $enforcePersistent) {
  Write-Warning "Persistent user environment differs for the current token. This is expected inside CodexSandboxOffline; run with -RequirePersistent from the target user context to enforce it."
  $failedEnv = @()
}

if ($failedEnv.Count -gt 0 -or $failedChecks.Count -gt 0) {
  throw "Codex environment check failed. Run tools\setup_codex_windows_access.ps1, then restart Codex if persistent values were changed."
}

Write-Host ""
Write-Host "Codex environment check passed."
