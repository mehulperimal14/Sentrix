# SENTRIX — Future Advancements, Cloud Architecture & Dataset Expansion

This document serves as the master engineering roadmap for scaling SENTRIX from an on-premise edge prototype into an enterprise-grade, distributed edge-to-cloud surveillance ecosystem.

---

## 1. Hardware Integration & Edge Node Engineering

### A. Edge Physical Sentinel BOM (Student Budget: Under ₹20,000 INR)
* **Compute Engine**: Orange Pi 5 (4GB RAM, Rockchip RK3588S with 6 TOPS NPU) or Raspberry Pi 4 (4GB).
* **Optical Sensor**: Sony IMX291 / IMX327 1080p @ 30 FPS Low-Light Starvis UVC USB Camera.
* **Acoustic Sensor**: USB Omnidirectional Boundary Microphone (16 kHz PCM Mono).
* **Environmental Sensor**: DHT22 (AM2302) Digital Temperature & Humidity Sensor on GPIO 4 (Pin 7).
* **Acoustic Deterrent**: 12V 110dB Piezo Siren triggered via 5V Optocoupler Relay on GPIO 17 (Pin 11).
* **Power Subsystem**: 12V 3A DC SMPS adapter with LM2596 DC-DC step-down buck converter (12V $\rightarrow$ 5.15V 3A).

### B. Fail-Safe Offline Edge Mode
If the edge sentinel loses internet or cloud connectivity:
1. The local neural pipeline (YOLOv8n + ResNet18 + Acoustic CNN + XGBoost) continues uninterrupted on-device.
2. If $TCI \ge 0.85$ (Level 5 Critical Threat), the local GPIO directly energizes Relay Pin 17 to sound the 110dB siren immediately without waiting for cloud confirmation.
3. Encrypted AES-256-GCM evidence snapshots are queued locally on the high-endurance MicroSD card and synced automatically upon network recovery.

---

## 2. Cloud Server Architecture & Scalable Hosting

```
┌─────────────────────────────────────────────────────────────┐
│             Physical Edge Sentinel Nodes (1..N)            │
│  • Local Inference • GPIO Siren • Local SQLite Sync Buffer  │
└──────────────────────────────┬──────────────────────────────┘
                               │ TLS 1.3 / mTLS WebSocket & WebRTC
                               ▼
┌─────────────────────────────────────────────────────────────┐
│               Cloud Ingestion & Gateway Layer               │
│  • NGINX Reverse Proxy / Traefik Load Balancer             │
│  • WebRTC Media Server (Janus / MediaSoup) for Video Feeds  │
│  • WebSocket Gateway (FastAPI / Golang Goroutines)          │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                 Distributed Processing Mesh                 │
│  ┌────────────────────────┐     ┌────────────────────────┐  │
│  │   Redis Pub/Sub & MQ   │     │ High-Density GPU Nodes │  │
│  │ (Threat Event Fan-out) │     │ (YOLOv8x / SlowFast)   │  │
│  └───────────┬────────────┘     └───────────┬────────────┘  │
│              │                              │               │
│              ▼                              ▼               │
│  ┌────────────────────────┐     ┌────────────────────────┐  │
│  │ PostgreSQL + Timescale │     │ S3 / MinIO Object Vault│  │
│  │ (TCI Telemetry & Logs) │     │ (AES-256-GCM Evidence) │  │
│  └────────────────────────┘     └────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────┘
                               │ WebSocket State Feed (/ws/threat @ 30 FPS)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│            Next.js Cloud Command & Control Center           │
│  • Live Video Wall • Multi-Site Map • Automated Dispatch    │
└─────────────────────────────────────────────────────────────┘
```

### Hosting Strategy for Ultra-Low Latency & High Efficiency
1. **Edge-Assisted Inference**: Run lightweight Nano/Small models on the edge to detect candidate anomalies. Only stream full uncompressed video to the cloud when $TCI > 0.25$. This reduces bandwidth consumption by **90%**.
2. **WebRTC Video Pipeline**: Replace HTTP MJPEG with WebRTC (VP8/H.264 hardware encoding) to reduce glass-to-glass latency from 500ms down to **<80ms**.
3. **Database Architecture**:
   - **TimescaleDB**: Time-series storage for continuous temperature, acoustic energy, and TCI history.
   - **PostgreSQL**: Relational storage for incidents, residents, and dispatch logs.
   - **MinIO / AWS S3**: Immutable object storage for encrypted forensic snapshots with SHA-256 manifests.

---

## 3. Acceleration & Latency Optimization Roadmap

| Optimization Technique | Target Subsystem | Expected Latency Gain | Implementation Path |
|---|---|---|---|
| **RKNN / NPU Acceleration** | Rockchip RK3588 NPU | **3.2x faster inference** | Convert YOLOv8 and ResNet18 to `.rknn` format using RKNN-Toolkit2. |
| **TensorRT / ONNX FP16** | Cloud NVIDIA GPUs | **4.5x faster inference** | Quantize models to FP16 / INT8 with calibration caches. |
| **CoreML / Apple Neural Engine (ANE)** | macOS / iOS edge clients | **2.8x faster inference** | Export with `coremltools` targeting the Apple Neural Engine. |
| **Temporal Sliding Window Voting** | Violence & Anomaly Classifiers | **+5–8% Precision gain** | Aggregate predictions across an $N=8$ frame FIFO queue. |

---

## 4. Curated Datasets for Further Training & Accuracy Scaling

To push model accuracy, recall, and mAP into the 95%+ tier, train on these open-access benchmarks:

### A. Weapon & Concealed Threat Detection
* **Roboflow Gun and Knife Dataset**: [https://universe.roboflow.com/roboflow-universe-projects/gun-and-knife-detection](https://universe.roboflow.com/roboflow-universe-projects/gun-and-knife-detection) (30,000+ labeled images with pistols, knives, shotguns).
* **OpenImages V7 (Weapons Split)**: [https://storage.googleapis.com/openimages/web/index.html](https://storage.googleapis.com/openimages/web/index.html) (Massive real-world diversity).
* **US Military Small Arms Dataset**: High-resolution tactical firearms in varying lighting and occlusions.

### B. Violence, Altercation & Fight Detection
* **RWF-2000 (Real World Fight Dataset)**: [https://github.com/m-h-g/RWF-2000](https://github.com/m-h-g/RWF-2000) (2,000 high-definition surveillance fight videos specifically filmed on CCTV cameras).
* **UCF-Crime Dataset**: [https://www.crcv.ucf.edu/research/projects/anomaly-detection-in-surveillance-videos/](https://www.crcv.ucf.edu/research/projects/anomaly-detection-in-surveillance-videos/) (128 hours of real CCTV footage covering Fighting, Assault, Robbery, and Arson).
* **CCTV-Fight-2023**: Modern benchmark for multi-person scuffles and sudden aggressive motions.

### C. Fire, Smoke & Thermal Combustion
* **D-Fire Dataset**: [https://github.com/gaiasd/DFireDataset](https://github.com/gaiasd/DFireDataset) (21,000+ labeled fire and smoke frames under night and day conditions).
* **Corsican Fire Database**: [https://smoke.u-corsica.fr/](https://smoke.u-corsica.fr/) (Calibrated outdoor and indoor combustion images).
* **FLAME (Fire Luminosity Airborne-based Machine Learning Evaluation)**: Aerial and ground optical fire sequences.

### D. Acoustic Threat & Security Sounds
* **Google AudioSet (Security Split)**: [https://research.google.com/audioset/](https://research.google.com/audioset/) (Over 2 million labeled 10-second clips, filterable for Gunshot, Screaming, Explosion, and Alarm).
* **ESC-50 (Environmental Sound Classification)**: [https://github.com/karolpiczak/ESC-50](https://github.com/karolpiczak/ESC-50) (Standard 2,000 environmental audio recordings).
* **MIVIA Audio Events**: Benchmark for surveillance acoustics (glass breaking, gunshots, screams).

### E. Anomaly Detection in Surveillance
* **ShanghaiTech Campus Dataset**: [https://svip-lab.github.io/dataset/campus_dataset.html](https://svip-lab.github.io/dataset/campus_dataset.html) (13 scenes, 437 videos with diverse anomalous behaviors).
* **CUHK Avenue Dataset**: [http://www.ee.cuhk.edu.hk/~xgwang/CUHK_pedestrian/](http://www.ee.cuhk.edu.hk/~xgwang/CUHK_pedestrian/) (Pedestrian surveillance anomalies).

---

## 5. Indian Context & Local Scenario Datasets

Deploying SENTRIX in Indian residential, institutional, and commercial environments presents unique visual, acoustic, and environmental challenges (e.g., dense crowd dynamics, varied street lighting, festival firecrackers vs gunshots, and regional soundscapes). The following Indian-specific datasets are integrated into the expansion roadmap:

### A. Indian Visual & Surveillance Datasets
* **India Driving & Surveillance Dataset (IDD - IIIT Hyderabad)**: [https://idd.insaan.iiit.ac.in/](https://idd.insaan.iiit.ac.in/) (Massive dataset capturing unstructured Indian road, pedestrian, and perimeter surveillance conditions across varied lighting, weather, and congestion).
* **IIT Mandi Surveillance Action Dataset**: Benchmark capturing Indian outdoor movement, aggressive crowd gatherings, and physical altercations.
* **Smart Cities Mission CCTV Open Corpus**: Real-world municipal surveillance footage featuring Indian public spaces, mixed-use commercial corridors, and low-light street scenes.

### B. Indian Acoustic Soundscape Benchmark (False Positive Suppression)
In Indian environments, high-decibel acoustic false alarms commonly occur due to festive firecrackers, vehicle pressure horns, and street celebrations. The acoustic classifier requires training on specialized regional subsets:
* **Indian Urban Acoustic Soundscapes (IUAS)**: Audio recordings of festive firecrackers (Diwali/New Year) vs actual gunshot/explosion waveforms, enabling the Acoustic CNN to distinguish recreational pyrotechnics from ballistics.
* **Indian Traffic & Street Noise Corpus**: Captures high-decibel multi-tone horns, auto-rickshaw engine harmonics, and ambient bazaar noise to maintain high precision.
* **Regional SOS Keyword Acoustic Model**: Extension of Vosk speech recognition with Indian regional distress keywords (*"Bachao"*, *"Chor"*, *"Aag"*, *"Help"*).


---

## 6. Labeled Dataset Creation, Training & IoT Edge-to-Cloud Integration Plan

This section outlines the workflow for creating, labeling, and training custom model versions, as well as integrating physical inputs/outputs with the cloud-hosted Sentrix system.

### A. Custom Dataset Creation (Mobile & CCTV)
To align training data with real deployment environments:
1. **Negative Samples (Anti-False Positives)**: Record normal actions that look similar to threats:
   - *Normal*: Holding a black smartphone, wallet, or keychain.
   - *Threat*: Holding a replica/toy pistol, rifle, or knife.
   - *Normal*: Lighting a candle, gas stove, or incense.
   - *Threat*: Lighter flame near curtains, papers, or furniture.
2. **Environmental Diversity**: Film at different times of day (bright daylight, indoor lighting, low-light night conditions) and camera angles (elevated CCTV overhead vs. eye-level mobile camera).
3. **Action Clipping**: For behavior/violence models, clip video sequences into short **2 to 4-second** segments centered exactly on the action.

### B. Image Annotation & Labeling Tools
YOLOv8 expects bounding box label coordinates in the normalized YOLO format (`class_id x_center y_center width height`).
*   **Roboflow (Web/Cloud)**: Upload raw video clips, auto-extract frames at a custom interval (e.g., 2 FPS), and use polygon or box tools to label objects (knife, gun, fire, smoke). Export in YOLOv8 TXT format.
*   **CVAT (Desktop/Server)**: Open-source tool. Draw a box at frame 1 and frame 30, and CVAT will **automatically interpolate** bounding boxes for all intermediate frames.
*   **LabelImg / Labelme**: Simple, desktop-based local annotation tools.

### C. Fine-Tuning the Models on Custom Data
Place your custom datasets inside the `data/` directory:
- Weapon: `data/weapon_data/` (with `data.yaml` referencing classes `0: knife`, `1: long_gun`, `2: pistol`).
- Fire: `data/fire_smoke_data/` (with `data.yaml` referencing classes `0: fire`, `1: smoke`).

Run the provided fast-track training scripts:
```bash
# Weapon Training
.venv\Scripts\python.exe training/scripts/train_weapon_detector.py <epochs>

# Fire & Smoke Training
.venv\Scripts\python.exe training/scripts/train_fire_smoke_detector.py <epochs>
```
*Note: The scripts will automatically leverage CUDA GPU acceleration (on Windows/Linux) or Apple Metal MPS (on macOS).*

Copy the best weights file (e.g., `best.pt`) to the production folder:
`backend/models/v3_real/weapon_detector_real_v3.pt`

---

## 7. IoT Edge-to-Cloud Integration Protocol

To deploy Sentrix in an enterprise edge-to-cloud topology, hardware inputs (PIR sensors, tripwires) and outputs (strobe lights, physical sirens) are wired locally and synced to the cloud.

```
  [ Physical Sensors ]  --(GPIO In)-->  [ Edge Controller ]  --(HTTP POST)-->  [ Cloud Server ]
(PIR, Tripwires, Mic)                  (Raspberry Pi/ESP32)                  (FastAPI Backend)
                                                                                    │
                                                                               (WebSocket)
                                                                                    │
                                                                                    ▼
  [ Physical Actuator ]  <--(GPIO Out)--  [ Edge Controller ]  <───────────  [ Dashboard UI ]
(12V Siren, Door Lock)                   (Raspberry Pi/ESP32)                  (Browser Client)
```

### A. Local Edge Code (RPi GPIO Input)
Wired PIR/sensor pins are monitored locally on a Raspberry Pi or ESP32 and uploaded to the cloud server via HTTP:
```python
# edge_input.py (Runs on local Raspberry Pi)
import RPi.GPIO as GPIO
import time
import requests

PIR_PIN = 17
CLOUD_URL = "http://<YOUR_CLOUD_IP>:8000/api/hardware/trigger"

GPIO.setmode(GPIO.BCM)
GPIO.setup(PIR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

def motion_callback(channel):
    print("🚨 Sensor tripped! Ingesting to Cloud...")
    try:
        requests.post(CLOUD_URL, json={"sensor_id": "entrance_PIR", "status": "tripped"}, timeout=2)
    except Exception as e:
        print("Cloud offline:", e)

GPIO.add_event_detect(PIR_PIN, GPIO.RISING, callback=motion_callback, bouncetime=500)

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    GPIO.cleanup()
```

### B. Cloud Ingestion Endpoint
Add an API router endpoint inside `backend/web/routes.py` to process hardware signals and dynamically update system threat indices:
```python
# backend/web/routes.py
@router.post("/api/hardware/trigger")
async def handle_hardware_trigger(data: dict):
    sensor_id = data.get("sensor_id")
    status = data.get("status")
    
    if status == "tripped":
        from core.state import state
        # Boost the threat metrics dynamically in memory
        state.update_motion_score(1.0)
        
    return {"status": "success", "sensor": sensor_id}
```

### C. Local Actuator Control (Cloud-to-Edge Feedback)
The edge controller listens to the real-time websocket thread `/ws/threat` running on the cloud. When a threat level thresholds (e.g. `weapon_score > 0.8`), it trips the physical relay output to sound a 12V local siren:
```python
# edge_output.py (Runs on local Raspberry Pi)
import RPi.GPIO as GPIO
import websocket
import json

SIREN_RELAY_PIN = 27
GPIO.setmode(GPIO.BCM)
GPIO.setup(SIREN_RELAY_PIN, GPIO.OUT)

def on_message(ws, message):
    data = json.loads(message)
    weapon_score = data.get("weapon", 0.0)
    fire_score = data.get("fire", 0.0)
    
    if weapon_score > 0.75 or fire_score > 0.75:
        print("🚨 Critical threat score received! Sounding physical alarm!")
        GPIO.output(SIREN_RELAY_PIN, GPIO.HIGH)
    else:
        GPIO.output(SIREN_RELAY_PIN, GPIO.LOW)

ws = websocket.WebSocketApp("ws://<YOUR_CLOUD_IP>:8000/ws/threat", on_message=on_message)
ws.run_forever()
```

