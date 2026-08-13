param([string]$Path = (Join-Path $PSScriptRoot '..\runtime'))

$ErrorActionPreference = 'Stop'
$target = (Resolve-Path $Path).Path
$drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($target).TrimEnd(':\'))
$total = $drive.Used + $drive.Free
$usedRatio = if ($total -gt 0) { $drive.Used / $total } else { 1 }
$freeGiB = $drive.Free / 1GB
if ($usedRatio -ge 0.70 -or $freeGiB -lt 12) {
    throw ("Refusing data build: disk usage {0:P1}, free space {1:N1} GiB. Required: under 70% used and at least 12 GiB free." -f $usedRatio, $freeGiB)
}
Write-Host ("Data build capacity accepted: disk usage {0:P1}, free space {1:N1} GiB. Limit builders to 8 CPUs and 8 GiB RAM." -f $usedRatio, $freeGiB)
