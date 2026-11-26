# One-time setup on Windows (PowerShell). Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File autonomous-dev\scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r autonomous-dev\requirements.txt -r dashboard\requirements.txt
# Target-application runtime dependencies for the bundled samples (your own project brings its own).
& $py -m pip install -r autonomous-dev\sample_apps\flask-inventory\requirements.txt -r autonomous-dev\sample_apps\pyside-notes\requirements.txt
& $py -m playwright install chromium

if (-not (Test-Path "autonomous-dev\.env")) { Copy-Item "autonomous-dev\.env.example" "autonomous-dev\.env"; Write-Host "Created autonomous-dev\.env - fill in Telegram settings (optional)." }

Push-Location dashboard\frontend
npm install --no-fund --no-audit
npm run build
Pop-Location

Push-Location autonomous-dev
& $py scripts\create_sample_workspace.py
& $py main.py doctor
Pop-Location

Write-Host "`nSetup complete. Next steps:"
Write-Host "  1. cd dashboard; ..\.venv\Scripts\python.exe -m backend.manage set-password"
Write-Host "  2. ..\.venv\Scripts\python.exe -m backend.app        (dashboard at http://127.0.0.1:8000)"
Write-Host "  3. cd ..\autonomous-dev; ..\.venv\Scripts\python.exe main.py run"
