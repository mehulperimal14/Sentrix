# SENTRIX — Operations, Runbook & Manual Training Commands

## 1. Quickstart Run Commands

### On macOS / Linux:
```bash
./run.sh
```

### On Windows:
```cmd
run.bat
```

Access the local surveillance command dashboard at `http://127.0.0.1:8000/dashboard`.

---

## 2. Independent Model Training Commands (Run Directly in Terminal)

To save credits and train models at your own pace directly on your M2 Mac:

```bash
# 1. Activate your virtual environment
source .venv/bin/activate

# 2. Train YOLOv8n Weapon Detector (Apple Silicon GPU accelerated)
python training/scripts/train_weapon_detector.py 15

# 3. Train YOLOv8n Fire & Smoke Detector
python training/scripts/train_fire_smoke_detector.py 15

# 4. Train ResNet18 Violence / Fight Classifier (19,610 frames)
python training/scripts/train_violence_classifier.py 5

# 5. Train MobileNetV2 Surveillance Anomaly Classifier
python training/scripts/train_anomaly_classifier.py 5

# 6. Train PyTorch Acoustic Mel-Spectrogram CNN
python training/scripts/train_audio_classifier.py 10

# 7. Refit Multi-Modal TCI XGBoost Booster Model
python training/scripts/refit_xgboost.py

# 8. Evaluate all models (Accuracy, Precision, Recall, F1, Confusion Matrices)
python training/scripts/evaluate_models.py

# 9. Run full end-to-end backend verification
python training/scripts/verify_system.py
```

---

## 3. Web & API Endpoints Reference

| Route | Method | Access | Function |
|---|---|---|---|
| `/dashboard` | `GET` | Authenticated | Live multi-camera streaming dashboard & gauges |
| `/events` | `GET` | Authenticated | Audit event log (Level 1-5 filterable) |
| `/alerts` | `GET` | Authenticated | Snapshot image gallery |
| `/evidence` | `GET` | Authenticated | Encrypted AES-256-GCM evidence vaults with SHA-256 validation |
| `/dispatch` | `GET` | Authenticated | Pre-populated emergency 911 / police dispatch packages |
| `/authorized` | `GET` | Authenticated | Authorized resident face registry |
| `/api/tci/status` | `GET` | Public / Key | Returns real-time JSON threat state |
| `/api/dispatch/send`| `POST`| Authenticated | Triggers SMS / Voice call / Hardware Siren |
| `/ws/threat` | `WS` | Authenticated | 30 FPS bidirectional real-time state stream |
