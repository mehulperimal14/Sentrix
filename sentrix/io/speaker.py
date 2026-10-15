"""Talk-down speaker: the owner's words, spoken in the room (decision D16).

The owner (through the Portal tunnel) or the device admin (on the LAN) types a
message or picks a preset on the dashboard; the device says it through its
speaker with offline text-to-speech. No audio leaves the house and no cloud
TTS is used.

Backends (``settings.speaker.backend``):

* ``pyttsx3`` - SAPI5 on Windows, NSSpeechSynthesizer on macOS, espeak(-ng)
  on Linux (``pip install pyttsx3``);
* ``espeak`` - the ``espeak-ng`` command (``apt install espeak-ng``), piped to
  ``aplay -D <device>`` when ``SENTRIX_SPEAKER_DEVICE`` names an ALSA device
  (a USB audio adapter on the Pi 5, which has no headphone jack);
* ``console`` - logs the text only (no sound);
* ``auto`` - pyttsx3 if importable, else espeak-ng if on PATH, else console.

Rules (:meth:`Speaker.say`): off unless ``SENTRIX_SPEAKER_ENABLED=1``; text at
most ``max_chars`` (200) printable characters, or a preset id; at most
``per_minute`` messages (3) for the whole device; one message at a time; every
message is written to the event log as ``speaker_say`` (who, preset, text),
which the link mirrors to the Portal's audit trail. Who may speak is the
tunnel path table's rule (owner + step-up, ``sentrix/link/policy.py``) and the
device admin on the LAN; members and the Desk may not.

Tests never make a sound: ``tests/conftest.py`` forces the speaker off and the
console backend, and blocks both real players.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from sentrix.config import settings
from sentrix.utils.logging import get_logger

log = get_logger("speaker")

#: Preset messages (id -> text). Plain, calm and short: they must be
#: understood through a 3 W speaker by someone who did not expect them.
PRESETS: dict[str, str] = {
    "leave": "You are being recorded. Leave the premises now.",
    "help": "Help is on the way.",
    "owner": "This is SENTRIX. The owner can hear you.",
    "police": "The police have been informed.",
    "check": "Is everyone all right? Please answer.",
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"\s+")


def clean_text(text: Any, limit: int | None = None) -> str:
    """Printable, single-line, trimmed; control characters removed."""
    limit = settings.speaker.max_chars if limit is None else limit
    value = _SPACES.sub(" ", _CONTROL.sub(" ", str(text or ""))).strip()
    return value[: max(0, int(limit)) + 1]          # one over: lets say() refuse


class Player:
    """Speaks one text, blocking until it has been said."""

    name = "none"
    real = False

    def play(self, text: str) -> None:              # pragma: no cover - interface
        raise NotImplementedError


class ConsolePlayer(Player):
    name = "console"

    def play(self, text: str) -> None:
        log.warning("SPEAKER (console, no sound): %s", text)


class Pyttsx3Player(Player):
    """pyttsx3: a fresh engine per message (its loop is not thread-safe)."""

    name = "pyttsx3"
    real = True

    def __init__(self, rate_wpm: int, volume: float) -> None:
        import pyttsx3  # noqa: F401  (fail at build time, not at the first message)
        self.rate_wpm, self.volume = int(rate_wpm), float(volume)

    def play(self, text: str) -> None:
        import pyttsx3
        engine = pyttsx3.init()
        try:
            engine.setProperty("rate", self.rate_wpm)
            engine.setProperty("volume", min(max(self.volume, 0.0), 1.0))
            engine.say(text)
            engine.runAndWait()
        finally:
            try:
                engine.stop()
            except Exception:
                pass


class EspeakPlayer(Player):
    """The ``espeak-ng`` command, optionally through ``aplay -D <device>``."""

    name = "espeak-ng"
    real = True

    def __init__(self, binary: str, rate_wpm: int, volume: float, device: str = "") -> None:
        self.binary, self.rate_wpm, self.volume, self.device = binary, rate_wpm, volume, device

    def command(self, text: str) -> list[str]:
        amplitude = str(int(min(max(self.volume, 0.0), 1.0) * 200))
        cmd = [self.binary, "-a", amplitude, "-s", str(int(self.rate_wpm))]
        if self.device:
            cmd.append("--stdout")
        return cmd + ["--", text]

    def play(self, text: str) -> None:
        if not self.device:
            subprocess.run(self.command(text), check=False, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        speak = subprocess.Popen(self.command(text), stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL)
        try:
            subprocess.run(["aplay", "-q", "-D", self.device], stdin=speak.stdout,
                           check=False, timeout=60, stderr=subprocess.DEVNULL)
        finally:
            if speak.stdout is not None:
                speak.stdout.close()
            speak.wait(timeout=5)


def build_player(cfg=None) -> Player:
    cfg = cfg or settings.speaker
    choice = cfg.backend
    if choice in ("auto", "pyttsx3"):
        try:
            return Pyttsx3Player(cfg.rate_wpm, cfg.volume)
        except Exception as exc:
            if choice == "pyttsx3":
                log.warning("pyttsx3 unavailable (%s); speaker on console", exc)
                return ConsolePlayer()
    if choice in ("auto", "espeak"):
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if binary:
            return EspeakPlayer(binary, cfg.rate_wpm, cfg.volume, cfg.device)
        if choice == "espeak":
            log.warning("espeak-ng not found; speaker on console")
    return ConsolePlayer()


@dataclass
class SayResult:
    ok: bool
    code: str = "ok"
    message: str = ""
    text: str = ""
    preset: str | None = None
    retry_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message,
                "text": self.text, "preset": self.preset, "retry_after": self.retry_after}


class Speaker:
    """Rate-limited, single-flight, audited text-to-speech."""

    def __init__(self, cfg=None, *, player: Player | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 audit: Callable[..., None] | None = None,
                 background: bool = True) -> None:
        self.cfg = cfg or settings.speaker
        self.player = player if player is not None else build_player(self.cfg)
        self._clock = clock
        self._audit = audit or _default_audit
        self._background = background
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()
        self._speaking = False
        self._last: dict[str, Any] | None = None

    # ── status ────────────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": bool(self.cfg.enabled), "backend": self.player.name,
                    "real": bool(self.player.real),
                    "ready": bool(self.cfg.enabled and self.player.real),
                    "speaking": self._speaking, "last": self._last,
                    "max_chars": int(self.cfg.max_chars),
                    "per_minute": int(self.cfg.per_minute),
                    "retry_after": self._retry_after_locked(self._clock()),
                    "presets": [{"id": k, "text": v} for k, v in PRESETS.items()]}

    def _retry_after_locked(self, now: float) -> float | None:
        while self._sent and now - self._sent[0] >= 60.0:
            self._sent.popleft()
        if len(self._sent) < max(1, int(self.cfg.per_minute)):
            return None
        return round(60.0 - (now - self._sent[0]), 1)

    # ── speaking ──────────────────────────────────────────────────────────────
    def say(self, text: str | None = None, preset_id: str | None = None, *,
            actor: Any = None) -> SayResult:
        """Validate, rate-limit, audit, then speak (on a worker thread)."""
        if not self.cfg.enabled:
            return SayResult(False, "conflict",
                             "the speaker is off (SENTRIX_SPEAKER_ENABLED=0)")
        preset = None
        if preset_id:
            preset = str(preset_id)
            if preset not in PRESETS:
                return SayResult(False, "invalid_args", f"unknown preset {preset!r}")
            phrase = PRESETS[preset]
        else:
            phrase = clean_text(text)
            if not phrase:
                return SayResult(False, "invalid_args", "say something (text or preset_id)")
            if len(phrase) > int(self.cfg.max_chars):
                return SayResult(False, "invalid_args",
                                 f"at most {int(self.cfg.max_chars)} characters")
        now = self._clock()
        by = getattr(actor, "ref", None) or (str(actor) if actor is not None else "unknown")
        with self._lock:
            if self._speaking:
                return SayResult(False, "conflict", "the speaker is still talking",
                                 text=phrase, preset=preset)
            wait = self._retry_after_locked(now)
            if wait is not None:
                return SayResult(False, "rate_limited",
                                 f"at most {int(self.cfg.per_minute)} messages a minute",
                                 text=phrase, preset=preset, retry_after=wait)
            self._sent.append(now)
            self._speaking = True
            self._last = {"text": phrase, "preset": preset, "by": by,
                          "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            self._audit("speaker_say", by=by, preset=preset, chars=len(phrase), text=phrase,
                        backend=self.player.name)
        except Exception as exc:                    # audit must not block a talk-down
            log.debug("speaker audit failed: %s", exc)
        if self._background:
            threading.Thread(target=self._speak, args=(phrase,), daemon=True,
                             name="sentrix-speaker").start()
        else:
            self._speak(phrase)
        log.warning("speaker: %s says %r (%s)", by, phrase, self.player.name)
        return SayResult(True, "ok", "" if self.player.real else
                         "no text-to-speech backend: logged only", text=phrase, preset=preset)

    def _speak(self, phrase: str) -> None:
        try:
            for _ in range(max(1, int(self.cfg.repeat))):
                self.player.play(phrase)
        except Exception as exc:
            log.error("speaker failed: %s", exc)
        finally:
            with self._lock:
                self._speaking = False
            _publish_state(self)


def _default_audit(event: str, **detail: Any) -> None:
    engine = sys.modules.get("sentrix.pipeline.engine")
    pipeline = getattr(engine, "_pipeline", None) if engine is not None else None
    events = getattr(pipeline, "events", None)
    if events is not None:
        events.audit(event, **detail)


def _publish_state(speaker: Speaker) -> None:
    try:
        from sentrix.pipeline.state import state
        status = speaker.status()
        state.update(speaker={k: status[k] for k in ("enabled", "backend", "ready",
                                                     "speaking", "last")})
    except Exception as exc:
        log.debug("speaker state not published: %s", exc)


_speaker: Speaker | None = None
_speaker_lock = threading.Lock()


def get_speaker() -> Speaker:
    """The process's speaker, built on first use (never at import)."""
    global _speaker
    with _speaker_lock:
        if _speaker is None:
            _speaker = Speaker()
            _publish_state(_speaker)
        return _speaker


def _reset_speaker(speaker: Speaker | None = None) -> None:
    """Tests only: replace (or forget) the process's speaker."""
    global _speaker
    with _speaker_lock:
        _speaker = speaker


__all__ = ["ConsolePlayer", "EspeakPlayer", "PRESETS", "Player", "Pyttsx3Player",
           "SayResult", "Speaker", "build_player", "clean_text", "get_speaker"]
