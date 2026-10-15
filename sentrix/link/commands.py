"""Link commands from the Portal, with the rules the device enforces itself.

The device does not trust the Portal (docs/7 §5.6). Every ``command`` carries
the acting :class:`~sentrix.actor.Actor`; this module checks the device's own
rule for that command, calls exactly one method (mostly on the SOS service,
``sentrix.sos.current_service()``), maps its result to ``command.result``
fields and writes one ``remote_audit`` row. It is a thin wrapper: no request
store access, no PIN handling (the SOS service checks PINs against the
profile's hashes and never logs them; neither does this module).

Allowlist (``protocol.COMMANDS``); anything else is ``unknown_command``. No
command runs a shell or code, turns the siren off or touches the evidence
vault.

==================== =============================================================
``sos.respond``      owner/member; a contact may only ``approve`` (the SOS service
                     also checks the request's frozen safety-defaults policy)
``sos.update``       cloud, operator (Desk) or contact; ``SOS_UPDATE_EVENTS`` only
``verdict.set``      owner/member; FALSE goes through ``sos.false_verdict`` (PIN)
``mode.set``         owner/member; disarming needs the SOS PIN (``sos.set_mode``)
``siren.test``       owner with a step-up <= 300 s; 1 s, once a minute process-wide
``sms.send``         owner, Desk or cloud; only numbers in the profile's allowlist
                     (owner, CONFIRMED contacts, Desk), <= 30 a day, never while
                     the send guard refuses it
``token.rotate``     owner with a step-up (the Portal then sends ``token.issue``)
``unpair``           owner with a step-up: token and profile cache wiped
``diag.ping``        anyone with tunnel access
``demo.*``           test mode only
==================== =============================================================
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sentrix.actor import Actor
from sentrix.link import protocol
from sentrix.link.policy import STEP_UP_SECONDS
from sentrix.utils.logging import get_logger

log = get_logger("link.commands")

#: Actor types allowed per command (docs/7 §5.6).
ALLOWED_ACTORS: dict[str, frozenset] = {
    "sos.respond": frozenset({"owner", "member", "contact"}),
    "sos.update": frozenset({"cloud", "operator", "contact"}),
    "verdict.set": frozenset({"owner", "member"}),
    "mode.set": frozenset({"owner", "member"}),
    "siren.test": frozenset({"owner"}),
    "sms.send": frozenset({"owner", "operator", "cloud"}),
    "token.rotate": frozenset({"owner"}),
    "unpair": frozenset({"owner"}),
    "diag.ping": frozenset({"owner", "member", "operator", "cloud"}),
    "demo.close_incident": frozenset({"owner", "member", "operator", "cloud"}),
    "demo.trigger": frozenset({"owner", "member", "operator", "cloud"}),
}
STEP_UP_COMMANDS = frozenset({"siren.test", "token.rotate", "unpair"})
TEST_MODE_COMMANDS = frozenset({"demo.close_incident", "demo.trigger"})
#: The service refuses "intrusion" unless SENTRIX_SOS_INTRUSION=1 (D15).
DEMO_TRIGGER_TYPES = ("weapon", "fire", "assault", "intrusion")
#: The SMS text the device sends on the cloud's behalf (4 GSM segments).
SMS_MAX_CHARS = 612
#: Default answer channel by actor type (``sentrix.sos.states.CHANNELS``).
_CHANNEL_FOR = {"owner": "portal", "member": "portal", "contact": "contact_page",
                "operator": "desk", "cloud": "system"}


def outcome(code: str = "ok", message: str = "", result: Any = None) -> dict[str, Any]:
    """``command.result`` fields: ``{ok, code, status, message, result}``."""
    status = 200 if code == "ok" else protocol.ERROR_CODES.get(code, 400)
    return {"ok": code == "ok", "code": code, "status": status, "message": message,
            "result": result}


def _from_sos(result: dict[str, Any], extra: dict | None = None) -> dict[str, Any]:
    """A ``SosResult`` as command-result fields (the request in the caller's
    view, as the SOS service returned it)."""
    body = {"request": result.get("request")}
    if extra:
        body.update(extra)
    return outcome(result.get("code", "internal"), result.get("message", ""), body)


@dataclass
class CommandHooks:
    """What the handlers need from the running device."""

    #: The live pipeline (siren). Raises when it cannot be built.
    get_pipeline: Callable[[], Any] = lambda: None
    #: The channels service (``services.registry.get("channels")``), or None.
    channels: Callable[[], Any] = lambda: None
    #: Wipe the device token (``unpair``); the profile is wiped here.
    wipe_token: Callable[[], None] = lambda: None
    #: Seconds since the link service started.
    uptime: Callable[[], float] = lambda: 0.0
    test_mode: Callable[[], bool] = field(default=lambda: _test_mode())


def _test_mode() -> bool:
    from sentrix.config import settings
    return bool(settings.test_mode)


class CommandHandler:
    """Runs one ``command`` message (call it from a worker thread: SOS calls
    block briefly on SQLite). ``handle`` never raises."""

    def __init__(self, hooks: CommandHooks | None = None, *, cfg: Any = None,
                 audit: Callable[..., Any] | None = None,
                 sos: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        if cfg is None:
            from sentrix.config import settings
            cfg = settings.link
        if audit is None:
            from sentrix.storage import link_store
            audit = link_store.record_remote
        if sos is None:
            from sentrix.sos import current_service
            sos = current_service
        self.hooks = hooks or CommandHooks()
        self.cfg = cfg
        self._audit = audit
        self._sos = sos
        self._clock = clock

    # ── entry point ───────────────────────────────────────────────────────────
    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        name = str(message.get("name") or "")
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        actor: Actor | None = None
        try:
            actor = Actor.from_dict(message.get("actor"))
            result = self._run(name, args, actor, message)
        except ValueError as exc:
            result = outcome("invalid_args", str(exc))
        except Exception as exc:              # a handler bug must not kill the link
            log.error("command %s failed: %s", name, exc, exc_info=True)
            result = outcome("internal", "the device could not run the command")
        self._record(name, actor, result, message)
        return result

    def _run(self, name: str, args: dict, actor: Actor, message: dict) -> dict[str, Any]:
        if name not in protocol.COMMANDS:
            return outcome("unknown_command", f"{name or '?'} is not a device command")
        expires = message.get("expires_at")
        if isinstance(expires, (int, float)) and expires < self._clock() * 1000:
            return outcome("timeout", "the command expired before it arrived")
        if actor.type not in ALLOWED_ACTORS[name]:
            return outcome("forbidden_role", f"{actor.type} may not use {name}")
        if name in STEP_UP_COMMANDS and not actor.stepped_up_within(
                STEP_UP_SECONDS, self._clock()):
            return outcome("step_up_required", "re-enter your password first")
        if name in TEST_MODE_COMMANDS and not self.hooks.test_mode():
            return outcome("test_mode_only", f"{name} exists only in test mode")
        handler = getattr(self, "_cmd_" + name.replace(".", "_"))
        return handler(args, actor, message)

    def _record(self, name: str, actor: Actor | None, result: dict, message: dict) -> None:
        try:
            self._audit(kind="command", target=name[:64] or "?", allowed=bool(result["ok"]),
                        user_id=actor.id if actor else None,
                        user_name=actor.name if actor else None,
                        role=actor.type if actor else None, status=result["status"],
                        reason=None if result["ok"] else result["code"],
                        request_id=str(message.get("id") or "") or None)
        except Exception as exc:
            log.error("remote audit failed: %s", exc)

    # ── SOS ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _channel(args: dict, actor: Actor) -> str:
        from sentrix.sos.states import CHANNELS
        channel = args.get("channel")
        return channel if channel in CHANNELS else _CHANNEL_FOR.get(actor.type, "portal")

    def _cmd_sos_respond(self, args: dict, actor: Actor, message: dict) -> dict:
        request_id = _text(args, "request_id")
        action = args.get("action")
        if action not in protocol.SOS_RESPOND_ACTIONS:
            raise ValueError(f"action must be one of {', '.join(protocol.SOS_RESPOND_ACTIONS)}")
        if actor.type == "contact" and action != "approve":
            return outcome("forbidden_role", "a contact may only ask for help")
        authority = args.get("authority")
        if authority is not None:
            from sentrix.sos.states import AUTHORITIES
            authority = str(authority).upper()
            if authority not in AUTHORITIES:
                raise ValueError(f"authority must be one of {', '.join(AUTHORITIES)}")
        pin = args.get("pin")
        result = self._sos().respond(request_id, action, actor=actor,
                                     channel=self._channel(args, actor), authority=authority,
                                     pin=pin if isinstance(pin, str) else None)
        return _from_sos(result)

    def _cmd_sos_update(self, args: dict, actor: Actor, message: dict) -> dict:
        request_id = _text(args, "request_id")
        event = args.get("event")
        if event not in protocol.SOS_UPDATE_EVENTS:
            raise ValueError(f"event must be one of {', '.join(protocol.SOS_UPDATE_EVENTS)}")
        detail = args.get("detail") if isinstance(args.get("detail"), dict) else None
        return _from_sos(self._sos().cloud_event(request_id, event, actor=actor, detail=detail))

    def _cmd_verdict_set(self, args: dict, actor: Actor, message: dict) -> dict:
        from sentrix.storage import database
        from sentrix.web.security import sanitize_filename
        incident_id = sanitize_filename(_text(args, "incident_id"))
        verdict = str(args.get("verdict") or "").upper()
        note = args.get("note") if isinstance(args.get("note"), str) else None
        note = note[:500] if note else None
        channel = self._channel(args, actor)
        if verdict == "FALSE":
            pin = args.get("pin")
            return _from_sos(self._sos().false_verdict(
                incident_id, pin=pin if isinstance(pin, str) else None, actor=actor,
                channel=channel, note=note))
        if verdict != "TRUE":
            raise ValueError("verdict must be TRUE or FALSE")
        ok = database.set_verdict(incident_id, "TRUE", note, by=actor.ref, channel=channel,
                                  actor_type=actor.event_actor_type)
        return outcome() if ok else outcome("not_found", "unknown incident")

    def _cmd_mode_set(self, args: dict, actor: Actor, message: dict) -> dict:
        from sentrix import mode
        armed = args.get("armed")
        if not isinstance(armed, bool):
            raise ValueError("armed must be true or false")
        pin = args.get("pin")
        result = self._sos().set_mode(armed, pin=pin if isinstance(pin, str) else None,
                                      actor=actor)
        return _from_sos(result, {"mode": mode.snapshot()})

    def _cmd_demo_close_incident(self, args: dict, actor: Actor, message: dict) -> dict:
        return _from_sos(self._sos().demo_close_incident(actor=actor))

    def _cmd_demo_trigger(self, args: dict, actor: Actor, message: dict) -> dict:
        kind = args.get("incident_type")
        if kind not in DEMO_TRIGGER_TYPES:
            raise ValueError(f"incident_type must be one of {', '.join(DEMO_TRIGGER_TYPES)}")
        return _from_sos(self._sos().demo_trigger(kind, actor=actor))

    # ── device ────────────────────────────────────────────────────────────────
    def _cmd_siren_test(self, args: dict, actor: Actor, message: dict) -> dict:
        from sentrix.io.actuators import siren_test
        try:
            pipeline = self.hooks.get_pipeline()
        except Exception as exc:
            return outcome("internal", f"no pipeline: {exc}")
        siren = getattr(pipeline, "siren", None)
        if siren is None:
            return outcome("unsupported", "this device has no siren")
        result = siren_test(siren, actor=actor)
        fields = {k: result.get(k) for k in ("seconds", "backend", "is_real", "retry_after")}
        return outcome(result.get("code", "internal"), result.get("message", ""), fields)

    def _cmd_sms_send(self, args: dict, actor: Actor, message: dict) -> dict:
        from sentrix.alerting import guard
        from sentrix.alerting.channels.base import Message, Recipient
        from sentrix.link import profile
        from sentrix.storage import link_store

        to = guard.normalize_address("modem_sms", _text(args, "to"))
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required")
        request_id = args.get("request_id") if isinstance(args.get("request_id"), str) else None
        current = profile.effective()
        if to not in profile.sms_allowlist(current):
            return outcome("not_allowed_recipient",
                           "only the owner, confirmed contacts and the Desk can be texted")
        since = datetime.now(timezone.utc) - timedelta(days=1)
        sent_today = link_store.count_remote(kind="command", target="sms.send", since=since)
        if sent_today >= self.cfg.sms_daily_cap:
            return outcome("daily_cap", f"at most {self.cfg.sms_daily_cap} texts a day")
        decision = guard.may_send("modem_sms", to)
        if not decision:
            return outcome("not_allowed_recipient", f"refused by the send guard: "
                                                    f"{decision.reason}")
        channels = self.hooks.channels()
        if channels is None:
            return outcome("unsupported", "the channel router is off")
        ref, name, role = _recipient_for(current, to)
        item = channels.enqueue(
            Recipient(ref=ref, name=name, channel="modem_sms", address=to, role=role),
            Message(template="sos.sms", step=f"cloud:{message.get('id') or time.time_ns()}",
                    text=text.strip()[:SMS_MAX_CHARS], request_id=request_id))
        if item is None:
            return outcome("conflict", "this text was already queued")
        return outcome(result={"notification_id": item})

    def _cmd_token_rotate(self, args: dict, actor: Actor, message: dict) -> dict:
        # The device cannot mint its own token (the Portal stores only the
        # digest); agreeing here lets the Portal follow with ``token.issue``.
        return outcome(result={"rotate": True})

    def _cmd_unpair(self, args: dict, actor: Actor, message: dict) -> dict:
        from sentrix.link import profile
        self.hooks.wipe_token()
        removed = profile.wipe()
        log.warning("unpaired by %s: token and %d cached profile(s) wiped", actor.ref, removed)
        return outcome(result={"profiles_wiped": removed})

    def _cmd_diag_ping(self, args: dict, actor: Actor, message: dict) -> dict:
        return outcome(result={"pong": True, "time_ms": protocol.now_ms(),
                               "uptime_s": round(self.hooks.uptime(), 1),
                               "protocol": protocol.VERSION})


def _text(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()[:128]


def _recipient_for(profile: dict, number: str) -> tuple[str, str, str]:
    """(ref, name, role) of an allowlisted number in the profile."""
    from sentrix.alerting import guard
    from sentrix.link.profile import desk_number

    def same(value: Any) -> bool:
        return bool(value) and guard.normalize_address("modem_sms", value) == number

    owner = profile.get("owner") or {}
    if same(owner.get("phone")):
        return "owner", owner.get("name") or "Owner", "owner"
    for contact in profile.get("contacts") or []:
        if isinstance(contact, dict) and same(contact.get("phone")):
            return (f"contact:{contact.get('id') or '?'}", contact.get("name") or "Contact",
                    "contact")
    if same(desk_number(profile)):
        return "desk", "SENTRIX Desk", "desk"
    return "owner", "Owner", "owner"


__all__ = ["CommandHandler", "CommandHooks", "outcome", "ALLOWED_ACTORS",
           "STEP_UP_COMMANDS", "TEST_MODE_COMMANDS", "SMS_MAX_CHARS"]
