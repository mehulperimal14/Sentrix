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

