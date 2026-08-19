#!/bin/sh
set -eu
export LC_ALL=C

controller_sha=""
if [ "${1:-}" = --controller-sha ]; then
  [ "$#" -eq 2 ] || { echo 'Usage: prepare_debian.sh [--controller-sha <40-character-main-sha>]' >&2; exit 64; }
  controller_sha="$2"
  printf '%s\n' "$controller_sha" | grep -Eq '^[0-9a-f]{40}$' || {
    echo 'The controller SHA must be 40 lowercase hexadecimal characters.' >&2
    exit 64
  }
elif [ "$#" -ne 0 ]; then
  echo 'Usage: prepare_debian.sh [--controller-sha <40-character-main-sha>]' >&2
  exit 64
fi

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root on Debian 13.' >&2
  exit 77
fi

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

for protected_path in \
  /srv/project-snow \
  /srv/project-snow/repo \
  /srv/project-snow/runtime \
  /srv/project-snow/releases \
  /srv/project-snow/data \
  /srv/project-snow/media \
  /srv/project-snow/inbox \
  /etc/project-snow \
  /etc/project-snow/secrets \
  /etc/project-snow/cloudflared; do
  [ ! -L "$protected_path" ] || {
    echo "Protected host path must not be a symlink: $protected_path" >&2
    exit 66
  }
done

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

apt-get update
apt-get install -y ca-certificates curl fail2ban git jq openssl python3 rclone restic rsync sudo ufw unattended-upgrades util-linux
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf '%s\n' "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
install -o root -g root -m 0755 -d /srv/project-snow
install -o root -g root -m 0750 -d /srv/project-snow/repo
install -o root -g root -m 0700 -d \
  /srv/project-snow/runtime \
  /srv/project-snow/releases \
  /srv/project-snow/backups/staging
install -o root -g root -m 0755 -d \
  /srv/project-snow/media/releases \
  /srv/project-snow/media/staging \
  /srv/project-snow/media/stickers/releases \
  /srv/project-snow/media/stickers/staging
install -o root -g root -m 0755 -d /srv/project-snow/data
install -o deploy -g deploy -m 0700 -d /srv/project-snow/inbox
if [ -d /srv/project-snow/app ] && [ ! -L /srv/project-snow/app ] && [ -z "$(find /srv/project-snow/app -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  rmdir /srv/project-snow/app
fi
if [ ! -e /srv/project-snow/app ] && [ ! -L /srv/project-snow/app ]; then
  ln -s /srv/project-snow/repo/App /srv/project-snow/app
  chown -h root:root /srv/project-snow/app
fi
if [ ! -L /srv/project-snow/app ] ||
   [ "$(readlink /srv/project-snow/app)" != /srv/project-snow/repo/App ]; then
  echo '/srv/project-snow/app must be a symlink to /srv/project-snow/repo/App.' >&2
  exit 78
fi
install -o root -g root -m 0750 -d /etc/project-snow
install -o root -g root -m 0700 -d /etc/project-snow/secrets
install -o root -g root -m 0700 -d /etc/project-snow/cloudflared
secure_existing_file /etc/project-snow/public.env
touch /etc/project-snow/public.env
chown root:root /etc/project-snow/public.env
chmod 0600 /etc/project-snow/public.env
secure_existing_file /etc/project-snow/images.env
touch /etc/project-snow/images.env
chown root:root /etc/project-snow/images.env
chmod 0600 /etc/project-snow/images.env
secure_existing_file /etc/project-snow/feedback-mailer.env
if [ ! -e /etc/project-snow/feedback-mailer.env ]; then
  install -o root -g root -m 0600 \
    "$script_dir/feedback-mailer.env.example" \
    /etc/project-snow/feedback-mailer.env
fi
chown root:root /etc/project-snow/feedback-mailer.env
chmod 0600 /etc/project-snow/feedback-mailer.env
secure_existing_file /etc/project-snow/secrets/feedback_mailer_database_password
if [ ! -e /etc/project-snow/secrets/feedback_mailer_database_password ]; then
  umask 077
  openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n' > /etc/project-snow/secrets/feedback_mailer_database_password
fi
chown root:root /etc/project-snow/secrets/feedback_mailer_database_password
chmod 0600 /etc/project-snow/secrets/feedback_mailer_database_password
secure_existing_file /etc/project-snow/secrets/feedback_smtp_password
touch /etc/project-snow/secrets/feedback_smtp_password
chown root:root /etc/project-snow/secrets/feedback_smtp_password
chmod 0400 /etc/project-snow/secrets/feedback_smtp_password

deploy_ssh_directory=/home/deploy/.ssh
deploy_authorized_keys="$deploy_ssh_directory/authorized_keys"
[ ! -L "$deploy_ssh_directory" ] || {
  echo 'Deploy SSH directory must not be a symlink.' >&2
  exit 78
}
install -o deploy -g deploy -m 0700 -d "$deploy_ssh_directory"
if [ -e "$deploy_authorized_keys" ] || [ -L "$deploy_authorized_keys" ]; then
  [ -f "$deploy_authorized_keys" ] && [ ! -L "$deploy_authorized_keys" ] &&
    [ "$(stat -c %h "$deploy_authorized_keys")" -eq 1 ] || {
      echo 'Deploy authorized_keys must be a single regular file.' >&2
      exit 78
    }
fi
if [ ! -s "$deploy_authorized_keys" ]; then
  [ -s /root/.ssh/authorized_keys ] && [ -f /root/.ssh/authorized_keys ] &&
    [ ! -L /root/.ssh/authorized_keys ] &&
    [ "$(stat -c %h /root/.ssh/authorized_keys)" -eq 1 ] &&
    [ "$(stat -c %u /root/.ssh/authorized_keys)" -eq 0 ] &&
    [ -z "$(find /root/.ssh/authorized_keys -maxdepth 0 -perm /022 -print -quit)" ] || {
      echo 'No safe non-empty authorized_keys source is available for deploy.' >&2
      exit 78
    }
  install -o deploy -g deploy -m 0600 /root/.ssh/authorized_keys "$deploy_authorized_keys"
else
  # Preserve the already-working deployment key set during migration; only
  # normalize its ownership and mode before hardening SSH.
  chown deploy:deploy "$deploy_authorized_keys"
  chmod 0600 "$deploy_authorized_keys"
fi
if [ ! -s "$deploy_authorized_keys" ]; then
  echo 'Refusing to disable root SSH before deploy has a non-empty authorized_keys file.' >&2
  exit 78
fi

[ ! -L /etc/docker ] || { echo '/etc/docker must not be a symlink.' >&2; exit 66; }
install -m 0755 -d /etc/docker
secure_existing_file /etc/docker/daemon.json
secure_existing_file /etc/sysctl.d/60-project-snow.conf
cp "$script_dir/docker-daemon.json" /etc/docker/daemon.json
cp "$script_dir/sysctl-project-snow.conf" /etc/sysctl.d/60-project-snow.conf
sysctl --system
systemctl enable docker
systemctl restart docker

current_effective_ports="$(sshd -T -C user=deploy,host=localhost,addr=127.0.0.1 | sed -n 's/^port //p')"
unexpected_43556_ports="$(printf '%s\n' "$current_effective_ports" | grep -vx '43556' || true)"
if [ -n "$current_effective_ports" ] && [ -z "$unexpected_43556_ports" ]; then
  # A previous managed drop-in may have repeated the same safe port.  Omitting
  # it here lets replacement of that drop-in self-heal the duplicate.
  write_ssh_port=0
elif [ "$current_effective_ports" = 22 ]; then
  # Debian's stock configuration exposes only the implicit default port.
  write_ssh_port=1
else
  echo 'Existing SSH configuration exposes an unexpected or mixed port set.' >&2
  exit 78
fi
ssh_hardening_tmp="$(mktemp /etc/ssh/sshd_config.d/.60-project-snow.XXXXXX)"
: > "$ssh_hardening_tmp"
if [ "$write_ssh_port" -eq 1 ]; then
  printf '%s\n' 'Port 43556' >> "$ssh_hardening_tmp"
fi
cat >> "$ssh_hardening_tmp" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
MaxSessions 4
AllowUsers deploy
AllowTcpForwarding yes
X11Forwarding no
EOF
chmod 0600 "$ssh_hardening_tmp"
previous_ssh_config=""
if [ -f /etc/ssh/sshd_config.d/60-project-snow.conf ]; then
  previous_ssh_config="$(mktemp /etc/ssh/sshd_config.d/.60-project-snow.previous.XXXXXX)"
  cp /etc/ssh/sshd_config.d/60-project-snow.conf "$previous_ssh_config"
fi
mv "$ssh_hardening_tmp" /etc/ssh/sshd_config.d/60-project-snow.conf
validate_effective_sshd() {
  effective_sshd="$(sshd -T -C user=deploy,host=localhost,addr=127.0.0.1)" || return 1
  for required_setting in \
    'passwordauthentication no' \
    'kbdinteractiveauthentication no' \
    'permitrootlogin no' \
    'pubkeyauthentication yes' \
    'maxauthtries 3' \
    'maxsessions 4' \
    'allowtcpforwarding yes' \
    'x11forwarding no' \
    'allowusers deploy'; do
    printf '%s\n' "$effective_sshd" | grep -Fxq "$required_setting" || return 1
  done
  effective_ports="$(printf '%s\n' "$effective_sshd" | sed -n 's/^port //p')"
  [ "$effective_ports" = 43556 ] || return 1
  effective_root_sshd="$(sshd -T -C user=root,host=localhost,addr=127.0.0.1)" || return 1
  printf '%s\n' "$effective_root_sshd" | grep -Fxq 'permitrootlogin no' &&
    printf '%s\n' "$effective_root_sshd" | grep -Fxq 'passwordauthentication no' &&
    printf '%s\n' "$effective_root_sshd" | grep -Fxq 'kbdinteractiveauthentication no'
}
if ! sshd -t || ! validate_effective_sshd; then
  if [ -n "$previous_ssh_config" ]; then
    mv "$previous_ssh_config" /etc/ssh/sshd_config.d/60-project-snow.conf
  else
    rm -f /etc/ssh/sshd_config.d/60-project-snow.conf
  fi
  echo 'SSH hardening syntax/effective-policy validation failed; previous configuration was restored.' >&2
  exit 78
fi
systemctl reload ssh
if ! ss -ltnH '( sport = :43556 )' | grep -q .; then
  if [ -n "$previous_ssh_config" ]; then
    mv "$previous_ssh_config" /etc/ssh/sshd_config.d/60-project-snow.conf
  else
    rm -f /etc/ssh/sshd_config.d/60-project-snow.conf
  fi
  systemctl reload ssh || true
  echo 'SSH did not listen on port 43556; previous configuration was restored before firewall changes.' >&2
  exit 78
fi
if [ -n "$previous_ssh_config" ]; then
  rm -f "$previous_ssh_config"
fi

ufw default deny incoming
ufw default allow outgoing
ufw allow 43556/tcp
ufw logging low
ufw --force enable
ufw status | grep -Fx 'Status: active' >/dev/null

secure_existing_file /etc/fail2ban/jail.d/project-snow-sshd.local
cat > /etc/fail2ban/jail.d/project-snow-sshd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
port = 43556
maxretry = 4
findtime = 10m
bantime = 1h
EOF
systemctl enable --now fail2ban
fail2ban-client status sshd >/dev/null

dpkg-reconfigure -f noninteractive unattended-upgrades
if [ -n "$controller_sha" ]; then
  "$script_dir/bootstrap-release-runner.sh" --controller-sha "$controller_sha"
else
  "$script_dir/bootstrap-release-runner.sh"
fi
echo 'Preparation complete. Keep this SSH session open and verify a second deploy login before disconnecting.'
echo 'Populate root-only secrets and the dedicated feedback-mailer.env, configure Cloudflare Access/Tunnel, then deploy behind private acceptance.'
