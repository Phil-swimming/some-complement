[CmdletBinding()]
param(
  [string]$ProjectPath = "MDK-ARM\LED_2.uvprojx",
  [ValidateSet("Build", "Rebuild")]
  [string]$Mode = "Build",
  [string]$Target = "",
  [string]$Uv4Path = "C:\Keil_v5\UV4\UV4.exe"
)

$ErrorActionPreference = "Stop"

function Resolve-NormalizedPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PathValue
  )

  return [System.IO.Path]::GetFullPath((Resolve-Path $PathValue).Path)
}

function Get-ProjectOutputInfo {
  param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectFullPath
  )

  [xml]$projXml = Get-Content -Path $ProjectFullPath
  $target = $projXml.Project.Targets.Target | Select-Object -First 1
  if ($null -eq $target) {
    throw "未能从工程文件解析 Target 信息: $ProjectFullPath"
  }

  $targetName = [string]$target.TargetName
  $outputDirectory = [string]$target.TargetOption.TargetCommonOption.OutputDirectory
  $outputName = [string]$target.TargetOption.TargetCommonOption.OutputName

  if ([string]::IsNullOrWhiteSpace($outputDirectory)) {
    $outputDirectory = "$targetName\"
  }
  if ([string]::IsNullOrWhiteSpace($outputName)) {
    $outputName = $targetName
  }

  [pscustomobject]@{
    TargetName = $targetName
    OutputDirectory = $outputDirectory
    OutputName = $outputName
  }
}

function Get-BuildSummaryFromLog {
  param(
    [Parameter(Mandatory = $true)]
    [string]$BuildLogPath
  )

  if (-not (Test-Path $BuildLogPath)) {
    return $null
  }

  $content = Get-Content -Path $BuildLogPath -Raw
  $match = [regex]::Match($content, '"[^"]+" - (?<errors>\d+) Error\(s\), (?<warnings>\d+) Warning\(s\)\.')
  if (-not $match.Success) {
    return $null
  }

  return [pscustomobject]@{
    Errors = [int]$match.Groups["errors"].Value
    Warnings = [int]$match.Groups["warnings"].Value
  }
}

function Quote-ForCmd {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  if ($Value.Contains(' ')) {
    return '"' + $Value + '"'
  }

  return $Value
}

function Test-DirectoryReadable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PathValue
  )

  if (-not (Test-Path $PathValue)) {
    return $false
  }

  try {
    Get-ChildItem -Path $PathValue -ErrorAction Stop | Select-Object -First 1 | Out-Null
    return $true
  } catch {
    return $false
  }
}

$projectFullPath = Resolve-NormalizedPath -PathValue $ProjectPath
$projectInfo = Get-ProjectOutputInfo -ProjectFullPath $projectFullPath

if (-not (Test-Path $Uv4Path)) {
  throw "未找到 UV4.exe: $Uv4Path"
}

$userHome = $env:USERPROFILE
$packChecks = @(
  @{ Name = "ARM::CMSIS"; Path = (Join-Path $userHome "AppData\Local\Arm\Packs\ARM\CMSIS") },
  @{ Name = "Keil::STM32H7xx_DFP"; Path = (Join-Path $userHome "AppData\Local\Arm\Packs\Keil\STM32H7xx_DFP") }
)

foreach ($packCheck in $packChecks) {
  if (-not (Test-DirectoryReadable -PathValue $packCheck.Path)) {
    Write-Warning ("无法读取 Pack 目录: {0} ({1})。如果后续出现 RTE / pack is not installed 错误，先运行 .\\tools\\setup_codex_windows_access.ps1。" -f $packCheck.Name, $packCheck.Path)
  }
}

$uv4Args = @()

if ($Mode -eq "Rebuild") {
  $uv4Args += "-r"
} else {
  $uv4Args += "-b"
}

$uv4Args += $projectFullPath

if (-not [string]::IsNullOrWhiteSpace($Target)) {
  $uv4Args += "-t"
  $uv4Args += $Target
}

$uv4Args += "-j0"

$cmdLine = (Quote-ForCmd -Value $Uv4Path) + " " + (($uv4Args | ForEach-Object { Quote-ForCmd -Value $_ }) -join " ")

Write-Host "Project : $projectFullPath"
Write-Host "UV4     : $Uv4Path"
Write-Host "Mode    : $Mode"
if (-not [string]::IsNullOrWhiteSpace($Target)) {
  Write-Host "Target  : $Target"
}
Write-Host "Command : $cmdLine"

Push-Location (Split-Path $projectFullPath -Parent)
try {
  & cmd.exe /d /c $cmdLine
  $exitCode = $LASTEXITCODE
} finally {
  Pop-Location
}

if ([string]::IsNullOrWhiteSpace($Target)) {
  $outputDir = Join-Path (Split-Path $projectFullPath -Parent) $projectInfo.OutputDirectory
  $buildLogPath = Join-Path $outputDir ($projectInfo.OutputName + ".build_log.htm")
} else {
  $outputDir = $null
  $buildLogPath = $null
}

if ($exitCode -ne 0) {
  $buildSummary = $null
  if ($buildLogPath) {
    $buildSummary = Get-BuildSummaryFromLog -BuildLogPath $buildLogPath
  }

  if (($buildSummary -ne $null) -and ($buildSummary.Errors -eq 0)) {
    Write-Warning ("Keil 返回退出码 {0}，但构建日志显示 0 Error(s), {1} Warning(s)。按 warnings-only 处理。" -f $exitCode, $buildSummary.Warnings)
  } else {
    throw "Keil 构建失败，退出码: $exitCode"
  }
}

if (Test-Path $outputDir) {
  Write-Host ""
  Write-Host "关键产物:"
  Get-ChildItem $outputDir -File |
    Where-Object { $_.Name -in @(
      ($projectInfo.OutputName + ".axf"),
      ($projectInfo.OutputName + ".hex"),
      ($projectInfo.OutputName + ".map"),
      ($projectInfo.OutputName + ".build_log.htm")
    ) } |
    Sort-Object Name |
    Format-Table Name, Length, LastWriteTime -AutoSize
}
