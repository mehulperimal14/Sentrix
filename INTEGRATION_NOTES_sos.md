# Integration notes — track T-SOS (device SOS engine)

Branch `wip/sos`, based on the foundation (`sentrix/portal`, "fix(foundation):
contract review"). Everything below is either a change another owner has to
make in a file T-SOS does not own, or a place where the engine answers the
contract differently than docs/7 reads today (with the reason). Nothing in a
shared file was edited.

## What T-SOS delivers (M10 status)

| Piece | File | State |
|---|---|---|
| Pure machine (eligibility, timeline, safety defaults, answers, duress, help path, cloud facts, reconnect merge) | `sentrix/sos/machine.py` | done |
| Frozen policy, rules, reach | `sentrix/sos/policy.py` | done |
| English templates (full, short SMS/Telegram with `{link}`, offline owner prompt, reminders, lapse, lapsed, resolved, duress, failed, (un)acknowledged), sandboxed Jinja, GSM-7 fitting, preview | `sentrix/sos/templates.py` | done (the Portal can import it: stdlib + Jinja2, no config read) |
| PIN check and the shared 5 / 10 min limiter | `sentrix/sos/pins.py` | done |
| Sweeper thread | `sentrix/sos/sweeper.py` | done |
| Engine = the `sos` service (`build_service`, `preflight`) | `sentrix/sos/service.py` | done |
| Request store additions | `sentrix/storage/requests.py` | done (frozen names keep their parameters; see below) |
| One profile-filled package per request | `sentrix/alerting/dispatch.py` | done |
| LC JSON API | `sentrix/web/sos_routes.py` | done |
| Tests | `tests/test_sos_{machine,engine,templates,pins,routes,replay}.py` | 201 new tests; full suite 540 passed, 1 skipped (the live-log replay without the private log) |

Replay of the first live session (`c27258a25457`, 1300 ticks) through the
current decision layer and the engine (`tests/test_sos_replay.py`, run with
`SENTRIX_LIVE_SESSION_LOG=<path>` since `var/` is not in git): **home mode 0
L3+ onsets, 0 requests**; armed mode 9 L3 onsets (the M1 figure), 6 held for
the entry delay, **1 request** (the other intrusions ended within 30 s).

## Changes needed in files T-SOS does not own

### docs/7 (foundation / integration)

1. **§7.1 / §7.2 — armed intrusion arrives through `on_level`, not `trigger`.**
   `wants("intrusion", level)` is False. The engine holds an intrusion when it
   sees the committed L3 change of an `intrusion` incident in armed mode
   (`LevelChange`), waits `intrusion_entry_delay_seconds`, then checks the
   authorised face / the mode and opens the request. Reason: the pipeline
   claims `"sos"` once per incident, so if intrusion were claimed, a weapon
   appearing in the same incident during the entry delay would never
   trigger, and after a face or PIN disarm the incident would stay claimed
   with no request. The hook in `engine.py` needs no change. `trigger()`
   still accepts an `intrusion` trigger (it is held the same way).
2. **§7.8 — silent requests (`flags.silent`).** The duress PIN at a disarm
   with nothing open, or on an already closed request, opens a request the
   household never saw. It has a restricted `created` event, owner views
   return nothing (`GET /api/sos/requests/{id}` answers 404 to owners), the
   LC block ignores it, and **no incident row is written** (the contract says
   "a synthetic duress incident": its row would show on the household's
   Events page, so the request carries `incident_type: "duress"` and the live
   incident if one is open, else none). Incidents are not re-linked to a
   silent request.
3. **§7.8 — the label reads FALSE on a duress decline too**, not only on the
   verdict path, so the Events page of a duress "decline" matches a real one.
   `set_verdict` leaves the help alone (the request is flagged duress). The
   Desk's resolve outcome overwrites it later (last writer wins, docs/7 §7.7).
   **M8 dataset builders must drop or relabel incidents of requests whose
   chain has a restricted `approve` with `detail.duress`.**
4. **§7.5 — owner SMS rule as implemented:** T0 when no remote prompt is
   possible (link down, or a standalone device without owner Telegram/e-mail);
   every reminder from T+10 min while the prompt is unseen (or every reminder
   while offline); `rise4` / `rise5` always (the legacy L4 SMS and, as a
   `modem_call`, the L5 call fold in there). Contacts and the Desk get modem
   SMS from the device only while the link is down or none is configured;
   online, the Portal pages them from the `sos.*` events.
5. **§7.5 — repeat throttle:** at most one re-prompt per `repeat_window` after
   a decline of the same incident type; a corroborated L4/L5 is never
   suppressed (stronger evidence than what was declined). A suppressed
   incident that later rises to L4/L5 opens then.
6. **§7.3 / §5.6 — `demo.close_incident` also resets the request** (test
   mode): prompting -> CANCELLED (`reason: test_reset`), DISPATCHED /
   ACKNOWLEDGED -> CLOSED (`no_action_needed`, no verdict). APPROVED and
   FAILED are kept (only a delivery or the Desk moves them on). Without this
   the next scripted scene would attach to the open request.
7. **§7.3 — APPROVED / FAILED never close by themselves** (the contract has no
   way out but `delivered`, `acknowledge`, `retry`). A standalone device in
   dry run therefore keeps a help request open (FAILED, re-paged up to the
   cap, then `unacknowledged`) until a Desk or a contact resolves it; new
   incidents attach to it. That is honest ("SOS NOT DELIVERED - call 112
   now") but means a rehearsal needs the Portal Desk to close it.
8. **§7.4 / §6.1 — store additions** in `storage/requests.py` (T-SOS owns it):
   optional keyword-only `at` on `create_request` / `try_transition` /
   `transition` (the engine's clock), `create_request(..., snapshot_path,
   evidence_bundle, evidence_hash, urgency, peak_level, restricted)`,
   `force_transition` (the reconnect merge's `conflict` jump, device chain),
   `open_requests()`, `requests_for_incident()`. `request_dict` gained
   `repage_count` (so it is in `RequestPayload` too).
9. **§9.2** — extra route `POST /api/sos/demo/trigger {incident_type}` (test
   mode only, the LC twin of `demo.trigger`); `GET /api/sos` returns
   `{current, recent, status}` (status = the `state.sos` block).
10. **§9.4 — `state.sos` extra keys:** `flags` (owner-visible flags:
    `repeat`, `undelivered`, `resident_safe`, `unacknowledged`, `simulated`,
    `camera_degraded`), `call_number` (112, or the Desk number in test mode),
    `test_mode`, and `last.actions` (`["help_now"]` for EXPIRED / DECLINED /
    CANCELLED). The legacy `dispatch_package` key is written by the engine
    (owner view of the open request's package) while SOS is on.
11. **§8.2 — message templates** used by the engine besides the listed ones:
    `sos.failed`, `sos.unacknowledged`, `sos.acknowledged`. Step names beyond
    the fixed list (each unique per request): `failed<n>`, `undelivered`,
    `unacknowledged`, `acknowledged`, `help_now<n>`. Please add them to the
    `Message` docstring in `sentrix/alerting/channels/base.py`.

### `.env.example` (foundation)

```
# Standalone device (no Portal): the owner's Telegram chat id / e-mail for
# SOS prompts (still behind both send switches and the test allowlist).
SENTRIX_OWNER_TELEGRAM=
SENTRIX_OWNER_EMAIL=
# Tests only: replay the first live session's log from another checkout.
# SENTRIX_LIVE_SESSION_LOG=G:/Sentrix/var/events/events_20260930_232502_c27258a25457.jsonl
```

### T-WEB

* The SOS prompt/banner and `/sos` read `state.sos` and post to `/api/sos/*`
  with `X-CSRF-Token` (the routes check it themselves for LC sessions, the
  same `auth.csrf_for_session` token the hardening will check); answers are
  `{ok, message, request}`, errors `{detail, code}`; `pin_locked` carries
  `Retry-After`. Button labels: `sentrix.sos.templates.BUTTONS`; "Call 112
  now" dials `state.sos.call_number` (never `tel:112` in test mode).
* `/dispatch` and `GET /api/dispatch/latest` must show packages through
  `sentrix.alerting.dispatch.owner_view(package)`: a duress request's package
  must look CANCELLED.
* The Events page should link incidents with a `request_id` to the request;
  for owners an unknown or silent request answers 404, so render the link
  only when `/api/sos/requests/{id}` answers.
* HOME/ARMED: with SOS on, disarming needs the PIN for every role (a
  standalone device's PIN prompt accepts the console password when
  `SENTRIX_SOS_PIN_HASH` is unset); the toggle must send `pin`.

### T-LINK

* `link/commands.py`: `sos.respond` -> `respond`, `sos.update` -> `cloud_event`
  (actor cloud / operator / contact), `sos.sync` -> `sync`, `verdict.set`
  FALSE -> `false_verdict`, `mode.set` -> `set_mode`, `demo.*` ->
  `demo_trigger` / `demo_close_incident`. Results are `SosResult`s in the
  actor's view (cloud: full; operator: Desk view; owner/member: owner view).
* The engine decides "online" from `state.link.connected` (and
  `settings.link.enabled`); keep that block current.
* `sos.update notification` should carry `recipient_ref` (`"owner"` for the
  owner) and `status` (`SENT` / `DELIVERED` / ...): an owner message
  delivered by the Portal clears the device's `undelivered` warning; send
  `seen` when the owner opens the prompt.
* Portal mirror: apply `states.owner_view`, hide `restricted` rows, treat
  `flags.silent` requests as non-existent for owners and members, and write
  the cloud's own events on a duress request as `restricted`. Accept
  `incident_type: "duress"` in the ingest class allowlist.
* `sos.snapshot` is published once per request when the frozen policy has
  consent `sos_snapshot` and the link is enabled (<= 640 px, <= 256 KiB).

### T-CH

* The engine registers `ChannelsService.add_listener` and expects
  `OutboxItem`s with `status`, `attempts`, `error`, `recipient` and
  `message.request_id` / `message.step` (as `ChannelRouter._notify` sends).
  A SENT/DELIVERED modem SMS of the help path while the link is down is what
  moves APPROVED -> DISPATCHED on a device without the Portal.
* `modem_call` receives the `rise5` owner call (template `sos.reminder`).
* SOS texts arrive already starting with the test banner (GSM-7 variant on
  SMS channels) and fitted to two GSM segments (three for UCS-2);
  `guard.prepare_text` does not add a second banner.

## Remaining (needs hardware, accounts or another track)

* No real transport was exercised: every message in the tests reaches
  `FakeTransport` behind the real send guard (T-CH owns the transports).
* Inbound SMS ("Reply 1") is Tier 2; the offline owner SMS therefore points
  to the console or the Portal and never offers a decline.
* Liveness for the authorised-face disarm is out of scope (a photograph of a
  resident could clear the entry delay).
* PIN failure counts live in memory (a restart clears them).
