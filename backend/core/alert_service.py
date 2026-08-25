# core/alert_service.py
#
# ARCHITECTURE: Sends Twilio SMS and voice call alerts.
# Gracefully degrades when credentials are missing — logs to console instead.
# send_sms_safe() and make_call_safe() never raise; they always fall back to print.
# send_sms_raw() sends a raw message string (used by DispatchService).
# Snapshot saving is also handled here for Level 2+ events.

import os
import time
import cv2
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from core.paths import ALERTS_DIR, ALERTS_DIR_STR

load_dotenv()

TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
FROM_NUMBER   = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
TO_NUMBER     = os.getenv("ALERT_PHONE_NUMBER", "").strip()
PUBLIC_URL    = os.getenv("PUBLIC_SERVER_URL", "http://127.0.0.1:8000").strip()

_TWILIO_AVAILABLE = bool(TWILIO_SID and TWILIO_TOKEN and FROM_NUMBER and TO_NUMBER)


def _get_twilio_client():
    if not _TWILIO_AVAILABLE:
        return None
    try:
        from twilio.rest import Client
        return Client(TWILIO_SID, TWILIO_TOKEN)
    except ImportError:
        print("[AlertService] twilio package not installed.")
        return None
    except Exception as e:
        print(f"[AlertService] Twilio init error: {e}")
        return None


class AlertService:
    """Twilio-backed SMS and voice call alerts with console fallback."""

    def __init__(self):
        self._client = _get_twilio_client()
        self.last_sms_time = 0
        self.last_sms_level = 0
        self.cooldown_seconds = 300  # 5 minutes for demo purposes
        if self._client:
            print("[AlertService] Twilio client ready.")
        else:
            print("[AlertService] No Twilio credentials — alerts will print to console.")

    def save_snapshot(self, frame, result=None) -> Optional[str]:
        """Save annotated frame as JPEG and return the public URL."""
        try:
            ALERTS_DIR.mkdir(parents=True, exist_ok=True)
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"alert_{ts}.jpg"
            path     = str(ALERTS_DIR / filename)
            cv2.imwrite(path, frame)
            return f"{PUBLIC_URL}/static/alerts/{filename}"
        except Exception as e:
            print(f"[AlertService] save_snapshot error: {e}")
            return None

    def _format_message(self, result):
        return (
            f"[SENTRIX ALERT]\n"
            f"Level {result.level} – {result.status}\n"
            f"TCI: {result.tci:.2f}\n"
            f"Incident: {result.incident_type.upper()}\n"
            f"Reason: {result.reason}"
        )

    def send_sms_safe(self, result, snapshot_url: Optional[str] = None):
        """Send SMS for a TCIResult. Falls back to console print."""
        now = time.time()
        
        # Always send if it's a Level 5 CRITICAL and we haven't sent a Level 5 recently
        if result.level == 5 and self.last_sms_level < 5:
            message = self._format_message(result)
            self._dispatch_sms(message, media_url=snapshot_url)
            self.last_sms_time = now
            self.last_sms_level = 5
            return
            
        # Rate limit everything else
        if (now - self.last_sms_time) < self.cooldown_seconds:
            print(f"[Alert] SMS throttled (Cooldown: {self.cooldown_seconds - int(now - self.last_sms_time)}s left)")
            return
            
        message = self._format_message(result)
        self._dispatch_sms(message, media_url=snapshot_url)
        self.last_sms_time = now
        self.last_sms_level = result.level

    def send_sms_raw(self, message: str):
        """Send an arbitrary SMS string. Falls back to console print."""
        self._dispatch_sms(message)

    def _dispatch_sms(self, message: str, media_url: Optional[str] = None):
        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            print(f"[AlertService-EVAL] Mock SMS dispatch")
            return
        if self._client:
            try:
                kwargs = dict(body=message, from_=FROM_NUMBER, to=TO_NUMBER)
                if media_url:
                    kwargs["media_url"] = [media_url]
                self._client.messages.create(**kwargs)
                print(f"[AlertService] SMS sent to {TO_NUMBER}")
            except Exception as e:
                print(f"[AlertService] SMS failed: {e}")
                print(f"[AlertService] SMS (console fallback):\n{message}")
        else:
            print(f"[AlertService] SMS (console fallback):\n{message}")

    def make_call_safe(self, result):
        """Make an automated Twilio voice call. Falls back to console print."""
        msg = (
            f"SENTRIX emergency alert. "
            f"Threat level {result.level}. {result.status}. "
            f"Incident type: {result.incident_type}. "
            f"TCI score: {int(result.tci * 100)} percent. "
            f"Immediate attention required."
        )
        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            print(f"[AlertService-EVAL] Mock Voice Call")
            return
        if self._client:
            try:
                twiml = f"<Response><Say>{msg}</Say></Response>"
                self._client.calls.create(twiml=twiml, from_=FROM_NUMBER, to=TO_NUMBER)
                print(f"[AlertService] Call initiated to {TO_NUMBER}")
            except Exception as e:
                print(f"[AlertService] Call failed: {e}")
                print(f"[AlertService] CALL (console fallback): {msg}")
        else:
            print(f"[AlertService] CALL (console fallback): {msg}")

    # Legacy API aliases
    def send_sms(self, message: str, media_url=None):
        self._dispatch_sms(message, media_url)

    def make_call(self, message: str):
        if self._client:
            try:
                twiml = f"<Response><Say>{message}</Say></Response>"
                self._client.calls.create(twiml=twiml, from_=FROM_NUMBER, to=TO_NUMBER)
            except Exception as e:
                print(f"[AlertService] make_call error: {e}")
        else:
            print(f"[AlertService] CALL (console fallback): {message}")

    def tick(self):
        """No-op: cooldown is now handled by system_engine with timestamps."""
        pass


# Module-level singleton
alert_service = AlertService()