# Deploying to Google Cloud (free e2-micro)

About 20 minutes. The 👤 steps happen in your browser or terminal with your own account; nothing here needs a password typed into a script.

## 1. Create the VM 👤
1. Go to [console.cloud.google.com](https://console.cloud.google.com), select (or create) a project, and make sure billing is enabled.
2. **Billing → Budgets & alerts → Create budget**: $1, with alerts at 50% and 100%. This is your safety net.
3. **Compute Engine → VM instances → Create instance**:
   | Field | Value | Why |
   |---|---|---|
   | Name | `deriv-agent` | |
   | Region | `us-central1` (or `us-west1` / `us-east1`) | Only these are in the free tier |
   | Machine type | `e2-micro` | Free tier |
   | Boot disk | Debian 12, **Standard persistent disk**, 30 GB | Free tier allows 30 GB standard |
   | Firewall | leave HTTP/HTTPS **unticked** for now | Nothing is public until the dashboard (P4) |
4. Leave the external IP as **ephemeral** (a static IP costs money). The agent only makes outbound connections.

Or from your terminal with the gcloud CLI:
```bash
gcloud compute instances create deriv-agent --zone us-central1-a --machine-type e2-micro --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-standard
```

## 2. Set it up
```bash
gcloud compute ssh deriv-agent --zone us-central1-a
```
On the VM:
```bash
curl -fsSL https://raw.githubusercontent.com/daphne1109/Trading-Agent/main/scripts/vm_setup.sh | bash
exit
```
(Log out so the docker group membership takes effect.)

## 3. Copy your secrets 👤
From your laptop, in the project folder:
```bash
gcloud compute scp .env deriv-agent:~/Trading-Agent/.env --zone us-central1-a
```

## 4. Start the agent
```bash
gcloud compute ssh deriv-agent --zone us-central1-a
cd ~/Trading-Agent && ./scripts/deploy.sh
```
Watch the logs until you see `ws_connected`, then (after ~2 minutes of price history) decision rounds:
```bash
docker compose logs -f agent
```

## 5. Check it's healthy
```bash
docker compose exec postgres psql -U agent -c "select value from agent_state where key='heartbeat';"
docker compose exec postgres psql -U agent -c "select created_at, action, confidence, skipped_reason from decisions order by id desc limit 5;"
free -m && df -h /
```

## Day-to-day
| Task | Command |
|---|---|
| Deploy latest `main` | `./scripts/deploy.sh` |
| Stop trading now | `docker compose stop agent` (or set `KILL_SWITCH=1` in `.env` and redeploy) |
| Logs | `docker compose logs -f agent` |
| Manual backup | `./scripts/backup.sh` |
| Restore a backup | `gunzip -c ~/backups/<file>.sql.gz \| docker compose exec -T postgres psql -U agent -d agent` |
