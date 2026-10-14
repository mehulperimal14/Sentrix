# T-WEB hand-off notes (branch `wip/web`)

Track T-WEB: the Local Console (LC) hardened for the tunnel, every parity "F"
fix, and the new LC pages (docs/7 §2.1, §5.8, §9; decisions D10, D13).
Only T-WEB files were changed: `sentrix/web/{routes,streaming,security,
hardening,setup_routes}.py`, `sentrix/web/templates/**`,
`sentrix/web/static/**`, `tests/test_web_*.py` and this file.

## M10 status (for CLAUDE.md)

- [x] Hardening middleware (`sentrix/web/hardening.py`, pure ASGI): tunnel
  identity verified on every HTTP and websocket request and stored at
  `scope[tunnel_auth.SCOPE_KEY]`; every bridge header stripped when it fails;
  `/login`, `/logout`, `/setup/*`, `/docs`, `/redoc`, `/openapi.json`,
  `/video`, `/ws/*` refused (404 JSON / websocket close) for tunnel identities.
- [x] CSRF: `X-CSRF-Token` = `auth.csrf_for_session(cookie)` required on every
  POST/PUT/PATCH/DELETE of an `lc_session`; any LAN state-changing request a
  browser marks cross-site (`Origin`, `Sec-Fetch-Site`, `Referer`) refused,
  login included; tunnelled requests are not re-checked (the Portal did).
- [x] CSP with a per-response nonce (`script-src 'self' 'nonce-…'`,
  `style-src 'self' 'nonce-…'`, `img-src 'self' blob: data:`, `connect-src
  'self'` + `ws(s)://<host>` on the LAN, `frame-ancestors 'none'`,
  `object-src 'none'`, `base-uri 'none'`, `form-action 'self'`), plus
  X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, COOP and
  `Cache-Control: no-store` outside `/static`. `SENTRIX_CSP=0` drops only
  the CSP.
- [x] No inline script, handler or style anywhere: 10 external scripts in
  `static/js/` (`app.js` core + one per page) with `data-*` hooks and
  `addEventListener`; every server/device string goes in with
  `textContent`/DOM building (a test scans for `innerHTML` & co).
- [x] Base-aware URLs: Jinja globals `url(path)` and `static_url(path)`
  (versioned `?v=` so the Portal's 1 h static cache cannot serve old JS),
  `<body data-base>`, `Sentrix.url()`/`Sentrix.api()`/`Sentrix.rel()` in
  scripts (websocket included), `routes.base_url`/`media_url` for every
  Python-built URL (snapshots, faces, evidence, paging links,
  `vault.view_url` re-prefixed in the route: `vault.py` untouched);
  `active_page` drives the nav highlight.
- [x] `lc_session` cookie at login (the legacy `session_token` is deleted at
  login and logout); `GET /logout` is a confirm page, `POST /logout` signs out.
- [x] `/video` and `/ws/threat` through `auth.principal_of`; websocket Origin
  check (foreign origin refused at the handshake); a missing/expired session
  is accepted then closed with 1008 so the page shows "sign in again"; the
  socket re-checks its session every 60 s.
- [x] APIs and media on `auth.require_lc()` (401 JSON); pages keep
  `require_auth` (307 to `/login` on the LAN, 401 through the tunnel).
- [x] `/api/incidents?limit&offset` (+ `incident_type`, `level`, `verdict`
  TRUE/FALSE/NONE, `since`/`until` in device time) with `total`;
  `/api/level-history`.
- [x] Redaction (`redact_url`, recursively) on `/api/health` (incl. every
  service health block), `/api/metrics`, `/api/config`, `/api/incidents`,
  `/api/dispatch/latest`, `/api/level-history` and the websocket payload.
- [x] Parity fixes: engine-bar % text (theft/harmful "n/a (no model)",
  is_night shown), scene thresholds + fresh flag, Events stats block,
  verdict note, who/when, filters, paging and an SOS column (owner view:
  duress shows as "Marked as false alarm"), FALSE-with-PIN flow; Evidence
  chain badge + problems, readable objects (`pistol 0.82`), View link, room
  label; Alerts "Level 3+", lightbox without inline JS; Dispatch & SOS:
  local time, EXPIRED/FAILED, chosen authority/sent at/by/to/request,
  authority choice incl. MEDICAL, no Send on SENT/CANCELLED/EXPIRED, the
  real error text, "Request help now"; Access: name field for uploads,
  identity backend / failing-closed status, grid refresh after capture,
  who changed the gallery (`by` on the gallery audit rows); Live page on the
  shared reconnecting websocket, "SECURE" default, remote fps label.
- [x] Dashboard SOS panel (ids `dispatch-panel`, `dp-incident`, `dp-tci`,
  `dp-address`, `dp-authority`, `dp-dismiss` kept): open request from
  `state.sos` (countdown, snapshot, Dispatch POLICE/FIRE/MEDICAL = approve
  with that authority, Call 112 now, I'm checking, False alarm (PIN));
  with SOS off, the current incident's pending package from
  `/api/dispatch/latest` at L4+ as before. Event Log loads
  `/api/level-history` first, then follows the committed `alarm.level`.
- [x] On every page: test-mode red banner, full-screen minimisable SOS prompt
  ("Do you need assistance?"; reopens on a new request or a level rise),
  SOS banner + countdown chip, "remote view: N", "no outbound channel"
  banner (`state.channels.warn`), session-expired banner, nav dots AI /
  Camera / Mic / **Link** (`state.link`) / **Modem** (`state.channels.modem`).
- [x] New pages: `/sos` (open request, history with paging, per-request audit
  trail with chain status, SOS preview, test-mode "close incident"),
  `/node` (telemetry + GPIO from `/api/node`, link, channels/modem, system
  health, latency, HOME/ARMED with PIN, confirmed 1 s siren test, read-only
  config), `/setup/wifi` (setup mode only).
- [x] Role-aware UI (`routes.ROLE_CAPABILITIES`, docs/7 §10): member = gallery
  view only and no siren test; Desk operator = Dashboard + Live only, no
  answer buttons, no history fetch; Portal users get no console logout.
- [x] "Call 112 now": built on the server (`routes.call_target`): `tel:112`
  outside test mode; in test mode the Desk number (`state.sos.desk_number`,
  else `link.profile.desk_number()`), or a disabled button; an emergency
  number is never accepted as the Desk number; no script holds 112.
- Tests: `tests/test_web_console.py` (pages under "" and `/d/abc`, URL
  prefixes, CSP markup, XSS, roles, banners, parity fixes),
  `tests/test_web_hardening.py` (bridge, CSRF, headers, websocket, cookies),
  `tests/test_web_api.py` (incidents API, redaction, gallery, setup mode):
  128 tests. Full suite: 468 passed (340 before T-WEB).

## Changes needed in files T-WEB does not own

1. **`tests/test_foundation_contract.py::TestLcRoutesAreThinWrappers`**
   (foundation) posts with the legacy `session_token` cookie and no CSRF
   header. It passes because the LC still honours the legacy cookie without
   the header for same-site requests (a pre-upgrade tab, at most 12 h). When
   integration removes `LEGACY_COOKIE` from `sentrix/web/auth.py`
   (`principal_of`/`session_cookie`, "until the rename lands"), switch that
   fixture to `lc_session` and add `headers={"X-CSRF-Token":
   auth.csrf_for_session(cookie)}` to its POSTs; then delete the
   `name == LEGACY_COOKIE` branch in `hardening.csrf_refusal`.
2. **Other tracks' LC tests** (T-SOS `/api/sos/*`, T-HW `/api/node/siren-test`,
   T-LINK `/api/link/reconnect`, T-CH `/api/channels/test`) that POST to the
   real app with an `lc_session` cookie must send `X-CSRF-Token:
   auth.csrf_for_session(cookie)`, or they get 403 `csrf` after the merge.
   Tunnelled test requests (bridge headers from `tunnel_auth.build_headers`)
   need nothing.
3. **`sentrix/pipeline/state.py`**: `cloud_online`/`cloud_available` are still
   always-True placeholders in `_with_aliases`. No page reads them any more
   (the Link dot reads `state.link`); drop them or set them from
   `link.connected`.
4. **`sentrix/pipeline/state.py::ws_payload`** is not redacted; the LC socket
   uses `routes.ws_payload()` (redacted). T-LINK's `state` messages should
   use `sentrix.web.routes.ws_payload()` or `state.ws_payload` should apply
   `routes.redacted` (no camera URL is in the state today: defence in depth).
5. **`sentrix/storage/database.py`**: `recent_incidents` has no filters, so
   `routes.query_incidents` runs the filtered query itself and mirrors the
   row shape in `routes._incident_row`. Suggested: `recent_incidents(limit,
   offset, *, incident_type=None, min_level=None, verdict=None, since=None,
   until=None)` plus `count_incidents(...)`, then delete `_incident_row`.
6. **Fonts** (docs/7 §9.1 "fonts served locally"): the Google Fonts CDN links
   are gone (CSP `font-src 'self'`); the CSS uses Inter / JetBrains Mono when
   installed, else the platform UI and monospace faces. Bundling the OFL
   woff2 files under `static/fonts/` needs a download that was not made here.

## Contract assumptions other tracks must match

- **T-SOS** (`/api/sos/*`): POST bodies are JSON — approve `{authority}`,
  decline/safe `{pin}`, checking/help-now `{}`; the help-now path is
  `/api/sos/requests/{id}/help-now`; errors `{detail, code}` with
  `pin_invalid` 403, `pin_locked` 429, `unsupported`, `conflict`. Pages read:
  `GET /api/sos/requests?limit&offset` → a list, or `{requests: [...]}` /
  `{items: [...]}`; `GET /api/sos/requests/{id}` → `{request, events,
  chain}` (`chain` = `requests.verify_chain`); `GET /api/sos/preview` →
  a string or `{text|full, sms}`; `POST /api/sos/demo/close-incident`. The
  prompt reads `state.sos` exactly as `SOS_IDLE` + `desk_number`, shows only
  the buttons named in `actions` (`SOS_ACTIONS`), takes the countdown from
  `expires_at` (or `remaining_s` when absent), and treats DRAFTED/PENDING
  or an `approve` action as "prompting". `snapshot` must stay base-relative
  (`media/snapshot/<name>`).
- **T-HW**: `GET /api/node` → the `state.node` block with `gpio = {siren,
  leds: {green, amber, red}, buzzer, pir, door, mains}`; `POST
  /api/node/siren-test` answers `rate_limited` (429) / `step_up_required`
  (403) as JSON. `sentrix.io.network`: `scan() -> [{ssid, signal,
  security}]`, `connect(ssid, psk) -> {ok, error}`, `status() -> {connected,
  ssid, ip, via}` (blocking calls are run in a thread).
- **T-ACCT** (`tools/provision_device.py`): `/setup/wifi` accepts the label
  code when `setup_code_digest` = SHA-256 hex of the code as printed
  (`KTRW-BQXL`), uppercased, or without spaces/hyphens; anything else fails.
- **T-LINK**: role/consent/step-up enforcement for tunnelled requests stays in
  `sentrix/link/policy.py` (the console only hides controls, per
  `routes.ROLE_CAPABILITIES`). The Desk view must allow `/api/health` (nav
  dots); the dashboard does not call `/api/level-history` or
  `/api/dispatch/latest` for operators. The console's redirects stay
  unprefixed (`Location: /dashboard`); the proxy rewrites them. The relay
  may add `remote_video: {fps, delay_ms}` to state payloads (Dashboard and
  Live show it). Forwarding `Origin`/`Referer`/`Sec-Fetch-*` would not break
  anything: verified tunnel requests skip the cross-site check.

## Incident during development (needs the owner's attention)

While trying to open a visual check in the desktop app's browser pane,
`preview_start(name="web-demo")` resolved the **main checkout's**
`G:\Sentrix\.claude\launch.json` ("sentrix": `python -m sentrix`) instead of
the worktree's demo config and started the real app for about 8 seconds
before it was stopped. Its log shows: the additive M10 migration ran on
`G:\Sentrix\var\sentrix.db` (new nullable columns on `incidents`,
`dispatch_packages`, `enrolled_faces`; `create_all` for the new tables;
nothing dropped), the pipeline began initialising and loaded the YOLO
detectors, and `CAMERA_SOURCES=0` in `.env` means the capture source (the
webcam) was probably opened briefly. No live loop ran, no frame was
analysed or stored, no snapshot/evidence/incident was written and nothing
was sent (Twilio disabled, live alerts off). The visual check then used a
separate server with a fake pipeline and a temporary database (scratchpad
`web_demo.py`) and a headless Chrome over CDP.
