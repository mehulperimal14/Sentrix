# SENTRIX Portal deployment (Oracle Always Free, India)

The Portal (`sentrix_portal`: accounts, onboarding, SOS, Desk, admin console
and the device link hub) on one small VM, as three containers:

* **Caddy** — HTTPS with Let's Encrypt, the only service with published
  ports (80/443);
* **Portal** — one process with one uvicorn worker (the link hub keeps the
  device sockets in memory); `PORTAL_WORKERS=1` starts its background workers
  (heartbeat watcher, cloud SOS timers, notification worker, retention);
* **Postgres 16** — on an internal Docker network with no published port and
  no route out.

Decisions: D11 (hosting), D13 (test mode is the only mode this semester).
Contract: [`docs/7_PORTAL_AND_SOS.md`](../../docs/7_PORTAL_AND_SOS.md). Device side:
[`deploy/pi5/README.md`](../pi5/README.md).

**Status:** none of these files has been run against Docker yet. They are
checked statically by `tests/test_deploy_portal.py` (structure, ports,
networks, non-root, names-only env), and the database models by
`tools/portal_pg_check.py` (§8; `ddl` passes, no live Postgres run yet). The
Portal itself has run on the laptop on SQLite, linked to a real device process
(docs/7 §17).

## Files

| File | What it is |
|---|---|
| `Dockerfile` (+ `Dockerfile.dockerignore`) | `python:3.12-slim`, non-root user `sentrix` (uid 10001); the build context holds only `sentrix/`, `sentrix_portal/`, `tools/provision_device.py`, `tools/portal_pg_check.py` and `requirements.txt`, so `.env`, `var/`, `models/` never reach the daemon. Health check on `/healthz` |
| `requirements.txt` | the Portal's dependencies only (no torch, OpenCV or face models) + `psycopg[binary]` |
| `docker-compose.yml` | `db` (internal network `backend` only), `portal` (`backend` + `frontend`, no ports), `caddy` (ports 80/443, fixed address 172.30.0.10, the only trusted proxy) |
| `Caddyfile` | automatic HTTPS, HSTS (1 year), WebSockets (`/device/v1/link`, `/d/{id}/ws/...`), unbuffered MJPEG relay (`flush_interval -1`), 12 MB request cap |
| `.env.example` | variable **names** and safe defaults; copy to `.env` on the VM, never commit it |
| `backup.sh`, `restore_test.sh` | nightly `pg_dump` + the key files; a restore into a scratch database |
| `check_ports.sh` | an `nmap` from outside: only 80/443 (and 22 if you kept it) may answer |

## 1. Accounts to create first (W0)

| What | Why |
|---|---|
| Oracle Cloud account, home region **Mumbai (ap-mumbai-1)** or Hyderabad (ap-hyderabad-1) | Always Free resources exist only in the home region, which cannot be changed later. Data stays in India |
| A Visa/Mastercard credit card | Needed for verification (RuPay and prepaid cards are commonly rejected). Decide whose card it is in week 0 |
| A **.in** domain (≈ ₹500–800/year) | DuckDNS and other dynamic-DNS names are often blocked by campus filters |
| A Telegram bot (@BotFather) | `TELEGRAM_BOT_TOKEN`, `PORTAL_TELEGRAM_BOT_NAME` |
| Brevo or Resend account | e-mail over an **HTTPS API** (`SENTRIX_EMAIL_PROVIDER`); the Portal never uses SMTP |

## 2. The ₹0 guard

Billing & Cost Management → Budgets → Create budget: scope the tenancy, amount
₹1, alert at 1 % actual spend to two team e-mails. Optionally upgrade to
pay-as-you-go (Always Free stays free, and an idle Always Free VM is then no
longer reclaimed after 7 days below 20 % CPU, network and memory). Without the
upgrade, keep real load on it.

## 3. Create the VM

Compute → Instances → Create: **Ampere A1 Flex, 1 OCPU / 6 GB** (Always Free
eligible), Canonical Ubuntu 24.04 (aarch64), a public IPv4, your SSH key.
"Out of host capacity": retry, or another availability domain.

## 4. Network: 80 and 443 only

1. **Security list** of the VM's subnet (Networking → VCN → Security Lists):
   ingress **TCP 80 and 443 from 0.0.0.0/0**, nothing else from the
   Internet. SSH: either **TCP 22 from the team's own IP /32 only**, or no
   port 22 at all and the OCI Bastion service / Cloud Shell. Delete the
   default "0.0.0.0/0 port 22" rule. Egress: all (Telegram and e-mail APIs,
   Let's Encrypt).
2. The Ubuntu image ships iptables rules that block 80/443. Open them:
   ```bash
   sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443 -j ACCEPT
   sudo netfilter-persistent save
   ```
3. Docker publishes **only Caddy's** 80/443. Postgres has no `ports:` and sits
   on the `internal: true` network. Do not add a `ports:` line to `db` or
   `portal`: a published Docker port bypasses ufw/iptables.

## 5. DNS

Add an **A record** `portal.<yourdomain>.in` → the VM's public IP (TTL 300).
Test that it resolves from the demo room's network and from a phone on 4G in
week 1. Caddy gets the certificate on first start (port 80 must be reachable
for the HTTP-01 challenge); renewal is automatic.

## 6. Install and start

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git nmap
sudo usermod -aG docker $USER && newgrp docker
sudo git clone <the repository> /opt/sentrix && sudo chown -R $USER /opt/sentrix
cd /opt/sentrix/deploy/portal
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # run twice: PORTAL_SECRET_KEY, PORTAL_DATA_KEY
nano .env
```

Fill in at least:

| Variable | Value |
|---|---|
| `PORTAL_DOMAIN` | `portal.<yourdomain>.in` |
| `ACME_EMAIL` | a team e-mail for Let's Encrypt |
| `POSTGRES_PASSWORD` | a long random string |
| `PORTAL_SECRET_KEY`, `PORTAL_DATA_KEY` | the two generated keys |
| `PORTAL_DESK_NUMBER` | the simulated Desk's number (shown instead of 112 in test mode) |
| `SENTRIX_TEST_RECIPIENTS` | the team phones and e-mails allowed to receive test messages |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `PORTAL_TELEGRAM_BOT_NAME` | from step 1; webhook mode is the default here |
| `SENTRIX_EMAIL_PROVIDER` (`brevo` or `resend`), its API key, `SENTRIX_EMAIL_FROM` | from step 1 |

Leave `PORTAL_TEST_MODE=1`, `SENTRIX_TEST_MODE=1` and every `*_LIVE` switch at
0 (D13). `docker-compose.yml` sets `PORTAL_DATABASE_URL`, `PORTAL_BASE_URL`,
`PORTAL_SECURE_COOKIES=1`, `PORTAL_WORKERS=1` and the trusted proxy itself.

```bash
docker compose up -d --build
docker compose ps                         # db healthy, portal healthy, caddy up
curl -s https://portal.<yourdomain>.in/healthz
```

**Keys.** `PORTAL_SECRET_KEY` signs every session, code, device token and the
audit chain; `PORTAL_DATA_KEY` encrypts phones, medical notes and TOTP
secrets. Losing either strands every device and login: keep a copy in the
team's password manager. They are required from the environment when
`PORTAL_TEST_MODE=0`; in test mode the Portal generates them into the
`portal-data` volume if they are missing (then `backup.sh` must run).

## 7. Check from outside

From a laptop on a phone hotspot (not the VM):

```bash
./check_ports.sh portal.<yourdomain>.in    # only 80 and 443 (and 22 if kept) may be open
```

## 8. Postgres compatibility

`python tools/portal_pg_check.py [all|ddl|suite|live]`:

* `ddl` — every table and index compiles for PostgreSQL, and so does the
  outbox's `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` (passes on the
  laptop);
* `suite` — the Portal tests run with a **VARCHAR length guard** (SQLite
  ignores `String(n)`, Postgres rejects longer values);
* `live --url postgresql+psycopg://user:pass@host/db` — tables in a throwaway
  schema on a real server, a few writes and the audit chain, then the schema
  is dropped. The laptop venv has no psycopg; run it once on the VM, where the
  image has the driver:
  ```bash
  docker compose exec portal sh -c 'python tools/portal_pg_check.py live --url "$PORTAL_DATABASE_URL"'
  ```

Handled in the code: timestamps are `timestamp with time zone` (SQLite returns
naive values; `accounts.as_utc` normalises both), the outbox dedupe uses the
dialect's own `ON CONFLICT`, and `ilike` is portable.

## 9. First admin and devices

Create the company admin inside the container. It prints a single-use
activation code once; TOTP is set up at the first sign-in:

```bash
docker compose exec portal python tools/provision_device.py admin --username admin1
```

Provision each device. It prints the claim code, bootstrap secret and setup
code once (only their digests are stored) and writes `identity.json`
(mode 0600) and a printable label:

```bash
docker compose exec portal python tools/provision_device.py device \
    --out /data/portal/exports/SX5-000001 --label /data/portal/exports/SX5-000001/label.html
docker compose cp portal:/data/portal/exports/SX5-000001 ./SX5-000001
docker compose exec portal rm -r /data/portal/exports/SX5-000001
```

Without `--serial` the tool takes the next free serial; `--assign SX-0007`
pre-assigns the device to a customer. Copy `identity.json` to the device's
`/etc/sentrix/` (deploy/pi5 README step 12) and print the label for the box.

## 10. Connect a device

On the device, in `.env`:

```
SENTRIX_LINK_ENABLED=1
SENTRIX_PORTAL_URL=https://portal.<yourdomain>.in
```

It connects to `wss://portal.<yourdomain>.in/device/v1/link` with the
bootstrap secret and gets its device token when the customer claims it in
onboarding step 5. Telegram in production: `SENTRIX_TELEGRAM_MODE=webhook`;
the Portal registers `https://portal.<yourdomain>.in/hooks/telegram` with
`TELEGRAM_WEBHOOK_SECRET` itself.

## 11. Backups and the restore test

```bash
sudo mkdir -p /var/backups/sentrix && sudo chown $USER /var/backups/sentrix
./backup.sh                 # pg_dump -Fc + a tar of the portal-data volume (key files)
crontab -e                  # 15 2 * * * /opt/sentrix/deploy/portal/backup.sh >> /var/backups/sentrix/backup.log 2>&1
./restore_test.sh           # restores the newest dump into a scratch DB, compares row counts, drops it
```

* Nightly dump plus the data tar, kept 14 days on the VM's block volume
  (encrypted at rest by OCI).
* **Weekly off-VM copy**, encrypted: `gpg --symmetric --cipher-algo AES256`
  the newest dump and data tar, and copy them to a team drive. A dump without
  the keys restores rows nobody can sign in to.
* **Restore test** weekly and after every schema change. Record the date of
  the last passing test in the gate report (P6).
* Full restore on a new VM: `docker compose up -d db`, then
  `docker compose exec -T db pg_restore -U sentrix -d sentrix --clean < portal-<stamp>.dump`,
  restore the data tar into the `portal-data` volume, then start the rest.

## 12. Updates

```bash
cd /opt/sentrix && git pull && cd deploy/portal && docker compose up -d --build
```

The schema is created and extended additively at start (`Database.init`). An
Alembic baseline is still to do (docs/7 §16) before any column is renamed or
dropped.

## 13. Fallback: Render + Supabase

If the Oracle VM fails (card rejected, capacity, reclamation):

1. **Supabase** free project in **Mumbai (ap-south-1)**: copy the "Session
   pooler" connection string into `PORTAL_DATABASE_URL` as
   `postgresql+psycopg://...?sslmode=require`. It pauses after 7 idle days;
   the device heartbeats keep it awake once a device is linked.
   **Never Render's free Postgres** (deleted after 30 days).
2. **Render** free web service from this repository: Docker runtime,
   Dockerfile path `deploy/portal/Dockerfile`, environment from `.env.example`
   plus `PORTAL_DATABASE_URL` (Supabase),
   `PORTAL_BASE_URL=https://<service>.onrender.com`, `PORTAL_SECURE_COOKIES=1`
   and `PORTAL_WORKERS=1`. Render terminates TLS itself (no Caddy) and
   proxies WebSockets. Leave `PORTAL_TRUSTED_PROXIES` / `FORWARDED_ALLOW_IPS`
   at their defaults (never `*`): login back-off then counts Render's proxy
   address, which is acceptable for a demo fallback.
3. It sleeps after 15 min without inbound traffic and takes about a minute to
   wake: open it 5 minutes before the demo.
4. **E-mail must use an HTTPS API** (`SENTRIX_EMAIL_PROVIDER=brevo` or
   `resend`): Render blocks outbound SMTP ports 25, 465 and 587. The Portal
   never uses SMTP anyway.
5. Rehearse the fallback once (W5), with the device's `SENTRIX_PORTAL_URL`
   pointed at it. Target: a fallback deploy in ≤ 30 min.
