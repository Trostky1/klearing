# Build the Windows distributable. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File build\build_windows.ps1
# Output: dist\Klearing\ (folder with Klearing.exe) and dist\Klearing-windows.zip

$ErrorActionPreference = "Stop"

if (-not (Test-Path "venv")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.12 -m venv venv
    } else {
        python -m venv venv
    }
}
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\pip.exe install -r requirements.txt pyinstaller

venv\Scripts\pyinstaller.exe build\klearing.spec --noconfirm --distpath dist --workpath build\work

Compress-Archive -Path dist\Klearing -DestinationPath dist\Klearing-windows.zip -Force
Write-Host ""
Write-Host "Done: dist\Klearing\Klearing.exe  (zipped: dist\Klearing-windows.zip)"
Write-Host "Reminder: unsigned builds trip SmartScreen - see docs\FIRST-LAUNCH.md"
