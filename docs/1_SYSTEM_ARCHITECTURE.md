# SENTRIX — System Architecture & Engineering Blueprint

## 1. System Overview
SENTRIX is an edge-to-cloud intelligent multimodal threat detection and physical security orchestration platform. It continuously fuses 5 asynchronous input streams (vision, acoustic signatures, kinematics/trajectory, identity, and environmental telemetry) to calculate a continuous **Threat Confidence Index (TCI)** from 0.0 to 1.0 mapped to Escalation Levels 1 through 5.

![SENTRIX System Architecture: Hardware, Cloud & Dashboard](sentrix_architecture_diagram.jpg)

---
┌──────────────────────────────────────────────────────────────────────────┐
│                      Physical Edge Sentinel Unit                         │
│  • 1080p Optical Sensor (UVC/RTSP)        • 16kHz Omnidirectional Mic    │
│  • DHT22 Temperature / Humidity Sensor    • 110dB Piezo Siren Relay      │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ Encrypted RTSP / Sensor Telemetry
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         SENTRIX Backend Core                             │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    AI Multi-Model Engine Hub                       │  │
│  │  • YOLOv8n (Weapons & Fire/Smoke)  • ResNet18 (Violence / Fight)   │  │
│  │  • MobileNetV2 (Anomaly Detector)  • Acoustic 2D CNN (Gun/Scream)  │  │
│  │  • Face Recognition (128-d Vector) • Kinematic Trajectory Tracker  │  │
│  └─────────────────────────────────┬──────────────────────────────────┘  │
│                                    │ Calibrated Modality Scores          │
│                                    ▼                                     │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │             TCI Late-Fusion Engine (XGBoost Booster)               │  │
│  │        Computes Continuous Threat Confidence Index (0.00 - 1.00)   │  │
│  └─────────────────────────────────┬──────────────────────────────────┘  │
│                                    │ Level >= 2: Evidence & Alerting     │
│        ┌───────────────────────────┴────────────────────────────┐        │
│        ▼                                                        ▼        │
│  ┌───────────────────────────┐                            ┌───────────┐  │
│  │ AES-256-GCM Evidence Vault│                            │  Twilio   │  │
│  │ SHA-256 Tamper Validation │                            │ Dispatch  │  │
│  └───────────────────────────┘                            └───────────┘  │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ WebSocket State Feed (/ws/threat @ 30 FPS)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      SENTRIX Command Dashboard                           │
│  • Real-Time Threat Gauge       • Audio Spectrogram Visualizer           │
│  • Multi-Camera Video Wall      • Authorized Facial Registry Audit       │
│  • Emergency Police Dispatch    • Encrypted Evidence Bundle Explorer     │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Threat Escalation Matrix (TCI Levels)

| TCI Score | Threat Level | Classification | System Actions & Dispatch Matrix |
|---|---|---|---|
| **0.00 – 0.20** | **Level 1** | `NORMAL` | Passive telemetry logging; green status indicator. |
| **0.21 – 0.45** | **Level 2** | `MONITOR` | Elevated frame capture; UI status alert; background tracking. |
| **0.46 – 0.65** | **Level 3** | `SUSPICIOUS` | JPEG snapshot saved; AES-256-GCM encrypted frame stored with SHA-256 hash. |
| **0.66 – 0.85** | **Level 4** | `HIGH` | Twilio SMS notification sent to security admin with incident metadata. |
| **0.86 – 1.00** | **Level 5** | `CRITICAL` | Twilio automated voice call trigger, hardware siren relay actuation (Pin 17), full police dispatch package pre-populated. |

---

## 3. Threat Fusion Formulation

The Threat Confidence Index ($TCI$) is determined through calibrated late fusion using an XGBoost booster model:

$$TCI = \text{XGBoost}(S_{\text{vision}}, S_{\text{audio}}, S_{\text{motion}}, S_{\text{identity}}, I_{\text{night}})$$

With fallback exponential moving average (EMA) smoothing:

$$TCI_t = \alpha \cdot TCI_{\text{raw}} + (1 - \alpha) \cdot TCI_{t-1}, \quad \alpha = 0.30$$

---

## 4. Evidence Cryptography & Chain of Custody
1. **Encryption**: AES-256-GCM authenticated encryption on 1080p JPEG raw frame bytes.
2. **Key Derivation**: HKDF-SHA256 from `EVIDENCE_AES_KEY` environment secret.
3. **Tamper Proofing**: Every `.enc` file has a companion `.json` sidecar storing the SHA-256 digest of the encrypted payload, engine confidence scores, and timestamp.
