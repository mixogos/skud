# Публикация новой версии на GitHub Releases.
# 1) берёт номер версии из backend/app/version.py
# 2) собирает приложение и установщик seture.exe (packaging\build.ps1)
# 3) создаёт тег vX.Y.Z и релиз на GitHub с приложенным seture.exe
#
# Запуск из корня проекта:  powershell -ExecutionPolicy Bypass -File packaging\release.ps1
# Перед релизом увеличьте номер в backend\app\version.py и запомните изменения в git.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Версия из version.py
$verLine = Select-String -Path "backend\app\version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $verLine) { throw "Не нашёл версию в backend\app\version.py" }
$version = $verLine.Matches[0].Groups[1].Value
$tag = "v$version"
Write-Host "Версия: $version  (тег $tag)" -ForegroundColor Cyan

if (git tag --list $tag) { throw "Тег $tag уже существует — увеличьте версию в version.py" }

# Сборка приложения и установщика
& (Join-Path $PSScriptRoot "build.ps1")
if (-not (Test-Path "dist\seture.exe")) { throw "Нет dist\seture.exe — установщик не собрался (нужен Inno Setup)" }

# Тег и релиз
git tag $tag
git push origin $tag
gh release create $tag "dist\seture.exe" --title "СКУД $version" --notes "Версия $version. Установите поверх текущей — данные сохранятся."
Write-Host "Релиз $tag опубликован. Установленные копии предложат обновиться." -ForegroundColor Green
