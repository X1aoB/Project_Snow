param(
    [ValidateSet('blue','green')][string]$Colour = 'blue',
    [Parameter(Mandatory = $true)][string]$Sha,
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
$shaPattern = '^[0-9a-f]{40}$'
if ($Sha -notmatch $shaPattern) { throw 'A verified 40-character main SHA is required.' }
$remoteCommand = "sudo -n /usr/local/sbin/project-snow-release promote '$Colour' '$Sha'"
Write-Host "Promoting the already-staged $Colour colour at main commit $Sha after private acceptance."
$sshArgs = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName", $remoteCommand)
& ssh @sshArgs
if ($LASTEXITCODE -ne 0) { throw 'Remote promotion failed; inspect Caddy and the previous colour before retrying.' }
