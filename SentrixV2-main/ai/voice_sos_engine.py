# ai/voice_sos_engine.py
#
# ARCHITECTURE: Optional voice command listener using VOSK speech recognition.
# Listens in a background thread. Returns "EMERGENCY", "SIREN", or None.
# Falls back silently if VOSK or sounddevice is not installed.
# get_command_safe() is non-blocking — reads latest recognised command and resets.

import json
import os
import queue
import threading
import time
from typing import Optional

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except ImportError:
    _SD_AVAILABLE = False

try:
    from vosk import KaldiRecognizer, Model
    _VOSK_AVAILABLE = True
except ImportError:
    _VOSK_AVAILABLE = False

VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", "vosk-model")
SAMPLE_RATE     = 16000
TRIGGER_WORDS   = {
    "emergency": "EMERGENCY",
    "help":      "EMERGENCY",
    "sos":       "EMERGENCY",
    "siren":     "SIREN",
    "alarm":     "SIREN",
}


class VoiceSosEngine:
    """
    Background voice command listener.
    Recognises trigger phrases and exposes the latest command via get_command_safe().
    """

    def __init__(self):
        self._command: Optional[str] = None
        self._lock    = threading.Lock()
        self._available = False
        self._stop    = threading.Event()  # Graceful shutdown signal

        if not _VOSK_AVAILABLE or not _SD_AVAILABLE:
            reason = []
            if not _VOSK_AVAILABLE:
                reason.append("vosk")
            if not _SD_AVAILABLE:
                reason.append("sounddevice")
            print(f"[VoiceSosEngine] {' and '.join(reason)} not installed. Voice SOS disabled.")
            return

        if not os.path.isdir(VOSK_MODEL_PATH):
            print(f"[VoiceSosEngine] VOSK model not found at '{VOSK_MODEL_PATH}'. Voice SOS disabled.")
            return

        self._available = True
        t = threading.Thread(target=self._listen_loop, daemon=True)
        t.start()
        print("[VoiceSosEngine] Voice SOS active. Say 'emergency' to trigger Level 5.")

    def stop(self):
        """Signal the background listen thread to exit cleanly."""
        self._stop.set()

    def _listen_loop(self):
        try:
            model = Model(VOSK_MODEL_PATH)
            rec   = KaldiRecognizer(model, SAMPLE_RATE)
            q: queue.Queue = queue.Queue()

            def callback(indata, frames, time_info, status):
                q.put(bytes(indata))

            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=8000,
                dtype="int16",
                channels=1,
                callback=callback,
            ):
                while not self._stop.is_set():
                    try:
                        data = q.get(timeout=1)
                    except queue.Empty:
                        continue

                    if rec.AcceptWaveform(data):
                        res  = json.loads(rec.Result())
                        text = res.get("text", "").lower()
                        for word, cmd in TRIGGER_WORDS.items():
                            if word in text:
                                with self._lock:
                                    self._command = cmd
                                print(f"[VoiceSosEngine] Command detected: {cmd} ('{text}')")
                                break
        except Exception as e:
            print(f"[VoiceSosEngine] Listen loop error: {e}. Voice SOS disabled.")
            self._available = False

    def get_command_safe(self) -> Optional[str]:
        """
        Returns the latest recognised command string, then resets it.
        Non-blocking — safe to call from the processing loop.
        """
        with self._lock:
            cmd = self._command
            self._command = None
            return cmd

    # Legacy alias
    def get_command(self) -> Optional[str]:
        return self.get_command_safe()