# SENTRIX Portal online, with phone alerts (Telegram or WhatsApp)

The demo set-up (decisions D17, D18 in `CLAUDE.md`):

```
 owner's phone ──┐                  ┌──────── Render (Singapore) ─────────┐
 any browser ────┼── https ────────►│  SENTRIX Portal (deploy/portal)      │
 authorities ────┘                  │  accounts · wizard · SOS · /d/<id>/… │
 phone (contact)                    │  SQLite + files on the /data disk    │
                                    └──────▲──────────────────────▲───────┘
                                           │ wss (device link)    │ webhook + Graph API
                                Raspberry Pi (SENTRIX)      Meta WhatsApp Cloud API
                                on the Galaxy S23 hotspot    (free test number)
```

The Pi connects **out** to the Portal; nothing connects into the Pi. While the
Pi is off the Portal shows the device offline; when it powers up it links
again by itself and the dashboard (`/d/<device id>/dashboard`) is live.

Test mode stays on (D13): messages go only to the addresses in
`SENTRIX_TEST_RECIPIENTS` (phone numbers, `tg:<chat id>`), and every one starts
with `[SENTRIX DEMO – TEST, NOT A REAL EMERGENCY]`. The demo of 2026-11-30 and
2026-12-05 uses **Telegram** (section 2A); WhatsApp (section 2B) is built but
needs Meta's business set-up.

---

## 1. Render (about 10 minutes)

Render's pricing page lists *workspace* plans (Hobby $0, Pro $25 …). The
**Hobby** workspace is enough; what costs money is the *instance* the
service runs on: **Starter** (0.5 CPU, 512 MB; `0.5c-512mb` in
`render.yaml`) plus the 1 GB disk (about $7 and $0.25 a month
when this was written; Render shows the exact price before you apply). A disk needs a paid instance, and without a disk every
restart would forget the accounts and the device. Cancel after the demo by
deleting the service.

1. Sign up at <https://render.com> with GitHub, choose the **Hobby**
   workspace, and add a card under **Billing** (the Starter instance needs
   it).
2. **New → Blueprint** → connect `mehulperimal14/Sentrix` → Render reads
   `render.yaml` at the repository root: one web service `sentrix-portal`,
   Docker (`deploy/portal/Dockerfile`), **Starter** (`0.5c-512mb`), **Singapore**, a 1 GB
   disk on `/data`, health check `/healthz`. The two Portal keys and the
   WhatsApp verify token are generated for you.
3. Render asks for the values marked `sync: false`. Fill in what you have
   now; the rest can be added later under the service's **Environment**:

| Variable | Value |
|---|---|
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` | from section 2 (leave empty until then) |
| `SENTRIX_TEST_RECIPIENTS` | `+91<owner phone>,+91<authorities phone>` |
| `SENTRIX_LIVE_ALERTS` | `1` to let messages leave (else every message is recorded as SUPPRESSED, a dry run) |
| `SENTRIX_WHATSAPP_LIVE` | `1` for WhatsApp |

4. **Apply**. The first build takes a few minutes. Then open the service →
   **Logs** and find:

```
SENTRIX Portal: company admin 'admin' - activate it once at https://sentrix-portal….onrender.com/activate/XXXX-XXXX …
```

   The service's address is at the top of its page
   (`https://sentrix-portal-xxxx.onrender.com`); the Portal uses it on its
   own (`RENDER_EXTERNAL_URL`). `…/healthz` answers `{"status": "ok", …}`.

Never change `PORTAL_SECRET_KEY` or `PORTAL_DATA_KEY` once the Portal holds
accounts: every stored phone number, code and device token depends on them.
Limits: one instance only (the device link lives in the process); a deploy
has a few seconds of downtime; the Hobby workspace includes 5 GB of
outbound traffic a month, and the live video through the Portal uses about
0.5–0.7 MB/s while someone watches.

**Railway instead** (if its sign-ups reopen): `railway.json` builds the same
image; add a volume on `/data`, generate a domain and set the same variables
(`PORTAL_WORKERS=1`, `PORTAL_SECURE_COOKIES=1`, `PORTAL_TRUSTED_PROXIES=*`,
`PORTAL_TEST_MODE=1`, `SENTRIX_TEST_MODE=1`, `PORTAL_BOOTSTRAP_ADMIN=admin`,
`PORTAL_SECRET_KEY` and `PORTAL_DATA_KEY` from
`python -c "import secrets; print(secrets.token_hex(32))"`, and the WhatsApp
and sending variables above). `PORTAL_BASE_URL` comes from
`RAILWAY_PUBLIC_DOMAIN`.

## 2A. Telegram (about 5 minutes; what the demo uses)

1. In Telegram open **@BotFather** → `/newbot` → a name (`SENTRIX Alerts`) and
   a username ending in `bot` → BotFather answers with the bot **token**
   (keep it private).
2. On each phone (owner, authorities) open **@userinfobot** → Start → note
   the **Id** it shows (not the phone number).
3. Render → `sentrix-portal` → **Environment** → add, then **Save changes**:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token |
| `PORTAL_TELEGRAM_BOT_NAME` | the bot's username without `@` |
| `SENTRIX_TELEGRAM_LIVE` | `1` |
| `SENTRIX_LIVE_ALERTS` | `1` |
| `SENTRIX_TEST_RECIPIENTS` | `tg:<owner Id>,tg:<authorities Id>` |

   `SENTRIX_TELEGRAM_MODE` defaults to `polling` (the Portal asks Telegram for
   updates; no webhook needed).
4. Check: `/start` to the bot from an allowlisted account answers "This is
   the SENTRIX alert bot…".
5. Linking happens in the owner's wizard: step 6 **Link Telegram** (owner);
   the contact's consent page **Get alerts on Telegram** (authorities).

## 2B. WhatsApp: Meta's Cloud API (about 20 minutes; optional)

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
   `whatsapp_business_management` → put it in `WHATSAPP_ACCESS_TOKEN`.
5. **App settings → Basic → App secret → Show** → `WHATSAPP_APP_SECRET`.
   Also set `WHATSAPP_PHONE_NUMBER_ID` (step 1) on Render, then **Save** (the
   service restarts).
6. **WhatsApp → Configuration → Webhook → Edit**: callback URL
   `https://<your service>.onrender.com/hooks/whatsapp`, verify token = the
   value of `WHATSAPP_VERIFY_TOKEN` (Render → service → **Environment**) →
   **Verify and save**. Then **Webhook fields → messages → Subscribe**.
7. **The three message templates.** WhatsApp only lets a business start a
   chat with an approved template. On the laptop, with
   `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_WABA_ID` (the account id from step
   1) in `.env`:

   ```
   python tools/whatsapp_templates.py create
   python tools/whatsapp_templates.py status
   ```

   (`show` prints them without contacting Meta, to create them by hand in
   WhatsApp Manager with exactly that text.) Meta usually approves UTILITY
   templates within minutes. Until all three say `APPROVED`,
   `WHATSAPP_MODE=auto` falls back to ordinary messages, which reach a phone
   only within 24 hours of that phone's last message to the test number:
   on demo morning send "hi" to the test number from both phones.

## 3. First sign-in and the device

1. Open the activation link from the logs → choose a password → scan the
   TOTP QR code with an authenticator app (Google Authenticator, Microsoft
   Authenticator) → you are in the **admin console**. Then delete
   `PORTAL_BOOTSTRAP_ADMIN` from the service's environment.
2. **Devices → Provision**: the page shows the device's **identity**
   (`identity.json`), its **claim code** and serial **once**. Keep the page
   open: the identity goes onto the Pi (`~/Sentrix/var/link/identity.json`)
   with `SENTRIX_LINK_ENABLED=1` and
   `SENTRIX_PORTAL_URL=https://<your service>.onrender.com` in the Pi's
   `.env`, then `systemctl --user restart sentrix.service`.
3. **Customers → New customer** (the owner) → the welcome letter shows the
   owner's activation link.

## 4. The owner's flow (what the professors see)

1. The owner opens the activation link → sets a password → the **7-step
   wizard**: name and **mobile number**, address (the nearest police, fire
   station and hospital are optional and only appear in the SOS text),
   household, **emergency contacts** (add the authorities phone; send it the
   consent link the page shows - on WhatsApp it arrives by itself - and it
   taps *Confirm*, then *Get alerts on Telegram*), **claim the device** with
   the serial and claim code, alerts (**Telegram** linked, or **WhatsApp**
   ticked; SOS PIN, duress PIN), consent.
2. **Home** shows the device and its live dashboard through the Portal.
3. A confirmed Level 3 (weapon or fire) → the owner gets a Telegram (or
   WhatsApp) message with **Alert my contacts / I'm checking / False alarm**,
   no sign-in needed. *Alert contacts* → the authorities phone gets the SOS
   with **I'm handling it / Open**. *False alarm* answers with the
   PIN page link (a PIN typed into a chat would stay in its history).
4. Pi switched off → the Portal shows **offline** within about a minute;
   switched on → online again and the dashboard is live.

## 5. Demo-morning checklist

- [ ] Render service **resumed** (Settings → Resume) if it was suspended;
      `…/healthz` is ok.
- [ ] Hotspot on and the Pi on it (powered about 2 minutes before).
- [ ] Telegram: `/start` to the bot answers (WhatsApp instead: both phones
      sent "hi" to the test number, templates APPROVED).
- [ ] Portal home shows the device **online**; the dashboard video moves.
- [ ] `SENTRIX_LIVE_ALERTS=1` and the channel's switch on Render; after the
      demo set them back to `0`, suspend the service, and delete it after
      2026-12-05.
