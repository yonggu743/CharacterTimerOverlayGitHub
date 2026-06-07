Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

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

Copy-Item `
  -LiteralPath tools\character_timers_db.json `
  -Destination dist\CharacterTimerOverlay\character_timers_db.json `
  -Force

Write-Host "Build complete: dist\CharacterTimerOverlay\CharacterTimerOverlay.exe"
