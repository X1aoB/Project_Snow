param(
    [ValidateSet('Start','Stop','ResetTest','Validate','DataLab')]
    [string]$Action = 'Start'
)

$ErrorActionPreference = 'Stop'
$appRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $appRoot 'compose.yml'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is unavailable. Install/start Docker Desktop with the WSL2 backend first.'
}

switch ($Action) {
    'Start' { docker compose -f $compose --profile dev up -d --build }
    'Stop' { docker compose -f $compose --profile dev --profile test --profile data-lab down }
    'ResetTest' {
        docker compose -f $compose --profile test down -v
        docker compose -f $compose --profile test up -d --build
    }
    'DataLab' { docker compose -f $compose --profile data-lab up -d }
    'Validate' { & (Join-Path $PSScriptRoot 'validate_all.ps1') }
}
