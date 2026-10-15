"""The SOS machine: every decision about a request, and nothing else (docs/7 §7).

``sentrix.sos.states`` is the contract's table (states, events, which event
may follow which). This module adds the *policy* on top of it, as pure
functions: given a :class:`Snapshot` of a request (its row plus what its
audit chain says has happened), a time and the :class:`~sentrix.sos.policy.
Rules`, it answers what must happen next as a list of :class:`Step` values.
It reads no clock, no database, no configuration and sends nothing; the
engine (``sentrix/sos/service.py``) writes each step through
``storage.requests.try_transition`` and carries out its effects (messages,
the dispatch package, verdicts, link events). Time-travel and property tests
drive this module directly.

What is decided here (decisions D8, D9; docs/7 §7.1, §7.4, §7.5, §7.7, §7.8):

* **Eligibility.** Only the D1 alarm classes (weapon, fire) open a request,
  at a confirmed L3+ commit; experimental classes never. An armed-mode
  intrusion is *held* for the entry delay first. One open request per device:
  later incidents attach. After a decline the same threat re-prompts at most
  once per ``repeat_window`` (flag ``repeat``); stronger evidence (L4/L5)
  always prompts. After EXPIRED a new L3 opens a new request.
* **The owner's timeline.** T0 prompt (modem SMS when no remote prompt is
  possible), reminders at the configured offsets collapsed into one when
  several are overdue, "I'm checking" pausing reminders only, the lapse
  warning, the owner SMS when unseen at T+10 min (or offline), expiry.
* **Safety defaults.** Contacts alerted while the owner is still asked
  (``policy.contacts_delay``).
* **Answers.** Approve (who may: owner, member, device admin; a contact only
  under the frozen safety defaults), decline and "I'm safe" with the PIN, the
  duress PIN (approve + duress, shown as a decline), "Request help now" on a
  closed request (a new linked request), and the help path's re-pages, the
  undelivered warning, FAILED after the delivery window, stale close.
* **Cloud facts** (``sos.update``) and the reconnect merge (``states.reconcile``).

Times are epoch seconds (float) throughout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sentrix.actor import Actor
from sentrix.sos import states as S
from sentrix.sos.policy import Reach, Rules, contact_ids, contacts_delay, recommended_authority

#: Evidence states that count as a confirmed commit.
CONFIRMED_STATES = ("confirmed", "corroborated")
#: Who may answer the owner's question (``respond``).
HOUSEHOLD_ACTORS = ("owner", "member", "device_admin")
#: Who may report cloud-side facts (``cloud_event``).
CLOUD_ACTORS = ("cloud", "operator", "contact", "system")
#: Cloud-side facts a contact may report (the contact page's "I'm handling
#: it", and closing it once help has arrived).
CONTACT_CLOUD_EVENTS = ("acknowledge", "resolve", "seen", "notification")
#: Channel statuses that count as "delivered" (a dry run's SUPPRESSED never does).
DELIVERED_STATUSES = ("SENT", "DELIVERED")
RESPOND_ACTIONS = ("approve", "decline", "checking", "safe", "help_now")
UPDATE_EVENTS = ("acknowledge", "resolve", "delivered", "delivery_failed",
                 "contacts_notified", "device_offline", "device_online", "seen",
                 "notification")

#: Message templates by role in the flow (``channels.base.Message.template``).
T_PROMPT, T_OFFLINE = "sos.prompt", "sos.offline_prompt"
T_REMINDER, T_LAPSE, T_LAPSED = "sos.reminder", "sos.lapse_warning", "sos.lapsed"
T_SMS, T_FULL, T_RESOLVED, T_DURESS = "sos.sms", "sos.full", "sos.resolved", "sos.duress"
T_FAILED, T_UNACK, T_ACK = "sos.failed", "sos.unacknowledged", "sos.acknowledged"
STEP_UNDELIVERED = "undelivered"
STEP_UNACKNOWLEDGED = "unacknowledged"


# ── values in ─────────────────────────────────────────────────────────────────
def epoch(value: Any) -> float | None:
    """ISO string / datetime / number -> epoch seconds (None stays None)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


@dataclass(frozen=True)
class Snapshot:
    """One request as the machine sees it: the row, plus facts its audit
    chain records (prompt time, reminder progress, steps already taken)."""

    id: str
    state: str
    version: int = 1
    incident_type: str = "weapon"
    evidence_state: str = "confirmed"
    trigger_level: int = 3
    peak_level: int = 3
    urgency: str = "NORMAL"
    flags: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float | None = None
    prompted_at: float | None = None
    responded_at: float | None = None
    seen_at: float | None = None
    authority: str | None = None
    policy: Mapping[str, Any] = field(default_factory=dict)
    repage_count: int = 0
    authority_side: str = "device"
    incident_ids: tuple = ()
    test_mode: bool = True
    parent_request_id: str | None = None
    #: Step names already recorded (``detail.step`` of the chain's events).
    done: frozenset = frozenset()
    #: Highest owner-reminder offset already sent (reminders are monotonic).
    reminder_upto: float = 0.0
    last_checking_at: float | None = None
    #: When the current help attempt started (approval, or the last retry).
    help_since: float | None = None
    help_attempt: int = 0
    #: The last delivery failure happened while the link was down.
    failed_offline: bool = False
    #: An owner message of this request was delivered (SENT/DELIVERED) or seen.
    owner_reached: bool = False

    @property
    def duress(self) -> bool:
        return bool(self.flags.get("duress"))

    @property
    def window(self) -> float:
        try:
            return float(self.policy.get("window_seconds") or 1800)
        except (TypeError, ValueError):
            return 1800.0

    @property
    def prompting(self) -> bool:
        return self.state in S.PROMPTING_STATES

    @property
    def help_open(self) -> bool:
        return self.state in S.HELP_PATH and self.state != S.CLOSED

    @classmethod
    def from_request(cls, request: Mapping[str, Any],
                     events: Iterable[Mapping[str, Any]] = ()) -> "Snapshot":
        done: set[str] = set()
        reminder_upto = 0.0
        prompted = checking = help_since = None
        attempts, failed_offline, owner_reached = 0, False, False
        for event in sorted(events, key=lambda e: (epoch(e.get("at")) or 0.0,
                                                   e.get("origin") or "", e.get("seq") or 0)):
            name, detail = event.get("event"), event.get("detail") or {}
            at = epoch(event.get("at"))
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except ValueError:
                    detail = {}
            if detail.get("step"):
                done.add(str(detail["step"]))
            if name == S.PROMPTED and prompted is None:
                prompted = at
            elif name == S.REMINDER:
                try:
                    reminder_upto = max(reminder_upto, float(detail.get("offset") or 0))
                except (TypeError, ValueError):
                    pass
            elif name == S.CHECKING:
                checking = at
            elif name == S.APPROVE:
                help_since = at
            elif name == S.RETRY:
                help_since, attempts = at, attempts + 1
            elif (name == S.CONFLICT and detail.get("to") in S.HELP_PATH
                  and detail.get("from") not in S.HELP_PATH):
                help_since = at                 # the reconnect merge approved it
            elif name == S.DELIVERY_FAILED:
                failed_offline = not detail.get("online", True)
            elif name == S.SEEN:
                owner_reached = True
            elif name == S.NOTIFICATION:
                if (str(detail.get("status") or "").upper() in DELIVERED_STATUSES
                        and str(detail.get("recipient_ref") or "") == "owner"):
                    owner_reached = True
        seen = epoch(request.get("seen_at"))
        return cls(
            id=str(request["id"]), state=str(request["state"]),
            version=int(request.get("version") or 1),
            incident_type=str(request.get("incident_type") or "weapon"),
            evidence_state=str(request.get("evidence_state") or "confirmed"),
            trigger_level=int(request.get("trigger_level") or 3),
            peak_level=int(request.get("peak_level") or request.get("trigger_level") or 3),
            urgency=str(request.get("urgency") or "NORMAL"),
            flags=dict(request.get("flags") or {}),
            created_at=epoch(request.get("created_at")) or 0.0,
            expires_at=epoch(request.get("expires_at")),
            prompted_at=prompted, responded_at=epoch(request.get("responded_at")),
            seen_at=seen, authority=request.get("authority"),
            policy=dict(request.get("policy") or {}),
            repage_count=int(request.get("repage_count") or 0),
            authority_side=str(request.get("authority_side") or "device"),
            incident_ids=tuple(request.get("incident_ids") or ()),
            test_mode=bool(request.get("test_mode", True)),
            parent_request_id=request.get("parent_request_id"),
            done=frozenset(done), reminder_upto=reminder_upto, last_checking_at=checking,
            help_since=(help_since if help_since is not None
                        else epoch(request.get("responded_at"))),
            help_attempt=attempts, failed_offline=failed_offline,
            owner_reached=owner_reached or seen is not None)


@dataclass(frozen=True)
class TriggerFacts:
    """A committed L3+ tick the pipeline handed over (or a level change)."""

    incident_id: str | None
    incident_type: str
    level: int
    evidence_state: str
    at: float
    armed: bool = False
    simulated: bool = False


# ── values out ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Notice:
    """A message the engine should send. ``audience`` owner | contacts |
    desk; ``step`` the outbox dedupe step (``sentrix.sos.states`` names);
    ``sms`` the owner also gets it by modem SMS (``sms_template`` then);
    ``call`` the owner also gets a modem call (the L5 call's ``rise5``)."""

    audience: str
    step: str
    template: str
    urgency: str = "NORMAL"
    sms: bool = False
    sms_template: str | None = None
    call: bool = False


@dataclass(frozen=True)
class Step:
    """One event for the request's audit chain and what comes with it.

    ``fields``: column values written in the same transaction (times as epoch
    seconds); ``flags`` merged into the request's flags; ``package`` what
    happens to its dispatch package (authority | sent | cancelled | expired);
    ``verdict`` the label set on every incident of the request;
    ``forced_state``: only for the reconnect merge (``conflict``)."""

    event: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    fields: Mapping[str, Any] = field(default_factory=dict)
    flags: Mapping[str, Any] = field(default_factory=dict)
    restricted: bool = False
    notices: tuple = ()
    package: str | None = None
    verdict: str | None = None
    rerender: bool = False
    forced_state: str | None = None


@dataclass(frozen=True)
class LinkedRequest:
    """"Request help now" (or a duress PIN on a closed request): open a new
    request linked to ``parent_id``, approved at once."""

    parent_id: str
    authority: str
    duress: bool = False


@dataclass(frozen=True)
class Outcome:
    code: str = "ok"
    message: str = ""
    steps: tuple = ()
    linked: LinkedRequest | None = None

    @property
    def ok(self) -> bool:
        return self.code == "ok"


@dataclass(frozen=True)
class TriggerDecision:
    """``action``: open | attach | hold | ignore | suppress."""

    action: str
    reason: str = ""
    flags: Mapping[str, Any] = field(default_factory=dict)
    steps: tuple = ()
    hold_until: float | None = None


def _restricted(snap: Snapshot, step: Step) -> Step:
    """After a duress approval every event is restricted (owners never see
    the help path of a request they were forced to "decline")."""
    if snap.duress and not step.restricted:
        return replace(step, restricted=True,
                       notices=tuple(n for n in step.notices if n.audience != "owner"))
    return step


def _finalise(snap: Snapshot, steps: Sequence[Step]) -> tuple:
    return tuple(_restricted(snap, step) for step in steps)


# ── eligibility ───────────────────────────────────────────────────────────────
def wants(incident_type: str, level: int, rules: Rules) -> bool:
    """The pipeline hook's O(1) question. Only the alarm classes are claimed;
    an armed intrusion arrives as a level change and is held for the entry
    delay, so a weapon appearing later in the same incident still triggers."""
    return incident_type in rules.alarm_classes and int(level) >= rules.min_level


def classify(facts: TriggerFacts, rules: Rules) -> str | None:
    """alarm | intrusion | None (never opens a request).

    The committed level is the confirmation: the L3 band needs evidence the
    evidence layer already confirmed over several observations (unconfirmed
    evidence stays under L2), so an alarm class at L3+ opens a request even
    if the tick's vote summary arrived without its state. The evidence state
    only chooses the wording ("possible ... (one camera check)" or
    "confirmed by two different models")."""
    if facts.incident_type == "duress":
        return "alarm"
    if facts.level < rules.min_level:
        return None
    if facts.incident_type in rules.alarm_classes:
        return "alarm"
    if facts.incident_type == "intrusion" and facts.armed:
        return "intrusion"
    return None


def on_trigger(facts: TriggerFacts, *, open_request: Snapshot | None,
               history: Sequence[Snapshot], rules: Rules,
               entry_delay_over: bool = False) -> TriggerDecision:
    """What a committed L3+ incident does: open a request, attach to the open
    one, hold (armed intrusion, until ``entry_delay_over``), ignore, or
    suppress (the repeat throttle). ``history``: recent requests, newest
    first."""
    kind = classify(facts, rules)
    if kind is None:
        return TriggerDecision("ignore", f"{facts.incident_type} at L{facts.level} "
                                         f"({facts.evidence_state}) does not open a request")
    if open_request is not None:
        return TriggerDecision("attach", "one open request per device",
                               steps=attach_steps(open_request, facts, rules))
    if kind == "intrusion" and not entry_delay_over:
        return TriggerDecision("hold", "armed intrusion: entry delay",
                               hold_until=facts.at + rules.intrusion_entry_delay)
    flags: dict[str, Any] = {"simulated": True} if facts.simulated else {}
    declined = _recent_decline(facts, history, rules)
    if declined is not None:
        # At most one re-prompt per window, however often it is declined.
        repeated = any(r.flags.get("repeat") and r.incident_type == facts.incident_type
                       and facts.at - r.created_at < rules.repeat_window
                       for r in history)
        if repeated and facts.level not in rules.contacts_immediate_levels:
            return TriggerDecision("suppress", f"already re-prompted once within "
                                               f"{int(rules.repeat_window)} s of a decline")
        flags["repeat"] = True
    return TriggerDecision("open", "confirmed alarm class", flags=flags)


def _recent_decline(facts: TriggerFacts, history: Sequence[Snapshot],
                    rules: Rules) -> Snapshot | None:
    for snap in history:
        # A FALSE verdict while prompting (CANCELLED with an answer recorded)
        # counts as a decline; a test reset or a merge (no answer) does not.
        answered = snap.state == S.DECLINED or (snap.state == S.CANCELLED
                                                and snap.responded_at is not None)
        if answered and snap.incident_type == facts.incident_type:
            declined_at = snap.responded_at or snap.created_at
            if facts.at - declined_at < rules.repeat_window:
                return snap
    return None


def attach_steps(snap: Snapshot, facts: TriggerFacts, rules: Rules) -> tuple:
    """Another incident joins the open request (rollover included); a higher
    level is a level rise."""
    steps: list[Step] = []
    if facts.incident_id and facts.incident_id not in snap.incident_ids:
        steps.append(Step(S.ATTACHED, detail={"incident_id": facts.incident_id,
                                              "incident_type": facts.incident_type,
                                              "level": facts.level},
                          fields={"incident_ids": list(snap.incident_ids) + [facts.incident_id]}))
    if steps and snap.state == S.FAILED:
        # Help for this request was not delivered: a new alarm joining it must
        # reach the owner again, not be absorbed silently.
        steps[-1] = replace(steps[-1], notices=steps[-1].notices + (
            Notice("owner", f"attached-{facts.incident_id}", T_FAILED, urgency="HIGH",
                   sms=True),))
    rise = level_rise(snap, facts.level, facts.evidence_state, facts.at, rules)
    if rise is not None:
        steps.append(rise)
    return _finalise(snap, steps)


# ── prompting ─────────────────────────────────────────────────────────────────
def prompt(snap: Snapshot, now: float, rules: Rules, reach: Reach) -> Step:
    """DRAFTED -> PENDING: the window starts at the request's creation; the
    owner is asked (modem SMS too when no remote prompt is possible, and
    when the request opens already at L4/L5: the legacy L4 SMS and L5 call
    fold into the prompt as they would into ``rise4``/``rise5``)."""
    expires = snap.created_at + snap.window
    if snap.expires_at is not None:
        expires = max(expires, snap.expires_at)
    corroborated = snap.peak_level >= 4
    owner = Notice("owner", S.STEP_T0, T_PROMPT, urgency=snap.urgency,
                   sms=corroborated or not reach.remote_prompt, sms_template=T_OFFLINE,
                   call=snap.peak_level >= 5)
    return _restricted(snap, Step(S.PROMPTED, detail={"window_seconds": int(snap.window)},
                                  fields={"expires_at": expires}, notices=(owner,)))


def level_rise(snap: Snapshot, level: int, evidence_state: str, now: float,
               rules: Rules) -> Step | None:
    """A committed L4/L5 above the request's peak: urgency HIGH, the window
    restarts from now (a request cannot lapse a minute after a handgun was
    corroborated), an immediate owner reminder (the legacy L4 SMS folds into
    ``rise4``, the L5 call into ``rise5``) and new SOS texts."""
    step = S.RISE_STEPS.get(int(level))
    if step is None or int(level) <= snap.peak_level or snap.state in S.TERMINAL_STATES:
        return None
    fields: dict[str, Any] = {"peak_level": int(level), "urgency": "HIGH"}
    if evidence_state in CONFIRMED_STATES:
        fields["evidence_state"] = evidence_state
    notices: tuple = ()
    if snap.prompting:
        fields["expires_at"] = max(snap.expires_at or 0.0, now + snap.window)
        notices = (Notice("owner", step, T_REMINDER, urgency="HIGH", sms=True,
                          call=int(level) >= 5),)
    return _restricted(snap, Step(S.LEVEL_RISE, detail={"step": step, "level": int(level),
                                                        "previous": snap.peak_level},
                                  fields=fields, notices=notices, rerender=True))


# ── answers ───────────────────────────────────────────────────────────────────
def may_respond(snap: Snapshot, action: str, actor: Actor) -> str | None:
    """None when ``actor`` may give this answer, else the §13 code."""
    if actor.type in HOUSEHOLD_ACTORS:
        return None
    if actor.type == "contact":
        if action != "approve":
            return "forbidden_role"
        if not snap.policy.get("safety_defaults") or actor.id not in contact_ids(snap.policy):
            return "forbidden_role"
        return None
    return "forbidden_role"


def needs_pin(action: str) -> bool:
    return action in ("decline", "safe")


def _authority(snap: Snapshot, authority: str | None) -> str | None:
    chosen = (authority or snap.authority or recommended_authority(snap.incident_type))
    chosen = str(chosen).upper()
    return chosen if chosen in S.AUTHORITIES else None


def help_notices(snap: Snapshot, *, duress: bool) -> tuple:
    if duress:
        return (Notice("desk", S.STEP_DURESS, T_DURESS, urgency="HIGH"),
                Notice("contacts", S.STEP_DURESS, T_DURESS, urgency="HIGH"))
    return (Notice("desk", S.STEP_DESK_T0, T_SMS, urgency="HIGH"),
            Notice("contacts", S.STEP_CONTACTS_T0, T_SMS, urgency="HIGH"))


def approve_step(snap: Snapshot, actor: Actor, channel: str, authority: str, now: float, *,
                 duress: bool = False, via: str = "approve") -> Step:
    detail: dict[str, Any] = {"authority": authority, "by_name": actor.name, "via": via}
    flags: dict[str, Any] = {}
    if duress:
        detail["duress"], flags["duress"] = True, True
    return Step(S.APPROVE, detail=detail, flags=flags, restricted=duress,
                fields={"authority": authority, "responded_at": now,
                        "responded_by": actor.ref, "response_channel": channel},
                notices=help_notices(snap, duress=duress), package="authority")


def _duress(snap: Snapshot, action: str, actor: Actor, channel: str,
            authority: str, now: float) -> Outcome:
    """The duress PIN: help, silently. The caller is told exactly what a
    decline would have told it, and the incidents' label reads FALSE like a
    decline's (``set_verdict`` leaves a duress request's help alone; the
    restricted event records the truth for the Desk and for the dataset)."""
    if snap.prompting:
        step = replace(approve_step(snap, actor, channel, authority, now, duress=True,
                                    via=action), verdict="FALSE")
        return Outcome(message="Marked as false alarm", steps=(step,))
    if snap.help_open:
        if snap.duress:
            return Outcome("conflict", f"the request is {S.DECLINED}")
        # From now on every owner surface shows "Marked as false alarm"
        # (states.owner_view), so the answer says the same.
        step = Step(S.CONTACTS_NOTIFIED, restricted=True,
                    flags={"duress": True, "contacts_notified": True},
                    detail={"step": S.STEP_DURESS, "duress": True, "via": action},
                    notices=help_notices(snap, duress=True))
        return Outcome(message="Marked as false alarm", steps=(step,))
    # Closed already: answer like a closed request, open a silent one.
    return Outcome("conflict", f"the request is {snap.state}",
                   linked=LinkedRequest(snap.id, authority, duress=True))


def respond(snap: Snapshot, action: str, *, actor: Actor, channel: str, now: float,
            rules: Rules, authority: str | None = None,
            pin: str | None = None) -> Outcome:
    """An answer to the prompt. ``pin`` is the PIN check's outcome (ok |
    duress | invalid | locked | unavailable) for the actions that need one;
    the engine verifies the PIN before calling this.

    A duress request is answered exactly like the declined request it
    appears to be."""
    if action not in RESPOND_ACTIONS:
        return Outcome("invalid_args", f"unknown action {action!r}")
    refused = may_respond(snap, action, actor)
    if refused:
        return Outcome(refused, f"{actor.type} may not {action} this request")
    chosen = _authority(snap, authority)
    if chosen is None:
        return Outcome("invalid_args", "authority must be POLICE, FIRE or MEDICAL")
    if needs_pin(action):
        if pin in (None, "invalid"):
            return Outcome("pin_invalid", "wrong SOS PIN",
                           steps=_finalise(snap, (Step(S.PIN_FAILED, detail={"action": action}),)))
        if pin == "locked":
            return Outcome("pin_locked", "too many wrong PINs; try again later")
        if pin == "unavailable":
            return Outcome("unprocessable", "no SOS PIN is set up for this household")
        if pin == "duress":
            return _duress(snap, action, actor, channel, chosen, now)
    if snap.duress:
        if action == "help_now":
            step = Step(S.NOTIFICATION, restricted=True,
                        detail={"step": f"help_now{snap.repage_count + 1}", "duress": True},
                        notices=help_notices(snap, duress=True))
            return Outcome(message="Help requested", steps=(step,))
        return Outcome("conflict", f"the request is {S.DECLINED}")

    if action == "approve" or (action == "help_now" and snap.prompting):
        if snap.prompting:
            return Outcome(message="Help requested",
                           steps=(approve_step(snap, actor, channel, chosen, now, via=action),))
        if snap.help_open:
            return Outcome(message="Help was already requested")
        return Outcome("conflict", f"the request is {snap.state}")

    if action == "checking":
        if snap.state == S.PENDING:
            return Outcome(message="Reminders paused",
                           steps=(Step(S.CHECKING, fields={"seen_at": now}),))
        return Outcome("conflict", f"the request is {snap.state}")

    if action == "help_now":
        if snap.state in (S.EXPIRED, S.DECLINED, S.CANCELLED):
            step = Step(S.HELP_REQUESTED_LATER, detail={"authority": chosen,
                                                        "by_name": actor.name})
            return Outcome(message="Help requested", steps=(step,),
                           linked=LinkedRequest(snap.id, chosen))
        if snap.help_open:
            return Outcome(message="Help was already requested")
        return Outcome("conflict", f"the request is {snap.state}")

    # decline or safe, with the SOS PIN
    if snap.prompting:
        notices = ((Notice("contacts", S.STEP_RESOLVED, T_RESOLVED),)
                   if snap.flags.get("contacts_notified") else ())
        step = Step(S.DECLINE, detail={"via": action},
                    fields={"responded_at": now, "responded_by": actor.ref,
                            "response_channel": channel},
                    notices=notices, package="cancelled", verdict="FALSE")
        return Outcome(message="Marked as false alarm", steps=(step,))
    if snap.help_open:
        if snap.flags.get("resident_safe"):
            return Outcome(message="Already marked as safe")
        step = Step(S.RESIDENT_SAFE, flags={"resident_safe": True}, detail={"via": action},
                    notices=(Notice("desk", S.STEP_RESOLVED, T_RESOLVED),
                             Notice("contacts", S.STEP_RESOLVED, T_RESOLVED)))
        return Outcome(message="Marked as safe; help stays open until the Desk or a "
                               "contact closes it", steps=(step,))
    return Outcome("conflict", f"the request is {snap.state}")


# ── cloud facts ───────────────────────────────────────────────────────────────
def _small(detail: Mapping[str, Any] | None, limit: int = 2000) -> dict[str, Any]:
    """A JSON-safe copy of a remote detail, capped so a peer cannot bloat the chain."""
    try:
        text = json.dumps(dict(detail or {}), default=str, sort_keys=True)
    except (TypeError, ValueError):
        return {}
    if len(text) > limit:
        return {"truncated": True}
    return json.loads(text)


def cloud_event(snap: Snapshot, event: str, *, actor: Actor, now: float,
                detail: Mapping[str, Any] | None = None) -> Outcome:
    """A cloud-side fact (``sos.update``): the Desk acknowledged or resolved,
    a target accepted the SOS, the Portal's channels delivered a message ..."""
    if event not in UPDATE_EVENTS:
        return Outcome("invalid_args", f"unknown event {event!r}")
    if actor.type not in CLOUD_ACTORS:
        return Outcome("forbidden_role", f"{actor.type} may not report {event}")
    if actor.type == "contact" and (event not in CONTACT_CLOUD_EVENTS
                                    or actor.id not in contact_ids(snap.policy)):
        return Outcome("forbidden_role", "only this request's contacts may report this")
    if S.next_state(snap.state, event) is None:
        return Outcome("conflict", f"{event} is not allowed in {snap.state}")
    info = _small(detail)
    info["by_name"] = actor.name
    fields: dict[str, Any] = {}
    flags: dict[str, Any] = {}
    package = verdict = None
    notices: tuple = ()
    if event == S.RESOLVE:
        outcome = str(info.get("outcome") or "")
        if outcome not in S.OUTCOMES:
            return Outcome("invalid_args", f"outcome must be one of {', '.join(S.OUTCOMES)}")
        fields["outcome"], verdict = outcome, S.OUTCOMES[outcome]
    elif event == S.DELIVERED:
        if snap.state in (S.APPROVED, S.FAILED):
            package = "sent"
        flags["undelivered"] = False
    elif event == S.ACKNOWLEDGE:
        notices = (Notice("owner", "acknowledged", T_ACK),)
    elif event == S.CONTACTS_NOTIFIED:
        flags["contacts_notified"] = True
    elif event == S.DEVICE_OFFLINE:
        flags["device_offline"] = True
    elif event == S.DEVICE_ONLINE:
        flags["device_offline"] = False
    elif event == S.SEEN:
        fields["seen_at"] = now
    step = Step(event, detail=info, fields=fields, flags=flags, notices=notices,
                package=package, verdict=verdict)
    return Outcome(steps=_finalise(snap, (step,)))


def reconcile(snap: Snapshot, cloud_state: str, *,
              cloud_version: int | None = None,
              cloud_outcome: str | None = None) -> Step | None:
    """The reconnect merge (docs/7 §7.6) as one ``conflict`` event that sets
    the merged state, with the effects of arriving there. None: nothing to do.

    The incidents get the label the cloud's answer implies, as they would on
    the device: a decline FALSE, a Desk resolve its outcome's verdict
    (``cloud_outcome``), a duress approval FALSE (it looks like a decline)."""
    if cloud_state not in S.STATES:
        return None
    target = S.reconcile(snap.state, cloud_state)
    if target == snap.state:
        return None
    package = verdict = None
    fields: dict[str, Any] = {}
    if target == S.EXPIRED:
        package = "expired"
    elif target in (S.DECLINED, S.CANCELLED):
        package = "cancelled"
    elif target in (S.APPROVED, S.DISPATCHED, S.ACKNOWLEDGED):
        package = "sent" if target != S.APPROVED else "authority"
    if target == S.DECLINED:
        verdict = "FALSE"
    elif target == S.CLOSED and cloud_outcome in S.OUTCOMES:
        verdict, fields["outcome"] = S.OUTCOMES[cloud_outcome], cloud_outcome
    elif snap.duress and target in S.HELP_PATH:
        verdict = "FALSE"
    step = Step(S.CONFLICT, forced_state=target, package=package, verdict=verdict,
                fields=fields,
                detail={"from": snap.state, "to": target, "cloud_state": cloud_state,
                        "cloud_version": cloud_version})
    return _restricted(snap, step)


# ── timers ────────────────────────────────────────────────────────────────────
def _reminder_offsets(snap: Snapshot, rules: Rules) -> list[float]:
    if snap.prompted_at is None:
        return []
    end = snap.expires_at if snap.expires_at is not None else snap.prompted_at + snap.window
    return [float(o) for o in sorted(rules.reminder_offsets) if snap.prompted_at + o < end]


def repage_at(snap: Snapshot, n: int, rules: Rules) -> float | None:
    """When the n-th (1-based) re-page of the help path is due: the
    configured offsets, then every ``repage_interval``."""
    if snap.help_since is None or n < 1:
        return None
    offsets = list(rules.repage_offsets) or [rules.repage_interval]
    if n <= len(offsets):
        return snap.help_since + offsets[n - 1]
    return snap.help_since + offsets[-1] + (n - len(offsets)) * rules.repage_interval


def expire_step(snap: Snapshot, reach: Reach, reason: str = "window") -> Step:
    notices = [Notice("owner", S.STEP_LAPSED, T_LAPSED, sms=not reach.remote_prompt)]
    if snap.flags.get("contacts_notified"):
        notices.append(Notice("contacts", S.STEP_LAPSED, T_LAPSED))
    return Step(S.EXPIRE, detail={"reason": reason}, package="expired", notices=tuple(notices))


def _owed_contacts(snap: Snapshot, rules: Rules) -> tuple:
    """(rule, owed, step): the safety-default contacts alert of a prompting
    request, and whether it is still owed."""
    rule = contacts_delay(snap.policy, incident_type=snap.incident_type,
                          peak_level=snap.peak_level, rules=rules)
    owed = rule is not None and not snap.flags.get("contacts_notified")
    contacts = Step(S.CONTACTS_NOTIFIED, flags={"contacts_notified": True},
                    detail={"step": S.STEP_CONTACTS_T0,
                            "reason": rule[1] if rule else None},
                    notices=(Notice("contacts", S.STEP_CONTACTS_T0, T_SMS,
                                    urgency=snap.urgency),))
    return rule, owed, contacts


def expiry_steps(snap: Snapshot, rules: Rules, reach: Reach,
                 reason: str = "window") -> tuple:
    """The window lapsed: a safety-default alert still owed goes out first
    (a window shorter than the delay, the 90 s demo window vs the fire rule's
    2 min, or a restart after the window, must not swallow it), then EXPIRE."""
    _, owed, contacts = _owed_contacts(snap, rules)
    if owed:
        told = replace(snap, flags={**snap.flags, "contacts_notified": True})
        return _finalise(snap, (contacts, expire_step(told, reach, reason)))
    return _finalise(snap, (expire_step(snap, reach, reason),))


def due(snap: Snapshot, now: float, rules: Rules, reach: Reach) -> tuple:
    """Every timed step due at ``now`` (the sweeper's question), in order."""
    if snap.authority_side == "cloud" or snap.state in S.TERMINAL_STATES:
        return ()
    steps: list[Step] = []
    if snap.state == S.PENDING:
        rule, owed, contacts = _owed_contacts(snap, rules)
        if snap.expires_at is not None and now >= snap.expires_at:
            return expiry_steps(snap, rules, reach)
        if owed and now >= snap.created_at + rule[0]:
            steps.append(contacts)
        steps.extend(_owner_timeline(snap, now, rules, reach))
    elif snap.state == S.APPROVED and snap.help_since is not None:
        attempt = snap.help_attempt
        if now >= snap.help_since + rules.delivery_fail_after:
            steps.append(Step(S.DELIVERY_FAILED,
                              detail={"step": f"failed{attempt}", "online": reach.online},
                              notices=(Notice("owner", f"failed{attempt}", T_FAILED,
                                              urgency="HIGH", sms=True),)))
        elif (now >= snap.help_since + rules.delivery_warning
              and f"{STEP_UNDELIVERED}{attempt}" not in snap.done):
            steps.append(Step(S.UNDELIVERED, flags={"undelivered": True},
                              detail={"step": f"{STEP_UNDELIVERED}{attempt}"}))
    elif snap.state in (S.FAILED, S.DISPATCHED):
        if snap.state == S.FAILED and reach.online and snap.failed_offline:
            # The link is back: let the cloud deliver it.
            steps.append(Step(S.RETRY, detail={"reason": "link_online"}))
        else:
            steps.extend(_repage(snap, now, rules))
    if snap.state in (S.DISPATCHED, S.ACKNOWLEDGED, S.FAILED) and snap.help_since is not None \
            and now >= snap.help_since + rules.stale_close:
        steps = [Step(S.STALE_CLOSE, flags={"stale": True},
                      detail={"after_seconds": int(rules.stale_close)})]
    elif (snap.state == S.FAILED and snap.flags.get("resident_safe") and not reach.online
          and snap.help_since is not None and now >= snap.help_since + rules.repeat_window):
        # Undeliverable (no link) and the resident said they are safe: nobody
        # else can close it, and it must not absorb every later alarm.
        steps = [Step(S.STALE_CLOSE, flags={"stale": True},
                      detail={"reason": "resident_safe_undelivered"})]
    return _finalise(snap, steps)


def _owner_timeline(snap: Snapshot, now: float, rules: Rules, reach: Reach) -> list[Step]:
    steps: list[Step] = []
    if snap.prompted_at is None:
        return steps
    reached = snap.owner_reached or snap.seen_at is not None
    if (not reached and STEP_UNDELIVERED not in snap.done
            and now >= snap.prompted_at + rules.delivery_warning):
        steps.append(Step(S.UNDELIVERED, flags={"undelivered": True},
                          detail={"step": STEP_UNDELIVERED}))
    snoozed = (snap.last_checking_at is not None
               and now < snap.last_checking_at + rules.seen_snooze)
    if not snoozed:
        overdue = [o for o in _reminder_offsets(snap, rules)
                   if o > snap.reminder_upto and now >= snap.prompted_at + o]
        if overdue:
            latest = overdue[-1]
            name = S.reminder_step(latest)
            unseen_long = latest >= rules.owner_sms_unseen_after and snap.seen_at is None
            # SMS only when no remote prompt can reach the owner, or unseen long (D8).
            sms = not reach.remote_prompt or unseen_long
            steps.append(Step(S.REMINDER, detail={"step": name, "offset": int(latest),
                                                  "collapsed": [int(o) for o in overdue[:-1]]},
                              notices=(Notice("owner", name, T_REMINDER, urgency=snap.urgency,
                                              sms=sms, sms_template=T_OFFLINE),)))
    if snap.expires_at is not None and S.STEP_LAPSE not in snap.done:
        warn_at = snap.expires_at - rules.lapse_warning
        if warn_at > snap.prompted_at and now >= warn_at:
            steps.append(Step(S.LAPSE_WARNING, detail={"step": S.STEP_LAPSE},
                              notices=(Notice("owner", S.STEP_LAPSE, T_LAPSE,
                                              urgency=snap.urgency),)))
    return steps


def _repage(snap: Snapshot, now: float, rules: Rules) -> list[Step]:
    n = snap.repage_count + 1
    if snap.repage_count >= rules.repage_cap:
        if snap.flags.get("unacknowledged"):
            return []
        return [Step(S.NOTIFICATION, flags={"unacknowledged": True},
                     detail={"step": STEP_UNACKNOWLEDGED, "repages": snap.repage_count},
                     notices=(Notice("owner", STEP_UNACKNOWLEDGED, T_UNACK, urgency="HIGH",
                                     sms=True),))]
    at = repage_at(snap, n, rules)
    if at is None or now < at:
        return []
    name = S.repage_step(n)
    template = T_DURESS if snap.duress else T_SMS
    return [Step(S.REPAGE, fields={"repage_count": n}, detail={"step": name, "n": n},
                 notices=(Notice("desk", name, template, urgency="HIGH"),
                          Notice("contacts", name, template, urgency="HIGH")))]


def next_due(snap: Snapshot, now: float, rules: Rules) -> float | None:
    """The earliest future moment a timer of this request fires (the sweeper
    sleeps until then, at most its interval)."""
    if snap.authority_side == "cloud" or snap.state in S.TERMINAL_STATES:
        return None
    times: list[float] = []
    if snap.state == S.PENDING:
        if snap.expires_at is not None:
            times.append(snap.expires_at)
            times.append(snap.expires_at - rules.lapse_warning)
        rule = contacts_delay(snap.policy, incident_type=snap.incident_type,
                              peak_level=snap.peak_level, rules=rules)
        if rule is not None and not snap.flags.get("contacts_notified"):
            times.append(snap.created_at + rule[0])
        if snap.prompted_at is not None:
            times += [snap.prompted_at + o for o in _reminder_offsets(snap, rules)
                      if o > snap.reminder_upto]
            times.append(snap.prompted_at + rules.delivery_warning)
            if snap.last_checking_at is not None:
                times.append(snap.last_checking_at + rules.seen_snooze)
    elif snap.help_since is not None:
        times += [snap.help_since + rules.delivery_warning,
                  snap.help_since + rules.delivery_fail_after,
                  snap.help_since + rules.stale_close]
        if snap.state == S.FAILED and snap.flags.get("resident_safe"):
            times.append(snap.help_since + rules.repeat_window)
        at = repage_at(snap, snap.repage_count + 1, rules)
        if at is not None:
            times.append(at)
    future = [t for t in times if t > now]
    return min(future) if future else None


__all__ = ["Snapshot", "TriggerFacts", "Notice", "Step", "Outcome", "LinkedRequest",
           "TriggerDecision", "epoch", "wants", "classify", "on_trigger", "attach_steps",
           "prompt", "level_rise", "may_respond", "needs_pin", "approve_step", "respond",
           "help_notices",
           "cloud_event", "reconcile", "due", "next_due", "repage_at", "expire_step",
           "expiry_steps",
           "CONFIRMED_STATES", "RESPOND_ACTIONS", "UPDATE_EVENTS", "DELIVERED_STATUSES"]
