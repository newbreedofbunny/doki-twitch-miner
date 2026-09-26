# Builds dist\DokiTwitchMiner.exe - a single file, no console window - with PyInstaller.
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# The fonts are deliberately NOT bundled (their licences don't allow redistribution);
# the app downloads them on first launch.

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
& .venv-build\Scripts\python.exe -m pip install --quiet --upgrade pip pyinstaller

$assets = 'monika.png', 'monika_glow.png', 'icon_256.png', 'icon_32.png', 'icon.png', 'icon.ico'
$addData = foreach ($a in $assets) { '--add-data'; "assets\$a;assets" }

& .venv-build\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed `
    --name DokiTwitchMiner --icon assets\icon.ico @addData twitch_drop_claimer.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

"Built: $(Resolve-Path dist\DokiTwitchMiner.exe)"
