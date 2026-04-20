[CmdletBinding()]
param(
  [string]$UserHome = "",
  [string[]]$ExtraPaths = @(),
  [switch]$AllowCustomUserHome,
  [switch]$DryRun,
  [switch]$NoCreateMissing,
  [switch]$SkipAclGrants,
  [switch]$SkipToolchainCaches,
  [switch]$SkipGroupGrant,
  [switch]$SkipArg0PowerShellWrappers,
  [switch]$SyncArg0Wrappers
)

$ErrorActionPreference = "Stop"

function Add-PathIfMissing {
  param(
    [Parameter(Mandatory = $true)]
    [AllowEmptyCollection()]
    [System.Collections.Generic.List[string]]$List,
    [Parameter(Mandatory = $true)]
    [string]$PathValue
  )

  if ([string]::IsNullOrWhiteSpace($PathValue)) {
    return
  }

  $fullPath = [System.IO.Path]::GetFullPath($PathValue)
  if (-not $List.Contains($fullPath)) {
    $List.Add($fullPath) | Out-Null
  }
}

function Test-LikelyUserHome {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PathValue
  )

  if ([string]::IsNullOrWhiteSpace($PathValue)) {
    return $false
  }

  try {
    $fullPath = [System.IO.Path]::GetFullPath($PathValue)
  } catch {
    return $false
  }

  if (-not (Test-Path $fullPath)) {
    return $false
  }

  foreach ($marker in @("AppData", "Desktop", "Documents")) {
    if (Test-Path (Join-Path $fullPath $marker)) {
      return $true
    }
  }

  return $false
}

function Resolve-DefaultUserHome {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
  )

  $candidates = New-Object 'System.Collections.Generic.List[string]'

  try {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (-not [string]::IsNullOrWhiteSpace($sid)) {
      $profileKey = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
      $profileImagePath = (Get-ItemProperty -Path $profileKey -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath
      if (-not [string]::IsNullOrWhiteSpace($profileImagePath)) {
        Add-PathIfMissing -List $candidates -PathValue ([Environment]::ExpandEnvironmentVariables($profileImagePath))
      }
    }
  } catch {
  }

  $repoDerivedHome = Split-Path (Split-Path (Split-Path $RepoRoot -Parent) -Parent) -Parent
  if (-not [string]::IsNullOrWhiteSpace($repoDerivedHome)) {
    Add-PathIfMissing -List $candidates -PathValue $repoDerivedHome
  }

  foreach ($candidate in @(
    $env:USERPROFILE,
    $HOME,
    [Environment]::GetFolderPath("UserProfile")
  )) {
    if (-not [string]::IsNullOrWhiteSpace($candidate)) {
      Add-PathIfMissing -List $candidates -PathValue $candidate
    }
  }

  foreach ($candidate in $candidates) {
    if (Test-LikelyUserHome -PathValue $candidate) {
      return [System.IO.Path]::GetFullPath($candidate)
    }
  }

  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return [System.IO.Path]::GetFullPath($candidate)
    }
  }

  $fallbackHome = Split-Path (Split-Path (Split-Path $RepoRoot -Parent) -Parent) -Parent
  if (-not (Test-Path $fallbackHome)) {
    throw "无法确定用户目录，请通过 -UserHome 显式传入。"
  }

  return [System.IO.Path]::GetFullPath($fallbackHome)
}

function Ensure-DirectoryIfNeeded {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PathValue
  )

  if (Test-Path $PathValue) {
    return
  }

  if ($NoCreateMissing -or $DryRun) {
    return
  }

  New-Item -ItemType Directory -Force -Path $PathValue | Out-Null
}

function Test-PathEntryEquals {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Left,
    [Parameter(Mandatory = $true)]
    [string]$Right
  )

  return $Left.Trim().TrimEnd("\").ToLowerInvariant() -eq $Right.Trim().TrimEnd("\").ToLowerInvariant()
}

function Resolve-CodexHelperSource {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $packageRoot = Join-Path $UserHome "AppData\Local\Packages"

  try {
    if (Test-Path $packageRoot) {
      $packageDirs = Get-ChildItem $packageRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "OpenAI.Codex_*" } |
        Sort-Object LastWriteTime -Descending

      foreach ($dir in $packageDirs) {
        $candidate = Join-Path $dir.FullName "LocalCache\Local\OpenAI\Codex\bin"
        if (Test-Path (Join-Path $candidate "codex.exe")) {
          return $candidate
        }
      }
    }
  } catch {
  }

  try {
    $appx = Get-AppxPackage -Name OpenAI.Codex -ErrorAction Stop |
      Sort-Object Version -Descending |
      Select-Object -First 1

    if ($null -ne $appx -and -not [string]::IsNullOrWhiteSpace($appx.InstallLocation)) {
      $resourceCandidate = Join-Path $appx.InstallLocation "app\resources"
      if (Test-Path (Join-Path $resourceCandidate "codex.exe")) {
        return $resourceCandidate
      }
    }
  } catch {
  }

  foreach ($pathEntry in ($env:Path -split ';')) {
    if ([string]::IsNullOrWhiteSpace($pathEntry)) {
      continue
    }

    $candidate = $pathEntry.Trim()
    if (Test-Path (Join-Path $candidate "codex.exe")) {
      return $candidate
    }
  }

  $windowsAppsRoots = @()
  $programFilesCandidates = @(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles),
    $env:ProgramW6432,
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)}
  )

  if (-not [string]::IsNullOrWhiteSpace($env:SystemDrive)) {
    $programFilesCandidates += (Join-Path $env:SystemDrive "Program Files")
  }

  $programFilesCandidates += "C:\Program Files"

  foreach ($rootCandidate in $programFilesCandidates) {
    if ([string]::IsNullOrWhiteSpace($rootCandidate)) {
      continue
    }

    $windowsAppsRoot = Join-Path $rootCandidate "WindowsApps"
    if (($windowsAppsRoots -notcontains $windowsAppsRoot) -and (Test-Path $windowsAppsRoot)) {
      $windowsAppsRoots += $windowsAppsRoot
    }
  }

  foreach ($windowsAppsRoot in $windowsAppsRoots) {
    try {
      $packageDirs = Get-ChildItem $windowsAppsRoot -Directory -Filter "OpenAI.Codex_*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending

      foreach ($dir in $packageDirs) {
        $candidate = Join-Path $dir.FullName "app\resources"
        if (Test-Path (Join-Path $candidate "codex.exe")) {
          return $candidate
        }
      }
    } catch {
    }
  }

  return $null
}

function Sync-FileIfNeeded {
  param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [Parameter(Mandatory = $true)]
    [string]$DestinationPath
  )

  if (-not (Test-Path $SourcePath)) {
    return
  }

  $shouldCopy = $true

  if (Test-Path $DestinationPath) {
    try {
      $sourceItem = Get-Item $SourcePath -ErrorAction Stop
      $destinationItem = Get-Item $DestinationPath -ErrorAction Stop
      if ($sourceItem.Length -eq $destinationItem.Length) {
        $smallFileThreshold = 1024 * 1024

        if ($sourceItem.Length -le $smallFileThreshold) {
          $sourceBytes = [System.IO.File]::ReadAllBytes($SourcePath)
          $destinationBytes = [System.IO.File]::ReadAllBytes($DestinationPath)
          $shouldCopy = -not [System.Linq.Enumerable]::SequenceEqual($sourceBytes, $destinationBytes)
        } else {
          $shouldCopy = $false
        }
      }
    } catch {
    }
  }

  if (-not $shouldCopy) {
    return
  }

  if ($DryRun) {
    Write-Host "DRYRUN Copy-Item `"$SourcePath`" `"$DestinationPath`" -Force"
    return
  }

  try {
    Copy-Item $SourcePath $DestinationPath -Force
  } catch {
    Write-Warning "同步 Codex helper 失败: $SourcePath -> $DestinationPath ; $($_.Exception.Message)"
  }
}

function Ensure-UserPathContains {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PathValue,
    [switch]$Prepend
  )

  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $entries = New-Object 'System.Collections.Generic.List[string]'

  foreach ($entry in ($userPath -split ";")) {
    if (-not [string]::IsNullOrWhiteSpace($entry)) {
      $normalizedEntry = $entry.Trim()
      if (-not (Test-PathEntryEquals -Left $normalizedEntry -Right $PathValue)) {
        $entries.Add($normalizedEntry) | Out-Null
      }
    }
  }

  if ($Prepend) {
    $updatedEntries = @($PathValue) + @($entries)
  } else {
    $updatedEntries = @($entries)
    $alreadyPresent = $false
    foreach ($entry in $entries) {
      if (Test-PathEntryEquals -Left $entry -Right $PathValue) {
        $alreadyPresent = $true
        break
      }
    }

    if (-not $alreadyPresent) {
      $updatedEntries += $PathValue
    }
  }

  $updatedPath = $updatedEntries -join ";"

  if ($DryRun) {
    if ($Prepend) {
      Write-Host "DRYRUN prepend user PATH with $PathValue"
    } else {
      Write-Host "DRYRUN set user PATH += $PathValue"
    }
    return
  }

  [Environment]::SetEnvironmentVariable("Path", $updatedPath, "User")
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if ([string]::IsNullOrWhiteSpace($machinePath)) {
    $env:PATH = $updatedPath
  } else {
    $env:PATH = $updatedPath + ";" + $machinePath
  }
}

function ConvertTo-PowerShellSingleQuotedLiteral {
  param(
    [Parameter(Mandatory = $true)]
    [AllowEmptyString()]
    [string]$Value
  )

  return "'" + ($Value -replace "'", "''") + "'"
}

function Get-CodexEnvironmentDefaults {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome,
    [Parameter(Mandatory = $true)]
    [string]$WindowsRoot
  )

  $values = [ordered]@{}

  $systemDrive = [System.IO.Path]::GetPathRoot($WindowsRoot)
  if ([string]::IsNullOrWhiteSpace($systemDrive)) {
    $systemDrive = [System.IO.Path]::GetPathRoot($UserHome)
  }
  if ([string]::IsNullOrWhiteSpace($systemDrive)) {
    $systemDrive = "C:\"
  }

  $systemDrive = $systemDrive.TrimEnd("\")
  $cmdPath = Join-Path $WindowsRoot "System32\cmd.exe"
  $programData = Join-Path $systemDrive "ProgramData"
  $appData = Join-Path $UserHome "AppData\Roaming"
  $localAppData = Join-Path $UserHome "AppData\Local"
  $tempPath = Join-Path $localAppData "Temp"

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

  $values["SystemDrive"] = $systemDrive
  $values["SystemRoot"] = $WindowsRoot
  $values["windir"] = $WindowsRoot
  $values["COMPUTERNAME"] = [Environment]::MachineName
  if (Test-Path $cmdPath) {
    $values["ComSpec"] = $cmdPath
  }
  if (Test-Path $programData) {
    $values["ProgramData"] = $programData
  }
  $values["USERPROFILE"] = $UserHome
  $values["HOME"] = $UserHome
  if (-not [string]::IsNullOrWhiteSpace($homeDrive)) {
    $values["HOMEDRIVE"] = $homeDrive
  }
  $values["HOMEPATH"] = $homePath
  if (Test-Path $appData) {
    $values["APPDATA"] = $appData
  }
  if (Test-Path $localAppData) {
    $values["LOCALAPPDATA"] = $localAppData
  }
  if (Test-Path $tempPath) {
    $values["TEMP"] = $tempPath
    $values["TMP"] = $tempPath
  }

  return $values
}

function New-PowerShellEnvironmentPrelude {
  param(
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$EnvironmentValues
  )

  $lines = @(
    "`$ErrorActionPreference = 'Stop'"
  )

  foreach ($key in $EnvironmentValues.Keys) {
    $value = [string]$EnvironmentValues[$key]
    if ([string]::IsNullOrWhiteSpace($value)) {
      continue
    }

    $lines += ('$env:{0} = {1}' -f $key, (ConvertTo-PowerShellSingleQuotedLiteral -Value $value))
  }

  return ($lines -join "`r`n") + "`r`n"
}

function New-CodexPowerShellProfileBootstrap {
  param(
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$EnvironmentValues,
    [Parameter(Mandatory = $true)]
    [string[]]$StableDirectories
  )

  $lines = @(
    "# <codex-environment-bootstrap>",
    "# Generated by tools\setup_codex_windows_access.ps1.",
    "# Fill essential Windows variables when a host starts PowerShell with a sparse environment.",
    "`$codexProfileValues = [ordered]@{"
  )

  foreach ($key in $EnvironmentValues.Keys) {
    $value = [string]$EnvironmentValues[$key]
    if ([string]::IsNullOrWhiteSpace($value)) {
      continue
    }

    $lines += ("  {0} = {1}" -f $key, (ConvertTo-PowerShellSingleQuotedLiteral -Value $value))
  }

  $lines += "}"
  $lines += ""
  $lines += "foreach (`$name in `$codexProfileValues.Keys) {"
  $lines += "  `$current = [Environment]::GetEnvironmentVariable(`$name, `"Process`")"
  $lines += "  if ([string]::IsNullOrWhiteSpace(`$current)) {"
  $lines += "    [Environment]::SetEnvironmentVariable(`$name, [string]`$codexProfileValues[`$name], `"Process`")"
  $lines += "  }"
  $lines += "}"
  $lines += ""
  $lines += "`$codexProfilePrepend = @("

  foreach ($dir in $StableDirectories) {
    if ([string]::IsNullOrWhiteSpace($dir)) {
      continue
    }

    $lines += ("  {0}" -f (ConvertTo-PowerShellSingleQuotedLiteral -Value $dir))
  }

  $lines += ") | Where-Object { Test-Path `$_ }"
  $lines += ""
  $lines += "`$codexProfilePathEntries = New-Object `"System.Collections.Generic.List[string]`""
  $lines += "foreach (`$entry in @(`$codexProfilePrepend + (`$env:PATH -split `";`"))) {"
  $lines += "  if ([string]::IsNullOrWhiteSpace(`$entry)) {"
  $lines += "    continue"
  $lines += "  }"
  $lines += ""
  $lines += "  `$normalized = `$entry.Trim()"
  $lines += "  `$alreadyPresent = `$false"
  $lines += "  foreach (`$existing in `$codexProfilePathEntries) {"
  $lines += "    if (`$existing.TrimEnd(`"\`").Equals(`$normalized.TrimEnd(`"\`"), [System.StringComparison]::OrdinalIgnoreCase)) {"
  $lines += "      `$alreadyPresent = `$true"
  $lines += "      break"
  $lines += "    }"
  $lines += "  }"
  $lines += ""
  $lines += "  if (-not `$alreadyPresent) {"
  $lines += "    `$codexProfilePathEntries.Add(`$normalized) | Out-Null"
  $lines += "  }"
  $lines += "}"
  $lines += ""
  $lines += "if (`$codexProfilePathEntries.Count -gt 0) {"
  $lines += "  `$env:PATH = `$codexProfilePathEntries -join `";`""
  $lines += "}"
  $lines += "# </codex-environment-bootstrap>"

  return ($lines -join "`r`n") + "`r`n"
}

function Install-PowerShellProfileBootstrap {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome,
    [Parameter(Mandatory = $true)]
    [System.Collections.IDictionary]$EnvironmentValues,
    [Parameter(Mandatory = $true)]
    [string[]]$StableDirectories
  )

  $profileDirectory = Join-Path $UserHome "Documents\PowerShell"
  $profilePath = Join-Path $profileDirectory "profile.ps1"
  $bootstrap = New-CodexPowerShellProfileBootstrap -EnvironmentValues $EnvironmentValues -StableDirectories $StableDirectories

  if ($DryRun) {
    Write-Host "DRYRUN install PowerShell profile bootstrap $profilePath"
    return
  }

  Ensure-DirectoryIfNeeded -PathValue $profileDirectory

  $existing = ""
  if (Test-Path $profilePath) {
    $existing = Get-Content -Path $profilePath -Raw -ErrorAction SilentlyContinue
  }

  $pattern = "(?s)# <codex-environment-bootstrap>\r?\n.*?\r?\n# </codex-environment-bootstrap>\r?\n?"
  if ([regex]::IsMatch($existing, $pattern)) {
    $updated = [regex]::Replace(
      $existing,
      $pattern,
      [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $bootstrap }
    )
  } elseif ([string]::IsNullOrWhiteSpace($existing)) {
    $updated = $bootstrap
  } else {
    $updated = $existing.TrimEnd() + "`r`n`r`n" + $bootstrap
  }

  if ($existing -cne $updated) {
    Set-Content -Path $profilePath -Value $updated -Encoding Ascii
  }
}

function New-PowerShellWrapperBodyForExecutable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  $targetLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $TargetPath
  return $script:CodexWrapperEnvironmentPrelude + @"
& $targetLiteral @args
exit `$LASTEXITCODE
"@ -replace "`n", "`r`n"
}

function New-PowerShellWrapperBodyForBatchScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  $targetLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $TargetPath
  return $script:CodexWrapperEnvironmentPrelude + @"
& `$env:ComSpec /d /c call $targetLiteral @args
exit `$LASTEXITCODE
"@ -replace "`n", "`r`n"
}

function New-PowerShellWrapperBodyForPowerShellScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  $targetLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $TargetPath
  return $script:CodexWrapperEnvironmentPrelude + @"
& powershell -NoProfile -ExecutionPolicy Bypass -File $targetLiteral @args
exit `$LASTEXITCODE
"@ -replace "`n", "`r`n"
}

function New-PowerShellWrapperBodyForPythonModule {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$ModuleName
  )

  $pythonLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $PythonExe
  return $script:CodexWrapperEnvironmentPrelude + @"
& $pythonLiteral -m $ModuleName @args
exit `$LASTEXITCODE
"@ -replace "`n", "`r`n"
}

function New-PowerShellWrapperBodyForPythonScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$ScriptPath
  )

  $pythonLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $PythonExe
  $scriptLiteral = ConvertTo-PowerShellSingleQuotedLiteral -Value $ScriptPath
  return $script:CodexWrapperEnvironmentPrelude + @"
& $pythonLiteral $scriptLiteral @args
exit `$LASTEXITCODE
"@ -replace "`n", "`r`n"
}

function Sync-PowerShellWrappersToCodexArg0 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $arg0Directories = Get-CodexArg0Directories -UserHome $UserHome
  if ($arg0Directories.Count -eq 0) {
    return 0
  }

  $wrapperFiles = Get-ChildItem $SourceDirectory -Filter *.ps1 -File -ErrorAction SilentlyContinue |
    Sort-Object Name

  if ($null -eq $wrapperFiles -or $wrapperFiles.Count -eq 0) {
    return 0
  }

  $syncedDirectoryCount = 0
  foreach ($arg0Directory in $arg0Directories) {
    if (-not (Test-Path $arg0Directory)) {
      continue
    }

    foreach ($wrapperFile in $wrapperFiles) {
      $destinationPath = Join-Path $arg0Directory $wrapperFile.Name
      Sync-FileIfNeeded -SourcePath $wrapperFile.FullName -DestinationPath $destinationPath
    }

    $syncedDirectoryCount++
  }

  return $syncedDirectoryCount
}

function Set-UserEnvironmentValue {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  if ($DryRun) {
    Write-Host "DRYRUN set user env $Name = $Value"
    return
  }

  [Environment]::SetEnvironmentVariable($Name, $Value, "User")
  Set-Item -Path "Env:$Name" -Value $Value
}

function Broadcast-EnvironmentChange {
  if ($DryRun) {
    return
  }

  Add-Type -Namespace Win32 -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true, CharSet=System.Runtime.InteropServices.CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, int Msg, System.UIntPtr wParam, string lParam, int fuFlags, int uTimeout, out System.UIntPtr lpdwResult);
'@ -ErrorAction SilentlyContinue

  [UIntPtr]$result = [UIntPtr]::Zero
  [void][Win32.Native]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$result)
}

function Resolve-PreferredPythonRoot {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $localPythonRoot = Join-Path $UserHome "AppData\Local\Python"
  if (Test-Path $localPythonRoot) {
    $candidates = Get-ChildItem $localPythonRoot -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like "pythoncore-*" } |
      Sort-Object Name -Descending

    foreach ($candidate in $candidates) {
      if (Test-Path (Join-Path $candidate.FullName "python.exe")) {
        return $candidate.FullName
      }
    }
  }

  $programsPythonRoot = Join-Path $UserHome "AppData\Local\Programs\Python"
  if (Test-Path $programsPythonRoot) {
    $candidates = Get-ChildItem $programsPythonRoot -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like "Python*" } |
      Sort-Object Name -Descending

    foreach ($candidate in $candidates) {
      if (Test-Path (Join-Path $candidate.FullName "python.exe")) {
        return $candidate.FullName
      }
    }
  }

  return $null
}

function Resolve-PythonCommandHost {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PreferredPythonRoot,
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $launcher = Join-Path $UserHome "AppData\Local\Python\bin\python.exe"
  if (Test-Path $launcher) {
    return $launcher
  }

  $preferredPython = Join-Path $PreferredPythonRoot "python.exe"
  if (Test-Path $preferredPython) {
    return $preferredPython
  }

  return $null
}

function Resolve-PythonToolSourceDirectories {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome,
    [Parameter(Mandatory = $false)]
    [string]$PreferredPythonRoot
  )

  $results = New-Object 'System.Collections.Generic.List[string]'

  $orderedCandidates = @(
    (Join-Path $UserHome "AppData\Local\Python\bin"),
    $PreferredPythonRoot,
    $(if (-not [string]::IsNullOrWhiteSpace($PreferredPythonRoot)) { Join-Path $PreferredPythonRoot "Scripts" } else { $null }),
    (Join-Path $UserHome "AppData\Local\Programs\Python\Python312"),
    (Join-Path $UserHome "AppData\Local\Programs\Python\Python312\Scripts")
  )

  foreach ($candidate in $orderedCandidates) {
    if ([string]::IsNullOrWhiteSpace($candidate)) {
      continue
    }

    if (Test-Path $candidate) {
      Add-PathIfMissing -List $results -PathValue $candidate
    }
  }

  $programsPythonRoot = Join-Path $UserHome "AppData\Local\Programs\Python"
  if (Test-Path $programsPythonRoot) {
    $programDirs = Get-ChildItem $programsPythonRoot -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -like "Python*" } |
      Sort-Object Name -Descending

    foreach ($dir in $programDirs) {
      Add-PathIfMissing -List $results -PathValue $dir.FullName
      $scriptsDir = Join-Path $dir.FullName "Scripts"
      if (Test-Path $scriptsDir) {
        Add-PathIfMissing -List $results -PathValue $scriptsDir
      }
    }
  }

  return $results
}

function Resolve-CommonCliSourceDirectories {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $results = New-Object 'System.Collections.Generic.List[string]'

  $programFilesRoots = New-Object 'System.Collections.Generic.List[string]'
  foreach ($programFilesRoot in @(
    $env:ProgramFiles,
    ${env:ProgramW6432},
    ${env:ProgramFiles(x86)}
  )) {
    if (-not [string]::IsNullOrWhiteSpace($programFilesRoot)) {
      Add-PathIfMissing -List $programFilesRoots -PathValue $programFilesRoot
    }
  }

  $homeDrive = [System.IO.Path]::GetPathRoot($UserHome)
  if (-not [string]::IsNullOrWhiteSpace($homeDrive)) {
    $homeDrive = $homeDrive.TrimEnd("\")
    Add-PathIfMissing -List $programFilesRoots -PathValue (Join-Path $homeDrive "Program Files")
    Add-PathIfMissing -List $programFilesRoots -PathValue (Join-Path $homeDrive "Program Files (x86)")
  }

  Add-PathIfMissing -List $programFilesRoots -PathValue "C:\Program Files"
  Add-PathIfMissing -List $programFilesRoots -PathValue "C:\Program Files (x86)"

  foreach ($programFilesRoot in $programFilesRoots) {
    foreach ($candidate in @(
      (Join-Path $programFilesRoot "nodejs"),
      (Join-Path $programFilesRoot "Git\cmd"),
      (Join-Path $programFilesRoot "GitHub CLI")
    )) {
      if (Test-Path $candidate) {
        Add-PathIfMissing -List $results -PathValue $candidate
      }
    }
  }

  foreach ($candidate in @(
    (Join-Path $UserHome "AppData\Roaming\npm"),
    (Join-Path $UserHome "AppData\Local\Programs\Microsoft VS Code\bin"),
    (Join-Path $UserHome "AppData\Local\Programs\GitHub CLI"),
    (Join-Path $UserHome "AppData\Local\Programs\nodejs")
  )) {
    if (Test-Path $candidate) {
      Add-PathIfMissing -List $results -PathValue $candidate
    }
  }

  return $results
}

function Resolve-CommonCliDataDirectories {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $results = New-Object 'System.Collections.Generic.List[string]'

  foreach ($candidate in @(
    (Join-Path $UserHome "AppData\Roaming\GitHub CLI"),
    (Join-Path $UserHome "AppData\Roaming\npm"),
    (Join-Path $UserHome "AppData\Local\npm-cache"),
    (Join-Path $UserHome ".node-red"),
    (Join-Path $UserHome "AppData\Roaming\Code")
  )) {
    if (Test-Path $candidate) {
      Add-PathIfMissing -List $results -PathValue $candidate
    }
  }

  return $results
}

function New-WrapperBodyForExecutable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  return "@echo off`r`n`"$TargetPath`" %*`r`n"
}

function New-WrapperBodyForBatchScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  return "@echo off`r`ncall `"$TargetPath`" %*`r`n"
}

function New-WrapperBodyForPowerShellScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  return "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"$TargetPath`" %*`r`n"
}

function New-WrapperBodyForVsCodeCli {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
  )

  return @"
@echo off
setlocal
set "_stderr_file=%TEMP%\codex-code-stderr-%RANDOM%%RANDOM%.log"
set "_debug_log=%CD%\debug.log"
set "_debug_log_preexisting="
if exist "%_debug_log%" set "_debug_log_preexisting=1"
call "$TargetPath" %* 2>"%_stderr_file%"
set "_exit=%ERRORLEVEL%"
if exist "%_stderr_file%" (
  if "%_exit%"=="0" (
    findstr /v /c:"registration_protocol_win.cc:108] CreateFile:" "%_stderr_file%" 1>&2
  ) else (
    type "%_stderr_file%" 1>&2
  )
  del /q "%_stderr_file%" >nul 2>&1
)
if not defined _debug_log_preexisting (
  if exist "%_debug_log%" del /q "%_debug_log%" >nul 2>&1
  start "" /b cmd /d /c "ping 127.0.0.1 -n 3 >nul & del /q ""%_debug_log%"" >nul 2>&1"
)
exit /b %_exit%
"@ -replace "`n", "`r`n"
}

function New-WrapperBodyForScript {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$ScriptPath
  )

  return "@echo off`r`n`"$PythonExe`" `"$ScriptPath`" %*`r`n"
}

function Write-WrapperFile {
  param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationPath,
    [Parameter(Mandatory = $true)]
    [string]$Content
  )

  if ($DryRun) {
    Write-Host "DRYRUN write wrapper $DestinationPath"
    return
  }

  $existingContent = $null
  if (Test-Path $DestinationPath) {
    try {
      $existingContent = Get-Content $DestinationPath -Raw -ErrorAction Stop
    } catch {
    }
  }

  if ($existingContent -ceq $Content) {
    return
  }

  Set-Content -Path $DestinationPath -Value $Content -Encoding Ascii
}

function Get-WrapperExtensionRank {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Extension
  )

  switch ($Extension.ToLowerInvariant()) {
    ".exe" { return 0 }
    ".cmd" { return 1 }
    ".bat" { return 2 }
    ".ps1" { return 3 }
    default { return 99 }
  }
}

function Sync-PythonToolWrappers {
  param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationDirectory,
    [Parameter(Mandatory = $true)]
    [string]$PreferredPythonRoot,
    [Parameter(Mandatory = $true)]
    [string]$PythonCommandHost,
    [Parameter(Mandatory = $true)]
    [AllowEmptyCollection()]
    [System.Collections.Generic.List[string]]$SourceDirectories
  )

  $createdNames = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)

  foreach ($pipName in @("pip", "pip3", "pip3.14", "pip3.14-64")) {
    $destinationPath = Join-Path $DestinationDirectory ($pipName + ".cmd")
    $content = "@echo off`r`n`"$PythonCommandHost`" -m pip %*`r`n"
    $psDestinationPath = Join-Path $DestinationDirectory ($pipName + ".ps1")
    $psContent = New-PowerShellWrapperBodyForPythonModule -PythonExe $PythonCommandHost -ModuleName "pip"
    $createdNames.Add($pipName) | Out-Null
    Write-WrapperFile -DestinationPath $destinationPath -Content $content
    Write-WrapperFile -DestinationPath $psDestinationPath -Content $psContent
  }

  foreach ($sourceDirectory in $SourceDirectories) {
    $files = Get-ChildItem $sourceDirectory -File -ErrorAction SilentlyContinue |
      Where-Object {
        $_.Extension -in @(".exe", ".py", ".pyw") -and
        $_.Name -notlike "*.exe.__target__" -and
        $_.Name -notlike "*.dll"
      } |
      Sort-Object Name

    foreach ($file in $files) {
      $toolName = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
      if ([string]::IsNullOrWhiteSpace($toolName)) {
        continue
      }

      if ($createdNames.Contains($toolName)) {
        continue
      }

      $destinationPath = Join-Path $DestinationDirectory ($toolName + ".cmd")
      $content = $null

      if ($file.Extension -eq ".exe") {
        $content = New-WrapperBodyForExecutable -TargetPath $file.FullName
        $psContent = New-PowerShellWrapperBodyForExecutable -TargetPath $file.FullName
      } else {
        $content = New-WrapperBodyForScript -PythonExe $PythonCommandHost -ScriptPath $file.FullName
        $psContent = New-PowerShellWrapperBodyForPythonScript -PythonExe $PythonCommandHost -ScriptPath $file.FullName
      }

      $createdNames.Add($toolName) | Out-Null
      Write-WrapperFile -DestinationPath $destinationPath -Content $content
      Write-WrapperFile -DestinationPath (Join-Path $DestinationDirectory ($toolName + ".ps1")) -Content $psContent
    }
  }

  return $createdNames.Count
}

function Sync-CommonCliWrappers {
  param(
    [Parameter(Mandatory = $true)]
    [string]$DestinationDirectory,
    [Parameter(Mandatory = $true)]
    [AllowEmptyCollection()]
    [System.Collections.Generic.List[string]]$SourceDirectories
  )

  $createdNames = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)

  foreach ($sourceDirectory in $SourceDirectories) {
    $groups = Get-ChildItem $sourceDirectory -File -ErrorAction SilentlyContinue |
      Where-Object { $_.Extension -in @(".exe", ".cmd", ".bat", ".ps1") } |
      Group-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Name) } |
      Sort-Object Name

    foreach ($group in $groups) {
      $toolName = $group.Name
      if ([string]::IsNullOrWhiteSpace($toolName)) {
        continue
      }

      if ($toolName -ieq "codex") {
        continue
      }

      if ($createdNames.Contains($toolName)) {
        continue
      }

      $selectedFile = $group.Group |
        Sort-Object @{ Expression = { Get-WrapperExtensionRank -Extension $_.Extension } }, Name |
        Select-Object -First 1

      if ($null -eq $selectedFile) {
        continue
      }

      $destinationPath = Join-Path $DestinationDirectory ($toolName + ".cmd")
      $content = $null

      if ($toolName -ieq "code" -and $selectedFile.Extension.ToLowerInvariant() -eq ".cmd") {
        $content = New-WrapperBodyForVsCodeCli -TargetPath $selectedFile.FullName
        $psContent = New-PowerShellWrapperBodyForBatchScript -TargetPath $selectedFile.FullName
      } else {
        switch ($selectedFile.Extension.ToLowerInvariant()) {
          ".exe" {
            $content = New-WrapperBodyForExecutable -TargetPath $selectedFile.FullName
            $psContent = New-PowerShellWrapperBodyForExecutable -TargetPath $selectedFile.FullName
            break
          }
          ".cmd" {
            $content = New-WrapperBodyForBatchScript -TargetPath $selectedFile.FullName
            $psContent = New-PowerShellWrapperBodyForBatchScript -TargetPath $selectedFile.FullName
            break
          }
          ".bat" {
            $content = New-WrapperBodyForBatchScript -TargetPath $selectedFile.FullName
            $psContent = New-PowerShellWrapperBodyForBatchScript -TargetPath $selectedFile.FullName
            break
          }
          ".ps1" {
            $content = New-WrapperBodyForPowerShellScript -TargetPath $selectedFile.FullName
            $psContent = New-PowerShellWrapperBodyForPowerShellScript -TargetPath $selectedFile.FullName
            break
          }
        }
      }

      if ($null -eq $content) {
        continue
      }

      $createdNames.Add($toolName) | Out-Null
      Write-WrapperFile -DestinationPath $destinationPath -Content $content
      Write-WrapperFile -DestinationPath (Join-Path $DestinationDirectory ($toolName + ".ps1")) -Content $psContent
    }
  }

  return $createdNames.Count
}

function Get-CodexArg0Directories {
  param(
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $directories = New-Object 'System.Collections.Generic.List[string]'
  $arg0Root = Join-Path $UserHome ".codex\tmp\arg0"

  if (-not (Test-Path $arg0Root)) {
    return $directories
  }

  $arg0Children = Get-ChildItem $arg0Root -Directory -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc -Descending

  foreach ($child in $arg0Children) {
    $directories.Add($child.FullName) | Out-Null
  }

  return $directories
}

function Sync-WrappersToCodexArg0 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDirectory,
    [Parameter(Mandatory = $true)]
    [string]$UserHome
  )

  $arg0Directories = Get-CodexArg0Directories -UserHome $UserHome
  if ($arg0Directories.Count -eq 0) {
    return 0
  }

  $wrapperFiles = Get-ChildItem $SourceDirectory -Filter *.cmd -File -ErrorAction SilentlyContinue |
    Sort-Object Name

  if ($null -eq $wrapperFiles -or $wrapperFiles.Count -eq 0) {
    return 0
  }

  $syncedDirectoryCount = 0
  foreach ($arg0Directory in $arg0Directories) {
    if (-not (Test-Path $arg0Directory)) {
      continue
    }

    foreach ($wrapperFile in $wrapperFiles) {
      $destinationPath = Join-Path $arg0Directory $wrapperFile.Name
      Sync-FileIfNeeded -SourcePath $wrapperFile.FullName -DestinationPath $destinationPath
    }

    $syncedDirectoryCount++
  }

  return $syncedDirectoryCount
}

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$resolvedUserHome = Resolve-DefaultUserHome -RepoRoot $repoRoot

if ([string]::IsNullOrWhiteSpace($UserHome)) {
  $UserHome = $resolvedUserHome
} else {
  $candidateUserHome = [System.IO.Path]::GetFullPath($UserHome)

  if (-not $AllowCustomUserHome -and -not (Test-LikelyUserHome -PathValue $candidateUserHome)) {
    if (Test-Path $candidateUserHome) {
      Write-Warning "检测到 -UserHome 指向的不是用户主目录，已按额外授权目录处理: $candidateUserHome"
      $ExtraPaths = @($ExtraPaths + $candidateUserHome)
    } else {
      Write-Warning "检测到 -UserHome 指向的目录不存在且不像用户主目录，已回退到当前用户目录: $candidateUserHome"
    }

    $UserHome = $resolvedUserHome
  } else {
    $UserHome = $candidateUserHome
  }
}

$targets = New-Object 'System.Collections.Generic.List[string]'
Add-PathIfMissing -List $targets -PathValue $repoRoot

$codexRoot = Join-Path $UserHome ".codex"
$stableCodexBin = Join-Path $UserHome "AppData\Local\OpenAI\Codex\bin"
$stablePythonToolDir = Join-Path $UserHome "AppData\Local\OpenAI\Codex\python-tools"
$stableCliToolDir = Join-Path $UserHome "AppData\Local\OpenAI\Codex\cli-tools"
$preferredPythonRoot = Resolve-PreferredPythonRoot -UserHome $UserHome
$pythonCommandHost = if (-not [string]::IsNullOrWhiteSpace($preferredPythonRoot)) { Resolve-PythonCommandHost -PreferredPythonRoot $preferredPythonRoot -UserHome $UserHome } else { $null }
$pythonToolSourceDirectories = if (-not [string]::IsNullOrWhiteSpace($preferredPythonRoot) -and -not [string]::IsNullOrWhiteSpace($pythonCommandHost)) { Resolve-PythonToolSourceDirectories -UserHome $UserHome -PreferredPythonRoot $preferredPythonRoot } else { New-Object 'System.Collections.Generic.List[string]' }
$commonCliSourceDirectories = Resolve-CommonCliSourceDirectories -UserHome $UserHome
$commonCliDataDirectories = Resolve-CommonCliDataDirectories -UserHome $UserHome
$codexDirs = @(
  $codexRoot,
  (Join-Path $codexRoot "scripts"),
  (Join-Path $codexRoot "logs"),
  $stableCodexBin,
  $stablePythonToolDir,
  $stableCliToolDir
)

foreach ($dir in $codexDirs) {
  Ensure-DirectoryIfNeeded -PathValue $dir
  Add-PathIfMissing -List $targets -PathValue $dir
}

foreach ($dir in $commonCliDataDirectories) {
  Add-PathIfMissing -List $targets -PathValue $dir
}

if (-not $SkipToolchainCaches) {
  $toolchainDirs = @(
    (Join-Path $UserHome "AppData\Local\Arm\Packs"),
    (Join-Path $UserHome "AppData\Local\Arm\Packs\ARM"),
    (Join-Path $UserHome "AppData\Local\Arm\Packs\Keil")
  )

  foreach ($dir in $toolchainDirs) {
    if (Test-Path $dir) {
      Add-PathIfMissing -List $targets -PathValue $dir
    }
  }
}

foreach ($pathItem in $ExtraPaths) {
  Ensure-DirectoryIfNeeded -PathValue $pathItem
  Add-PathIfMissing -List $targets -PathValue $pathItem
}

foreach ($pythonPath in @(
  (Join-Path $UserHome "AppData\Local\Python"),
  (Join-Path $UserHome "AppData\Local\Programs\Python")
)) {
  if (Test-Path $pythonPath) {
    Add-PathIfMissing -List $targets -PathValue $pythonPath
  }
}

$principals = New-Object 'System.Collections.Generic.List[string]'
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principals.Add($currentIdentity) | Out-Null

if (-not $SkipGroupGrant) {
  $computerName = $env:COMPUTERNAME
  if ([string]::IsNullOrWhiteSpace($computerName)) {
    $computerName = [Environment]::MachineName
  }

  $codexGroup = "$computerName\CodexSandboxUsers"

  try {
    $null = (New-Object System.Security.Principal.NTAccount($codexGroup)).Translate([System.Security.Principal.SecurityIdentifier])
    $principals.Add($codexGroup) | Out-Null
  } catch {
    Write-Host "未检测到组 $codexGroup，跳过组授权。"
  }
}

Write-Host "User home : $UserHome"
Write-Host "Repo root : $repoRoot"
Write-Host "Principals:"
$principals | ForEach-Object { Write-Host "  - $_" }
Write-Host ""
Write-Host "Targets:"
$targets | ForEach-Object {
  $existsMark = if (Test-Path $_) { "exists" } else { "missing" }
  Write-Host "  - $_ [$existsMark]"
}

if ($SkipAclGrants) {
  Write-Host ""
  Write-Host "已按参数跳过 ACL 授权步骤。"
} else {
  foreach ($targetPath in $targets) {
    if (-not (Test-Path $targetPath)) {
      Write-Host "跳过不存在目录: $targetPath"
      continue
    }

    foreach ($principal in $principals) {
      $grantRule = "${principal}:(OI)(CI)F"

      if ($DryRun) {
        Write-Host "DRYRUN icacls `"$targetPath`" /grant:r `"$grantRule`" /T /C"
        continue
      }

      Write-Host ""
      Write-Host "Granting FullControl:"
      Write-Host "  Path      : $targetPath"
      Write-Host "  Principal : $principal"

      & icacls $targetPath /grant:r $grantRule /T /C

      if ($LASTEXITCODE -ne 0) {
        throw "icacls 执行失败: $targetPath -> $principal"
      }
    }
  }
}

$codexEnvironmentDefaults = $null
$windowsRoot = $env:SystemRoot
if ([string]::IsNullOrWhiteSpace($windowsRoot) -or -not (Test-Path $windowsRoot)) {
  $candidateSystemDrive = if (-not [string]::IsNullOrWhiteSpace($env:SystemDrive)) { $env:SystemDrive } else { "C:" }
  $candidateWindowsRoot = Join-Path $candidateSystemDrive "Windows"
  if (Test-Path $candidateWindowsRoot) {
    $windowsRoot = $candidateWindowsRoot
  } elseif (Test-Path "C:\Windows") {
    $windowsRoot = "C:\Windows"
  }
}

if (-not [string]::IsNullOrWhiteSpace($windowsRoot) -and (Test-Path $windowsRoot)) {
  $codexEnvironmentDefaults = Get-CodexEnvironmentDefaults -UserHome $UserHome -WindowsRoot $windowsRoot
  $script:CodexWrapperEnvironmentPrelude = New-PowerShellEnvironmentPrelude -EnvironmentValues $codexEnvironmentDefaults

  Write-Host ""
  Write-Host "Windows / Codex 基础环境变量:"
  foreach ($envName in $codexEnvironmentDefaults.Keys) {
    Set-UserEnvironmentValue -Name $envName -Value ([string]$codexEnvironmentDefaults[$envName])
  }
} else {
  Write-Warning "未能定位 Windows 目录，已跳过 SystemRoot/windir/ComSpec 设置。"
  $script:CodexWrapperEnvironmentPrelude = "`$ErrorActionPreference = 'Stop'`r`n"
}

$codexHelperSource = Resolve-CodexHelperSource -UserHome $UserHome
$codexHelperNames = @(
  "codex.exe",
  "rg.exe",
  "codex-command-runner.exe",
  "codex-windows-sandbox-setup.exe"
)

Write-Host ""
Write-Host "Codex helper 同步:"
Write-Host "  Stable bin : $stableCodexBin"
Write-Host "  Source     : $(if ($null -ne $codexHelperSource) { $codexHelperSource } else { '未找到' })"

if ($null -ne $codexHelperSource) {
  foreach ($helperName in $codexHelperNames) {
    Sync-FileIfNeeded -SourcePath (Join-Path $codexHelperSource $helperName) -DestinationPath (Join-Path $stableCodexBin $helperName)
  }
} else {
  Write-Warning "未找到 Codex helper 源目录，已跳过 helper 同步。"
}

$stableCodexExe = Join-Path $stableCodexBin "codex.exe"
Ensure-UserPathContains -PathValue $stableCodexBin -Prepend
Set-UserEnvironmentValue -Name "CODEX_CLI_PATH" -Value $stableCodexExe

if ($null -ne $codexEnvironmentDefaults) {
  $profileEnvironmentDefaults = [ordered]@{}
  foreach ($envName in $codexEnvironmentDefaults.Keys) {
    $profileEnvironmentDefaults[$envName] = [string]$codexEnvironmentDefaults[$envName]
  }
  $profileEnvironmentDefaults["CODEX_CLI_PATH"] = $stableCodexExe

  Install-PowerShellProfileBootstrap `
    -UserHome $UserHome `
    -EnvironmentValues $profileEnvironmentDefaults `
    -StableDirectories @($stableCliToolDir, $stablePythonToolDir, $stableCodexBin)
}

$staleCodexCliWrapper = Join-Path $stableCliToolDir "codex.cmd"
if (Test-Path $staleCodexCliWrapper) {
  if ($DryRun) {
    Write-Host "DRYRUN Remove-Item `"$staleCodexCliWrapper`" -Force"
  } else {
    Remove-Item -LiteralPath $staleCodexCliWrapper -Force
  }
}

Write-Host ""
Write-Host "Python tool wrapper 同步:"
Write-Host "  Stable dir : $stablePythonToolDir"
Write-Host "  Python root: $(if (-not [string]::IsNullOrWhiteSpace($preferredPythonRoot)) { $preferredPythonRoot } else { '未找到' })"
Write-Host "  Host exe   : $(if (-not [string]::IsNullOrWhiteSpace($pythonCommandHost)) { $pythonCommandHost } else { '未找到' })"

if (-not [string]::IsNullOrWhiteSpace($preferredPythonRoot) -and -not [string]::IsNullOrWhiteSpace($pythonCommandHost)) {
  $wrapperCount = Sync-PythonToolWrappers -DestinationDirectory $stablePythonToolDir -PreferredPythonRoot $preferredPythonRoot -PythonCommandHost $pythonCommandHost -SourceDirectories $pythonToolSourceDirectories
  $arg0DirectoryCount = if ($SyncArg0Wrappers) { Sync-WrappersToCodexArg0 -SourceDirectory $stablePythonToolDir -UserHome $UserHome } else { 0 }
  $arg0PowerShellDirectoryCount = if (-not $SkipArg0PowerShellWrappers) { Sync-PowerShellWrappersToCodexArg0 -SourceDirectory $stablePythonToolDir -UserHome $UserHome } else { 0 }
  Write-Host "  Wrapper 数 : $wrapperCount"
  Write-Host "  Arg0 .cmd 同步 : $(if ($SyncArg0Wrappers) { $arg0DirectoryCount } else { '跳过，避免当前 Codex 线程优先命中 .cmd wrapper' })"
  Write-Host "  Arg0 .ps1 同步 : $(if (-not $SkipArg0PowerShellWrappers) { $arg0PowerShellDirectoryCount } else { '跳过' })"
  Ensure-UserPathContains -PathValue $stablePythonToolDir -Prepend
} else {
  Write-Warning "未找到可用的 Python 安装，已跳过 Python wrapper 同步。"
}

Write-Host ""
Write-Host "Common CLI wrapper 同步:"
Write-Host "  Stable dir : $stableCliToolDir"
Write-Host "  Source dirs:"
if ($commonCliSourceDirectories.Count -gt 0) {
  $commonCliSourceDirectories | ForEach-Object { Write-Host "    - $_" }
  $commonCliWrapperCount = Sync-CommonCliWrappers -DestinationDirectory $stableCliToolDir -SourceDirectories $commonCliSourceDirectories
  $commonCliArg0Count = if ($SyncArg0Wrappers) { Sync-WrappersToCodexArg0 -SourceDirectory $stableCliToolDir -UserHome $UserHome } else { 0 }
  $commonCliArg0PowerShellCount = if (-not $SkipArg0PowerShellWrappers) { Sync-PowerShellWrappersToCodexArg0 -SourceDirectory $stableCliToolDir -UserHome $UserHome } else { 0 }
  Write-Host "  Wrapper 数 : $commonCliWrapperCount"
  Write-Host "  Arg0 .cmd 同步 : $(if ($SyncArg0Wrappers) { $commonCliArg0Count } else { '跳过，避免当前 Codex 线程优先命中 .cmd wrapper' })"
  Write-Host "  Arg0 .ps1 同步 : $(if (-not $SkipArg0PowerShellWrappers) { $commonCliArg0PowerShellCount } else { '跳过' })"
  Ensure-UserPathContains -PathValue $stableCliToolDir -Prepend
} else {
  Write-Warning "未找到常用 CLI 源目录，已跳过 common CLI wrapper 同步。"
}

Broadcast-EnvironmentChange

Write-Host ""
Write-Host "环境检查:"
$uv4 = Get-Command UV4.exe -ErrorAction SilentlyContinue
if ($null -ne $uv4) {
  Write-Host "  UV4.exe : $($uv4.Source)"
} else {
  Write-Host "  UV4.exe : 未在 PATH 中找到"
}

$rg = Get-Command rg.exe -ErrorAction SilentlyContinue
if ($null -ne $rg) {
  Write-Host "  rg.exe  : $($rg.Source)"
} else {
  Write-Host "  rg.exe  : 未在 PATH 中找到"
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($null -ne $python) {
  Write-Host "  python  : $($python.Source)"
} else {
  Write-Host "  python  : 未在 PATH 中找到"
}

$pip = Get-Command pip.exe -ErrorAction SilentlyContinue
if ($null -ne $pip) {
  Write-Host "  pip     : $($pip.Source)"
} else {
  Write-Host "  pip     : 未在 PATH 中找到"
}

foreach ($commandName in @("node", "npm", "npx", "corepack", "git", "gh", "code")) {
  $resolvedCommand = Get-Command $commandName -ErrorAction SilentlyContinue
  if ($null -ne $resolvedCommand) {
    Write-Host ("  {0,-7} : {1}" -f $commandName, $resolvedCommand.Source)
  } else {
    Write-Host ("  {0,-7} : 未在 PATH 中找到" -f $commandName)
  }
}

Write-Host "  CODEX_CLI_PATH : $([Environment]::GetEnvironmentVariable('CODEX_CLI_PATH', 'User'))"

Write-Host ""
Write-Host "完成。"
