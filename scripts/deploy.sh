#!/usr/bin/env bash
# Deploy the latest main on the VM: pull, rebuild, restart, show status.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Missing .env (copy it from your laptop first; see docs/deploy.md)" >&2
  exit 1
fi
chmod 600 .env

git fetch --quiet origin main
git reset --hard origin/main            # the VM never carries local edits
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
sleep 5
docker compose ps
echo
echo "Recent agent logs:"
docker compose logs --tail 30 agent
