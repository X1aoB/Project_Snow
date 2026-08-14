param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,

    [int]$PollSeconds = 60
)

$ErrorActionPreference = "Stop"
$appRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $appRoot ".venv\Scripts\python.exe"
$runRoot = Join-Path $appRoot "runtime\review\automation\runs\$RunId"
$manifestPath = Join-Path $runRoot "manifest.json"
$logPath = Join-Path $runRoot "background-monitor.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment was not found: $python"
}
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Automation manifest was not found: $manifestPath"
}

function Write-MonitorLog {
    param([string]$Message)
    $timestamp = [DateTimeOffset]::UtcNow.ToString("o")
    Add-Content -LiteralPath $logPath -Value "$timestamp $Message" -Encoding utf8
}

Set-Location -LiteralPath $appRoot
Write-MonitorLog "monitor_started run_id=$RunId poll_seconds=$PollSeconds"

while ($true) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $status = [string]$manifest.status

        if ($status -eq "ready_to_admit") {
            Write-MonitorLog "admission_started"
            & $python -m pipelines.review_evidence_batch admit $RunId --confirm-apply |
                Out-File -LiteralPath (Join-Path $runRoot "background-admission-result.json") -Encoding utf8
            if ($LASTEXITCODE -ne 0) {
                throw "Admission command exited with code $LASTEXITCODE."
            }
            $admitted = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            Write-MonitorLog "admission_finished status=$($admitted.status)"
            exit 0
        }

        if ($status -eq "admitted") {
            Write-MonitorLog "already_admitted"
            exit 0
        }

        if ($status -in @("failed", "expired", "cancelled", "canceled")) {
            Write-MonitorLog "terminal_failure status=$status error=$($manifest.last_error)"
            exit 1
        }

        & $python -m pipelines.review_evidence_batch sync $RunId | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Sync command exited with code $LASTEXITCODE."
        }

        $updated = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        $phase = $updated.phases | Where-Object { $_.name -eq $updated.active_phase } | Select-Object -First 1
        Write-MonitorLog (
            "sync status=$($updated.status) phase=$($updated.active_phase) " +
            "provider=$($phase.provider_status) completed=$($phase.request_counts.completed) " +
            "total=$($phase.request_counts.total) failed=$($phase.request_counts.failed)"
        )
    }
    catch {
        Write-MonitorLog "transient_error message=$($_.Exception.Message)"
    }

    Start-Sleep -Seconds ([Math]::Max(15, $PollSeconds))
}
