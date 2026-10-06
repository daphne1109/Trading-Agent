#!/usr/bin/env bash
# One-time setup of a fresh Debian 12 / Ubuntu 24.04 VM (GCP e2-micro) for the agent.
# Usage (on the VM):  curl -fsSL <raw url of this file> | bash   — or copy it over and run it.
# Idempotent: safe to run twice.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/daphne1109/Trading-Agent.git}"
APP_DIR="${APP_DIR:-$HOME/Trading-Agent}"

echo "==> Installing Docker Engine + compose plugin"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

echo "==> 2 GB swap file (the e2-micro has 1 GB RAM)"
if ! sudo swapon --show | grep -q /swapfile; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
  sudo sysctl --system >/dev/null
fi

echo "==> Unattended security updates"
sudo apt-get update -y
sudo apt-get install -y git unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Cloning $REPO_URL"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Nightly Postgres backup at 03:00 UTC (keeps 7)"
mkdir -p "$HOME/backups"
CRON_LINE="0 3 * * * cd $APP_DIR && ./scripts/backup.sh >> $HOME/backups/backup.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'scripts/backup.sh' ; echo "$CRON_LINE" ) | crontab -

cat <<EOF

Done. Next steps:
  1. Log out and back in (so your user can run docker without sudo).
  2. Copy your .env to $APP_DIR/.env from your laptop:
       gcloud compute scp .env <vm-name>:$APP_DIR/.env --zone <zone>
     then on the VM:  chmod 600 $APP_DIR/.env
  3. Start it:  cd $APP_DIR && ./scripts/deploy.sh
EOF
