# T-CH hand-off: channel router, outbox, Telegram, e-mail, modem, Twilio

Branch `wip/ch` (from `sentrix/portal`). Only T-CH files were edited (docs/7
§2.3); the items under "Requested changes" are for the integration owner.

## M10 status (for CLAUDE.md)

- [x] **T-CH channels**: `Router` (completes `base.ChannelRouter`: SENDING
  claim + crash recovery, 30 s / 90 s back-off then ×3 growth, FAILED retried
  every 120 s until 24 h old, non-retryable errors final, DELIVERED on send
  (Telegram) or on a modem status report, per-(template, recipient, channel)
  cooldowns for `legacy.l4_sms` 300 s / `legacy.l5_call` 600 s, `send_now`,
  `cancel`, provider errors scrubbed of numbers/e-mails); device `channels`
  service (`router.build_service`, off unless `SENTRIX_CHANNELS_ENABLED=1`;
  worker thread; `state.channels` block; `preflight`); SQLite outbox
  extensions; modem (PDU-mode GSM-7/UCS-2 concatenated SMS with status
  reports, text-mode fallback, CSQ/CREG/CEREG/CPIN/COPS, ringing call,
  `+CDS`/`+CMTI` URCs, CMGL/CMGD read API for Tier 2, probe + reconnect,
  ModemManager `mmcli` backend, 30 segments/day cap, profile numbers only);
  Telegram (sendMessage + inline buttons, URL-button fallback, token masked
  in errors and httpx logs, sync + async clients, update parser, deep links,
  webhook secret check); e-mail over Brevo / Resend HTTPS (plain + HTML,
  banner in subject and HTML, Resend idempotency key); Twilio SMS/voice
  transports (lazy client); legacy notifier routed through the router;
  `/api/channels`, `/api/channels/test`; Portal outbox/worker/polling/webhook
  registration, `/start` linking, button callbacks -> `services.sos.respond`,
  `POST /hooks/telegram`. Tests: `tests/test_ch_*.py` (199); full suite 539 passed.

## What other tracks call

### Device (`registry.get("channels")`, a `DeviceChannels`)

Contract (`ChannelsService`): `enqueue(recipient, message) -> id | None`,
`ready(channel)`, `status()` (the `state.channels` block), `add_listener(fn)`
/ `remove_listener`, `start` / `stop` / `health`.

Extras (additive, safe to ignore):

| Call | Use |
|---|---|
| `cancel(request_id, reason="request closed", steps=None) -> int` | T-SOS: when a request terminates (approved, declined, expired, cancelled), withdraw its queued / retrying reminders so a FAILED `r120` is not retried for 24 h. They become SUPPRESSED, `error="cancelled: <reason>"`; listeners hear it. |
| `send_now(recipient, message) -> OutboxItem | None` | Deliver at once on the caller's thread and return the outcome (used by `notifier.send_raw` / dispatch "Send now" and `/api/channels/test`). |
| `recent(limit=20, request_id=None) -> [dict]` | Masked rows for UIs (the LC SOS page's per-request delivery list). |
| `channels_for("sms" | "call") -> [name]` | Registered phone channels, ready first, modem before Twilio. |
| `owner_target() -> (name, phone) | None` | Profile owner phone, else `ALERT_PHONE_NUMBER`. |
| `router.notification_payload(item)` (module function) | T-LINK: the `notification.status` outbox payload of §5.5 from a listener's `OutboxItem` (masked). |

`status()` keys: `enabled`, `outbound_ready`, `live`, `test_mode`,
`test_recipients` (count), `channels` (per channel: `live`, `available`,
`ready`, `detail`, `sent`, `delivered`, `failed`, `suppressed`,
`last_status`, `last_error`, `last_at`), `outbox_depth`, `outbox` (counts per
status), `last_error`, `modem` (`present`, `registered`, `signal_dbm`,
`sms_today`, `sms_daily_cap`, `roaming`, `operator`, `sim_ready`, `port`,
`backend`, `status_reports`, `inbound_pending`, `error`, `checked_at`, or
None), `legacy_twilio`, `warn`.

- **T-LINK heartbeat**: `modem` = `status()["modem"]` subset `{present,
  registered, signal_dbm, sms_today}`; `channels.outbound_ready` =
  `status()["outbound_ready"]`.
- **T-LINK `sms.send`**: enqueue on `modem_sms`. The cap counts **SMS
  segments** (a 300-character GSM text is 2-3); when it is reached the item
  FAILS with an error starting `daily_cap:` — map that (or check
  `status()["modem"]["sms_today"] >= sms_daily_cap` first) to the `daily_cap`
  429. Numbers outside `link.profile.sms_allowlist()` are SUPPRESSED with an
  error starting `not_allowed_recipient:` (device rule, whatever the Portal
  says).
- **T-SOS**: SUPPRESSED never counts as delivered; a SENT `modem_sms` while
  offline is the device's own `delivered`; DELIVERED arrives later if the
  network sends a status report. `ready("modem_sms")` is False while the modem
  is unplugged, unregistered or capped.
- **T-HW Node page**: the `modem` block above (from `state.get()["channels"]`
  or the service).

### Portal (`sentrix_portal.services.notify`)

Frozen: `router(db)`, `enqueue(db, recipient, message)`, `start(app)`.
Extras: `add_listener(fn)` / `remove_listener` (process-wide, worker
thread), `cancel(db, request_id, reason, steps=None)`, `ready(channel)`,
`status(database=None)`, `handle_update(app, update)`, `worker_router(db)`,
`configure(transports=..., policy_factory=...)` and `reset()` for tests.

- Messages queued with `enqueue(db, ...)` commit with the caller's
  transaction (`INSERT ... ON CONFLICT (dedupe_key) DO NOTHING`; a duplicate
  never rolls the caller back). The worker sends them within ~1 s of the
  commit.
- `household_id` on each row is derived from the request, else the
  `contact:<id>` / `user:<id>` recipient ref.

## Contracts other tracks must follow for the bot

- **T-ACCT `mint_telegram_link`** must store
  `ActionToken(purpose="telegram_link", token_hash=security.token_digest(code),
  subject_type="user" | "contact", subject_id, household_id, expires_at)` and
  return a code of 1-64 characters from `A-Z a-z 0-9 _ -` (e.g.
  `security.new_token(24)`); the link is `telegram.deep_link(bot_name, code)`.
  On `/start <code>` the bot (single use) revokes the subject's other active
  links, adds a `TelegramLink`, sets `emergency_contacts.telegram_chat_id` for
  a contact, marks the token used, audits `telegram.linked`, bumps the
  household's `profile_version` (the profile carries `contacts[].telegram`)
  and calls `hub.push_profile` for its CLAIMED devices after the commit.
- **T-LINK Telegram buttons**: one `ActionToken(purpose="action_link" or
  "contact_page", nonce=channels.base.new_callback_nonce(), request_id,
  subject_type, subject_id, household_id, expires_at=close + 2 h)` per message;
  buttons `callback_data(nonce, "approve" | "checking" | "acknowledge")`;
  "False alarm (PIN)" as a URL button to the PIN page. A press is accepted only
  from a private chat whose sender id is an active `TelegramLink.chat_id` of
  the token's subject (or the contact's `telegram_chat_id`), with the token
  not revoked or expired; `approve` and `acknowledge` use the token up,
  `checking` does not. The bot calls `services.sos.respond(db, request_id,
  action, actor=<owner|member|operator|contact Actor dict>,
  channel="telegram")` and shows a toast from the result; every press on a
  known token is audited as `telegram.callback`. Revoke the token
  (`revoked_at`) on decline.
- Telegram never gets a photo: `TelegramTransport` only calls `sendMessage`
  (plain text, link previews off). Keep address and medical notes out of the
  `sos.*` Telegram text (T-SOS templates / T-LINK).

## New template and step names (for docs/7 §8.2)

Templates: `legacy.dispatch` (dispatch "Send now" through the router),
`telegram.linked`, `telegram.link_failed`, `telegram.help`. Steps: `l4:<unix
ms>`, `l5:<unix ms>`, `dispatch:<unix ms>`, `test:<unix ms>`,
`tglink:<token id>`, `tgbad:<update id>`, `tghelp:<update id>`.

## Requested changes in shared files (not made)

1. `docs/7_PORTAL_AND_SOS.md` §8.2 — document the extras above, the
   transport conventions the router reads (`transport.state() -> (available,
   detail)` for readiness, `transport.delivers_on_send = True` for
   DELIVERED-on-success), the segment-based daily cap, and that
   `SENTRIX_MODEM_PORT` also accepts `mmcli` / `mmcli:<index>`.
   §3.3 — the Portal reads `SENTRIX_TELEGRAM_MODE`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_WEBHOOK_SECRET`, `SENTRIX_EMAIL_PROVIDER`, `BREVO_API_KEY` /
   `RESEND_API_KEY` and `SENTRIX_EMAIL_FROM` from its environment at use time
   (the webhook is registered at `<PORTAL_BASE_URL>/hooks/telegram`, https
   only). Reason: the contract lists them for the device only.
2. `sentrix_portal/settings.py` — optional `telegram_mode` field
   (`SENTRIX_TELEGRAM_MODE`) instead of the env read in `notify.NotifyWorker`.
   Reason: symmetry with `ChannelConfig.telegram_mode`; not required.
3. `.env.example` — under "Channel router + outbox": `SENTRIX_MODEM_PORT=mmcli`
   for ModemManager; note that Telegram/e-mail on the device are sent only
   without a Portal link (standalone).
4. `tests/test_foundation_contract.py::TestPortalServiceStubs` asserts the
   stub's `notify.enqueue(None, None, None) is None`; still true (argument
   guard), but the test can now be replaced by the T-CH tests.
5. `sentrix/alerting/channels/base.py` — optional: a `retry_after` field on
   `SendResult` (Telegram 429 `retry_after`), and the two transport
   conventions above in the `Transport` docstring. Not needed today: the
   30 s first back-off already exceeds Telegram's usual `retry_after`.

## What remains (needs hardware or accounts)

- **Real modem**: the AT driver is tested against a scripted fake port
  (SIM7600/EC25 replies). On hardware check: PDU mode accepted, `AT+CNMI`
  ds=1 routing of `+CDS` (some modules need `AT+CSMS=1`), IMS registration
  for Jio (VoLTE-only), the AT port number (ttyUSB2 on SIM7600 / EC25), and
  that `ATD<number>;` returns OK before the call is answered.
- **Real Telegram bot / Brevo / Resend accounts**: payloads follow the
  providers' documented APIs and are tested with `httpx.MockTransport`; run
  one rehearsal message per channel to team accounts (allowlisted, both
  switches) before the demo.
- **Inbound SMS parsing** (Tier 2): `ModemWorker.read_inbound()` /
  `delete_inbound()` and the `+CMTI` counter exist; nothing acts on replies.
- **Device-side Telegram callbacks in standalone mode** are not handled (no
  link-token store on the device): standalone Telegram is outbound text only.
  With a Portal, every button goes through the Portal bot.
- **E-mail delivery receipts** (Brevo / Resend webhooks) are not wired: e-mail
  stays SENT (provider accepted).
