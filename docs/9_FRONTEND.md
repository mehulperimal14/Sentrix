# 9. The SENTRIX frontend (v2)

Status: **done and deployed** (decision D22 in CLAUDE.md). Built on branch
`sentrix/frontend-v2`, merged to GitHub `main` and deployed on 2026-11-18/19 to
the Render Portal and the Pi (the Pi runs `2867bdf`). Tests at the switch-over:
Python suite 2243 passed, 1 skipped (`2024b07`); Vitest 15 passed; Playwright
22 passed, 1 skipped at desktop and phone sizes (`c1241c1`).
Design source: the SENTRIX Experience Plan and the platform prototype (private
artifacts linked from CLAUDE.md). This file is the contract between the two
React apps and the two Python apps.

## 1. What changes and what does not

* The server-rendered Jinja pages of the Portal (`sentrix_portal/templates`,
  `sentrix_portal/static`) and of the device console (`sentrix/web/templates`,
  `sentrix/web/static/js`, `sentrix/web/static/css`) were replaced by two
  single-page apps built from `frontend/`; the templates, the old static files
  and the HTML route modules are gone from both Python apps.
* **No data changes.** No table, column, file format, retention rule or
  directory under `var/` (device) or the Portal data dir changes meaning. New
  storage is additive only (the existing guarded `ALTER TABLE ADD COLUMN`
  pattern). The SOS engine, link protocol, tunnel, channels, guard, PIN rules,
  rate limits, roles and audit chain stay as they are: the new JSON endpoints
  call the same service functions the old HTML routes called.
* Links already in the world keep working: `/activate/{code}`, `/claim?sn=`,
  `/consent/{token}`, `/c/{token}`, `/a/{token}`, `/sos/{id}`, `/desk`,
  `/admin/...`, the device's `/dashboard`, `/setup/wifi`. The SPA routes them
  (§8).

## 2. Stack

| Part | Choice |
|---|---|
| Language | TypeScript (strict) |
| UI | React 19, React Router 7 (data routers), TanStack Query 5 |
| Build | Vite, npm workspaces (`frontend/apps/*`, `frontend/packages/*`) |
| Styling | Tailwind CSS 4 with the design tokens of the Experience Plan as CSS variables; light and dark themes; the device screen dark by default |
| Fonts | Anek Latin (display, countdowns), Noto Sans (body), bundled with `@fontsource` (no font CDN: the device works offline and the CSP blocks third parties) |
| Icons | lucide-react, always next to a word |
| Forms | react-hook-form + zod; server field errors mapped onto fields |
| API types | generated from each FastAPI app's OpenAPI with `openapi-typescript` |
| Tests | Vitest + Testing Library (units), Playwright (end to end against the real Python apps in a demo world) |

Layout:

```
frontend/
  package.json            npm workspaces, scripts: dev, build, test, e2e, gen:api
  packages/ui/            tokens.css, Tailwind theme, components, time/format helpers, strings
  packages/api/           fetch client (CSRF header, error model), generated types
  apps/portal/            owner and member app, setup, contact pages, Desk, Admin, sign-in
  apps/device/            device screen (wall view, alert), device admin on the LAN
  e2e/                    Playwright specs + a runner that starts tools/demo_run.py in a temp dir
```

Build output is committed so neither Render nor the Pi needs Node:
`apps/portal` → `sentrix_portal/static/app/`, `apps/device` →
`sentrix/web/static/app/`, each served under `/static/app/` (Vite `base`).
`cd frontend && npm install && npm run build` regenerates both; commit the
result. A Python test fails when `index.html` is missing or points at an asset
that is not there. Without a build the app paths answer 503 `not_built`.

Commands (in `frontend/`): `npm run build`, `npm test` (Vitest), `npm run e2e`
(Playwright; Edge by default, `E2E_CHANNEL=chrome` or `chromium` to change it),
`npm run typecheck`, `npm run dev:portal` (Vite on :5173) and
`npm run dev:device` (:5174), both proxying `/api`, `/d/`, `/ws`, `/video` and
`/media` to a running Python app, `npm run gen:api` (types from OpenAPI).

## 3. Who sees what

* **Portal app** (`sentrix_portal`, `/`): owner and member (Home, Activity,
  People, Settings, Live, the alert screen, setup), emergency contacts (token
  pages, no account), Desk operators, company admins, sign-in and activation.
  Owner views of the device (live picture, level, the device's own incidents,
  mode, recording) call the device's JSON API through the tunnel
  `/d/{device_id}/api/...`, `/d/{device_id}/video`, `/d/{device_id}/ws/threat`.
  The device's engineering views are never shown to owners.
* **Device app** (`sentrix/web`, `/`): the device screen (wall view, alert
  takeover with an on-screen PIN pad) and device admin on the LAN (Diagnostics,
  Activity, Help requests, Record video, Faces, Hardware, Network, Wi-Fi setup
  mode). The device app is LAN-only: no page is tunnelled (the path table has
  no page or static class, and a tunnelled request for an app path is a JSON
  404); the recordings' files stay LAN-only by `LC_ONLY_PREFIXES`.
* **Faces** are enrolled on the device admin's Faces page (capture from the
  device's camera or upload, on the LAN) and, since 2026-11-19, by the
  **owner** in the Portal app (People → *Faces SENTRIX recognises*: upload
  photos, or take one with the SENTRIX camera from its live preview; removing
  a photo asks for the password again). Members see the gallery read-only;
  the Desk never. Both need the household's `remote_view` and
  `face_recognition` consents. Photos go through the tunnel to the device and
  are stored only there (D12). Body re-identification needs no photo: the
  device learns a person's body look from their verified face (D24). The
  Portal app has no recording buttons (the device's path table still lets the
  owner start a recording through the tunnel after a step-up, and stop it).

## 4. JSON API conventions (both apps)

* Portal prefix `/api/v2`; the device keeps its `/api/...` paths (the tunnel
  path table and existing tests name them) and adds what is missing there.
* JSON in and out (`application/json`), except file downloads and images.
* **Auth**: the existing cookies (`portal_session`, `lc_session`). State-changing
  requests carry `X-CSRF-Token` (Portal: the session's token from
  `GET /api/v2/session`; before sign-in the double-submit login token the same
  endpoint sets). Portal endpoints use `require_role(..., csrf="header")`.
  API routes never redirect: 401/403 are JSON.
* **Errors**: HTTP status plus
  `{"error": {"code": "...", "message": "...", "fields": {"name": "message"}}}`.
  Codes in use: `not_signed_in` 401, `forbidden` 403, `csrf` 403,
  `step_up_required` 403, `totp_required` 403, `not_found` 404, `conflict`
  409, `device_offline` 409/503, `validation` 422 (with `fields`),
  `rate_limited` 429 (with `retry_after` seconds), `test_mode_only` 403.
  `message` is plain English for the user, never an exception string, a
  setting name or a file path.
* **Times**: ISO 8601 UTC with `Z`. The session says the household time zone
  (`Asia/Kolkata` by default); the apps format every time in it.
* **Never in a response**: password, PIN or TOTP hashes or secrets, session
  tokens, device bearer tokens, encryption keys, raw `.env` values. One-time
  secrets that the old pages showed once (activation code to the admin, claim
  code, recovery codes, a contact's share link) are returned once in the
  response that creates them, as before.
* Mutations return the updated resource (or `{"ok": true}` when there is none).
* Lists take `?limit=` and `?cursor=` where they can grow.

## 5. Portal API v2 (resources)

`session` · `auth` (login, logout, activate, step-up, password, e-mail
verification, sessions, 2FA) · `onboarding` (state of all steps, save a step,
claim, SOS preview) · `telegram` (link code) · `home` (device cards, open
requests) · `devices` (name, room, floor, default mode, mode switch) ·
`activity` (incidents mirror, requests, device events; verdicts) ·
`household` (occupants, pets, medical notes) · `members` · `contacts`
(add, edit, remove, resend, share link) · `audit` (household log) ·
`settings` (window, safety net, channels, PINs) · `privacy` (consents, notice,
history, export, erase, cancel erase) · `sos` (list, one, answer, snapshot) ·
`action/{token}` · `consent/{token}` · `contact-page/{token}` · `desk`
(queue, request, acknowledge, called 112, resolve, snapshot) · `admin`
(customers, devices, letters and labels as data, operators, audit chain).

The SPA renders printable letters and labels from data; QR codes are drawn
client side from the URLs the API returns.

## 6. Security

* CSP stays strict, with no inline script or style and no third-party origin.
  The device (`sentrix/web/hardening.py`): `script-src 'self'`,
  `style-src 'self'`, `connect-src 'self'` plus `ws:`/`wss:` to its own host,
  `img-src 'self' blob: data:`, no nonces. The Portal
  (`sentrix_portal/hardening.py`): the same, except that `script-src` still
  carries a per-request nonce that no page uses any more. React sets styles
  through the CSSOM, which CSP allows. Tunnelled responses (`/d/{id}/...`)
  carry the device's headers, not the Portal's.
* Test mode, the send guard, `SENTRIX_TEST_RECIPIENTS`, "no `tel:112` in test
  mode", duress parity (the owner's screens after a duress PIN look exactly like
  a normal false alarm) and the SOS contract's labels (docs/7 §7.10) hold in the
  new UI and are covered by tests.

## 7. Testing

* Python: every API endpoint has pytest coverage through the existing fixtures
  (temp `var/`, forced-off switches, loopback-only sockets). Tests that read old
  HTML are rewritten against the JSON API; the product e2e world drives the API.
* Frontend: Vitest for components, formatters and the client.
* End to end: Playwright starts `tools/demo_reset.py` + `tools/demo_run.py` in a
  temp dir (test mode, video-file camera, nothing sent) and walks the journeys
  of the Experience Plan at desktop and phone sizes.
* Nothing is pushed to GitHub before the suite, the frontend tests and a manual
  local run pass.

## 8. Routes

### Portal app (`sentrix_portal`)

The Python app answers `/api/v2/...` (`sentrix_portal/routes/api_v2/`), the
link hub, `/hooks/...`, the device tunnel `/d/{device_id}/api|media|static/...`
with the relays `/d/{device_id}/video` and `/d/{device_id}/ws/threat`, and
`/healthz`. Every other GET gets the app's `index.html`
(`sentrix_portal/routes/spa.py`, registered last; its routes yield to every
other route). A GET under `api`, `d`, `hooks`, `static`, `healthz` or `link`
that nothing claims, and any write nothing claims, is a JSON 404, never the
app. `/d/{device_id}` and any other non-API tunnel path (an old console page)
answer 303 to the app's `/live/{device_id}`.

| Path | Who | Screen |
|---|---|---|
| `/` | anyone | sends the person to their home screen, or to `/login` |
| `/login`, `/account/2fa` | anyone; signed-in people | sign-in; TOTP set-up and check (admin, Desk) |
| `/activate`, `/activate/:code` | new customer | activation with the welcome letter's code |
| `/setup`, `/setup/:step` (`you`, `people`, `device`, `alerts`, `review`, `done`) | owner | setup: 5 screens over the backend's 7 steps (1+2, 3+4, 5, 6, 7) |
| `/home` | owner, member | device cards, open alerts |
| `/activity` | owner, member | incidents, help requests, device events, verdicts |
| `/people` | owner, member | household, members, emergency contacts |
| `/settings`, `/settings/alerts`, `/settings/home`, `/settings/devices`, `/settings/account`, `/settings/privacy` | owner, member | alert window, safety net, channels, PINs; home details; devices and mode; account; consents, export, erasure |
| `/alert/:id` (old `/sos/:id` still works) | owner, member | the alert screen: answers first, then the snapshot and what contacts would get |
| `/live/:deviceId` | owner, member | live picture and level through the tunnel |
| `/claim` (`?sn=`) | owner | claim a device |
| `/account` | signed in | account (household people go to `/settings/account`) |
| `/consent/:token`, `/c/:token`, `/a/:token` | emergency contact, no account | consent link; contact alert page; one-tap action link |
| `/account/verify-email/:token`, `/privacy/notice` | anyone | e-mail verification; the privacy notice |
| `/desk`, `/desk/:id` (old `/desk/requests/:id`) | Desk operator (TOTP) | queue; one request |
| `/admin`, `/admin/customers`, `/admin/customers/:id`, `/admin/devices`, `/admin/operators`, `/admin/audit` | company admin (TOTP) | overview, customers (letters drawn from data), devices (labels drawn from data), staff, audit chain |

Old Portal paths redirect: `/onboarding[/:step]` → `/setup`; `/household` →
`/settings/home`; `/household/contacts`, `/household/members` → `/people`;
`/household/audit`, `/sos` → `/activity`; `/privacy`, `/privacy/erase`,
`/privacy/export` → `/settings/privacy`; `/account/password`,
`/account/sessions` → `/account`; `/customers`, `/devices`, `/operators`,
`/audit` → `/admin/...`; `/admin/customers/:id/letter` → the customer;
`/admin/devices/:id/label` → `/admin/devices`. Anything else shows "This page
doesn't exist".

### Device app (`sentrix/web`)

The Python app answers its JSON API (`/api/...`, sign-in by
`POST /api/login`, `GET /api/session`), `/video`, `/ws/threat` and `/media/...`;
every other GET gets the app's `index.html` (`sentrix/web/spa_routes.py`,
registered last). A GET under `api`, `ws`, `video`, `media` or `static` that
nothing claims, any write nothing claims, and any tunnelled request for an app
path are a JSON 404.

| Path | Who | Screen |
|---|---|---|
| `/` | signed in | wall view on the device screen; an open SOS request takes it over with the alert screen and its PIN pad, which also offers a **Device admin** link to the device admin on the LAN |
| `/login` | anyone | sign-in with the device password (`POST /api/login`) |
| `/setup/wifi` | anyone on the device's own setup network, with the setup code from the label | Wi-Fi setup mode |
| `/admin/diagnostics` | device admin | live picture with overlays, fusion, level changes, engine scores, scene classifier, pipeline rates (the old dashboard) |
| `/admin/activity` | device admin | incidents with verdicts, snapshots, sealed evidence, dispatch packages |
| `/admin/sos` | device admin | help requests; in test mode the demo controls (close the incident, the disclosed scripted trigger) |
| `/admin/recordings` | device admin | Record video: start, stop, download, delete (D20) |
| `/admin/faces` | device admin | face gallery: capture from the camera, upload, remove |
| `/admin/hardware` | device admin | this unit, camera and microphone, LEDs and sensors, services |
| `/admin/network` | device admin | Portal link, alert channels (test message), who watched through the Portal |

Old device paths redirect: `/dashboard`, `/live` → `/`; `/events`, `/alerts`,
`/evidence`, `/dispatch` → `/admin/activity`; `/sos` → `/admin/sos`; `/node` →
`/admin/hardware`; `/authorized` → `/admin/faces`; any unknown path → `/`.
