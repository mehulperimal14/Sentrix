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
                "cryptography", "cv2", "numpy", "torch", "ultralytics", "scipy"]
    optional = {
        "xgboost": "only the retired synthetic booster and notebooks use it (D6); not needed at runtime",
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
    """Report the file each engine will actually load, not a fixed list.

    Engines resolve weights from preference lists (v4 before v3 before
    legacy), so a hardcoded list here would report files that are present but
    never served.
    """
    from sentrix.paths import MODELS_DIR
    from sentrix.perception import detector, scene, violence

    report.section("model weights (as resolved by each engine)")
    roles = [
        ("person detection", detector._resolve(detector._PERSON_WEIGHTS), True),
        ("weapon detection", detector._resolve(detector._WEAPON_WEIGHTS), True),
        ("fire/smoke detection", detector._resolve(detector._FIRE_WEIGHTS), True),
        ("scene classifier", scene._resolve(), False),
        ("violence (used only without a scene model)", violence._resolve(), False),
    ]
    for role, path, critical in roles:
        if path is not None:
            relative = path.relative_to(MODELS_DIR).as_posix()
            report.add(OK, role, f"{relative}, {path.stat().st_size / 1e6:.1f} MB")
        elif critical:
            report.add(FAIL, role, "MISSING")
        else:
            report.add(WARN, role, "not present")

    from sentrix.perception.sound_tagger import WEIGHTS_NAME
    tagger = MODELS_DIR / "audio" / WEIGHTS_NAME
    if tagger.exists():
        report.add(OK, "sound model", f"audio/{WEIGHTS_NAME} (PANNs CNN10, AudioSet), "
                                       f"{tagger.stat().st_size / 1e6:.1f} MB")
    else:
        report.add(WARN, "sound model", "PANNs CNN10 not present: the 4-class audio CNN "
                                         "is used (python tools/fetch_models.py)")
    expected = {
        "v3_real/audio_classifier_real_v3.pt": ("audio (V3, fallback)", False),
        "audio_classifier.pt": ("audio (V2 fallback)", False),
        "tci_xgboost.json": ("legacy synthetic booster, NOT used (CLAUDE.md D6)", False),
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
    report.add(OK, "level bands", str(settings.policy.thresholds))
    report.add(OK, "escalate/de-escalate",
               f"up {settings.policy.escalate_dwell_seconds} s per threshold "
               f"(obs {settings.policy.min_escalate_observations}) / down "
               f"{settings.policy.deescalate_dwell_seconds}s")
    report.add(OK, "SMS from level", str(settings.alert.sms_min_level))
    report.add(OK, "siren from level", str(settings.alert.siren_min_level))

    threat = settings.threat
    report.add(OK, "threat confirmation",
               f"{threat.confirm_k} of {threat.confirm_n} ticks, sources={threat.sources}")
    report.add(OK, "escalation caps",
               f"one source <= {threat.single_source_cap} (L3), classifier + detector "
               f"{threat.corroborated_floor}-{threat.corroborated_cap} (L4/L5), "
               f"experimental <= {threat.experimental_cap} (L2)")
    report.add(OK if not threat.armed else WARN, "mode",
               "home (presence is logged only)" if not threat.armed
               else "ARMED (unrecognised people raise intrusions)")


def check_engines(report: Report) -> None:
    report.section("engines")

    from sentrix.fusion.features import FeatureVector
    from sentrix.fusion.tci import TCIEngine

    engine = TCIEngine()
    report.add(OK, "TCI fusion", f"source={engine.model_source} (strongest confirmed "
                                 f"class evidence)")

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

    # Loading is the real check: a delta checkpoint also verifies the public
    # base weights it was trained on, and needs them cached when offline.
    from sentrix.perception.scene import SceneEngine, _resolve as scene_weights
    if scene_weights() is not None:
        scene = SceneEngine()
        if scene.available:
            report.add(OK, "scene classifier",
                       f"{scene.weights_name} loaded, thresholds {scene.thresholds} "
                       f"({scene.threshold_source})")
        else:
            report.add(FAIL, "scene classifier",
                       "weights present but failed to load — see the log (a delta "
                       "checkpoint needs its public base weights cached)")
    else:
        report.add(WARN, "scene classifier",
                   "not installed — the RWF-2000 violence model is used instead")

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


def check_camera(report: Report, probe: bool = False) -> None:
    """Report the configured camera. No device is opened unless ``probe`` is
    set (``--probe-camera``), and then only the configured indices."""
    import os

    report.section("camera")
    spec = os.getenv("CAMERA_SOURCES", "0")
    if any(part.strip().lower().startswith(("picamera", "picam"))
           for part in spec.split(",")):
        # OpenCV cannot open the CSI camera, and the running service may hold
        # it: the hardware section enumerated it through libcamera instead.
        report.add(OK, "camera source", f"{spec} (Picamera2; see the hardware section)")
        return
    parts = [part.strip() for part in spec.split(",") if part.strip()] or ["0"]
    from pathlib import Path

    from sentrix.utils.redact import redact_url
    indices = []
    for part in parts:
        if part.isdigit():
            indices.append(int(part))
        elif "://" in part:
            report.add(OK, "camera source", f"{redact_url(part)} (stream; not opened)")
        else:
            exists = Path(part).expanduser().is_file()
            report.add(OK if exists else FAIL, "camera source",
                       f"{part} ({'video file' if exists else 'file not found'})")
    if not indices:
        return
    if not probe:
        for index in indices:
            report.add(OK, f"camera index {index}",
                       "configured; not opened (tools/doctor.py --probe-camera to test it)")
        return
    try:
        import cv2
    except ImportError:
        report.add(FAIL, "opencv", "not installed")
        return
    for index in indices:
        cap = (cv2.VideoCapture(index, cv2.CAP_DSHOW) if sys.platform == "win32"
               else cv2.VideoCapture(index))
        try:
            ok, frame = cap.read() if cap.isOpened() else (False, None)
        finally:
            cap.release()
        if ok and frame is not None:
            report.add(OK, f"camera index {index}", f"{frame.shape[1]}x{frame.shape[0]}")
        else:
            report.add(WARN, f"camera index {index}",
                       "not readable — set CAMERA_SOURCES or use a file")


def check_services(report: Report) -> None:
    """Send guard, mode and every service's own preflight checks (docs/7
    §3.6): tracks add rows through ``preflight(settings)`` in their service
    module, not by editing this file."""
    from sentrix import services
    from sentrix.alerting import guard
    from sentrix.config import settings

    report.section("services and send guard (docs/7)")
    status = guard.status()
    live = [ch for ch, on in status["channels_live"].items() if on]
    report.add(OK if status["test_mode"] else WARN, "test mode",
               "on (only allowlisted recipients, test banner)" if status["test_mode"]
               else "OFF: live alerts reach real people once switched on")
    report.add(OK, "live alerts",
               f"on for {', '.join(live)}" if live else "dry run (nothing leaves)")
    report.add(OK, "test recipients", f"{status['test_recipients']} allowlisted")
    flags = {"sos": settings.sos.enabled, "link": settings.link.enabled,
             "channels": settings.channels.enabled, "gpio": settings.gpio.enabled,
             "node": settings.node.enabled}
    report.add(OK, "services enabled",
               ", ".join(k for k, v in flags.items() if v) or "none (default)")
    levels = {"ok": OK, "warn": WARN, "fail": FAIL}
    for name, rows in services.preflight_report(settings).items():
        for row in rows:
            report.add(levels.get(row.get("status"), WARN),
                       f"{name}: {row.get('name', '')}", row.get("detail", ""))


def check_hardware(report: Report, rows: list[dict] | None = None) -> None:
    """Raspberry Pi platform checks (``sentrix.io.node.hardware_report``):
    venv, CSI camera (enumerated, never opened), OpenCV, temperature,
    throttling, EXT5V, config.txt lines, PSU_MAX_CURRENT, systemd unit,
    network. On a laptop it prints one "skipped" row. GPIO pins and the node
    service report through their ``preflight`` rows (services section)."""
    from sentrix.io.node import hardware_report

    report.section("hardware (Raspberry Pi)")
    levels = {"ok": OK, "warn": WARN, "fail": FAIL}
    for row in hardware_report() if rows is None else rows:
        report.add(levels.get(row.get("status"), WARN), row.get("name", ""),
                   row.get("detail", ""))


def check_exports(report: Report, directory=None) -> None:
    """Exported models for ``SENTRIX_RUNTIME`` onnx / ncnn (T-CLIP): which
    exports exist, their recorded parity, and that a model without one falls
    back to torch. Nothing is loaded."""
    import json

    from sentrix.runtime import EXPORT_DIR, installed, resolve_backend

    report.section("model runtime")
    backend = resolve_backend()
    if backend == "torch":
        report.add(OK, "runtime", "torch (no exported models needed)")
        return
    report.add(OK if installed(backend) else FAIL, "runtime",
               f"{backend} ({'installed' if installed(backend) else 'package missing'})")
    directory = EXPORT_DIR if directory is None else directory
    manifest = {}
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    entries = manifest.get("exports") or {}
    suffix = "_ncnn_model" if backend == "ncnn" else ".onnx"
    found = sorted(p.name for p in directory.glob(f"*{suffix}")) if directory.is_dir() else []
    if not found:
        report.add(WARN, "exports", f"none in {directory.name}/ for {backend}: every model "
                   "falls back to torch (python tools/export_models.py)")
        return
    for name in found:
        parity = (entries.get(name) or {}).get("parity") or {}
        ok = parity.get("parity_ok")
        report.add(OK if ok else WARN, f"export {name}",
                   "parity OK" if ok else ("parity MISMATCH" if ok is False
                                           else "no parity record in manifest.json"))
    report.add(OK, "fallback", "models without an export run on torch")


def main(argv: list[str] | None = None) -> int:
    import argparse

    from dotenv import load_dotenv

    from sentrix.paths import ROOT_DIR
    parser = argparse.ArgumentParser(description="SENTRIX installation checks")
    parser.add_argument("--no-camera", action="store_true",
                        help="skip the camera section entirely")
    parser.add_argument("--probe-camera", action="store_true",
                        help="open the configured camera index and read one frame "
                             "(off by default: the doctor never opens a device)")
    args = parser.parse_args(argv)
    load_dotenv(ROOT_DIR / ".env")

    print("SENTRIX doctor")
    print("=" * 68)
    report = Report()
    checks = [check_dependencies, check_models, check_config, check_services,
              check_hardware, check_exports, check_engines]
    if not args.no_camera:
        def camera(report: Report) -> None:
            check_camera(report, probe=args.probe_camera)
        camera.__name__ = "check_camera"
        checks.append(camera)
    for check in checks:
        try:
            check(report)
        except Exception as exc:
            report.add(FAIL, check.__name__, f"{type(exc).__name__}: {exc}")
    report.render()
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
