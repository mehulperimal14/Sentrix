# SENTRIX Portal deployment (Oracle Always Free, India)

The Portal (`sentrix_portal`) on one small VM: **Caddy** (HTTPS, the only
published service), the **Portal** (one process, one worker: the device link
hub keeps its sockets in memory) and **Postgres 16** on an internal Docker
network with no published port (decision D11; reviews: a published Docker port
bypasses ufw/iptables). Test mode stays on this semester (D13).

| File | What it is |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | `python:3.12-slim`, non-root user `sentrix` (uid 10001), only `sentrix/`, `sentrix_portal/`, two tools and `requirements.txt` in the build context: `.env`, `var/`, `models/` never reach the daemon |
| `requirements.txt` | the Portal's dependencies only (no torch, OpenCV or face models) + `psycopg[binary]` |
| `docker-compose.yml` | `db` (internal network `backend` only), `portal` (`backend` + `frontend`, no ports), `caddy` (ports 80/443, fixed address 172.30.0.10, the only trusted proxy) |
| `Caddyfile` | automatic Let's Encrypt, HSTS (1 year), WebSockets (`/device/v1/link`, `/d/{id}/ws/...`), unbuffered MJPEG relay (`flush_interval -1`), 12 MB request cap |
| `.env.example` | variable **names** (and safe defaults); copy to `.env` on the VM, never commit it |
| `backup.sh`, `restore_test.sh` | nightly `pg_dump` + the key files; a restore into a scratch database |
| `check_ports.sh` | an `nmap` from outside: only 80/443 (and 22 if you kept it) may answer |

Nothing here was run against Docker on the laptop; the files are checked by
`tests/test_deploy_portal.py` (structure, ports, networks, non-root, names-only
env) and the models by `tools/portal_pg_check.py` (below).

## 1. Oracle Cloud account and the ₹0 guard

1. Sign up at cloud.oracle.com with **home region Mumbai (ap-mumbai-1)** or
   Hyderabad (ap-hyderabad-1). The home region cannot be changed later, and
   Always Free resources exist only there. Data then stays in India (the
   privacy notice says "stored in India; messages transit Telegram and the
   e-mail provider").
2. A Visa/Mastercard credit card is needed for verification (RuPay and
   prepaid cards are commonly rejected). Decide whose card it is in week 0.
3. **Budget alert at ₹0**: Billing & Cost Management → Budgets → Create
   budget, scope the tenancy, amount ₹1, alert at 1 % actual spend to two team
   e-mails. Optionally upgrade to pay-as-you-go (Always Free stays free; an
   idle Always Free VM is no longer reclaimed after 7 days below 20 % CPU,
   network and memory). Without the upgrade, keep real load on it.
4. Compute → Instances → Create: **Ampere A1 Flex, 1 OCPU / 6 GB** (Always
   Free eligible), Canonical Ubuntu 24.04 (aarch64), a public IPv4, your SSH
   key. "Out of host capacity" in Hyderabad: retry, or another availability
   domain.

## 2. Network: 80/443 only

* **Security list** of the VM's subnet (Networking → VCN → Security Lists):
  ingress **TCP 80 and 443 from 0.0.0.0/0**; nothing else from the Internet.
  SSH: either **TCP 22 from the team's own IP /32 only**, or no port 22 at all
  and the OCI Bastion service / Cloud Shell. Delete the default
  "0.0.0.0/0 port 22" rule. Egress: all (Telegram/Brevo APIs, Let's Encrypt).
* The Ubuntu image also ships iptables rules that block 80/443: open them
  (`sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT`
  and `sudo netfilter-persistent save`).
* Docker publishes **only Caddy's** 80/443; Postgres has no `ports:` and sits on
  the `internal: true` network, which has no route out either.
* After the first start, run `./check_ports.sh <domain>` **from outside** (a
  phone hotspot laptop): only 80 and 443 (and 22 if kept) may be open.

## 3. Domain and DNS

Buy a **.in** name (≈ ₹500–800/year; DuckDNS and other dynamic-DNS domains are
often blocked by campus filters). Add an **A record** `portal.<yourdomain>.in`
→ the VM's public IP (TTL 300). Test it from the demo room's network in week 1.
Caddy gets the certificate on first start (port 80 must be reachable for the
HTTP-01 challenge); renewal is automatic.

## 4. Install and start

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git nmap
sudo usermod -aG docker $USER && newgrp docker
git clone <the repository> /opt/sentrix && cd /opt/sentrix/deploy/portal
cp .env.example .env && chmod 600 .env
# fill in PORTAL_DOMAIN, ACME_EMAIL, POSTGRES_PASSWORD, PORTAL_SECRET_KEY,
# PORTAL_DATA_KEY (python3 -c "import secrets; print(secrets.token_hex(32))")
docker compose up -d --build
docker compose ps                     # db healthy, portal healthy, caddy up
curl -s https://portal.<yourdomain>.in/healthz
```

Then create the company admin inside the container (prints the activation code
once; TOTP is set up at the first sign-in) and provision devices (the identity
file goes to the data volume, then onto the device's SD card as
`/etc/sentrix/identity.json`):

```bash
docker compose exec portal python tools/provision_device.py admin --username admin1
docker compose exec portal python tools/provision_device.py device     --out /data/portal/exports/SX5-000001 --label /data/portal/exports/SX5-000001/label.html
docker compose cp portal:/data/portal/exports/SX5-000001 ./SX5-000001   # then delete it there
```

Devices connect to `wss://portal.<yourdomain>.in/device/v1/link`: on the device
set `SENTRIX_LINK_ENABLED=1` and `SENTRIX_PORTAL_URL=https://portal.<yourdomain>.in`
(`deploy/pi5/pi.env` lists both). Telegram in production: `SENTRIX_TELEGRAM_MODE=webhook`,
the webhook is `https://portal.<yourdomain>.in/hooks/telegram` with
`TELEGRAM_WEBHOOK_SECRET`.

**Keys.** `PORTAL_SECRET_KEY` signs every session, code, device token and the
audit chain; `PORTAL_DATA_KEY` encrypts phones, medical notes and TOTP secrets.
Losing either strands every device and login: keep a copy in the team's
password manager. They are required from the environment when
`PORTAL_TEST_MODE=0`; in test mode the Portal can generate them into the
`portal-data` volume instead (then `backup.sh` must run).

**Updates:** `git pull && docker compose up -d --build`. The schema is created
and extended additively at start (`Database.init`); an Alembic baseline is
still to do (docs/7 §16 q7) before any column is renamed or dropped.

## 5. Backups and the restore test

```bash
sudo mkdir -p /var/backups/sentrix && sudo chown $USER /var/backups/sentrix
./backup.sh                                    # pg_dump -Fc + the key files
crontab -e   # 15 2 * * * /opt/sentrix/deploy/portal/backup.sh >> /var/backups/sentrix/backup.log 2>&1
./restore_test.sh                              # restores the newest dump into a scratch DB
```

* Nightly custom-format dump plus a tar of the `portal-data` volume (key files),
  14 days kept on the VM's block volume (encrypted at rest by OCI).
* **Weekly off-VM copy**, encrypted: `gpg --symmetric --cipher-algo AES256`
  the newest dump and data tar and copy them to a team drive. A dump without
  the keys restores rows nobody can sign in to.
* **Restore test** weekly and after every schema change: `restore_test.sh`
  loads the dump into `restore_check`, compares row counts of the key tables
  with the live database and drops it. Record the date of the last passing
  test in the gate report (P6).
* Full restore on a new VM: `docker compose up -d db`, then
  `docker compose exec -T db pg_restore -U sentrix -d sentrix --clean < portal-<stamp>.dump`,
  restore the data tar into the `portal-data` volume, then start the rest.

## 6. Fallback: Render + Supabase

If the Oracle VM fails (card rejected, capacity, reclamation):

* **Supabase** free project in **Mumbai (ap-south-1)**: copy the
  "Session pooler" connection string into `PORTAL_DATABASE_URL` as
  `postgresql+psycopg://...` (`?sslmode=require`). It pauses after 7 idle days;
  the device heartbeats keep it awake once a device is linked.
  **Never Render's free Postgres** (deleted after 30 days).
* **Render** free web service from this repository: Docker runtime,
  Dockerfile path `deploy/portal/Dockerfile`, environment from `.env.example`
  plus `PORTAL_DATABASE_URL` (Supabase), `PORTAL_BASE_URL=https://<service>.onrender.com`
  and `PORTAL_SECURE_COOKIES=1`. Render terminates TLS itself (no Caddy) and
  proxies WebSockets. Leave `PORTAL_TRUSTED_PROXIES` / `FORWARDED_ALLOW_IPS` at
  their defaults (never `*`): login back-off then counts Render's proxy
  address, which is acceptable for a demo fallback.
* It sleeps after 15 min without inbound traffic and takes about a minute to
  wake: open it 5 minutes before the demo.
* **E-mail must use an HTTPS API** (`SENTRIX_EMAIL_PROVIDER=brevo` or `resend`):
  Render blocks outbound SMTP ports 25, 465 and 587. The Portal never uses
  SMTP anyway.
* Rehearse the fallback once (W5), with the device's `SENTRIX_PORTAL_URL`
  pointed at it.

## 7. Postgres compatibility

`python tools/portal_pg_check.py` (on the laptop, no server needed):

* `ddl`: every table and index compiles for PostgreSQL, and the outbox's
  `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` too;
* `suite`: the Portal tests run with a **VARCHAR length guard** (SQLite ignores
  `String(n)`, Postgres rejects longer values);
* `live --url postgresql+psycopg://user:pass@host/db`: tables in a throwaway
  schema on a real server, a few writes and the audit chain, then the schema is
  dropped. Needs `pip install "psycopg[binary]"` (not in the laptop venv); run it
  once against the VM's database with `docker compose exec portal python
  tools/portal_pg_check.py live --url "$PORTAL_DATABASE_URL"` (the image has
  the driver), or against a local Postgres.

Known differences handled in the code: timestamps are `timestamp with time
zone` (SQLite returns naive values; `accounts.as_utc` normalises both), the
outbox dedupe uses the dialect's own `ON CONFLICT`, and `ilike` is portable.
