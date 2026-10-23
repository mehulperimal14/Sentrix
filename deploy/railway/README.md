# SENTRIX Portal on Railway, with WhatsApp alerts

The demo set-up (decisions D17, D18 in `CLAUDE.md`):

```
 owner's phone ──┐                    ┌──────────── Railway (Singapore) ────────────┐
 any browser ────┼── https ──────────►│  SENTRIX Portal  (deploy/portal/Dockerfile)  │
 authorities ────┘                    │  accounts · wizard · SOS · /d/<device>/…     │
 phone (contact)                      │  SQLite + keys on the /data volume            │
                                      └───────▲────────────────────────▲─────────────┘
                                              │ wss (device link)      │ webhook + Graph API
                                   Raspberry Pi (SENTRIX)       Meta WhatsApp Cloud API
                                   on the Galaxy S23 hotspot     (free test number)
```

The Pi connects **out** to the Portal; nothing connects into the Pi. When the
Pi is off, the Portal shows the device offline; when it powers up it links
again within seconds and the dashboard (`/d/<device id>/dashboard`) is live.

Test mode stays on (D13): WhatsApp messages go only to the numbers in
`SENTRIX_TEST_RECIPIENTS` and every one starts with
`[SENTRIX DEMO – TEST, NOT A REAL EMERGENCY]`.

---

## 1. Railway (about 10 minutes)

1. Sign up at <https://railway.com> with your GitHub account (trial: a
   one-time $5 credit for 30 days; volumes up to 0.5 GB).
2. **New Project → Deploy from GitHub repo →** `mehulperimal14/Sentrix`.
   Railway reads `railway.json` at the repository root and builds
   `deploy/portal/Dockerfile` (the Portal only: no models, no torch).
3. Open the service → **Settings**:
   * **Region**: Southeast Asia (Singapore).
   * **Networking → Generate Domain**: you get
     `https://<something>.up.railway.app`. This is the link people open.
4. Right-click the service on the canvas → **Attach volume**, mount path
   **`/data`** (accounts, devices and requests live there; without a volume
   every redeploy forgets them).
5. **Variables → Raw editor**, paste and fill in (secrets are typed by you,
   here only):

```
RAILWAY_RUN_UID=0
PORTAL_BASE_URL=https://<something>.up.railway.app
PORTAL_WORKERS=1
PORTAL_SECURE_COOKIES=1
PORTAL_TRUSTED_PROXIES=*
PORTAL_TEST_MODE=1
SENTRIX_TEST_MODE=1
PORTAL_LOG_LEVEL=info
PORTAL_SECRET_KEY=<64 hex characters>
PORTAL_DATA_KEY=<another 64 hex characters>
PORTAL_BOOTSTRAP_ADMIN=admin

WHATSAPP_ACCESS_TOKEN=<from step 2.4>
WHATSAPP_PHONE_NUMBER_ID=<from step 2.1>
WHATSAPP_APP_SECRET=<from step 2.5>
WHATSAPP_VERIFY_TOKEN=<any long random text you choose>
WHATSAPP_MODE=auto

SENTRIX_LIVE_ALERTS=1
SENTRIX_WHATSAPP_LIVE=1
SENTRIX_TEST_RECIPIENTS=+91<owner phone>,+91<authorities phone>
```

   Make each key with `python -c "import secrets; print(secrets.token_hex(32))"`.
   **Never change the two keys afterwards**: every stored phone number, code and
   device token depends on them.

   * `RAILWAY_RUN_UID=0` — the image runs as a non-root user and a Railway
     volume is root-owned.
   * `PORTAL_TRUSTED_PROXIES=*` — the client address comes from Railway's
     `X-Real-IP` (sign-in back-off per visitor, not per proxy).
   * `PORTAL_BOOTSTRAP_ADMIN` — see step 3; remove it once the admin is active.
   * Leave out the WhatsApp and `SENTRIX_*` sending lines until section 2 is
     done: the Portal runs fine without them (nothing is sent).
6. **Deploy**. When the build finishes, open **Deploy Logs** and find:

```
SENTRIX Portal: company admin 'admin' - activate it once at https://…/activate/XXXX-XXXX …
```

`https://<something>.up.railway.app/healthz` answers `{"status": "ok", …}`.

Limits to know: one replica only (the device link lives in the process); a
redeploy has a few seconds of downtime; one HTTP response is capped at 15
minutes, so the dashboard reopens the live video every 10 minutes by itself.

## 2. WhatsApp: Meta's Cloud API (about 20 minutes)

1. <https://developers.facebook.com> → **My Apps → Create App** → use case
   *Other* → type **Business** → create. In the app: **Add product →
   WhatsApp → Set up**. Meta creates a free **test number**. On **WhatsApp →
   API Setup** copy the **Phone number ID** and the **WhatsApp Business
   Account ID**.
2. Same page, **To → Manage phone number list**: add the **owner phone** and
   the **authorities phone** (up to 5). Each receives a code on WhatsApp to
   confirm. Only these numbers can get messages from the test number.
3. Still there, send Meta's sample `hello_world` to each phone once: it
   proves the phones are set up.
4. **A permanent token** (the one on API Setup expires in 24 h):
   <https://business.facebook.com/settings> → **Users → System users → Add**
   (role Admin) → **Assign assets**: your app (full control) and your
   WhatsApp account (full control) → **Generate new token** → choose the app,
   expiry **Never**, permissions `whatsapp_business_messaging` and
   `whatsapp_business_management` → copy it into `WHATSAPP_ACCESS_TOKEN`.
5. **App settings → Basic → App secret → Show** → `WHATSAPP_APP_SECRET`.
6. **WhatsApp → Configuration → Webhook → Edit**: callback URL
   `https://<something>.up.railway.app/hooks/whatsapp`, verify token = your
   `WHATSAPP_VERIFY_TOKEN` → **Verify and save** (the Portal must already be
   deployed with that variable). Then **Webhook fields → messages →
   Subscribe**.
7. **The three message templates.** WhatsApp only lets a business start a
   chat with an approved template. On the laptop, with
   `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_WABA_ID` (the account id from 2.1)
   in `.env`:

   ```
   python tools/whatsapp_templates.py create
   python tools/whatsapp_templates.py status
   ```

   (`show` prints them without contacting Meta, if you prefer to create them
   by hand in WhatsApp Manager with exactly that text.) Meta usually approves
   UTILITY templates within minutes. Until all three say `APPROVED`,
   `WHATSAPP_MODE=auto` falls back to ordinary messages, which reach a phone
   only within 24 hours of that phone's last message to the test number:
   on demo morning send "hi" to the test number from both phones.

## 3. First sign-in and the device

1. Open the activation link from the logs → choose a password → scan the
   TOTP QR code with an authenticator app (Google Authenticator, Microsoft
   Authenticator) → you are in the **admin console**. Remove
   `PORTAL_BOOTSTRAP_ADMIN` from the variables.
2. **Devices → Provision**: the page shows the device's **identity**
   (`identity.json`), its **claim code** and serial **once**. Keep the page
   open: the identity goes onto the Pi (`~/Sentrix/var/link/identity.json`)
   with `SENTRIX_LINK_ENABLED=1` and
   `SENTRIX_PORTAL_URL=https://<something>.up.railway.app` in the Pi's `.env`,
   then `systemctl --user restart sentrix.service`.
3. **Customers → New customer** (the owner) → the welcome letter shows the
   owner's activation link.

## 4. The owner's flow (what the professors see)

1. The owner opens the activation link → sets a password → the **7-step
   wizard**: name and **mobile number** (WhatsApp goes there), address,
   household, **emergency contacts** (add the authorities phone: it gets the
   consent link on WhatsApp and must tap *Confirm*), **claim the device**
   with the claim code, alerts (**WhatsApp** ticked, SOS PIN, duress PIN),
   consent.
2. **Home** shows the device and its live dashboard through the Portal.
3. A confirmed Level 3 (weapon or fire) → the owner gets a WhatsApp message
   with **Alert contacts / I'm checking / False alarm**, no sign-in needed.
   *Alert contacts* → the authorities phone gets the SOS on WhatsApp with
   **I'm handling it / Open the SOS page**. *False alarm* answers with the
   PIN page link (a PIN typed into a chat would stay in its history).
4. Pi switched off → the Portal shows **offline** within about a minute;
   switched on → online again and the dashboard is live.

## 5. Demo-morning checklist

- [ ] Hotspot on; the Pi and the laptop on it; `…/healthz` is ok.
- [ ] Both phones sent "hi" to the WhatsApp test number (opens 24 h).
- [ ] `python tools/whatsapp_templates.py status` → all APPROVED (else
      `WHATSAPP_MODE=auto` and the "hi" above carry the demo).
- [ ] Portal home shows the device **online**; the dashboard video moves.
- [ ] Railway trial credit left (Usage page).
