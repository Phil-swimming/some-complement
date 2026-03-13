# Run cppcheck on important source folders
param(
  [string]$Out = "cppcheck-report.xml"
)

# Require cppcheck to be installed and in PATH
$folders = @("App", "Core", "USB_HOST")
$inc = @("Drivers/CMSIS/Include", "Drivers/STM32H7xx_HAL_Driver/Inc")

$incArgs = $inc | ForEach-Object { "-I `"$_`"" } | Out-String
$foldersArg = $folders -join ' '

Write-Host "Running cppcheck on: $foldersArg"
cppcheck --enable=warning,performance,portability --inconclusive --xml --xml-version=2 $foldersArg 2> $Out
Write-Host "Report written to $Out"
