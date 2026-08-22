# ai/audio_engine.py
#
# ARCHITECTURE: Records 1-second audio bursts in a background daemon thread.
# Uses RMS amplitude + zero-crossing rate + FFT spectral peak to classify sounds
# using simple heuristics (no trained model — prototype quality).
# Falls back gracefully if no microphone is available or sounddevice is missing.
# detect_safe() is non-blocking — always returns the most recent cached result.

import threading
import time
from dataclasses import dataclass
from typing import Tuple

try:
    import numpy as np
    import sounddevice as sd
    _AUDIO_DEPS = True
except ImportError:
    _AUDIO_DEPS = False

SAMPLE_RATE     = 16000
RECORD_SECONDS  = 1
CHANNELS        = 1
RMS_HIGH        = 0.15
RMS_MEDIUM      = 0.05
PEAK_GUNSHOT    = 6000    # Hz — above this suggests impulsive high-freq sound
PEAK_SCREAM_LOW = 500     # Hz
PEAK_SCREAM_HIGH= 1500    # Hz


@dataclass
class AudioResult:
    score: float
    label: str


class AudioEngine:
    """Background audio anomaly detector. Always safe to call detect_safe()."""

    def __init__(self):
        self.available = False
        self.result    = AudioResult(score=0.0, label="initializing")
        self._lock     = threading.Lock()
        self._stop     = threading.Event()  # Graceful shutdown signal

        # Load trained CNN classifier if available (Phase 3)
        try:
            from ai.audio_cnn_engine import AudioCNNEngine
            self._cnn = AudioCNNEngine()
        except Exception:
            self._cnn = None

        if not _AUDIO_DEPS:
            self.result = AudioResult(score=0.0, label="mic_unavailable")
            print("[AudioEngine] sounddevice/numpy not available. Audio disabled.")
            return

        # Try to open microphone
        try:
            sd.query_devices(kind="input")
            self.available = True
            cnn_status = "CNN" if (self._cnn and self._cnn.is_available()) else "heuristic"
            print(f"[AudioEngine] Microphone detected. Classifier: {cnn_status}. Starting audio loop.")
        except Exception as e:
            print(f"[AudioEngine] No microphone: {e}. Audio disabled.")
            self.result = AudioResult(score=0.0, label="mic_unavailable")
            return

        t = threading.Thread(target=self._record_loop, daemon=True)
        t.start()

    def stop(self):
        """Signal the background audio thread to exit cleanly."""
        self._stop.set()

    def _record_loop(self):
        while not self._stop.is_set():
            if not self.available:
                time.sleep(5)
                continue
            try:
                audio_data = sd.rec(
                    int(SAMPLE_RATE * RECORD_SECONDS),
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="float32",
                )
                sd.wait()
                samples = np.nan_to_num(audio_data.flatten().astype(np.float32))

                # --- Path A: Trained CNN classifier (Phase 3) ---
                if self._cnn is not None and self._cnn.is_available():
                    score, label = self._cnn.classify_samples(samples)
                    result = AudioResult(score=score, label=label)

                # --- Path B: Heuristic fallback ---
                else:
                    samples_f64 = samples.astype(np.float64)
                    rms = float(np.sqrt(np.mean(samples_f64 ** 2)))
                    zcr = float(np.mean(np.abs(np.diff(np.sign(samples_f64)))))

                    fft_result   = np.abs(np.fft.rfft(samples_f64))
                    peak_bin     = int(np.argmax(fft_result))
                    peak_freq_hz = peak_bin * SAMPLE_RATE / len(samples_f64)

                    if rms > RMS_HIGH and peak_freq_hz > PEAK_GUNSHOT:
                        result = AudioResult(score=0.75, label="gunshot_like")
                    elif rms > RMS_MEDIUM and PEAK_SCREAM_LOW < peak_freq_hz < PEAK_SCREAM_HIGH:
                        result = AudioResult(score=0.60, label="scream_like")
                    elif zcr > 0.3 and peak_freq_hz > PEAK_GUNSHOT:
                        result = AudioResult(score=0.55, label="glass_break_like")
                    else:
                        result = AudioResult(score=0.10, label="normal_ambient")

                with self._lock:
                    self.result = result

            except Exception as e:
                print(f"[AudioEngine] Non-fatal error: {e}")
                with self._lock:
                    self.result = AudioResult(score=0.0, label="error")
                time.sleep(1)

    def detect_safe(self) -> Tuple[float, str]:
        """Non-blocking. Returns latest (score, label)."""
        import time
        start_time = time.time()
        from core.instrumentation import log_instrumentation
        if not self.available:
            log_instrumentation("AudioEngine", "missing_output", {"reason": "mic_unavailable"})
            return 0.0, "mic_unavailable"
        with self._lock:
            latency = time.time() - start_time
            log_instrumentation("AudioEngine", "inference", {"score": self.result.score, "label": self.result.label, "latency": latency})
            return self.result.score, self.result.label

    # Legacy alias
    def detect(self) -> float:
        score, _ = self.detect_safe()
        return score