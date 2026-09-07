param(
    [ValidateSet('Start','Stop','ResetTest','Validate','DataLab')]
    [string]$Action = 'Start'
)

$ErrorActionPreference = 'Stop'
$appRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$compose = Join-Path $appRoot 'compose.yml'

if ($Action -ne 'Validate' -and -not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is unavailable. Install/start Docker Desktop with the WSL2 backend first.'
}

function Invoke-LocalCompose {
    param([string[]]$ComposeArguments)
    & docker compose -f $compose @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose failed with exit code $LASTEXITCODE."
    }
}

switch ($Action) {
    'Start' { Invoke-LocalCompose @('--profile', 'dev', 'up', '-d', '--build') }
    'Stop' { Invoke-LocalCompose @('--profile', 'dev', '--profile', 'test', '--profile', 'data-lab', 'down') }
    'ResetTest' {
        Invoke-LocalCompose @('--profile', 'test', 'down', '-v')
        Invoke-LocalCompose @('--profile', 'test', 'up', '-d', '--build')
    }
    'DataLab' { Invoke-LocalCompose @('--profile', 'data-lab', 'up', '-d') }
    'Validate' { & (Join-Path $PSScriptRoot 'validate_all.ps1') }
}
