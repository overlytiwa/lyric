$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python -m pip install -r requirements.txt pyinstaller==6.21.0
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$extensionSource = (Resolve-Path ..\extension).Path + ';extension'
python -m PyInstaller --noconfirm --onefile --console --add-data $extensionSource --name lyric --distpath .. --workpath build --specpath build lyric.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Copy-Item -LiteralPath ..\lyric.exe -Destination ..\Lyric-Setup.exe -Force
Write-Host 'Built lyric.exe and Lyric-Setup.exe in the parent folder.'

