param(
    [ValidateSet('blue','green')][string]$Colour = 'blue',
    [string]$Sha = '',
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
$shaArgument = if ($Sha) { " '$Sha'" } else { '' }
$remoteCommand = "cd /srv/project-snow/app && ./ops/promote.sh '$Colour'$shaArgument"
Write-Host "Promoting the already-staged $Colour colour after private acceptance."
$sshArgs = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName", $remoteCommand)
& ssh @sshArgs
if ($LASTEXITCODE -ne 0) { throw 'Remote promotion failed; inspect Caddy and the previous colour before retrying.' }
