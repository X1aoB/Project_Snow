param(
    [ValidateSet('blue','green')][string]$Colour = 'blue',
    [string]$Sha = '',
    [string]$HostName = '45.207.211.216',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519"
)

$ErrorActionPreference = 'Stop'
$shaArgument = if ($Sha) { " '$Sha'" } else { '' }
$remoteCommand = "cd /srv/project-snow/app && ./ops/promote.sh '$Colour'$shaArgument"
Write-Host "Promoting the already-staged $Colour colour after private acceptance."
ssh -i $IdentityFile -p $Port "deploy@$HostName" $remoteCommand
if ($LASTEXITCODE -ne 0) { throw 'Remote promotion failed; inspect Caddy and the previous colour before retrying.' }
