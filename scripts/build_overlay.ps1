Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$RuntimeDb = Join-Path $Root "dist\CharacterTimerOverlay\character_timers_db.json"
$DbBackup = Join-Path $env:TEMP "CharacterTimerOverlay.character_timers_db.json"
$HadRuntimeDb = Test-Path -LiteralPath $RuntimeDb
if ($HadRuntimeDb) {
  Copy-Item -LiteralPath $RuntimeDb -Destination $DbBackup -Force
}

python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --uac-admin `
  --name CharacterTimerOverlay `
  --paths tools `
  --collect-all rapidocr_onnxruntime `
  --collect-all onnxruntime `
  --collect-all cv2 `
  --hidden-import keyboard `
  tools\character_timer_overlay.py

if ($HadRuntimeDb) {
  Copy-Item -LiteralPath $DbBackup -Destination $RuntimeDb -Force
  Remove-Item -LiteralPath $DbBackup -Force
  Write-Host "Preserved existing database: $RuntimeDb"
} elseif (-not (Test-Path -LiteralPath $RuntimeDb)) {
  Copy-Item `
    -LiteralPath tools\character_timers_db.json `
    -Destination $RuntimeDb `
    -Force
  Write-Host "Created initial database: $RuntimeDb"
}

Write-Host "Build complete: dist\CharacterTimerOverlay\CharacterTimerOverlay.exe"
