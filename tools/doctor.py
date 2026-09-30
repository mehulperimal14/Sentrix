"""System self-check: report what is actually working, not what the docs claim.

The previous project documented eleven subsystems as "operational" while four of
them were silently inert — face recognition had no backend installed, DeepSORT
was missing so tracking degraded to per-frame indices, torchreid was missing so
re-id collapsed onto one identity, and Vosk resolved a relative model path that
never existed. Every one of those failed inside a bare ``except`` and logged
nothing at the level anyone reads.

Run this before a demo, and before quoting any capability in a report::

    python tools/doctor.py
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, WARN, FAIL = "  OK  ", " WARN ", " FAIL "


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failures = 0
        self.warnings = 0

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        if status == FAIL:
            self.failures += 1
        elif status == WARN:
            self.warnings += 1

    def section(self, title: str) -> None:
        self.rows.append(("", f"── {title} ", ""))

    def render(self) -> None:
        for status, name, detail in self.rows:
            if not status:
                print(f"\n{name}{'─' * max(0, 62 - len(name))}")
                continue
            print(f"[{status}] {name:<30} {detail}")
        print()
        if self.failures:
            print(f"{self.failures} failure(s), {self.warnings} warning(s)")
        elif self.warnings:
            print(f"all critical checks passed, {self.warnings} warning(s)")
        else:
            print("all checks passed")


def check_dependencies(report: Report) -> None:
    report.section("dependencies")
    required = ["fastapi", "uvicorn", "jinja2", "sqlalchemy", "dotenv",
                "cryptography", "cv2", "numpy", "torch", "ultralytics",
                "xgboost", "scipy"]
    optional = {
        "sounddevice": "microphone capture — audio modality is disabled without it",
        "insightface": "recommended face backend (pip install insightface onnxruntime)",
        "face_recognition": "alternative face backend (needs a C++ compiler)",
        "torchreid": "appearance re-id — falls back to colour histograms",
        "twilio": "SMS and voice alerts — falls back to console",
        "librosa": "audio front-end fallback when torchaudio is absent",
        "pandas": "needed by the notebooks and event-log tooling",
    }
    for module in required:
        try:
            importlib.import_module(module)
            report.add(OK, module)
        except ImportError:
            report.add(FAIL, module, "REQUIRED — pip install -r requirements.txt")
    for module, why in optional.items():
        try:
            importlib.import_module(module)
            report.add(OK, module, "optional")
        except ImportError:
            report.add(WARN, module, why)


def check_models(report: Report) -> None:
    from sentrix.paths import MODELS_DIR

    report.section("model weights")
    expected = {
        "yolov8n.pt": ("person detection", True),
        "v3_real/weapon_detector_real_v3.pt": ("weapon detection", True),
        "v3_real/fire_smoke_detector_real_v3.pt": ("fire/smoke detection", True),
        "v3_real/violence_classifier_real_v3.pt": ("violence (V3)", False),
        "violence_classifier.pt": ("violence (V2 fallback)", False),
        "v3_real/audio_classifier_real_v3.pt": ("audio (V3)", False),
        "audio_classifier.pt": ("audio (V2 fallback)", False),
        "tci_xgboost.json": ("TCI fusion booster", False),
        "tci_calibrator.json": ("fitted TCI calibrator", False),
    }
    for relative, (role, critical) in expected.items():
        path = MODELS_DIR / relative
        if path.exists():
            report.add(OK, relative, f"{role}, {path.stat().st_size / 1e6:.1f} MB")
        elif critical:
            report.add(FAIL, relative, f"{role} — MISSING")
        else:
            report.add(WARN, relative, f"{role} — not present")


def check_config(report: Report) -> None:
    import os

    from sentrix.config import settings

    report.section("configuration")
    if os.getenv("SENTRIX_PASSWORD", "").strip():
        password = os.getenv("SENTRIX_PASSWORD", "")
        if password in ("admin", "password", "sentrix"):
            report.add(WARN, "SENTRIX_PASSWORD", "set, but to a default value")
        else:
            report.add(OK, "SENTRIX_PASSWORD", "set")
    else:
        report.add(FAIL, "SENTRIX_PASSWORD", "unset — the dashboard cannot log in")

    report.add(OK if os.getenv("SESSION_SECRET", "").strip() else WARN,
               "SESSION_SECRET",
               "set" if os.getenv("SESSION_SECRET", "").strip()
               else "unset — session keys derive from the password alone")

    if os.getenv("EVIDENCE_AES_KEY", "").strip():
        report.add(OK, "EVIDENCE_AES_KEY", "set — evidence survives restarts")
    else:
        report.add(WARN, "EVIDENCE_AES_KEY",
                   "unset — evidence becomes unreadable when the process exits")

    report.add(OK, "inference rate", f"{settings.pipeline.inference_fps} Hz")
    report.add(OK, "EMA time constant", f"{settings.fusion.ema_tau_seconds} s")
    report.add(OK, "level bands", str(settings.policy.thresholds))
    report.add(OK, "escalate/de-escalate",
               f"{settings.policy.escalate_dwell_seconds}s / "
               f"{settings.policy.deescalate_dwell_seconds}s")
    report.add(OK, "SMS from level", str(settings.alert.sms_min_level))
    report.add(OK, "siren from level", str(settings.alert.siren_min_level))

    if settings.fusion.calibrate_inputs:
        report.add(FAIL, "calibrate_inputs",
                   "TRUE — reintroduces the train/serve skew, see fusion/calibration.py")
    else:
        report.add(OK, "calibrate_inputs", "False (correct)")


def check_engines(report: Report) -> None:
    report.section("engines")

    from sentrix.fusion.features import FeatureVector
    from sentrix.fusion.tci import TCIEngine

    engine = TCIEngine()
    report.add(OK, "TCI fusion", f"source={engine.model_source}, "
                                 f"contract={engine._contract}")

    # The single most important behavioural check in the whole system.
    idle, now = FeatureVector(), 0.0
    for _ in range(72):
        now += 1 / 6
        result = engine.compute(idle, now=now)
    if result.tci < 0.10:
        report.add(OK, "empty-scene TCI", f"{result.tci:.3f} (expected < 0.10)")
    else:
        report.add(FAIL, "empty-scene TCI",
                   f"{result.tci:.3f} — the system will alarm on an empty room")

    from sentrix.perception.identity import IdentityEngine
    identity = IdentityEngine()
    if identity.backend:
        report.add(OK, "identity backend",
                   f"{identity.backend}, {len(identity.enrolled)} enrolled")
    else:
        report.add(WARN, "identity backend",
                   "NONE — failing closed, nobody will be authorised")

    from sentrix.io.actuators import build_siren
    siren = build_siren()
    report.add(OK if siren.is_real else WARN, "siren backend",
               f"{siren.name}{'' if siren.is_real else ' (stub — logs only)'}")

    from sentrix.evidence.vault import vault
    chain = vault.verify_chain()
    if chain.get("ok"):
        report.add(OK, "evidence chain",
                   f"{chain.get('entries', 0)} entries verified")
    else:
        report.add(FAIL, "evidence chain",
                   f"{len(chain.get('problems', []))} problem(s)")


def check_camera(report: Report) -> None:
    report.section("camera")
    try:
        import cv2
    except ImportError:
        report.add(FAIL, "opencv", "not installed")
        return

    found = []
    for index in range(3):
        cap = cv2.VideoCapture(index, getattr(cv2, "CAP_DSHOW", 0))
        if cap.isOpened():
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append((index, frame.shape[1], frame.shape[0]))
        cap.release()

    if found:
        for index, width, height in found:
            report.add(OK, f"camera index {index}", f"{width}x{height}")
    else:
        report.add(WARN, "camera", "none detected — set CAMERA_SOURCES or use a file")


def main() -> int:
    from dotenv import load_dotenv

    from sentrix.paths import ROOT_DIR
    load_dotenv(ROOT_DIR / ".env")

    print("SENTRIX doctor")
    print("=" * 68)
    report = Report()
    for check in (check_dependencies, check_models, check_config,
                  check_engines, check_camera):
        try:
            check(report)
        except Exception as exc:
            report.add(FAIL, check.__name__, f"{type(exc).__name__}: {exc}")
    report.render()
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
