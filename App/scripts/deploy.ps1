param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [ValidateSet('blue','green')][string]$Colour = 'green',
    [string]$DataReleasePath = '',
    [string]$AvatarReleasePath = '',
    [string]$StickerReleasePath = '',
    [Parameter(Mandatory = $true)][string]$OriginPrivateKeyPath,
    [string]$HostName = 'project-snow-prod',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519",
    [string]$SshConfig = ''
)

$ErrorActionPreference = 'Stop'
$resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
$configPath = if ($SshConfig) {
    (Resolve-Path -LiteralPath $SshConfig).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\runtime\project-snow-ssh-config')).Path
}
$resolvedManifest = (Resolve-Path -LiteralPath $ManifestPath).Path
$manifest = Get-Content -Raw -LiteralPath $resolvedManifest | ConvertFrom-Json
if ($manifest.schema_version -ne 'project-snow-release-1') { throw 'Unsupported release manifest schema.' }
$Sha = [string]$manifest.commit_sha
$AppDigest = [string]$manifest.application.digest
$EmbeddingDigest = [string]$manifest.embedding.digest
$MediaVersion = [string]$manifest.media_version
$StickerVersion = [string]$manifest.sticker_version
$DataVersion = [string]$manifest.data_version
$shaPattern = '^[0-9a-f]{40}$'
$digestPattern = '^sha256:[0-9a-f]{64}$'
if ($Sha -notmatch $shaPattern) { throw 'A verified 40-character main SHA is required.' }
if ($AppDigest -notmatch $digestPattern -or $EmbeddingDigest -notmatch $digestPattern) { throw 'Both image digests must use sha256:<64 hex>.' }
if ([string]$manifest.application.image -ne 'ghcr.io/x1aob/project_snow-public') { throw 'Unexpected public image repository.' }
if ([string]$manifest.embedding.image -ne 'ghcr.io/x1aob/project_snow-embedding') { throw 'Unexpected embedding image repository.' }
if ([string]::IsNullOrWhiteSpace($MediaVersion)) { throw 'Release manifest has no media version.' }
if ([string]::IsNullOrWhiteSpace($StickerVersion)) { throw 'Release manifest has no sticker version.' }
if ([string]::IsNullOrWhiteSpace($DataVersion)) { throw 'Release manifest has no data version.' }
$versionPattern = '^[0-9A-Za-z._-]+$'
if ($MediaVersion -notmatch $versionPattern -or $StickerVersion -notmatch $versionPattern -or $DataVersion -notmatch $versionPattern) {
    throw 'Release manifest data or media version is invalid.'
}
$artifactBindings = @(
    [pscustomobject]@{ Kind = 'data'; Version = $DataVersion; Binding = $manifest.release_artifacts.data; RequiresChecksums = $false },
    [pscustomobject]@{ Kind = 'avatar'; Version = $MediaVersion; Binding = $manifest.release_artifacts.avatar; RequiresChecksums = $true },
    [pscustomobject]@{ Kind = 'sticker'; Version = $StickerVersion; Binding = $manifest.release_artifacts.sticker; RequiresChecksums = $true }
)
foreach ($artifactBinding in $artifactBindings) {
    if ([string]$artifactBinding.Binding.version -ne $artifactBinding.Version) {
        throw "Release manifest $($artifactBinding.Kind) artifact binding has the wrong version."
    }
    if ([string]$artifactBinding.Binding.manifest_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Release manifest has no trusted $($artifactBinding.Kind) manifest digest."
    }
    if ($artifactBinding.RequiresChecksums -and [string]$artifactBinding.Binding.checksums_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Release manifest has no trusted $($artifactBinding.Kind) SHA256SUMS digest."
    }
}
foreach ($configurationPath in @('compose.prod.yml', 'infra/Caddyfile', 'infra/OriginEdge.Caddyfile', 'config/origin-edge/origin-cert.pem', 'config/origin-edge/aop-ca.pem', 'scripts/install_origin_tls.py', 'scripts/cloudflare_origin_firewall.py', 'ops/project-snow-origin-firewall.service', 'ops/project-snow-origin-firewall.timer', 'infra/egress-squid.conf', 'infra/neo4j-entrypoint.sh', 'infra/postgres/postgresql.conf', 'infra/public-api.Dockerfile', 'requirements-public.txt')) {
    $configurationHash = [string]$manifest.configuration_sha256.PSObject.Properties[$configurationPath].Value
    if ($configurationHash -notmatch '^[0-9a-f]{64}$') { throw "Release manifest has no valid hash for $configurationPath." }
}
foreach ($releaseControlPath in @('ops/project-snow-release', 'ops/project-snow-release.sudoers')) {
    $releaseControlHash = [string]$manifest.release_control_sha256.PSObject.Properties[$releaseControlPath].Value
    if ($releaseControlHash -notmatch '^[0-9a-f]{64}$') { throw "Release manifest has no valid hash for $releaseControlPath." }
}
$tlsBinding = $manifest.direct_origin_tls
if ($null -eq $tlsBinding -or @($tlsBinding.PSObject.Properties).Count -ne 5) {
    throw 'Release manifest has no exact direct-origin TLS binding.'
}
if ([string]$tlsBinding.schema_version -ne 'project-snow-origin-tls-1' -or [string]$tlsBinding.hostname -ne 'snow.xiaob.dev') {
    throw 'Release manifest has an unexpected direct-origin TLS identity.'
}
foreach ($tlsHashName in @('bundle_sha256', 'origin_certificate_sha256', 'aop_ca_sha256')) {
    if ([string]$tlsBinding.PSObject.Properties[$tlsHashName].Value -notmatch '^[0-9a-f]{64}$') {
        throw "Release manifest has an invalid direct-origin TLS hash: $tlsHashName."
    }
}
$resolvedOriginPrivateKey = (Resolve-Path -LiteralPath $OriginPrivateKeyPath).Path
$originPrivateKeyItem = Get-Item -Force -LiteralPath $resolvedOriginPrivateKey
if ($originPrivateKeyItem.PSIsContainer -or -not [string]::IsNullOrEmpty([string]$originPrivateKeyItem.LinkType)) {
    throw 'Origin private key must be one local regular file, not a directory or link.'
}
if ($originPrivateKeyItem.Length -lt 80 -or $originPrivateKeyItem.Length -gt 65536) {
    throw 'Origin private key size is outside the accepted range.'
}
$originPrivateKeySha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedOriginPrivateKey).Hash.ToLowerInvariant()
if ($originPrivateKeySha256 -notmatch '^[0-9a-f]{64}$') {
    throw 'Origin private key has no valid exact SHA256.'
}
$statusArgs = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName", 'sudo -n /usr/local/sbin/project-snow-release status')
$statusOutput = & ssh @statusArgs
if ($LASTEXITCODE -ne 0) { throw 'The root-owned release runner is not ready.' }
$runnerStatus = ($statusOutput -join "`n") | ConvertFrom-Json
if ([bool]$runnerStatus.deploy_has_docker_group) { throw 'The deploy account still has root-equivalent Docker access.' }
if ([bool]$runnerStatus.deploy_can_access_docker) { throw 'The deploy account can still reach the root Docker daemon.' }
if (-not [bool]$runnerStatus.ufw_active) { throw 'The production default-deny firewall is not active.' }
if (-not [bool]$runnerStatus.fail2ban_active) { throw 'The production SSH fail2ban jail is not active.' }
if (-not [bool]$runnerStatus.sshd_hardened) { throw 'The production SSH effective policy is not hardened.' }

$appRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$releaseSpecs = @(
    [pscustomobject]@{
        Kind = 'data'; Version = $DataVersion; VersionField = 'data_version'
        SuppliedPath = $DataReleasePath; DefaultPath = Join-Path $appRoot "runtime\releases\$DataVersion"
        Binding = $manifest.release_artifacts.data
        Installed = @($runnerStatus.installed_data_versions)
    },
    [pscustomobject]@{
        Kind = 'avatar'; Version = $MediaVersion; VersionField = 'media_version'
        SuppliedPath = $AvatarReleasePath; DefaultPath = Join-Path $appRoot "media\releases\$MediaVersion"
        Binding = $manifest.release_artifacts.avatar
        Installed = @($runnerStatus.installed_avatar_versions)
    },
    [pscustomobject]@{
        Kind = 'sticker'; Version = $StickerVersion; VersionField = 'media_version'
        SuppliedPath = $StickerReleasePath; DefaultPath = Join-Path $appRoot "media\releases\$StickerVersion"
        Binding = $manifest.release_artifacts.sticker
        Installed = @($runnerStatus.installed_sticker_versions)
    }
)
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) { throw 'tar is required to package immutable release archives.' }
foreach ($releaseSpec in $releaseSpecs) {
    if ($releaseSpec.Installed -contains $releaseSpec.Version) {
        Write-Host "$($releaseSpec.Kind) release $($releaseSpec.Version) is already installed; archive upload skipped."
        continue
    }
    $candidatePath = if ($releaseSpec.SuppliedPath) { $releaseSpec.SuppliedPath } else { $releaseSpec.DefaultPath }
    $resolvedRelease = (Resolve-Path -LiteralPath $candidatePath).Path
    if (-not (Test-Path -LiteralPath $resolvedRelease -PathType Container)) {
        throw "Missing $($releaseSpec.Kind) release directory: $candidatePath"
    }
    $packageManifestPath = Join-Path $resolvedRelease 'manifest.json'
    if (-not (Test-Path -LiteralPath $packageManifestPath -PathType Leaf)) {
        throw "$($releaseSpec.Kind) release has no manifest.json."
    }
    $packageManifest = Get-Content -Raw -LiteralPath $packageManifestPath | ConvertFrom-Json
    $packageVersion = [string]$packageManifest.($releaseSpec.VersionField)
    if ($packageVersion -ne $releaseSpec.Version) {
        throw "$($releaseSpec.Kind) package version '$packageVersion' does not match '$($releaseSpec.Version)'."
    }
    $actualManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $packageManifestPath).Hash.ToLowerInvariant()
    if ($actualManifestHash -ne [string]$releaseSpec.Binding.manifest_sha256) {
        throw "$($releaseSpec.Kind) manifest does not match the trusted CI release binding."
    }
    if ($releaseSpec.Kind -ne 'data') {
        $checksumsPath = Join-Path $resolvedRelease 'SHA256SUMS'
        if (-not (Test-Path -LiteralPath $checksumsPath -PathType Leaf)) {
            throw "$($releaseSpec.Kind) release has no SHA256SUMS."
        }
        $actualChecksumsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $checksumsPath).Hash.ToLowerInvariant()
        if ($actualChecksumsHash -ne [string]$releaseSpec.Binding.checksums_sha256) {
            throw "$($releaseSpec.Kind) SHA256SUMS does not match the trusted CI release binding."
        }
    }
    $archivePath = Join-Path ([System.IO.Path]::GetTempPath()) ("project-snow-{0}-{1}.tar" -f $releaseSpec.Kind, [guid]::NewGuid().ToString('N'))
    try {
        & tar -cf $archivePath -C $resolvedRelease .
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            throw "Failed to package $($releaseSpec.Kind) release."
        }
        $archiveRemotePath = "/srv/project-snow/inbox/$($releaseSpec.Kind)-$($releaseSpec.Version).tar"
        $archiveScpArgs = @('-q', '-F', $configPath, '-i', $resolvedIdentity, '-P', [string]$Port, $archivePath, "deploy@${HostName}:$archiveRemotePath")
        & scp @archiveScpArgs
        if ($LASTEXITCODE -ne 0) { throw "$($releaseSpec.Kind) release archive upload failed." }
    } finally {
        if (Test-Path -LiteralPath $archivePath -PathType Leaf) { Remove-Item -LiteralPath $archivePath -Force }
    }
}
$originPrivateKeyRemotePath = "/srv/project-snow/inbox/origin-key-$Sha-$originPrivateKeySha256.pem"
$originKeySshBase = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName")
$originKeyScpArgs = @('-q', '-F', $configPath, '-i', $resolvedIdentity, '-P', [string]$Port, $resolvedOriginPrivateKey, "deploy@${HostName}:$originPrivateKeyRemotePath")
$originKeyCleanupRequired = $true
try {
    & ssh @originKeySshBase "rm -f -- '$originPrivateKeyRemotePath'"
    if ($LASTEXITCODE -ne 0) { throw 'Could not clear the fixed origin-key inbox coordinate.' }
    & scp @originKeyScpArgs
    if ($LASTEXITCODE -ne 0) { throw 'Origin private key upload failed.' }
    & ssh @originKeySshBase "chmod 0600 -- '$originPrivateKeyRemotePath'"
    if ($LASTEXITCODE -ne 0) { throw 'Origin private key inbox mode could not be restricted.' }

    $manifestRemotePath = "/srv/project-snow/inbox/release-$Sha.json"
    $scpArgs = @('-q', '-F', $configPath, '-i', $resolvedIdentity, '-P', [string]$Port, $resolvedManifest, "deploy@${HostName}:$manifestRemotePath")
    & scp @scpArgs
    if ($LASTEXITCODE -ne 0) { throw 'Release manifest upload failed.' }
    Write-Host "Staging main commit $Sha in inactive private-acceptance colour $Colour using immutable image digests, avatar media $MediaVersion and stickers $StickerVersion."
    $remoteCommand = "sudo -n /usr/local/sbin/project-snow-release stage '$Colour' '$Sha'"
    $sshArgs = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName", $remoteCommand)
    & ssh @sshArgs
    if ($LASTEXITCODE -ne 0) { throw 'Remote staging failed; active traffic was not changed.' }
} finally {
    if ($originKeyCleanupRequired) {
        & ssh @originKeySshBase "rm -f -- '$originPrivateKeyRemotePath'"
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Could not confirm cleanup of the fixed origin-key inbox coordinate.'
        }
    }
}
