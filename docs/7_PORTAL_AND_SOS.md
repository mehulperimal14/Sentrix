# 7. Portal, Onboarding and SOS — Developer Contract

This is the contract every implementation track codes against. It turns the
accepted decisions (D8–D14, recorded in `CLAUDE.md` §6 / M10) and the
independent reviews into exact interfaces: file ownership, configuration,
database schemas, the device-link protocol, the tunnel and its auth bridge,
the SOS state machine, channels, roles and endpoints.

* **Precedence:** accepted decisions > review findings > the original design.
  Where this document and the code disagree, the code in the foundation commit
  is the reference and this document is fixed.
* **Foundation commits** on branch `sentrix/portal`: "feat(foundation):
  contracts, config, guard, schema, services, portal skeleton" and
  "fix(foundation): contract review" (30 integration gaps closed; §16 lists
  the ones answered differently). Everything marked *(foundation)* below exists
  and is tested in `tests/test_foundation.py` and
  `tests/test_foundation_contract.py`.
* **Shared files** (§2.2) are changed only by the foundation/integration owner.
  A track that needs a change there writes it in its hand-off note
  (`INTEGRATION_NOTES_<track>.md`) instead of editing the file. **Additive
  seams** (§2.4) are the exception: the named track may *append* the named
  function or class to a shared file and change nothing else in it.
* Tracks report their M10 status in their hand-off notes; only integration
  edits `CLAUDE.md`.

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
  Portal; the Pi node exposes modem and siren as node commands (docs/8_RASPBERRY_PI5_INTEGRATION.md T3).

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
`sentrix/actor.py`, `sentrix/listeners.py`, `sentrix/mode.py`,
`sentrix/paths.py`, `sentrix/storage/models.py`,
`sentrix/storage/database.py`, `sentrix/pipeline/state.py`,
`sentrix/pipeline/escalation.py`, `sentrix/pipeline/engine.py`,
`sentrix/pipeline/incidents.py`, `sentrix/pipeline/events.py`,
`sentrix/evidence/vault.py`, `sentrix/fusion/*`,
`sentrix/evaluation/harness.py`, `sentrix/alerting/guard.py`,
`sentrix/alerting/channels/base.py`, `sentrix/alerting/channels/__init__.py`,
`sentrix/link/__init__.py` (`publish`), `sentrix/link/protocol.py`,
`sentrix/link/tunnel_auth.py`, `sentrix/sos/__init__.py`,
`sentrix/sos/states.py`, `sentrix/utils/redact.py`, `sentrix/web/auth.py`,
`sentrix/web/extensions.py`,
`sentrix_portal/{app,db,models,settings}.py`, `sentrix_portal/routes/__init__.py`,
`sentrix_portal/services/__init__.py`, `requirements.txt`, `pyproject.toml`,
`.env.example`, `CLAUDE.md`, this file, `tests/conftest.py`,
`tests/test_foundation.py`, `tests/test_foundation_contract.py`.

Frozen names inside track-owned files (the owner keeps them working and does
not change their signatures): `storage/requests.py` (`REQUEST_LOCK`,
`append_event`, `create_request`, `transition`, `try_transition`,
`import_event`, `request_dict`, `event_dict`), `alerting/dispatch.py`
(`SendOutcome`, `DispatchService.send(package_id, authority, *, by)`),
`io/actuators.py` (`build_siren`, `Actuator.activate/deactivate/name/
is_real`, `siren_test`), `link/profile.py` (every public function),
`web/routes.py` (`require_auth`, `health_payload`), `sentrix_portal/
services/{hub,sos,accounts,notify,retention}.py` (every public function),
`sentrix_portal/security.py` (`require_role`, `audit`, the primitives of §11).

### 2.3 Track-owned files (create or edit freely)

| Path | Owner | State after foundation |
|---|---|---|
| `sentrix/io/picamera.py` (`PiCameraSource`, guarded `picamera2` import; `latest_lores()` for the clip engine's low-resolution stream) | T-HW | to create |
| `sentrix/io/gpio.py` (`build_service`, `preflight`: LEDs, buzzer, PIR, door, mains-sense) | T-HW | to create |
| `sentrix/io/actuators.py` (siren on gpiozero behind the frozen `build_siren`/`Actuator`/`siren_test`) | T-HW | rewrite; `siren_test` (foundation) |
| `sentrix/io/sources.py` (add `picamera:` spec; keep the redacted names) | T-HW | edit |
| `sentrix/io/sensors.py`, `sentrix/io/node.py` (`build_service`, `preflight`), `sentrix/io/network.py` (nmcli: `scan() -> [{ssid, signal, security}]`, `connect(ssid, psk) -> {ok, error}`, `status() -> {connected, ssid, ip, via}`) | T-HW | to create |
| `sentrix/web/node_routes.py` (`GET /api/node`, `POST /api/node/siren-test` via `actuators.siren_test`) | T-HW | empty router |
| `run.sh` (Pi path), `requirements-pi.txt`, `deploy/pi5/**`, `tools/doctor.py` (hardware checks; services print through `preflight`) | T-HW | edit/create |
| `sentrix/pipeline/clip.py` (`ClipBuffer`, `ClipScheduler`, `ClipEngine`, `ClipScores`, `build_strategy(pipeline) -> InferenceStrategy`, `prepare_eval_pipeline(pipeline) -> Pipeline`), `sentrix/pipeline/fastlane.py`, `sentrix/runtime/**` | T-CLIP | to create |
| `tools/export_models.py`, `tools/bench_clip.py` (calls `services.assert_measurement_safe`), `tools/replay_custom_videos.py` (`--mode clip` via `build_eval_pipeline(mode="clip")`), `sentrix/perception/identity.py` (read `settings.identity.face_model`), `sentrix/perception/scene.py` (runtime backends; loading T-MODEL's checkpoints) | T-CLIP | create/edit |
| `sentrix/sos/service.py` (`build_service`, `preflight`), `machine.py`, `policy.py`, `templates.py` (the one English template source; the Portal imports it), `sweeper.py`, `pins.py` (the shared PIN limiter, 5 / 10 min, every caller) | T-SOS | to create |
| `sentrix/storage/requests.py` | T-SOS | CRUD + lock + chains (foundation) |
| `sentrix/alerting/dispatch.py` (profile-filled packages, one per request) | T-SOS | `SendOutcome` (foundation) |
| `sentrix/web/sos_routes.py` (`/api/sos/*`, thin wrappers over the SOS service) | T-SOS | empty router |
| `sentrix/alerting/channels/router.py` (`build_service` -> a `ChannelsService`, `preflight`), `telegram.py`, `email.py`, `modem.py`, `twilio.py` | T-CH | to create |
| `sentrix/alerting/notifier.py` (route L4 SMS / L5 call through the router when SOS does not own the incident; keep the guard) | T-CH | guarded (foundation) |
| `sentrix/storage/notifications.py` | T-CH | `SqlOutbox` with meta (foundation) |
| `sentrix/web/channel_routes.py`; `sentrix_portal/routes/hooks.py`; `sentrix_portal/services/notify.py` (transports, outbox, worker, inbound Telegram) | T-CH | empty / stub |
| `sentrix/web/routes.py`, `streaming.py`, `security.py`, `hardening.py`, `setup_routes.py`, `templates/**`, `static/**` | T-WEB | edit / stubs; `require_auth`, `health_payload`, verdict/send/mode wrappers (foundation) |
| `sentrix/link/client.py` (`build_service`, `preflight`), `tunnel.py`, `commands.py`, `policy.py` (the tunnel path table: the only place tunnelled roles, consents, step-up and rate limits are enforced), `profile.py`, `sentrix/storage/link_store.py`, `sentrix/web/link_routes.py` | T-LINK | to create / working `profile.py` / minimal CRUD / empty |
| `sentrix_portal/routes/{link_hub,device_proxy,sos,desk,contacts}.py`, `sentrix_portal/services/{hub,sos}.py` (who gets which SOS message, tokens, Telegram buttons), their templates | T-LINK | empty routers / stubs |
| `sentrix_portal/routes/{auth,admin,onboarding,household,settings,privacy,home,consent}.py`, `sentrix_portal/security.py`, `sentrix_portal/hardening.py`, `sentrix_portal/services/{accounts,retention}.py`, their templates, `tools/provision_device.py` | T-ACCT | empty routers / primitives / default headers / stubs |
| `training/**` (scene model) | T-MODEL | |
| `tests/test_<track>_*.py`, `INTEGRATION_NOTES_<track>.md` | each track | |

Templates: Portal templates live in `sentrix_portal/templates/<area>/...`
(`base.html` is shared, foundation). Static files: `sentrix_portal/static/<area>/`.

### 2.4 Additive seams in shared files

A track may **append** exactly these to the shared file named and must not
change anything else there (each is new code with no caller in the
foundation, so two tracks never edit the same lines):

| Seam | File | Track | Contract |
|---|---|---|---|
| `ClipConfirmation` | `sentrix/fusion/evidence.py` | T-CLIP | within-clip confirmation (a class confirmed on `confirm_keyframes` keyframes of one track / one fire region) producing the same per-class evidence dicts as `EvidenceAccumulator`, so `decide` and the TCI are unchanged |
| `EventLog.write_clip(...)` | `sentrix/pipeline/events.py` | T-CLIP | one row per clip, `"schema": 4, "kind": "clip"`; tick rows stay schema 3; readers skip unknown kinds |
| `EvidenceVault.save_clip(...)` | `sentrix/evidence/vault.py` | T-CLIP | a pre-roll clip bundle in the same manifest chain; emits the `evidence` listener like `save` |
| `prepare_eval_pipeline` hook | `sentrix/evaluation/harness.py` | T-CLIP | already wired: `build_eval_pipeline(mode="clip")` calls `sentrix.pipeline.clip.prepare_eval_pipeline(pipeline)`, which must keep every no-op of the harness |

Seams already public in `engine.py` (no edit needed): `Pipeline.observe(frame,
now) -> FeatureVector`, `Pipeline.decide(frame, features, now, label=None, *,
started=None) -> dict`, `Pipeline.run_live(stop, strategy=None)`,
`InferenceStrategy` (`name`, `on_frame(frame, captured_at)` on the capture
thread, O(1); `run(stop)` on the inference thread; `wake()`),
`Pipeline.note_inference(captured_at)`, `Pipeline.request_close_incident(
reason)`. The clip strategy builds its `LevelPolicy` with
`settings.clip.min_escalate_observations` (1). `SENTRIX_PIPELINE_MODE=clip`
makes `run_live` use `sentrix.pipeline.clip.build_strategy(pipeline)`; capture,
preview, the identity worker and the HUD stay in `run_live`.

**T-MODEL's output** is a scene checkpoint `SceneEngine` already loads: a
`torch.save` dict with `architecture` (`"timm:<name>"`, e.g.
`timm:efficientnet_b0`, `timm:mobilenetv3_large_100`, or `resnet18`),
`classes` (the five scene classes), `model_state_dict`, `storage` (`"full"`,
or `"delta"` with `base_fingerprint`), `image_size`, `normalization`
`{mean, std}`, `temperature`, `thresholds` (per class, from out-of-fold
predictions), next to `<name>_metadata.json` in the `models/v4_custom/`
format, and an optional `<name>.onnx` with the same metadata. Any change to
`sentrix/perception/scene.py` (a loader for another format, an ONNX runtime)
is T-CLIP's; T-MODEL writes it into its integration notes.

### 2.5 Ownership map (summary)

| Owner | Owns |
|---|---|
| Foundation / integration | every §2.2 file; §2.4 seams' hosts; the device prune + vault tombstones (retention caps); demo tools, Docker/Caddy, Alembic, README, end-to-end tests, `CLAUDE.md` |
| T-HW | `sentrix/io/**` (picamera, gpio, actuators rewrite, sensors, node, network), `web/node_routes.py`, `run.sh`, `requirements-pi.txt`, `deploy/pi5/**`, doctor hardware checks |
| T-CLIP | `pipeline/clip.py`, `pipeline/fastlane.py`, `runtime/**`, `perception/{identity,scene}.py`, `tools/{export_models,bench_clip}.py`, `replay_custom_videos.py --mode clip`, the §2.4 seams |
| T-SOS | `sos/{service,machine,policy,templates,sweeper,pins}.py`, `storage/requests.py`, `alerting/dispatch.py`, `web/sos_routes.py` |
| T-CH | `alerting/channels/{router,telegram,email,modem,twilio}.py`, `alerting/notifier.py`, `storage/notifications.py`, `web/channel_routes.py`, Portal `routes/hooks.py`, `services/notify.py` |
| T-WEB | `web/{routes,streaming,security,hardening,setup_routes}.py`, `web/templates/**`, `web/static/**` |
| T-LINK | `link/{client,tunnel,commands,policy,profile}.py`, `storage/link_store.py`, `web/link_routes.py`, Portal `routes/{link_hub,device_proxy,sos,desk,contacts}.py`, `services/{hub,sos}.py` |
| T-ACCT | Portal `routes/{auth,admin,onboarding,household,settings,privacy,home,consent}.py`, `security.py`, `hardening.py`, `services/{accounts,retention}.py`, `tools/provision_device.py` |
| T-MODEL | `training/**`, `models/v5_slim/**` |

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
| `gpio.led_green_pin` / `led_amber_pin` / `led_red_pin` | — | 5 / 12 / 13 | Amber kept off GPIO6 (UPS HAT); docs/8_RASPBERRY_PI5_INTEGRATION.md §6 still says 6 — T-HW updates it |
| `gpio.buzzer_pin` | — | 26 | |
| `gpio.pir_pin` / `door_pin` / `door_pull_up` | — | 23 / 24 / True | |
| `gpio.mains_sense_pin` / `mains_present_low` | — | 16 / True | PC817 output low while mains present |
| `gpio.input_bounce_seconds` | — | 0.05 | |
| `node.enabled` | `SENTRIX_NODE_ENABLED` | False | Telemetry service |
| `node.profile` | `SENTRIX_PROFILE` | laptop | laptop / pi5-clip / pi5-hailo / pi5-node / hub |
| `node.interval_seconds` / `temp_warn_c` / `temp_critical_c` | — | 5 / 80 / 85 | |
| `clip.mode` | `SENTRIX_PIPELINE_MODE` | tick | `clip` → `Pipeline.run_live` runs `sentrix.pipeline.clip.build_strategy(pipeline)` on the inference thread (§2.4) |
| `clip.min_escalate_observations` | — | 1 | One clip is one observation for L4/L5 (the tick loop needs 2 ticks) |
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
| `secret_key` | `PORTAL_SECRET_KEY` | test mode: generated **once** into `<data_dir>/secret_key` (0600) and reused | HMAC for session ids, device bootstrap/token digests, claim and activation codes, action tokens, audit chain, IP hashes; **required** from the env when test mode is off. Back the file up: without it no device, login or code verifies |
| `data_key` | `PORTAL_DATA_KEY` | test mode: `<data_dir>/data_key` (0600), separate from the secret | Column encryption (`seal`/`unseal`); required from the env when test mode is off |
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
| `data_dir` | `PORTAL_DATA_DIR` | `<repo>/var/portal` (read when the settings are built) | Key files, exports |
| `workers` | `PORTAL_WORKERS` | False | Start `BACKGROUND_MODULES` in the lifespan (§4); off in tests |

Tests use `PortalSettings.for_tests(tmp_path)`: its own database and data dir,
throwaway keys, no workers (the only place a temporary key is allowed).

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
    settings; state; get_pipeline: Callable[[], Pipeline]; registry: ServiceRegistry
    extras: dict      # live app: {"app": <LC FastAPI app>, "loop": <running event loop>}

def build_service(ctx: ServiceContext) -> Service | None   # each track's factory; None = flag off
def preflight(settings) -> list[{"status": "ok"|"warn"|"fail", "name", "detail"}]  # optional
```

Threading rules: `start`/`stop` run on the event loop thread inside the
lifespan (spawn a thread or `asyncio.create_task`, return at once). A service
thread schedules coroutines with `asyncio.run_coroutine_threadsafe(coro,
ctx.extras["loop"])` and never blocks the loop. `ctx.extras["app"]` is the LC
app (the tunnel executor's `ASGITransport` target). Listener callbacks
(§6.3) run on the writer's thread: enqueue only. `preflight` (optional) is
cheap and local (env vars, files; no network, no devices):
`tools/doctor.py` prints it for every service (`services.preflight_report`),
so no track edits the doctor for channel, link or SOS checks.

Start order (`BUILTIN_FACTORIES`), stop in reverse:

| # | Name | Factory | Flag |
|---|---|---|---|
| 1 | `node` | `sentrix.io.node:build_service` | `SENTRIX_NODE_ENABLED` |
| 2 | `gpio` | `sentrix.io.gpio:build_service` | `SENTRIX_GPIO_ENABLED` |
| 3 | `channels` | `sentrix.alerting.channels.router:build_service` | `SENTRIX_CHANNELS_ENABLED` (modem worker inside, `SENTRIX_MODEM_ENABLED`) |
| 4 | `sos` | `sentrix.sos.service:build_service` | `SENTRIX_SOS_ENABLED` |
| 5 | `link` | `sentrix.link.client:build_service` | `SENTRIX_LINK_ENABLED` |

A missing module is skipped; a failing import/factory/start is logged, marked
`failed` in health and never stops the app. Later services find earlier ones
with `ctx.registry.get("channels")`; routes and command handlers find the SOS
service with `sentrix.sos.current_service()` (the null one when off).

Lifespan order *(foundation, `sentrix/app.py`)*. Start: `init_db` → build the
pipeline → `mode.load()` (persisted HOME/ARMED) → prune → services (`pipeline.sos
= registry.get("sos")`; without a channel router the legacy notifier's
readiness goes to `state.channels`) → `close_interrupted` (after the services,
so the link client hears the closures) → live loop. Stop: the live loop →
`shutdown_pipeline()` (its FIFO worker drains every queued side effect, a
pending `_sos_trigger` included, and saves the open incident while the
services still listen) → `registry.clear()` (reverse order) →
`listeners.clear()`.
`services.health_fields()` → `{"services", "send_guard", "test_mode", "mode"}`
is merged into `/api/health`. `services.assert_measurement_safe(pipeline)`
raises `MeasurementUnsafe` unless `pipeline.sos` is a `NullSosService` and no
service is running (called by the eval harness, `tools/replay.py`,
`tools/bench_live.py`; T-CLIP adds it to `tools/bench_clip.py`).

### 3.7 Runtime mode *(foundation: `sentrix/mode.py`)*

`is_armed()`, `set_armed(armed, by, *, persist=True)`, `armed_override()`,
`load()`, `clear_override(*, forget=False)`, `snapshot()`, `MODE_PATH`.
`SENTRIX_ARMED` is the value at first start; the TCI engine reads the
override every tick. `set_armed` writes `var/mode.json` (atomic replace),
publishes `state.mode`, emits the `mode` listener event (→ `mode.changed`) and
writes a `mode_set` audit row to the live pipeline's event log. The app
lifespan calls `load()` at start-up, so a power cut cannot silently disarm the
house; replays, tools and tests never call it and keep the configured value.
Callers check the PIN first: everything goes through `sos.set_mode` (arming
needs no PIN, disarming the SOS PIN; the duress PIN disarms visibly and opens a
silent request). Without the SOS service only the device admin may disarm.

### 3.8 Retention — `settings.retention` (`RetentionConfig`)

| Field | Env | Default | Owner of the prune |
|---|---|---|---|
| `reviewed_low_days` | `RETENTION_DAYS` | 30 | device (`database.prune`, today's rule) |
| `sos_snapshot_days` | — | 30 | Portal (T-ACCT `services/retention.py`) and device |
| `unreviewed_days` | — | 180 | device + Portal mirrors |
| `true_days` | — | 365 | device + Portal mirrors |
| `audit_days` | — | 365 | device JSONL/`remote_audit` + Portal `audit_log` |
| `declined_contact_days` | — | 7 | Portal |

The caps override every exemption (the device's "never prune L4/L5 or
unreviewed" holds only inside them); pruned evidence leaves a signed
tombstone so `verify_chain` reports a retention gap, not tampering. Device
prune and tombstones: integration (they touch `database.py` and `vault.py`).
Portal prune: T-ACCT's background module.

## 4. The Portal application *(foundation: `sentrix_portal/`)*

### 4.1 App, lifespan, middleware

* `create_app(settings=None)` builds the database (tables created, missing
  nullable columns added: additive only, idempotent), `app.state.settings`,
  `app.state.db` (`Database`: `.session()` context manager, `.SessionLocal`),
  `app.state.templates` (Jinja globals `test_mode`, `desk_number`; context
  processor `csp_nonce`), `/static`, every router in `routes.ROUTER_MODULES`,
  every module in `app.MIDDLEWARE_MODULES` and `GET /healthz` →
  `{"status": "ok", "db": "ok", "test_mode": bool, "version"}`.
* **Lifespan**: with `settings.workers` (`PORTAL_WORKERS=1`; off in tests),
  each module in `app.BACKGROUND_MODULES` that exports `start(app) -> stop` is
  started on the event loop (it may `asyncio.create_task`; it reads
  `app.state.db` / `app.state.settings`) and stopped in reverse order (`stop`
  may return an awaitable). Missing modules are skipped; a start failure is
  logged and skipped. Order: `services.hub` (T-LINK heartbeat watcher),
  `services.sos` (T-LINK cloud timers), `services.notify` (T-CH worker and
  Telegram polling), `services.retention` (T-ACCT). `app.state.background`
  lists what runs.
* **Middleware**: each module in `MIDDLEWARE_MODULES` exports `install(app)`
  and must set `request.state.csp_nonce`. A failure is fatal (the Portal faces
  the Internet). The default `sentrix_portal/hardening.py` (T-ACCT owns it)
  adds CSP with the nonce, `X-Frame-Options: DENY`, `X-Content-Type-Options`
  and `Referrer-Policy` — and **no CSP and no frame header on `/d/{id}/…`
  responses**: the console's own CSP (its own nonce) passes through untouched,
  because two policies on one response combine and would block the console's
  scripts. It is pure ASGI so the MJPEG relay streams unbuffered.
* `python -m sentrix_portal` serves it on `PORTAL_HOST:PORTAL_PORT` (8100).
* `db.get_db` is the per-request session dependency (commit on success). A
  route that needs its write visible before responding calls `db.commit()`.
* Templates extend `base.html`: blocks `title`, `extra_head`, `nav`, `content`,
  `scripts`; context `principal` (a `security.Principal` or None) and
  `active_page` (a nav key). `base.html` renders the test-mode banner, the CSRF
  meta tag and a role-based nav; scripts must be external files carrying
  `nonce="{{ csp_nonce }}"`.
* Schema changes after the first deployment go through Alembic (integration).

### 4.2 Cross-track Python APIs on the Portal (signatures frozen; stubs in the foundation)

`actor` is always `sentrix.actor.Actor.to_dict()`: `{type, id, name,
step_up_at}` with `type` in owner, member, operator, contact, cloud, system.

* T-LINK `sentrix_portal/services/hub.py`:
  * `is_online(device_id) -> bool` — plain function (in-memory lookup);
  * `async send_command(device_id, name, args, actor, *, timeout=10,
    queue_if_offline=True) -> {ok, code, status, message, result, queued,
    command_id}` — online: sent with `expires_at = now + timeout`, the
    device's `command.result` awaited (`timeout` code otherwise); offline with
    `queue_if_offline`: a QUEUED `device_commands` row, `queued: True`, code
    `device_offline`, sent after the next `welcome`;
  * `async push_profile(db, device_id) -> int` — pushes
    `accounts.build_profile` at `households.profile_version` (0 when offline:
    it goes out after the next `welcome`);
  * `async issue_token(db, device_id)` — sends `token.issue` now, or right
    after the device's next bootstrap `hello`; stores only the digest;
  * `async unpair(db, device_id, actor) -> dict` — `unpair` command, token
    revoked, socket closed 4003, `device.unpaired` audit;
  * `start(app)` — heartbeat watcher (device offline after 45 s).
* T-LINK `sentrix_portal/services/sos.py`:
  `async respond(db, request_id, action, *, actor, channel, authority=None,
  pin=None, detail=None) -> SosResult-shaped dict` (actions `approve`,
  `decline`, `checking`, `safe`, `help_now`, `acknowledge`, `resolve`) — the
  one entry point for Portal pages, `/a/` action links, `/c/` contact pages and
  Telegram callbacks; online it sends the device command and returns its
  result, offline the cloud applies it; `open_requests(db, household_id) ->
  list[dict]` (owner view); `start(app)` — cloud timers. It also owns *which
  event goes to which recipient at which step* (§7.11).
* T-ACCT `sentrix_portal/services/accounts.py`:
  `build_profile(db, household_id, device_id) -> dict` (§5.11),
  `confirmed_contacts(db, household_id) -> list[EmergencyContact]`
  *(foundation)*, `bump_profile_version(db, household_id) -> int`
  *(foundation; call it in the same transaction as the change, then
  `hub.push_profile` after the commit; the hub never bumps)*,
  `mint_telegram_link(db, *, subject_type, subject_id, household_id) -> str`.
* T-CH `sentrix_portal/services/notify.py`: `router(db) -> ChannelRouter`,
  `enqueue(db, recipient, message) -> str | None`, `start(app)` (worker,
  Telegram polling).
* T-ACCT `sentrix_portal/services/retention.py`: `prune(db, now=None) ->
  dict`, `start(app)`.

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
"bootstrap_secret", "setup_code_digest", "provisioned_at"}`. `device_id` is a
**UUID4** (the Portal's ingest validates ids with a UUID regex; the tunnel
base pattern accepts it). `setup_code_digest` is the SHA-256 hex of the
one-time setup code printed on the label; `/setup/wifi` (only while
`SENTRIX_SETUP_MODE=1`) checks the typed code against it.
`<identity_dir>/token.json`: `{"token", "issued_at"}` (written on
`token.issue`). `<identity_dir>/profile.key`: the profile cache key (§5.11).
The Portal stores only `token_digest()` values of the bootstrap secret, the
token and the claim code.
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
| `hello` | D→C | first message | `device_id`, `serial`, `firmware` (git short hash), `app_version`, `protocol` (1), `session_id`, `profile_version` (active cache, 0 if none), `outbox` {`head_seq`, `tail_seq`, `depth`}, `capabilities` [tunnel, video, state, sms, siren, gpio, clip], `test_mode`, `armed`, `health` (`sentrix.web.routes.health_payload()`: the redacted `/api/health` field set, incl. `audio`), `time_ms` |
| `welcome` | C→D | reply to hello | `device_id`, `claimed`, `server_time_ms`, `profile_version` (latest; the device expects a push when newer), `resend_from_seq` (cloud's highest contiguous stored seq), `subscriptions` {`state`: bool, `video`: {max_fps} or null}, `heartbeat_seconds` |
| `heartbeat` | D→C | every `heartbeat_seconds` (10 s) | `camera_online`, `video_fps`, `ai_hz`, `frame_age_ms`, `audio`, `armed`, `alarm` {level, incident_id, incident_type}, `open_request_id`, `node` {cpu_temp_c, throttled, under_voltage, mains, ext5v_v}, `modem` {present, registered, signal_dbm, sms_today}, `channels` {outbound_ready}, `outbox_depth`, `remote_viewers`, `uptime_s`, `profile_version`, `time_ms` |
| `event` | D→C | outbox (§5.5) | `seq`, `kind`, `priority`, `at` (ISO), `payload` |
| `ack` | C→D | ≤ 1 s after commit, batched | `seqs` [int] and/or `upto` (every seq ≤ upto stored) |
| `state` | D→C | 2 Hz while subscribed, else every 10 s | `n` (per-connection counter), `payload` (the `state.ws_payload()` object; paths in it are base-relative, §9.4). The Portal relay adds `remote_video: {fps, delay_ms}` before fanning it out; the console's Live page shows it when present |
| `state.subscribe` | C→D | first dashboard/live viewer, renewed < lease | `sub`, `hz` (≤ 2), `lease_s` (75) |
| `state.unsubscribe` | C→D | last viewer gone | `sub` |
| `video.subscribe` | C→D | first video viewer, renewed < lease | `sub`, `max_fps` (≤ 15), `lease_s`, `actor` (Actor dict) |
| `video.unsubscribe` | C→D | | `sub` |
| `viewers` | C→D | on change | `count` (remote viewers of video and dashboards) → `state.set_remote_viewers` |
| `profile.push` | C→D | on change and after welcome | `version`, `sha256` (of the canonical profile JSON), `profile` (§5.11) |
| `profile.ack` | D→C | reply | `version`, `ok`, `error` (`stale` when ≤ the active version: still ok) |
| `command` | C→D | user action | `name` (§5.6), `args`, `actor` (`sentrix.actor.Actor` dict: `{type, id, name, step_up_at}`, type owner / member / operator / contact / cloud / system), `issued_at`, `expires_at` (ms) |
| `command.result` | D→C | reply | `ref` (command id), `ok`, `code` ("ok" or §13 code), `status`, `message`, `result` |
| `tunnel.request` / `tunnel.response` / `tunnel.cancel` | C→D / D→C / C→D | §5.7 | |
| `token.issue` | C→D | claim, rotation | `token`, `issued_at` |
| `sos.sync` | C→D | after welcome, for requests the cloud advanced (or opened) while the device was offline | `request_id`, `cloud_state`, `cloud_version`, `authority_side`, `events` [cloud-origin event dicts, `requests.event_dict` shape]; the device calls `sos.sync(...)` (§7.6) |
| `error` | both | | `code`, `status`, `message`, `ref` |

Binary messages (frames, body chunks): §5.9.

### 5.5 Outbox events (D→C, at-least-once)

* Every producer calls **`sentrix.link.publish(kind, payload, *,
  dedupe_key=None)`** *(foundation)*, never `link_store.enqueue` directly. It
  is a no-op returning None unless `SENTRIX_LINK_ENABLED=1`, so the outbox
  never grows on a device without a link; it never raises. `seq` is the
  `link_outbox` autoincrement and never changes. Priority 0 = SOS kinds
  (`protocol.SOS_EVENT_KINDS`), 1 = the rest. SOS dedupe key:
  `protocol.sos_dedupe_key(request_id, origin, seq)` = `sos:<request>:<origin>:<seq>`.
* After `welcome` the client sends pending rows ordered by (priority, seq), at
  most `outbox_window` unacked; `ack` marks them; unacked rows are re-sent on
  every reconnect. Acked rows are pruned after 7 days.
* The Portal stores each `(device_id, seq)` in `device_events` (primary key) in
  the same transaction as its effect, then acks. A duplicate is re-acked and not
  re-applied. The Portal never assumes seq order across kinds: an SOS event may
  reference an incident not mirrored yet.
* SOS events of one request are applied in **per-origin request event seq**
  order; a gap is buffered until filled. The device publishes **device-origin**
  request events only: cloud events came from the Portal (`sos.sync`) and
  never go back up.

| Kind | Payload | Producer (listener → `publish`) |
|---|---|---|
| `incident.upsert` | the "incident" listener dict: id, session_id, ongoing, started_at, duration_seconds, peak_level, peak_tci, incident_type, reason, frames, evidence_count, snapshot_path, evidence_bundle, evidence_hash, person_present, authorized, actions | T-LINK on `incident` (save_incident, close_interrupted) |
| `dispatch.upsert` | `database.get_dispatch()` dict (status, request_id, chosen_authority, sent_at/by/to, error, …) | T-LINK on `dispatch` |
| `verdict.set` | incident_id, verdict, note, by, actor_type, channel, effect (`label_only`/`packages_cancelled`/`request_cancelled`/`resident_safe`), request_id, request_state, request_version, at | T-LINK on `verdict` |
| `sos.created`, `sos.transition`, `sos.event`, `sos.snapshot` | §7.9 | T-SOS (it knows the event seqs) |
| `evidence.meta` | bundle_id, incident_id, created_at, level, tci, sha256, camera_id, incident_type, detections [{label, conf}] (no image) | T-LINK on `evidence` (`vault.save`) |
| `snapshot.meta` | name, incident_id, created_at (no image) | T-LINK on `snapshot` (the pipeline's snapshot capture) |
| `gallery.audit` | event (gallery_enrolled/removed/rejected), person, file, via, by (no image) | T-LINK on `audit`, gallery events only |
| `mode.changed` | armed, by, at | T-LINK on `mode` (`mode.set_armed`) |
| `remote.audit` | rows of `remote_audit` (batched) | T-LINK |
| `notification.status` | id, request_id, channel, recipient_ref, recipient_masked, step, status, error, attempts, sent_at | T-LINK on the channels service's listener |
| `node.alert` | kind (over_temp, under_voltage, mains_lost, mains_restored, tamper), at, detail | T-HW |

Listener kinds and payloads: §6.3. The link client registers its listeners in
`start()` and removes them in `stop()`.

### 5.6 Commands (C→D allowlist, device-enforced)

The device does not trust the Portal. Every command carries the acting
`actor` (an `Actor` dict); the device checks its own rules, writes a
`remote_audit` row (`link_store.record_remote`) and answers `command.result`.
Anything not listed is refused with `unknown_command`. No command runs a shell
or code, disables the siren or the evidence vault. `link/commands.py` is a
**thin wrapper**: parse `args`, `Actor.from_dict(actor)`, call the method in
the last column, map the `SosResult` (or dict) to `command.result` (`ok`,
`code`, `status`, `message`, `result`).

| Command | Args | Device rule | Calls |
|---|---|---|---|
| `sos.respond` | `request_id`, `action` (approve, decline, checking, safe, help_now), `authority` (POLICE/FIRE/MEDICAL), `pin` (decline/safe), `channel` | owner/member; a **contact** may `approve` only when the request's frozen `policy_json` has safety defaults on and lists that contact as CONFIRMED; PIN checked on the device against the profile hashes (duress PIN → approve + duress); 5 PIN tries / 10 min (`sos/pins.py`); never logs the PIN | `sos.respond(...)` |
| `sos.update` | `request_id`, `event` (`protocol.SOS_UPDATE_EVENTS`: acknowledge, resolve, delivered, delivery_failed, contacts_notified, device_offline, device_online, seen, notification), `detail` | actor cloud/operator/contact; applied through `states.next_state` | `sos.cloud_event(...)` |
| `verdict.set` | `incident_id`, `verdict`, `note`, `pin` (FALSE) | owner/member | TRUE: `database.set_verdict(..., by=actor.ref, actor_type=actor.event_actor_type)`; FALSE: `sos.false_verdict(...)` |
| `mode.set` | `armed`, `pin` | arm: owner/member; disarm: SOS PIN (duress → disarm + silent request) | `sos.set_mode(...)` |
| `siren.test` | — | owner + step-up ≤ 300 s; ≤ 1/min process-wide (shared with `POST /api/node/siren-test`); 1 s | `actuators.siren_test(pipeline.siren, actor=...)` |
| `sms.send` | `to`, `text`, `request_id` | only numbers in `link.profile.sms_allowlist()` (owner, CONFIRMED contacts, Desk); ≤ 30/day; never while test mode refuses it (guard) | `registry.get("channels").enqueue(...)` (`modem_sms`) |
| `token.rotate` | — | owner + step-up; answered by `token.issue` | T-LINK |
| `unpair` | — | owner + step-up; wipe token (`token.json`) and profile (`link.profile.wipe()`) | T-LINK |
| `diag.ping` | — | anyone with tunnel access | T-LINK |
| `demo.close_incident` | — | test mode only (`test_mode_only` otherwise) | `sos.demo_close_incident(...)` → `Pipeline.request_close_incident("demo")` (applied on the inference thread after the next decision: incident closed, level dwell, evidence windows and the intrusion timer cleared, so the next scene opens a new incident and request) |
| `demo.trigger` | `incident_type` | test mode only; disclosed scripted L3 marked `simulated` / "SIMULATED DETECTION" in text and audit | `sos.demo_trigger(...)` |

`command.expires_at` passed → `timeout`. The Portal queues commands for an
offline device in `device_commands` (verdicts show "queued, will sync").

### 5.7 Tunnel

`tunnel.request` (C→D): `stream` (u32, unique per connection), `method`,
`path` (absolute, starts with `/`, no scheme/host, no `..`), `query` (without
`?`), `headers` [[name, value]] — the Portal forwards only `accept`,
`accept-language`, `content-type`, `content-length`, `x-csrf-token`,
`x-requested-with`, `if-none-match`, `user-agent` — `body_b64` (≤ 256 KiB) or
`body_len` + `chunked: true` followed by BODY chunks, `actor` (Actor dict;
type owner / member / operator — mapped onto the bridge headers of §5.8),
`base` (`/d/<device_id>`), `csrf` (the Portal session's CSRF token),
`timeout_ms`.

`tunnel.response` (D→C): `stream`, `status`, `headers` [[name, value]] (the
device drops hop-by-hop headers and every `set-cookie`), `body_b64` or
`body_len` + `chunked` + BODY chunks, `elapsed_ms`. `tunnel.cancel` (C→D):
`stream`.

Executor (T-LINK): replays the request into the LC app in-process with
`httpx.AsyncClient(transport=httpx.ASGITransport(app=ctx.extras["app"],
client=tunnel_auth.TUNNEL_CLIENT))`, path unchanged (no root_path), adding
`tunnel_auth.build_headers(...)`. The ASGI client `("tunnel", 0)` marks every
replayed request, so the console answers 401 JSON (never a redirect to its
`/login`) even if the identity failed to verify, and console rate limiters key
on the actor (`sentrix.web.auth.rate_key`), not on one shared address. Limits:
request ≤ 11 MiB (the Portal also caps while streaming), response ≤ 16 MiB,
`tunnel_timeout_seconds` (60 s for uploads/captures), ≤ 8 in flight (else 503
`device_busy`). The Portal adds `Cache-Control: no-store` to `/media/*` and
`/api/*`, `public, max-age=3600` to `/static/*`.

Never tunnelled (refused by both sides): `/login`, `/logout`, `/setup/*`,
`/docs`, `/redoc`, `/openapi.json`, `/video` and `/ws/*` (relayed instead).

Request handling on the Portal proxy (T-LINK): the route uses
`security.require_role("owner", "member", "operator", csrf="header")` — only
the `X-CSRF-Token` header counts and the body is never read, so form and
multipart posts are forwarded untouched (T-WEB makes every state-changing
console request a `fetch` with that header; no native form posts on pages the
tunnel serves). A logged-out HTML GET of `/d/<id>/<page>` is redirected to
`/login` (`require_role` handles it); APIs get 401 JSON. A `Location: /x`
answer from the console is rewritten to `/d/<id>/x`. Role, consent, step-up
and rate-limit rules for tunnelled requests are enforced **only** by
`sentrix/link/policy.py` on the device, before the in-process call; console
routes enforce only their business rules (§5.8).

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
* **One principal for every console route** *(foundation:
  `sentrix/web/auth.py`)*: frozen `LcPrincipal(kind: "lc"|"tunnel", role:
  device_admin|owner|member|operator, actor: "lc:device_admin"|"portal:<id>"|
  "desk:<id>", user_id, user_name, step_up_at, csrf)` with `.channel`
  (lc / portal / desk) and `.to_actor()`; `principal_of(request)`;
  `require_lc(*roles, step_up_seconds=None)` (dependency; sets
  `request.state.lc_principal`); `rate_key(request)`; `csrf_for_session(cookie)`
  (HMAC of the LC session); `LcError(code, message)` → `{"detail", "code"}`
  JSON with the §13 status (handler installed by `extensions.install`).
  `routes.require_auth` is the same check with the pre-M10 307 for LC
  requests. Use `require_lc()` with no roles on every console route; pass
  roles only for LC-only routes (`require_lc("device_admin")` on
  `/api/link/reconnect`, `/api/channels/test`). The device admin's password
  session counts as stepped up.
* If the hardening module fails to load, `extensions.install` installs a
  fail-closed middleware: every POST/PUT/PATCH/DELETE answers 503 while pages,
  video and reads keep working; tunnel identities never exist without it.

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
the device encrypted in `profile_cache` with AES-GCM under HKDF(`<identity_dir>/
profile.key`, `b"sentrix-profile-v1"`) — 32 random bytes made on first use
(0600) next to `identity.json`, so the cache survives restarts and a factory
reset of `var/` (protection against casual copying only). `sha256` is
`protocol.sha256_hex(profile)` = SHA-256 of `protocol.canonical_json(profile)`
(sorted keys, `(",", ":")`, UTF-8 without escapes, no `default=str`), computed
the same way on both sides.

Device accessor *(foundation: `sentrix/link/profile.py`, T-LINK owns it)*:
`current() -> dict | None`, `current_version()` (alias `version()`),
`effective()` (the Portal profile, else `standalone_profile()`),
`add_listener(fn)` / `remove_listener`, `store(profile, *, version, sha256) ->
profile.ack fields` (refuses forbidden fields and a wrong sha; a version not
newer than the active one is acked `stale`), `wipe()`, `desk_number(profile)`
(the profile's `desk.phone`, else `SENTRIX_DESK_NUMBER`), `sms_allowlist(
profile)`. `standalone_profile()` serves a device without a Portal: owner =
`ALERT_PHONE_NUMBER`, no contacts, Desk from `SENTRIX_DESK_NUMBER`, PIN hash
from `SENTRIX_SOS_PIN_HASH` (Argon2id) or, when unset, `pin_source:
"console_password"` (the LC's PIN prompt accepts the console password).

Templates: `profile.sos.templates` is **null in Tier 0**; both sides render
with the built-in English templates of `sentrix/sos/templates.py` (T-SOS;
sandboxed Jinja). Network-supplied templates (Tier 2) would be rendered with
`jinja2.sandbox.SandboxedEnvironment`.

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
recovery codes — the device refuses a profile that carries one
(`profile.validate`, `FORBIDDEN_KEYS`). PIN hashes are Argon2id
(`sentrix_portal.security.hash_pin`); the device verifies with `argon2-cffi`.
`"templates": null` in Tier 0 (the example above shows the Tier 2 shape).

---

## 6. Data model

### 6.1 Device SQLite (`var/sentrix.db`) *(foundation)*

Migration rule: `create_all` adds missing tables; `_ADDED_COLUMNS` adds missing
columns (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`); `_ADDED_INDEXES`
creates missing indexes `(name, table, columns, unique)`. Nothing is dropped
or rewritten.

New columns:

| Table | Columns |
|---|---|
| `incidents` | `verdict_by` (str 64: `lc:device_admin`, `portal:<user>`, `desk:<op>`), `verdict_at` (UTC), `request_id` (indexed) |
| `dispatch_packages` | `request_id` (indexed), `chosen_authority` (POLICE/FIRE/MEDICAL), `sent_at`, `sent_by`, `sent_to_json` (masked list), `error`, `expires_at`, `updated_at`; statuses PENDING, SENT, CANCELLED, **EXPIRED**, **FAILED** (`database.DISPATCH_STATUSES`) |
| `enrolled_faces` | `member_id`, `consent_id`, `enrolled_by` |
| `notifications` | `meta_json` (recipient name and role, e-mail HTML, Telegram buttons, urgency: `channels.base.message_meta`, rebuilt by `SqlOutbox.due`) |

New index: `ux_request_events_origin_seq` UNIQUE (`request_id`, `origin`,
`seq`) — one chain per (request, origin).

New tables (`sentrix/storage/models.py`): `assistance_requests`
(`AssistanceRequestRecord`), `request_events` (`RequestEventRecord`, hash
chain per (request, origin), `restricted` rows hidden from owners), `link_outbox`,
`profile_cache`, `notifications` (router outbox), `remote_audit`. Column lists
are in the model docstrings; the request fields mirror §7.9's payload.

CRUD *(foundation, then owned by the track)*: `storage/requests.py` —
`REQUEST_LOCK` (process-wide `RLock` held by every write to a request:
`create_request`, `try_transition`/`transition`, `import_event`,
`set_fields` and `database.set_verdict`; hold it across a read-decide-write
sequence), `create_request(..., origin="device"|"cloud", authority_side=None)`
(idempotent; `origin="cloud"` = a request the cloud opened while the device was
offline: `authority_side="cloud"`, no device event, the cloud's chain is
imported), `try_transition(request_id, event, *, expected_version=None, ...)
-> TransitionResult(request, code ok|not_found|conflict, message, event)`
(stale version or an event not allowed in the current state write nothing),
`transition(...)` (the same, returning the request or None), `append_event`
(this side's chain; seq and `prev_hash` per (request, origin); frozen),
`import_event(event_dict) -> (event, ok|duplicate|gap|invalid)` (the other
side's events, stored verbatim after checking the hash and the link),
`set_fields` (timer bookkeeping, no event, no version bump), `get_request`,
`open_request`, `list_requests`, `events(request_id, include_restricted,
origin)`, `last_seq(request_id, origin)`, `verify_chain` (every chain:
`{ok, entries, problems, origins}`); `storage/link_store.py` (`enqueue`, `pending`, `mark_sent`,
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
| `request_events` | (request_id, seq, origin) unique; device and cloud events; restricted; `at_text` = the sender's ISO `at` verbatim (the chain hashes it) |
| `sos_snapshots` | request_id (unique), JPEG bytes, sha256, expires_at (30 d) |
| `notifications` | the Portal router outbox (address_enc, dedupe_key unique, status, attempts, next_attempt_at, `meta` JSON = `message_meta`, …) |
| `action_tokens` | token_hash, purpose (`contact_page`, `contact_consent`, `action_link`, `telegram_link`, `email_verify`), household/request/subject, nonce (unique; the Telegram callback looks the token up by it), expires_at, used_at, revoked_at, opened_count |
| `telegram_links` | subject (user/contact), household_id, chat_id (private chats only), status |
| `push_subscriptions` | Tier 2 |
| `incidents_mirror`, `dispatch_mirror`, `evidence_meta_mirror`, `snapshot_mirror` | metadata for offline pages; never images |
| `audit_log` | append-only, `hash = HMAC(secret, prev_hash + canonical_json(row))` (`security.audit` stores `detail` as JSON values; `verify_audit_chain`) |

`Database.init()` creates missing tables and adds missing nullable columns to
existing ones (development databases from earlier builds); after the first
deployment, Alembic.

Retention caps (override every prune exemption): §3.8. Device prune and
tombstones: integration; Portal prune: T-ACCT (`services/retention.py`).

### 6.3 Change listeners *(foundation: `sentrix/listeners.py`)*

`listeners.add_listener(kind, fn)` / `remove_listener` / `emit` / `clear`
(`database.add_listener` and `database.LISTENER_KINDS` are the same registry).
Called after the write committed, with a plain dict, on the writer's thread
(usually the pipeline's FIFO worker or a web request thread): enqueue only. A
failing listener is logged and never breaks the write. A service registers its
listeners in `start()` and removes them in `stop()`. Nothing registers in
tests, replays or tools (`tests/conftest.py` clears the registry after every
test).

| Kind | Emitted by | Payload |
|---|---|---|
| `incident` | `database.save_incident`; `database.close_interrupted` (each row it ends, `ongoing: false`) | §5.5 `incident.upsert` |
| `dispatch` | `save_dispatch`, `update_dispatch_status` | `get_dispatch()` dict |
| `verdict` | `set_verdict` | incident_id, verdict, note, by, actor_type, channel, effect, request_id, request_state, request_version, at |
| `evidence` | `vault.save` | §5.5 `evidence.meta` |
| `snapshot` | the pipeline's snapshot capture | name, incident_id, created_at |
| `audit` | `EventLog.audit` | the JSONL audit row (event, session, ts, detail fields) |
| `mode` | `mode.set_armed` | armed, by, at, source |

Consumers: T-LINK publishes the mirror events (§5.5); **T-SOS registers
`verdict`**: for `request_cancelled` and `resident_safe` it re-reads the
request, publishes its new events (`sos.transition` / `sos.event`), refreshes
`state.sos`, cancels its timers and sends notified contacts "resolved by
resident".

## 7. SOS

### 7.1 When a request opens (D8)

A request opens only at a **confirmed L3+ commit** of `sos.alarm_classes`
(weapon, fire) — experimental classes are capped at L2 and never prompt — and,
in armed mode, for an `intrusion` that outlives the 30 s entry delay. No siren
at L3. One open request per device: later incidents (including the 300 s
rollover) attach to it.

Entry delay (decided): T-SOS holds an armed-intrusion trigger for
`intrusion_entry_delay_seconds`. It is cleared by (a) a disarm with the SOS
PIN on the LC or the Portal (`sos.set_mode`), or (b) an **authorised face**:
at the end of the delay T-SOS reads the live per-track identity (`state.get()`
→ `authorized` true and `identity_degraded` false: everyone in view is
recognised); if so it calls `mode.set_armed(False, by="face:<name>")` — a
visible, audited disarm — and opens no request. The identity layer itself
never changes the mode. (Liveness is out of scope: a photograph of a resident
could clear the delay; say so in the report.)

### 7.2 The SOS service interface *(foundation: `sentrix/sos/__init__.py`, `engine.py`)*

```python
class SosService(Protocol):
    name: str
    # pipeline hook
    def wants(self, incident_type: str, level: int) -> bool   # inference thread; O(1), no I/O
    def trigger(self, trigger: SosTrigger) -> None             # FIFO worker; enqueue only; idempotent per incident
    def on_level(self, change: LevelChange) -> None            # inference thread; enqueue only
    # people and the cloud (web threads, link worker); never raise
    def respond(self, request_id, action, *, actor, channel, authority=None, pin=None) -> SosResult
    def cloud_event(self, request_id, event, *, actor, detail=None) -> SosResult
    def sync(self, request_id, *, cloud_state, cloud_version, authority_side, events) -> SosResult
    def false_verdict(self, incident_id, *, pin, actor, channel, note=None) -> SosResult
    def set_mode(self, armed, *, pin, actor) -> SosResult
    def approve_package(self, package_id, authority, *, actor, channel) -> SosResult
    def demo_trigger(self, incident_type, *, actor) -> SosResult
    def demo_close_incident(self, *, actor) -> SosResult
    def current(self, view="owner") -> dict | None              # views: owner | desk | contact | full
    # service
    def status(self) -> dict                                   # the state.sos block (owner view)
    def start(self) -> None; def stop(self) -> None; def health(self) -> dict

class SosResult(TypedDict): ok: bool; code: str; status: int; message: str; request: dict | None
sos_result(code="ok", message="", request=None) -> SosResult   # status from protocol.ERROR_CODES
current_service() -> the registered "sos" service, else a shared NullSosService
```

* `actor` is a `sentrix.actor.Actor`; `channel` a `states.CHANNELS` value
  (`LcPrincipal.channel` on the LC). `code` is "ok" or a §13 code; `request`
  is the request after the call **in the caller's view** (owner view for
  owners and members: duress hidden).
* **Thin wrappers**: `/api/sos/*` (T-SOS), the verdict, dispatch-send and
  `/api/mode` routes (foundation in `routes.py`, T-WEB keeps them) and
  `link/commands.py` (T-LINK) parse input, build the actor, call one method
  and map the result. They never touch the request store.
* `NullSosService` (SOS off, the default; measurement pipelines): `wants` is
  False; `respond`, `cloud_event`, `sync`, `demo_*` → `unsupported`;
  `current` → None; and the console keeps its pre-portal behaviour:
  `false_verdict` → `database.set_verdict` (PENDING packages cancelled),
  `set_mode` → arm always, disarm by the device admin only,
  `approve_package` → the guarded legacy `dispatch_service.send`.
* `ActionPlan.sos = level >= settings.sos.min_level`. In `_act`, when
  `plan.sos`, `"sos"` is not yet in `incident.actions` and `sos.wants(type,
  level)`, the pipeline adds `"sos"` and queues `_sos_trigger` **after** the
  snapshot and evidence submits (same FIFO), or runs it inline when the queue
  is full (never dropped). An incident that starts as an unwanted type and
  later becomes a weapon still triggers.
* **Once `"sos"` is in `incident.actions` the SOS service owns every message
  and the package of that incident**: the legacy L4 SMS, the L4 dispatch
  package and the L5 voice call are skipped (D8: SMS only offline or unseen
  after 10 min; the L4 SMS folds into the `rise4` reminder, the L5 call into
  `rise5`; the one package per request opens at L3). The siren stays. Without
  the SOS service the legacy path is unchanged.
* `SosTrigger`: incident_id, level, incident_type, evidence_state, tci,
  reason, session_id, camera_id, armed, at_wall, at_mono, snapshot_path,
  evidence_bundle, evidence_hash, votes, simulated.
* `LevelChange` on every committed level change (both directions): level,
  previous_level, incident_id, incident_type, evidence_state, tci, at_wall,
  at_mono. The same call records `state.record_level_change` (the dashboard's
  Event Log history, last 50).
* The lifespan installs the real service only when `SENTRIX_SOS_ENABLED=1`.
  The eval harness sets the null one explicitly and calls
  `assert_measurement_safe`.

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
| DRAFTED | `expire` | EXPIRED | recovery only: a request whose prompt never went out before a crash, found past its window |
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
5 min"), T+30 EXPIRED. Message dedupe key `request|recipient|channel|step`
with the fixed step names of `sentrix.sos.states`: `t0`, `r120`, `r300`,
`r600`, `r1200` (`reminder_step`), `lapse`, `lapsed`, `rise4`, `rise5`,
`contacts_t0`, `desk_t0` (first page to the Desk number: modem SMS while
offline), `repage1`..`repageN` (`repage_step`), `resolved`, `duress`. The
legacy L4 owner SMS folds into `rise4`, the L5 call into `rise5`.

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

**Chains stay per origin.** Each side appends its own events (`origin`
device or cloud) numbered 1, 2, 3 … with its own `prev_hash`; the device
stores the cloud's events with `requests.import_event` (verbatim, hash and link
checked; a gap is buffered), the Portal stores the device's (`request_events`
unique on (request_id, seq, origin)). Neither side ever re-sends the other's
events.

**Requests the cloud opens.** When the heartbeat is lost during an open L2+
incident (D9 safety default), the cloud may have no request yet: it creates
one with its own UUID, `authority_side="cloud"`, flag `device_offline`, origin
cloud, and alerts CONFIRMED contacts (Telegram / e-mail only — the Portal
cannot send SMS; T-ACCT warns when a contact has neither). On `sos.sync` for an
unknown id the device runs `create_request(..., origin="cloud")`, imports the
cloud's chain, attaches the incident, and links any request it opened itself
for the same incident with a `detail.merged_into` event.

### 7.7 Verdict semantics by request state *(foundation: `database.set_verdict`)*

`set_verdict(incident_id, verdict, note, *, by, channel, actor_type=None)`
records `verdict_by` (= `Actor.ref`) and `verdict_at` under `REQUEST_LOCK`;
`actor_type` is `Actor.event_actor_type` (device_admin, owner, member, desk,
contact, cloud; guessed from `by` when omitted, e.g. `desk:` → desk). FALSE
then depends on the incident's request:

| Request | Effect |
|---|---|
| none | cancel the incident's PENDING packages (pre-portal rule) |
| DRAFTED / PENDING | request CANCELLED (event `cancel`, reason `verdict_false`); its PENDING package CANCELLED (by `request_id`) |
| help path (not CLOSED) | flag `resident_safe` (event); package and help untouched |
| duress | nothing (the caller still shows success) |
| terminal | label only |

**A FALSE verdict goes only through `sos.false_verdict`** (the console's
verdict route, the `verdict.set` command): with an open request it needs the
SOS PIN, and it maps the **duress PIN** to approve + duress *before*
`set_verdict` is called, so the label reads FALSE while help is on its way.
`set_verdict` itself never sees a PIN. The `verdict` listener carries
`request_id`, `request_state`, `request_version`; T-SOS uses it to publish the
change, cancel timers, refresh `state.sos` and tell notified contacts.
Last writer wins on the label; the Desk's resolve outcome is final for the
request. `dispatch_service.send` returns `SendOutcome(status: sent | failed |
conflict | not_found | invalid)`, truthy only when sent; a failed legacy send
(the guard's dry run by default) leaves the package PENDING with `error` set;
SENT, CANCELLED and EXPIRED packages (and a DECLINED request) answer 409. "Send now" on a package linked to a request goes
to `sos.approve_package`.

### 7.8 Duress

A duress PIN approves (event `approve`, `detail.duress = true`, `restricted =
true`) while every owner-facing surface shows "Marked as false alarm":
`states.owner_view(state, flags)` maps it to DECLINED and drops the flag. The
LC `state.sos` block, owner/member Portal pages and the owner's per-request
audit view never show restricted rows or the flag. Desk and contacts get the
DURESS banner: "Do not go to the home. Call 112."

The duress PIN works wherever the SOS PIN is asked: decline / "I'm safe"
(→ approve + duress), a FALSE verdict (same) and **disarm** (`sos.set_mode`:
the house disarms visibly — the person forcing the resident sees it work —
and a silent request flagged `duress` opens for the open incident, or for a
synthetic "duress" incident when none is open).

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

### 7.11 Who writes and sends each SOS message

| Side | Owner | Does |
|---|---|---|
| Device | T-SOS (`sos/service.py`) | the LC prompt (`state.sos`), offline modem SMS to owner / contacts / Desk (`desk_t0`, `contacts_t0`), standalone Telegram/e-mail (no link configured), all through `registry.get("channels").enqueue` |
| Portal | T-LINK (`services/sos.py`) | which event goes to which recipient at which step, the `/a/` and `/c/` tokens, the Telegram buttons, rendering with `sentrix/sos/templates.py`, then `notify.enqueue` |
| Portal | T-CH (`services/notify.py`, `routes/hooks.py`) | transports, outbox, worker, inbound Telegram (`/start <code>` linking with T-ACCT's token, callbacks → `services.sos.respond`) |

Delivery feedback: the channels service's listeners (§8.2) hear every status
change. **SUPPRESSED (a dry run) never counts as delivered.** While the device
is online, the cloud sends `sos.update delivered` once the request is in the
Desk queue (the Desk counts as a target even in a dry run); the device emits
`delivered` itself only for a SENT modem SMS while offline. T-SOS derives
`delivered` / `delivery_failed` / `undelivered`; T-LINK publishes
`notification.status`.

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
* `Button(label, action, url, callback_data)`. Telegram allows 64 bytes of
  `callback_data`: `channels.base.callback_data(nonce, action)` =
  `"<nonce>:<letter>"` with a 16-character nonce (`new_callback_nonce()`,
  `token_urlsafe(12)`, stored unique in `action_tokens.nonce`) and
  `CALLBACK_ACTIONS` `a` approve, `c` checking, `k` acknowledge;
  `parse_callback_data`. "False alarm" is a URL button to the Portal PIN page,
  never a callback.
* `SendResult(ok, provider_id, error, retryable, suppressed)`.
* `Transport` protocol: `name`, `send(recipient, message) -> SendResult` (never
  raises). `FakeTransport` records; tests use only fakes.
* `Channel(name, transport, policy_factory)`: guard → banner → transport. A
  refusal is `SUPPRESSED` (final) with the reason and the rendered text kept.
* `Outbox` protocol (`add` → False on duplicate key, `due(now, limit)`,
  `update(item)`): `MemoryOutbox` (tests), `storage.notifications.SqlOutbox`
  (device), the Portal's `notifications` table (T-CH). Both persistent
  outboxes round-trip `message_meta(recipient, message)` (recipient name and
  role, HTML, buttons, urgency) and rebuild with `rebuild(...)`.
* `ChannelRouter(channels, outbox, max_attempts=3, backoff=(30, 90),
  failed_retry_seconds=120)`: `register`, `enqueue` (id or None), `pump(now)`,
  `ready(channel)`, `add_listener(fn)` / `remove_listener` (`fn(OutboxItem)`
  after every status change `pump` makes), `status()`. Statuses QUEUED,
  SENDING, SENT, DELIVERED, FAILED, SUPPRESSED. Per-channel cooldowns (SMS
  300 s, call 600 s) stay for the legacy path only.
* **`ChannelsService`** (the `channels` service on the device, and the Portal's
  `notify`): `enqueue(recipient, message) -> str | None`; `ready(channel) ->
  bool` (registered and both send switches on); `status() -> dict` (the
  `state.channels` block); `add_listener(fn)` / `remove_listener`; plus
  `start`/`stop`/`health`. The SOS engine and the link's `sms.send` reach every
  channel only through it.

### 8.3 Who sends what

| Side | Channels |
|---|---|
| Device | `modem_sms` (offline prompt, SOS to contacts/Desk, `sms.send`), `modem_call` (optional L5), legacy `twilio_*` (only when enabled), LC prompt, siren; `telegram`/`email` **only** in standalone mode (no link configured) |
| Portal | `telegram`, `email`, Desk console, contact pages; `twilio_sms` as an optional backup when the device is gone |

Standalone LC (no Portal, no contacts): `ALERT_PHONE_NUMBER` (or the profile
owner phone) stays the dispatch target (`link.profile.standalone_profile`).
Every channel's readiness goes to `state.channels`, `/api/health`, the Node
page and `doctor` (through `preflight`). `outbound_ready` counts the legacy
Twilio notifier (`legacy_twilio`; the lifespan reports it while no router
runs); the red LC banner "no outbound channel: SMS and calls will not be
sent" shows only when `state.channels.warn` (SOS enabled and nothing can
leave).

### 8.4 Inbound and links

* Telegram: private chats only; webhook `secret_token` checked
  (`X-Telegram-Bot-Api-Secret-Token`); `callback_data` = `"<nonce>:<letter>"`
  (≤ 64 bytes; the nonce finds the `action_tokens` row, which binds request and
  recipient); callbacks accepted only from the linked chat id; development uses
  `getUpdates` long polling. "False alarm" opens a Portal PIN page (a PIN typed
  in chat stays in its history). T-ACCT mints the `/start` link code
  (`accounts.mint_telegram_link`); T-CH's bot consumes it.
* E-mail and SMS links open a confirmation page; the action happens only on a
  POST with CSRF. Tokens are single use, bound to request + recipient, expire
  with the request + 2 h, revoked on decline; every open is logged.
* Contact pages `/c/{token}` (T-LINK) are **SOS only**: per contact and
  request; medical notes only with consent; the snapshot only there (never a
  standalone link). Consent confirmation is `/consent/{token}` (T-ACCT,
  purpose `contact_consent`); e-mail verification `/account/verify-email/
  {token}` (T-ACCT).
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
  `SENTRIX_SECURE_COOKIES=1`; `auth.principal_of` also accepts the legacy
  `session_token` until the rename lands); `/video` and `/ws/threat` through
  `auth.principal_of` like every route; websocket Origin check; logout is a
  POST.
* API errors return JSON (401 for API/fetch, not a 307 to the login HTML):
  move the API routes from `require_auth` to `auth.require_lc()`.
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
| POST | `/api/mode` | foundation (T-WEB keeps it) | JSON `{armed, pin}` → `sos.set_mode`; `{mode}` or `{detail, code}` |
| POST | `/api/incidents/{id}/verdict` | foundation (T-WEB keeps it) | form `verdict`, `note`, `pin`; FALSE → `sos.false_verdict` (403 `pin_invalid`, 429 `pin_locked`, 404) |
| POST | `/api/dispatch/{id}/send/{authority}` | foundation (T-WEB keeps it) | POLICE / FIRE / MEDICAL → `sos.approve_package`; 409 `conflict` for SENT/CANCELLED/EXPIRED; `{status: sent|failed}` |
| GET | `/api/node` | T-HW | the `state.node` block; `gpio` = `{siren, leds: {green, amber, red}, buzzer, pir, door, mains}` |
| POST | `/api/node/siren-test` | T-HW | device admin or owner + step-up (policy.py); `actuators.siren_test` (1 s; ≤ 1/min, shared with the command) |
| GET | `/api/link` | T-LINK | the `state.link` block + `remote_viewers` |
| GET | `/api/link/remote-audit?limit` | T-LINK | `remote_audit` rows |
| POST | `/api/link/reconnect` | T-LINK | device admin |
| GET | `/api/channels` | T-CH | the `state.channels` block; `modem` = `{present, registered, signal_dbm, sms_today}` |
| POST | `/api/channels/test` | T-CH | device admin; guarded test message to the owner number |
| GET | `/api/level-history` | T-WEB | `state.level_history()` |
| GET | `/sos`, `/node` | T-WEB | pages |
| GET/POST | `/setup/wifi` | T-WEB (+ T-HW `io/network.py`) | only while `SENTRIX_SETUP_MODE=1`; the label's one-time code checked against `identity.json` `setup_code_digest` |
| GET | `/api/incidents?limit&offset` | T-WEB | limit ≤ 500 (database caps it) |

### 9.3 `/api/health` additions

Foundation: `camera_source` redacted; `armed` from the runtime mode;
`services` (per-service status + health), `send_guard`, `test_mode`, `mode`.
Tracks add their fields through their service's `health()` (they appear under
`services.<name>`), not by editing the route. The route body is
`routes.health_payload()` (also the link's `hello.health`). Integration added
`identity_model` (`buffalo_l` / `buffalo_s`) and `scene_runtime` (torch /
onnx / ncnn).

### 9.4 Live state blocks *(foundation: `sentrix/pipeline/state.py`)*

Single writer per block:

| Key | Writer | Idle value |
|---|---|---|
| `sos` | T-SOS (`state.update(sos=service.status())`) | `SOS_IDLE` (`active: false`, …); owner view only, never `duress` |
| `link` | T-LINK | `LINK_IDLE` (`state: "disabled"`, `connected: false`, …) |
| `node` | T-HW (`io.node.merge_node_block`, the single writer) | `NODE_IDLE` (all None, `profile: "laptop"`, `telemetry: false`; also `temp_state`, `throttled_raw`, `throttle_flags`, `under_voltage_occurred`, `power_w`, `environment`, `tamper`, `platform`) |
| `clip` | T-CLIP (`ClipStrategy`: `state.update(clip=runner.status())` after every clip) | `CLIP_IDLE` (`mode: "tick"`, `clips: 0`, `dropped: 0`, `compute_ms`, `budget_ms`, `runtime`, `threads`, `fast_lane`, `last`) |
| `channels` | T-CH (the lifespan while no router runs) | `CHANNELS_IDLE` (`outbound_ready`, `legacy_twilio`, `warn`, `modem: null`) |
| `mode` | `sentrix.mode.set_armed` | `MODE_IDLE` |
| `remote_viewers` | T-LINK via `state.set_remote_viewers(n)` | 0 (HUD shows "REMOTE VIEW: n" when > 0) |
| `test_mode` | foundation | `settings.test_mode` |
| `dispatch_package` (legacy) | T-SOS (owner view of the open request's package while SOS is on) | null |
| `cloud_online` / `cloud_available` (legacy aliases) | derived from `link.connected` / `link.enabled` | false |

`sos` fields: active, request_id, short_id, state, label, incident_id,
incident_type, level, urgency, evidence_state, created_at, expires_at,
remaining_s, seen, repeat, authority, recommended_authority, sos_text,
snapshot, actions (allowed buttons, names from `sentrix.sos.SOS_ACTIONS`:
`approve`, `call_112`, `checking`, `decline`, `safe`, `help_now`), last (the
most recent closed request: id, state, label, at), desk_number (shown instead
of 112 in test mode: `profile.desk_number()`); T-SOS adds `flags`
(owner-visible: `repeat`, `undelivered`, `resident_safe`, `unacknowledged`,
`simulated`, `camera_degraded`), `call_number` (112, or the Desk number in
test mode), `test_mode`, and `last.actions` (`["help_now"]` for EXPIRED /
DECLINED / CANCELLED). `state.level_history()` returns the last 50 committed
level changes. `SystemState.ws_payload()` (LC websocket and the link's state
messages) is redacted with `sentrix.utils.redact.redact_tree`.

**Paths in state blocks are base-relative** (`"media/snapshot/<name>"`, no
leading slash): one websocket payload reaches LC and tunnel viewers alike, so
the page prefixes `document.body.dataset.base`. Only request-scoped HTML and
JSON use `base_for(request)`.

### 9.5 URL and base helper

Paths on the device never change; the prefix comes from the tunnel identity.
`tunnel_auth.base_for(request)` → `/d/<id>` or "". T-WEB adds Jinja globals
`url(path)` (base + path) and `static_url(path)`, `active_page` in every page
context (nav highlighting by name, not by raw path), and `<body
data-base="{{ base }}">`; scripts read `document.body.dataset.base || ""` and
build every fetch, link, image and WebSocket URL from it. Python-built URLs
(`/media/snapshot/…`, `/media/face/…`, `vault.view_url`) go through the same
helper; T-WEB prefixes `vault.view_url` in routes and templates (`vault.py`
is left alone). A test renders every page through the tunnel and asserts every
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
| auth | `GET/POST /login`, `POST /logout`, `GET/POST /activate/{token}`, `/account`, `/account/password`, `/account/2fa`, `/account/sessions` (revoke), `GET/POST /account/verify-email/{token}` | T-ACCT |
| consent | `GET/POST /consent/{token}` (contact confirms or opts out) | T-ACCT |
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
| contacts | `GET/POST /c/{token}` (SOS contact pages only) | T-LINK |
| hooks | `POST /hooks/telegram` | T-CH |

Every page and API uses `security.require_role(...)` (roles, TOTP, CSRF,
step-up; `csrf="header"` for the device proxy); tenant isolation filters every
query on the principal's household. Unauthenticated routes (gate P2):
`/login`, `/activate/*`, `/account/verify-email/*`, `/consent/*`,
`/static/*`, `/healthz`, `/c/*`, `/a/*` (confirmation only; POST needs CSRF),
`/hooks/telegram` (secret-checked), `/device/v1/link` (device auth).

Primitives *(foundation: `sentrix_portal/security.py`)*: `hash_password`,
`verify_password`, `hash_pin`/`verify_pin` (Argon2id m=19 MiB, t=2, p=1),
`new_token`, `token_digest`, `new_code`, `code_digest`, `ip_digest`,
`client_ip`, `seal`/`unseal`, `create_session` (new id every login),
`load_session`, `revoke_session`, `revoke_user_sessions`, `principal_of`,
`set_session_cookie`, `issue_login_csrf`/`verify_login_csrf`,
`supplied_csrf(request, *, header_only=False)`, `verify_csrf`,
`require_role(*roles, csrf=True|"header"|False, step_up=False)` (a logged-out
HTML GET — `/d/<id>/<page>` included — is redirected to `/login`; APIs, media
and the relays get 401), `audit`, `verify_audit_chain`.

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
On the LC, raise `sentrix.web.auth.LcError(code, message)`; the handler
answers `{"detail": message, "code": code}` with the table's status.

Device JSONL audit rows (`EventLog.audit(event, **detail)`): existing
`gallery_enrolled`, `gallery_removed`, `gallery_rejected` (+ `by`); new
`sos_created`, `sos_transition`, `sos_notification`, `verdict_set`,
`dispatch_sent`, `mode_set`, `login`, `login_failed`, `remote_access`,
`siren_test` *(foundation: `actuators.siren_test`)*, `incident_closed`
*(foundation: `request_close_incident`)*, `profile_applied`, `link_connected`,
`link_lost`, `channel_suppressed`, `node_input` (input, value) and
`node_alert` (kind, detail) *(T-HW)*. `mode_set` *(foundation: `mode.set_armed`)*.
Every audit row also reaches the `audit` listeners (§6.3). Remote requests and
commands also go to `remote_audit`.

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
`security.admin_then_new_ip`; added by T-ACCT: `auth.activation_failed`,
`auth.totp_failed`, `auth.recovery_code_used`, `auth.recovery_codes_regenerated`,
`auth.step_up_failed`, `auth.email_verified`, `account.updated`,
`account.phone_code_sent`, `account.phone_verified`, `admin.staff_created`,
`admin.staff_<disable|enable|reset_2fa|resend>`, `admin.customer_deactivated`,
`admin.customer_reactivated`, `admin.letter_printed`, `admin.claim_code_reissued`,
`admin.device_retired`, `device.unpaired_by_owner`, `contact.updated`,
`contact.removed`, `member.added`, `member.removed`, `profile.pins_changed`,
`onboarding.completed`, `telegram.unlinked`, `privacy.erasure_cancelled`,
`privacy.erased`, `retention.audit_pruned`; by T-LINK: `device.outbox_reset`,
`device.node_alert`, `device.tamper`, `desk.note`.

---

## 14. Testing rules

* `tests/conftest.py` *(foundation)* runs before any `sentrix` import: test
  mode on, `SENTRIX_LIVE_ALERTS=0`, every channel switch `0`, no allowlist,
  provider keys empty (so `load_dotenv` in `sentrix/app.py` cannot bring a
  developer's live `.env` switches or Twilio keys into the session), every
  service flag off, no systemd `NOTIFY_SOCKET`, and the device `var/` tree
  (`SENTRIX_VAR_DIR`: database, events, evidence, snapshots, faces,
  calibration), the Portal data dir and database and the device identity dir
  in a session temp dir. Before integration a test without `temp_db` opened
  the developer's `var/sentrix.db`. An **always-on socket guard** allows loopback only
  (`socket.connect`, `getaddrinfo`, and the Windows Proactor's `ConnectEx`);
  blocked attempts are listed at the end of the run.
* Shared fixtures: `temp_db` (the device database on a temporary file, tables
  created), `portal_app` (`create_app(PortalSettings.for_tests(tmp_path))`),
  `fake_router` (`(router, transports)`: a `FakeTransport` per channel, memory
  outbox, `live_test_policy()`). After every test the listener and service
  registries, the runtime mode (`MODE_PATH` is per-test), the cached profile,
  the siren-test limiter, the siren arming source and the shared GPIO pin
  factory are reset. Tracks use these instead of their own
  copies.
* Never a real alert: transports are `FakeTransport`; the guard refuses by
  default.
* Databases are temporary files. Never `var/sentrix.db`.
* No webcam or microphone: sources are files or fakes; `enable_audio=False`.
* Measurement code keeps `NullSosService` and calls `assert_measurement_safe`.
* Never tune or select on `wa02`.
* Each track adds `tests/test_<track>_*.py`; the full suite stays green
  (`G:\Sentrix\.venv\Scripts\python.exe -m pytest`; add `-o addopts=""` to
  see the summary line).

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
5. **Docs**: README (new switches; the Twilio breaking change), docs/8_RASPBERRY_PI5_INTEGRATION.md
   (pin table, connectivity phase, modem in the BOM), `CLAUDE.md` M10 status
   (from the tracks' hand-off notes).
6. **Retention**: the device prune with the §3.8 caps and signed vault
   tombstones (`database.py`, `vault.py`).
7. **Gates**, measured once on the final build alongside M9: P1 parity
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

### 15.1 Contract amendments at integration (from the tracks' hand-off notes)

The eight tracks were merged onto `sentrix/portal` in the order above (T-HW,
T-CLIP, T-CH, T-SOS, T-WEB, T-ACCT, T-LINK, then T-MODEL); no file was edited
by two tracks. Their `INTEGRATION_NOTES_<track>.md` were applied and removed;
what they changed in this contract is recorded here (the code is the
reference), and what was not applied is in §16.

**Configuration (§3).**
* `settings.gpio` gained `siren_max_on_seconds` (90, `SENTRIX_SIREN_MAX_ON_SECONDS`),
  `siren_arm_pin` (None, `SENTRIX_SIREN_ARM_PIN`), `siren_armed` (True,
  `SENTRIX_SIREN_ARMED`; False refuses siren tests), `tamper_pin` (None,
  `SENTRIX_TAMPER_PIN`), `buzzer_enabled` (True); `settings.node` gained
  `bme280`, `dht22` (False), `picamera_main` "1280x720", `picamera_lores`
  "640x480". `sentrix.io.tunables` reads the environment at call time first,
  then the field.
* `settings.clip` gained the engine tunables `buffer_fps` 15, `analysis_width`
  640, `scene_keyframes` 2, `gate_motion_floor` 0.015, `gate_hold_seconds` 10,
  `idle_refresh_seconds` 10, `identity_interval_seconds` 2,
  `action_motion_floor` 0.10, `flame_fraction` 0.0005, `evidence_fps` 5,
  `post_roll_seconds` 8 (`clip.option(name)` reads them).
  `SENTRIX_SCENE_RUNTIME` overrides `SENTRIX_RUNTIME` for the scene classifier
  only (read by `perception/scene.py` at load time).
* `PortalSettings` gained `telegram_mode` (`SENTRIX_TELEGRAM_MODE`, polling |
  webhook) and `grievance_contact` (`PORTAL_GRIEVANCE_CONTACT`). The Portal
  still reads `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
  `SENTRIX_EMAIL_PROVIDER`, `BREVO_API_KEY` / `RESEND_API_KEY` and
  `SENTRIX_EMAIL_FROM` from its environment at use time; the webhook is
  `<PORTAL_BASE_URL>/hooks/telegram`, https only.
* `SENTRIX_VAR_DIR` moves the device's whole `var/` tree (read at import by
  `sentrix/paths.py`); the test suite uses it.
* Login back-off (T-ACCT, §10): per (key, address) 4 free failures, then 30 s
  doubling to 15 min; per address across keys 20 free; never a lockout. Keys:
  the lower-cased username, `activate:<username>` / `activate:link`,
  `stepup:<user id>`, `totp:<user id>`, `phone:<user id>`.

**Portal service seams (§4.2).**
* `hub.issue_token(db, id)` commits `db` before sending (the device reconnects
  within milliseconds). `hub.unpair` revokes the token and closes 4003; the
  caller then sets the device row (owner unpair: ASSIGNED; admin transfer:
  UNASSIGNED + new claim code; retire: RETIRED; erasure: UNASSIGNED).
* `notify.enqueue(db, ...)` commits with the caller's transaction (`ON
  CONFLICT DO NOTHING` on the dedupe key). The notification worker reports
  every status change of an `sos.*` message to
  `services.sos.notification_status` (online: `sos.update notification` to the
  device; offline: the cloud chain).
* `accounts.render_sos_preview` uses `sentrix.sos.templates.render_preview`
  (the device's wording: `preview()` with the profile's test mode).
* Telegram `/start <code>` consumes an `ActionToken(purpose="telegram_link",
  token_hash=security.token_digest(code))`; buttons carry
  `callback_data(nonce, approve|checking|acknowledge)` on an `action_link` /
  `contact_page` token; "False alarm (PIN)" is a URL button. Presses are
  accepted only from the linked private chat.

**Link protocol (§5).**
* `hello.outbox.epoch` (ISO `created_at` of the oldest outbox row): a changed
  epoch, or `tail_seq` below the Portal's watermark, is a factory-reset device;
  the Portal drops its dedupe rows and resets `last_acked_seq`
  (`device.outbox_reset`). The same seq with a different payload counts as new.
* The Portal answers every `heartbeat` with `ack{seqs: [], upto}`; the device
  treats any Portal message as liveness and drops a connection silent for
  `offline_after_seconds` (30 s).
* `welcome.subscriptions.video` is always `null`: the hub sends
  `video.subscribe` with the viewer's actor right after the welcome (the device
  checks consent per actor).
* The tunnel replays the path unchanged (no `root_path`); the base travels in
  `x-sentrix-base`. The console's own security headers (its CSP with the
  page's nonce, `X-Frame-Options`) pass through the tunnel unchanged.
* Relay override while a device is offline: `camera_online=false`,
  `camera_available=false`, `ai_hz=0`, `video_fps=0`, `link={..., connected:
  false, state: "offline"}`, `link_online=false`, `device_offline=true`.
* `device_commands.args` never stores `pin` or `text`; `sms.send` is never
  queued. Command to SOS service: `sos.respond` -> `respond`, `sos.update` ->
  `cloud_event`, `sos.sync` -> `sync`, `verdict.set` FALSE -> `false_verdict`,
  `mode.set` -> `set_mode`, `demo.*` -> `demo_trigger` / `demo_close_incident`.
* `link_outbox` is created with SQLite AUTOINCREMENT (a seq is never reused);
  on older databases `link_store.prune` keeps the oldest and newest rows.

**SOS (§7), as T-SOS implemented it.**
* Armed intrusion reaches the engine through `on_level` (the committed L3 of an
  `intrusion` incident), not `trigger`: `wants("intrusion", ...)` is False, so
  a weapon appearing during the 30 s entry delay can still trigger its own
  request, and a face or PIN disarm leaves nothing claimed. `trigger()` still
  accepts an intrusion trigger (held the same way).
* Silent requests (`flags.silent`): the duress PIN at a disarm with nothing
  open, or on a closed request, opens a request the household never sees
  (restricted `created` event; owner views answer 404; the LC block ignores
  it; no incident row; `incident_type: "duress"`). A duress decline also labels
  the incidents FALSE (the Events page matches a real decline). **M8 dataset
  builders must drop or relabel incidents of requests whose chain has a
  restricted `approve` with `detail.duress`.**
* Owner SMS: T0 when no remote prompt is possible (link down, or a standalone
  device without owner Telegram/e-mail); every reminder from T+10 min while
  the prompt is unseen (every reminder while offline); `rise4` / `rise5`
  always (the legacy L4 SMS and, as a `modem_call`, the L5 call fold in there).
  Contacts and the Desk get modem SMS from the device only while the link is
  down or none is configured; online, the Portal pages them.
* Repeat throttle: at most one re-prompt per `repeat_window_seconds` after a
  decline of the same incident type; a corroborated L4/L5 is never suppressed.
* `demo.close_incident` (test mode) also resets the request: prompting ->
  CANCELLED (`reason: test_reset`), DISPATCHED / ACKNOWLEDGED -> CLOSED
  (`no_action_needed`, no verdict). APPROVED and FAILED never close by
  themselves (only a delivery, the Desk or a contact moves them on).
* Store additions (`storage/requests.py`): keyword-only `at` on
  `create_request` / `try_transition` / `transition`; `create_request(...,
  snapshot_path, evidence_bundle, evidence_hash, urgency, peak_level,
  restricted)`; `force_transition`; `open_requests()`;
  `requests_for_incident()`; `request_dict` carries `repage_count`.
* `sos.snapshot` is published once per request when the frozen policy has the
  `sos_snapshot` consent and the link is enabled (at most 640 px, 256 KiB).

**Channels (§8.2).** The device `channels` service (a `DeviceChannels`) adds
`cancel(request_id, reason, steps=None)`, `send_now(recipient, message)`,
`recent(limit, request_id)`, `channels_for("sms" | "call")`, `owner_target()`,
and `router.notification_payload(item)` (the masked `notification.status`
payload). Transports may expose `state() -> (available, detail)` and
`delivers_on_send = True`. `SendResult.retry_after` (Telegram 429) is a floor
on the next retry. The modem's 30/day cap counts SMS **segments** (`daily_cap:`
error); numbers outside the profile are SUPPRESSED (`not_allowed_recipient:`).
`SENTRIX_MODEM_PORT` also accepts `mmcli` / `mmcli:<index>`. Templates and
step names beyond the fixed list are in the `Message` docstring
(`sentrix/alerting/channels/base.py`).

**LC endpoints and the pipeline (§9).** `GET /api/node` also returns `siren`,
`siren_arming`, `siren_test`, `camera`, `services`; `POST /api/node/siren-test`
answers 409 `conflict` while the siren line is disarmed (`siren_test`'s
result gained `armed`; `Actuator` gained `armed` and `status()`). `GET
/api/sos` returns `{current, recent, status}`; `POST /api/sos/demo/trigger
{incident_type}` (test mode only). `GET /api/channels`, `POST
/api/channels/test` (device admin); `GET /api/link`, `GET
/api/link/remote-audit`, `POST /api/link/reconnect` (device admin). The legacy
`session_token` cookie is no longer a session (it skipped the CSRF header);
login and logout still delete it. `database.recent_incidents` takes the
Events filters and `database.count_incidents` counts them. Clip mode: tick rows
written by a clip decision carry `extra: {"mode": "clip" | "fast_lane",
"clip": seq}` (`Pipeline.decide(..., extra=)`), `tools/replay_decisions.py`
refuses clip-mode logs, and the HUD's "AI STALLED" limit is
`max(stale_overlay_seconds, hop * (1 + budget_fraction))` in clip mode.
`AudioEngine.classify_window(samples, rms)` and
`add_sample_listener(callback(samples, end_at))` feed the clip buffer the
microphone's own samples. The Node page shows the `clip` block.

**Portal endpoints (§11), T-ACCT additions:** `POST /account/step-up`,
`POST /account/phone`, `POST /account/verify-email` (re-send),
`/household/audit`, `/privacy/notice`, `POST /privacy/consents`,
`POST /admin/customers/{id}/resend|status|assign`, `POST /admin/devices`
(provision), `POST /admin/devices/{id}/assign|transfer|retire`,
`POST /admin/operators/{id}`. The unauthenticated list is unchanged.
Vocabulary: token purpose `phone_verify`; household status `active | disabled
| erasing | closed`; a contact's opt-out is a withdrawn `consents` row.
`base.html` carries the global SOS banner (`/static/sos/sos.js`) on every
owner/member page and links the user's name to `/account`.

**Tools.** `python -m sentrix` and `python -m sentrix_portal` take `--help`,
`--host`, `--port`. `tools/doctor.py` never opens a camera by default (it
reports the configured `CAMERA_SOURCES`; `--probe-camera` reads one frame from
the configured index only, `--no-camera` skips the section); a "model runtime" section
lists the exports for `SENTRIX_RUNTIME` onnx/ncnn. `deploy/pi5/pi.env` sets
the clip profile (`SENTRIX_PIPELINE_MODE=clip`, `SENTRIX_RUNTIME=onnx`,
`SENTRIX_FAST_LANE=1`, `SENTRIX_FACE_DET_SIZE=320`).

**Demo tools and deployment (§15 items 3–4).**
* `tools/demo_reset.py [--dir var/demo] [--live-steps 0-3] [--activation]
  [--safety-net on|off] [--window 90]` builds the demo world in a directory it
  marks (`.sentrix-demo`) and wipes only such a directory (never `var/`):
  `demo_admin` and `demo_desk` (password + TOTP secret printed once),
  "Riya Sharma (demo)" (`riya_demo`), a CONFIRMED contact, a provisioned,
  assigned and (unless step 5 is live) claimed device with
  `<dir>/device/link/identity.json` and `label.html`. `--live-steps N` leaves
  the last N of steps 5–7 for the stage. Team phones, e-mails and the
  contact's Telegram chat come from `SENTRIX_DEMO_OWNER_PHONE`,
  `SENTRIX_DEMO_OWNER_EMAIL`, `SENTRIX_DEMO_CONTACT_PHONE`,
  `SENTRIX_DEMO_CONTACT_EMAIL`, `SENTRIX_DEMO_CONTACT_TELEGRAM` (env only);
  `<dir>/demo.json` (0600) keeps only paths, ids and the demo device's own
  LC password / session secret / evidence key.
* `tools/demo_run.py [--dir] [start|scene NAME|close|trigger KIND|clips]`:
  `start` runs `python -m sentrix_portal` (workers on) and `python -m sentrix`
  (SOS, channels, link to `http://127.0.0.1:<portal port>`, node on, GPIO off,
  console siren, `SENTRIX_AUDIO=0`) with every path in the demo dir, test mode
  on, `SENTRIX_LIVE_ALERTS` and every channel switch forced to 0, provider
  keys and `SENTRIX_USER_*` blanked, `SENTRIX_TEST_RECIPIENTS` from the
  environment only, `SENTRIX_SOS_DEMO_WINDOW_SECONDS` = the demo window;
  `assert_safe` refuses a webcam, a stream, the microphone, a live switch or
  the real `var/`. Browsers use `http://portal.localhost:8100`
  (`portal_session`) and `http://127.0.0.1:8000` (`lc_session`).
* Camera: `CAMERA_SOURCES=demo-reel:<reel.json>` (`sentrix/io/demo_reel.py`):
  normal footage (vid07 0–60 s) in a loop; `demo_run.py scene gun|fire|gun-b|fire-b`
  writes `<dir>/scene.txt` and the reel plays that clip once, resized to the
  idle size, then returns to idle. A token present at start is ignored. The
  test video (wa02) is refused by file name and by the dataset's split table.
  Scenes by measured reliability (M5 replay, commit `0b9e659`, 6 Hz):
  `gun` = gun.mov (val) L3 at 7.3 s, `fire` = 4.mov (val) L3 at 10.9 s;
  in-sample backups `gun-b` = 2.mov, `fire-b` = 6.mov (reach L5). The 10 live
  pre-runs per clip (keep ≥ 9/10) are measured on the demo laptop.
* `POST /api/sos/demo/trigger` is now **device admin only**
  (`require_lc("device_admin")`; a tunnelled owner gets 403), still test mode
  only in the engine, with a button on the LC SOS page and
  `demo_run.py trigger`; `close-incident` stays open to the household (the
  owner's own "next scene" control). Both are the `demo.*` seams of §5.6.
* `deploy/portal/`: `Dockerfile` (python:3.12-slim, uid 10001, Portal
  packages + `tools/provision_device.py` + `tools/portal_pg_check.py`;
  `Dockerfile.dockerignore` denies everything else), `requirements.txt`
  (Portal-only, `psycopg[binary]`), `docker-compose.yml` (`db` on the internal
  `backend` network only, `portal` without ports, `caddy` the only service with
  ports 80/443 at 172.30.0.10 = `PORTAL_TRUSTED_PROXIES` =
  `FORWARDED_ALLOW_IPS`), `Caddyfile` (HSTS, WebSockets, `flush_interval -1`),
  `.env.example` (names only, test mode on), `backup.sh` / `restore_test.sh` /
  `check_ports.sh`, and the Oracle / Render + Supabase runbook. Validated by
  `tests/test_deploy_portal.py`; Docker itself was not run.
* `tools/portal_pg_check.py`: `ddl` (every table, index and the outbox
  `ON CONFLICT` compile for PostgreSQL), `suite` (the Portal tests under a
  VARCHAR length guard, which SQLite does not enforce), `live --url` (a
  throwaway schema on a real server; needs psycopg, not in the laptop venv).

---

## 16. Open questions and contract-review decisions

Resolved:

1. D13 wording — **resolved** as read in §8.1 (test mode allows only
   allowlisted recipients, with both switches); recorded as a clarification in
   the accepted decisions.
2. Device id format — **UUID4** (§5.2); the Portal validates ids with a UUID
   regex.

Contract-review findings answered differently (with the reason):

* *NullSosService returns `unsupported` for everything.* Only the SOS-only
  calls do. `false_verdict`, `set_mode` and `approve_package` keep the
  pre-portal behaviour, because the console's routes are thin wrappers over
  the service and SOS is **off by default**: refusing them would break
  verdicts, HOME/ARMED and "Send now" on every default install.
* *`current(view) -> dict`.* Returns `dict | None` (None when no request is
  open), which is what the LC panel and `/api/sos` need.
* *Roles in `require_lc`.* Kept, but only for LC-only routes
  (`require_lc("device_admin")`); the tunnel path table stays the single place
  that enforces tunnelled roles. The device admin's session counts as a
  step-up.
* *Step names.* The list is kept and `desk_t0` added (the Desk page while
  offline needs its own dedupe step).
* *Telegram actions.* Letters `a`, `c`, `k` only; "False alarm" is a URL
  button to the PIN page (a PIN typed in a chat stays in its history).
* *Failed hardening.* The LC installs the fallback that refuses every
  state-changing request (the alarm must stay visible); the Portal treats a
  failed middleware as fatal (it faces the Internet).
* *401 JSON.* Tunnelled requests, APIs and fetches get 401 JSON from
  `require_lc`; `routes.require_auth` keeps the 307 to `/login` for every
  LAN request until T-WEB moves its APIs to `require_lc` (§9.1), so no
  existing page breaks in between.
* *The L5 voice call* (the review left it open): skipped once SOS owns the
  incident; its job moves to the `rise5` step (a `modem_call` there is Tier 1).
* *Authorised-face disarm* (left open): T-SOS checks the live per-track
  identity at the end of the entry delay and disarms visibly
  (`by="face:<name>"`); the identity layer never changes the mode (§7.1).

Still open:

3. Siren relay polarity — **settled by T-HW** as active-high (NPN driver,
   `gpio=17=op,dl`; `SENTRIX_SIREN_ACTIVE_HIGH=0` with `gpio=17=op,dh` for
   opto boards); docs/8_RASPBERRY_PI5_INTEGRATION.md §6 updated. Still to verify on the bench with an
   LED in place of the siren (`deploy/pi5/README.md`).
4. Whether owners may ever log in to the LC directly (currently: no — LC keeps
   `SENTRIX_PASSWORD` only; owners use the Portal or the LC via the tunnel).
5. The multi-process Pi layout (review blocker) is deferred by the
   single-process decision; revisit if Gate A fails with the link running.
6. Liveness for the authorised-face disarm (a photograph could clear the entry
   delay): out of scope for the capstone; state it in the report.

Integration requests not applied (with the reason):

7. *`devices.bootstrap_revoked_at` column* (T-LINK). Not added: the Portal has
   no migration baseline yet (Alembic is a §15 deploy step) and `create_all`
   does not add columns to an existing `portal.db`. The hub keeps the
   server-computed `devices.last_hello["token_confirmed"]` flag, which has the
   same meaning; move it to a column with the Alembic baseline.
8. *The Portal's own SOS texts from `sentrix/sos/templates.py`* (T-LINK:
   "switch `services/sos._after_event` once its API exists"). Not switched:
   the owner prompts the Portal writes use the fixed §7.10 wording in
   `sentrix_portal/services/sos.py`, and contacts and the Desk get the
   device's `sos_text_full`; moving every Portal message to the template module
   touches most of T-LINK's message logic and needs its own tests. The SOS
   preview already uses the template module.
9. *Bundled Inter / JetBrains Mono fonts* (T-WEB): needs a download; the CSS
   falls back to system fonts (CSP `font-src 'self'`).
10. *`ncnn` / `pnnx` and the `buffalo_s` face pack* (T-CLIP): not installed
    (each downloads binaries or models); the backends are tested with fakes.
    `buffalo_s`'s match threshold needs a re-check on enrolled team faces.
11. *Promoting a slim scene model* (T-MODEL): neither slim head passes its
    pre-registered bar (LOVO macro-F1 0.229 vs DINOv2 0.440); `models/v5_slim`
    stays out of `SceneEngine._WEIGHTS` and DINOv2 stays the scene model.

---

## 17. Verification (live smoke run, 2026-10-15)

The integrated product was run for real on the maintainer laptop (Windows 11,
`.venv` Python 3.14, CPU torch), not only in-process: `python -m
sentrix_portal` (workers on) and `python -m sentrix` as **two processes**,
linked over a real WebSocket to `127.0.0.1:8100`, driven by an `httpx`
script that behaves like a browser (cookies, the page's CSRF token, TOTP).
Environment: `tools/demo_reset.py --dir <scratch>/demo --window 90`, both
processes started with `demo_run.py`'s own `portal_env` / `device_env` (and
once with `demo_run.py start` itself), test mode on, every live switch 0,
no allowlist, `SENTRIX_AUDIO=0`, camera = the demo reel (vid07 idle, scenes
`gun.mov` / `4.mov`; never wa02), device `var/` and Portal database in the
scratch directory. `var/sentrix.db` and its WAL/SHM kept their sizes and
timestamps. No webcam or microphone was opened; every message ended
SUPPRESSED (or QUEUED, then SUPPRESSED) in the Portal's outbox.

**What was run, and what happened** (times from the driver's clock):

| Step | Result |
|---|---|
| Admin sign-in + TOTP, provision a device (label + identity), create a customer with it, welcome letter | 303/200, letter and label `Cache-Control: no-store`; < 0.5 s |
| Activation with the letter's code, password chosen; the same code again | 303 to `/onboarding`; reuse 400 |
| Wizard steps 1-4, contact added; the contact confirms through the consent link (read from the SUPPRESSED e-mail in the outbox) | all 303 to the next step; contact CONFIRMED |
| Device started on the provisioned identity (bootstrap, unclaimed), then claim at step 5 | "is now yours"; token on the device and the tunnel answering in **0.7 s** |
| Steps 5-7 (room, PINs, window, consents) | 303 to `/home?welcome=1`; review page carries "SENTRIX does not contact 112 itself." |
| Console pages direct and through `/d/{id}/`: dashboard, live, events, alerts, evidence, dispatch, authorized, sos, node | all 200 both ways, 4-33 ms; same DOM ids except the LAN-only `logout-link` and the device-admin scripted-trigger box on `/sos`; tunnelled URLs under the prefix |
| 14 GET APIs direct and tunnelled (`/api/health`, `config`, `incidents`, `level-history`, `metrics`, `node`, `sos`, `sos/requests`, `sos/preview`, `dispatch/latest`, `link`, `link/remote-audit`, `channels`, `authorized/preview.jpg`) | same status and size both ways (`/api/mode` is POST-only: 405 both) |
| `/ws/threat` direct and `/d/{id}/ws/threat` | 4 state messages in 1.6 s / 1.3 s; cross-origin `Origin` refused |
| MJPEG `/video` direct and `/d/{id}/video` | 30 frames at **29.9 fps** direct; **13.6 fps** tunnelled (cap 15), first frame 0.13 s |
| Real detection: `demo_run.py scene gun` | PENDING weapon request **9.8 s** after the command (8.0 s and 9.3 s on two more runs); Portal mirror PENDING +0.9-1.1 s; owner e-mail `t0` SUPPRESSED; no `tel:112` on the page |
| Owner approves on the Portal; Desk (TOTP) acknowledges and resolves | device DISPATCHED **0.01-0.28 s** after the POST; contact e-mail `contacts_t0`; ACKNOWLEDGED +0.05-0.5 s; CLOSED on both sides +0.3-0.5 s; incident verdict TRUE |
| Disclosed scripted trigger (`demo_run.py trigger weapon`), "I'm checking", no answer | flagged `simulated`, text says SIMULATED DETECTION; EXPIRED at **91.6 s** (90 s window), owner page "Unanswered", lapse e-mail; "Request help now" opened a linked request, DISPATCHED in 0.3 s, parent stayed EXPIRED |
| Real detection again, decline with a wrong PIN, then the right PIN | wrong: `pin_invalid`, still PENDING; right: DECLINED in 0.4 s, incident FALSE, no PIN in the device's remote audit |
| `scene fire` (4.mov) | PENDING fire request 11.6-11.9 s after the command |
| Portal killed (`taskkill /F`) with the fire request PENDING | device saw the link down at once and kept authority: the LC showed and answered the request (device-admin approve offline -> APPROVED), modem off so nothing was texted; outbox rows queued |
| Portal restarted | link back in 1.5 s; outbox drained at once (5 rows); mirror DISPATCHED. Before fix 2 below the Portal first logged `device_offline` and paged the contacts; after it, a second restart after a 60 s outage logged no `device_offline` and the device's queued `checking` arrived |
| `demo_run.py start`, then the device process killed | the banner printed, the tool noticed and stopped the Portal itself |

**Bugs found and fixed** (regressions in `tests/test_live_smoke_regressions.py`
and `tests/test_sos_engine.py`):

1. *Portal: sign-in sometimes landed back on /login.* With FastAPI 0.141 a
   `yield` dependency's exit (the session commit) runs **after** the response
   is sent, so a client following the 303 at once arrived before its session
   row existed; the in-process ASGI client hid it. `sentrix_portal.db.DB =
   Depends(get_db, scope="function")` is now what every route and security
   dependency takes: the work commits before the response leaves.
2. *Portal restart read as a lost device heartbeat.* At start the watcher
   compared the device's last-seen time (from before the Portal went down)
   with now and declared the device offline 0.5 s before it reconnected:
   tamper signal, cloud takeover and contacts paged for the Portal's own
   outage. Silence is now counted from `max(last_seen, hub.started_at)`.
3. *A confirmed fire never reached the contacts with the demo window.* The
   fire safety default fires at T+2 min; the 90 s demo window expired first
   and the rule was dropped. A safety-default alert still owed when the
   window lapses now goes out first (contacts also get the lapse notice).
   Production windows (30-60 min) were not affected.
4. *LC showed "0 queued" while offline.* The link client refreshed the outbox
   depth only in a connected session's heartbeat; while offline it now
   refreshes every 2 s (a kick still ends the back-off at once).
5. *The device phoned huggingface.co at every start* ("unauthenticated
   requests to the HF Hub") although the DINOv2 base weights were cached: timm
   asked for the latest revision. Cached base weights now load from the
   cache file (`pretrained_cfg_overlay`); only a missing cache downloads, and
   `merge_delta` still checks the fingerprint.

**Not verified / still open.** Telegram (no bot token: owner Telegram was not
linked, so only e-mail prompts were exercised live; the e-2-e suite covers the
buttons with a fake). The modem path and SMS (modem off on the laptop). Real
delivery to allowlisted team phones (needs both live switches; not done in
this run by rule). Ctrl+C on `demo_run.py start` from a console (processes were
stopped with `taskkill`). The 10 live pre-runs per clip: 3 of 3 `gun.mov`
and 3 of 3 `4.mov` runs reached L3 in this session, not 10. Wording nit
unchanged: "near opposite the SBI ATM" (the template prefixes "near" to a
landmark that already has a preposition). The undelivered flag is raised on
every prompt in test mode (deliveries are SUPPRESSED), as designed.

