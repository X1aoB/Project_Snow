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
        $publicDataRootWasSet = Test-Path Env:PUBLIC_DATA_ROOT
        $previousPublicDataRoot = $env:PUBLIC_DATA_ROOT
        $publicEnvFileWasSet = Test-Path Env:PUBLIC_ENV_FILE
        $previousPublicEnvFile = $env:PUBLIC_ENV_FILE
        $publicMailerEnvFileWasSet = Test-Path Env:PUBLIC_MAILER_ENV_FILE
        $previousPublicMailerEnvFile = $env:PUBLIC_MAILER_ENV_FILE
        $publicEnvFixture = New-TemporaryFile
        $mailerEnvFixture = New-TemporaryFile
        try {
            $env:ORIGIN_TLS_ROOT = '/etc/project-snow/origin-edge/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            $env:PUBLIC_DATA_ROOT = '/srv/project-snow/data/releases/local-validation-fixture'
            $env:PUBLIC_ENV_FILE = $publicEnvFixture.FullName
            $env:PUBLIC_MAILER_ENV_FILE = $mailerEnvFixture.FullName
            docker compose -f compose.yml config --quiet
            docker compose -f compose.prod.yml --profile blue --profile admin config --quiet
        } finally {
            if ($originTlsRootWasSet) {
                $env:ORIGIN_TLS_ROOT = $previousOriginTlsRoot
            } else {
                Remove-Item Env:ORIGIN_TLS_ROOT -ErrorAction SilentlyContinue
            }
            if ($publicDataRootWasSet) {
                $env:PUBLIC_DATA_ROOT = $previousPublicDataRoot
            } else {
                Remove-Item Env:PUBLIC_DATA_ROOT -ErrorAction SilentlyContinue
            }
            if ($publicEnvFileWasSet) {
                $env:PUBLIC_ENV_FILE = $previousPublicEnvFile
            } else {
                Remove-Item Env:PUBLIC_ENV_FILE -ErrorAction SilentlyContinue
            }
            if ($publicMailerEnvFileWasSet) {
                $env:PUBLIC_MAILER_ENV_FILE = $previousPublicMailerEnvFile
            } else {
                Remove-Item Env:PUBLIC_MAILER_ENV_FILE -ErrorAction SilentlyContinue
            }
            Remove-Item -LiteralPath $publicEnvFixture.FullName -Force
            Remove-Item -LiteralPath $mailerEnvFixture.FullName -Force
        }
    } else {
        Write-Warning 'Docker is unavailable; Compose validation was skipped.'
    }
} finally {
    Pop-Location
}
