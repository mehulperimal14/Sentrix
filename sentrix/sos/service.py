"""The device SOS engine: the real ``SosService`` (docs/7 §7; decisions D8, D9).

``build_service`` registers it as the ``sos`` service when
``SENTRIX_SOS_ENABLED=1``; the app lifespan then hands it to the pipeline
(``pipeline.sos``). Replays, benchmarks and the evaluation harness keep
``NullSosService`` (``services.assert_measurement_safe``), so nothing in a
measurement can open a request.

How the parts fit:

* :mod:`sentrix.sos.machine` decides (pure); this module carries out: it
  reads the request and its audit chain, asks the machine, writes each step
  through ``storage.requests`` (hash-chained ``request_events``), and runs
  the effects: messages through the ``channels`` service, the request's
  dispatch package, verdicts, the LC ``state.sos`` block, JSONL audit rows
  and the ``sos.*`` link events for the Portal.
* The pipeline hook (``trigger``, ``on_level``) and every listener only
  enqueue; the :class:`~sentrix.sos.sweeper.Sweeper` thread drains the queue
  at once and runs the timers (``machine.due``) on time.
* People and the cloud (``respond``, ``cloud_event``, ``sync``,
  ``false_verdict``, ``set_mode``, ``approve_package``, ``demo_*``) are
  served synchronously on the caller's thread (web requests through
  ``asyncio.to_thread``, the link worker), under ``REQUEST_LOCK``; the PIN is
  verified before the lock is taken.

Who the device itself sends to (docs/7 §7.11, §8.3): the LC prompt always;
the owner's modem SMS when no remote prompt is possible or when the prompt is
unseen at T+10 min (and the legacy L4 SMS / L5 call folded into ``rise4`` /
``rise5``); contacts and the Desk by modem SMS only while the Portal link is
down (or none is configured); Telegram and e-mail to the owner only on a
standalone device (``SENTRIX_OWNER_TELEGRAM``, ``SENTRIX_OWNER_EMAIL``).
While the link is up the Portal sends everything else, reacting to the
``sos.*`` events this engine publishes. Every message passes the send guard
inside the channel (test mode, two switches, allowlist).

Duress: the request is approved with the ``duress`` flag and every event from
then on is ``restricted``; owners, members and the LC see it exactly as a
decline (``states.owner_view``), its package as cancelled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from sentrix.actor import Actor
from sentrix.sos import LevelChange, SosResult, SosTrigger, sos_result
from sentrix.sos import machine as M
from sentrix.sos import pins
from sentrix.sos import states as S
from sentrix.sos import templates as T
from sentrix.sos.policy import Reach, Rules, freeze, recommended_authority
from sentrix.utils.logging import get_logger

log = get_logger("sos")

#: Requests kept for the repeat rule and the "last" block.
HISTORY_LIMIT = 20
#: Fields a step may set that hold times (epoch seconds -> UTC datetimes).
_TIME_FIELDS = frozenset({"expires_at", "responded_at", "seen_at"})
#: Steps whose SENT message on the device counts as "delivered" (help path).
_HELP_STEP_PREFIXES = (S.STEP_DESK_T0, S.STEP_CONTACTS_T0, S.STEP_DURESS, "repage",
                       "help_now")
#: SOS snapshot sent to the Portal: at most this wide and this large (docs/7 §7.9).
SNAPSHOT_MAX_WIDTH = 640
SNAPSHOT_MAX_BYTES = 256 * 1024
#: Owner addresses a standalone device (no Portal) may use (read at use time).
ENV_OWNER_TELEGRAM = "SENTRIX_OWNER_TELEGRAM"
ENV_OWNER_EMAIL = "SENTRIX_OWNER_EMAIL"


#: Incidents remembered per structure (triggered, levels, suppressed repeats).
INCIDENT_MEMORY = 512
#: Sweeps an incident -> request link is retried before it is given up.
LINK_RETRIES = 40


def _dt(epoch_seconds: float) -> datetime:
    return datetime.fromtimestamp(float(epoch_seconds), timezone.utc)


def _cloud_outcome(cloud_events: list[dict]) -> str | None:
    """The outcome of the cloud's last RESOLVE event, if any."""
    outcome = None
    for event in cloud_events:
        if event.get("event") == S.RESOLVE:
            detail = event.get("detail") or {}
            if isinstance(detail, dict) and detail.get("outcome") in S.OUTCOMES:
                outcome = detail["outcome"]
    return outcome


def _remember(store: OrderedDict, key: Any, value: Any) -> None:
    """Insert (as newest) and forget the oldest beyond ``INCIDENT_MEMORY``."""
    store[key] = value
    store.move_to_end(key)
    while len(store) > INCIDENT_MEMORY:
        store.popitem(last=False)


@dataclass
class _Hold:
    """An armed intrusion waiting out the entry delay."""

    trigger: SosTrigger
    facts: M.TriggerFacts
    until: float


class SosEngine:
    """The ``sos`` service. Constructor arguments other than ``settings``
    exist for tests and tools: an injectable clock (epoch seconds), the live
    state, and getters for the channels service, the live pipeline, the
    effective profile, the link publisher and the device's reach."""

    name = "sos"
    enabled = True

    def __init__(self, settings: Any = None, *, clock: Callable[[], float] = time.time,
                 state: Any = None, channels: Callable[[], Any] | None = None,
                 pipeline: Callable[[], Any] | None = None,
                 profile: Callable[[], dict] | None = None,
                 publish: Callable[..., Any] | None = None,
                 reach: Callable[[], Reach] | None = None,
                 limiter: pins.PinLimiter | None = None,
                 snapshot_dir: Path | None = None) -> None:
        if settings is None:
            from sentrix.config import settings as default_settings
            settings = default_settings
        if state is None:
            from sentrix.pipeline.state import state as live_state
            state = live_state
        self.settings = settings
        self.state = state
        self._clock = clock
        self._channels_getter = channels or self._registry_channels
        self._pipeline_getter = pipeline or self._live_pipeline
        self._profile_getter = profile or self._effective_profile
        self._publish_fn = publish or self._link_publish
        self._reach_fn = reach
        self.limiter = limiter or pins.LIMITER
        if snapshot_dir is None:
            from sentrix.paths import SNAPSHOT_DIR
            snapshot_dir = SNAPSHOT_DIR
        self.snapshot_dir = Path(snapshot_dir)
        self.rules = Rules.from_settings(settings, test_mode=self._test_mode())

        from sentrix.storage.requests import REQUEST_LOCK
        #: One re-entrant lock for request writes *and* this engine's memory.
        self._lock = REQUEST_LOCK
        self._inbox: deque[tuple[str, Any]] = deque()
        self._held: dict[str, _Hold] = {}
        # Per incident, oldest first and capped (a device runs for months).
        self._suppressed: OrderedDict[str, tuple[SosTrigger, M.TriggerFacts]] = OrderedDict()
        self._triggered: OrderedDict[str, None] = OrderedDict()
        self._levels: OrderedDict[str, int] = OrderedDict()
        self._level, self._level_incident = 1, None
        #: (incident, request) links still to write -> attempts so far.
        self._unlinked: dict[tuple[str, str], int] = {}
        self._published: dict[str, int] = {}
        self._snapshots_sent: set[str] = set()
        self._notified_failures: set[str] = set()
        self._buffered: dict[str, list[dict]] = {}
        self._channels_service = None
        self._started = False
        self.counters = {"triggers": 0, "opened": 0, "attached": 0, "held": 0,
                         "ignored": 0, "suppressed": 0, "face_disarms": 0,
                         "messages": 0, "no_channel": 0, "errors": 0}
        from sentrix.sos.sweeper import Sweeper
        self._sweeper = Sweeper(self.work, settings.sos.sweep_interval_seconds)

    # ── collaborators ─────────────────────────────────────────────────────────
    @staticmethod
    def _registry_channels():
        from sentrix import services
        return services.registry.get("channels")

    @staticmethod
    def _live_pipeline():
        """The running pipeline, never built just for SOS (it loads models)."""
        engine = sys.modules.get("sentrix.pipeline.engine")
        return getattr(engine, "_pipeline", None) if engine else None

    @staticmethod
    def _effective_profile() -> dict:
        from sentrix.link import profile
        return profile.effective()

    @staticmethod
    def _link_publish(kind: str, payload: dict, *, dedupe_key: str | None = None):
        from sentrix import link
        return link.publish(kind, payload, dedupe_key=dedupe_key)

    @staticmethod
    def _test_mode() -> bool:
        from sentrix.alerting import guard
        return guard.test_mode()

    def _now(self) -> float:
        return float(self._clock())

    def _profile(self) -> dict:
        try:
            return self._profile_getter() or {}
        except Exception as exc:
            log.error("SOS: profile unavailable: %s", exc)
            return {}

    def _channels(self):
        try:
            return self._channels_getter()
        except Exception as exc:
            log.error("SOS: channels service unavailable: %s", exc)
            return None

    def _pipeline(self):
        try:
            return self._pipeline_getter()
        except Exception:
            return None

    def _reach(self) -> Reach:
        if self._reach_fn is not None:
            return self._reach_fn()
        link_enabled = bool(self.settings.link.enabled)
        try:
            connected = bool((self.state.get().get("link") or {}).get("connected"))
        except Exception:
            connected = False
        online = link_enabled and connected
        standalone = not link_enabled
        remote = online or (standalone and bool(self._standalone_owner_channels()))
        return Reach(online=online, standalone=standalone, remote_prompt=remote)

    def _standalone_owner_channels(self) -> list[tuple[str, str]]:
        """(channel, address) for the owner on a device without a Portal,
        where the channel could actually leave now."""
        channels = self._channels()
        found = []
        for channel, env in (("telegram", ENV_OWNER_TELEGRAM), ("email", ENV_OWNER_EMAIL)):
            address = os.getenv(env, "").strip()
            if not address:
                continue
            if channel == "telegram" and not address.startswith("tg:"):
                address = f"tg:{address}"
            try:
                if channels is not None and channels.ready(channel):
                    found.append((channel, address))
            except Exception:
                continue
        return found

    # ── pipeline hook (inference thread / FIFO worker: enqueue only) ─────────
    def wants(self, incident_type: str, level: int) -> bool:
        return M.wants(incident_type, level, self.rules)

    def trigger(self, trigger: SosTrigger) -> None:
        self._enqueue("trigger", trigger)

    def on_level(self, change: LevelChange) -> None:
        self._enqueue("level", change)

    def _enqueue(self, kind: str, payload: Any) -> None:
        self._inbox.append((kind, payload))
        self._sweeper.wake()

    # listeners (writer's thread: enqueue only)
    def _on_verdict(self, payload: dict) -> None:
        self._enqueue("verdict", payload)

    def _on_outbox(self, item: Any) -> None:
        self._enqueue("outbox", item)

    def _on_profile(self, _profile: dict) -> None:
        self._enqueue("profile", None)

    # ── the worker ────────────────────────────────────────────────────────────
    def work(self) -> float | None:
        """Drain the queue, release finished entry delays, apply due timers,
        refresh ``state.sos``. Returns seconds until the next timer."""
        self.drain()
        now = self._now()
        self._release_holds(now)
        self.sweep(now)
        self.drain()
        self._retry_links()
        self._refresh_state()
        return self._next_in(self._now())

    def drain(self) -> int:
        handled = 0
        while self._inbox:
            kind, payload = self._inbox.popleft()
            handled += 1
            try:
                with self._lock:
                    {"trigger": self._handle_trigger, "level": self._handle_level,
                     "verdict": self._handle_verdict, "outbox": self._handle_outbox,
                     "profile": lambda _p: None}[kind](payload)
            except Exception as exc:
                self.counters["errors"] += 1
                log.error("SOS: %s handling failed: %s", kind, exc, exc_info=True)
        return handled

    def sweep(self, now: float | None = None) -> int:
        """Apply every due timer of every open request (device authority)."""
        from sentrix.storage import requests
        now = self._now() if now is None else now
        applied = 0
        with self._lock:
            for request in requests.open_requests():
                applied += self._sweep_request(request["id"], now)
        return applied

    def _sweep_request(self, request_id: str, now: float) -> int:
        applied = 0
        for _ in range(12):                      # a few chained steps at most
            snap = self._snapshot(request_id)
            if snap is None:
                break
            steps = M.due(snap, now, self.rules, self._reach())
            if not steps:
                break
            for step in steps:
                result = self._apply(snap, step, now=now)
                if result is None:
                    break
                applied += 1
                snap = self._advance(snap, result)
            else:
                continue
            break
        return applied

    def _next_in(self, now: float) -> float | None:
        from sentrix.storage import requests
        times = [hold.until for hold in self._held.values()]
        try:
            for request in requests.open_requests():
                snap = self._snapshot(request["id"])
                if snap is not None:
                    due = M.next_due(snap, now, self.rules)
                    if due is not None:
                        times.append(due)
        except Exception as exc:
            log.debug("SOS: next timer unknown: %s", exc)
        future = [t - now for t in times if t > now]
        return min(future) if future else None

    # ── reading ───────────────────────────────────────────────────────────────
    def _snapshot(self, request_id: str) -> M.Snapshot | None:
        from sentrix.storage import requests
        request = requests.get_request(request_id)
        if request is None:
            return None
        return M.Snapshot.from_request(request, requests.events(request_id,
                                                                 include_restricted=True))

    @staticmethod
    def _advance(snap: M.Snapshot, request: dict) -> M.Snapshot:
        """The snapshot after one applied step (row fields re-read; the chain
        facts of the step are folded in by the caller's next full read)."""
        return replace(snap, state=request["state"], version=int(request["version"]),
                       flags=dict(request.get("flags") or {}),
                       peak_level=int(request.get("peak_level") or snap.peak_level),
                       urgency=request.get("urgency") or snap.urgency,
                       expires_at=M.epoch(request.get("expires_at")),
                       seen_at=M.epoch(request.get("seen_at")),
                       repage_count=int(request.get("repage_count") or 0),
                       incident_ids=tuple(request.get("incident_ids") or ()),
                       authority=request.get("authority"),
                       responded_at=M.epoch(request.get("responded_at")))

    def _open_snapshot(self) -> M.Snapshot | None:
        from sentrix.storage import requests
        request = requests.open_request()
        return self._snapshot(request["id"]) if request else None

    def _history(self) -> list[M.Snapshot]:
        from sentrix.storage import requests
        return [M.Snapshot.from_request(r) for r in requests.list_requests(HISTORY_LIMIT)]

    # ── writing ───────────────────────────────────────────────────────────────
    def _columns(self, snap: M.Snapshot, step: M.Step) -> dict[str, Any]:
        columns: dict[str, Any] = {}
        for key, value in step.fields.items():
            if key in _TIME_FIELDS:
                columns[key] = _dt(value) if value is not None else None
            elif key == "incident_ids":
                columns["incident_ids_json"] = json.dumps(list(value))
            else:
                columns[key] = value
        if step.flags:
            columns["flags_json"] = json.dumps({**dict(snap.flags), **dict(step.flags)},
                                               sort_keys=True)
        return columns

    def _apply(self, snap: M.Snapshot, step: M.Step, *, now: float,
               actor: Actor | None = None, channel: str | None = None) -> dict | None:
        """Write one step and run its effects. Returns the request after it,
        or None when the write was refused (stale version, not allowed)."""
        from sentrix.storage import requests
        actor = actor or Actor.system()
        restricted = bool(step.restricted or snap.duress or step.flags.get("duress"))
        kwargs = dict(expected_version=snap.version, actor_type=actor.event_actor_type,
                      actor_id=None if actor.type == "system" else actor.ref,
                      channel=channel or "system", detail=dict(step.detail),
                      restricted=restricted, at=_dt(now), **self._columns(snap, step))
        with self._lock:
            if step.forced_state:
                result = requests.force_transition(snap.id, step.forced_state,
                                                   event=step.event, **kwargs)
            else:
                result = requests.try_transition(snap.id, step.event, **kwargs)
            if not result.ok:
                log.warning("SOS %s: %s refused: %s", snap.id[:8], step.event, result.message)
                return None
            request = result.request
            self._audit_event(result.event, request)
            if step.rerender:
                request = self._render_texts(request) or request
            if step.package:
                self._package(request, step.package, actor)
            if step.verdict:
                self._verdicts(request, step.verdict, actor, channel or "system",
                               note=step.detail.get("note") or step.detail.get("outcome"))
            for notice in step.notices:
                self._notify(request, notice, now=now)
            self._publish(request)
            return request

    def _package(self, request: dict, change: str, actor: Actor) -> None:
        from sentrix.alerting.dispatch import dispatch_service
        try:
            sent_to = self._sent_to(request) if change == "sent" else None
            status = dispatch_service.apply_request_change(
                request, change, by=actor.ref if actor.type != "system" else None,
                authority=request.get("authority"), sent_to=sent_to)
            if status == "SENT":
                self._audit("dispatch_sent", request_id=request["id"],
                            package_id=request.get("package_id"),
                            authority=request.get("authority"))
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS: package %s for %s failed: %s", change, request["id"][:8], exc)

    def _sent_to(self, request: dict) -> list[str]:
        """Masked targets of the help request (the package's ``sent_to``)."""
        from sentrix.utils.redact import mask_phone
        policy = request.get("policy") or {}
        targets = ["SENTRIX Desk"]
        targets += [f"{c.get('name') or 'contact'} {mask_phone(c.get('phone'))}".strip()
                    for c in policy.get("contacts") or []]
        return targets

    def _verdicts(self, request: dict, verdict: str, actor: Actor, channel: str,
                  note: str | None = None) -> None:
        from sentrix.storage import database
        for incident_id in request.get("incident_ids") or [request.get("incident_id")]:
            if not incident_id:
                continue
            if database.set_verdict(incident_id, verdict, note, by=actor.ref,
                                    channel=channel, actor_type=actor.event_actor_type):
                self._audit("verdict_set", incident_id=incident_id, verdict=verdict,
                            request_id=request["id"], by=actor.ref)

    # ── triggers and level changes ────────────────────────────────────────────
    def _facts(self, trigger: SosTrigger) -> M.TriggerFacts:
        level = max(int(trigger.level), self._levels.get(trigger.incident_id, 0))
        return M.TriggerFacts(incident_id=trigger.incident_id,
                              incident_type=trigger.incident_type, level=level,
                              evidence_state=trigger.evidence_state,
                              at=float(trigger.at_wall or self._now()),
                              armed=bool(trigger.armed), simulated=bool(trigger.simulated))

    def _handle_trigger(self, trigger: SosTrigger, *, entry_delay_over: bool = False,
                        actor: Actor | None = None) -> dict | None:
        """One incident handed over by the pipeline (idempotent per incident)."""
        if trigger.incident_id in self._triggered and not entry_delay_over:
            return None
        _remember(self._triggered, trigger.incident_id, None)
        self.counters["triggers"] += 1
        facts = self._facts(trigger)
        open_snap = self._open_snapshot()
        decision = M.on_trigger(facts, open_request=open_snap, history=self._history(),
                                rules=self.rules, entry_delay_over=entry_delay_over)
        if decision.action == "open":
            # A weapon or fire in an incident held for the entry delay opens at
            # once; the hold is dropped so it cannot open a second time.
            self._held.pop(trigger.incident_id, None)
            return self._open(trigger, facts, decision.flags, actor=actor)
        if decision.action == "attach" and open_snap is not None:
            self.counters["attached"] += 1
            request = self._apply_all(open_snap, decision.steps)
            self._link_incident(trigger.incident_id, open_snap.id)
            self._sweep_request(open_snap.id, self._now())
            return request
        if decision.action == "hold":
            self.counters["held"] += 1
            self._held[trigger.incident_id] = _Hold(trigger, facts, decision.hold_until)
            # Not "triggered": a weapon or fire later in the same incident must
            # still open a request, even if this hold is dropped (disarm, face).
            self._triggered.pop(trigger.incident_id, None)
            self._audit("sos_entry_delay", incident_id=trigger.incident_id,
                        seconds=self.rules.intrusion_entry_delay)
            return None
        if decision.action == "suppress":
            self.counters["suppressed"] += 1
            _remember(self._suppressed, trigger.incident_id, (trigger, facts))
            self._audit("sos_suppressed", incident_id=trigger.incident_id,
                        incident_type=trigger.incident_type, reason=decision.reason)
            return None
        self.counters["ignored"] += 1
        log.debug("SOS: %s", decision.reason)
        return None

    def _apply_all(self, snap: M.Snapshot, steps: Iterable[M.Step], *,
                   actor: Actor | None = None, channel: str | None = None,
                   now: float | None = None) -> dict | None:
        now = self._now() if now is None else now
        request = None
        for step in steps:
            result = self._apply(snap, step, now=now, actor=actor, channel=channel)
            if result is None:
                break
            request, snap = result, self._advance(snap, result)
        return request

    def _handle_level(self, change: LevelChange) -> None:
        self._level, self._level_incident = int(change.level), change.incident_id
        if change.incident_id:
            _remember(self._levels, change.incident_id,
                      max(self._levels.get(change.incident_id, 0), int(change.level)))
        now = self._now()
        if (change.incident_type == "intrusion" and change.incident_id
                and self.rules.intrusion_requests
                and change.level >= self.rules.min_level):
            from sentrix import mode
            if mode.is_armed() and change.incident_id not in self._held \
                    and change.incident_id not in self._triggered:
                self._handle_trigger(self._trigger_from_level(change))
        open_snap = self._open_snapshot()
        if open_snap is not None and change.incident_id in open_snap.incident_ids:
            step = M.level_rise(open_snap, change.level, change.evidence_state, now, self.rules)
            if step is not None:
                self._apply(open_snap, step, now=now)
                self._sweep_request(open_snap.id, now)
        suppressed = self._suppressed.get(change.incident_id or "")
        if suppressed is not None and change.level in self.rules.contacts_immediate_levels:
            # Stronger evidence than what was declined always prompts.
            del self._suppressed[change.incident_id]
            trigger, _ = suppressed
            self._triggered.pop(trigger.incident_id, None)
            self._handle_trigger(replace(trigger, level=int(change.level),
                                         evidence_state=change.evidence_state))

    def _trigger_from_level(self, change: LevelChange) -> SosTrigger:
        """An armed intrusion arrives as a level change; its snapshot and
        evidence come from the live incident when it is the same one."""
        live = self._live_incident(change.incident_id)
        session = getattr(self._pipeline(), "session_id", "") or ""
        return SosTrigger(
            incident_id=change.incident_id, level=int(change.level),
            incident_type=change.incident_type, evidence_state=change.evidence_state,
            tci=change.tci, reason="Unrecognised person (armed mode)", session_id=session,
            armed=True, at_wall=change.at_wall or self._now(), at_mono=change.at_mono,
            snapshot_path=getattr(live, "snapshot_path", None),
            evidence_bundle=getattr(live, "evidence_bundle", None),
            evidence_hash=getattr(live, "evidence_hash", None))

    def _live_incident(self, incident_id: str | None):
        pipeline = self._pipeline()
        current = getattr(getattr(pipeline, "incidents", None), "current", None)
        return current if current is not None and current.id == incident_id else None

    def _release_holds(self, now: float) -> None:
        """Armed intrusions whose entry delay ended: disarmed meanwhile ->
        dropped; the alarm over -> dropped; an authorised face in view ->
        disarm visibly (``face:<name>``); otherwise open the request."""
        from sentrix import mode
        with self._lock:
            for incident_id, hold in list(self._held.items()):
                if not mode.is_armed():
                    del self._held[incident_id]
                    continue
                if now < hold.until:
                    continue
                del self._held[incident_id]
                if self._level < self.rules.min_level and not hold.trigger.simulated:
                    self._audit("sos_entry_delay_ended", incident_id=incident_id,
                                outcome="alarm_over")
                    continue
                face = self._authorised_face()
                if face is not None:
                    self.counters["face_disarms"] += 1
                    mode.set_armed(False, by=f"face:{face}")
                    self._audit("sos_entry_delay_ended", incident_id=incident_id,
                                outcome="authorised_face", by=f"face:{face}")
                    continue
                trigger = replace(hold.trigger, level=max(hold.trigger.level, self._level))
                self._handle_trigger(trigger, entry_delay_over=True)

    def _authorised_face(self) -> str | None:
        """The live per-track identity: everyone in view recognised and the
        camera not degraded (liveness is out of scope: a photograph could pass)."""
        try:
            live = self.state.get()
        except Exception:
            return None
        if not live.get("authorized") or live.get("identity_degraded"):
            return None
        if int(live.get("person_count") or 0) < 1:
            return None
        overlay = getattr(self._pipeline(), "_overlay", None)
        persons = ((getattr(overlay, "detections", None) or {}).get("persons") or [])
        names = [p.get("identity") for p in persons
                 if p.get("identity") not in (None, "", "unknown", "unverified")]
        return T.clean(names[0], 40) if names else "resident"

    # ── opening a request ─────────────────────────────────────────────────────
    def _open(self, trigger: SosTrigger, facts: M.TriggerFacts, flags: dict, *,
              parent_id: str | None = None, approve: M.LinkedRequest | None = None,
              actor: Actor | None = None, channel: str = "system",
              template: dict | None = None) -> dict | None:
        """Create the request (DRAFTED), render its texts, open its package,
        then prompt the owner (or, for "Request help now", approve at once)."""
        from sentrix.alerting.dispatch import dispatch_service
        from sentrix.link import profile as profile_module
        from sentrix.storage import requests

        now = self._now()
        profile = self._profile()
        test_mode = self._test_mode()
        policy = freeze(profile, self.settings, test_mode=test_mode, now=_dt(now),
                        desk_phone=profile_module.desk_number(profile))
        flags = dict(flags)
        if self._camera_degraded():
            flags["camera_degraded"] = True
        if approve is not None and approve.duress:
            flags["duress"] = True
        # A silent request (the duress PIN at a disarm or on a closed request)
        # was never shown to the household: owners never see it at all.
        silent = bool(flags.get("silent"))
        peak = max(facts.level, self._levels.get(trigger.incident_id or "", 0))
        request_id = str(uuid.uuid4())
        creator = actor or Actor.system()
        detail = {"incident_id": trigger.incident_id, "incident_type": facts.incident_type,
                  "level": facts.level, "evidence_state": facts.evidence_state,
                  "reason": T.clean(trigger.reason, 200), "simulated": bool(trigger.simulated),
                  "repeat": bool(flags.get("repeat")), "parent_request_id": parent_id}
        request = requests.create_request(
            request_id, incident_id=trigger.incident_id, incident_type=facts.incident_type,
            level=min(int(trigger.level), peak), evidence_state=facts.evidence_state,
            policy=policy, expires_at=_dt(now + policy["window_seconds"]),
            test_mode=test_mode, session_id=trigger.session_id or None,
            camera_id=trigger.camera_id or "CAM_0", parent_request_id=parent_id,
            flags=flags, actor_type=creator.event_actor_type,
            actor_id=None if creator.type == "system" else creator.ref,
            channel=channel, detail=detail, at=_dt(now),
            snapshot_path=trigger.snapshot_path, evidence_bundle=trigger.evidence_bundle,
            evidence_hash=trigger.evidence_hash, urgency="HIGH" if peak >= 4 else "NORMAL",
            peak_level=peak, restricted=silent)
        if request is None:
            return None
        if template:
            # "Request help now": the incidents and evidence of the parent.
            requests.set_fields(request_id, incident_ids_json=json.dumps(
                template.get("incident_ids") or []))
        self.counters["opened"] += 1
        requests.set_fields(request_id, profile_version=policy["profile_version"])
        request = self._render_texts(requests.get_request(request_id)) or request
        package = dispatch_service.create_for_request(
            request, profile, tci=trigger.tci, reason=trigger.reason,
            sos_text=request.get("sos_text_full"))
        if package is not None:
            request = requests.set_fields(request_id, package_id=package.id) or request
        if not silent:
            # (A silent request leaves the Events page's links as they were.)
            for incident_id in request.get("incident_ids") or []:
                self._link_incident(incident_id, request_id)
        self._audit("sos_created", request_id=request_id, short_id=request["short_id"],
                    incident_id=trigger.incident_id, incident_type=facts.incident_type,
                    level=peak, repeat=bool(flags.get("repeat")),
                    simulated=bool(trigger.simulated), parent_request_id=parent_id,
                    restricted=bool(flags.get("duress")))
        self._publish(request)
        snap = self._snapshot(request_id)
        if approve is not None:
            step = M.approve_step(snap, creator, channel, approve.authority, now,
                                  duress=approve.duress,
                                  via="duress_pin" if approve.duress else "help_now")
        else:
            step = M.prompt(snap, now, self.rules, self._reach())
        result = self._apply(snap, step, now=now, actor=creator if approve else None,
                             channel=channel if approve else None)
        self._sweep_request(request_id, now)
        self._refresh_state()
        return requests.get_request(request_id) or result

    def _camera_degraded(self) -> bool:
        """D5: a dark camera is reported in the SOS text."""
        try:
            return float((self.state.get().get("features") or {}).get("is_night", 0.0)) >= 0.5
        except Exception:
            return False

    def _render_texts(self, request: dict | None) -> dict | None:
        """Store the request's full and short texts (never with the duress
        banner: owners read these; the Desk and contacts get theirs rendered
        for them)."""
        from sentrix.storage import requests
        if request is None:
            return None
        profile = self._profile()
        safe = {**request, "flags": {k: v for k, v in (request.get("flags") or {}).items()
                                     if k != "duress"}}
        kwargs = dict(test_mode=bool(request.get("test_mode", True)),
                      degraded=bool((request.get("flags") or {}).get("camera_degraded")))
        try:
            full = T.render_full(profile, safe, approved_by=self._approved_by(request),
                                 **kwargs)
            short = T.render_short(profile, safe, channel="sms", **kwargs)
        except Exception as exc:
            log.error("SOS: rendering failed: %s", exc)
            return request
        return requests.set_fields(request["id"], sos_text_full=full, sos_text_sms=short)

    def _approved_by(self, request: dict) -> str:
        if not request.get("responded_at") or request.get("state") not in S.HELP_PATH:
            return ""
        from sentrix.storage import requests
        name = ""
        for event in reversed(requests.events(request["id"], include_restricted=True)):
            if event["event"] == S.APPROVE:
                name = (event.get("detail") or {}).get("by_name") or ""
                break
        via = {"lc": "the console", "portal": "the Portal", "desk": "the Desk",
               "telegram": "Telegram", "email": "e-mail", "sms": "SMS",
               "contact_page": "a contact page"}.get(request.get("response_channel") or "", "")
        who = f" by {T.clean(name, 60)}" if name else ""
        return (f"help requested{who}{' via ' + via if via else ''} "
                f"{T.clock_time(request['responded_at'], seconds=True)} IST").strip()

    def _link_incident(self, incident_id: str | None, request_id: str) -> None:
        from sentrix.storage import database
        if not incident_id:
            return
        if not database.link_incident_request(incident_id, request_id):
            # The incident row is written by the pipeline's FIFO worker; try again.
            self._unlinked.setdefault((incident_id, request_id), 0)

    def _retry_links(self) -> None:
        from sentrix.storage import database
        for key, attempts in list(self._unlinked.items()):
            if database.link_incident_request(*key) or attempts + 1 >= LINK_RETRIES:
                self._unlinked.pop(key, None)
            else:
                self._unlinked[key] = attempts + 1

    # ── messages ──────────────────────────────────────────────────────────────
    def _recipients(self, request: dict, notice: M.Notice, reach: Reach) -> list:
        from sentrix.alerting.channels.base import Recipient
        policy = request.get("policy") or {}
        owner = policy.get("owner") or {}
        out: list[tuple[Recipient, str]] = []
        if notice.audience == "owner":
            name = owner.get("name") or "Owner"
            if reach.standalone:
                for channel, address in self._standalone_owner_channels():
                    out.append((Recipient("owner", name, channel, address, "owner"),
                                notice.template))
            if notice.sms and owner.get("phone"):
                out.append((Recipient("owner", name, "modem_sms", owner["phone"], "owner"),
                            notice.sms_template or notice.template))
            if notice.call and owner.get("phone"):
                out.append((Recipient("owner", name, "modem_call", owner["phone"], "owner"),
                            notice.template))
            return out
        if reach.online:
            return out                       # the Portal pages the Desk and the contacts
        if notice.audience == "contacts":
            for contact in policy.get("contacts") or []:
                if contact.get("phone"):
                    out.append((Recipient(f"contact:{contact['id']}",
                                          contact.get("name") or "Contact", "modem_sms",
                                          contact["phone"], "contact"), notice.template))
        elif notice.audience == "desk":
            desk = policy.get("desk") or {}
            if desk.get("phone"):
                out.append((Recipient("desk", desk.get("name") or "SENTRIX Desk",
                                      "modem_sms", desk["phone"], "desk"), notice.template))
        return out

    def _notify(self, request: dict, notice: M.Notice, *, now: float) -> int:
        """Render and enqueue one notice for every recipient the device owns."""
        from sentrix.alerting.channels.base import Message
        reach = self._reach()
        recipients = self._recipients(request, notice, reach)
        if not recipients:
            return 0
        channels = self._channels()
        if channels is None:
            self.counters["no_channel"] += len(recipients)
            log.warning("SOS %s: no channels service; %s (%s) not sent",
                        request["short_id"], notice.step, notice.audience)
            return 0
        profile = self._profile()
        approved_by = self._approved_by(request)
        sent = 0
        for recipient, template in recipients:
            audience = {"owner": "owner", "desk": "desk"}.get(recipient.role, "contact")
            try:
                text, subject = T.message_text(
                    template, profile, request, audience=audience, channel=recipient.channel,
                    test_mode=bool(request.get("test_mode", True)), now=now,
                    approved_by=approved_by, urgent=notice.urgency == "HIGH",
                    degraded=bool((request.get("flags") or {}).get("camera_degraded")))
            except Exception as exc:
                self.counters["errors"] += 1
                log.error("SOS: rendering %s failed: %s", template, exc)
                continue
            message = Message(template=template, step=notice.step, text=text,
                              request_id=request["id"], subject=subject,
                              urgency=notice.urgency, created_at=now)
            try:
                item_id = channels.enqueue(recipient, message)
            except Exception as exc:
                self.counters["errors"] += 1
                log.error("SOS: enqueue failed: %s", exc)
                continue
            if item_id:
                sent += 1
                self.counters["messages"] += 1
        return sent

    def _handle_outbox(self, item: Any) -> None:
        """A channel status change for one of our messages: record it in the
        request's chain and derive ``delivered`` (a SENT modem SMS of the help
        path while the Portal is unreachable; SUPPRESSED never counts)."""
        message = getattr(item, "message", None)
        request_id = getattr(message, "request_id", None)
        status = str(getattr(item, "status", "") or "").upper()
        if not request_id or status not in ("SENT", "DELIVERED", "FAILED", "SUPPRESSED"):
            return
        if status == "FAILED":
            if item.id in self._notified_failures:
                return                        # the router retries; record the first
            if len(self._notified_failures) > 10_000:
                self._notified_failures.clear()
            self._notified_failures.add(item.id)
        snap = self._snapshot(request_id)
        if snap is None:
            return
        from sentrix.utils.redact import mask_recipient
        recipient = item.recipient
        # "message_step", not "step": a delivery report is not the step itself.
        detail = {"message_step": message.step, "channel": recipient.channel,
                  "recipient_ref": recipient.ref,
                  "recipient_masked": mask_recipient(recipient.address),
                  "status": status, "attempts": int(getattr(item, "attempts", 0) or 0),
                  "error": T.clean(getattr(item, "error", None), 200) or None}
        now = self._now()
        result = self._apply(snap, M.Step(S.NOTIFICATION, detail=detail), now=now)
        self._audit("sos_notification", request_id=request_id, step=message.step,
                    channel=recipient.channel, recipient=recipient.ref, status=status)
        if result is None or status not in M.DELIVERED_STATUSES:
            return
        snap = self._advance(snap, result)
        if (snap.state in (S.APPROVED, S.FAILED) and not self._reach().online
                and str(message.step).startswith(_HELP_STEP_PREFIXES)):
            outcome = M.cloud_event(snap, S.DELIVERED, actor=Actor.system(), now=now,
                                    detail={"channel": recipient.channel,
                                            "recipient_ref": recipient.ref,
                                            "step": message.step})
            self._apply_all(snap, outcome.steps, now=now)

    def _handle_verdict(self, payload: dict) -> None:
        """``database.set_verdict`` changed a request (a FALSE label while it
        prompted, or on the help path): publish, tell notified contacts / the
        Desk, refresh the LC."""
        effect, request_id = payload.get("effect"), payload.get("request_id")
        if effect not in ("request_cancelled", "resident_safe") or not request_id:
            return
        from sentrix.storage import requests
        request = requests.get_request(request_id)
        if request is None:
            return
        now = self._now()
        flags = request.get("flags") or {}
        if effect == "request_cancelled":
            if flags.get("contacts_notified"):
                self._notify(request, M.Notice("contacts", S.STEP_RESOLVED, M.T_RESOLVED),
                             now=now)
        else:
            for audience in ("desk", "contacts"):
                self._notify(request, M.Notice(audience, S.STEP_RESOLVED, M.T_RESOLVED),
                             now=now)
        self._audit("sos_transition", request_id=request_id, sos_event=effect,
                    state=request["state"], by=payload.get("by"))
        self._publish(request)
        self._refresh_state()

    # ── link events ───────────────────────────────────────────────────────────
    @staticmethod
    def _payload(request: dict) -> dict:
        """``RequestPayload``: the request minus its file path and session."""
        return {k: v for k, v in request.items() if k not in ("snapshot_path", "session_id")}

    def _publish(self, request: dict) -> int:
        """Every device-origin event of the request not yet handed to the link
        (deduplicated by ``sos:<request>:device:<seq>``), then the consented
        snapshot once."""
        from sentrix.link import protocol
        from sentrix.storage import requests
        request_id = request["id"]
        last = self._published.get(request_id, 0)
        try:
            events = [e for e in requests.events(request_id, include_restricted=True,
                                                 origin="device") if e["seq"] > last]
        except Exception as exc:
            log.error("SOS: reading events failed: %s", exc)
            return 0
        payload = self._payload(request)
        for event in events:
            if event["event"] == S.CREATED:
                kind, body = "sos.created", {"request": payload, "event": event}
            else:
                moved = event["from_state"] != event["to_state"]
                kind = "sos.transition" if moved else "sos.event"
                body = {"request_id": request_id, "version": request["version"],
                        "request": payload, "event": event}
            try:
                self._publish_fn(kind, body,
                                 dedupe_key=protocol.sos_dedupe_key(request_id, "device",
                                                                    event["seq"]))
            except Exception as exc:
                log.error("SOS: publish %s failed: %s", kind, exc)
            self._published[request_id] = max(self._published.get(request_id, 0), event["seq"])
        self._publish_snapshot(request)
        return len(events)

    def _publish_snapshot(self, request: dict) -> None:
        from sentrix import link
        request_id = request["id"]
        if request_id in self._snapshots_sent or not request.get("snapshot_path"):
            return
        if not ((request.get("policy") or {}).get("consents") or {}).get("sos_snapshot"):
            return
        if self._publish_fn == self._link_publish and not link.link_enabled():
            return
        encoded = self._snapshot_jpeg(request["snapshot_path"])
        if encoded is None:
            return
        data, width, height = encoded
        self._snapshots_sent.add(request_id)
        self._publish_fn("sos.snapshot", {
            "request_id": request_id, "content_type": "image/jpeg",
            "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height,
            "data_b64": base64.b64encode(data).decode("ascii")},
            dedupe_key=f"sos:{request_id}:snapshot")

    def _snapshot_jpeg(self, name: str) -> tuple[bytes, int, int] | None:
        """The incident snapshot, at most 640 px wide and 256 KiB."""
        from sentrix.web.security import sanitize_filename
        path = self.snapshot_dir / sanitize_filename(name)
        try:
            import cv2
            image = cv2.imread(str(path))
        except Exception:
            return None
        if image is None:
            return None
        height, width = image.shape[:2]
        if width > SNAPSHOT_MAX_WIDTH:
            scale = SNAPSHOT_MAX_WIDTH / width
            image = cv2.resize(image, (SNAPSHOT_MAX_WIDTH, max(1, int(height * scale))),
                               interpolation=cv2.INTER_AREA)
            height, width = image.shape[:2]
        for quality in (70, 55, 40):
            ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok and len(buffer) <= SNAPSHOT_MAX_BYTES:
                return buffer.tobytes(), width, height
        return None

    # ── audit ─────────────────────────────────────────────────────────────────
    def _audit(self, event: str, **detail: Any) -> None:
        """A JSONL audit row in the live pipeline's event log (docs/7 §13)."""
        pipeline = self._pipeline()
        events = getattr(pipeline, "events", None)
        if events is None:
            return
        try:
            events.audit(event, **detail)
        except Exception as exc:
            log.debug("SOS audit %s failed: %s", event, exc)

    def _audit_event(self, event: dict | None, request: dict) -> None:
        if not event:
            return
        name = "sos_created" if event["event"] == S.CREATED else "sos_transition"
        if name == "sos_created":
            return                                   # written by _open with more detail
        self._audit(name, request_id=request["id"], seq=event["seq"], sos_event=event["event"],
                    from_state=event["from_state"], to_state=event["to_state"],
                    actor_type=event["actor_type"], channel=event["channel"],
                    restricted=event["restricted"])

    # ── answers from people and the cloud ─────────────────────────────────────
    def respond(self, request_id: str, action: str, *, actor: Actor, channel: str,
                authority: str | None = None, pin: str | None = None) -> SosResult:
        try:
            return self._respond(request_id, action, actor=actor, channel=channel,
                                 authority=authority, pin=pin)
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS respond failed: %s", exc, exc_info=True)
            return sos_result("internal", "the SOS service failed; call 112 if you need help")

    def _respond(self, request_id: str, action: str, *, actor: Actor, channel: str,
                 authority: str | None, pin: str | None) -> SosResult:
        if action not in M.RESPOND_ACTIONS:
            return sos_result("invalid_args", f"unknown action {action!r}")
        snap = self._snapshot(request_id)
        if snap is None:
            return sos_result("not_found", "unknown request")
        outcome_pin = None
        if M.needs_pin(action) and M.may_respond(snap, action, actor) is None:
            outcome_pin = pins.check(pin, self._profile(), limiter=self.limiter).outcome
        now = self._now()
        with self._lock:
            snap = self._snapshot(request_id)
            outcome = M.respond(snap, action, actor=actor, channel=channel, now=now,
                                rules=self.rules, authority=authority, pin=outcome_pin)
            request = self._apply_all(snap, outcome.steps, actor=actor, channel=channel,
                                      now=now)
            linked = None
            if outcome.linked is not None:
                linked = self._open_linked(outcome.linked, actor, channel)
            self._sweep_request(request_id, now)
        self._refresh_state()
        from sentrix.storage import requests
        if linked is not None and not outcome.linked.duress:
            shown = linked
        else:
            shown = requests.get_request(request_id) or request
        return sos_result(outcome.code, outcome.message, self._view_for(actor, shown))

    def _open_linked(self, linked: M.LinkedRequest, actor: Actor, channel: str) -> dict | None:
        """"Request help now" (or a duress PIN on a closed request): a new
        request, pre-filled from the parent, approved at once."""
        from sentrix.storage import requests
        parent = requests.get_request(linked.parent_id)
        if parent is None:
            return None
        current = requests.open_request()
        if current is not None:
            # One open request per device: the answer goes to the open one.
            now = self._now()
            snap = self._snapshot(current["id"])
            outcome = M.respond(snap, "decline" if linked.duress else "approve", actor=actor,
                                channel=channel, now=now, rules=self.rules,
                                authority=linked.authority,
                                pin=pins.DURESS if linked.duress else None)
            self._apply_all(snap, outcome.steps, actor=actor, channel=channel, now=now)
            return requests.get_request(current["id"])
        trigger = SosTrigger(
            incident_id=parent.get("incident_id"), level=int(parent.get("peak_level") or 3),
            incident_type=parent.get("incident_type") or "weapon",
            evidence_state=parent.get("evidence_state") or "confirmed", tci=0.0,
            reason="Request help now" if not linked.duress else "Duress PIN",
            session_id=parent.get("session_id") or "",
            camera_id=parent.get("camera_id") or "CAM_0",
            at_wall=self._now(), snapshot_path=parent.get("snapshot_path"),
            evidence_bundle=parent.get("evidence_bundle"),
            evidence_hash=parent.get("evidence_hash"),
            simulated=bool((parent.get("flags") or {}).get("simulated")))
        facts = self._facts(trigger)
        flags = {"simulated": True} if trigger.simulated else {}
        if linked.duress:
            flags["silent"] = True
        return self._open(trigger, facts, flags, parent_id=parent["id"], approve=linked,
                          actor=actor, channel=channel,
                          template={"incident_ids": parent.get("incident_ids") or []})

    def cloud_event(self, request_id: str, event: str, *, actor: Actor,
                    detail: dict | None = None) -> SosResult:
        try:
            snap = self._snapshot(request_id)
            if snap is None:
                return sos_result("not_found", "unknown request")
            now = self._now()
            with self._lock:
                snap = self._snapshot(request_id)
                outcome = M.cloud_event(snap, event, actor=actor, now=now, detail=detail)
                channel = {"operator": "desk", "contact": "contact_page"}.get(actor.type,
                                                                              "system")
                self._apply_all(snap, outcome.steps, actor=actor, channel=channel, now=now)
            self._refresh_state()
            from sentrix.storage import requests
            return sos_result(outcome.code, outcome.message,
                              self._view_for(actor, requests.get_request(request_id)))
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS cloud_event failed: %s", exc, exc_info=True)
            return sos_result("internal", str(exc))

    def sync(self, request_id: str, *, cloud_state: str, cloud_version: int,
             authority_side: str, events: list[dict]) -> SosResult:
        """``sos.sync`` (docs/7 §7.6): import the cloud's chain, merge the
        state, take the authority back."""
        try:
            return self._sync(request_id, cloud_state=cloud_state, cloud_version=cloud_version,
                              authority_side=authority_side, events=events)
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS sync failed: %s", exc, exc_info=True)
            return sos_result("internal", str(exc))

    def _sync(self, request_id: str, *, cloud_state: str, cloud_version: int,
              authority_side: str, events: list[dict]) -> SosResult:
        from sentrix.storage import requests
        if cloud_state not in S.STATES:
            return sos_result("invalid_args", f"unknown state {cloud_state!r}")
        cloud_events = sorted((e for e in events or [] if isinstance(e, dict)
                               and e.get("origin") == "cloud"),
                              key=lambda e: int(e.get("seq") or 0))
        now = self._now()
        with self._lock:
            created = requests.get_request(request_id) is None
            if created:
                self._create_from_cloud(request_id, cloud_events, now)
            pending = self._buffered.pop(request_id, []) + cloud_events
            codes = {"ok": 0, "duplicate": 0, "gap": 0, "invalid": 0}
            gaps = []
            for event in sorted(pending, key=lambda e: int(e.get("seq") or 0)):
                _, code = requests.import_event(event)
                codes[code] = codes.get(code, 0) + 1
                if code == "gap":
                    gaps.append(event)
            if gaps:
                self._buffered[request_id] = gaps
            self._merge_cloud_flags(request_id, cloud_events)
            snap = self._snapshot(request_id)
            step = M.reconcile(snap, cloud_state, cloud_version=cloud_version,
                               cloud_outcome=_cloud_outcome(cloud_events))
            if step is not None:
                self._apply(snap, step, now=now, actor=Actor.cloud())
            requests.set_fields(request_id, authority_side="device", cloud_synced=True)
            if created:
                self._merge_own_requests(request_id, now)
            self._sweep_request(request_id, now)
        self._refresh_state()
        message = ", ".join(f"{n} {code}" for code, n in codes.items() if n)
        return sos_result("ok", message, requests.get_request(request_id))

    def _merge_cloud_flags(self, request_id: str, cloud_events: list[dict]) -> None:
        """Flags the cloud's answers set (duress, contacts notified, resident
        safe), derived from its imported events, merged into the row before the
        reconnect merge: a duress approval made in the cloud must stay hidden
        from the owner on the LC too."""
        from sentrix.storage import requests
        found: dict[str, bool] = {}
        for event in cloud_events:
            name, detail = event.get("event"), event.get("detail") or {}
            if not isinstance(detail, dict):
                detail = {}
            if detail.get("duress") or (name == S.APPROVE and event.get("restricted")):
                found["duress"] = True
            if name == S.CONTACTS_NOTIFIED:
                found["contacts_notified"] = True
            if name == S.RESIDENT_SAFE:
                found["resident_safe"] = True
        if not found:
            return
        request = requests.get_request(request_id)
        if request is None:
            return
        flags = dict(request.get("flags") or {})
        if all(flags.get(k) for k in found):
            return
        flags.update(found)
        requests.set_fields(request_id, flags_json=json.dumps(flags, sort_keys=True))

    def _create_from_cloud(self, request_id: str, cloud_events: list[dict], now: float) -> None:
        """A request the cloud opened while this device was offline (D9:
        heartbeat lost during an open L2+ incident, or "Request help now").
        The cloud's CREATED detail carries the incident(s), type, level and
        the policy it froze (window, safety defaults, contacts)."""
        from sentrix.link import profile as profile_module
        from sentrix.storage import requests
        first = next((e for e in cloud_events if e.get("event") == S.CREATED), {}) or {}
        detail = first.get("detail") or {}
        profile = self._profile()
        incident_ids = [str(i) for i in (detail.get("incident_ids") or []) if i]
        incident_id = detail.get("incident_id") or (incident_ids[0] if incident_ids else None)
        if incident_id and incident_id not in incident_ids:
            incident_ids.insert(0, incident_id)
        incident_type = str(detail.get("incident_type") or "intrusion")[:32]
        try:
            level = int(detail.get("level") or 3)
        except (TypeError, ValueError):
            level = 3
        test_mode = self._test_mode()
        created_at = M.epoch(first.get("at")) or now
        policy = freeze(profile, self.settings, test_mode=test_mode, now=_dt(created_at),
                        desk_phone=profile_module.desk_number(profile))
        cloud_policy = detail.get("policy") if isinstance(detail.get("policy"), dict) else {}
        if cloud_policy:
            # The policy is frozen when the request opens (D9): the cloud's.
            try:
                policy["window_seconds"] = int(cloud_policy.get("window_seconds")
                                               or policy["window_seconds"])
            except (TypeError, ValueError):
                pass
            if "safety_defaults" in cloud_policy:
                policy["safety_defaults"] = bool(cloud_policy["safety_defaults"])
            listed = cloud_policy.get("contacts")
            if isinstance(listed, list):
                ids = {str(c.get("id")) for c in listed if isinstance(c, dict)}
                policy["contacts"] = [c for c in policy.get("contacts") or []
                                      if str(c.get("id")) in ids]
        expires_at = M.epoch(detail.get("expires_at")) or (created_at + policy["window_seconds"])
        requests.create_request(
            request_id, incident_id=incident_id, incident_type=incident_type, level=level,
            evidence_state=str(detail.get("evidence_state") or "unconfirmed")[:16],
            policy=policy, expires_at=_dt(expires_at),
            test_mode=test_mode, flags={"device_offline": True}, origin="cloud",
            authority_side="cloud", at=_dt(created_at))
        if len(incident_ids) > 1:
            requests.set_fields(request_id, incident_ids_json=json.dumps(incident_ids))
        self.counters["opened"] += 1
        self._render_texts(requests.get_request(request_id))
        for linked in incident_ids:
            self._link_incident(linked, request_id)
        self._audit("sos_created", request_id=request_id, incident_id=incident_id,
                    incident_type=incident_type, origin="cloud")

    def _merge_own_requests(self, cloud_request_id: str, now: float) -> None:
        """The device's own request for any incident of the cloud's becomes
        part of the cloud's (``detail.merged_into``)."""
        from sentrix.storage import requests
        cloud = requests.get_request(cloud_request_id)
        if not cloud:
            return
        incident_ids = list(cloud.get("incident_ids") or [])
        if cloud.get("incident_id") and cloud["incident_id"] not in incident_ids:
            incident_ids.append(cloud["incident_id"])
        seen: set[str] = set()
        others = [o for i in incident_ids for o in requests.requests_for_incident(i)]
        for other in others:
            if other["id"] in seen:
                continue
            seen.add(other["id"])
            if other["id"] == cloud_request_id or other["state"] in S.TERMINAL_STATES:
                continue
            snap = self._snapshot(other["id"])
            if snap.prompting:
                step = M.Step(S.CANCEL, package="cancelled",
                              detail={"reason": "merged", "merged_into": cloud_request_id})
            else:
                step = M.Step(S.NOTIFICATION, detail={"merged_into": cloud_request_id})
            self._apply(snap, step, now=now, actor=Actor.cloud())

    # ── verdicts, mode, packages ──────────────────────────────────────────────
    def false_verdict(self, incident_id: str, *, pin: str | None, actor: Actor,
                      channel: str, note: str | None = None) -> SosResult:
        try:
            return self._false_verdict(incident_id, pin=pin, actor=actor, channel=channel,
                                       note=note)
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS false_verdict failed: %s", exc, exc_info=True)
            return sos_result("internal", str(exc))

    def _open_request_of(self, incident_id: str) -> dict | None:
        from sentrix.storage import requests
        for request in requests.requests_for_incident(incident_id):
            if request["state"] in S.OPEN_STATES:
                return request
        return None

    def _false_verdict(self, incident_id: str, *, pin: str | None, actor: Actor,
                       channel: str, note: str | None) -> SosResult:
        from sentrix.storage import database
        if actor.type not in M.HOUSEHOLD_ACTORS:
            return sos_result("forbidden_role", f"{actor.type} may not label incidents")
        request = self._open_request_of(incident_id)
        if request is None:
            ok = database.set_verdict(incident_id, "FALSE", note, by=actor.ref,
                                      channel=channel, actor_type=actor.event_actor_type)
            return sos_result() if ok else sos_result("not_found", "unknown incident")
        check = pins.check(pin, self._profile(), limiter=self.limiter)
        now = self._now()
        with self._lock:
            snap = self._snapshot(request["id"])
            if check.outcome == pins.INVALID:
                self._apply(snap, M.Step(S.PIN_FAILED, detail={"action": "false_verdict"}),
                            now=now, actor=actor, channel=channel)
                return sos_result("pin_invalid", "wrong SOS PIN")
            if check.outcome in (pins.LOCKED, pins.UNAVAILABLE):
                return sos_result(check.code, "too many wrong PINs; try again later"
                                  if check.outcome == pins.LOCKED else
                                  "no SOS PIN is set up for this household")
            if check.outcome == pins.DURESS and not snap.duress:
                outcome = M.respond(snap, "decline", actor=actor, channel=channel, now=now,
                                    rules=self.rules, pin=pins.DURESS)
                self._apply_all(snap, outcome.steps, actor=actor, channel=channel, now=now)
            ok = database.set_verdict(incident_id, "FALSE", note, by=actor.ref,
                                      channel=channel, actor_type=actor.event_actor_type)
            if not ok:
                return sos_result("not_found", "unknown incident")
            self._audit("verdict_set", incident_id=incident_id, verdict="FALSE",
                        request_id=request["id"], by=actor.ref)
        self.drain()
        self._refresh_state()
        from sentrix.storage import requests
        return sos_result("ok", "Marked as false alarm",
                          self._view_for(actor, requests.get_request(request["id"])))

    def set_mode(self, armed: bool, *, pin: str | None, actor: Actor) -> SosResult:
        try:
            return self._set_mode(bool(armed), pin=pin, actor=actor)
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS set_mode failed: %s", exc, exc_info=True)
            return sos_result("internal", str(exc))

    def _set_mode(self, armed: bool, *, pin: str | None, actor: Actor) -> SosResult:
        from sentrix import mode
        if actor.type not in M.HOUSEHOLD_ACTORS:
            return sos_result("forbidden_role", f"{actor.type} may not change the mode")
        if armed:
            mode.set_armed(True, by=actor.ref)
            return sos_result("ok", "Armed")
        profile = self._profile()
        if not pins.configured(profile):
            if actor.type == "device_admin":
                self._clear_holds()
                mode.set_armed(False, by=actor.ref)
                return sos_result("ok", "Disarmed")
            return sos_result("unprocessable", "no SOS PIN is set up for this household")
        check = pins.check(pin, profile, limiter=self.limiter)
        if check.outcome in (pins.INVALID, pins.LOCKED):
            now = self._now()
            with self._lock:
                open_snap = self._open_snapshot()
                if open_snap is not None and check.outcome == pins.INVALID:
                    self._apply(open_snap, M.Step(S.PIN_FAILED, detail={"action": "disarm"}),
                                now=now, actor=actor, channel="lc")
            return sos_result(check.code, "wrong SOS PIN" if check.outcome == pins.INVALID
                              else "too many wrong PINs; try again later")
        with self._lock:
            self._clear_holds()
            mode.set_armed(False, by=actor.ref)
            if check.outcome == pins.DURESS:
                self._silent_duress(actor)
        self._refresh_state()
        return sos_result("ok", "Disarmed")

    def _clear_holds(self) -> None:
        with self._lock:
            for incident_id in list(self._held):
                self._audit("sos_entry_delay_ended", incident_id=incident_id,
                            outcome="disarmed")
            self._held.clear()

    def _silent_duress(self, actor: Actor) -> None:
        """The duress PIN at a disarm: help, while the house visibly disarms.

        With an open request it becomes a duress approval (it then reads as
        declined). Otherwise a *silent* request opens for the live incident,
        or for none: a "duress" request with no incident row, because an
        incident row would show on the household's Events page."""
        now = self._now()
        open_snap = self._open_snapshot()
        if open_snap is not None:
            outcome = M.respond(open_snap, "decline", actor=actor, channel="lc", now=now,
                                rules=self.rules, pin=pins.DURESS)
            self._apply_all(open_snap, outcome.steps, actor=actor, channel="lc", now=now)
            return
        live = getattr(getattr(self._pipeline(), "incidents", None), "current", None)
        incident_id = live.id if live is not None else None
        trigger = SosTrigger(incident_id=incident_id, level=4, incident_type="duress",
                             evidence_state="confirmed", tci=0.0, reason="Duress PIN",
                             session_id=getattr(self._pipeline(), "session_id", "") or "",
                             at_wall=now,
                             snapshot_path=getattr(live, "snapshot_path", None),
                             evidence_bundle=getattr(live, "evidence_bundle", None),
                             evidence_hash=getattr(live, "evidence_hash", None))
        if incident_id:
            _remember(self._triggered, incident_id, None)
        self._open(trigger, self._facts(trigger), {"silent": True}, actor=actor, channel="lc",
                   approve=M.LinkedRequest("", recommended_authority("duress"), duress=True))

    def _synthetic_incident(self, incident_type: str, reason: str, *, level: int = 3) -> str:
        """An incident row for a request no detection opened (duress, the
        disclosed demo trigger), so verdicts and the Events page work."""
        from sentrix.pipeline.incidents import Incident, PeakSnapshot
        from sentrix.storage import database
        now = self._now()
        incident = Incident(id=str(uuid.uuid4()), started_at=0.0, started_wall=now,
                            level=level, peak_level=level, last_active=0.0, ended_at=0.0,
                            peak=PeakSnapshot(tci=0.0, level=level, incident_type=incident_type,
                                              reason=reason, at=0.0, wall=now))
        incident.actions.add("sos")
        session = getattr(self._pipeline(), "session_id", None) or "sos"
        database.save_incident(incident, session)
        return incident.id

    def approve_package(self, package_id: str, authority: str, *, actor: Actor,
                        channel: str) -> SosResult:
        from sentrix.alerting.dispatch import AUTHORITIES, dispatch_service
        from sentrix.storage import database, requests
        authority = (authority or "").upper()
        if authority not in AUTHORITIES:
            return sos_result("invalid_args", "authority must be POLICE, FIRE or MEDICAL")
        package = database.get_dispatch(package_id)
        if package is None:
            return sos_result("not_found", "unknown package")
        if not package.get("request_id"):
            outcome = dispatch_service.send(package_id, authority, by=actor.ref)
            return sos_result(outcome.code, outcome.message, None)
        request = requests.get_request(package["request_id"])
        if request is None:
            return sos_result("not_found", "unknown request")
        flags = request.get("flags") or {}
        if (package["status"] in ("SENT", "CANCELLED", "EXPIRED")
                or request["state"] == S.DECLINED or flags.get("duress")):
            return sos_result("conflict", f"package is {dispatch_owner_status(package, request)}")
        return self.respond(request["id"], "approve", actor=actor, channel=channel,
                            authority=authority)

    # ── demo (test mode only) ─────────────────────────────────────────────────
    def demo_trigger(self, incident_type: str, *, actor: Actor) -> SosResult:
        if not self._test_mode():
            return sos_result("test_mode_only", "demo triggers exist in test mode only")
        if actor.type not in M.HOUSEHOLD_ACTORS + ("cloud",):
            return sos_result("forbidden_role", f"{actor.type} may not start a demo")
        incident_type = str(incident_type or "").lower()
        allowed = tuple(self.rules.alarm_classes) + (
            ("intrusion",) if self.rules.intrusion_requests else ())
        if incident_type not in allowed:
            return sos_result("invalid_args",
                              f"incident_type must be one of {', '.join(allowed)}")
        from sentrix import mode
        if incident_type == "intrusion" and not mode.is_armed():
            return sos_result("unprocessable", "an intrusion opens a request in armed mode only")
        reason = f"{T.WORDING_SIMULATED} (test mode, scripted by {actor.ref})"
        incident_id = self._synthetic_incident(incident_type, reason)
        # A fight only reaches L3 corroborated (video + aligned audio, D15).
        evidence = "corroborated" if incident_type == "assault" else "confirmed"
        trigger = SosTrigger(incident_id=incident_id, level=3, incident_type=incident_type,
                             evidence_state=evidence, tci=0.5, reason=reason,
                             session_id=getattr(self._pipeline(), "session_id", "") or "",
                             armed=mode.is_armed(), at_wall=self._now(), simulated=True)
        self._audit("sos_demo_trigger", incident_id=incident_id, incident_type=incident_type,
                    by=actor.ref, note=T.WORDING_SIMULATED)
        with self._lock:
            request = self._handle_trigger(trigger, actor=None)
        self._refresh_state()
        if request is None:
            if incident_id in self._held:
                return sos_result("ok", f"{T.WORDING_SIMULATED}: held for the "
                                        f"{int(self.rules.intrusion_entry_delay)} s entry delay")
            return sos_result("conflict", "no request opened (suppressed or attached)")
        return sos_result("ok", T.WORDING_SIMULATED, self._view_for(actor, request))

    def demo_close_incident(self, *, actor: Actor) -> SosResult:
        if not self._test_mode():
            return sos_result("test_mode_only", "demo controls exist in test mode only")
        if actor.type not in M.HOUSEHOLD_ACTORS + ("cloud",):
            return sos_result("forbidden_role", f"{actor.type} may not use demo controls")
        pipeline = self._pipeline()
        if pipeline is None or not hasattr(pipeline, "request_close_incident"):
            return sos_result("unsupported", "no live pipeline is running")
        was_open = pipeline.request_close_incident("demo")
        reset = self._test_reset(actor)
        self._audit("sos_demo_close", by=actor.ref, was_open=bool(was_open), request=reset)
        messages = ["the open incident closes after the next decision" if was_open
                    else "no incident was open"]
        if reset == "kept":
            messages.append("the help request stays open until the Desk resolves it")
        elif reset:
            messages.append(f"the open request was {reset.lower()}")
        self._refresh_state()
        return sos_result("ok", "; ".join(messages))

    def _test_reset(self, actor: Actor) -> str | None:
        """The demo reset's request half (test mode): the next scene must open
        a *new* request, not attach to this one. A prompting request is
        cancelled ("test reset", docs/7 §7.3); a dispatched or acknowledged
        one resolved as ``no_action_needed``; an approved or failed one is
        kept (only a delivery or the Desk moves it on). Returns the state it
        ended in, "kept", or None (none open)."""
        from sentrix.storage import requests
        now = self._now()
        with self._lock:
            self._held.clear()
            current = requests.open_request()
            if current is None:
                return None
            snap = self._snapshot(current["id"])
            if snap.prompting:
                step = M.Step(S.CANCEL, detail={"reason": "test_reset"}, package="cancelled")
            elif snap.state in (S.DISPATCHED, S.ACKNOWLEDGED):
                step = M.Step(S.RESOLVE, detail={"outcome": "no_action_needed",
                                                 "reason": "test_reset"},
                              fields={"outcome": "no_action_needed"})
            else:
                return "kept"
            result = self._apply(snap, step, now=now, actor=actor, channel="system")
            return result["state"] if result else "kept"

    # ── views ─────────────────────────────────────────────────────────────────
    def _view_for(self, actor: Actor, request: dict | None) -> dict | None:
        view = {"operator": "desk", "contact": "contact", "cloud": "full",
                "system": "full"}.get(actor.type, "owner")
        return self.project(request, view)

    def project(self, request: dict | None, view: str = "owner") -> dict | None:
        """A request as ``view`` (owner | desk | contact | full) may see it."""
        if request is None:
            return None
        if view == "full":
            return dict(request)
        if view == "owner":
            return owner_projection(request)
        flags = dict(request.get("flags") or {})
        snapshot = _snapshot_path(request)
        profile = self._profile()
        audience = "desk" if view == "desk" else "contact"
        try:
            text = T.render_full(profile, request, audience=audience,
                                 approved_by=self._approved_by(request),
                                 test_mode=bool(request.get("test_mode", True)),
                                 degraded=bool(flags.get("camera_degraded")))
        except Exception:
            text = request.get("sos_text_full")
        banner = T.DURESS_BANNER if flags.get("duress") else None
        if view == "desk":
            out = {k: v for k, v in request.items() if k not in ("snapshot_path", "session_id")}
            out.update(label=S.STATE_LABELS.get(request["state"], request["state"]),
                       sos_text_full=text, duress_banner=banner, snapshot=snapshot)
            return out
        return {"id": request["id"], "short_id": request["short_id"],
                "state": request["state"],
                "label": S.STATE_LABELS.get(request["state"], request["state"]),
                "incident_type": request["incident_type"], "peak_level": request["peak_level"],
                "created_at": request["created_at"], "test_mode": request["test_mode"],
                "flags": {k: flags.get(k, False) for k in ("duress", "resident_safe",
                                                           "simulated")},
                "duress_banner": banner, "sos_text_full": text}

    def current(self, view: str = "owner") -> dict | None:
        from sentrix.storage import requests
        request = requests.open_request()
        if request is None:
            return None
        if view == "owner" and (request.get("flags") or {}).get("duress"):
            return None           # to the household it was declined: nothing is open
        return self.project(request, view)

    def events_view(self, request_id: str, view: str = "owner") -> list[dict]:
        """A request's audit trail as ``view`` may read it. Owners never see a
        restricted row, nor a gap or hash that would hint at one: their rows
        are numbered 1..n and a duress approval reads as the decline it looks
        like (docs/7 §7.8)."""
        from sentrix.storage import requests
        if view != "owner":
            return requests.events(request_id, include_restricted=view in ("desk", "full"))
        if owner_projection(requests.get_request(request_id)) is None:
            return []
        return owner_events(requests.events(request_id, include_restricted=True))

    def history(self, limit: int = 50, offset: int = 0, view: str = "owner") -> list[dict]:
        from sentrix.storage import requests
        projected = (self.project(r, view) for r in requests.list_requests(limit, offset))
        return [r for r in projected if r is not None]

    # ── state.sos ─────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        """The ``state.sos`` block: owner view, base-relative paths."""
        from sentrix.link import profile as profile_module
        from sentrix.pipeline.state import SOS_IDLE
        from sentrix.storage import requests
        test_mode = self._test_mode()
        profile = self._profile()
        block = dict(SOS_IDLE)
        block.update(desk_number=profile_module.desk_number(profile), test_mode=test_mode,
                     call_number=T.call_number(profile, test_mode), flags={})
        recent = [r for r in requests.list_requests(HISTORY_LIMIT)
                  if not (r.get("flags") or {}).get("silent")]
        active = next((r for r in recent if r["state"] in S.OPEN_STATES
                       and not (r.get("flags") or {}).get("duress")), None)
        closed = next((r for r in recent if r is not active
                       and S.owner_view(r["state"], r.get("flags"))["state"]
                       in S.TERMINAL_STATES), None)
        if closed is not None:
            shown = S.owner_view(closed["state"], closed.get("flags"))
            block["last"] = {"id": closed["id"], "short_id": closed["short_id"],
                             "state": shown["state"], "label": shown["label"],
                             "at": closed["updated_at"],
                             "actions": (["help_now"] if shown["state"] in
                                         (S.EXPIRED, S.DECLINED, S.CANCELLED) else [])}
        if active is None:
            return block
        shown = S.owner_view(active["state"], active.get("flags"))
        prompting = active["state"] in S.PROMPTING_STATES
        now = self._now()
        expires = M.epoch(active.get("expires_at"))
        block.update(
            active=True, request_id=active["id"], short_id=active["short_id"],
            state=shown["state"], label=shown["label"], incident_id=active["incident_id"],
            incident_type=active["incident_type"], level=active["peak_level"],
            urgency=active["urgency"], evidence_state=active["evidence_state"],
            created_at=active["created_at"],
            expires_at=active["expires_at"] if prompting else None,
            remaining_s=(max(0, int(expires - now)) if prompting and expires else None),
            seen=active.get("seen_at") is not None,
            repeat=bool((active.get("flags") or {}).get("repeat")),
            authority=active.get("authority"),
            recommended_authority=recommended_authority(active["incident_type"]),
            sos_text=active.get("sos_text_full"),
            snapshot=(f"media/snapshot/{active['snapshot_path']}"
                      if active.get("snapshot_path") else None),
            actions=self._actions(active, shown), flags=shown["flags"])
        return block

    @staticmethod
    def _actions(request: dict, shown: dict) -> list[str]:
        state = shown["state"]
        if state in S.PROMPTING_STATES:
            answer = ["approve"] + (["checking"] if state == S.PENDING else []) + ["decline"]
            # "Call 112 now" first for weapon and fire (docs/7 §7.10).
            if request["incident_type"] in ("weapon", "fire"):
                return ["call_112"] + answer
            return answer[:1] + ["call_112"] + answer[1:]
        if state in S.HELP_PATH and state != S.CLOSED:
            return ["call_112"] + ([] if shown["flags"].get("resident_safe") else ["safe"])
        return []

    def _refresh_state(self) -> None:
        from sentrix.alerting.dispatch import dispatch_service, owner_view
        try:
            block = self.status()
            package = None
            if block.get("active"):
                found = dispatch_service.package_for_request(block["request_id"])
                if found is not None and found.status in ("PENDING", "FAILED"):
                    from sentrix.storage import database
                    package = owner_view(database.get_dispatch(found.id) or {})
            self.state.update(sos=block, dispatch_package=package)
        except Exception as exc:
            log.error("SOS: state refresh failed: %s", exc)

    # ── service lifecycle ─────────────────────────────────────────────────────
    def recover(self) -> dict[str, int]:
        """Start-up (docs/7 §7.6 and design §5.4): an overdue PENDING (or a
        DRAFTED whose prompt never went out) expires "while the device was
        off"; a DRAFTED inside its window is prompted; a PENDING inside its
        window is announced again (the next reminder fires on time; overdue
        ones collapse into one); an APPROVED request resumes its dispatch.
        Requests the cloud holds wait for ``sos.sync``."""
        from sentrix.storage import requests
        counts = {"expired": 0, "prompted": 0, "reannounced": 0, "resumed": 0}
        now = self._now()
        reach = self._reach()
        with self._lock:
            for request in requests.open_requests():
                if request.get("authority_side") == "cloud":
                    continue
                snap = self._snapshot(request["id"])
                if snap.prompting and snap.expires_at is not None and now >= snap.expires_at:
                    # Through the same rule as the sweeper: a safety-default
                    # contacts alert still owed goes out before the expiry.
                    steps = M.expiry_steps(snap, self.rules, reach,
                                           "expired while the device was off")
                    self._apply_all(snap, steps, now=now)
                    if (requests.get_request(snap.id) or {}).get("state") == S.EXPIRED:
                        counts["expired"] += 1
                elif snap.state == S.DRAFTED:
                    if self._apply(snap, M.prompt(snap, now, self.rules, reach), now=now):
                        counts["prompted"] += 1
                elif snap.state == S.PENDING:
                    counts["reannounced"] += 1
                    self._audit("sos_reannounced", request_id=snap.id)
                elif snap.state == S.APPROVED:
                    counts["resumed"] += 1
                    for notice in M.help_notices(snap, duress=snap.duress):
                        self._notify(requests.get_request(snap.id), notice, now=now)
                self._publish(requests.get_request(snap.id))
        if any(counts.values()):
            log.info("SOS recovery: %s", counts)
        return counts

    def start(self) -> None:
        if self._started:
            return
        from sentrix import listeners
        from sentrix.link import profile as profile_module
        listeners.add_listener("verdict", self._on_verdict)
        profile_module.add_listener(self._on_profile)
        channels = self._channels()
        if channels is not None and hasattr(channels, "add_listener"):
            channels.add_listener(self._on_outbox)
            self._channels_service = channels
        self._started = True
        try:
            self.recover()
        except Exception as exc:
            self.counters["errors"] += 1
            log.error("SOS recovery failed: %s", exc, exc_info=True)
        self._refresh_state()
        self._sweeper.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        from sentrix import listeners
        from sentrix.link import profile as profile_module
        self._sweeper.stop()
        try:
            self.drain()                       # a trigger queued at shutdown still opens
        except Exception as exc:
            log.error("SOS: final drain failed: %s", exc)
        listeners.remove_listener("verdict", self._on_verdict)
        profile_module.remove_listener(self._on_profile)
        if self._channels_service is not None:
            try:
                self._channels_service.remove_listener(self._on_outbox)
            except Exception:
                pass
            self._channels_service = None

    def health(self) -> dict[str, Any]:
        from sentrix.storage import requests
        try:
            current = requests.open_request()
        except Exception:
            current = None
        shown = (S.owner_view(current["state"], current.get("flags"))
                 if current is not None else None)
        return {
            "enabled": True, "service": "engine", "running": self._sweeper.running,
            "open_request": (current["short_id"] if current is not None
                             and shown["state"] in S.OPEN_STATES else None),
            "state": shown["state"] if shown and shown["state"] in S.OPEN_STATES else None,
            "held_intrusions": len(self._held), "queue": len(self._inbox),
            "sweeps": self._sweeper.runs, "sweep_failures": self._sweeper.failures,
            "pin_locked": self.limiter.locked(), "test_mode": self._test_mode(),
            "channels": self._channels() is not None,
            **{k: v for k, v in self.counters.items()},
        }


def dispatch_owner_status(package: dict, request: dict | None) -> str:
    from sentrix.alerting.dispatch import owner_view
    return owner_view(package, request).get("status", "")


# ── what the household may see (also used by the LC routes with SOS off) ─────
#: Internal bookkeeping no owner-facing view carries.
_OWNER_HIDDEN = frozenset({"snapshot_path", "session_id", "version", "authority_side",
                           "cloud_synced", "repage_count", "flags", "state"})


def _snapshot_path(request: dict) -> str | None:
    """Base-relative, so one payload serves the LC and the tunnel (docs/7 §9.4)."""
    name = request.get("snapshot_path")
    return f"media/snapshot/{name}" if name else None


def owner_projection(request: dict | None) -> dict | None:
    """A request as an owner, a member or the LC may see it: the duress flag
    and the help path behind it hidden (``states.owner_view``). A silent
    request (opened by the duress PIN, never shown to the household) does
    not exist for them: None."""
    if request is None:
        return None
    flags = dict(request.get("flags") or {})
    if flags.get("silent"):
        return None
    shown = S.owner_view(request["state"], flags)
    out = {k: v for k, v in request.items() if k not in _OWNER_HIDDEN}
    out.update(state=shown["state"], label=shown["label"], flags=shown["flags"],
               snapshot=_snapshot_path(request))
    if flags.get("duress"):
        out.update(authority=None, outcome=None)
    return out


def owner_events(events: Iterable[dict]) -> list[dict]:
    """A request's audit trail as owners may read it. No restricted row,
    nor a sequence gap or hash that would hint at one: rows are numbered
    1..n, and a duress approval reads as the decline it appears to be
    (docs/7 §7.8)."""
    rows = []
    for event in events:
        if event.get("restricted"):
            detail = event.get("detail") or {}
            if event["event"] == S.APPROVE and detail.get("duress"):
                rows.append({"at": event["at"], "event": S.DECLINE,
                             "from_state": event["from_state"], "to_state": S.DECLINED,
                             "actor_type": event["actor_type"],
                             "actor_id": event["actor_id"], "channel": event["channel"],
                             "detail": {"via": detail.get("via")},
                             "origin": event["origin"]})
            continue
        rows.append({k: event.get(k) for k in ("at", "event", "from_state", "to_state",
                                               "actor_type", "actor_id", "channel",
                                               "detail", "origin")})
    rows.sort(key=lambda r: r["at"] or "")
    for index, row in enumerate(rows, 1):
        row["n"] = index
    return rows


# ── service factory and doctor checks ─────────────────────────────────────────
def build_service(ctx: Any) -> SosEngine | None:
    """The ``sos`` service (``SENTRIX_SOS_ENABLED=1``), else None."""
    settings = ctx.settings
    if settings is None or not settings.sos.enabled:
        return None
    registry = ctx.registry

    def pipeline():
        engine = sys.modules.get("sentrix.pipeline.engine")
        if engine is None or getattr(engine, "_pipeline", None) is None:
            return None
        return ctx.get_pipeline()

    return SosEngine(settings, state=ctx.state,
                     channels=lambda: registry.get("channels") if registry else None,
                     pipeline=pipeline)


def preflight(settings: Any) -> list[dict[str, str]]:
    """Cheap local checks for ``tools/doctor.py`` (no network, no devices)."""
    rows: list[dict[str, str]] = []

    def row(status: str, name: str, detail: str) -> None:
        rows.append({"status": status, "name": name, "detail": detail})

    sos = settings.sos
    if not sos.enabled:
        row("ok", "sos", "off (SENTRIX_SOS_ENABLED=0): no assistance requests")
        return rows
    intrusion = (f"armed intrusion after {int(sos.intrusion_entry_delay_seconds)} s"
                 if getattr(sos, "intrusion_requests", False)
                 else "unrecognised people stay at L2 (no request)")
    row("ok", "sos", f"on: requests for {', '.join(sos.alarm_classes)} at L{sos.min_level}+, "
                     f"{intrusion}")
    try:
        import argon2  # noqa: F401
    except ImportError:
        row("fail", "sos.pins", "argon2-cffi is not installed: SOS PINs cannot be checked")
    from sentrix.link import profile as profile_module
    try:
        profile = profile_module.current()
    except Exception:
        profile = None
    if profile is None:
        if os.getenv("SENTRIX_SOS_PIN_HASH", "").strip():
            row("ok", "sos.pin", "standalone: SOS PIN from SENTRIX_SOS_PIN_HASH")
        elif os.getenv("SENTRIX_PASSWORD", "").strip():
            row("warn", "sos.pin", "standalone: the console password stands in for the "
                                   "SOS PIN (set SENTRIX_SOS_PIN_HASH)")
        else:
            row("fail", "sos.pin", "no SOS PIN and no console password: nobody can decline")
        if not (os.getenv("ALERT_PHONE_NUMBER", "").strip()
                or os.getenv("SENTRIX_USER_PHONE", "").strip()):
            row("warn", "sos.owner", "standalone: ALERT_PHONE_NUMBER is not set (no owner SMS)")
    else:
        if not ((profile.get("sos") or {}).get("pin_hash")):
            row("fail", "sos.pin", "the Portal profile has no SOS PIN")
        if not profile_module.desk_number(profile):
            row("warn", "sos.desk", "no Desk number in the profile or SENTRIX_DESK_NUMBER")
    if not settings.channels.enabled:
        row("warn", "sos.channels", "SENTRIX_CHANNELS_ENABLED=0: only the console shows "
                                    "prompts; no SMS can leave")
    if sos.demo_window_seconds and not settings.test_mode:
        row("warn", "sos.window", "SENTRIX_SOS_DEMO_WINDOW_SECONDS is ignored outside test mode")
    if sos.window_seconds not in sos.window_choices:
        row("warn", "sos.window", f"SENTRIX_SOS_WINDOW_SECONDS={sos.window_seconds} is not one "
                                  f"of {sos.window_choices}; it will be clamped")
    if sos.contacts_for_single_source_l3:
        row("warn", "sos.contacts", "single-source weapon L3 alerts contacts "
                                    "(only once gate P8 passed)")
    return rows


__all__ = ["SosEngine", "build_service", "preflight", "owner_projection", "owner_events"]
