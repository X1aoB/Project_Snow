#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run as root on Debian 13.' >&2
  exit 77
fi

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

apt-get update
apt-get install -y ca-certificates curl git jq rclone restic rsync ufw unattended-upgrades
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf '%s\n' "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod -aG docker deploy
install -o deploy -g deploy -m 0750 -d /srv/project-snow/repo /srv/project-snow/runtime /srv/project-snow/releases /srv/project-snow/backups/staging /srv/project-snow/media/releases /srv/project-snow/media/staging
install -o deploy -g deploy -m 0755 -d /srv/project-snow/data
if [ -d /srv/project-snow/app ] && [ ! -L /srv/project-snow/app ] && [ -z "$(find /srv/project-snow/app -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  rmdir /srv/project-snow/app
fi
if [ ! -e /srv/project-snow/app ] && [ ! -L /srv/project-snow/app ]; then
  ln -s /srv/project-snow/repo/App /srv/project-snow/app
  chown -h deploy:deploy /srv/project-snow/app
fi
if [ ! -L /srv/project-snow/app ]; then
  echo '/srv/project-snow/app must be a symlink to /srv/project-snow/repo/App.' >&2
  exit 78
fi
install -o root -g deploy -m 0750 -d /etc/project-snow
install -o root -g root -m 0700 -d /etc/project-snow/secrets
install -o root -g root -m 0700 -d /etc/project-snow/cloudflared
touch /etc/project-snow/public.env
chown root:deploy /etc/project-snow/public.env
chmod 0640 /etc/project-snow/public.env
touch /etc/project-snow/images.env
chown root:deploy /etc/project-snow/images.env
chmod 0640 /etc/project-snow/images.env

if [ -f /root/.ssh/authorized_keys ]; then
  install -o deploy -g deploy -m 0700 -d /home/deploy/.ssh
  install -o deploy -g deploy -m 0600 /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
fi

install -m 0755 -d /etc/docker
cp "$script_dir/docker-daemon.json" /etc/docker/daemon.json
cp "$script_dir/sysctl-project-snow.conf" /etc/sysctl.d/60-project-snow.conf
sysctl --system
systemctl enable docker
systemctl restart docker

ufw default deny incoming
ufw default allow outgoing
ufw allow 43556/tcp
ufw --force enable

cat > /etc/ssh/sshd_config.d/60-project-snow.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
MaxAuthTries 3
AllowTcpForwarding yes
X11Forwarding no
EOF
sshd -t
systemctl reload ssh

dpkg-reconfigure -f noninteractive unattended-upgrades
echo 'Preparation complete. Populate root-only secrets, configure Cloudflare Access/Tunnel, then deploy behind private acceptance.'
