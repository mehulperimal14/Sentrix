# Integration notes: T-LINK (device link, tunnel, relays, Portal SOS, Desk, contact pages)

Branch `wip/link`, built on the foundation commits of `sentrix/portal`. Every
file below is T-LINK's per docs/7 §2.3/§2.5; no shared file was edited.

## What landed

| Area | Files |
|---|---|
| Device link client (service `link`, off unless `SENTRIX_LINK_ENABLED=1`) | `sentrix/link/client.py` |
| Tunnel executor (in-process `httpx.ASGITransport` into the LC) | `sentrix/link/tunnel.py` |
| Device-enforced command rules | `sentrix/link/commands.py` |
| Tunnel path table (roles, consents, step-up, media rate limit, Desk rule) | `sentrix/link/policy.py` |
| Outbox / remote audit storage (epoch, prune anchors, counters) | `sentrix/storage/link_store.py` |
| LC `/api/link`, `/api/link/remote-audit`, `POST /api/link/reconnect` | `sentrix/web/link_routes.py` |
| Portal link hub (sockets, ingest, mirror, commands, tunnel, relays, watcher, profile, token, unpair) | `sentrix_portal/services/hub.py`, `sentrix_portal/routes/link_hub.py` |
| `/d/{id}/…` proxy, `/d/{id}/video` MJPEG relay, `/d/{id}/ws/threat` state relay, offline mirror page | `sentrix_portal/routes/device_proxy.py`, `templates/device/offline.html`, `static/device/offline.js` |
| Portal SOS: mirror, cloud authority, timers, `respond`, messages, tokens | `sentrix_portal/services/sos.py` |
| Portal SOS pages, `/a/{token}` action links | `sentrix_portal/routes/sos.py`, `templates/sos/*`, `static/sos/*` |
| Desk console | `sentrix_portal/routes/desk.py`, `templates/desk/*`, `static/desk/desk.js` |
| Contact SOS pages `/c/{token}` | `sentrix_portal/routes/contacts.py`, `templates/contacts/*` |
| Tests | `tests/test_link_support.py` (helpers), `test_link_policy.py`, `test_link_tunnel.py`, `test_link_client.py`, `test_link_portal.py`, `test_link_pages.py`, `test_link_e2e.py` |

## Changes needed in shared / other tracks' files (not made here)

1. **`sentrix_portal/templates/base.html`** (foundation): for owner/member
   principals add `<link rel="stylesheet" href="/static/sos/sos.css">` in the
   head and `<script src="/static/sos/sos.js" nonce="{{ csp_nonce }}"></script>`
   before `</body>`. Reason: the global "Do you need assistance?" banner on
   every Portal page (T-ACCT's pages included). My own pages include it already;
   the script polls `/sos/api/open` and builds the banner with `textContent`.
2. **`sentrix/web/hardening.py`** (T-WEB): must do what
   `tests/test_link_support.py::fake_hardening_install` does — `tunnel_auth.verify`
   on every request, store the identity at `scope[tunnel_auth.SCOPE_KEY]`, strip
   the bridge headers otherwise. Until it lands the LC answers every tunnelled
   request 401 (fail closed). The "every href/src starts with /d/{id}" parity
   assertion belongs to T-WEB's base-aware templates; the tunnel already
   delivers `base=/d/<id>` and the Portal CSRF token to the console
   (asserted end to end).
3. **`sentrix/storage/models.py`** (foundation): optional hardening —
   `__table_args__ = {"sqlite_autoincrement": True}` on `LinkOutboxRecord` (new
   databases) so a seq can never be reused. Meanwhile `link_store.prune` never
   deletes the oldest and the newest outbox row (the newest prevents rowid
   reuse; the oldest's `created_at` is the outbox *epoch* in `hello`).
4. **`sentrix_portal/models.py`** (foundation): suggest a column
   `devices.bootstrap_revoked_at` (TS). Today "bootstrap revoked at claim" is
   kept as `devices.last_hello["token_confirmed"]` (set on the first Bearer
   hello; cleared by `issue_token` and `unpair`): Bootstrap auth is accepted
   for an unclaimed device, and for a CLAIMED one only until its token worked
   once (so a token issued while the device was away, or lost in a Portal
   restart, still reaches it: the hub re-mints it).
5. **`tests/test_foundation_contract.py::TestPortalServiceStubs`** (foundation)
   asserts stub behaviour. It still passes: an unknown device is never queued
   (`queued: False`), and `sos.respond(None, …)` answers `unsupported` (no
   database session). Integration may rewrite it against the real hub.
6. **docs/7 (integration edits the contract)** — additive protocol details
   this implementation uses:
   * §5.4 `hello.outbox` gains `epoch` (ISO `created_at` of the oldest outbox
     row). A changed epoch, or `tail_seq` below the Portal's watermark, means a
     factory-reset device numbering from 1 again: the Portal drops that
     device's `device_events` dedupe rows and resets `last_acked_seq` (audit
     `device.outbox_reset`). The Portal also treats "same seq, different
     payload" as new.
   * §5.4 the Portal answers every `heartbeat` with `ack{seqs: [], upto}`; the
     device uses any Portal message as liveness and drops a connection silent
     for `offline_after_seconds` (30 s).
   * §5.4 `welcome.subscriptions.video` is always `null`: the hub sends
     `video.subscribe` with the viewer's actor right after the welcome,
     because the device checks consent per actor.
   * §5.7 the device replays the path unchanged (no `root_path`), as §5.7
     says; the base travels in `x-sentrix-base`. (The T-LINK task text said
     "root_path /d/{id}"; the contract wins.)
   * §5.7 relay override while offline: `camera_online=false`,
     `camera_available=false`, `ai_hz=0`, `video_fps=0`,
     `link={…, connected: false, state: "offline"}`, `link_online=false`,
     `device_offline=true`.
   * §4.2 `hub.issue_token(db, id)` commits `db` before sending (the device
     reconnects within milliseconds and must find the new digest).
   * §4.2 `hub.unpair` leaves the device ASSIGNED to its household, token
     revoked; Bootstrap works again (unclaimed, re-claim needs a new claim code).
   * `device_commands.args` never stores `pin` or `text`; `_actor` holds the
     queued command's actor; `sms.send` is never queued.

## What other tracks' code must do for this to work

* **T-ACCT**
  * Claim flow: set `Device.state = "CLAIMED"`, `claimed_at`, then
    `await services.hub.issue_token(db, device_id)`.
  * `tools/provision_device.py`: `identity.json` (UUID4 `device_id`, serial,
    `bootstrap_secret`) and `Device.bootstrap_hash = security.token_digest(secret)`.
  * `accounts.build_profile` returns the §5.11 profile; the hub sets
    `profile["version"] = households.profile_version` and pushes on connect when
    the device's copy is older, and on `hub.push_profile` after a change.
  * Consents are read from `consents` rows (`withdrawn_at` NULL = active):
    `remote_view` (video relay), `sos_snapshot` (snapshot kept), `desk_live_view`
    (Desk live view), `medical_notes` + `households.include_medical` (notes on
    Desk and contact pages). The device enforces the profile's copy too.
  * Owner recipients: `users.email`, `TelegramLink(subject_type="user")`;
    contacts: `EmergencyContact.email`, `telegram_chat_id` or
    `TelegramLink(subject_type="contact")`; `households.channels` toggles
    `telegram` / `email`. `users.phone_enc` (sealed) is the Desk's callback.
  * Home page: link a device card to `/d/{id}/dashboard`; `hub.is_online(id)`.
  * Unpair route: owner + step-up, then `await hub.unpair(db, id, actor)`.
* **T-SOS** (the Portal mirror reads exactly the contract shapes)
  * `sos.created` = `{request, event}`; `sos.transition` / `sos.event` =
    `{request_id, version, request, event}`; `request` = `request_dict` minus
    `snapshot_path`/`session_id` with full flags; `event` = `event_dict`;
    dedupe `protocol.sos_dedupe_key(request, "device", event.seq)`.
  * `reminder` events should carry `detail.step` (`r120`…) and `level_rise`
    `detail.level`; the Portal falls back to the elapsed time / peak level.
  * Frozen `policy` keys the Portal reads: `safety_defaults` (bool),
    `contacts` (list of `{id, consent_status}` or ids). Without `contacts` it
    uses the household's CONFIRMED contacts.
  * Handle `sos.update` events from the cloud: `delivered` (detail
    `{target: "desk"}`, sent once the request is in the Desk queue), `seen`,
    `notification` (Portal channel statuses, below), `acknowledge` and
    `resolve` (detail `{outcome, note, channel}`; outcome -> verdict).
  * `sync(request_id, cloud_state, cloud_version, authority_side, events)`
    receives the full cloud chain each reconnect (import is idempotent).
  * Offline ladder: `services.registry.get("link").is_online()` (False when
    off, unwelcomed, or the Portal silent for 30 s) or `state.link.connected`.
  * The Portal texts the owner with fixed §7.10 wording and uses the device's
    `sos_text_full` for contacts and the Desk; switch `services/sos._after_event`
    to `sentrix/sos/templates.py` once its API exists.
* **T-CH**
  * `notify.enqueue(db, recipient, message)` receives `Recipient(ref owner |
    contact:<id>, channel telegram | email)` and `Message(template sos.prompt |
    sos.reminder | sos.lapse_warning | sos.lapsed | sos.full | sos.duress |
    sos.resolved, step per §7.5)`. Telegram buttons carry
    `callback_data(nonce, approve|checking|acknowledge)`; the nonce is
    `ActionToken.nonce` (purpose `action_link` for the owner, `contact_page`
    for contacts; `request_id`, `subject_type/id` bind it). Inbound callback ->
    look the token up by nonce (not revoked, not expired, chat matches) ->
    `await services.sos.respond(db, token.request_id, action, actor=…,
    channel="telegram")`.
  * After every status change of an SOS message call
    `services.sos.notification_status(db, request_id=…, channel=…,
    recipient_ref=…, step=…, status=…, error=…)`: online the device gets
    `sos.update notification`, offline the cloud chain records it.
* **T-WEB**: item 2 above; the Node page can list `GET /api/link/remote-audit`.
* **T-HW**: publish `node.alert` (the Portal audits `device.node_alert`);
  `state.channels.modem` feeds the heartbeat's `modem` block.

## Tests and result

118 new tests (`tests/test_link_*.py`); the full suite: **458 passed**
(`G:\Sentrix\.venv\Scripts\python.exe -m pytest`). They cover the path table
and every command rule, the tunnel executor against the real LC app, the
client and hub over an in-memory pipe (pairing, profile, outbox, heartbeats,
commands queued offline, tunnel incl. chunked bodies, state and frames,
consent refusal, silent Portal, reconnect), the SOS mirror (order, gaps,
tampered chain, snapshot consent), the heartbeat watcher and tamper signal,
cloud timers / answers / duress / PIN back-off / contact rule, the reconnect
sync, **100 forced disconnects with 0 lost outbox events**, the Portal pages
(SOS, `/a/`, Desk, `/c/`, offline proxy, tenant isolation, CSP-clean HTML),
and one end-to-end run over loopback sockets (uvicorn Portal + `websockets`
device client): claim -> connect -> tunnelled dashboard -> state and frames
relayed -> SOS mirrored -> owner approves -> device applies -> Desk
acknowledges -> resolve.

## Still open (needs hardware, accounts or other tracks)

* Real delivery of the Portal's messages: T-CH's `notify.enqueue` is a stub
  in this branch (messages are built and handed over; tests capture them).
* Profile push needs T-ACCT's `build_profile` (logged and skipped until then).
* WSS through Caddy/TLS on the VM and through CGNAT is untested here (the
  client is the same `websockets` connector with a `wss://` URL; loopback
  `ws://` is tested). `tools/bench_link.py` (remote fps, glass-to-glass,
  uplink, Pi CPU; gate P4) is integration's.
* The adaptive video rate is driven by send back-pressure; its real behaviour
  on a 4G uplink needs the Pi measurement.
