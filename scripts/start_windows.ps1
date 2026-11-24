$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  Write-Host "[Agent Continuity] Creating local Python environment..."
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
Write-Host "[Agent Continuity] Starting..."
.\.venv\Scripts\python.exe app.py --open
