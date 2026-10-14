"""Outbound notifications of the pre-portal path: snapshots, L4 SMS, L5 call,
dispatch "Send now".

Cooldowns are per-incident *and* per-channel. Previously the SMS path had a
special case — ``if result.level == 5 and self.last_sms_level < 5`` — that
bypassed the cooldown entirely, so a flapping level-5 signal could send
unbounded messages. Here every send is claimed against the incident (see
``IncidentManager.claim``), so a channel fires at most once per incident, with
the time-based cooldown as a second, independent guard.

**Nothing leaves the machine unless explicitly enabled** (decision D13). Every
SMS and call asks :mod:`sentrix.alerting.guard`: ``SENTRIX_LIVE_ALERTS=1``, the
channel's own switch and, in test mode (the default), an allowlisted number.
Otherwise the message is logged (or recorded SUPPRESSED) and nothing is sent.
``.env`` may hold live keys: a test, replay or rehearsal must never text
anyone by accident.

Two routes (docs/7 §8.2, §8.3):

* **Channel router** — when the ``channels`` service runs and has a phone
  channel (``modem_sms`` / ``twilio_sms``, ``modem_call`` / ``twilio_call``),
  the L4 SMS (``legacy.l4_sms``), the L5 call (``legacy.l5_call``) and the
  dispatch text (``legacy.dispatch``) are queued there for the owner: the
  profile's owner phone, else ``ALERT_PHONE_NUMBER``. The modem is preferred
  when it can send, then Twilio. The router keeps the old per-channel
  cooldowns (SMS 300 s, call 600 s), retries, and records every message; a
  dispatch send waits for its one attempt so "Send now" reports the outcome.
* **Direct Twilio** — otherwise, as before the portal: the Twilio client is
  built only when ``SENTRIX_TWILIO_ENABLED=1`` and every send passes the
  guard. With nothing enabled this is a logged no-op.

Once the SOS service owns an incident the pipeline does not call the L4 SMS
or the L5 call at all (``Pipeline._act``); the once-per-incident claims stay
the pipeline's.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sentrix.alerting import guard
from sentrix.alerting.channels.base import DELIVERED, SENT, Message, Recipient
from sentrix.alerting.channels.twilio import twiml_say
from sentrix.config import settings
from sentrix.paths import SNAPSHOT_DIR
from sentrix.utils.logging import get_logger
from sentrix.utils.redact import mask_recipient

log = get_logger("notifier")

try:
    import cv2
except ImportError:                                    # pragma: no cover
    cv2 = None                                         # type: ignore[assignment]


@dataclass
class Credentials:
    sid: str = ""
    token: str = ""
    from_number: str = ""
    to_number: str = ""

    @classmethod
    def from_env(cls) -> "Credentials":
        return cls(
            sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            from_number=os.getenv("TWILIO_PHONE_NUMBER", "").strip(),
            to_number=os.getenv("ALERT_PHONE_NUMBER", "").strip(),
        )

    @property
    def complete(self) -> bool:
        return all((self.sid, self.token, self.from_number, self.to_number))


def _router_service():
    """The running ``channels`` service, or None (imported lazily: the router
    module is not needed on the direct path)."""
    try:
        from sentrix.alerting.channels.router import current_service
        return current_service()
    except Exception as exc:                            # never block an alert path
        log.error("channel router unavailable: %s", exc)
        return None


class Notifier:
    """Owner alerts through the channel router when it runs, else Twilio
    directly; both behind the send guard; degrade to structured logs."""

    def __init__(self) -> None:
        self.cfg = settings.alert
        self.creds = Credentials.from_env()
        self._client = None
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        enabled = guard.SendPolicy.from_env().switches.get("twilio_sms", False)
        if not enabled:
            log.info("Twilio disabled (SENTRIX_TWILIO_ENABLED is off) — alerts "
                     "log to console")
        elif self.creds.complete:
            try:
                from twilio.rest import Client
                self._client = Client(self.creds.sid, self.creds.token)
                log.info("Twilio client ready (to %s); every send still passes "
                         "the send guard", mask_recipient(self.creds.to_number))
            except ImportError:
                log.warning("twilio not installed — alerts log to console")
            except Exception as exc:
                log.error("Twilio init failed: %s", exc)
        else:
            log.info("Twilio not configured — alerts log to console")

    @property
    def online(self) -> bool:
        """True only when a real SMS could leave right now."""
        service = _router_service()
        if service is not None and service.channels_for("sms"):
            target = service.owner_target()
            return target is not None and any(
                service.ready(name) and bool(guard.may_send(name, target[1]))
                for name in service.channels_for("sms"))
        return (self._client is not None
                and bool(guard.may_send("twilio_sms", self.creds.to_number)))

    def _route(self, kind: str, template: str, step: str, text: str, *,
               wait: bool = False) -> bool | None:
        """Hand the message to the channel router. None when no router runs
        or it has no phone channel of ``kind`` ("sms" | "call"): the caller
        takes the direct path. ``wait``: deliver now and report the outcome."""
        service = _router_service()
        if service is None:
            return None
        names = service.channels_for(kind)
        if not names:
            return None
        target = service.owner_target()
        if target is None:
            log.warning("%s not sent: no owner number (profile or ALERT_PHONE_NUMBER)",
                        template)
            return False
        name, phone = target
        recipient = Recipient("owner", name, names[0], phone, "owner")
        message = Message(template=template, step=f"{step}:{int(time.time() * 1000)}",
                          text=text)
        try:
            if wait:
                item = service.send_now(recipient, message)
                return item is not None and item.status in (SENT, DELIVERED)
            return service.enqueue(recipient, message) is not None
        except Exception as exc:
            log.error("%s via the channel router failed: %s", template, exc)
            return False

    def _guarded(self, channel: str, body: str) -> tuple[bool, str]:
        """(may send, text to send). Logs the reason and a console copy if not."""
        decision = guard.may_send(channel, self.creds.to_number)
        text = guard.prepare_text(channel, body)
        if self._client is None or not decision:
            reason = ("Twilio client not built" if self._client is None
                      else decision.reason)
            log.warning("%s suppressed (%s) — console copy:\n%s", channel, reason, text)
            return False, text
        return True, text

    def _cooldown_ok(self, channel: str, seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(channel, -1e9) < seconds:
                return False
            self._last[channel] = now
            return True

    # ── snapshot ──────────────────────────────────────────────────────────────
    def save_snapshot(self, frame, incident_id: str | None = None) -> Path | None:
        if cv2 is None or frame is None:
            return None
        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            millis = int((time.time() % 1) * 1000)
            tag = (incident_id or "none")[:8]
            path = SNAPSHOT_DIR / f"snap_{stamp}_{millis:03d}_{tag}.jpg"
            cv2.imwrite(str(path), frame)
            return path
        except Exception as exc:
            log.error("snapshot failed: %s", exc)
            return None

    # ── messaging ─────────────────────────────────────────────────────────────
    @staticmethod
    def _format(result, level: int, status: str) -> str:
        features = result.features
        return (f"[SENTRIX] Level {level} {status}\n"
                f"TCI {result.tci:.2f} | {result.incident_type.upper()}\n"
                f"{result.reason}\n"
                f"weapon={features.weapon:.2f} fire={features.fire:.2f} "
                f"audio={features.audio:.2f}")

    def send_sms(self, result, level: int, status: str) -> bool:
        """L4 owner SMS. Routed: True once queued (the router owns the
        cooldown and the retries); direct: True once Twilio accepted it."""
        routed = self._route("sms", "legacy.l4_sms", "l4", self._format(result, level, status))
        if routed is not None:
            return routed
        if not self._cooldown_ok("sms", self.cfg.sms_cooldown_seconds):
            log.debug("SMS suppressed by cooldown")
            return False
        ok, body = self._guarded("twilio_sms", self._format(result, level, status))
        if not ok:
            return False
        try:
            self._client.messages.create(body=body, from_=self.creds.from_number,
                                         to=self.creds.to_number)
            log.info("SMS sent (level %d)", level)
            return True
        except Exception as exc:
            log.error("SMS failed: %s", exc)
            return False

    def make_call(self, result, level: int, status: str) -> bool:
        """L5 owner call (Twilio reads the text aloud; the modem only rings)."""
        speech = (f"Sentrix critical alert. Level {level}. "
                  f"{result.incident_type}. {result.reason}.")
        routed = self._route("call", "legacy.l5_call", "l5", speech)
        if routed is not None:
            return routed
        if not self._cooldown_ok("call", self.cfg.call_cooldown_seconds):
            return False
        ok, message = self._guarded("twilio_call", speech)
        if not ok:
            return False
        try:
            # The reason is free text: escaped, or "<" / "&" break the TwiML.
            self._client.calls.create(twiml=twiml_say(message), from_=self.creds.from_number,
                                      to=self.creds.to_number)
            log.info("voice call placed (level %d)", level)
            return True
        except Exception as exc:
            log.error("voice call failed: %s", exc)
            return False

    def send_raw(self, body: str) -> bool:
        """The dispatch package text, operator-confirmed: True only when it
        left (routed: after its one immediate attempt)."""
        routed = self._route("sms", "legacy.dispatch", "dispatch", body, wait=True)
        if routed is not None:
            return routed
        ok, body = self._guarded("twilio_sms", body)
        if not ok:
            return False
        try:
            self._client.messages.create(body=body, from_=self.creds.from_number,
                                         to=self.creds.to_number)
            return True
        except Exception as exc:
            log.error("SMS failed: %s", exc)
            return False


notifier = Notifier()

__all__ = ["Notifier", "Credentials", "notifier"]
