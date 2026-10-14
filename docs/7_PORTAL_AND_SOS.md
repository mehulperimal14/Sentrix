# 7. Portal, Onboarding and SOS — Developer Contract

This is the contract every implementation track codes against. It turns the
accepted decisions (D8–D14, recorded in `CLAUDE.md` §6 / M10) and the
independent reviews into exact interfaces: file ownership, configuration,
database schemas, the device-link protocol, the tunnel and its auth bridge,
the SOS state machine, channels, roles and endpoints.

* **Precedence:** accepted decisions > review findings > the original design.
  Where this document and the code disagree, the code in the foundation commit
  is the reference and this document is fixed.
* **Foundation commit:** "feat(foundation): contracts, config, guard, schema,
  services, portal skeleton" on branch `sentrix/portal`. Everything marked
  *(foundation)* below exists and is tested in `tests/test_foundation.py`.
* **Shared files** (§2.2) are changed only by the foundation/integration owner.
  A track that needs a change there writes it in its hand-off note instead of
  editing the file.

---

## 1. Architecture

### 1.1 Product flow (unchanged from the team's proposal)

Company provisions device → admin creates customer → welcome letter (username,
activation code, device serial + claim code) → customer installs the device →
activates the account (sets own password) → 7-step onboarding wizard → daily use
of the **same dashboard** in any browser → a confirmed L3 commit of an alarm
class opens an **assistance request** pre-filled from the profile → "Do you need
assistance?" with a 30/45/60-minute window → approve / false alarm (PIN) /
no answer → EXPIRED ("unanswered") → help path: SENTRIX Desk (simulated
responder console) + confirmed emergency contacts → Desk resolves.

### 1.2 Components and processes

```
 HOME (behind CGNAT)                         CLOUD (Oracle Always Free, India; Caddy TLS)
+-------------------------------------+     +------------------------------------------+
| ONE device process (python -m       | WSS | sentrix_portal (FastAPI, 1 worker)       |
|   sentrix)                          |====>|  link hub   /device/v1/link              |
|  Pipeline: capture, inference, HUD  | out |  tunnel     /d/{device_id}/...           |
|    -> sos.wants/trigger/on_level    | only|  relays     /d/{id}/video, /d/{id}/ws/... |
|  LC web app (sentrix/web)           |     |  own pages: home, onboarding, account,   |
|  services (sentrix/services.py):    |<====|    settings, SOS, Desk, admin, contacts, |
|    node, gpio, channels (+modem),   | cmds|    privacy; offline mirror               |
|    sos (engine + sweeper), link     | tun.|  cloud SOS authority (device offline)    |
|  SQLite var/sentrix.db, vault, faces|     |  channel router: Telegram, e-mail        |
|  siren relay, LEDs, PIR, door, mains|     |  Postgres (internal network only)        |
+------------------+------------------+     +------------------------------------------+
   LAN :8000 (LC)  |  USB 4G modem (SMS)
```

* **One device process** owns the web app (LC), the SOS service, the link
  client, the outbox and the channel router — today's single-process app. The
  pipeline only *enqueues* an idempotent SOS trigger per incident. A
  multi-process Pi split is future work; when it comes, the routes must read
  through a backend interface instead of `get_pipeline()` (review blocker,
  deferred with this decision).
* **Parity by tunnel (D10):** the Portal does not re-implement LC pages.
  `/d/{device_id}/...` is forwarded over the link and replayed *in-process* into
  the device's own LC app (`httpx.ASGITransport`), so the same code serves both.
  `/video` and `/ws/threat` are relayed specially (frame and state messages).
* **Topologies.** Laptop (W0–W3): device = `python -m sentrix` with a video file
  as the camera; Portal = `python -m sentrix_portal` on
  `http://portal.localhost:8100`; LC on `http://127.0.0.1:8000`. Pi 5 T1: the
  same device process on the Pi. Pi T3 (hub): the hub is the "device" to the
  Portal; the Pi node exposes modem and siren as node commands (HARDWARE.md T3).

### 1.3 What lives where

| Data | Device | Portal | Rule |
|---|---|---|---|
| Detection, levels, incidents, siren, evidence vault, `EVIDENCE_AES_KEY` | yes (authority) | metadata mirror only | edge first |
| Face gallery, embeddings, evidence images, continuous video | **only here** | streamed on demand through the tunnel, never stored | D12 |
| SOS snapshot | yes | **one** consented snapshot per request, 30 days, may show faces | D12 amended |
| Profile, household, contacts, consents | encrypted cache (for offline SOS) | **authority** | |
| SOS requests | creates them; authority until the cloud has the request *and* the heartbeat is lost | mirror; authority while the device is offline | §7.6 |
| Accounts, roles, sessions | LC keeps only `SENTRIX_PASSWORD` (device admin) | authority | D14: Portal password hashes never reach the device |
| Messages out | modem SMS, LC prompt, siren, legacy Twilio | Telegram, e-mail, Desk | §8.5 |

---

## 2. Module map and file ownership

### 2.1 Tracks

| Track | Scope |
|---|---|
| **T-HW** | Pi hardware I/O and deploy: Picamera2 source, gpiozero actuators and inputs, sensors, node telemetry, `run.sh` Pi path, `requirements-pi.txt`, `deploy/pi5/`, doctor hardware checks, LC Node API |
| **T-CLIP** | 4-second clip engine, fast lane, runtime backends, model export, `bench_clip`, clip-mode replay, `SENTRIX_FACE_MODEL` |
| **T-SOS** | Device SOS engine (pure machine use, policy, templates, sweeper, recovery, verdict semantics, duress, repeat throttle, Request help now), pipeline hook implementation, LC SOS JSON API, dispatch packages |
| **T-CH** | Channel router, persistent outbox, retries, dedupe; Telegram, e-mail (Brevo/Resend HTTPS), modem SMS (pyserial AT), Twilio wrapper; legacy notifier routed through the router |
| **T-WEB** | LC hardening and UI: base-aware URLs, external JS, CSP nonces, CSRF header, `textContent`, tunnel acceptance, roles for tunnelled users, every parity "F" fix, new LC pages (SOS prompt/banner, Node, `/setup/wifi`), "remote view: N" |
| **T-LINK** | Device link client, outbox pump, heartbeat, tunnel executor, subscriptions, device-enforced command rules; Portal link hub, `/d/{id}` proxy and relays, Portal SOS mirror and cloud authority, Desk, contact pages, profile delivery |
| **T-ACCT** | Portal accounts and onboarding: users/roles/sessions UX, activation codes, admin + Desk TOTP, back-off, audit UI, admin console (customers, welcome letter, provisioning, labels with QR, assignment), 7-step wizard, household/members/contacts with consent, settings, privacy, home, test-mode banner |
| **T-MODEL** | Slim scene model training in the GPU env (`training/` scripts only) |

### 2.2 Shared files — foundation/integration owner only

`sentrix/config.py`, `sentrix/app.py`, `sentrix/services.py`,
`sentrix/mode.py`, `sentrix/paths.py`, `sentrix/storage/models.py`,
`sentrix/storage/database.py`, `sentrix/pipeline/state.py`,
`sentrix/pipeline/escalation.py`, `sentrix/pipeline/engine.py` (except the
`run_clip_live` seam, which only calls T-CLIP), `sentrix/fusion/*`,
`sentrix/evaluation/harness.py`, `sentrix/alerting/guard.py`,
`sentrix/alerting/channels/base.py`, `sentrix/link/protocol.py`,
`sentrix/link/tunnel_auth.py`, `sentrix/sos/__init__.py`,
`sentrix/sos/states.py`, `sentrix/utils/redact.py`, `sentrix/web/extensions.py`,
`sentrix_portal/{app,db,models,settings}.py`, `sentrix_portal/routes/__init__.py`,
`requirements.txt`, `pyproject.toml`, `.env.example`, `CLAUDE.md`, this file,
`tests/test_foundation.py`.

### 2.3 Track-owned files (create or edit freely)

| Path | Owner | State after foundation |
|---|---|---|
| `sentrix/io/picamera.py` (`PiCameraSource`, guarded `picamera2` import) | T-HW | to create |
| `sentrix/io/gpio.py` (`build_service`: LEDs, buzzer, PIR, door, mains-sense) | T-HW | to create |
| `sentrix/io/actuators.py` (siren on gpiozero, `build_siren`) | T-HW | rewrite |
| `sentrix/io/sources.py` (add `picam:` spec; keep the redacted names) | T-HW | edit |
| `sentrix/io/sensors.py`, `sentrix/io/node.py` (`build_service`), `sentrix/io/network.py` (nmcli: `scan`, `connect`, `status`) | T-HW | to create |
| `sentrix/web/node_routes.py` | T-HW | empty router |
| `run.sh` (Pi path), `requirements-pi.txt`, `deploy/pi5/**`, `tools/doctor.py` (hardware checks) | T-HW | edit/create |
| `sentrix/pipeline/clip.py` (`ClipBuffer`, `ClipScheduler`, `ClipEngine`, `ClipScores`, `run_clip_live(pipeline, stop)`), `sentrix/pipeline/fastlane.py`, `sentrix/runtime/**` | T-CLIP | to create |
| `tools/export_models.py`, `tools/bench_clip.py`, `tools/replay_custom_videos.py` (`--mode clip`), `sentrix/perception/identity.py` (read `settings.identity.face_model`) | T-CLIP | create/edit |
| `sentrix/sos/service.py` (`build_service`), `machine.py`, `policy.py`, `templates.py`, `sweeper.py`, `pins.py` | T-SOS | to create |
| `sentrix/storage/requests.py` | T-SOS | minimal CRUD (foundation) |
| `sentrix/alerting/dispatch.py` (profile-filled packages, one per request, 409 on SENT/CANCELLED/EXPIRED) | T-SOS | edit |
| `sentrix/web/sos_routes.py` | T-SOS | empty router |
| `sentrix/alerting/channels/router.py` (`build_service`), `telegram.py`, `email.py`, `modem.py`, `twilio.py` | T-CH | to create |
| `sentrix/alerting/notifier.py` (route L4 SMS / L5 call through the router; keep the guard) | T-CH | guarded (foundation) |
| `sentrix/storage/notifications.py` | T-CH | minimal `SqlOutbox` (foundation) |
| `sentrix/web/channel_routes.py`; `sentrix_portal/routes/hooks.py`; `sentrix_portal/services/notify.py` | T-CH | empty / to create |
| `sentrix/web/routes.py`, `streaming.py`, `security.py`, `hardening.py`, `setup_routes.py`, `templates/**`, `static/**` | T-WEB | edit / stubs |
| `sentrix/link/client.py` (`build_service`), `tunnel.py`, `commands.py`, `policy.py`, `profile.py`, `sentrix/storage/link_store.py`, `sentrix/web/link_routes.py` | T-LINK | to create / minimal CRUD / empty |
| `sentrix_portal/routes/{link_hub,device_proxy,sos,desk,contacts}.py`, `sentrix_portal/services/{hub,sos}.py`, their templates | T-LINK | empty routers |
| `sentrix_portal/routes/{auth,admin,onboarding,household,settings,privacy,home}.py`, `sentrix_portal/security.py`, `sentrix_portal/services/accounts.py`, their templates, `tools/provision_device.py` | T-ACCT | empty routers / primitives |
| `training/**` (scene model) | T-MODEL | |
| `tests/test_<track>_*.py` | each track | |

Templates: Portal templates live in `sentrix_portal/templates/<area>/...`
(`base.html` is shared, foundation). Static files: `sentrix_portal/static/<area>/`.

---

## 3. Configuration

All device tunables live in `sentrix/config.py` (env read once at import;
seconds, never frames). Tracks only read them. Secrets are **never** config
fields (`/api/config` publishes `settings.to_dict()`): channel code reads them
from the environment at use time. `settings.test_mode` mirrors
`channels.test_mode`. Every service is **off** unless its flag is set.

### 3.1 `settings.sos` — `SosConfig`

| Field | Env | Type | Default | Meaning |
|---|---|---|---|---|
| `enabled` | `SENTRIX_SOS_ENABLED` | bool | False | Build the SOS service (engine + sweeper) |
| `min_level` | — | int | 3 | `ActionPlan.sos` from this committed level |
| `alarm_classes` | — | tuple | ("weapon", "fire") | D1/D8 classes that open a request (+ `intrusion` in armed mode) |
| `intrusion_entry_delay_seconds` | — | float | 30 | Armed intrusion opens a request only after this, unless disarmed (PIN or authorised face) |
| `window_seconds` | `SENTRIX_SOS_WINDOW_SECONDS` | int | 1800 | Fallback window; the profile's value wins; clamp to `window_choices` |
| `window_choices` | — | tuple | (1800, 2700, 3600) | Allowed windows |
| `demo_window_seconds` | `SENTRIX_SOS_DEMO_WINDOW_SECONDS` | int | 0 | Test mode only, ≥ 60 when set |
| `reminder_offsets_seconds` | — | tuple | (120, 300, 600, 1200) | Owner reminders after T0 |
| `lapse_warning_seconds` | — | float | 300 | "Lapses in 5 min" before expiry |
| `seen_snooze_seconds` | — | float | 300 | "I'm checking" pauses reminders only |
| `owner_sms_unseen_after_seconds` | — | float | 600 | Owner SMS if offline, or unseen after this |
| `delivery_warning_seconds` | — | float | 60 | No remote channel delivered → `undelivered` |
| `safety_defaults` | — | bool | True | Default for new households (D9) |
| `contacts_immediate_levels` | — | tuple | (4, 5) | Corroborated levels that alert contacts at once |
| `fire_contacts_after_seconds` | — | float | 120 | Confirmed fire: contacts at T+2 min |
| `contacts_for_single_source_l3` | `SENTRIX_SOS_CONTACTS_SINGLE_SOURCE_L3` | bool | False | Until gate P8 passes, single-source weapon L3 → owner only |
| `single_source_contacts_after_seconds` | — | float | 300 | When the flag above is on |
| `heartbeat_lost_seconds` | — | float | 45 | Cloud: device silent this long = offline |
| `repeat_window_seconds` | — | float | 600 | Re-prompt after a decline at most once per window (flag `repeat`) |
| `pin_length` | — | int | 6 | SOS and duress PINs |
| `pin_attempts`, `pin_window_seconds` | — | int, float | 5, 600 | PIN back-off (decline is throttled; help never is) |
| `duress_min_digit_difference` | — | int | 2 | Duress PIN differs from the SOS PIN in ≥ 2 digits |
| `repage_offsets_seconds` | — | tuple | (120, 300, 600) | Help path: re-page Desk/contacts, then hourly |
| `repage_interval_seconds` | — | float | 3600 | |
| `repage_cap`, `repage_cap_test_mode` | — | int | 10, 3 | Then flag `unacknowledged` and tell the owner |
| `stale_close_seconds` | — | float | 86400 | Help path closes as stale after 24 h |
| `sweep_interval_seconds` | — | float | 15 | Sweeper period (and at startup) |
| `link_grace_seconds` | — | float | 7200 | Contact pages / action links live until close + 2 h |

### 3.2 `settings.link` — `LinkConfig`

| Field | Env | Default | Meaning |
|---|---|---|---|
| `enabled` | `SENTRIX_LINK_ENABLED` | False | Build the link client service |
| `portal_url` | `SENTRIX_PORTAL_URL` | "" | `https://host` → `wss://host/device/v1/link` (`http://` → `ws://`, dev only) |
| `identity_dir` | `SENTRIX_DEVICE_IDENTITY_DIR` | "" | "" = `/etc/sentrix` if it exists, else `var/link` (`paths.LINK_DIR`) |
| `protocol_version` | — | 1 | |
| `heartbeat_seconds` | — | 10 | |
| `offline_after_seconds` | — | 30 | Device: Portal silent this long → offline ladder (SMS) |
| `reconnect_min_seconds` / `reconnect_max_seconds` | — | 1 / 60 | Full-jitter exponential backoff |
| `outbox_window` | — | 64 | Unacked events in flight |
| `state_hz_subscribed` / `state_idle_seconds` | — | 2.0 / 10 | `state` message rate |
| `subscription_lease_seconds` | — | 75 | State/video subscriptions expire unless renewed |
| `video_max_fps` / `video_min_fps` | — | 15 / 2 | Adaptive remote video |
| `video_jpeg_quality` | — | 70 | = the MJPEG stream's, so `latest_jpeg` encodes once |
| `video_idle_stop_seconds` | — | 10 | Stop pushing frames after the last viewer |
| `max_viewers` | — | 3 | Owner/member and Desk are exempt during an open request |
| `tunnel_timeout_seconds` / `tunnel_upload_timeout_seconds` | — | 20 / 60 | |
| `tunnel_max_inflight` | — | 8 | More → 503 `device_busy` |
| `tunnel_inline_body_bytes` / `tunnel_chunk_bytes` | — | 256 KiB | Inline base64 up to this; larger in BODY chunks |
| `tunnel_max_request_bytes` / `tunnel_max_response_bytes` | — | 11 MiB / 16 MiB | |
| `sms_daily_cap` | — | 30 | `sms.send` from the cloud |
| `siren_test_min_interval_seconds` / `siren_test_seconds` | — | 60 / 1 | |
| `media_requests_per_minute` | — | 60 | Tunnelled media per user |

### 3.3 `settings.channels` — `ChannelConfig` (and the guard's env)

| Field | Env | Default | Meaning |
|---|---|---|---|
| `enabled` | `SENTRIX_CHANNELS_ENABLED` | False | Build the router + outbox worker service |
| `test_mode` | `SENTRIX_TEST_MODE` | **True** | D13; only mode this semester |
| `live_alerts` | `SENTRIX_LIVE_ALERTS` | False | Global switch 1 of 2 |
| `modem_live` | `SENTRIX_MODEM_LIVE` | False | Switch 2 for `modem_sms`, `modem_call` |
| `twilio_enabled` | `SENTRIX_TWILIO_ENABLED` | False | Switch 2 for `twilio_sms`, `twilio_call` (**breaking**: keys alone send nothing) |
| `telegram_live` | `SENTRIX_TELEGRAM_LIVE` | False | Switch 2 for `telegram` |
| `email_live` | `SENTRIX_EMAIL_LIVE` | False | Switch 2 for `email` |
| — | `SENTRIX_WEBPUSH_LIVE` | False | Switch 2 for `webpush` (Tier 2) |
| — | `SENTRIX_TEST_RECIPIENTS` | "" | Allowlist (comma/semicolon/newline separated: E.164, e-mail, `tg:<chat id>`); read by the guard only |
| `modem_enabled` | `SENTRIX_MODEM_ENABLED` | False | Build the modem worker |
| `modem_port` / `modem_baud` | `SENTRIX_MODEM_PORT` / `SENTRIX_MODEM_BAUD` | "" / 115200 | "" = probe |
| `telegram_mode` | `SENTRIX_TELEGRAM_MODE` | polling | polling (laptop) / webhook (VM) |
| `email_provider` | `SENTRIX_EMAIL_PROVIDER` | "" | brevo / resend (HTTPS API, never SMTP) |
| `max_attempts` / `retry_backoff_seconds` | — | 3 / (30, 90) | "3 tries over 2 min" |
| `failed_retry_seconds` | — | 120 | FAILED items retried this often |
| `request_timeout_seconds` | — | 15 | Provider HTTP timeout |

Secrets (env names only, read at use time): `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `BREVO_API_KEY`, `RESEND_API_KEY`,
`SENTRIX_EMAIL_FROM`, `SENTRIX_DESK_NUMBER`, `TWILIO_*`, `ALERT_PHONE_NUMBER`.

### 3.4 `settings.gpio`, `settings.node`, `settings.clip`, `settings.web`, identity

| Field | Env | Default | Meaning |
|---|---|---|---|
| `gpio.enabled` | `SENTRIX_GPIO_ENABLED` | False | GPIO service (LEDs, buzzer, inputs) |
| `gpio.pin_factory` | `SENTRIX_GPIO_PIN_FACTORY` | "" | "" auto (lgpio on a Pi, mock elsewhere) / lgpio / mock |
| `gpio.siren_pin` / `siren_active_high` | — / `SENTRIX_SIREN_ACTIVE_HIGH` | 17 / True | NPN driver, `gpio=17=op,dl` keeps it off at boot |
| `gpio.led_green_pin` / `led_amber_pin` / `led_red_pin` | — | 5 / 12 / 13 | Amber kept off GPIO6 (UPS HAT); HARDWARE.md §6 still says 6 — T-HW updates it |
| `gpio.buzzer_pin` | — | 26 | |
| `gpio.pir_pin` / `door_pin` / `door_pull_up` | — | 23 / 24 / True | |
| `gpio.mains_sense_pin` / `mains_present_low` | — | 16 / True | PC817 output low while mains present |
| `gpio.input_bounce_seconds` | — | 0.05 | |
| `node.enabled` | `SENTRIX_NODE_ENABLED` | False | Telemetry service |
| `node.profile` | `SENTRIX_PROFILE` | laptop | laptop / pi5-clip / pi5-hailo / pi5-node / hub |
| `node.interval_seconds` / `temp_warn_c` / `temp_critical_c` | — | 5 / 80 / 85 | |
| `clip.mode` | `SENTRIX_PIPELINE_MODE` | tick | `clip` → `Pipeline.run_live` calls `sentrix.pipeline.clip.run_clip_live(pipeline, stop)` |
| `clip.window_seconds` / `hop_seconds` | — | 4 / 2 | |
| `clip.keyframes` / `uniform_keyframes` / `motion_keyframes` / `confirm_keyframes` | — | 6 / 4 / 2 / 2 | |
| `clip.budget_fraction` | — | 0.8 | p95 clip compute < 80 % of the hop |
| `clip.fast_lane` / `fast_lane_imgsz` / `fast_lane_every_n` / `fast_lane_hits` | `SENTRIX_FAST_LANE` | False / 320 / 4 / 2 | |
| `clip.runtime` / `clip.threads` | `SENTRIX_RUNTIME` / `SENTRIX_CLIP_THREADS` | torch / 4 | torch / onnx / ncnn |
| `web.api_docs` | `SENTRIX_API_DOCS` | False | `/docs`, `/redoc`, `/openapi.json` (now off by default) |
| `web.cookie_name` | — | lc_session | T-WEB renames `session_token` → `lc_session` |
| `web.csrf_header` | — | X-CSRF-Token | |
| `web.csp` | `SENTRIX_CSP` | True | |
| `web.setup_mode` | `SENTRIX_SETUP_MODE` | False | `/setup/wifi` exists only in physical setup mode |
| `identity.face_model` | `SENTRIX_FACE_MODEL` | buffalo_l | buffalo_s on the Pi (re-embeds the gallery) |

### 3.5 Portal settings — `sentrix_portal.settings.PortalSettings`

Built per app (`PortalSettings.from_env()`, or passed to `create_app` in tests).

| Field | Env | Default | Meaning |
|---|---|---|---|
| `database_url` | `PORTAL_DATABASE_URL` | `sqlite:///<repo>/var/portal/portal.db` | Postgres in production |
| `secret_key` | `PORTAL_SECRET_KEY` | ephemeral in test mode | HMAC for session ids, codes, tokens, audit chain, IP hashes; **required** when test mode is off |
| `data_key` | `PORTAL_DATA_KEY` | = secret in test mode | Column encryption (`seal`/`unseal`); required when test mode is off |
| `base_url` | `PORTAL_BASE_URL` | http://portal.localhost:8100 | Links in messages, QR labels |
| `test_mode` | `PORTAL_TEST_MODE` | **True** | Banner, Desk number instead of 112, guard allowlist |
| `host` / `port` | `PORTAL_HOST` / `PORTAL_PORT` | 127.0.0.1 / 8100 | |
| `cookie_name` / `csrf_cookie_name` / `csrf_header` | — | portal_session / portal_csrf / X-CSRF-Token | |
| `secure_cookies` | `PORTAL_SECURE_COOKIES` | False | True behind Caddy |
| `session_seconds` / `session_idle_seconds` | — | 12 h / 2 h | |
| `step_up_seconds` | — | 300 | Fresh password/PIN for sensitive actions |
| `activation_ttl_seconds` | — | 72 h | Activation/reset codes |
| `claim_attempts` / `claim_window_seconds` | — | 5 / 900 | Per account and per serial |
| `trusted_proxies` | `PORTAL_TRUSTED_PROXIES` | 127.0.0.1,::1 | X-Forwarded-For trusted only from these |
| `desk_number` | `PORTAL_DESK_NUMBER` | "" | |
| `telegram_bot_name` | `PORTAL_TELEGRAM_BOT_NAME` | "" | Deep link `t.me/<bot>?start=<code>` |
| `notice_version` | — | 2026-10-1 | Stored with each consent |
| `api_docs` | `PORTAL_API_DOCS` | False | |
| `data_dir` | `PORTAL_DATA_DIR` | `<repo>/var/portal` | |

The Portal's send guard is `guard.SendPolicy.from_env(test_mode_env="PORTAL_TEST_MODE")`
with the same `SENTRIX_LIVE_ALERTS`, channel switches and `SENTRIX_TEST_RECIPIENTS`.

### 3.6 Services registry *(foundation: `sentrix/services.py`)*

```python
class Service(Protocol):
    name: str
    def start(self) -> None: ...      # returns quickly (spawns a thread/task)
    def stop(self) -> None: ...       # idempotent
    def health(self) -> dict: ...     # non-blocking; merged into /api/health["services"]

@dataclass
class ServiceContext:
    settings; state; get_pipeline: Callable[[], Pipeline]; registry: ServiceRegistry; extras: dict

def build_service(ctx: ServiceContext) -> Service | None   # each track's factory; None = flag off
```

Start order (`BUILTIN_FACTORIES`), stop in reverse:

| # | Name | Factory | Flag |
|---|---|---|---|
| 1 | `node` | `sentrix.io.node:build_service` | `SENTRIX_NODE_ENABLED` |
| 2 | `gpio` | `sentrix.io.gpio:build_service` | `SENTRIX_GPIO_ENABLED` |
| 3 | `channels` | `sentrix.alerting.channels.router:build_service` | `SENTRIX_CHANNELS_ENABLED` (modem worker inside, `SENTRIX_MODEM_ENABLED`) |
| 4 | `sos` | `sentrix.sos.service:build_service` | `SENTRIX_SOS_ENABLED` |
| 5 | `link` | `sentrix.link.client:build_service` | `SENTRIX_LINK_ENABLED` |

A missing module is skipped; a failing import/factory/start is logged, marked
`failed` in health and never stops the app. The lifespan starts services
**before** the live loop and sets `pipeline.sos = registry.get("sos")` when one
exists. Later services find earlier ones with `ctx.registry.get("channels")`.
`services.health_fields()` → `{"services", "send_guard", "test_mode", "mode"}`
is merged into `/api/health`. `services.assert_measurement_safe(pipeline)`
raises `MeasurementUnsafe` unless `pipeline.sos` is a `NullSosService` and no
service is running (called by the eval harness, `tools/replay.py`,
`tools/bench_live.py`; T-CLIP adds it to `tools/bench_clip.py`).

### 3.7 Runtime mode *(foundation: `sentrix/mode.py`)*

`is_armed()`, `set_armed(armed, by)` (publishes `state.mode`),
`armed_override()`, `clear_override()`, `snapshot()`. `SENTRIX_ARMED` is the
start-up value; the TCI engine reads the override every tick. Callers check
the PIN first (disarm needs the SOS PIN; arming does not).

---

## 4. The Portal application *(foundation: `sentrix_portal/`)*

* `create_app(settings=None)` builds the database (tables created, idempotent),
  `app.state.settings`, `app.state.db` (`Database`: `.session()` context
  manager, `.SessionLocal`), `app.state.templates` (Jinja globals `test_mode`,
  `desk_number`), `/static`, every router in `routes.ROUTER_MODULES`, and
  `GET /healthz` → `{"status": "ok", "db": "ok", "test_mode": bool, "version"}`.
* `python -m sentrix_portal` serves it on `PORTAL_HOST:PORTAL_PORT` (8100).
* `db.get_db` is the per-request session dependency (commit on success). A
  route that needs its write visible before responding calls `db.commit()`.
* Templates extend `base.html`: blocks `title`, `extra_head`, `nav`, `content`,
  `scripts`; context `principal` (a `security.Principal` or None) and
  `active_page` (a nav key). `base.html` renders the test-mode banner, the CSRF
  meta tag and a role-based nav; scripts must be external files with the CSP
  nonce (`{{ csp_nonce }}`).
* Schema changes after the first deployment go through Alembic (integration).
* Cross-track Python APIs on the Portal (create these modules; signatures fixed):
  * T-LINK `sentrix_portal/services/sos.py`:
    `respond(db, request_id, action, *, actor_type, actor_id, channel, authority=None, pin=None) -> dict`
    (actions: `approve`, `decline`, `checking`, `safe`, `help_now`, `acknowledge`,
    `resolve`), `open_requests(db, household_id) -> list[dict]`.
  * T-LINK `sentrix_portal/services/hub.py`: `is_online(device_id) -> bool`,
    `send_command(device_id, name, args, actor) -> dict` (awaitable result or
    queued), `push_profile(db, device_id) -> int` (version).
  * T-ACCT `sentrix_portal/services/accounts.py`:
    `build_profile(db, household_id, device_id) -> dict` (§5.11),
    `confirmed_contacts(db, household_id) -> list[EmergencyContact]`.
  * T-CH `sentrix_portal/services/notify.py`: `router(db) -> ChannelRouter`,
    `enqueue(db, recipient, message) -> str | None`, the worker loop.

---

## 5. Device link protocol v1 *(foundation: `sentrix/link/protocol.py`)*

### 5.1 Transport

* One outbound WebSocket: `wss://<portal>/device/v1/link` (`ws://` only for an
  `http://` development Portal). The device never listens for the Portal.
* Handshake header `Authorization`: `Bearer <device token>` for a claimed
  device; `Bootstrap <device_id>:<bootstrap secret>` before the claim (limited
  session: `hello`, `heartbeat`, `error` only; the Portal shows "online,
  unclaimed").
* `websockets` ping interval 20 s, timeout 20 s on both sides; max message
  1 MiB (`MAX_MESSAGE_BYTES`).
* Reconnect: full jitter, `delay = random(0, min(60, 1 · 2^attempt))`; attempt
  resets after 60 s connected. After close code 4001 wait the maximum (60 s).
* Close codes: 1000 normal, 1001 going away (redeploy), 1011 server error,
  **4001** unauthorised (bad token/bootstrap), **4003** revoked/unpaired (device
  wipes token and profile cache), **4008** protocol version unsupported,
  **4009** replaced by a newer connection of the same device.

### 5.2 Identity and pairing

`<identity_dir>/identity.json` (mode 0600, written by
`tools/provision_device.py` at the company): `{"device_id", "serial",
"bootstrap_secret", "provisioned_at"}`. `<identity_dir>/token.json`:
`{"token", "issued_at"}` (written on `token.issue`). The Portal stores only
`token_digest()` values of the bootstrap secret, the token and the claim code.
Claim: the household types/scans serial + claim code (8 chars from
`BCDFGHJKLMNPQRSTVWXZ`, `KTRW-BQXL`; 5 attempts / 15 min per account and per
serial) → device CLAIMED → the hub sends `token.issue` → the device reconnects
with `Bearer` within 5 s → bootstrap revoked → `profile.push`. Unpair: owner +
step-up → `unpair` command → device wipes token and profile → Portal closes 4003.
Factory reset (button 10 s) wipes `var/` but keeps `identity.json`; the Portal
then requires a new claim (transfer issues a new claim code).

### 5.3 Envelope

```json
{"v": 1, "type": "<type>", "id": "<uuid4>", "ts": 1760430000000, "...": "type fields"}
```

`ts` is the sender's wall clock (ms). Unknown fields are ignored. Unknown
`type` → `error{code: "unknown_type", ref}`. A different `v` → close 4008.
`protocol.envelope(type, **fields)`, `encode()`, `decode()` (raises
`ProtocolError(code, message)`), `error(code, message, ref)`.

### 5.4 Message catalogue

| Type | Dir | When | Fields |
|---|---|---|---|
| `hello` | D→C | first message | `device_id`, `serial`, `firmware` (git short hash), `app_version`, `protocol` (1), `session_id`, `profile_version` (active cache, 0 if none), `outbox` {`head_seq`, `tail_seq`, `depth`}, `capabilities` [tunnel, video, state, sms, siren, gpio, clip], `test_mode`, `armed`, `health` (the redacted `/api/health` field set, incl. `audio`), `time_ms` |
| `welcome` | C→D | reply to hello | `device_id`, `claimed`, `server_time_ms`, `profile_version` (latest; the device expects a push when newer), `resend_from_seq` (cloud's highest contiguous stored seq), `subscriptions` {`state`: bool, `video`: {max_fps} or null}, `heartbeat_seconds` |
| `heartbeat` | D→C | every `heartbeat_seconds` (10 s) | `camera_online`, `video_fps`, `ai_hz`, `frame_age_ms`, `audio`, `armed`, `alarm` {level, incident_id, incident_type}, `open_request_id`, `node` {cpu_temp_c, throttled, under_voltage, mains, ext5v_v}, `modem` {present, registered, signal_dbm, sms_today}, `channels` {outbound_ready}, `outbox_depth`, `remote_viewers`, `uptime_s`, `profile_version`, `time_ms` |
| `event` | D→C | outbox (§5.5) | `seq`, `kind`, `priority`, `at` (ISO), `payload` |
| `ack` | C→D | ≤ 1 s after commit, batched | `seqs` [int] and/or `upto` (every seq ≤ upto stored) |
| `state` | D→C | 2 Hz while subscribed, else every 10 s | `n` (per-connection counter), `payload` (the `state.ws_payload()` object) |
| `state.subscribe` | C→D | first dashboard/live viewer, renewed < lease | `sub`, `hz` (≤ 2), `lease_s` (75) |
| `state.unsubscribe` | C→D | last viewer gone | `sub` |
| `video.subscribe` | C→D | first video viewer, renewed < lease | `sub`, `max_fps` (≤ 15), `lease_s`, `actor` {user_id, role} |
| `video.unsubscribe` | C→D | | `sub` |
| `viewers` | C→D | on change | `count` (remote viewers of video and dashboards) → `state.set_remote_viewers` |
| `profile.push` | C→D | on change and after welcome | `version`, `sha256` (of the canonical profile JSON), `profile` (§5.11) |
| `profile.ack` | D→C | reply | `version`, `ok`, `error` (`stale` when ≤ the active version: still ok) |
| `command` | C→D | user action | `name` (§5.6), `args`, `actor` {user_id, user_name, role, step_up_at}, `issued_at`, `expires_at` (ms) |
| `command.result` | D→C | reply | `ref` (command id), `ok`, `code` ("ok" or §13 code), `status`, `message`, `result` |
| `tunnel.request` / `tunnel.response` / `tunnel.cancel` | C→D / D→C / C→D | §5.7 | |
| `token.issue` | C→D | claim, rotation | `token`, `issued_at` |
| `sos.sync` | C→D | after welcome, for requests the cloud advanced while the device was offline | `request_id`, `cloud_state`, `cloud_version`, `authority_side`, `events` [cloud-origin event dicts] |
| `error` | both | | `code`, `status`, `message`, `ref` |

Binary messages (frames, body chunks): §5.9.

### 5.5 Outbox events (D→C, at-least-once)

* The device writes every event to `link_outbox` (`link_store.enqueue(kind,
  payload, priority, dedupe_key)`); `seq` is the autoincrement and never
  changes. Priority 0 = SOS kinds (`protocol.SOS_EVENT_KINDS`), 1 = the rest.
* After `welcome` the client sends pending rows ordered by (priority, seq), at
  most `outbox_window` unacked; `ack` marks them; unacked rows are re-sent on
  every reconnect. Acked rows are pruned after 7 days.
* The Portal stores each `(device_id, seq)` in `device_events` (primary key) in
  the same transaction as its effect, then acks. A duplicate is re-acked and not
  re-applied. The Portal never assumes seq order across kinds: an SOS event may
  reference an incident not mirrored yet.
* SOS events of one request are applied in **request event seq** order; a gap
  is buffered until filled.

| Kind | Payload |
|---|---|
| `incident.upsert` | the `database` "incident" listener dict: id, session_id, ongoing, started_at, duration_seconds, peak_level, peak_tci, incident_type, reason, frames, evidence_count, snapshot_path, evidence_bundle, evidence_hash, person_present, authorized, actions |
| `dispatch.upsert` | `database.get_dispatch()` dict (status, request_id, chosen_authority, sent_at/by/to, error, …) |
| `verdict.set` | incident_id, verdict, note, by, channel, effect (`label_only`/`packages_cancelled`/`request_cancelled`/`resident_safe`), at |
| `sos.created`, `sos.transition`, `sos.event`, `sos.snapshot` | §7.9 |
| `evidence.meta` | bundle_id, incident_id, created_at, level, tci, sha256, camera_id, detections [{label, conf}] (no image) |
| `snapshot.meta` | name, incident_id, created_at (no image) |
| `gallery.audit` | event (gallery_enrolled/removed/rejected), person, file, via, by (no image) |
| `mode.changed` | armed, by, at |
| `remote.audit` | rows of `remote_audit` (batched) |
| `notification.status` | id, request_id, channel, recipient_ref, recipient_masked, step, status, error, attempts, sent_at |
| `node.alert` | kind (over_temp, under_voltage, mains_lost, mains_restored, tamper), at, detail |

The link client registers `database.add_listener("incident"|"dispatch"|"verdict", fn)`
to enqueue the first three; the others are enqueued by their owners.

### 5.6 Commands (C→D allowlist, device-enforced)

The device does not trust the Portal. Every command carries the Portal user id
and role; the device checks its own rules, writes a `remote_audit` row
(`link_store.record_remote`) and answers `command.result`. Anything not listed
is refused with `unknown_command`. No command runs a shell or code, disables
the siren or the evidence vault.

| Command | Args | Device rule |
|---|---|---|
| `sos.respond` | `request_id`, `action` (approve, decline, checking, safe, help_now), `authority` (POLICE/FIRE/MEDICAL), `pin` (decline/safe), `channel` | owner/member; contact `approve` only with safety defaults on and CONFIRMED; PIN checked on the device against the profile hashes (duress PIN → approve + duress); 5 PIN tries / 10 min; never logs the PIN |
| `sos.update` | `request_id`, `event` (acknowledge, resolve, delivered, delivery_failed, contacts_notified, device_offline, seen), `detail` | cloud-side events; applied through `states.next_state` |
| `verdict.set` | `incident_id`, `verdict`, `note`, `pin` (FALSE with an open request) | owner/member; `database.set_verdict(..., by="portal:<user>")` |
| `mode.set` | `armed`, `pin` | arm: owner/member; disarm: SOS PIN |
| `siren.test` | — | owner + step-up ≤ 300 s; ≤ 1/min; 1 s |
| `sms.send` | `to`, `text`, `request_id` | only owner, CONFIRMED contacts, Desk numbers of the current profile; ≤ 30/day; through the router (guard applies) |
| `token.rotate` | — | owner + step-up; answered by `token.issue` |
| `unpair` | — | owner + step-up; wipe token + profile cache |
| `diag.ping` | — | anyone with tunnel access |
| `demo.close_incident` | — | test mode only (`test_mode_only` otherwise); closes the open incident so the next scene opens a new request |
| `demo.trigger` | `incident_type` | test mode only; disclosed scripted L3 marked `simulated` / "SIMULATED DETECTION" in text and audit |

`command.expires_at` passed → `timeout`. The Portal queues commands for an
offline device in `device_commands` (verdicts show "queued, will sync").

### 5.7 Tunnel

`tunnel.request` (C→D): `stream` (u32, unique per connection), `method`,
`path` (absolute, starts with `/`, no scheme/host, no `..`), `query` (without
`?`), `headers` [[name, value]] — the Portal forwards only `accept`,
`accept-language`, `content-type`, `content-length`, `x-csrf-token`,
`x-requested-with`, `if-none-match`, `user-agent` — `body_b64` (≤ 256 KiB) or
`body_len` + `chunked: true` followed by BODY chunks, `actor` {user_id,
user_name, role, step_up_at}, `base` (`/d/<device_id>`), `csrf` (the Portal
session's CSRF token), `timeout_ms`.

`tunnel.response` (D→C): `stream`, `status`, `headers` [[name, value]] (the
device drops hop-by-hop headers and every `set-cookie`), `body_b64` or
`body_len` + `chunked` + BODY chunks, `elapsed_ms`. `tunnel.cancel` (C→D):
`stream`.

Executor (T-LINK): replays the request into the LC app in-process with
`httpx.AsyncClient(transport=httpx.ASGITransport(app=lc_app))`, path
unchanged (no root_path), adding `tunnel_auth.build_headers(...)`. Limits:
request ≤ 11 MiB (the Portal also caps while streaming), response ≤ 16 MiB,
`tunnel_timeout_seconds` (60 s for uploads/captures), ≤ 8 in flight (else 503
`device_busy`). The Portal adds `Cache-Control: no-store` to `/media/*` and
`/api/*`, `public, max-age=3600` to `/static/*`.

Never tunnelled (refused by both sides): `/login`, `/logout`, `/setup/*`,
`/docs`, `/redoc`, `/openapi.json`, `/video` and `/ws/*` (relayed instead).

Relays:
* `/d/{id}/video` → `video.subscribe`; the device pushes FRAME messages from
  `pipeline.latest_jpeg(70)` (one encode per frame, shared with the LC MJPEG)
  only when `preview_seq` changed, at most `max_fps` (adaptive 2–15 fps from the
  measured uplink); the Portal fans out `multipart/x-mixed-replace;
  boundary=frame` to each viewer and shows "remote N fps · relay delay N ms".
  The LC keeps full rate.
* `/d/{id}/ws/threat` → `state.subscribe`; each `state.payload` is sent as the
  same JSON text the LC websocket sends. When the heartbeat is lost the Portal
  overrides `camera_online=false`, `ai_hz=0` and shows the Link dot red.

Device-enforced path rules (T-LINK `sentrix/link/policy.py`, before the ASGI
call; every request → `remote_audit`):

| Path class | Roles | Extra |
|---|---|---|
| pages and read APIs (`/dashboard`, `/live`, `/events`, `/alerts`, `/evidence`, `/dispatch`, `/authorized`, `/sos`, `/node`, `/api/*` GET, `/static/*`) | owner, member | |
| media (`/media/*`, `/api/authorized/preview.jpg`), video relay | owner, member | consent `remote_view`; faces also `face_recognition`; ≤ 60/min per user |
| gallery writes (`POST /authorized/upload`, `POST /api/authorized/capture`, `DELETE /api/authorized/*`) | owner | consent `face_recognition`; DELETE needs step-up |
| `POST /api/incidents/*/verdict`, `POST /api/sos/*`, `POST /api/dispatch/*` | owner, member | |
| `POST /api/node/siren-test` | owner | step-up; 1/min |
| Desk view: `GET /dashboard`, `/live`, `/static/*`, `/api/health`, `/api/metrics`, video and state relays | operator | only while a request of this device is on the help path **and** consent `desk_live_view` |
| anything | company_admin | always refused |

### 5.8 Tunnel auth bridge *(foundation: `sentrix/link/tunnel_auth.py`)*

| Header | Value |
|---|---|
| `x-sentrix-tunnel` | per-process secret (`process_secret()`, `secrets.token_urlsafe(32)` at import, memory only) |
| `x-sentrix-user-id` | Portal user id |
| `x-sentrix-user-name` | display name (sanitised, ≤ 100) |
| `x-sentrix-role` | owner / member / operator (`TUNNEL_ROLES`) |
| `x-sentrix-step-up-at` | unix seconds of the last step-up, or "" |
| `x-sentrix-request-id` | tunnel request id |
| `x-sentrix-base` | `/d/<device_id>` (validated `^/d/[A-Za-z0-9_-]{1,64}$`) |
| `x-sentrix-csrf` | the Portal session's CSRF token |

* `verify(headers) -> TunnelIdentity | None` (constant-time secret check; bad
  role/base/user → None). `TunnelIdentity`: `user_id`, `user_name`, `role`,
  `step_up_at`, `request_id`, `base`, `csrf`, `.actor` (`portal:<id>`),
  `.stepped_up_within(seconds, now)`.
* LC hardening (T-WEB, `sentrix/web/hardening.py`): verify on every request;
  store at `scope[tunnel_auth.SCOPE_KEY]`; **strip all `BRIDGE_HEADERS`** from
  requests that fail. `require_auth` accepts a verified identity. The bridge
  headers coming from the network are therefore inert.
* CSRF bridging: the Portal validates **its own** CSRF token before forwarding
  any state-changing tunnel request; it passes `x-sentrix-csrf` so the LC page
  renders `<meta name="csrf-token" content="...">` and its scripts send it back
  as `X-CSRF-Token` (which the Portal checks next time). The LC does not re-check
  CSRF for verified tunnel identities; it checks its own token for LC sessions.
* Cookies: LC `lc_session` (device admin), Portal `portal_session` (+
  `portal_csrf` before login). Distinct names; dev uses distinct hosts.
* `tunnel_auth.base_for(request)` → `/d/<id>` or "" (§9.5).

### 5.9 Binary messages

16-byte big-endian header `>BBHIQ`: `kind u8 | version u8 (1) | flags u16 |
a u32 | b u64`, then the payload.

| Kind | a | b | flags | payload |
|---|---|---|---|---|
| `0x01` FRAME | frame seq (wraps) | capture wall time, ms | 0 | JPEG ≤ 512 KiB |
| `0x02` BODY | tunnel stream id | chunk index | bit 0 = last chunk | ≤ 256 KiB |

`pack_frame`, `pack_body_chunk`, `unpack_binary` (raises `ValueError`).

### 5.10 Error handling

* Malformed JSON / missing envelope → `error{invalid_args}`; the connection
  stays up. Three protocol errors within 10 s → close 1008.
* A command or tunnel request the device cannot serve → `command.result` /
  `tunnel.response` with the §13 code and HTTP-like status; never a silent drop.
* The device treats any send failure as disconnect: rows stay unacked.
* Clock skew: the Portal records `received_at` beside every device `at`.

### 5.11 Profile JSON (`profile.push`)

Built by T-ACCT `services/accounts.build_profile`, pushed by T-LINK, stored by
the device encrypted (`profile_cache`, key HKDF(EVIDENCE_AES_KEY,
`b"sentrix-profile-v1"`): protection against casual copying only). Templates
are rendered on the device with `jinja2.sandbox.SandboxedEnvironment` (they
come from the network) and fall back to the built-in English templates.

```json
{
  "schema": 1, "version": 7, "issued_at": "2026-10-14T09:00:00+00:00",
  "device_id": "…", "household_id": "…", "customer_code": "SX-0007",
  "language": "en", "test_mode": true,
  "owner": {"user_id": "…", "name": "Riya Sharma (demo)", "phone": "+91XXXXXXXXXX",
            "phone_verified": false},
  "members": [{"id": "…", "name": "…", "relation": "…", "age_band": "adult",
               "user_id": null}],
  "contacts": [{"id": "…", "name": "…", "relation": "…", "phone": "+91XXXXXXXXXX",
                "telegram": true, "priority": 1, "consent_status": "CONFIRMED",
                "can_reach_30min": true}],
  "desk": {"name": "SENTRIX Desk (simulated)", "phone": "+91XXXXXXXXXX", "simulated": true},
  "address": {"house": "…", "building": "…", "street": "…", "locality": "…",
              "landmark": "…", "city": "…", "district": "…", "state": "…",
              "pincode": "147001", "floor": "…", "entry_note": "…",
              "lat": 30.3398, "lon": 76.3869, "map_url": "https://maps.google.com/?q=30.3398,76.3869"},
  "household": {"occupants": {"adults": 2, "children": 1, "seniors": 1},
                "pets": "…", "medical_notes": null},
  "nearest": {"police": {"name": "…", "phone": "…"}, "fire": {"name": "…", "phone": "…"},
              "hospital": {"name": "…", "phone": "…"}},
  "cameras": [{"camera_id": "CAM_0", "room_label": "Living room", "floor": "Ground"}],
  "sos": {"window_seconds": 1800, "safety_defaults": true,
          "contacts_for_single_source_l3": false,
          "pin_hash": "$argon2id$…", "duress_pin_hash": "$argon2id$…", "pin_version": 3,
          "channels": {"telegram": true, "email": true, "sms": true},
          "templates": {"version": "en-1", "full": "…", "sms": "…", "prompt_sms": "…"}},
  "consents": {"account_sos": true, "face_recognition": false, "medical_notes": false,
               "sos_snapshot": true, "remote_view": true, "desk_live_view": false,
               "model_improvement": false, "notice_version": "2026-10-1"},
  "mode": {"armed": false}
}
```

Rules: `medical_notes` only when consent `medical_notes` **and** "include in
SOS" are on. Only CONFIRMED contacts receive an SOS; the device derives its SMS
allowlist = owner + CONFIRMED contacts + Desk. **Never** in a profile: Portal
password hashes, TOTP secrets, session ids, contacts' e-mail addresses,
recovery codes. PIN hashes are Argon2id (`sentrix_portal.security.hash_pin`);
the device verifies with `argon2-cffi`.

---

## 6. Data model

### 6.1 Device SQLite (`var/sentrix.db`) *(foundation)*

Migration rule: `create_all` adds missing tables; `_ADDED_COLUMNS` adds missing
columns (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`); `_ADDED_INDEXES`
creates missing indexes. Nothing is dropped or rewritten.

New columns:

| Table | Columns |
|---|---|
| `incidents` | `verdict_by` (str 64: `lc:device_admin`, `portal:<user>`, `desk:<op>`), `verdict_at` (UTC), `request_id` (indexed) |
| `dispatch_packages` | `request_id` (indexed), `chosen_authority` (POLICE/FIRE/MEDICAL), `sent_at`, `sent_by`, `sent_to_json` (masked list), `error`, `expires_at`, `updated_at`; statuses PENDING, SENT, CANCELLED, **EXPIRED**, **FAILED** (`database.DISPATCH_STATUSES`) |
| `enrolled_faces` | `member_id`, `consent_id`, `enrolled_by` |

New tables (`sentrix/storage/models.py`): `assistance_requests`
(`AssistanceRequestRecord`), `request_events` (`RequestEventRecord`, hash
chain per request, `restricted` rows hidden from owners), `link_outbox`,
`profile_cache`, `notifications` (router outbox), `remote_audit`. Column lists
are in the model docstrings; the request fields mirror §7.9's payload.

CRUD *(foundation, then owned by the track)*: `storage/requests.py`
(`create_request` idempotent, `transition` through `states.next_state`,
`append_event`, `get_request`, `open_request`, `list_requests`, `events`,
`verify_chain`), `storage/link_store.py` (`enqueue`, `pending`, `mark_sent`,
`ack`, `depth`, `save_profile`, `active_profile`, `wipe_profiles`,
`record_remote`, `recent_remote`), `storage/notifications.py` (`SqlOutbox`),
`database.link_incident_request`, `database.update_dispatch_status(id, status,
**fields)`, `database.recent_incidents(limit ≤ 500, offset)`.

### 6.2 Portal tables *(foundation: `sentrix_portal/models.py`)*

| Table | Purpose / key columns |
|---|---|
| `households` | customer_code, address fields, lat/lon, entry_note, occupants (JSON), pets, medical_enc, include_medical, nearest (JSON), language, sos_window_s, safety_defaults, sos_pin_hash, duress_pin_hash, pin_version, channels (JSON), profile_version, onboarding_step/completed_at, status, closed_at |
| `users` | username (unique), display_name, email(+verified_at), phone_enc(+verified_at), role (`ROLES`), household_id, status (`pending_activation`/`active`/`disabled`/`closed`), password_hash (null until activation), password_changed_at, totp_secret_enc, totp_enabled, recovery_codes (digests), last_login_at/ip_hash |
| `activation_codes` | user_id, purpose (activate/reset), code_hash (unique), expires_at (72 h), used_at, attempts, created_by |
| `sessions` | id_hash (pk = HMAC of the cookie value), user_id, expires_at, last_seen_at, ip_hash, user_agent, csrf_token, totp_verified, step_up_at, revoked_at |
| `login_attempts` | username, ip_hash, at, success (back-off per (username, IP)) |
| `household_members` | household_id, name, relation, age_band, user_id, consent_id |
| `emergency_contacts` | household_id, name, relation, phone_enc, email, telegram_chat_id, priority, can_reach_30min, consent_status (`PENDING`/`CONFIRMED`/`DECLINED`/`REVOKED`), confirmed_at |
| `consents` | household_id, subject_type/id, purpose (`CONSENT_PURPOSES`), notice_version, granted_at, withdrawn_at, method, actor_user_id |
| `devices` | serial (unique), household_id, name, state (`UNASSIGNED`/`ASSIGNED`/`CLAIMED`/`RETIRED`), claim_code_hash, bootstrap_hash, token_hash, token_rotated_at, firmware, last_seen_at, last_hello, last_heartbeat, online, armed, last_acked_seq, profile_version_acked, assigned/claimed_at, claimed_by |
| `claim_attempts` | serial, user_id, ip_hash, at, success |
| `cameras` | device_id + camera_id (unique), room_label, floor |
| `device_events` | (device_id, seq) pk, kind, received_at, payload — outbox dedupe |
| `device_commands` | device_id, name, args, user_id, created/sent_at, result, status (QUEUED/SENT/DONE/FAILED) |
| `assistance_requests` | the device's request fields + device_id, household_id, desk_operator_id, desk_acknowledged_at |
| `request_events` | (request_id, seq, origin) unique; device and cloud events; restricted |
| `sos_snapshots` | request_id (unique), JPEG bytes, sha256, expires_at (30 d) |
| `notifications` | the Portal router outbox (address_enc, dedupe_key unique, status, attempts, next_attempt_at, …) |
| `action_tokens` | token_hash, purpose (`contact_page`, `contact_consent`, `action_link`, `telegram_link`, `email_verify`), household/request/subject, nonce, expires_at, used_at, revoked_at, opened_count |
| `telegram_links` | subject (user/contact), household_id, chat_id (private chats only), status |
| `push_subscriptions` | Tier 2 |
| `incidents_mirror`, `dispatch_mirror`, `evidence_meta_mirror`, `snapshot_mirror` | metadata for offline pages; never images |
| `audit_log` | append-only, `hash = HMAC(secret, prev_hash + canonical row)` (`security.audit`, `verify_audit_chain`) |

Retention caps (override every prune exemption): SOS snapshot 30 d;
unreviewed incidents 180 d; TRUE 1 y; audit logs 1 y; erasure on closure.
L4/L5 and unreviewed incidents keep the device's never-prune rule inside these
caps; pruned evidence leaves a signed tombstone so `verify_chain` reports a
retention gap, not tampering (T-SOS/integration).

### 6.3 Database change listeners *(foundation)*

`database.add_listener(kind, fn)` / `remove_listener`; kinds `incident`
(after `save_incident`), `dispatch` (after `save_dispatch` /
`update_dispatch_status`), `verdict` (after `set_verdict`). Called after the
commit with a plain dict, on the writer's thread (usually the pipeline's FIFO
worker): enqueue only. A failing listener is logged and never breaks the
write. A service registers its listeners in `start()` and removes them in
`stop()`. Nothing registers in tests, replays or tools.

---

## 7. SOS

### 7.1 When a request opens (D8)

A request opens only at a **confirmed L3+ commit** of `sos.alarm_classes`
(weapon, fire) — experimental classes are capped at L2 and never prompt — and,
in armed mode, for an `intrusion` that outlives the 30 s entry delay (disarm by
SOS PIN on LC/Portal or an authorised face). No siren at L3. One open request
per device: later incidents (including the 300 s rollover) attach to it.

### 7.2 The pipeline hook *(foundation: `sentrix/sos/__init__.py`, `engine.py`)*

```python
class SosService(Protocol):
    name: str
    def wants(self, incident_type: str, level: int) -> bool   # inference thread; O(1), no I/O
    def trigger(self, trigger: SosTrigger) -> None             # FIFO worker; enqueue only; idempotent per incident
    def on_level(self, change: LevelChange) -> None            # inference thread; enqueue only
    def status(self) -> dict                                   # the state.sos block (owner view)
    def start(self) -> None; def stop(self) -> None; def health(self) -> dict
```

* `ActionPlan.sos = level >= settings.sos.min_level`. In `_act`, when
  `plan.sos`, `"sos"` is not yet in `incident.actions` and `sos.wants(type,
  level)`, the pipeline adds `"sos"` and queues `_sos_trigger` **after** the
  snapshot and evidence submits (same FIFO), or runs it inline when the queue
  is full (never dropped). An incident that starts as an unwanted type and
  later becomes a weapon still triggers.
* `SosTrigger`: incident_id, level, incident_type, evidence_state, tci,
  reason, session_id, camera_id, armed, at_wall, at_mono, snapshot_path,
  evidence_bundle, evidence_hash, votes, simulated.
* `LevelChange` on every committed level change (both directions): level,
  previous_level, incident_id, incident_type, evidence_state, tci, at_wall,
  at_mono. The same call records `state.record_level_change` (the dashboard's
  Event Log history, last 50).
* `NullSosService` is every `Pipeline`'s default. The lifespan installs the
  real one only when `SENTRIX_SOS_ENABLED=1`. The eval harness sets it
  explicitly and calls `assert_measurement_safe`.
* The service never raises into the pipeline; it must not block it.

### 7.3 States *(foundation: `sentrix/sos/states.py`)*

| State | Meaning | Terminal |
|---|---|---|
| DRAFTED | created at the L3 commit; text rendered, snapshot/evidence attached | no (≤ 2 s) |
| PENDING | owner asked; window running | no |
| APPROVED | help asked for (owner, member, contact via safety defaults, duress, SMS "1") | no |
| DISPATCHED | ≥ 1 target (Desk or a contact) accepted it | no |
| ACKNOWLEDGED | Desk or a contact took it on | no |
| FAILED | approved, no target reachable after retries; retried every 2 min; LC/owner: "SOS NOT DELIVERED, call 112 now" | no |
| CLOSED | resolved with an outcome (help path only) | yes |
| DECLINED | false alarm with the SOS PIN | yes |
| EXPIRED | window ran out unanswered (label "Unanswered", never "safe") | yes |
| CANCELLED | Events-page FALSE verdict while prompting, test reset, account closed | yes |

Help path: APPROVED, DISPATCHED, ACKNOWLEDGED, FAILED, CLOSED. Approved
requests never expire. Flags: `duress`, `device_offline`, `contacts_notified`,
`resident_safe`, `repeat`, `stale`, `undelivered`, `unacknowledged`,
`simulated`. Urgency NORMAL/HIGH.

### 7.4 Transitions (`states.TRANSITIONS`, `SELF_EVENTS`, `next_state`)

| From | Event | To | Side effects (engine) |
|---|---|---|---|
| — | `created` | DRAFTED | render from profile; package PENDING (`request_id`); priority outbox `sos.created`; `sos.snapshot` if consented |
| DRAFTED | `prompted` | PENDING | `expires_at = created + window`; prompt LC (+ Portal/Telegram/e-mail via the cloud; modem SMS if offline) |
| DRAFTED/PENDING | `approve` | APPROVED | actor, channel, authority recorded; reminders stop |
| DRAFTED/PENDING | `decline` | DECLINED | verdict FALSE; package CANCELLED; notified contacts get "resolved by resident" |
| DRAFTED/PENDING | `cancel` | CANCELLED | (verdict path, test reset, closure) |
| PENDING | `expire` | EXPIRED | package EXPIRED; owner "lapsed unanswered, please review"; notified contacts "request lapsed"; incident stays unreviewed |
| APPROVED/FAILED | `delivered` | DISPATCHED | package SENT (authority, sent_at, sent_by, sent_to) |
| APPROVED | `delivery_failed` | FAILED | |
| FAILED | `retry` | APPROVED | |
| APPROVED/DISPATCHED/FAILED | `acknowledge` | ACKNOWLEDGED | owner told "Acknowledged by …" |
| DISPATCHED/ACKNOWLEDGED | `resolve` | CLOSED | outcome → verdict (`states.OUTCOMES`) |
| DISPATCHED/ACKNOWLEDGED | `stale_close` | CLOSED | flag `stale` |
| open states | `attached`, `seen`, `level_rise`, `contacts_notified`, `device_offline`, `device_online` | same | `level_rise` (L4/L5): urgency HIGH, `expires_at = max(expires_at, now + window)`, immediate reminder, contacts at once (safety defaults), text says "confirmed by two different models" |
| PENDING | `checking`, `reminder`, `lapse_warning` | PENDING | `checking` pauses reminders `seen_snooze_seconds`, never expiry or safety timers |
| PENDING/APPROVED/FAILED | `undelivered` | same | |
| APPROVED…FAILED | `resident_safe` | same | Desk told; only the Desk or a contact closes |
| DISPATCHED/FAILED | `repage` | same | 2, 5, 10 min then hourly; cap 10 (3 in test mode) |
| any | `pin_failed`, `notification`, `conflict` | same | |
| EXPIRED/DECLINED/CANCELLED | `help_requested_later` | same | "Request help now": a **new** request with `parent_request_id`, pre-filled, audited |

Everything else is refused (`next_state` → None); terminal states absorb.

### 7.5 Policy, timers and safety defaults

The policy is **frozen into `policy_json` when the request opens**: window,
safety defaults on/off, `contacts_for_single_source_l3`, the confirmed contact
list, channels, profile version. Settings changes (fresh password/PIN needed)
apply to future requests only. Window = profile `sos.window_seconds` clamped to
`window_choices`; in test mode `demo_window_seconds` (≥ 60) when set.

Owner timeline for a 30-min window: T+0 (LC, Portal, Telegram, e-mail), T+2,
T+5, T+10 (e-mail again; SMS if unseen or offline), T+20, T+25 ("lapses in
5 min"), T+30 EXPIRED. Message dedupe key per (request, recipient, channel,
step); the legacy L4 owner SMS folds into the `level_rise` reminder.

Safety defaults (ON by default, D9) alert CONFIRMED contacts while the owner is
still asked: corroborated L4/L5 at once; confirmed fire at T+2 min; armed
intrusion at once; device offline / heartbeat lost during an open L2+ incident
at once (cloud side). Single-source weapon L3: owner only until gate P8
(`contacts_for_single_source_l3`, T+5 min when on). The duress PIN exists
whenever contacts exist and differs from the SOS PIN in ≥ 2 digits. Repeats:
after a DECLINED, the same threat within `repeat_window_seconds` opens at most
one new request flagged `repeat`; after EXPIRED a new L3 opens a new request.

### 7.6 Authority and sync (device ↔ cloud)

The device is the authority until the cloud has the request **and** has lost
the device's heartbeat (45 s); then the cloud runs the timers
(`authority_side = "cloud"`). On reconnect the cloud sends `sos.sync`; both
sides apply `states.reconcile(device_state, cloud_state)`: any help-path state
wins (the furthest along: FAILED < APPROVED < DISPATCHED < ACKNOWLEDGED <
CLOSED); otherwise the cloud's terminal state; otherwise the more advanced of
DRAFTED/PENDING. A change writes a `conflict` event and notifies the owner.
Events from the other side are appended with `origin` set; the chains stay
per-origin.

### 7.7 Verdict semantics by request state *(foundation: `database.set_verdict`)*

`set_verdict(incident_id, verdict, note, *, by, channel)` records
`verdict_by`/`verdict_at`; FALSE then depends on the incident's request:

| Request | Effect |
|---|---|
| none | cancel the incident's PENDING packages (pre-portal rule) |
| DRAFTED / PENDING | request CANCELLED (event `cancel`, reason `verdict_false`); its PENDING package CANCELLED (by `request_id`) |
| help path (not CLOSED) | flag `resident_safe` (event); package and help untouched |
| duress | nothing (the caller still shows success) |
| terminal | label only |

A FALSE verdict with an open request needs the SOS PIN at the route (T-SOS /
T-WEB). Last writer wins on the label; the Desk's resolve outcome is final for
the request. `send`/approve refuse SENT, CANCELLED, DECLINED and EXPIRED
packages with 409 (T-SOS in `dispatch.py`).

### 7.8 Duress

A duress PIN approves (event `approve`, `detail.duress = true`, `restricted =
true`) while every owner-facing surface shows "Marked as false alarm":
`states.owner_view(state, flags)` maps it to DECLINED and drops the flag. The
LC `state.sos` block, owner/member Portal pages and the owner's per-request
audit view never show restricted rows or the flag. Desk and contacts get the
DURESS banner: "Do not go to the home. Call 112."

### 7.9 SOS event payloads (device → cloud)

* `RequestPayload` = `requests.request_dict(row)` minus `snapshot_path` and
  `session_id`; includes the **full** flags (the cloud needs `duress` for the
  Desk and must apply `owner_view` for owners).
* `sos.created`: `{"request": RequestPayload, "event": EventDict}`.
* `sos.transition`: `{"request_id", "version", "request": RequestPayload,
  "event": EventDict}` (from ≠ to).
* `sos.event`: same shape, from = to (attached, seen, reminder, notification…).
* `sos.snapshot`: `{"request_id", "content_type": "image/jpeg", "sha256",
  "width", "height", "data_b64"}` — ≤ 256 KiB (640 px wide, q70), only with
  consent `sos_snapshot`.
* `EventDict` = `requests.event_dict(row)`: request_id, seq, at, from_state,
  to_state, event, actor_type, actor_id, channel, detail, restricted, origin,
  prev_hash, hash.

### 7.10 Wording (fixed)

Buttons: **"Alert my contacts and the SENTRIX desk"** (with Police / Fire /
Medical context), **"Call 112 now"** (shown first for weapon/fire; the Desk
number in test mode — never a `tel:112`/`sms:112` link in test mode),
**"I'm checking"**, **"False alarm (PIN)"**. SMS can only approve or raise,
never decline. Every SOS text ends "SENTRIX does not contact 112 itself."
Corroborated evidence is "confirmed by two different models" (never
"independent camera checks"). Single-source L3: "possible (one camera check)".
Degraded camera: "camera view degraded (dark), detection unreliable". Test
messages start with the guard's banner. Telegram gets a short text and a
Portal link only (no photo, address or medical notes).

---

## 8. Channels

### 8.1 Send guard *(foundation: `sentrix/alerting/guard.py`)*

```python
may_send(channel, recipient, *, policy=None) -> Decision        # never raises; truthy when allowed
assert_may_send(channel, recipient, text=None, *, policy=None)   # raises SendRefused; checks the banner
prepare_text(channel, text, *, policy=None) -> str              # adds TEST_PREFIX (GSM variant for SMS)
SendPolicy.from_env(test_mode_env="SENTRIX_TEST_MODE")           # re-read on every send
status(policy=None) -> {"test_mode", "live_alerts", "channels_live", "test_recipients": count}
normalize_address(channel, address); is_emergency_number(address); test_mode()
```

Channels: `modem_sms`, `modem_call`, `twilio_sms`, `twilio_call`, `telegram`,
`email`, `webpush` (`CHANNEL_SWITCHES`). Order of rules (first match decides):

| # | Condition | Result |
|---|---|---|
| 1 | unknown channel, empty/malformed address | refused |
| 2 | emergency number (112, 100, 101, 102, 108, …) | **always** refused |
| 3 | `SENTRIX_LIVE_ALERTS` ≠ 1 | refused ("dry run") |
| 4 | channel switch ≠ 1 | refused |
| 5 | test mode and address ∉ `SENTRIX_TEST_RECIPIENTS` | refused |
| 6 | otherwise | allowed (test mode: banner required) |

Interpretation of D13: "real sending is refused while test mode is on" means
nothing reaches a **non-allowlisted** person in test mode; the final
rehearsals send to team phones (allowlisted) in test mode with both switches.
The banner is `TEST_PREFIX = "[SENTRIX DEMO – TEST, NOT A REAL EMERGENCY]"`
(`TEST_PREFIX_GSM` with "-" for SMS, which keeps GSM-7). The legacy notifier
(`twilio_sms`, `twilio_call`, dispatch `send_raw`) is guarded and builds no
Twilio client unless `SENTRIX_TWILIO_ENABLED=1` (*foundation*).

### 8.2 Interfaces *(foundation: `sentrix/alerting/channels/base.py`)*

* `Recipient(ref, name, channel, address, role)`: ref = `owner`,
  `member:<id>`, `contact:<id>`, `desk`, `user:<id>`.
* `Message(template, step, text, request_id, subject, html, buttons, urgency,
  created_at)`; `dedupe_key(recipient) = "request|ref|channel|step"`.
  Templates: `sos.prompt`, `sos.reminder`, `sos.lapse_warning`, `sos.lapsed`,
  `sos.full`, `sos.sms`, `sos.offline_prompt`, `sos.resolved`, `sos.duress`,
  `contact.consent`, `account.activation`, `account.reset`, `legacy.l4_sms`,
  `legacy.l5_call`, `channel.test`.
* `Button(label, action, url, callback_data)` (Telegram `callback_data =
  "<request id>:<nonce>:<action>"`).
* `SendResult(ok, provider_id, error, retryable, suppressed)`.
* `Transport` protocol: `name`, `send(recipient, message) -> SendResult` (never
  raises). `FakeTransport` records; tests use only fakes.
* `Channel(name, transport, policy_factory)`: guard → banner → transport. A
  refusal is `SUPPRESSED` (final) with the reason and the rendered text kept.
* `Outbox` protocol (`add` → False on duplicate key, `due(now, limit)`,
  `update(item)`): `MemoryOutbox` (tests), `storage.notifications.SqlOutbox`
  (device), the Portal's `notifications` table (T-CH).
* `ChannelRouter(channels, outbox, max_attempts=3, backoff=(30, 90),
  failed_retry_seconds=120)`: `register`, `enqueue` (id or None), `pump(now)`,
  `status()`. Statuses QUEUED, SENDING, SENT, DELIVERED, FAILED, SUPPRESSED.
  Per-channel cooldowns (SMS 300 s, call 600 s) stay for the legacy path only.

### 8.3 Who sends what

| Side | Channels |
|---|---|
| Device | `modem_sms` (offline prompt, SOS to contacts/Desk, `sms.send`), `modem_call` (optional L5), legacy `twilio_*` (only when enabled), LC prompt, siren; `telegram`/`email` **only** in standalone mode (no link configured) |
| Portal | `telegram`, `email`, Desk console, contact pages; `twilio_sms` as an optional backup when the device is gone |

Standalone LC (no Portal, no contacts): `ALERT_PHONE_NUMBER` (or the profile
owner phone) stays the dispatch target. Every channel's readiness goes to
`state.channels`, `/api/health`, the Node page, `doctor`, and a red LC banner
"no outbound channel: SMS and calls will not be sent".

### 8.4 Inbound and links

* Telegram: private chats only; webhook `secret_token` checked
  (`X-Telegram-Bot-Api-Secret-Token`); `callback_data` carries request id +
  nonce (an `action_tokens` row); callbacks accepted only from the linked chat
  id; development uses `getUpdates` long polling. "False alarm" opens a Portal
  PIN page (a PIN typed in chat stays in its history).
* E-mail and SMS links open a confirmation page; the action happens only on a
  POST with CSRF. Tokens are single use, bound to request + recipient, expire
  with the request + 2 h, revoked on decline; every open is logged.
* Contact pages `/c/{token}`: per contact and request; medical notes only with
  consent; the snapshot only there (never a standalone link).
* Inbound SMS parsing is Tier 2 (offline approval happens on the LC).

---

## 9. Local Console (LC)

### 9.1 Hardening (T-WEB)

* External JS only (no inline handlers or scripts); CSP with a per-request
  nonce (`script-src 'self' 'nonce-…'`; `img-src 'self' blob: data:`;
  `connect-src 'self'` + ws(s) same origin; `frame-ancestors 'none'`).
* `X-CSRF-Token` header (from `<meta name="csrf-token">`) on every POST/PUT/
  PATCH/DELETE fetch; LC token = HMAC(session key, "csrf:" + session cookie),
  stateless and session-bound; tunnelled requests: §5.8.
* `innerHTML` with server or device data → `textContent`/DOM building;
  `data-*` attributes + `addEventListener` instead of `onclick='...'`.
* Cookie `lc_session` (HttpOnly, SameSite=Lax, Secure with
  `SENTRIX_SECURE_COOKIES=1`); `/video` and `/ws/threat` through the same
  verifier as `require_auth`; websocket Origin check; logout is a POST.
* API errors return JSON (401 for API/fetch, not a 307 to the login HTML).
* Fonts served locally; `/docs` off unless `SENTRIX_API_DOCS=1` (foundation).

### 9.2 New LC endpoints

| Method | Path | Track | Notes |
|---|---|---|---|
| GET | `/api/sos` | T-SOS | `{current, recent}` (current = owner view or null) |
| GET | `/api/sos/requests?limit&offset` | T-SOS | |
| GET | `/api/sos/requests/{id}` | T-SOS | request (owner view) + non-restricted events + chain status |
| POST | `/api/sos/requests/{id}/approve` | T-SOS | `{authority}` |
| POST | `/api/sos/requests/{id}/decline` | T-SOS | `{pin}`; 403 `pin_invalid`, 429 `pin_locked` |
| POST | `/api/sos/requests/{id}/checking` | T-SOS | |
| POST | `/api/sos/requests/{id}/safe` | T-SOS | `{pin}` |
| POST | `/api/sos/requests/{id}/help-now` | T-SOS | EXPIRED/DECLINED/CANCELLED → new linked request |
| GET | `/api/sos/preview` | T-SOS | rendered SOS text from the current profile |
| POST | `/api/sos/demo/close-incident` | T-SOS | test mode only |
| GET | `/api/node` | T-HW | telemetry (the `state.node` block + GPIO state) |
| POST | `/api/node/siren-test` | T-HW | device admin or owner + step-up; 1 s; ≤ 1/min |
| GET | `/api/link` | T-LINK | link status, claimed, portal host, outbox depth, viewers |
| GET | `/api/link/remote-audit?limit` | T-LINK | `remote_audit` rows |
| POST | `/api/link/reconnect` | T-LINK | device admin |
| GET | `/api/channels` | T-CH | per-channel readiness, guard status, outbox depth, last errors, modem |
| POST | `/api/channels/test` | T-CH | device admin; guarded test message to the owner number |
| GET | `/api/level-history` | T-WEB | `state.level_history()` |
| GET | `/sos`, `/node` | T-WEB | pages |
| GET/POST | `/setup/wifi` | T-WEB (+ T-HW `io/network.py`) | only while `SENTRIX_SETUP_MODE=1`; one-time code from the label |
| GET | `/api/incidents?limit&offset` | T-WEB | limit ≤ 500 (database caps it) |

### 9.3 `/api/health` additions

Foundation: `camera_source` redacted; `armed` from the runtime mode;
`services` (per-service status + health), `send_guard`, `test_mode`, `mode`.
Tracks add their fields through their service's `health()` (they appear under
`services.<name>`), not by editing the route.

### 9.4 Live state blocks *(foundation: `sentrix/pipeline/state.py`)*

Single writer per block:

| Key | Writer | Idle value |
|---|---|---|
| `sos` | T-SOS (`state.update(sos=service.status())`) | `SOS_IDLE` (`active: false`, …); owner view only, never `duress` |
| `link` | T-LINK | `LINK_IDLE` (`state: "disabled"`, `connected: false`, …) |
| `node` | T-HW | `NODE_IDLE` (all None, `profile: "laptop"`) |
| `channels` | T-CH | `CHANNELS_IDLE` (`outbound_ready: false`, `modem: null`) |
| `mode` | `sentrix.mode.set_armed` | `MODE_IDLE` |
| `remote_viewers` | T-LINK via `state.set_remote_viewers(n)` | 0 (HUD shows "REMOTE VIEW: n" when > 0) |
| `test_mode` | foundation | `settings.test_mode` |
| `dispatch_package` (legacy) | T-SOS until T-WEB moves the panel to `sos` | null |

`sos` fields: active, request_id, short_id, state, label, incident_id,
incident_type, level, urgency, evidence_state, created_at, expires_at,
remaining_s, seen, repeat, authority, recommended_authority, sos_text,
snapshot (URL via the base helper), actions (allowed buttons), last (the most
recent closed request: id, state, label, at). `state.level_history()` returns
the last 50 committed level changes.

### 9.5 URL and base helper

Paths on the device never change; the prefix comes from the tunnel identity.
`tunnel_auth.base_for(request)` → `/d/<id>` or "". T-WEB adds Jinja globals
`url(path)` (base + path) and `static_url(path)`, `active_page` in every page
context (nav highlighting by name, not by raw path), and `<body
data-base="{{ base }}">`; scripts read `document.body.dataset.base || ""` and
build every fetch, link, image and WebSocket URL from it. Python-built URLs
(`/media/snapshot/…`, `/media/face/…`, `vault.view_url`) go through the same
helper. A test renders every page through the tunnel and asserts every
`href`, `src`, fetch and WebSocket target starts with the base.

---

## 10. Roles

| Capability | device_admin (LC password) | owner | member | operator (Desk) | company_admin |
|---|---|---|---|---|---|
| LC pages, live video, state | ✓ | ✓ (tunnel; video needs `remote_view`) | ✓ (same) | only during a help-path request with `desk_live_view` | ✗ |
| Snapshots / evidence images | ✓ | ✓ (`remote_view`) | ✓ (`remote_view`) | ✗ | ✗ |
| Face gallery view / enrol / delete | ✓ | ✓ / ✓ / ✓ step-up (`face_recognition`) | view only | ✗ | ✗ |
| Verdict Real/False | ✓ | ✓ (PIN with an open request) | ✓ (same) | via resolve | ✗ |
| SOS approve / I'm checking | ✓ | ✓ | ✓ | ✗ | ✗ |
| SOS decline / I'm safe | ✓ (PIN) | ✓ (PIN) | ✓ (PIN) | ✗ | ✗ |
| Dispatch send (approve with authority) | ✓ | ✓ | ✓ | ✗ | ✗ |
| HOME/ARMED | ✓ (disarm: PIN) | ✓ (disarm: PIN) | ✓ (disarm: PIN) | ✗ | ✗ |
| Test siren | ✓ | ✓ step-up | ✗ | ✗ | ✗ |
| Profile, contacts, PINs, settings, privacy | — | ✓ (step-up for safety settings/PINs) | view | ✗ | ✗ |
| Members (invite/remove) | — | ✓ | ✗ | ✗ | ✗ |
| Desk queue, acknowledge, resolve | — | ✗ | ✗ | ✓ (TOTP) | ✗ |
| Customers, devices, provisioning, operators | — | ✗ | ✗ | ✗ | ✓ (TOTP; never sees a credential, video, faces or evidence) |
| Contacts (no login) | — | token pages only: confirm/opt out; SOS view; "I'm handling it" | | | |

Account rules (D14): username + single-use activation code (72 h, hashed, rate
limited); the company never sees a password; an admin "reset" re-sends an
activation link and notifies the customer; admin and operator need TOTP;
back-off per (username, IP), never a lockout that blocks help; an audit alert
when an admin action is followed by a customer login from a new IP.

---

## 11. Portal endpoints

| Area | Paths | Track |
|---|---|---|
| health | `GET /healthz` | foundation |
| auth | `GET/POST /login`, `POST /logout`, `GET/POST /activate/{token}`, `/account`, `/account/password`, `/account/2fa`, `/account/sessions` (revoke) | T-ACCT |
| home | `GET /`, `GET /home` | T-ACCT |
| onboarding | `/onboarding/{1..7}`, `GET /claim?sn=` | T-ACCT |
| household | `/household`, `/household/members`, `/household/contacts` | T-ACCT |
| settings | `/settings` (window, safety defaults, PINs, channels, Telegram link) | T-ACCT |
| privacy | `/privacy`, `/privacy/export`, `/privacy/erase` | T-ACCT |
| admin | `/admin`, `/admin/customers`, `/admin/customers/{id}/letter`, `/admin/devices`, `/admin/devices/{id}/label`, `/admin/operators`, `/admin/audit` | T-ACCT |
| device link | `WS /device/v1/link` | T-LINK |
| tunnel | `/d/{device_id}/{path:path}`, `GET /d/{id}/video`, `WS /d/{id}/ws/threat` | T-LINK |
| SOS | `/sos`, `/sos/{request_id}`, `POST /sos/{request_id}/{action}`, `GET/POST /a/{token}` | T-LINK |
| Desk | `/desk`, `/desk/requests/{id}`, `POST /desk/requests/{id}/acknowledge`, `POST /desk/requests/{id}/resolve` | T-LINK |
| contacts | `GET/POST /c/{token}` | T-LINK |
| hooks | `POST /hooks/telegram` | T-CH |

Every page and API uses `security.require_role(...)` (roles, TOTP, CSRF,
step-up); tenant isolation filters every query on the principal's
household. Unauthenticated routes (gate P2): `/login`, `/activate/*`,
`/static/*`, `/healthz`, `/c/*`, `/a/*` (confirmation only; POST needs CSRF),
`/hooks/telegram` (secret-checked), `/device/v1/link` (device auth).

Primitives *(foundation: `sentrix_portal/security.py`)*: `hash_password`,
`verify_password`, `hash_pin`/`verify_pin` (Argon2id m=19 MiB, t=2, p=1),
`new_token`, `token_digest`, `new_code`, `code_digest`, `ip_digest`,
`client_ip`, `seal`/`unseal`, `create_session` (new id every login),
`load_session`, `revoke_session`, `revoke_user_sessions`, `principal_of`,
`set_session_cookie`, `issue_login_csrf`/`verify_login_csrf`, `verify_csrf`,
`require_role(*roles, csrf=True, step_up=False)`, `audit`,
`verify_audit_chain`.

---

## 12. Redaction and privacy rules

* `sentrix.utils.redact.redact_url` on every camera source name, health field,
  heartbeat, link payload and log line that can carry a URL
  (`rtsp://u:p@host` → `rtsp://***@host`; secret query values masked). The
  capture source name is redacted at construction *(foundation)*.
* Phone numbers and e-mails are masked in UIs and logs (`mask_phone`:
  `+91 98xxxxxx10`, `mask_email`, `mask_recipient`).
* No Portal password hash, TOTP secret or session id ever reaches the device;
  no face image, embedding, evidence image or continuous video is stored on the
  Portal (viewing passes through in memory over TLS; not end-to-end encrypted —
  say so).
* Device-supplied strings are re-validated at Portal ingest (UUID regex for
  ids, `sanitize_filename` for names, class-name allowlist for types) and
  rendered with `textContent` only.
* Minors' faces are never enrolled in the capstone; demo data uses fictional
  people and consenting team members.
* "Stored in India; messages transit Telegram and the e-mail provider."

---

## 13. Errors, statuses, logging and audit

Error codes (`protocol.ERROR_CODES`, also used as `{"detail", "code"}` in new
JSON APIs): `invalid_args` 400, `unknown_command` 400, `unknown_type` 400,
`unsupported` 400, `unauthorised` 401, `forbidden_role` 403,
`consent_required` 403, `step_up_required` 403, `test_mode_only` 403,
`not_allowed_recipient` 403, `pin_invalid` 403, `not_found` 404, `conflict`
409, `too_large` 413, `unprocessable` 422, `pin_locked` 429, `rate_limited`
429 (with `Retry-After`), `daily_cap` 429, `internal` 500, `device_busy` 503,
`timeout` 504. Portal-specific: `csrf` 403, `totp_required` 403,
`device_offline` 503 (tunnel while the device is offline → offline page or JSON).

Device JSONL audit rows (`EventLog.audit(event, **detail)`): existing
`gallery_enrolled`, `gallery_removed`, `gallery_rejected` (+ `by`); new
`sos_created`, `sos_transition`, `sos_notification`, `verdict_set`,
`dispatch_sent`, `mode_set`, `login`, `login_failed`, `remote_access`,
`siren_test`, `profile_applied`, `link_connected`, `link_lost`,
`channel_suppressed`. Remote requests and commands also go to `remote_audit`.

Portal `audit_log` events: `auth.login`, `auth.login_failed`, `auth.logout`,
`auth.activation_issued`, `auth.activated`, `auth.password_changed`,
`auth.totp_enabled`, `auth.session_revoked`, `admin.customer_created`,
`admin.activation_resent`, `admin.device_provisioned`, `admin.device_assigned`,
`admin.device_transferred`, `device.claimed`, `device.claim_failed`,
`device.unpaired`, `device.token_rotated`, `device.online`, `device.offline`,
`profile.updated`, `profile.pushed`, `contact.added`, `contact.confirmed`,
`contact.declined`, `consent.granted`, `consent.withdrawn`, `sos.<event>`,
`desk.acknowledged`, `desk.resolved`, `contact_page.opened`,
`action_link.used`, `telegram.linked`, `telegram.callback`, `verdict.set`,
`mode.set`, `siren.test`, `privacy.export`, `privacy.erasure_requested`,
`security.admin_then_new_ip`.

---

## 14. Testing rules

* Never a real alert: transports are `FakeTransport`; the guard refuses by
  default; a socket guard (`tests/test_no_network.py`, integration) will allow
  only localhost.
* Databases are temporary files (patch `database.engine`/`SessionLocal`, or
  `create_app(PortalSettings(database_url=...))`). Never `var/sentrix.db`.
* No webcam or microphone: sources are files or fakes; `enable_audio=False`.
* Measurement code keeps `NullSosService` and calls `assert_measurement_safe`.
* Never tune or select on `wa02`.
* Each track adds `tests/test_<track>_*.py`; the full suite stays green
  (`G:\Sentrix\.venv\Scripts\python.exe -m pytest`).

---

## 15. Integration (after the tracks)

1. **Merge order** (each a fast-forward or a reviewed merge onto
   `sentrix/portal`, full suite green after each): T-HW, T-CLIP, T-CH, T-SOS,
   T-WEB, T-ACCT, T-LINK (last: it consumes SOS events, the web hardening and
   the account profile). T-MODEL lands models separately.
2. **End-to-end tests** (laptop, one process): Portal and device app wired by
   an in-memory link pair; scenarios approve, decline, expire, L4 rise, device
   offline, duplicate rollover, duress; parity contract test (every LC route
   directly vs through the tunnel: status + key DOM ids, production CSP/CSRF);
   cross-tenant isolation; XSS payloads inert on every page and the Desk.
3. **Tools**: `tools/provision_device.py` (identity + label), `tools/demo_reset.py`,
   `tools/bench_link.py` (remote fps, glass-to-glass p95, uplink, CPU),
   scripted trigger (test mode, disclosed).
4. **Deploy**: `deploy/portal/` (compose: Portal, Postgres on an internal
   network only, Caddy as the only published service on 80/443), Alembic
   baseline, backups + tested restore; `deploy/pi5/` (systemd, watchdog,
   NetworkManager failover, ufw LAN-only).
5. **Docs**: README (new switches; the Twilio breaking change), HARDWARE.md
   (pin table, connectivity phase, modem in the BOM), `CLAUDE.md` M10 status.
6. **Gates**, measured once on the final build alongside M9: P1 parity
   (contract test green, nothing removed); P2 isolation (0 cross-tenant leaks;
   only the routes listed in §11 unauthenticated); P3 SOS correctness (20
   scripted runs per path reach the expected state with a complete audit
   trail); P4 latency (L3 → phone prompt p95 ≤ 5 s online; approve → Desk
   ≤ 3 s; offline L3 → modem SMS p95 ≤ 60 s); P5 offline (uplink pulled: LC
   prompt and SMS work; 100 % of outbox events sync); P6 privacy (0 faces or
   evidence on the Portal; export/erasure verified); P7 no alerts in tests
   (socket guard, eval opens 0 requests, allowlist enforced); P8 prompt budget
   (≤ 1 false prompt per 3 h of normal footage; unlocks
   `contacts_for_single_source_l3`).

---

## 16. Open questions

1. D13 wording: confirm the reading in §8.1 (test mode allows only allowlisted
   recipients, with both switches) — the literal "refused while test mode is
   on" would make the final rehearsals impossible.
2. Siren relay polarity: roadmap v2 uses an NPN driver (`active_high=True`);
   HARDWARE.md §6 still says active-low and amber on GPIO6. T-HW must settle
   both on the bench and update HARDWARE.md.
3. Device id format: UUID (current model default) or a short `dev_<base32>`;
   the tunnel base pattern accepts both.
4. Whether owners may ever log in to the LC directly (currently: no — LC keeps
   `SENTRIX_PASSWORD` only; owners use the Portal or the LC via the tunnel).
5. The multi-process Pi layout (review blocker) is deferred by the
   single-process decision; revisit if Gate A fails with the link running.
