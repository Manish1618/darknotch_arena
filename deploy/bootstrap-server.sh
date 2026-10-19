#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu (22.04/24.04) or Debian (12) server
# for Darkn0tch Arena. Run it yourself on the server:
#
#   sudo bash bootstrap-server.sh              # Docker, firewall, fail2ban, updates
#   sudo KEYS_ONLY=1 bash bootstrap-server.sh  # later, once your SSH key works: turn off password logins
#
# It installs Docker from Docker's own apt repository, lets your user run
# docker without sudo, opens only SSH/80/443 in the firewall, bans repeated
# SSH password guessing, turns on automatic security updates and creates
# /opt/darkn0tch-arena for the app.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo."; exit 1; }
DEPLOY_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo "This script supports Ubuntu and Debian only (found $ID)."; exit 1 ;; esac

if [ "${KEYS_ONLY:-0}" = "1" ]; then
  # Only after you've confirmed `ssh -i <key> user@server` works without a password.
  install -d /etc/ssh/sshd_config.d
  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/90-keys-only.conf
  sshd -t && systemctl reload ssh 2>/dev/null || systemctl reload sshd
  echo "Password logins are off. Keep a second terminal open until you've checked you can still log in."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades rsync openssl

# Docker Engine + Compose plugin from Docker's repository
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker "$DEPLOY_USER"

# Containers reach the internet (package installs, Google sign-in, email) and
# players reach Caddy only while IP forwarding is on. Docker turns it on when
# it starts, but a system update that restarts networking can turn it off
# again, so keep it on for good: at boot and whenever networkd reconfigures.
printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/99-zz-docker-forwarding.conf
for f in /run/systemd/network/*.network; do
  [ -e "$f" ] || continue
  d="/etc/systemd/network/$(basename "$f").d"
  install -d "$d"
  printf '[Network]\nIPForward=yes\n' > "$d/docker-forwarding.conf"
done
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# Firewall: only SSH, HTTP and HTTPS (TCP + UDP for HTTP/3) come in.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# Ban IPs that keep failing SSH logins; install security updates automatically.
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/darkn0tch-arena

echo
echo "Done. Log out and back in so '$DEPLOY_USER' can use docker without sudo."
echo "Check with:  docker run --rm hello-world   and   docker compose version"
