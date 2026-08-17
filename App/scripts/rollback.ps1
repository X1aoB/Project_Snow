param(
    [ValidateSet('blue','green')][string]$Colour,
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
$sshArgs = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName", "cd /srv/project-snow/app && ./ops/rollback.sh '$Colour'")
& ssh @sshArgs
if ($LASTEXITCODE -ne 0) { throw 'Remote rollback failed.' }
