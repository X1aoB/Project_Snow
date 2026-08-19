param(
    [Parameter(Mandatory = $true)][string]$Sha,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [string]$HostName = 'project-snow-prod',
    [int]$Port = 43556,
    [string]$IdentityFile = "$env:USERPROFILE\.ssh\project_snow_prod_ed25519",
    [string]$SshConfig = ''
)

$ErrorActionPreference = 'Stop'
if ($Sha -notmatch '^[0-9a-f]{40}$') { throw 'An exact 40-character CI-passing main SHA is required.' }
$resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
$configPath = if ($SshConfig) {
    (Resolve-Path -LiteralPath $SshConfig).Path
} else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\runtime\project-snow-ssh-config')).Path
}
$resolvedManifest = (Resolve-Path -LiteralPath $ManifestPath).Path
$manifest = Get-Content -Raw -LiteralPath $resolvedManifest | ConvertFrom-Json
if ($manifest.schema_version -ne 'project-snow-release-1' -or [string]$manifest.commit_sha -ne $Sha) {
    throw 'The CI release manifest does not bind the selected main SHA.'
}
if ([string]$manifest.application.image -ne 'ghcr.io/x1aob/project_snow-public' -or
    [string]$manifest.application.digest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'The CI release manifest has no trusted Project Snow public image digest.'
}
$bootstrapImage = "$([string]$manifest.application.image)@$([string]$manifest.application.digest)"
$sshBase = @('-F', $configPath, '-i', $resolvedIdentity, '-p', [string]$Port, "deploy@$HostName")
$hardenedSshBase = @('-F', $configPath, '-i', $resolvedIdentity, '-p', '43556', "deploy@$HostName")

Write-Host 'Cloudflare Access must remain enabled during exact-SHA host preparation and privilege migration.'
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$expectedOrigin = 'https://github.com/X1aoB/Project_Snow.git'
$actualOrigin = (& git -C $repoRoot config --get remote.origin.url).Trim()
if ($LASTEXITCODE -ne 0 -or $actualOrigin -ne $expectedOrigin) {
    throw 'The local repository does not use the pinned Project Snow HTTPS origin.'
}
& git -c core.hooksPath=/dev/null -c core.fsmonitor=false -C $repoRoot fetch --quiet --force origin '+refs/heads/main:refs/remotes/origin/main'
if ($LASTEXITCODE -ne 0) { throw 'Cannot fetch the pinned origin/main branch.' }
& git -C $repoRoot cat-file -e "$Sha`^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'The selected controller commit is unavailable from the pinned origin.' }
& git -C $repoRoot merge-base --is-ancestor $Sha refs/remotes/origin/main
if ($LASTEXITCODE -ne 0) { throw 'The selected controller commit is not an ancestor of origin/main.' }

$hostBootstrapSource = ((& git -C $repoRoot show "$Sha`:App/scripts/bootstrap_release_host.py") -join "`n") + "`n"
if ($LASTEXITCODE -ne 0 -or $hostBootstrapSource -notmatch 'schedule_host_prepare') {
    throw 'The selected main commit has no trusted host bootstrap source.'
}
$bundlePaths = @(
    'App/ops/bootstrap-release-runner.sh',
    'App/ops/docker-daemon.json',
    'App/ops/feedback-mailer.env.example',
    'App/ops/prepare_debian.sh',
    'App/ops/project-snow-release',
    'App/ops/project-snow-release.sudoers',
    'App/ops/sysctl-project-snow.conf'
)
$nonce = [guid]::NewGuid().ToString('N')
$bundlePath = Join-Path ([System.IO.Path]::GetTempPath()) "project-snow-host-prepare-$nonce.tar"
$remoteArchive = "/tmp/project-snow-prepare-$Sha-$nonce.tar"
$uploaded = $false
try {
    # git archive may otherwise apply the Windows checkout EOL policy to text
    # members.  The bundle is executed by Debian /bin/sh, so derive it from the
    # exact Git object with an explicit LF conversion policy.
    $archiveArguments = @(
        '-c', 'core.autocrlf=false',
        '-c', 'core.eol=lf',
        '-C', $repoRoot,
        'archive', '--format=tar', "--output=$bundlePath", $Sha, '--'
    ) + $bundlePaths
    & git @archiveArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $bundlePath -PathType Leaf)) {
        throw 'Could not build the exact-SHA host preparation bundle.'
    }
    $bundleSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $bundlePath).Hash.ToLowerInvariant()
    $scpArguments = @('-q', '-F', $configPath, '-i', $resolvedIdentity, '-P', [string]$Port, $bundlePath, "deploy@${HostName}:$remoteArchive")
    & scp @scpArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not upload the exact-SHA host preparation bundle.' }
    $uploaded = $true

    # The exact Git-object Python source arrives over stdin. It opens the
    # deploy-owned archive with O_NOFOLLOW, verifies the local Git-archive
    # digest, extracts only seven fixed files, then schedules a host systemd
    # oneshot. The Docker helper exits before that unit restarts Docker.
    $scheduleCommand = @'
set -eu
bootstrap_image='__BOOTSTRAP_IMAGE__'
printf '%s\n' "$bootstrap_image" | grep -Eq '^ghcr\.io/x1aob/project_snow-public@sha256:[0-9a-f]{64}$'
docker pull "$bootstrap_image" >/dev/null
deploy_uid="$(id -u)"
docker run --rm -i --pid host --network host \
  --mount type=bind,src=/,dst=/host \
  --entrypoint python "$bootstrap_image" - \
  --host-root /host \
  --sha '__CONTROLLER_SHA__' \
  --archive '__CONTAINER_ARCHIVE__' \
  --archive-sha256 '__ARCHIVE_SHA256__' \
  --expected-owner-uid "$deploy_uid"
'@
    $scheduleCommand = $scheduleCommand.Replace('__BOOTSTRAP_IMAGE__', $bootstrapImage)
    $scheduleCommand = $scheduleCommand.Replace('__CONTROLLER_SHA__', $Sha)
    $scheduleCommand = $scheduleCommand.Replace('__CONTAINER_ARCHIVE__', "/host$remoteArchive")
    $scheduleCommand = $scheduleCommand.Replace('__ARCHIVE_SHA256__', $bundleSha256)
    $hostBootstrapSource | & ssh @sshBase $scheduleCommand
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not schedule exact-SHA host preparation. Keep Access enabled and use the provider root console for recovery.'
    }
} finally {
    if (Test-Path -LiteralPath $bundlePath -PathType Leaf) {
        Remove-Item -LiteralPath $bundlePath -Force
    }
    if ($uploaded) {
        & ssh @sshBase "rm -f -- '$remoteArchive'" 2>$null | Out-Null
    }
}

$statusPath = "/srv/project-snow/prepare-$Sha.status"
$waitCommand = @'
set -eu
status_path='__STATUS_PATH__'
deadline=$(( $(date +%s) + 1800 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  status="$(cat "$status_path" 2>/dev/null || true)"
  case "$status" in
    success) printf '%s\n' "$status"; exit 0 ;;
    failed:*) printf '%s\n' "$status" >&2; exit 1 ;;
    scheduled|running|'') sleep 5 ;;
    *) printf 'Unexpected host preparation status: %s\n' "$status" >&2; exit 1 ;;
  esac
done
echo 'Timed out waiting for exact-SHA host preparation.' >&2
exit 1
'@.Replace('__STATUS_PATH__', $statusPath)
& ssh @sshBase $waitCommand
if ($LASTEXITCODE -ne 0) {
    throw 'Host preparation failed. Keep Cloudflare Access enabled and inspect the systemd unit from the provider root console.'
}

# This is intentionally a new SSH process after the preparation session exits,
# so it cannot retain the historical Docker supplementary group.
$verifyCommand = @'
set -eu
if id -nG | tr ' ' '\n' | grep -Fxq docker; then
  echo 'Fresh deploy session still has the Docker group.' >&2
  exit 78
fi
if docker info >/dev/null 2>&1; then
  echo 'Fresh deploy session can still reach Docker.' >&2
  exit 78
fi
sudo -n /usr/local/sbin/project-snow-release status
if sudo -n /bin/true >/dev/null 2>&1; then
  echo 'Deploy has an unexpected general sudo grant.' >&2
  exit 78
fi
'@
$verification = & ssh @hardenedSshBase $verifyCommand
if ($LASTEXITCODE -ne 0) {
    throw 'Post-prepare least-privilege verification failed. Keep Cloudflare Access enabled.'
}
$runnerStatus = ($verification -join "`n") | ConvertFrom-Json
if ([string]$runnerStatus.controller_sha -ne $Sha) {
    throw 'The root-owned release controller does not match the selected exact main SHA.'
}
if ([bool]$runnerStatus.deploy_has_docker_group -or [bool]$runnerStatus.deploy_can_access_docker) {
    throw 'The fresh deploy session retains root-equivalent Docker access.'
}
if (-not [bool]$runnerStatus.ufw_active -or -not [bool]$runnerStatus.fail2ban_active -or
    -not [bool]$runnerStatus.sshd_hardened) {
    throw 'The firewall, fail2ban or SSH hardening gate is not active.'
}
$verification
Write-Host 'Exact-SHA host preparation and root-owned runner migration completed. Cloudflare Access was not changed.'
Write-Host 'Close any older deploy SSH sessions before staging the release.'
