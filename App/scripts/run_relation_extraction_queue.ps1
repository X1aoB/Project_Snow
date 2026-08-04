[CmdletBinding()]
param(
    [ValidateRange(1, 50)]
    [int]$BatchSize = 5,
    [ValidateRange(0, 300)]
    [int]$PauseSeconds = 2,
    [ValidateRange(0, 10000)]
    [int]$MaxBatches = 0
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $appRoot "runtime\logs\relation_extraction_queue.log"

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
Set-Location $appRoot

# The extractor loads endpoint credentials from the untracked App/.env file.
$env:RELATION_CANDIDATE_DISABLE_THINKING = "true"

function Write-QueueLog([string]$Message) {
    Add-Content -LiteralPath $logPath -Value ("[{0:O}] {1}" -f (Get-Date), $Message) -Encoding utf8
}

function Get-QueuedJobCount() {
    $count = & python -c "import json; from pathlib import Path; p=Path('runtime/review/narrative_relation_jobs.jsonl'); print(sum(json.loads(line).get('status') == 'queued' for line in p.read_text(encoding='utf-8').splitlines() if line.strip()))"
    return [int]$count
}

$batch = 0
Write-QueueLog "Relation extraction queue started. batch_size=$BatchSize max_batches=$MaxBatches"

while ($true) {
    $queued = Get-QueuedJobCount
    if ($queued -eq 0) {
        Write-QueueLog "Queue complete: no queued jobs remain."
        break
    }
    if ($MaxBatches -gt 0 -and $batch -ge $MaxBatches) {
        Write-QueueLog "Stopped after configured max_batches=$MaxBatches with queued=$queued."
        break
    }

    $batch += 1
    Write-QueueLog "Starting batch=$batch queued_before=$queued"
    $output = & python -m pipelines.extract_relation_candidates --queued-only --limit $BatchSize 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    Add-Content -LiteralPath $logPath -Value $output -Encoding utf8
    if ($exitCode -ne 0) {
        Write-QueueLog "Batch=$batch exited with code=$exitCode; queue runner stopped."
        exit $exitCode
    }
    if ($PauseSeconds -gt 0) {
        Start-Sleep -Seconds $PauseSeconds
    }
}
