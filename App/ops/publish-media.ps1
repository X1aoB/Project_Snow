param(
    [string]$Version = "2026.08.19.avatar.1",
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
if ($manifestObject.schema_version -ne "project-snow-avatar-media-3" -or
    $manifestObject.release_basis -ne "verified_public_release" -or
    [bool]$manifestObject.private_candidate -or
    $manifestObject.license_review_status -ne "verified_public_release") {
    throw "The avatar manifest has not passed the public release review."
}

$releasePrefix = $resolvedRelease.TrimEnd([char[]]"\/") + [IO.Path]::DirectorySeparatorChar
function Resolve-ReleaseFile([string]$RelativePath) {
    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath)) {
        throw "Unsafe media path '$RelativePath'."
    }
    $candidate = [IO.Path]::GetFullPath((Join-Path $resolvedRelease $RelativePath))
    if (-not $candidate.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Media file is missing or outside the release: $RelativePath"
    }
    return $candidate
}

function Assert-AvatarAttribution([object]$Entry, [string]$Identity) {
    if (-not $Entry) { throw "Avatar attribution is missing: $Identity" }
    foreach ($field in @(
        "file_page_url", "source_image_url", "source_revision_id",
        "source_revision_timestamp", "source_uploader", "source_sha1",
        "original_sha1", "original_sha256", "license_source_page",
        "license_source_url", "license_source_revision_id"
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$Entry.$field)) {
            throw "Avatar attribution '$Identity' has no $field."
        }
    }
    if ([string]$Entry.file_page_url -notlike "https://wiki.biligame.com/sonw/*" -or
        [string]$Entry.source_image_url -notmatch '^https://' -or
        [string]$Entry.source_revision_id -notmatch '^\d+$' -or
        [string]$Entry.source_uploader -match '^(unknown|未知)$' -or
        [string]$Entry.source_sha1 -notmatch '^[0-9a-z]{1,40}$' -or
        [string]$Entry.original_sha1 -notmatch '^[0-9a-f]{40}$' -or
        [string]$Entry.original_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Entry.license_source_revision_id -notmatch '^\d+$' -or
        $Entry.license_status -notin @("verified", "verified_explicit", "verified_site_policy_no_page_exception") -or
        $Entry.license -ne "CC BY-NC-SA 4.0" -or
        $Entry.license_version -ne "4.0" -or
        $Entry.license_source_url -ne "https://creativecommons.org/licenses/by-nc-sa/4.0/" -or
        $Entry.release_basis -ne "verified_public_release" -or
        @($Entry.transformations).Count -eq 0) {
        throw "Avatar attribution is invalid: $Identity"
    }
    foreach ($variant in @("thumbnail", "stage")) {
        $assetPath = Resolve-ReleaseFile ([string]$Entry."${variant}_path")
        $expectedHash = [string]$Entry."${variant}_sha256"
        if ($expectedHash -notmatch '^[0-9a-f]{64}$' -or
            (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
            throw "Avatar derivative hash mismatch: $Identity/$variant"
        }
    }
}

$characters = @($manifestObject.characters)
if ([int]$manifestObject.character_count -ne 22 -or $characters.Count -ne 22) {
    throw "The media manifest must contain exactly 22 character avatars."
}
foreach ($character in $characters) {
    $characterId = [string]$character.character_id
    if ($characterId -notmatch '^[0-9a-f]{12}$') {
        throw "Invalid character avatar identity '$characterId'."
    }
    Assert-AvatarAttribution $character $characterId
}
$analyst = $manifestObject.analyst
if (-not $analyst -or $analyst.asset_id -ne "analyst-default") {
    throw "The media manifest must contain the analyst-default asset."
}
Assert-AvatarAttribution $analyst "analyst-default"

$listedFiles = @()
foreach ($line in Get-Content -LiteralPath $checksums -Encoding UTF8) {
    if ($line -notmatch '^([0-9a-f]{64})  (.+)$') {
        throw "Invalid SHA256SUMS entry: $line"
    }
    $expectedHash = $Matches[1]
    $relativePath = $Matches[2]
    $releaseFile = Resolve-ReleaseFile $relativePath
    $actualHash = (Get-FileHash -LiteralPath $releaseFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "Release checksum mismatch: $relativePath"
    }
    $listedFiles += $relativePath.Replace('\', '/')
}
$packagedFiles = @(
    Get-ChildItem -LiteralPath $resolvedRelease -Recurse -File |
        Where-Object { $_.FullName -ne $checksums } |
        ForEach-Object { $_.FullName.Substring($releasePrefix.Length).Replace('\', '/') } |
        Sort-Object
)
$checksumFiles = @($listedFiles | Sort-Object)
if (Compare-Object -ReferenceObject $packagedFiles -DifferenceObject $checksumFiles) {
    throw "SHA256SUMS does not cover every packaged avatar file exactly once."
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
