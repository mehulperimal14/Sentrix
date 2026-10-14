"""The policy a request runs under, frozen when it opens (decision D9).

A request's window, safety defaults, contact list and channels are copied
from the household profile into ``policy_json`` at the L3 commit and never
change afterwards: a settings change (which needs a fresh password or PIN on
the Portal) applies to the next request only, so nobody can shorten the window
or remove the contacts of a request that is already running. The PIN hashes
are *not* frozen (a changed PIN works at once) and never appear in the policy,
which travels to the Portal with every SOS event.

Everything here is pure: plain dicts in, plain dicts out, no I/O, so the
machine, the engine and the tests share one definition. :class:`Rules` is the
numeric part of ``settings.sos`` the machine needs.

Safety defaults (D9), as :func:`contacts_delay` encodes them, alert the
household's CONFIRMED contacts while the owner is still being asked:

==============================  ==========================================
Corroborated L4 / L5            at once (``contacts_immediate_levels``)
Confirmed fire                  T+2 min (``fire_contacts_after_seconds``)
Armed intrusion                 at once
Duress                          at once (it is a silent "yes")
Single-source weapon L3         owner only, until gate P8 enables
                                ``contacts_for_single_source_l3`` (T+5 min)
Device offline (heartbeat)      the cloud's rule (the device is gone)
==============================  ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

POLICY_VERSION = 1


@dataclass(frozen=True)
class Rules:
    """The timings and thresholds of ``settings.sos`` (seconds), resolved for
    test mode, so the machine never reads configuration itself."""

    min_level: int = 3
    alarm_classes: tuple = ("weapon", "fire")
    intrusion_entry_delay: float = 30.0
    reminder_offsets: tuple = (120, 300, 600, 1200)
    lapse_warning: float = 300.0
    seen_snooze: float = 300.0
    owner_sms_unseen_after: float = 600.0
    delivery_warning: float = 60.0
    #: Approved but nothing delivered after this: FAILED ("3 tries over 2 min").
    delivery_fail_after: float = 120.0
    contacts_immediate_levels: tuple = (4, 5)
    fire_contacts_after: float = 120.0
    single_source_contacts_after: float = 300.0
    repeat_window: float = 600.0
    repage_offsets: tuple = (120, 300, 600)
    repage_interval: float = 3600.0
    repage_cap: int = 10
    stale_close: float = 86400.0
    sweep_interval: float = 15.0
    test_mode: bool = True

    @classmethod
    def from_settings(cls, settings: Any, *, test_mode: bool | None = None) -> "Rules":
        sos = settings.sos
        test_mode = bool(settings.test_mode if test_mode is None else test_mode)
        backoff = tuple(getattr(settings.channels, "retry_backoff_seconds", (30.0, 90.0)))
        return cls(
            min_level=int(sos.min_level), alarm_classes=tuple(sos.alarm_classes),
            intrusion_entry_delay=float(sos.intrusion_entry_delay_seconds),
            reminder_offsets=tuple(int(o) for o in sos.reminder_offsets_seconds),
            lapse_warning=float(sos.lapse_warning_seconds),
            seen_snooze=float(sos.seen_snooze_seconds),
            owner_sms_unseen_after=float(sos.owner_sms_unseen_after_seconds),
            delivery_warning=float(sos.delivery_warning_seconds),
            delivery_fail_after=max(float(sos.delivery_warning_seconds),
                                    float(sum(backoff)) or 120.0),
            contacts_immediate_levels=tuple(sos.contacts_immediate_levels),
            fire_contacts_after=float(sos.fire_contacts_after_seconds),
            single_source_contacts_after=float(sos.single_source_contacts_after_seconds),
            repeat_window=float(sos.repeat_window_seconds),
            repage_offsets=tuple(float(o) for o in sos.repage_offsets_seconds),
            repage_interval=float(sos.repage_interval_seconds),
            repage_cap=int(sos.repage_cap_test_mode if test_mode else sos.repage_cap),
            stale_close=float(sos.stale_close_seconds),
            sweep_interval=float(sos.sweep_interval_seconds),
            test_mode=test_mode)


# ── window ────────────────────────────────────────────────────────────────────
def clamp_window(seconds: Any, choices: tuple = (1800, 2700, 3600)) -> int:
    """The nearest allowed window (30/45/60 min); unreadable values give the
    shortest choice."""
    allowed = sorted(int(c) for c in choices) or [1800]
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return allowed[0]
    # Ties go to the longer window: more time to answer is the safer error.
    return min(allowed, key=lambda c: (abs(c - value), -c))


def window_seconds(profile: Mapping | None, sos_cfg: Any, *, test_mode: bool) -> int:
    """The household's window clamped to ``window_choices``; in test mode the
    demo window (at least 60 s) when ``SENTRIX_SOS_DEMO_WINDOW_SECONDS`` is set."""
    demo = int(getattr(sos_cfg, "demo_window_seconds", 0) or 0)
    if test_mode and demo > 0:
        return max(60, demo)
    sos = (profile or {}).get("sos") if isinstance(profile, Mapping) else None
    chosen = (sos or {}).get("window_seconds") if isinstance(sos, Mapping) else None
    return clamp_window(chosen if chosen is not None else sos_cfg.window_seconds,
                        tuple(sos_cfg.window_choices))


# ── who may be told ───────────────────────────────────────────────────────────
def confirmed_contacts(profile: Mapping | None) -> list[dict[str, Any]]:
    """CONFIRMED contacts only, by priority (docs/7 §5.11: only they receive
    an SOS). Phone numbers are kept: the device texts them when offline."""
    contacts = []
    for entry in (profile or {}).get("contacts") or []:
        if not isinstance(entry, Mapping) or entry.get("consent_status") != "CONFIRMED":
            continue
        if not entry.get("id"):
            continue
        try:
            priority = int(entry.get("priority") or 99)
        except (TypeError, ValueError):
            priority = 99
        contacts.append({"id": str(entry["id"])[:64], "name": str(entry.get("name") or "")[:100],
                         "relation": str(entry.get("relation") or "")[:40],
                         "phone": entry.get("phone") or None,
                         "telegram": bool(entry.get("telegram")), "priority": priority,
                         "can_reach_30min": bool(entry.get("can_reach_30min"))})
    return sorted(contacts, key=lambda c: (c["priority"], c["name"]))


def _flag(sos: Mapping, key: str, default: bool) -> bool:
    value = sos.get(key)
    return default if value is None else bool(value)


def freeze(profile: Mapping | None, settings: Any, *, test_mode: bool,
           now: datetime | None = None, desk_phone: str | None = None) -> dict[str, Any]:
    """The ``policy_json`` of a new request.

    ``contacts_for_single_source_l3`` needs both the system gate
    (``SENTRIX_SOS_CONTACTS_SINGLE_SOURCE_L3``, on only once gate P8 passes)
    and the household not having opted out.
    """
    profile = profile if isinstance(profile, Mapping) else {}
    sos_cfg = settings.sos
    sos = profile.get("sos") if isinstance(profile.get("sos"), Mapping) else {}
    owner = profile.get("owner") if isinstance(profile.get("owner"), Mapping) else {}
    desk = profile.get("desk") if isinstance(profile.get("desk"), Mapping) else {}
    consents = profile.get("consents") if isinstance(profile.get("consents"), Mapping) else {}
    channels = sos.get("channels") if isinstance(sos.get("channels"), Mapping) else {}
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "version": POLICY_VERSION,
        "frozen_at": moment.isoformat(),
        "profile_version": int(profile.get("version") or 0),
        "standalone": bool(profile.get("standalone")),
        "test_mode": bool(test_mode),
        "window_seconds": window_seconds(profile, sos_cfg, test_mode=test_mode),
        "safety_defaults": _flag(sos, "safety_defaults", bool(sos_cfg.safety_defaults)),
        "contacts_for_single_source_l3": bool(sos_cfg.contacts_for_single_source_l3)
        and _flag(sos, "contacts_for_single_source_l3", True),
        "contacts": confirmed_contacts(profile),
        "owner": {"name": str(owner.get("name") or "")[:100] or None,
                  "phone": owner.get("phone") or None},
        "desk": {"name": str(desk.get("name") or "SENTRIX Desk")[:100],
                 "phone": desk_phone or desk.get("phone") or None,
                 "simulated": bool(desk.get("simulated", True))},
        "channels": {k: bool(v) for k, v in channels.items()},
        "consents": {k: bool(consents.get(k)) for k in
                     ("sos_snapshot", "medical_notes", "desk_live_view", "remote_view")},
        "has_duress_pin": bool(sos.get("duress_pin_hash")),
        "pin_version": int(sos.get("pin_version") or 0),
    }


def contact_ids(policy: Mapping | None) -> frozenset[str]:
    return frozenset(c["id"] for c in (policy or {}).get("contacts") or [] if c.get("id"))


def contacts_delay(policy: Mapping | None, *, incident_type: str, peak_level: int,
                   rules: Rules) -> tuple[float, str] | None:
    """(seconds after the request opened, reason) when the safety defaults
    alert the confirmed contacts, or None (owner only)."""
    policy = policy or {}
    if not policy.get("contacts"):
        return None
    if incident_type == "duress":
        return 0.0, "duress"
    if not policy.get("safety_defaults"):
        return None
    if incident_type == "intrusion":
        return 0.0, "armed_intrusion"
    if peak_level in rules.contacts_immediate_levels:
        return 0.0, "corroborated"
    if incident_type == "fire":
        return rules.fire_contacts_after, "confirmed_fire"
    if incident_type == "weapon" and policy.get("contacts_for_single_source_l3"):
        return rules.single_source_contacts_after, "single_source_weapon"
    return None


def recommended_authority(incident_type: str) -> str:
    """FIRE for fire, POLICE otherwise (the pre-portal dispatch rule). The
    owner may choose Police, Fire or Medical when approving."""
    return "FIRE" if incident_type == "fire" else "POLICE"


@dataclass(frozen=True)
class Reach:
    """How far the device can talk right now (the engine fills it in).

    ``online``: the Portal link is up, so the cloud sends the remote prompts
    and the contact/Desk messages. ``standalone``: no Portal is configured.
    ``remote_prompt``: the owner can be prompted somewhere other than the LC
    (the Portal while online; Telegram/e-mail configured on a standalone
    device). Offline means neither: the device's modem SMS is the way out.
    """

    online: bool = False
    standalone: bool = True
    remote_prompt: bool = False

    @property
    def offline(self) -> bool:
        return not self.online


__all__ = ["Rules", "Reach", "POLICY_VERSION", "clamp_window", "window_seconds",
           "confirmed_contacts", "freeze", "contact_ids", "contacts_delay",
           "recommended_authority"]
