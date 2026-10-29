# Panel deploy on Google Cloud (Ubuntu VM)

## 0. What you need
- VM in GCloud (e2-micro is enough for hundreds of rigs: panel is I/O-light).
- **Static external IP** (VPC network → IP addresses → reserve, attach to VM).
- A DNS **A-record** `panel.yourdomain.com` → that static IP.
- Ports 80/443 open (see step 2).

## 1. Firewall
```bash
gcloud compute firewall-rules create pearl-panel-web \
  --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags pearl-panel
gcloud compute instances add-tags INSTANCE_NAME --tags pearl-panel --zone ZONE
```

## 2. Deploy (one command)
```bash
ssh into the VM, then:
sudo apt-get update -qq && sudo apt-get install -y git
git clone <your repo> pearl && cd pearl
sudo bash deploy/panel/deploy.sh      # asks domain + admin password
```
Caddy obtains a Let's Encrypt cert automatically; panel is at
`https://panel.yourdomain.com`.

Docker-free alternative: see `pearl-panel.service` (systemd) in this folder.

## 3. First login
1. Open the URL → password from `.env`.
2. Copy the **enroll key** shown in the header.
3. Upload the miner bundle (`dist\bundle-<ver>.zip`) → «Загрузить» → «Развернуть».
4. Upload the rig installer (`fleet-rig.zip`) in «Установщик рига» — rigs download
   it from the panel.

## 4. Put a rig online
On the rig (admin PowerShell):
```powershell
# download and unpack the installer
Invoke-WebRequest https://panel.yourdomain.com/api/admin/rigpkg -OutFile rig.zip   # (admin cookie; or copy dist\fleet-rig)
Expand-Archive rig.zip C:\rig -Force
cd C:\rig\rig-agent
# agent.json: panel_url + enroll_key (+ rig_name)
powershell -ExecutionPolicy Bypass -File install_task.ps1
```
The agent enrolls, reports GPUs/AVs every 30 s, starts the miner, and applies
panel changes (pool, wallet, %, version, restart) without a reboot.

## 5. Operations
```bash
docker compose logs -f panel     # logs
docker compose restart panel     # restart
# backup: everything is in the panel-data volume
docker run --rm -v panel_panel-data:/d -v $PWD:/b alpine tar czf /b/panel-backup.tgz -C /d .
```
`/healthz` is exposed for uptime monitors.

## Notes
- Bundles and the rig package live in the volume — `docker compose down` is safe;
  `down -v` deletes them.
- One uvicorn worker is intentional (SQLite). For very large fleets move to
  Postgres later; the schema is plain SQL.
- Behind Cloudflare/another proxy set `--forwarded-allow-ips` accordingly
  (already `*` in compose; tighten if the panel is internet-exposed).
