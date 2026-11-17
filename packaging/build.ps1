# Сборка СКУД: сначала собирает веб-интерфейс, потом автономное приложение, потом установщик.
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root "backend\.venv\Scripts\python.exe"

Write-Host "== 1/3 Сборка веб-интерфейса ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "frontend")
if (-not (Test-Path "node_modules")) { npm install }
npm run build
Pop-Location

Write-Host "== 2/3 Сборка приложения (PyInstaller) ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean (Join-Path $root "packaging\skud.spec")
if (-not (Test-Path (Join-Path $root "dist\SKUD\SKUD.exe"))) { throw "Приложение не собралось: нет dist\SKUD\SKUD.exe" }
Write-Host "Готово: dist\SKUD\SKUD.exe" -ForegroundColor Green

Write-Host "== 3/3 Установщик (Inno Setup) ==" -ForegroundColor Cyan
$iscc = @(
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  & $iscc (Join-Path $root "packaging\installer.iss")
  Write-Host "Готово: dist\seture.exe" -ForegroundColor Green
} else {
  Write-Host "Inno Setup не установлен — установщик seture.exe не собран." -ForegroundColor Yellow
  Write-Host "Установите его: winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
  Write-Host "затем: & '${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe' packaging\installer.iss" -ForegroundColor Yellow
  Write-Host "Само приложение уже готово — можно запускать dist\SKUD\SKUD.exe" -ForegroundColor Green
}
