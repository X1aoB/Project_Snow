param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha,
    [ValidateSet('blue','green')][string]$Colour = 'green',
    [Parameter(Mandatory=$true)][string]$AppDigest,
    [Parameter(Mandatory=$true)][string]$EmbeddingDigest,
    [string]$HostName = '45.207.211.216',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519"
)

$ErrorActionPreference = 'Stop'
$digestPattern = '^sha256:[0-9a-f]{64}$'
if ($AppDigest -notmatch $digestPattern -or $EmbeddingDigest -notmatch $digestPattern) { throw 'Both image digests must use sha256:<64 hex>.' }
$appImage = "ghcr.io/x1aob/project_snow-public@$AppDigest"
$embeddingImage = "ghcr.io/x1aob/project_snow-embedding@$EmbeddingDigest"
Write-Host "Deploying main commit $Sha to private acceptance colour $Colour using immutable image digests."
ssh -i $IdentityFile -p $Port "deploy@$HostName" "cd /srv/project-snow/app && PUBLIC_API_IMAGE='$appImage' EMBEDDING_IMAGE='$embeddingImage' ./ops/deploy.sh '$Colour' '$Sha'"
if ($LASTEXITCODE -ne 0) { throw 'Remote deployment failed.' }
