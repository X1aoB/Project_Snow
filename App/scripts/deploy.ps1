param(
    [string]$ManifestPath = '',
    [string]$Sha = '',
    [ValidateSet('blue','green')][string]$Colour = 'green',
    [string]$AppDigest = '',
    [string]$EmbeddingDigest = '',
    [string]$MediaVersion = '',
    [string]$HostName = '45.207.211.216',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519"
)

$ErrorActionPreference = 'Stop'
$manifestRemotePath = ''
if ($ManifestPath) {
    $resolvedManifest = (Resolve-Path -LiteralPath $ManifestPath).Path
    $manifest = Get-Content -Raw -LiteralPath $resolvedManifest | ConvertFrom-Json
    if ($manifest.schema_version -ne 'project-snow-release-1') { throw 'Unsupported release manifest schema.' }
    $Sha = [string]$manifest.commit_sha
    $AppDigest = [string]$manifest.application.digest
    $EmbeddingDigest = [string]$manifest.embedding.digest
    $MediaVersion = [string]$manifest.media_version
    if ([string]$manifest.application.image -ne 'ghcr.io/x1aob/project_snow-public') { throw 'Unexpected public image repository.' }
    if ([string]$manifest.embedding.image -ne 'ghcr.io/x1aob/project_snow-embedding') { throw 'Unexpected embedding image repository.' }
    if ([string]::IsNullOrWhiteSpace($MediaVersion)) { throw 'Release manifest has no media version.' }
    $manifestRemotePath = '/srv/project-snow/runtime/release-candidate.json'
    scp -q -i $IdentityFile -P $Port $resolvedManifest "deploy@${HostName}:$manifestRemotePath"
    if ($LASTEXITCODE -ne 0) { throw 'Release manifest upload failed.' }
}
$shaPattern = '^[0-9a-f]{40}$'
$digestPattern = '^sha256:[0-9a-f]{64}$'
if ($Sha -notmatch $shaPattern) { throw 'A verified 40-character main SHA is required.' }
if ($AppDigest -notmatch $digestPattern -or $EmbeddingDigest -notmatch $digestPattern) { throw 'Both image digests must use sha256:<64 hex>.' }
if ($ManifestPath -and $MediaVersion -notmatch '^\S+$') { throw 'Release manifest media version is invalid.' }
$appImage = "ghcr.io/x1aob/project_snow-public@$AppDigest"
$embeddingImage = "ghcr.io/x1aob/project_snow-embedding@$EmbeddingDigest"
Write-Host "Deploying main commit $Sha to private acceptance colour $Colour using immutable image digests and media $MediaVersion."
$manifestEnvironment = if ($manifestRemotePath) { "PROJECT_SNOW_RELEASE_MANIFEST='$manifestRemotePath' " } else { '' }
$remoteCommand = "cd /srv/project-snow/repo && git fetch --quiet origin main && git cat-file -e '$Sha^{commit}' && git merge-base --is-ancestor '$Sha' origin/main && git checkout --quiet --detach '$Sha' && git rev-parse HEAD | grep -Fx '$Sha' && cd App && ${manifestEnvironment}PUBLIC_API_IMAGE='$appImage' EMBEDDING_IMAGE='$embeddingImage' ./ops/deploy.sh '$Colour' '$Sha'"
ssh -i $IdentityFile -p $Port "deploy@$HostName" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Remote deployment failed.' }
