param(
    [ValidateSet('blue','green')][string]$Colour,
    [string]$HostName = '45.207.211.216',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519"
)

$ErrorActionPreference = 'Stop'
ssh -i $IdentityFile -p $Port "deploy@$HostName" "cd /srv/project-snow/app && ./ops/rollback.sh '$Colour'"
if ($LASTEXITCODE -ne 0) { throw 'Remote rollback failed.' }
