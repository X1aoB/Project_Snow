param(
    [string]$Python = '',
    [switch]$Browser,
    [switch]$RuntimeData,
    [switch]$Desktop
)

$ErrorActionPreference = 'Stop'
$appRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $Python) {
    $Python = Join-Path $appRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $Python)) { $Python = 'python' }
}
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $appRoot

Push-Location $appRoot
try {
    & $Python -m pytest tests -m 'not runtime_data' --ignore=tests/test_public_frontend_e2e.py --ignore=tests/test_public_frontend_reliability.py -q
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    if ($RuntimeData) {
        if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'runtime/manifest.json'))) {
            throw 'A verified private runtime release is required for -RuntimeData.'
        }
        & $Python -m pytest tests -m runtime_data -q
        if ($LASTEXITCODE -ne 0) { throw 'Private runtime tests failed.' }
        & $Python scripts/validate_architecture.py
        if ($LASTEXITCODE -ne 0) { throw 'Architecture validation failed.' }
    }
    if ($Browser) {
        $previousE2E = $env:RUN_PUBLIC_E2E
        try {
            $env:RUN_PUBLIC_E2E = '1'
            & $Python -m pytest tests/test_public_frontend_e2e.py tests/test_public_frontend_reliability.py -q
            if ($LASTEXITCODE -ne 0) { throw 'Browser tests failed.' }
        } finally {
            if ($null -eq $previousE2E) { Remove-Item Env:RUN_PUBLIC_E2E -ErrorAction SilentlyContinue }
            else { $env:RUN_PUBLIC_E2E = $previousE2E }
        }
    }
    foreach ($command in @('check', 'test', 'build')) {
        npm run $command --prefix public_frontend_src
        if ($LASTEXITCODE -ne 0) { throw "Frontend $command failed. Run npm ci --prefix public_frontend_src first." }
    }
    git diff --exit-code -- public_frontend/modules
    if ($LASTEXITCODE -ne 0) { throw 'The generated frontend bundle differs from its committed sources.' }
    node --check public_frontend/app.js
    if ($LASTEXITCODE -ne 0) { throw 'Public frontend JavaScript validation failed.' }
    if ($Desktop) {
        npm run smoke:brand --prefix client
        if ($LASTEXITCODE -ne 0) { throw 'Desktop smoke failed. Install the client dependencies first.' }
    }
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
        $imagePins = @{}
        try {
            foreach ($name in @('PUBLIC_API_IMAGE','EMBEDDING_IMAGE','CADDY_IMAGE','CLOUDFLARED_IMAGE','POSTGRES_IMAGE','QDRANT_IMAGE','NEO4J_IMAGE','EGRESS_PROXY_IMAGE')) {
                $imagePins[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
                [Environment]::SetEnvironmentVariable($name, ('example.invalid/validation@sha256:' + ('a' * 64)), 'Process')
            }
            foreach ($name in @('POSTGRES_PASSWORD', 'NEO4J_PASSWORD')) {
                $imagePins[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
                [Environment]::SetEnvironmentVariable($name, 'synthetic-validation-only', 'Process')
            }
            $env:ORIGIN_TLS_ROOT = '/etc/project-snow/origin-edge/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            $env:PUBLIC_DATA_ROOT = '/srv/project-snow/data/releases/local-validation-fixture'
            $env:PUBLIC_ENV_FILE = $publicEnvFixture.FullName
            $env:PUBLIC_MAILER_ENV_FILE = $mailerEnvFixture.FullName
            docker compose --env-file $publicEnvFixture.FullName -f compose.yml --profile dev --profile test --profile data-lab config --quiet --no-env-resolution
            if ($LASTEXITCODE -ne 0) { throw 'Local Compose validation failed.' }
            docker compose --env-file $publicEnvFixture.FullName -f compose.prod.yml --profile blue --profile admin config --quiet --no-env-resolution
            if ($LASTEXITCODE -ne 0) { throw 'Production Compose validation failed.' }
        } finally {
            foreach ($name in $imagePins.Keys) {
                [Environment]::SetEnvironmentVariable($name, $imagePins[$name], 'Process')
            }
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
    if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
}
