# T-ACCT hand-off notes (branch `wip/acct`)

Track T-ACCT of M10 (docs/7 §2.1): Portal accounts and onboarding. Everything
below lives in files T-ACCT owns (docs/7 §2.3); nothing in a §2.2 shared file
was edited. Where a shared file needs a change, it is listed in section 2 and
the code works without it.

## 1. What is implemented

| Area | Where | Notes |
|---|---|---|
| Sign-in / sign-out | `routes/auth.py`, `security.py` | Username + password (Argon2id); `portal_csrf` double-submit before login; POST + CSRF logout; a stale session just goes to `/login`; `?next=` only same-site paths (`security.safe_next`) |
| Back-off, never a lockout | `security.backoff_remaining` / `record_attempt` | Per (key, address): 4 free failures, then 30 s doubling to 15 min; per address across keys: 20 free. Keys in `login_attempts.username`: the lower-cased username, `activate:<username>` / `activate:link`, `stepup:<user id>`, `totp:<user id>`, `phone:<user id>`. Another address (the owner's phone) is never affected; a blocked attempt is not checked, recorded or audited |
| Activation (D14) | `services/accounts.py` (`issue_activation_code`, `activation_for_typed/_link`, `complete_activation`), `/activate`, `/activate/{code}` | 12 characters of the RFC 8628 alphabet (~52 bits), HMAC digest only, 72 h, single use, one valid code per account, burned after 10 wrong codes, back-off per username and per address. The customer chooses the password; activation signs them in and sends an e-mail verification link |
| Admin never sees a credential | `routes/admin.py` | The letter (the only clear-text code) prints only while the owner was never activated, in a `no-store` response; after activation the admin can only send a reset link to the customer's e-mail (`send_activation_email`); the old password keeps working until the link is used. Admin pages never show hashes, codes, media or `/d/` links |
| TOTP | `/account/2fa`, `security.verify_totp`, `new_recovery_codes`, `consume_recovery_code` | Required for company_admin and operator every session (`require_role` already redirects); setup with QR (inline SVG) + key; replay of an accepted step refused; 10 single-use recovery codes (digests), regenerate behind step-up; admin can reset a staff member's TOTP |
| Sessions | `/account/sessions` | Lists your live sessions; revoke one or all others; only your own |
| Step-up | `POST /account/step-up` | Password re-entry → `sessions.step_up_at`; safety settings, PINs, unpair, export, erasure and e-mail change require it |
| E-mail and mobile verification | `/account/verify-email/{token}` (GET confirm page, POST verifies), `POST /account/phone` | Mobile: a 6-digit code sent by the household's own device (`hub.send_command(device, "sms.send", …, queue_if_offline=False)`), 3 per hour, 10 min, bound to user + number |
| Roles | `require_role` everywhere | owner edits; member views (and gets invited by e-mail, never sees an admin); operator → `/desk` (T-LINK); company_admin → `/admin` and nothing household |
| Audit | `security.audit` (unchanged), `prune_audit_before`, `verify_audit_chain` | Retention drops the oldest prefix behind a signed `retention.audit_pruned` boundary row; `verify_audit_chain` then also returns `retention_gaps` (only when there is one, so the old equality tests hold). User-visible: `/household/audit` (own household, restricted/duress rows hidden). Admin: `/admin/audit` with chain status and filters |
| Admin console | `routes/admin.py`, `templates/admin/*` | Customers (create with optional device, list/search, detail, deactivate/reactivate, re-send activation or reset, welcome letter with activation QR and claim QR + optional fresh claim codes), devices (provision → label with claim + setup codes, QR and `identity.json`; reprint = new claim code; assign by customer code; transfer = unpair + new claim code; retire; fleet status from `hub.is_online` and `devices.last_heartbeat`), staff (operators/admins, one-time activation code + e-mail, disable/enable, reset TOTP, re-send) |
| Provisioning CLI | `tools/provision_device.py` | `device` (default): UUID4 id, serial, claim code, bootstrap secret, setup code; writes `<out>/identity.json` (0600, atomic, refuses to overwrite without `--force`, warns when inside `var/`); `--label` HTML with QR; `--assign SX-0007`. `admin --username …`: first company_admin + activation code |
| Home | `routes/home.py` | `/` routes by role; `/home` cards: online, HOME/ARMED, AI rate, last seen, last incident (mirror), open SOS (`services.sos.open_requests`), links to `/d/{id}/dashboard|live|events`; onboarding and erasure banners |
| Onboarding wizard | `routes/onboarding.py`, `templates/onboarding/*` | 7 steps per design §4.1 as amended: E.164 +91 (mobile 6-9), adult attestation, address with landmark, PIN code `^[1-8]\d{5}$`, state list, India-bounded map pin + "use my location", entry note refuses door codes, nearest services; occupants, members (minors: no account, no faces), medical notes behind a separate consent; 1-3 contacts (not the owner's number, unique priority, unreachable-offline warning); claim by code or QR (`/claim?sn=`, the code rides in the URL fragment), name, room label, default mode (ARMED → `mode.set`), "I have checked the view" with the tunnelled live view; window 30/45/60, safety defaults on (off needs an acknowledgement), channels, Telegram link, 6-digit SOS PIN with weak-PIN refusal, duress PIN ≥ 2 digits different (both PINs always entered together); notice, per-purpose consents and the SOS preview rendered from the household's own profile |
| Claim | `accounts.claim_device` | Device must be ASSIGNED to the user's household; 5 failures / 15 min per account and per serial (`claim_attempts`); claim code single use; `hub.issue_token` after the commit, then `push_profile` |
| Household | `routes/household.py` | Profile/address edit (owner), members (add/invite, edit, remove → account closed), contacts (add, edit → new number/e-mail needs re-confirmation, remove, new link), activity log |
| Contact consent | `routes/consent.py`, `accounts.decide_contact_consent` | Link by e-mail (when given) and shown once to the owner to share; GET never changes state (counts opens); POST + double-submit CSRF; confirm → CONFIRMED + consent row + profile push + a year-long opt-out link + Telegram deep link (when the bot is configured); opt out → DECLINED/REVOKED, phone/e-mail/Telegram erased at once, row deleted by retention after 7 days. Only CONFIRMED contacts are in the profile |
| Settings | `routes/settings.py` | Window + safety defaults (step-up), channels (no step-up), PINs (step-up), Telegram link code / unlink, device name + room label, unpair (step-up; device stays ASSIGNED and needs a new claim code) |
| Privacy | `routes/privacy.py`, `templates/privacy/*` | Standalone notice with version (DPDP Rule 3 items, India storage + processors sentence, retention caps, rights, grievance contact, Board), per-purpose toggles with history (withdrawing medical notes erases them; account_sos only by closing), JSON export (step-up; no secrets; duress requests in owner view), erasure scheduled with a 7-day cancellable grace period |
| Profile JSON | `accounts.build_profile` | Exactly the §5.11 shape (test pins every key), validated with `sentrix.link.profile.validate` before returning; `preview_profile` for the wizard |
| Retention worker | `services/retention.py` | `prune(db, now)` (caps of `settings.retention`; erasure of due households; housekeeping) and `start(app)` (first pass after 60 s, then daily; claimed devices of due households are unpaired over the link first) |
| Hardening | `hardening.py` | Adds `Permissions-Policy` (camera/microphone off, geolocation self), `Cache-Control: no-store` on Portal pages (not `/static`), HSTS when `PORTAL_SECURE_COOKIES=1`; still nothing on `/d/…` but `nosniff` and `Referrer-Policy` |
| Test-mode banner | `base.html` (foundation) | Every T-ACCT page extends it through `templates/acct/layout.html`; a test renders every page for every role and checks the banner, the nonce scripts, no inline script/style and a CSRF token in every POST form |

Tests: `tests/test_acct_*.py` (8 files, ~140 tests: pages, auth, admin + CLI,
onboarding, household/settings/privacy/profile/retention, isolation + CSRF).
`test_acct_support.py` holds the helpers (recording `notify.enqueue`, a fake
hub, a CSRF-aware browser). Nothing is sent and no device is reached.

## 2. Changes needed in shared files (not made)

1. **`tests/test_foundation_contract.py`** (`TestPortalServiceStubs.test_signatures_and_safe_defaults`):
   it still asserts the stub behaviour `pytest.raises(NotImplementedError): accounts.build_profile(None, "h", "d")`.
   Replace it with a real call, e.g. `pytest.raises(LookupError): accounts.build_profile(db, home.id, "no-such-device")`,
   then delete the `if db is None: raise NotImplementedError` guard at the top of
   `sentrix_portal/services/accounts.build_profile` (it exists only to keep that assertion green).
2. **`sentrix_portal/templates/base.html`**: make `<span class="nav-user">{{ principal.display_name }}</span>`
   a link to `/account` (the account, sessions, password and 2FA pages have no nav entry; today they are reached from
   `/home` and the settings/privacy pages).
3. **`sentrix_portal/models.py`** (comments/vocabulary only, no schema change needed):
   * `TOKEN_PURPOSES` += `"phone_verify"` (mobile codes, `accounts.mint_phone_code`);
   * `Household.status`: `active | disabled | erasing | closed` (`disabled` = admin deactivation, `erasing` = erasure
     scheduled, `closed_at` = when it was requested);
   * a contact's opt-out is a `consents` row with `subject_type="contact"`, `granted_at NULL`, `withdrawn_at` = the
     decision time (retention counts the 7 days from it). Optional schema improvement: `emergency_contacts.decided_at`.
4. **`sentrix/alerting/channels/base.py`** `Message` docstring and docs/7 §8.2 template list: add
   `account.verify_email` and `account.security` (password changed; new sign-in after a staff action). Steps used by
   T-ACCT: `activation:<code id>`, `verify:<token id>`, `consent:<token id>`, `password:<unix s>`,
   `newip:<audit id>:<ip digest>`.
5. **docs/7 §11** endpoint table, T-ACCT rows: add `POST /account/step-up`, `POST /account/phone`,
   `POST /account/verify-email` (re-send), `/household/audit`, `/privacy/notice`, `POST /privacy/consents`,
   `POST /admin/customers/{id}/resend|status|assign`, `POST /admin/devices` (provision),
   `POST /admin/devices/{id}/assign|transfer|retire`, `POST /admin/operators/{id}`. The unauthenticated list is
   unchanged (`/privacy/notice` needs a session; contacts read their notice on `/consent/{token}`).
6. **docs/7 §13** Portal audit events added by T-ACCT: `auth.activation_failed`, `auth.totp_failed`,
   `auth.recovery_code_used`, `auth.recovery_codes_regenerated`, `auth.step_up_failed`, `auth.email_verified`,
   `account.updated`, `account.phone_code_sent`, `account.phone_verified`, `admin.staff_created`,
   `admin.staff_<disable|enable|reset_2fa|resend>`, `admin.customer_deactivated`, `admin.customer_reactivated`,
   `admin.letter_printed`, `admin.claim_code_reissued`, `admin.device_retired`, `device.unpaired_by_owner`,
   `contact.updated`, `contact.removed`, `member.added`, `member.removed`, `profile.pins_changed`,
   `onboarding.completed`, `telegram.unlinked`, `privacy.erasure_cancelled`, `privacy.erased`,
   `retention.audit_pruned`.
7. **`.env.example` / `PortalSettings`**: `PORTAL_GRIEVANCE_CONTACT` (the team's grievance address on the notice;
   read by `accounts.grievance_contact()` at render time, a settings field would be cleaner).
8. **docs/7 §3.5 / §10**: record the back-off numbers of section 1.

## 3. Contracts T-ACCT codes against (other tracks)

* **T-LINK `services/hub.py`**
  * `push_profile(db, device_id)` builds with `accounts.build_profile`; T-ACCT calls it (through
    `accounts.push_profile(db, household_id)`) after every committed profile change, for every CLAIMED device.
  * `issue_token(db, device_id)` right after a claim commits.
  * `unpair(db, device_id, actor)`: **the caller sets the device row afterwards** — owner unpair → `ASSIGNED`,
    token/claim hashes cleared, cameras deleted; admin transfer → `UNASSIGNED` with a new claim code; retire →
    `RETIRED`; erasure → `UNASSIGNED` without a claim code. The hub should revoke the token and close 4003 but not
    rewrite `state`. Admin/erasure unpairs use `Actor("cloud", id=<admin id>, name="SENTRIX admin")` /
    `Actor.system()`: the device's own rule (owner + step-up) may refuse the command, and the 4003 close makes it
    wipe anyway.
  * `send_command(device_id, "mode.set", {"armed": True}, actor)` (wizard step 5, ARMED default) and
    `send_command(device_id, "sms.send", {"to", "text", "request_id": None}, actor, queue_if_offline=False)`
    (mobile verification; the device only texts profile numbers, through its guard).
  * `is_online(device_id)`; the cards also read `devices.last_heartbeat["ai_hz"|"armed"]`, `last_seen_at`,
    `firmware`, `profile_version_acked` (the hub keeps them current).
* **T-LINK `services/sos.py`**: `/home` reads `open_requests(db, household_id)` dicts by `id`, `device_id`,
  `state`, `label`, `incident_type`, `expires_at` (ISO string) and links to `/sos/{id}`.
  Audit rows that must stay hidden from owners (duress) need `detail.restricted = true` or `detail.duress = true`:
  `/household/audit` and the export drop them. The export applies `states.owner_view` to requests, drops
  restricted events, and for a duress request drops every help-path event, its packages and its notifications.
* **T-CH**: `notify.enqueue(db, recipient, message)` for `account.activation`, `account.reset`,
  `account.verify_email`, `account.security`, `contact.consent` (e-mail; `Recipient.ref` `user:<id>` /
  `contact:<id>`); a `None` result is shown to the admin as "could not be queued". Telegram `/start <code>`:
  find the `action_tokens` row by `security.token_digest(code)` (purpose `telegram_link`, unused, unrevoked,
  unexpired), create `TelegramLink(subject_type, subject_id, household_id, chat_id)` (private chat), set `used_at`,
  audit `telegram.linked`, then `accounts.bump_profile_version(db, household_id)` and `hub.push_profile` (the
  profile's `contacts[].telegram` comes from the link). Contacts link the same way (`subject_type="contact"`).
* **T-SOS `sentrix/sos/templates.py`**: if it exports `render_preview(profile, *, incident_type) -> {"full", "sms"}`,
  the wizard and settings preview use it automatically (`accounts.render_sos_preview`); until then the Portal
  renders the same fixed wording itself (single-source wording, test banner, "SENTRIX does not contact 112 itself.").
* **T-WEB `/setup/wifi`**: `identity.json["setup_code_digest"] == sha256(normalize(typed)).hexdigest()` with
  normalize = upper-case and keep only `A-Z0-9` (the Portal's `security.normalize_code`); setup codes are 8 letters
  of `BCDFGHJKLMNPQRSTVWXZ` printed `XXXX-XXXX`.

## 4. What remains, and why

* **Owner TOTP** (Tier 2): not offered; `require_role` enforces TOTP for company_admin and operator only.
* **Hindi messages** (Tier 2): English only (`LANGUAGES`).
* **Real delivery** of activation/reset/verification/consent e-mails and the mobile SMS needs T-CH (transports,
  outbox worker) and a device with a modem (T-HW/T-CH); here they go to `notify.enqueue` / `hub.send_command`,
  faked in tests. With the foundation stubs in place they return `None` / `device_offline`, which the pages report.
* **Armed schedule** (design step 5, optional): not built; the default mode is set once.
* **Map**: latitude/longitude fields with the browser's geolocation; no map tiles or geocoding (the Portal makes no
  external requests). Nearest stations are typed, not suggested.
* **Alembic**: none needed (no schema change); integration owns migrations.
* `CLAUDE.md` M10 status: for integration to record.
