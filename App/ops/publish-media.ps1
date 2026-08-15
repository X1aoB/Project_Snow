param(
    [string]$Version = "2026.08.15.avatar.1",
    [string]$ReleaseRoot = "",
    [Parameter(Mandatory = $true)][string]$R2Remote
)

$ErrorActionPreference = "Stop"
$appRoot = Split-Path -Parent $PSScriptRoot
if (-not $ReleaseRoot) {
    $ReleaseRoot = Join-Path $appRoot "media/releases/$Version"
}
$resolvedRelease = (Resolve-Path -LiteralPath $ReleaseRoot).Path
$manifest = Join-Path $resolvedRelease "manifest.json"
$checksums = Join-Path $resolvedRelease "SHA256SUMS"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf) -or -not (Test-Path -LiteralPath $checksums -PathType Leaf)) {
    throw "The media release is incomplete: $resolvedRelease"
}
$manifestVersion = (Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).media_version
if ($manifestVersion -ne $Version) {
    throw "Media manifest version '$manifestVersion' does not match '$Version'."
}
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    throw "rclone is required to publish the media release."
}

$destination = "$($R2Remote.TrimEnd('/'))/$Version"
& rclone copy $resolvedRelease $destination --immutable --checksum --metadata
if ($LASTEXITCODE -ne 0) { throw "rclone copy failed." }
& rclone check $resolvedRelease $destination --checksum --one-way
if ($LASTEXITCODE -ne 0) { throw "rclone verification failed." }
Write-Output "Published and verified media $Version at $destination"
