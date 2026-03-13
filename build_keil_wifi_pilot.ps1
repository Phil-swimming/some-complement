[CmdletBinding()]
param(
  [ValidateSet("Build", "Rebuild")]
  [string]$Mode = "Build"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

& (Join-Path $scriptDir "build_keil.ps1") `
  -ProjectPath (Join-Path $repoRoot "MDK-ARM\LED_2_wifi_pilot.uvprojx") `
  -Mode $Mode
