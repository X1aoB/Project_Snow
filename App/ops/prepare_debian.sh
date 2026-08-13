#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root on Debian 13.' >&2
  exit 77
fi

apt-get update
apt-get install -y ca-certificates curl git jq restic rsync ufw unattended-upgrades
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf '%s\n' "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -o deploy -g deploy -m 0750 -d /srv/project-snow/app /srv/project-snow/data /srv/project-snow/runtime /srv/project-snow/releases /srv/project-snow/backups/staging
install -o root -g root -m 0700 -d /etc/project-snow/secrets
install -o root -g root -m 0700 -d /etc/project-snow/cloudflared
touch /etc/project-snow/public.env
chmod 0600 /etc/project-snow/public.env

install -m 0755 -d /etc/docker
cp /srv/project-snow/app/ops/docker-daemon.json /etc/docker/daemon.json
systemctl enable --now docker

ufw default deny incoming
ufw default allow outgoing
ufw allow 43556/tcp
ufw --force enable

sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sshd -t
systemctl reload ssh

dpkg-reconfigure -f noninteractive unattended-upgrades
echo 'Preparation complete. Populate root-only secrets, configure Cloudflare Access/Tunnel, then deploy behind private acceptance.'
