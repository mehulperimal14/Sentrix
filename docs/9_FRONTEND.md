# 9. The SENTRIX frontend (v2)

Status: in progress on branch `sentrix/frontend-v2` (decision D22 in CLAUDE.md).
Design source: the SENTRIX Experience Plan and the platform prototype (private
artifacts linked from CLAUDE.md). This file is the contract between the two
React apps and the two Python apps.

## 1. What changes and what does not

* The server-rendered Jinja pages of the Portal (`sentrix_portal/templates`,
  `sentrix_portal/static`) and of the device console (`sentrix/web/templates`,
  `sentrix/web/static`) are replaced by two single-page apps built from
  `frontend/`.
* **No data changes.** No table, column, file format, retention rule or
  directory under `var/` (device) or the Portal data dir changes meaning. New
  storage is additive only (the existing guarded `ALTER TABLE ADD COLUMN`
  pattern). The SOS engine, link protocol, tunnel, channels, guard, PIN rules,
  rate limits, roles and audit chain stay as they are: the new JSON endpoints
  call the same service functions the old HTML routes called.
* Links already in the world keep working: `/activate/{code}`, `/claim?sn=`,
  `/consent/{token}`, `/c/{token}`, `/a/{token}`, `/sos/{id}`, `/desk`,
  `/admin/...`, the device's `/dashboard`, `/setup/wifi`. The SPA routes them.

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
`sentrix/web/static/app/`. `npm run build` regenerates both; a Python test
fails when `index.html` is missing or points at an asset that is not there.

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
  Activity, Record video, Hardware, Faces, Network, Wi-Fi setup mode). Device
  admin pages stay LAN-only (path table `LC_ONLY_PREFIXES`).

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

* CSP stays strict: `script-src 'self'`, `style-src 'self'`, `connect-src 'self'`
  (plus `ws:`/`wss:` self), `img-src 'self' data: blob:`, no inline script, no
  third-party origin. React sets styles through the CSSOM, which CSP allows.
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
