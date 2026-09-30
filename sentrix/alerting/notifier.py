"""Outbound notifications: snapshots, SMS, voice calls.

Cooldowns are per-incident *and* per-channel. Previously the SMS path had a
special case — ``if result.level == 5 and self.last_sms_level < 5`` — that
bypassed the cooldown entirely, so a flapping level-5 signal could send
unbounded messages. Here every send is claimed against the incident (see
``IncidentManager.claim``), so a channel fires at most once per incident, with
the time-based cooldown as a second, independent guard.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import settings
from sentrix.paths import SNAPSHOT_DIR
from sentrix.utils.logging import get_logger

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


class Notifier:
    """Twilio-backed alerts that degrade to structured logs."""

    def __init__(self) -> None:
        self.cfg = settings.alert
        self.creds = Credentials.from_env()
        self._client = None
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        if self.creds.complete:
            try:
                from twilio.rest import Client
                self._client = Client(self.creds.sid, self.creds.token)
                log.info("Twilio ready (to %s)", self.creds.to_number)
            except ImportError:
                log.warning("twilio not installed — alerts log to console")
            except Exception as exc:
                log.error("Twilio init failed: %s", exc)
        else:
            log.info("Twilio not configured — alerts log to console")

    @property
    def online(self) -> bool:
        return self._client is not None

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
        if not self._cooldown_ok("sms", self.cfg.sms_cooldown_seconds):
            log.debug("SMS suppressed by cooldown")
            return False
        body = self._format(result, level, status)
        if self._client is None:
            log.warning("SMS (console fallback):\n%s", body)
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
        if not self._cooldown_ok("call", self.cfg.call_cooldown_seconds):
            return False
        message = (f"Sentrix critical alert. Level {level}. "
                   f"{result.incident_type}. {result.reason}.")
        if self._client is None:
            log.warning("VOICE CALL (console fallback): %s", message)
            return False
        try:
            twiml = f'<Response><Say voice="alice">{message}</Say></Response>'
            self._client.calls.create(twiml=twiml, from_=self.creds.from_number,
                                      to=self.creds.to_number)
            log.info("voice call placed (level %d)", level)
            return True
        except Exception as exc:
            log.error("voice call failed: %s", exc)
            return False

    def send_raw(self, body: str) -> bool:
        if self._client is None:
            log.warning("SMS (console fallback): %s", body)
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
