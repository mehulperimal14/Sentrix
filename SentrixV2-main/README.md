# SENTRIX

SENTRIX is an edge-first multimodal physical security and threat orchestration platform that fuses multi-camera video, acoustic telemetry, dual-mode face recognition, trajectory behaviour analysis, and cloud threat intelligence into a unified 5-level **Threat Confidence Index (TCI $\in [0.0, 1.0]$)**. It runs entirely on local edge hardware, falls back gracefully when optional subsystems are offline, and escalates incidents automatically through high-resolution snapshots, AES-256-GCM encrypted evidence bundles, Twilio SMS/call alerts, and pre-populated emergency dispatch packages.

---

## Feature Overview

| Feature | Status | Notes |
|---|---|---|
| YOLO person detection | ✅ | YOLOv8-nano via `ultralytics` (<10ms inference) |
| Motion scoring | ✅ | Vectorized frame-differencing pixel shift energy |
| Behaviour classification | ✅ | Centroid trajectory velocity, aspect-ratio & loitering model |
| Dual face authorization | ✅ | Dual-engine: 128-d deep embeddings + 512-bin HSV color descriptor |
| Person Re-ID | ✅ | DeepSORT + appearance gallery with FIFO cap (200 identities) |
| Audio anomaly detection | ✅ | Low-latency 16 kHz background capture (RMS, ZCR, FFT) |
| Cloud weapon/fire detection | ✅ | Roboflow API with instant local OpenCV heuristic fallback |
| Voice SOS ("emergency") | ✅ | VOSK offline speech recognition |
| TCI fusion + smoothing | ✅ | XGBoost late fusion model + EMA temporal filter ($\alpha = 0.3$) |
| Explainability panel | ✅ | Top-3 contributing factors, uncertainty metric, confidence band |
| 5-Level Threat escalation | ✅ | Policy: L1 Normal → L2 Suspicious → L3 Elevated → L4 High → L5 Critical |
| AES-256-GCM evidence vault | ✅ | HKDF-SHA256 stable key derivation, SHA-256 tamper detection hash |
| Emergency dispatch | ✅ | Pre-populated Police / Fire dispatch package, Twilio SMS & Calls |
| Zero-Trust HMAC sessions | ✅ | 12-hour HMAC-SHA256 signed session token, `httponly` cookie |
| Upload sanitization | ✅ | MIME whitelist (`image/jpeg`, `image/png`), path-traversal safe |
| Auth on all endpoints | ✅ | `/api/*`, `/video`, `/ws/threat`, file uploads, evidence review |
| Async side-effect queue | ✅ | Bounded FIFO queue (`maxsize=50`) offloading disk/network I/O |
| Graceful shutdown | ✅ | Thread join, camera release, SQLite flush, clean exit |
| DB indexes + retention | ✅ | SQLAlchemy ORM indexes; auto-prune events older than `RETENTION_DAYS` |
| Live command dashboard | ✅ | WebSocket-driven, TCI gauge, score bars, real-time HUD stream |
| MJPEG video stream | ✅ | High-throughput `/video` stream (auth-guarded) |

---

## Architecture Overview

```
                        ┌───────────────────────────────────────┐
                        │          FastAPI App (app.py)          │
                        │  Lifespan: DB init, engines init,      │
                        │  retention prune, bg thread start       │
                        └──────────────┬────────────────────────┘
                                       │
                       ┌───────────────▼────────────────┐
                       │    Background Processing Thread  │  ~30 fps
                       │       SystemEngine.process()    │
                       └──┬──────────────────────────┬──┘
                          │  Hot Path (synchronous)   │
               ┌───────────▼───────────────────────────▼────────────┐
               │ Camera → YOLO → Motion → Behaviour → Audio → Face  │
               │    Cloud Threat → ReID → Fusion → State Update      │
               └───────────────────────┬────────────────────────────┘
                                       │
                          ┌────────────▼────────────┐
                          │   Async Task Queue      │  ← non-blocking
                          │  Worker Thread          │     (<0.05ms)
                          │  snapshot / evidence    │
                          │  SMS / dispatch / DB    │
                          └─────────────────────────┘
```

---

## Hardware Architecture & Physical Implementation Blueprint

The SENTRIX platform is engineered for physical deployment on local edge hardware with complete electrical isolation, multi-sensor coverage, and battery-backed power continuity.

### 1. Electronics & Hardware Block Diagram

![SENTRIX Electronics Block Diagram](static/img/sentrix_hardware_block_diagram.jpg)

```
┌───────────────────────────┐     ┌─────────────────────────────────────────────────────────────┐
│    1. SYSTEM OVERVIEW     │     │           3. OPTICAL INGESTION SUBSYSTEM                    │
│ • Multi-Camera Inputs     │     │ ┌─────────────────────────┐     ┌─────────────────────────┐ │
│ • Local Subnet (VLAN 10)  ├────►│ │ Primary 1080p USB 3.0   │     │ Secondary 1080p RTSP    │ │ │
│ • Cloud Refinement Gateway│     │ │ Wide-Angle UVC Camera   │     │ IP Camera (IR Night Vis)│ │ │
└─────────────┬─────────────┘     │ └────────────┬────────────┘     └────────────┬────────────┘ │
              │                   └──────────────┼───────────────────────────────┼──────────────┘
              │                                  │ (USB 3.0 / <2ms)              │ (RTSP / Port 554)
              ▼                                  ▼                               ▼
┌───────────────────────────────────────────────────────────────────────────────────────────────┐
│                          2. MAIN CONTROLLER & EDGE COMPUTE HOST                               │
│                   Intel NUC / Apple Silicon Host (8-Core CPU, 8GB+ RAM)                       │
│                                                                                               │
│  [USB 3.0 Host Controller] ◄── Ingests 30 FPS raw UVC frame matrix buffers                    │
│  [Gigabit Ethernet RJ45]   ◄── Ingests H.264 RTSP bitstream (192.168.1.50)                    │
│  [3.5mm / USB Audio ADC]   ◄── Ingests 16 kHz 16-bit PCM mono acoustic stream                 │
│  [GPIO Header / USB-Relay] ──► Dispatches 5V TTL trigger to Optocoupler Relay                 │
│  [Internal NVMe M.2 SSD]   ──► Stores AES-256-GCM encrypted evidence & SQLite WAL database    │
└─────────────┬──────────────────────────────────┬───────────────────────────────▲──────────────┘
              │                                  │                               │
              │ (3.5mm PCM Audio)                │ (5V TTL Signal)               │ (12V DC Power)
              ▼                                  ▼                               │
┌───────────────────────────┐     ┌───────────────────────────┐     ┌────────────┴──────────────┐
│   4. ACOUSTIC SENSING     │     │    5. THREAT ACTUATORS    │     │ 6. POWER DISTRIBUTION SYS │
│ ┌───────────────────────┐ │     │ ┌───────────────────────┐ │     │ • 230V AC Mains Input     │
│ │ Omnidirectional Mic   │ │     │ │ 5V Optocoupler Relay  │ │     │ • 600VA Line-Int. UPS     │
│ │ (16 kHz, SNR >= 58dB) │ │     │ │ Module (PC817 + Diode)│ │     │ • 12V 5A Regulated SMPS   │
│ └───────────┬───────────┘ │     │ └───────────┬───────────┘ │     │ • 5V 3A Step-Down Buck    │
│             ▼             │     │             ▼             │     │ • Power Rails:            │
│ ┌───────────────────────┐ │     │ ┌───────────────────────┐ │     │   - 12V Rail (Host/Siren) │
│ │ USB/Audio ADC Codec   │ │     │ │ 12V 110dB Siren +     │ │     │   - 5V Rail (USB/Relay)   │
│ └───────────────────────┘ │     │ │ Strobe Warning Beacon │ │     │   - 3.3V Logic Rail       │
└───────────────────────────┘     │ └───────────────────────┘ │     └───────────────────────────┘
                                  └───────────────────────────┘
```

---

### 2. Step-by-Step Hardware-to-Hardware Interfacing Guide

#### Step 1: Power Conditioning & Uninterruptible Distribution
* **Implementation (How):**
  1. $230\text{V AC}$ mains is connected to an **APC 600VA / 360W Line-Interactive UPS**.
  2. The UPS AC output feeds a **12V 5A (60W) Regulated Switching Mode Power Supply (SMPS)**.
  3. The $12\text{V DC}$ rail powers the Edge Compute Host, the Secondary IP Camera IR illuminators, and the $110\text{dB}$ Security Siren.
  4. An **LM2596 High-Efficiency Step-Down Buck Converter** steps down $12\text{V DC} \rightarrow 5.0\text{V DC} \pm 1\%$ ($3\text{A}$ max) to power the USB Optical Camera, the Optocoupler Relay VCC, and the Audio ADC Codec.
* **Engineering Rationale (Why):**
  Isolates computing elements from mains voltage spikes, power dips, and deliberate power sabotage. Ensures **2.5+ hours of autonomous operation** during utility grid failure.
* **Design Alternatives:**
  - *Alternative A (PoE Switch):* A centralized 802.3at Power-over-Ethernet (PoE+) Gigabit switch (higher cost, but eliminates separate DC power cabling to IP cameras).
  - *Alternative B (Direct USB-PD Power Bank):* A $20,000\text{ mAh}$ USB-PD power bank with pass-through charging for ultra-compact battery-backed lab setups.

---

#### Step 2: Optical Camera Subsystem Integration
* **Implementation (How):**
  1. **Primary Camera (Entry Choke-Point):** A $1080\text{p}$ Wide-Angle ($90^\circ\text{ FOV}$) USB 3.0 UVC Camera is mounted at a height of $2.4\text{m}$ tilted $15^\circ$ downward, plugged directly into the host's USB 3.0 port.
  2. **Secondary Camera (Perimeter / Approach):** An IP Bullet Camera with $850\text{nm}$ Infrared Night Vision LEDs is connected via Cat6 Ethernet cable to the local network switch, streaming H.264/MJPEG over RTSP on Port 554 (`rtsp://admin:password@192.168.10.50:554/live/ch0`).
* **Engineering Rationale (Why):**
  USB 3.0 delivers uncompressed raw frame matrices directly into host RAM with **$<2\text{ms}$ ingestion latency**, critical for high-speed face recognition and identity authorization. RTSP covers wide outdoor perimeters over standard structured cabling.
* **Design Alternatives:**
  - *Alternative A (All-RTSP IP Multi-Cam Pool):* Connect 4x RTSP cameras over a dedicated Security VLAN.
  - *Alternative B (Embedded MIPI-CSI2):* On NVIDIA Jetson / Raspberry Pi hosts, use direct ribbon-cable MIPI-CSI2 camera modules (e.g., Sony IMX477) to bypass USB bus contention.

---

#### Step 3: Acoustic Sensing Subsystem Integration
* **Implementation (How):**
  1. An **Omnidirectional Electret Boundary Condenser Microphone** (SNR $\ge 58\text{ dB}$, frequency response $65\text{ Hz} - 18\text{ kHz}$) is mounted centrally on a wall or ceiling at $2.0\text{m}$ height away from cooling fan airflow.
  2. Connected via $3.5\text{mm}$ analog jack or USB Audio Codec.
  3. The `sounddevice` engine continuously samples audio in non-blocking ring buffers at **16,000 Hz, 16-bit PCM, single-channel mono**.
* **Engineering Rationale (Why):**
  $16\text{ kHz}$ captures human vocal harmonics (screams: $600\text{ Hz} - 2\text{ kHz}$) and transient shockwaves (glass break: $5.5\text{ kHz} - 7.5\text{ kHz}$, gunshots: broad spectral burst) according to the Nyquist theorem while consuming 66% less memory bandwidth than $48\text{ kHz}$ studio audio.
* **Design Alternatives:**
  - *Alternative A (I2S Digital MEMS Microphone):* An INMP441 I2S microphone connected directly to host GPIO pins (zero analog noise interference).
  - *Alternative B (USB Beamforming Array):* A 4-microphone USB array with onboard hardware acoustic echo cancellation (AEC) for noisy commercial environments.

---

#### Step 4: Galvanic Optocoupler Relay & Siren Actuator
* **Implementation (How):**
  1. The edge host GPIO header (or FTDI/CH340 USB-to-TTL board) outputs a $5\text{V TTL}$ trigger signal to Pin 1 (Anode) of a **PC817 Optocoupler**.
  2. Pin 3 (Emitter) of the optocoupler drives the Base of an **NPN 2N2222 Transistor**.
  3. The transistor collector switches the ground side of a $5\text{V DC}$ Relay Coil.
  4. A **1N4007 Flyback Clamping Diode** is connected in reverse-parallel across the relay coil.
  5. The Normally Open (NO) and Common (COM) isolated relay contacts switch $12\text{V DC}$ power to a **110 dB Piezoelectric Siren** and Strobe Beacon.
* **Engineering Rationale (Why):**
  The PC817 provides **$5,000\text{ V}_{RMS}$ galvanic optical isolation**. The 1N4007 flyback diode clamps high-voltage inductive back-EMF spikes generated when the relay coil turns off, completely protecting the host processor motherboard from electrical destruction.
* **Design Alternatives:**
  - *Alternative A (Direct Audio Sounder):* Use the edge host's native audio output to trigger [`static/sounds/siren.wav`](static/sounds/siren.wav) via `afplay` (macOS), `winsound` (Windows), or `aplay` (Linux) with zero external relay hardware.
  - *Alternative B (Solid State Relay / SSR):* Use a Fotek SSR-25DA solid-state relay for completely silent, spark-free optical switching.

---

### 3. Step-by-Step Hardware-to-Software Integration Flow

```
+--------------------------------------------------------------------------------------------------+
| 1. HARDWARE INGESTION (THREAD ISOLATION)                                                         |
| • hardware/camera.py: Spawns daemon thread grabbing frames continuously from cv2.VideoCapture    |
| • ai/audio_engine.py: sounddevice callback buffers 16,000 samples into memory ring buffer       |
+--------------------------------------------------------------------------------------------------+
                                                 │
                                                 ▼
+--------------------------------------------------------------------------------------------------+
| 2. SYNCHRONOUS INFERENCE & MULTIMODAL FUSION (< 5.0ms Hot Path)                                  |
| • ai/vision_engine.py: YOLOv8n identifies persons + FrameDifferencer computes motion energy      |
| • ai/behaviour_engine.py: Evaluates centroid bounding-box velocity & loitering aspect ratio     |
| • ai/face_engine.py: Matches face against enrolled profiles in static/authorized_faces/         |
| • ai/fusion_engine.py: XGBoost evaluates TCI in [0.0, 1.0], applies EMA filter & uncertainty     |
| • core/state.py: Atomically updates thread-safe state singleton under threading.Lock             |
+--------------------------------------------------------------------------------------------------+
                                                 │
                                                 ▼
+--------------------------------------------------------------------------------------------------+
| 3. ASYNCHRONOUS ESCALATION & ACTUATOR TRIGGERING (Task Worker Queue)                             |
| • core/system_engine.py: Enqueues side-effect tasks into queue.Queue(maxsize=50) in <0.05ms       |
| • hardware/siren.py: Triggers physical relay + acoustic siren (with 60s cooldown lock)          |
| • core/encrypted_evidence.py: Encrypts forensic JPEG with AES-256-GCM (HKDF key) + SHA-256 hash  |
| • core/alert_service.py: Dispatches Twilio SMS and automated emergency voice calls               |
| • web/streaming.py & web/routes.py: Serves live HUD /video MJPEG and /ws/threat WebSocket state  |
+--------------------------------------------------------------------------------------------------+
```

---

### 4. Hardware Bill of Materials (BOM)

| Item | Component Description | Interface Type | Qty | Est. Cost (INR) |
|---|---|---|---|---|
| 1 | **Edge Compute Host** (Intel Core i5 NUC / Apple Silicon Host, 8-Core CPU, 8GB+ RAM) | USB 3.0 / PCIe / RJ45 | 1 | ₹0 (Lab/Dev Host) |
| 2 | **Primary Optical Camera** ($1080\text{p}$ FHD Wide-Angle UVC CMOS, $90^\circ\text{ FOV}$) | USB 3.0 (UVC 1.5) | 1 | ₹2,450 |
| 3 | **Secondary Perimeter Camera** ($1080\text{p}$ IP Camera, $850\text{nm}$ IR Night Vision, IP66) | RTSP / Ethernet / Wi-Fi | 1 | ₹2,800 |
| 4 | **Acoustic Anomaly Microphone** (Omnidirectional Boundary Condenser, $16\text{ kHz}$, SNR $\ge 58\text{ dB}$) | $3.5\text{mm}$ Jack / USB PCM | 1 | ₹1,200 |
| 5 | **High-Decibel Security Siren** ($12\text{V DC}$ Piezoelectric, $110\text{ dB} @ 1\text{m}$, $250\text{mA}$) | GPIO via $5\text{V}$ Relay | 1 | ₹650 |
| 6 | **Galvanic Optocoupler Relay** (PC817 $5\text{V}$ Opto-Isolated Module + 1N4007 Diode) | $5\text{V TTL}$ GPIO Header | 1 | ₹250 |
| 7 | **Line-Interactive UPS** ($600\text{VA} / 360\text{W}$, $2.5+\text{ hours}$ runtime) | $230\text{V AC}$ Output | 1 | ₹2,950 |
| 8 | **Regulated DC SMPS** ($12\text{V 5A}$, $60\text{W}$ Universal AC/DC Adapter) | $5.5 \times 2.1\text{mm}$ Barrel | 1 | ₹550 |
| 9 | **DC-DC Step-Down Converter** (LM2596 $12\text{V} \rightarrow 5\text{V} @ 3\text{A}$ Buck Module) | Screw Terminals | 1 | ₹180 |
| 10 | **Cabling, Mounts & Shielding** (Cat6 Patch Cables, USB 3.0 Extenders, Swivel Brackets) | Hardware Mounts | 1 | ₹850 |
| | **TOTAL SYSTEM PROCUREMENT BUDGET:** | | | **₹11,880 INR** |

---

## Directory Structure

```
Sentrix-main/
├── app.py                    # FastAPI entry point, lifespan, processing loop
├── requirements.txt          # Python dependencies
├── .env.example              # Config template
│
├── core/
│   ├── security.py           # HMAC sessions, upload sanitizer
│   ├── system_engine.py      # Per-frame orchestrator + async task queue
│   ├── engine_instance.py    # Engine singleton factory
│   ├── state.py              # Thread-safe shared state (dashboard source of truth)
│   ├── health_monitor.py     # Subsystem availability tracker
│   ├── escalation.py         # 5-level declarative action policy
│   ├── alert_service.py      # Twilio SMS + voice call
│   ├── dispatch_service.py   # Emergency dispatch package builder
│   └── encrypted_evidence.py # AES-256-GCM encryption + HKDF key derivation
│
├── ai/
│   ├── vision_engine.py      # YOLOv8 detection + motion scoring
│   ├── behaviour_engine.py   # Centroid trajectory behaviour classifier
│   ├── audio_engine.py       # Background audio anomaly detector
│   ├── face_engine.py        # Face recognition + authorization persistence
│   ├── reid_engine.py        # Person Re-ID with gallery cap
│   ├── tracking_engine.py    # DeepSORT multi-object tracker
│   ├── cloud_engines.py      # Roboflow cloud weapon/fire inference
│   ├── local_fallback_engine.py # OpenCV weapon heuristic (cloud fallback)
│   ├── fusion_engine.py      # XGBoost TCI fusion + uncertainty + explainability
│   └── voice_sos_engine.py   # VOSK voice command listener
│
├── hardware/
│   ├── camera.py             # Single-camera OpenCV wrapper (background thread)
│   ├── camera_manager.py     # Multi-camera pool
│   └── siren.py              # Platform-aware alert sound & relay driver
│
├── db/
│   ├── models.py             # SQLAlchemy ORM (EventLog, DispatchPackage) with indexes
│   └── database.py           # SQLite helper layer + retention prune helpers
│
├── web/
│   ├── routes.py             # Page + API + WebSocket routes (all auth-guarded)
│   └── streaming.py          # MJPEG /video endpoint (auth-guarded)
│
├── templates/                # Jinja2 HTML pages
│   ├── base.html             # Nav + shared assets
│   ├── login.html            # Auth page
│   ├── dashboard.html        # TCI gauge, score bars, explainability, event log
│   ├── live.html             # Full-screen MJPEG + WS stats
│   ├── events.html           # Historical event log
│   ├── alerts.html           # Snapshot gallery
│   ├── evidence.html         # Encrypted evidence vault
│   ├── dispatch.html         # Emergency dispatch packages
│   └── authorized.html       # Face enrollment management
│
├── static/
│   ├── css/style.css         # Design system
│   ├── js/app.js             # WebSocket client, dashboard + explainability logic
│   ├── img/                  # Electronics block diagrams & visual assets
│   ├── sounds/               # Siren audio assets
│   └── authorized_faces/     # Enrolled face images (runtime)
│
├── models/
│   ├── tci_xgboost.json      # XGBoost fusion model
│   └── yolov8n.pt            # YOLO model (auto-downloaded by ultralytics)
│
├── Capstone Docs/            # Capstone Evaluation & Academic Reports (TIET 2026)
│   ├── 1_MID_SEMESTER_TECHNICAL_REPORT_SENTRIX.md
│   ├── 2_FINALIZED_DESIGN_MODEL.md
│   ├── 3_HARDWARE_SPECIFICATIONS_AND_BOM.md
│   ├── 4_HARDWARE_INTEGRATION_AND_DEPLOYMENT_ARCHITECTURE.md
│   └── sentrix_hardware_block_diagram.jpg
│
└── Doc/
    ├── MID_SEMESTER_TECHNICAL_REPORT_SENTRIX.md
    ├── FINALIZED_DESIGN_MODEL.md
    ├── HARDWARE_SPECIFICATIONS_AND_BOM.md
    ├── HARDWARE_INTEGRATION_AND_DEPLOYMENT_ARCHITECTURE.md
    └── SENTRIX_MASTER_TECHNICAL_REPORT.md
```

---

## Quick Start

### 1. Create virtual environment
```bash
python -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment
```bash
cp .env.example .env
```

Edit `.env` with at minimum:
- `SENTRIX_PASSWORD` — dashboard login password
- `SESSION_SECRET` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`
- `CAMERA_SOURCES` — webcam index (usually `0`) or RTSP URL (`rtsp://admin:pass@192.168.1.50:554/live`)

Optional:
- `ROBOFLOW_API_KEY` — enables cloud weapon/fire detection
- Twilio credentials — enables SMS + call alerts
- `EVIDENCE_AES_KEY` — makes encrypted evidence readable across restarts

### 4. Run the app
```bash
python app.py
```

Open the dashboard at **http://127.0.0.1:8000** and log in with your `SENTRIX_PASSWORD`.

> The database and tables are created automatically on first run. No separate `init_db` step is needed.

### 5. Run the smoke test
```bash
python smoke_test.py
```

---

## Configuration Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `SENTRIX_PASSWORD` | ✅ | `admin` | Dashboard login password |
| `SESSION_SECRET` | Recommended | — | HMAC signing key for session tokens |
| `CAMERA_SOURCES` | ✅ | `0` | Comma-separated webcam index or RTSP URL |
| `PUBLIC_SERVER_URL` | — | `http://127.0.0.1:8000` | Base URL for snapshot links in alerts |
| `ROBOFLOW_API_KEY` | Optional | — | Enables cloud weapon/fire detection |
| `TWILIO_ACCOUNT_SID` | Optional | — | Twilio SMS/call alerts |
| `TWILIO_AUTH_TOKEN` | Optional | — | Twilio auth |
| `TWILIO_PHONE_NUMBER` | Optional | — | From number |
| `ALERT_PHONE_NUMBER` | Optional | — | Target number for alerts |
| `EVIDENCE_AES_KEY` | Optional | auto-random | Hex key for evidence encryption (min 32 chars) |
| `RETENTION_DAYS` | Optional | `30` | Auto-prune days for events/snapshots |
| `VOSK_MODEL_PATH` | Optional | `vosk-model` | Path to VOSK model directory |
| `SENTRIX_USER_NAME` | Optional | `Unknown User` | Used in dispatch packages |
| `SENTRIX_USER_ADDRESS` | Optional | — | Used in dispatch packages |
| `SENTRIX_USER_PHONE` | Optional | — | Used in dispatch packages |
| `SENTRIX_CAMERA_LOCATION` | Optional | `Main Entrance` | Used in dispatch packages |

---

## TCI Threat Levels

| Level | Status | TCI Range | Automated Escalation Actions |
|---|---|---|---|
| 1 | NORMAL | 0.00–0.25 | Local DB log only; HUD Green indicator |
| 2 | SUSPICIOUS | 0.26–0.50 | High-res snapshot saved; Twilio SMS notification |
| 3 | ELEVATED | 0.51–0.70 | + Local hardware siren pulse + AES-256-GCM Evidence Archival |
| 4 | HIGH | 0.71–0.85 | + Emergency Dispatch Package pre-populated (Police/Fire) |
| 5 | CRITICAL | 0.86–1.00 | + Automated Twilio Voice Call & Continuous Siren activation |

**Hard overrides** (bypass fusion): fire ≥ 0.70 → Level 5 immediately; weapon ≥ 0.70 → Level 5; weapon ≥ 0.50 → Level 4.

---

## Security Posture

- **Authentication**: All pages, API endpoints, WebSocket, and MJPEG stream require a valid HMAC-SHA256 session token (12h expiry)
- **Session storage**: Tokens stored in `httponly` cookies with `SameSite=Lax` — JavaScript cannot read them
- **Password safety**: Raw password never stored in cookie; HMAC signing key derived via PBKDF2
- **Upload safety**: Extension + MIME type validation (`image/jpeg`, `image/png`), path-traversal-safe filename sanitization, 10MB size cap
- **Evidence integrity**: AES-256-GCM encryption with HKDF-derived stable key; SHA-256 tamper detection hash
- **Graceful shutdown**: Ctrl+C cleanly drains the task queue and releases camera handles

---

## Capstone Academic Documentation (TIET Patiala)

The full Capstone evaluation suite is maintained in [`Capstone Docs/`](Capstone%20Docs/):
- [1_MID_SEMESTER_TECHNICAL_REPORT_SENTRIX.md](Capstone%20Docs/1_MID_SEMESTER_TECHNICAL_REPORT_SENTRIX.md) — 5-Chapter Technical Report adhering to CSED TIET template.
- [2_FINALIZED_DESIGN_MODEL.md](Capstone%20Docs/2_FINALIZED_DESIGN_MODEL.md) — Design improvements incorporating First Evaluation mentor feedback.
- [3_HARDWARE_SPECIFICATIONS_AND_BOM.md](Capstone%20Docs/3_HARDWARE_SPECIFICATIONS_AND_BOM.md) — Technical component datasheets, power budgets, and itemized BOM.
- [4_HARDWARE_INTEGRATION_AND_DEPLOYMENT_ARCHITECTURE.md](Capstone%20Docs/4_HARDWARE_INTEGRATION_AND_DEPLOYMENT_ARCHITECTURE.md) — Complete electronics schematics, wiring topology, and network isolation guide.
