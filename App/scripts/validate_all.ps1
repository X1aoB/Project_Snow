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
        $originTlsRootWasSet = Test-Path Env:ORIGIN_TLS_ROOT
        $previousOriginTlsRoot = $env:ORIGIN_TLS_ROOT
        try {
            $env:ORIGIN_TLS_ROOT = '/etc/project-snow/origin-edge/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            docker compose -f compose.yml config --quiet
            docker compose -f compose.prod.yml --profile blue --profile admin config --quiet
        } finally {
            if ($originTlsRootWasSet) {
                $env:ORIGIN_TLS_ROOT = $previousOriginTlsRoot
            } else {
                Remove-Item Env:ORIGIN_TLS_ROOT -ErrorAction SilentlyContinue
            }
        }
    } else {
        Write-Warning 'Docker is unavailable; Compose validation was skipped.'
    }
} finally {
    Pop-Location
}
