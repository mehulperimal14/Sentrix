"""The SOS request lifecycle as data: states, events, transitions, merge rule.

This module is the contract shared by the device SOS engine (``sentrix/sos``,
track T-SOS) and the Portal's mirror and cloud authority (``sentrix_portal``,
track T-LINK). It is pure and stdlib-only so both sides import the same table;
side effects (prompts, timers, messages) live in the engines. docs/7 §7 is the
prose version; a change here is a contract change.

Vocabulary (decisions D8, D9):

* A request opens DRAFTED at a confirmed L3 commit of an alarm class, becomes
  PENDING once the owner has been asked, and waits for the household window.
* Help path: APPROVED -> DISPATCHED -> ACKNOWLEDGED -> CLOSED (FAILED retries).
  An approved request never expires.
* Terminal: CLOSED, DECLINED, EXPIRED, CANCELLED. EXPIRED means *unanswered*,
  never "safe".
* A duress PIN approves (flag ``duress``, restricted audit row) while every
  owner-facing surface shows "Marked as false alarm".
"""

from __future__ import annotations

DRAFTED = "DRAFTED"
PENDING = "PENDING"
APPROVED = "APPROVED"
DISPATCHED = "DISPATCHED"
ACKNOWLEDGED = "ACKNOWLEDGED"
FAILED = "FAILED"
CLOSED = "CLOSED"
DECLINED = "DECLINED"
EXPIRED = "EXPIRED"
CANCELLED = "CANCELLED"

STATES = (DRAFTED, PENDING, APPROVED, DISPATCHED, ACKNOWLEDGED, FAILED,
          CLOSED, DECLINED, EXPIRED, CANCELLED)
TERMINAL_STATES = frozenset({CLOSED, DECLINED, EXPIRED, CANCELLED})
OPEN_STATES = frozenset(STATES) - TERMINAL_STATES
#: Waiting for the household's answer.
PROMPTING_STATES = frozenset({DRAFTED, PENDING})
#: Someone asked for help. CLOSED is reachable only through this path.
HELP_PATH = frozenset({APPROVED, DISPATCHED, ACKNOWLEDGED, FAILED, CLOSED})
#: Order along the help path, for the reconnect rule.
HELP_RANK = {FAILED: 1, APPROVED: 2, DISPATCHED: 3, ACKNOWLEDGED: 4, CLOSED: 5}

#: What owners and members read. EXPIRED is "unanswered", never "safe".
STATE_LABELS = {
    DRAFTED: "Preparing", PENDING: "Waiting for your answer",
    APPROVED: "Help requested", DISPATCHED: "Help request sent",
    ACKNOWLEDGED: "Acknowledged", FAILED: "NOT DELIVERED - call 112 now",
    CLOSED: "Closed", DECLINED: "Marked as false alarm",
    EXPIRED: "Unanswered", CANCELLED: "Cancelled",
}

# ── events ────────────────────────────────────────────────────────────────────
CREATED = "created"                # L3 commit of an alarm class (or Request help now)
PROMPTED = "prompted"              # SOS text rendered, owner asked
ATTACHED = "attached"              # another incident joined the open request
APPROVE = "approve"                # owner/member/contact/duress/SMS "1"
DECLINE = "decline"                # SOS PIN "false alarm"
CHECKING = "checking"              # "I'm checking": pauses reminders only
SEEN = "seen"                      # a prompt was opened
LEVEL_RISE = "level_rise"          # committed L4/L5 during the window
REMINDER = "reminder"
LAPSE_WARNING = "lapse_warning"
CONTACTS_NOTIFIED = "contacts_notified"   # safety defaults fired
DEVICE_OFFLINE = "device_offline"         # cloud: heartbeat lost
DEVICE_ONLINE = "device_online"
UNDELIVERED = "undelivered"        # no remote channel delivered within 60 s
EXPIRE = "expire"
CANCEL = "cancel"                  # Events-page FALSE verdict, test reset, account closed
DELIVERED = "delivered"            # >= 1 target accepted the SOS
DELIVERY_FAILED = "delivery_failed"
RETRY = "retry"
ACKNOWLEDGE = "acknowledge"        # Desk or contact "I'm handling it"
RESIDENT_SAFE = "resident_safe"    # owner "I'm safe now" (PIN) on the help path
RESOLVE = "resolve"                # Desk outcome
STALE_CLOSE = "stale_close"        # 24 h on the help path without a resolve
REPAGE = "repage"
PIN_FAILED = "pin_failed"
NOTIFICATION = "notification"      # a message queued/sent/failed (detail says which)
HELP_REQUESTED_LATER = "help_requested_later"  # "Request help now" on a closed request
CONFLICT = "conflict"              # reconnect merge changed the state

EVENTS = (CREATED, PROMPTED, ATTACHED, APPROVE, DECLINE, CHECKING, SEEN,
          LEVEL_RISE, REMINDER, LAPSE_WARNING, CONTACTS_NOTIFIED, DEVICE_OFFLINE,
          DEVICE_ONLINE, UNDELIVERED, EXPIRE, CANCEL, DELIVERED, DELIVERY_FAILED,
          RETRY, ACKNOWLEDGE, RESIDENT_SAFE, RESOLVE, STALE_CLOSE, REPAGE,
          PIN_FAILED, NOTIFICATION, HELP_REQUESTED_LATER, CONFLICT)

#: State-changing transitions: (from, event) -> to. ``None`` = no request yet.
TRANSITIONS: dict[tuple[str | None, str], str] = {
    (None, CREATED): DRAFTED,
    (DRAFTED, PROMPTED): PENDING,
    (DRAFTED, APPROVE): APPROVED,
    (PENDING, APPROVE): APPROVED,
    (DRAFTED, DECLINE): DECLINED,
    (PENDING, DECLINE): DECLINED,
    (DRAFTED, CANCEL): CANCELLED,
    (PENDING, CANCEL): CANCELLED,
    (PENDING, EXPIRE): EXPIRED,
    (APPROVED, DELIVERED): DISPATCHED,
    (FAILED, DELIVERED): DISPATCHED,
    (APPROVED, DELIVERY_FAILED): FAILED,
    (FAILED, RETRY): APPROVED,
    (APPROVED, ACKNOWLEDGE): ACKNOWLEDGED,
    (DISPATCHED, ACKNOWLEDGE): ACKNOWLEDGED,
    (FAILED, ACKNOWLEDGE): ACKNOWLEDGED,
    (DISPATCHED, RESOLVE): CLOSED,
    (ACKNOWLEDGED, RESOLVE): CLOSED,
    (DISPATCHED, STALE_CLOSE): CLOSED,
    (ACKNOWLEDGED, STALE_CLOSE): CLOSED,
}

_ALL_OPEN = OPEN_STATES
#: Events that leave the state unchanged, and where they are allowed.
SELF_EVENTS: dict[str, frozenset] = {
    ATTACHED: _ALL_OPEN,
    CHECKING: frozenset({PENDING}),
    SEEN: _ALL_OPEN,
    LEVEL_RISE: _ALL_OPEN,
    REMINDER: frozenset({PENDING}),
    LAPSE_WARNING: frozenset({PENDING}),
    CONTACTS_NOTIFIED: _ALL_OPEN,
    DEVICE_OFFLINE: _ALL_OPEN,
    DEVICE_ONLINE: _ALL_OPEN,
    UNDELIVERED: frozenset({PENDING, APPROVED, FAILED}),
    RESIDENT_SAFE: frozenset({APPROVED, DISPATCHED, ACKNOWLEDGED, FAILED}),
    ACKNOWLEDGE: frozenset({ACKNOWLEDGED}),
    REPAGE: frozenset({DISPATCHED, FAILED}),
    DELIVERY_FAILED: frozenset({FAILED}),
    DELIVERED: frozenset({DISPATCHED, ACKNOWLEDGED}),
    PIN_FAILED: frozenset(STATES),
    NOTIFICATION: frozenset(STATES),
    HELP_REQUESTED_LATER: frozenset({EXPIRED, DECLINED, CANCELLED}),
    CONFLICT: frozenset(STATES),
}

FLAGS = ("duress", "device_offline", "contacts_notified", "resident_safe",
         "repeat", "stale", "undelivered", "unacknowledged", "simulated")
ACTORS = ("system", "owner", "member", "contact", "desk", "device_admin", "cloud")
#: Where an answer came from (``response_channel``) or a message went.
CHANNELS = ("lc", "portal", "telegram", "email", "sms", "desk", "contact_page",
            "system")
AUTHORITIES = ("POLICE", "FIRE", "MEDICAL")
URGENCIES = ("NORMAL", "HIGH")
#: Desk resolve outcomes: the verdict they set.
OUTCOMES = {"help_arrived": "TRUE", "real_threat": "TRUE",
            "false_alarm_confirmed": "FALSE", "no_action_needed": "FALSE"}


def next_state(state: str | None, event: str) -> str | None:
    """The state after ``event``, or None when the event is not allowed there."""
    if (state, event) in TRANSITIONS:
        return TRANSITIONS[(state, event)]
    allowed = SELF_EVENTS.get(event)
    if allowed is not None and state in allowed:
        return state
    return None


def is_terminal(state: str | None) -> bool:
    return state in TERMINAL_STATES


def reconcile(device_state: str, cloud_state: str) -> str:
    """Single-authority merge after a reconnect (docs/7 §7.6).

    The device is the authority until the cloud has the request *and* has lost
    the device's heartbeat; the cloud then runs the timers. When both sides
    have moved: any help-path state wins (the furthest along); otherwise the
    cloud's terminal state; otherwise whichever side has advanced further.
    Commutative only in the help-path case by design: outside it the cloud's
    terminal state is preferred.
    """
    if device_state == cloud_state:
        return device_state
    device_help, cloud_help = device_state in HELP_PATH, cloud_state in HELP_PATH
    if device_help or cloud_help:
        if device_help and cloud_help:
            return max((device_state, cloud_state), key=lambda s: HELP_RANK[s])
        return device_state if device_help else cloud_state
    if cloud_state in TERMINAL_STATES:
        return cloud_state
    if device_state in TERMINAL_STATES:
        return device_state
    order = {DRAFTED: 0, PENDING: 1}
    return max((device_state, cloud_state), key=lambda s: order.get(s, 0))


def owner_view(state: str, flags: dict | None = None) -> dict:
    """What an owner, a member or the LC screen may see of a request.

    A duress approval is shown exactly like a decline: someone forcing the
    resident to cancel must not learn that help is coming.
    """
    flags = flags or {}
    shown = DECLINED if flags.get("duress") else state
    visible = {k: v for k, v in flags.items() if k != "duress"}
    return {"state": shown, "label": STATE_LABELS.get(shown, shown), "flags": visible}


__all__ = [name for name in dir() if name.isupper() and not name.startswith("_")] + [
    "next_state", "is_terminal", "reconcile", "owner_view"]
