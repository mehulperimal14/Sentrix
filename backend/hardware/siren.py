# hardware/siren.py
#
# ARCHITECTURE: Cross-platform alert sound.
# macOS: uses afplay with static/sounds/siren.wav or system alert.
# Windows: winsound.PlaySound or winsound.Beep.
# Linux: paplay / aplay / beep / terminal bell.
# Wrapped in try/except — never crashes on any unsupported platform.
# activate() runs in a daemon thread so it never blocks the processing loop.

import os
import platform
import subprocess
import threading


class Siren:
    """Platform-aware siren that plays audio or prints an alert across macOS, Windows, and Linux."""

    def __init__(self):
        self.sound_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "static", "sounds", "siren.wav"
        )

    def activate(self, duration_ms: int = 2000):
        """Fire siren in a daemon thread so it doesn't block the pipeline."""
        t = threading.Thread(target=self._beep, args=(duration_ms,), daemon=True)
        t.start()

    def _beep(self, duration_ms: int):
        if os.getenv("SENTRIX_EVAL_MODE") == "1":
            print("[Siren-EVAL] Mock Siren Activation")
            return
        print("[Siren] *** ALERT: LOCAL SIREN ACTIVATED ***")
        try:
            sys_name = platform.system()
            if sys_name == "Windows":
                try:
                    import winsound
                    if os.path.exists(self.sound_path):
                        winsound.PlaySound(
                            self.sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC
                        )
                    else:
                        winsound.Beep(1000, duration_ms)
                except Exception as e:
                    print(f"[Siren] Windows sound error: {e}")

            elif sys_name == "Darwin":  # macOS
                try:
                    if os.path.exists(self.sound_path):
                        subprocess.run(
                            ["afplay", self.sound_path],
                            timeout=max(3, int(duration_ms / 1000) + 1),
                            capture_output=True,
                        )
                    else:
                        subprocess.run(
                            ["afplay", "/System/Library/Sounds/Ping.aiff"],
                            timeout=3,
                            capture_output=True,
                        )
                except Exception as e:
                    print(f"[Siren] macOS sound error: {e}")

            else:  # Linux / Unix
                played = False
                if os.path.exists(self.sound_path):
                    for player in ["paplay", "aplay", "play"]:
                        try:
                            res = subprocess.run(
                                [player, self.sound_path],
                                timeout=max(3, int(duration_ms / 1000) + 1),
                                capture_output=True,
                            )
                            if res.returncode == 0:
                                played = True
                                break
                        except Exception:
                            continue
                if not played:
                    try:
                        subprocess.run(
                            ["beep", "-f", "1000", "-l", str(duration_ms)],
                            timeout=3,
                            capture_output=True,
                        )
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Siren] Could not activate: {e}")