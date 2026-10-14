"""Who acted: one shape for every console route, link command and SOS call.

A request on the Local Console comes from the device admin (the LC password)
or, through the Portal tunnel, from an owner, a member or a Desk operator. A
link command can also come from a contact (a token page), the cloud (its own
timers while the device is offline) or the system. Every SOS call, verdict
and audit row records an :class:`Actor` (docs/7 §5.6, §7.7, §10).

Stdlib only: the Portal builds the same dicts (``Actor.to_dict``) for the
``actor`` field of ``command`` and ``tunnel.request`` messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Actor types. ``operator`` is a Desk user; contacts have no login (token pages).
ACTOR_TYPES = ("device_admin", "owner", "member", "operator", "contact", "cloud",
               "system")
#: Types a tunnelled request may carry (``sentrix.link.tunnel_auth.TUNNEL_ROLES``).
PORTAL_USER_TYPES = ("owner", "member", "operator")

#: Prefix of the audit string (``verdict_by``, ``sent_by``, ``responded_by``).
_REF_PREFIX = {"device_admin": "lc", "owner": "portal", "member": "portal",
               "operator": "desk", "contact": "contact"}
#: ``request_events.actor_type`` uses the SOS vocabulary (states.ACTORS), where
#: a Desk operator is "desk".
_EVENT_TYPE = {"operator": "desk"}


@dataclass(frozen=True)
class Actor:
    """``type`` is one of :data:`ACTOR_TYPES`; ``id`` the Portal user,
    contact or operator id (None for device_admin, cloud and system);
    ``step_up_at`` unix seconds of the last password/PIN re-entry."""

    type: str
    id: str | None = None
    name: str | None = None
    step_up_at: float | None = None

    def __post_init__(self) -> None:
        if self.type not in ACTOR_TYPES:
            raise ValueError(f"unknown actor type {self.type!r}")

    # ── audit strings ─────────────────────────────────────────────────────────
    @property
    def ref(self) -> str:
        """``lc:device_admin``, ``portal:<id>``, ``desk:<id>``, ``contact:<id>``,
        ``cloud`` or ``system`` (fits the 64-character ``*_by`` columns)."""
        if self.type == "device_admin":
            return "lc:device_admin"
        prefix = _REF_PREFIX.get(self.type)
        if prefix is None:
            return self.type
        return f"{prefix}:{self.id or '?'}"[:64]

    @property
    def event_actor_type(self) -> str:
        """The value for ``request_events.actor_type`` (``sentrix.sos.states.ACTORS``)."""
        return _EVENT_TYPE.get(self.type, self.type)

    def stepped_up_within(self, seconds: float, now: float) -> bool:
        return self.step_up_at is not None and 0 <= now - self.step_up_at <= seconds

    # ── wire format ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "name": self.name,
                "step_up_at": self.step_up_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Actor":
        """Parse a command/tunnel ``actor``; raises ``ValueError`` when invalid.

        Accepts the v1 draft spelling ``{user_id, user_name, role}`` as well.
        """
        if not isinstance(data, dict):
            raise ValueError("actor must be an object")
        kind = data.get("type") or data.get("role")
        raw_step = data.get("step_up_at")
        try:
            step = float(raw_step) if raw_step not in (None, "") else None
        except (TypeError, ValueError):
            step = None
        ident = data.get("id", data.get("user_id"))
        name = data.get("name", data.get("user_name"))
        return cls(type=str(kind or ""), id=None if ident is None else str(ident)[:64],
                   name=None if name is None else str(name)[:100], step_up_at=step)

    # ── common actors ─────────────────────────────────────────────────────────
    @classmethod
    def device_admin(cls) -> "Actor":
        return cls("device_admin", name="Device admin")

    @classmethod
    def system(cls) -> "Actor":
        return cls("system")

    @classmethod
    def cloud(cls) -> "Actor":
        return cls("cloud", name="SENTRIX Portal")


def event_actor_type_for_ref(by: str | None) -> str:
    """Best guess of the event actor type from an audit string, for callers
    that pass only ``by`` (legacy). New code passes the :class:`Actor`."""
    text = by or ""
    if text.startswith("lc:"):
        return "device_admin"
    if text.startswith("desk:"):
        return "desk"
    if text.startswith("contact:"):
        return "contact"
    if text.startswith("portal:"):
        return "owner"
    if text in ("cloud", "system"):
        return text
    return "system"


__all__ = ["Actor", "ACTOR_TYPES", "PORTAL_USER_TYPES", "event_actor_type_for_ref"]
