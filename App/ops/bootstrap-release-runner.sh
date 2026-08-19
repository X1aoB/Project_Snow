#!/bin/sh
set -eu
umask 077
export LC_ALL=C

# One-time, root-run migration from the historical deploy-in-docker-group
# layout.  It creates a fresh root-owned checkout instead of trusting Git
# metadata that the former Docker-equivalent account could modify.
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run bootstrap-release-runner.sh as root.' >&2
  exit 77
fi

expected_origin=https://github.com/X1aoB/Project_Snow.git
repo=/srv/project-snow/repo
inbox=/srv/project-snow/inbox
controller_sha=""
if [ "${1:-}" = --controller-sha ]; then
  [ "$#" -eq 2 ] || { echo 'Usage: bootstrap-release-runner.sh [--controller-sha <40-character-main-sha>]' >&2; exit 64; }
  controller_sha="$2"
  printf '%s\n' "$controller_sha" | grep -Eq '^[0-9a-f]{40}$' || {
    echo 'The controller SHA must be 40 lowercase hexadecimal characters.' >&2
    exit 64
  }
elif [ "$#" -ne 0 ]; then
  echo 'Usage: bootstrap-release-runner.sh [--controller-sha <40-character-main-sha>]' >&2
  exit 64
fi

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
dependencies_ready=1
for required_command in curl git jq openssl python3 sudo flock runuser; do
  command -v "$required_command" >/dev/null 2>&1 || dependencies_ready=0
done
[ -r /etc/ssl/certs/ca-certificates.crt ] || dependencies_ready=0
if [ "$dependencies_ready" -ne 1 ]; then
  apt-get update
  apt-get install -y ca-certificates curl git jq openssl python3 sudo util-linux
fi
command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1 || {
  echo 'Docker Engine with the buildx plugin must be installed before bootstrap.' >&2
  exit 69
}

for protected_path in \
  /srv/project-snow \
  "$inbox" \
  /srv/project-snow/runtime \
  /srv/project-snow/releases \
  /srv/project-snow/data \
  /srv/project-snow/media \
  /etc/project-snow \
  /etc/project-snow/secrets \
  /etc/project-snow/cloudflared; do
  [ ! -L "$protected_path" ] || {
    echo "Protected release path must not be a symlink: $protected_path" >&2
    exit 66
  }
done

install -o root -g root -m 0755 -d /srv/project-snow /srv/project-snow/backups
install -o deploy -g deploy -m 0700 -d "$inbox"

fresh_repo="$(mktemp -d /srv/project-snow/.repo-root.XXXXXX)"
cleanup() {
  [ -z "${fresh_repo:-}" ] || rm -rf -- "$fresh_repo"
}
trap cleanup EXIT HUP INT TERM
rmdir "$fresh_repo"
git -c core.hooksPath=/dev/null clone --quiet --no-checkout "$expected_origin" "$fresh_repo"
git -C "$fresh_repo" config core.hooksPath /dev/null
git -C "$fresh_repo" config core.fsmonitor false
git -C "$fresh_repo" config remote.origin.url "$expected_origin"
git -C "$fresh_repo" fetch --quiet --force --prune origin \
  '+refs/heads/main:refs/remotes/origin/main'
if [ -z "$controller_sha" ]; then
  # Fresh provisioning follows the currently verified main tip.  A migration
  # should pass the exact CI-passing 0.9.0 SHA explicitly.
  controller_sha="$(git -C "$fresh_repo" rev-parse refs/remotes/origin/main)"
fi
git -C "$fresh_repo" cat-file -e "$controller_sha^{commit}" 2>/dev/null || {
  echo 'The requested controller commit is not present in the pinned origin.' >&2
  exit 65
}
git -C "$fresh_repo" merge-base --is-ancestor "$controller_sha" refs/remotes/origin/main || {
  echo 'The requested controller commit is not an ancestor of origin/main.' >&2
  exit 65
}
git -c core.hooksPath=/dev/null -C "$fresh_repo" checkout --quiet --detach "$controller_sha"
git -c core.hooksPath=/dev/null -C "$fresh_repo" clean -ffdx >/dev/null
[ -f "$fresh_repo/App/ops/project-snow-release" ] &&
  [ -f "$fresh_repo/App/ops/project-snow-release.sudoers" ] &&
  [ -f "$fresh_repo/App/ops/feedback-mailer.env.example" ] || {
    echo 'The selected main commit does not contain the root-owned release runner.' >&2
    exit 65
  }

runner_tmp="$(mktemp /usr/local/sbin/.project-snow-release.XXXXXX)"
sudoers_tmp="$(mktemp /etc/sudoers.d/.project-snow-release.XXXXXX)"
install -o root -g root -m 0755 "$fresh_repo/App/ops/project-snow-release" "$runner_tmp"
install -o root -g root -m 0440 "$fresh_repo/App/ops/project-snow-release.sudoers" "$sudoers_tmp"
visudo -cf "$sudoers_tmp" >/dev/null
mv -f "$runner_tmp" /usr/local/sbin/project-snow-release
mv -f "$sudoers_tmp" /etc/sudoers.d/project-snow-release

if [ -e "$repo" ] || [ -L "$repo" ]; then
  legacy_repo="/srv/project-snow/backups/repo-before-root-runner-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$repo" "$legacy_repo"
  chown -R root:root "$legacy_repo"
  chmod 0700 "$legacy_repo"
fi
mv "$fresh_repo" "$repo"
fresh_repo=""
chown -R root:root "$repo"
chmod 0700 "$repo"

if [ -e /srv/project-snow/app ] || [ -L /srv/project-snow/app ]; then
  [ -L /srv/project-snow/app ] || {
    echo '/srv/project-snow/app is not a symlink; refusing to replace it.' >&2
    exit 66
  }
  rm -f /srv/project-snow/app
fi
ln -s /srv/project-snow/repo/App /srv/project-snow/app
chown -h root:root /srv/project-snow/app

# Only the inbox and deploy's home remain writable by the SSH account.
for private_root in /srv/project-snow/runtime /srv/project-snow/releases /srv/project-snow/backups/staging; do
  install -o root -g root -m 0700 -d "$private_root"
  chown -R root:root "$private_root"
  chmod 0700 "$private_root"
done
for served_root in /srv/project-snow/data /srv/project-snow/media; do
  install -o root -g root -m 0755 -d "$served_root"
  chown -R root:root "$served_root"
  chmod 0755 "$served_root"
done
install -o root -g root -m 0750 -d /etc/project-snow
install -o root -g root -m 0700 -d /etc/project-snow/secrets /etc/project-snow/cloudflared
secure_existing_file() {
  secure_path="$1"
  [ ! -L "$secure_path" ] && { [ ! -e "$secure_path" ] || [ -f "$secure_path" ]; } || {
    echo "Protected configuration path is not a regular file: $secure_path" >&2
    exit 66
  }
  if [ -e "$secure_path" ] && [ "$(stat -c %h "$secure_path")" -ne 1 ]; then
    echo "Protected configuration file has multiple hard links: $secure_path" >&2
    exit 66
  fi
}
for environment_file in \
  /etc/project-snow/public.env \
  /etc/project-snow/images.env; do
  secure_existing_file "$environment_file"
  touch "$environment_file"
  chown root:root "$environment_file"
  chmod 0600 "$environment_file"
done
secure_existing_file /etc/project-snow/feedback-mailer.env
if [ ! -e /etc/project-snow/feedback-mailer.env ]; then
  install -o root -g root -m 0600 \
    "$repo/App/ops/feedback-mailer.env.example" \
    /etc/project-snow/feedback-mailer.env
fi
chown root:root /etc/project-snow/feedback-mailer.env
chmod 0600 /etc/project-snow/feedback-mailer.env
secure_existing_file /etc/project-snow/secrets/feedback_mailer_database_password
if [ ! -e /etc/project-snow/secrets/feedback_mailer_database_password ]; then
  openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n' \
    > /etc/project-snow/secrets/feedback_mailer_database_password
fi
chown root:root /etc/project-snow/secrets/feedback_mailer_database_password
chmod 0600 /etc/project-snow/secrets/feedback_mailer_database_password
secure_existing_file /etc/project-snow/secrets/feedback_smtp_password
touch /etc/project-snow/secrets/feedback_smtp_password
chown root:root /etc/project-snow/secrets/feedback_smtp_password
chmod 0400 /etc/project-snow/secrets/feedback_smtp_password

if id -nG deploy | tr ' ' '\n' | grep -Fxq docker; then
  gpasswd -d deploy docker >/dev/null
fi
deploy_primary_group="$(id -gn deploy)"
[ "$deploy_primary_group" = deploy ] || {
  echo 'The deploy account must use its dedicated deploy primary group.' >&2
  exit 78
}
for supplemental_group in $(id -nG deploy); do
  [ "$supplemental_group" = "$deploy_primary_group" ] ||
    gpasswd -d deploy "$supplemental_group" >/dev/null
done
if runuser -u deploy -- id -nG | tr ' ' '\n' | grep -Fxq docker; then
  echo 'A fresh deploy login still receives the Docker group.' >&2
  exit 78
fi
if runuser -u deploy -- docker info >/dev/null 2>&1; then
  echo 'The deploy account can still reach the root Docker daemon.' >&2
  exit 78
fi
runuser -u deploy -- sudo -n /usr/local/sbin/project-snow-release status >/dev/null
if runuser -u deploy -- sudo -n /bin/true >/dev/null 2>&1; then
  echo 'The deploy account has an unexpected sudo grant outside the release runner.' >&2
  exit 78
fi

printf '%s\n' "Installed root-owned Project Snow release control at $controller_sha."
printf '%s\n' 'Existing deploy sessions retain their old supplementary groups until logout.'
printf '%s\n' 'Keep Cloudflare Access enabled, disconnect every deploy session, reconnect, and run:'
printf '%s\n' '  id -nG; ! docker info; sudo -n /usr/local/sbin/project-snow-release status'
