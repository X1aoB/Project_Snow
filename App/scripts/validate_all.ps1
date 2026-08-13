$ErrorActionPreference = 'Stop'
$appRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $appRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
$env:PYTHONPATH = $appRoot

Push-Location $appRoot
try {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    & $python scripts/export_publishable_graph.py
    if ($LASTEXITCODE -ne 0) { throw 'Publishable graph export failed.' }
    & $python scripts/validate_architecture.py
    if ($LASTEXITCODE -ne 0) { throw 'Architecture validation failed.' }
    node --check public_frontend/app.js
    if ($LASTEXITCODE -ne 0) { throw 'Public frontend JavaScript validation failed.' }
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        docker compose -f compose.yml config --quiet
        docker compose -f compose.prod.yml --profile blue --profile admin config --quiet
    } else {
        Write-Warning 'Docker is unavailable; Compose validation was skipped.'
    }
} finally {
    Pop-Location
}
