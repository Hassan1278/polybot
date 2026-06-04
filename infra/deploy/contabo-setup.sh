#!/usr/bin/env bash
# Idempotent first-boot script for a fresh Ubuntu 24.04 VPS. Run as root.
# Review BEFORE piping into bash.

set -euo pipefail

apt-get update && apt-get upgrade -y
apt-get install -y \
    ca-certificates curl gnupg lsb-release \
    git ufw fail2ban tmux htop jq unattended-upgrades

# 1. unattended security upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

# 2. firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 3. fail2ban defaults are fine
systemctl enable --now fail2ban

# 4. docker
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# 5. SSH hardening (DISABLE password login — make sure your key works first)
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh

# 6. swap (Contabo VPS L has 16G RAM, no swap by default)
if ! swapon --show | grep -q swap; then
    fallocate -l 4G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# 7. project dir
mkdir -p /opt/polybot
chown -R "$SUDO_USER:$SUDO_USER" /opt/polybot 2>/dev/null || true

# 8. nightly backup cron
cat > /etc/cron.daily/polybot-pg-backup <<'CRON'
#!/usr/bin/env bash
set -e
mkdir -p /var/backups/polybot
docker exec polybot-postgres pg_dump -U polybot polybot \
  | gzip > "/var/backups/polybot/pg-$(date +%F).sql.gz"
find /var/backups/polybot -mtime +14 -delete
CRON
chmod +x /etc/cron.daily/polybot-pg-backup

echo
echo "DONE. Next:"
echo "  git clone <repo> /opt/polybot"
echo "  cd /opt/polybot && cp .env.example .env && \$EDITOR .env"
echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d"
