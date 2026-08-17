param(
    [string]$Version = "2026.08.17.avatar.2",
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
$manifestObject = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$manifestVersion = $manifestObject.media_version
if ($manifestVersion -ne $Version) {
    throw "Media manifest version '$manifestVersion' does not match '$Version'."
}
$analyst = $manifestObject.analyst
if (-not $analyst -or $analyst.asset_id -ne "analyst-default") {
    throw "The media manifest must contain the analyst-default asset."
}
if ($analyst.license_status -notin @("verified", "verified_explicit", "verified_site_policy_no_page_exception") -or
    $analyst.license -ne "CC BY-NC-SA 4.0" -or $analyst.license_version -ne "4.0" -or
    [string]::IsNullOrWhiteSpace([string]$analyst.license_source_page) -or
    [string]::IsNullOrWhiteSpace([string]$analyst.license_source_url) -or
    [string]::IsNullOrWhiteSpace([string]$analyst.license_source_revision_id)) {
    throw "Analyst avatar license evidence is incomplete."
}
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    throw "rclone is required to publish the media release."
}

$destination = "$($R2Remote.TrimEnd('/'))/$Version"
# Cloudflare R2 returns 501 for the legacy rclone HEAD/checksum metadata
# round-trip.  The release manifest and SHA256SUMS are verified separately;
# these flags keep the upload compatible with the server's rclone build.
& rclone copy $resolvedRelease $destination --immutable --size-only --s3-no-check-bucket --s3-no-head --s3-disable-checksum --s3-no-system-metadata
if ($LASTEXITCODE -ne 0) { throw "rclone copy failed." }
& rclone check $resolvedRelease $destination --size-only --one-way --no-traverse
if ($LASTEXITCODE -ne 0) { throw "rclone verification failed." }
Write-Output "Published and verified media $Version at $destination"
