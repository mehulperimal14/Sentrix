# smoke_test.py
#
# ARCHITECTURE: Standalone hardware, security & AI verification script.
# Validates environment variables, HMAC sessions, DB access, camera feeds,
# fusion engine, and optional subsystems on both macOS and Windows.
# Run before starting the main app to catch configuration errors early.

import os
import sys
import time
from dotenv import load_dotenv

def log_step(name: str):
    print(f"\n[+] Testing {name}...")

def log_ok(msg: str):
    print(f"    [OK] {msg}")

def log_warn(msg: str):
    print(f"    [WARN] {msg}")

def log_err(msg: str):
    print(f"    [ERROR] {msg}")

def main():
    print("========================================")
    print("       SENTRIX SMOKE TEST SUITE         ")
    print("========================================")
    
    load_dotenv()
    
    # 1. Environment configuration
    log_step("Environment Configuration")
    required = ["CAMERA_SOURCES", "SENTRIX_PASSWORD"]
    for req in required:
        if os.getenv(req):
            log_ok(f"{req} is set")
        else:
            log_warn(f"{req} not set. Will use defaults.")
            
    optional = ["SESSION_SECRET", "ROBOFLOW_API_KEY", "TWILIO_ACCOUNT_SID", "EVIDENCE_AES_KEY"]
    for opt in optional:
        if os.getenv(opt):
            log_ok(f"{opt} is set")
        else:
            log_warn(f"{opt} not set. Feature will run in demo/fallback mode.")

    # 2. Security Module (HMAC sessions & upload validator)
    log_step("Security Module")
    try:
        from core.security import generate_token, verify_token, sanitize_filename, validate_image_upload
        pwd = os.getenv("SENTRIX_PASSWORD", "admin")
        token = generate_token(pwd)
        assert verify_token(token, pwd) == True
        assert verify_token(token, "wrong") == False
        assert sanitize_filename("../../test.jpg") == "test.jpg"
        assert validate_image_upload("face.png") == True
        log_ok("HMAC session token generation and verification passed.")
    except Exception as e:
        log_err(f"Security module error: {e}")

    # 3. Database
    log_step("Database Access & Retention")
    try:
        import db.database as database
        database.init_db()
        database.prune_old_events()
        database.prune_old_snapshots()
        events = database.get_recent_events(limit=1)
        log_ok("Database initialized, indexed, and retention prune executed successfully.")
    except Exception as e:
        log_err(f"Database error: {e}")

    # 4. Directories
    log_step("Directory Structure")
    dirs = ["static/alerts", "static/alerts/evidence", "static/authorized_faces"]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        if os.access(d, os.W_OK):
            log_ok(f"Directory {d} is writable.")
        else:
            log_err(f"Directory {d} is not writable.")

    # 5. Fusion Engine (Explainability & Uncertainty)
    log_step("Threat Fusion Engine")
    try:
        from ai.fusion_engine import FusionEngine
        fe = FusionEngine()
        sample_scores = {
            "vision": 0.1, "audio": 0.0, "motion": 0.1, "behaviour": 0.1,
            "identity": 0.0, "weapon": 0.0, "fire": 0.0, "theft": 0.0,
            "harmful": 0.0, "intrusion": 0.05, "unauthorized": False,
            "authorized": True, "behaviour_label": "normal"
        }
        res = fe.compute(sample_scores)
        log_ok(f"Fusion computed level={res.level}, TCI={res.tci:.2f}, uncertainty={res.uncertainty:.2f}")
    except Exception as e:
        log_err(f"Fusion engine error: {e}")

    # 6. Encrypted Evidence & Tamper Detection
    log_step("Encrypted Evidence")
    try:
        from core.encrypted_evidence import encrypted_evidence
        items = encrypted_evidence.list_evidence()
        log_ok(f"Encrypted evidence vault ready ({len(items)} items found).")
    except Exception as e:
        log_err(f"Encrypted evidence error: {e}")

    # 7. Siren Module
    log_step("Hardware Siren")
    try:
        from hardware.siren import Siren
        siren = Siren()
        log_ok(f"Siren module ready for platform: {sys.platform}")
    except Exception as e:
        log_err(f"Siren error: {e}")

    # 8. Camera Hardware
    log_step("Camera Hardware")
    try:
        from hardware.camera_manager import CameraManager
        cm = CameraManager()
        frames = cm.get_all_frames()
        if frames and frames[0] is not None:
            shape = frames[0].shape
            log_ok(f"Camera captured frame successfully: {shape}")
        else:
            log_warn("Camera manager returned empty/blank frame. Check hardware.")
        cm.release_all()
    except Exception as e:
        log_err(f"Camera error: {e}")

    # 9. Vision Engine (YOLO)
    log_step("Vision Engine (YOLOv8)")
    try:
        from ai.vision_engine import VisionEngine
        import numpy as np
        ve = VisionEngine()
        if ve._available:
            log_ok("YOLO model loaded.")
            dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
            conf, dets = ve.detect(dummy_frame)
            log_ok(f"Inference test passed. Conf: {conf}, Dets: {len(dets)}")
        else:
            log_warn("ultralytics not loaded. Vision engine running in safe zero-score fallback mode.")
    except Exception as e:
        log_err(f"Vision engine error: {e}")

    # 9.5 Local Weapon Detector
    log_step("Local Weapon Detector")
    try:
        from ai.local_fallback_engine import LocalFallbackEngine
        import numpy as np
        lfe = LocalFallbackEngine()
        dummy = np.zeros((480, 640, 3), dtype=np.uint8)
        score = lfe.detect_weapon(dummy)
        log_ok(f"Local fallback detector ran. Score: {score:.2f}")
    except Exception as e:
        log_err(f"Local fallback detector error: {e}")

    # 10. Audio Engine
    log_step("Audio Engine")
    try:
        from ai.audio_engine import AudioEngine
        ae = AudioEngine()
        if ae.available:
            log_ok("Microphone detected and audio loop started.")
            time.sleep(1.2)  # Let it process one chunk
            score, label = ae.detect_safe()
            log_ok(f"Audio test passed. Label: {label}")
            ae.stop()
        else:
            log_warn("Audio engine not available (no mic or sounddevice missing).")
    except Exception as e:
        log_err(f"Audio engine error: {e}")
        
    print("\n========================================")
    print("Smoke tests complete. If there are no ERRORs, you are ready to start.")
    print("Run: python app.py")

if __name__ == "__main__":
    main()
