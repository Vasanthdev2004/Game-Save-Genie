# Build the standalone gsg.exe with PyInstaller.
# Run from the repo root: powershell -File packaging\build_exe.ps1
# Output: dist\gsg.exe (single file, no Python required on the target machine).

$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m pip install --upgrade pyinstaller | Out-Null

& $python -m PyInstaller `
    --onefile `
    --console `
    --clean `
    --name gsg `
    --distpath dist `
    --workpath build `
    --specpath build `
    packaging\gsg_entry.py

if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

Write-Output ""
Write-Output "Built dist\gsg.exe"
& ".\dist\gsg.exe" --version
