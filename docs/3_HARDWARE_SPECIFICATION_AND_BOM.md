# SENTRIX — Hardware Specifications, Dual Architecture & Bill of Materials (BOM)

## Student Budget Constraint: Under ₹20,000 INR

To accommodate different deployment scales and budgets, SENTRIX supports two distinct physical hardware architectures:
1. **Architecture A: Ultra-Budget Wi-Fi Sentinel Node (ESP32-S3 + Cloud/Server AI)** — **Total Cost: ~₹3,240 INR**
2. **Architecture B: Autonomous Edge Sentinel Node (Orange Pi 5 / Raspberry Pi 4)** — **Total Cost: ~₹14,850 INR**

Both designs operate under the **₹20,000 INR ceiling**, providing options based on cost and autonomy requirements.

---

## 1. Architectural Comparison Matrix

| Technical Metric | Architecture A: ESP32-S3 Wi-Fi Node | Architecture B: Orange Pi 5 / RPi 4 Node |
|---|---|---|
| **Topology Paradigm** | **Thin Edge + Host Server/Cloud AI** | **Thick Edge (All-In-One Autonomous AI)** |
| **Total Hardware Cost** | **₹3,240 INR ($39 USD)** | **₹14,850 INR ($178 USD)** |
| **AI Inference Location** | Host Laptop / Cloud Server GPU | Local On-Chip (RK3588 NPU / ARM CPU) |
| **Optical Ingestion** | OV2640 / OV5640 (720p/SVGA @ 15–20 FPS) | Sony IMX291/IMX327 (1080p @ 30 FPS Starvis) |
| **Acoustic Ingestion** | INMP441 I2S Digital Mic (16 kHz PCM Mono) | USB Condenser Boundary Mic (16 kHz / 48 kHz) |
| **Environmental Telemetry** | DHT22 on GPIO 4 | DHT22 on GPIO 4 (Pin 7) |
| **Siren Actuator** | 5V Relay on GPIO 17 $\rightarrow$ 12V 110dB Siren | 5V Relay on GPIO 17 (Pin 11) $\rightarrow$ 12V 110dB Siren |
| **End-to-End Latency** | **60 ms – 120 ms** (includes Wi-Fi streaming) | **15 ms – 25 ms** (zero-copy memory bus) |
| **Offline Independence** | Requires Wi-Fi to reach server for inference | **100% Autonomous**: Operates without internet |
| **Power Consumption** | **1.8W – 2.5W** (can run days on a power bank) | **10W – 15W** (requires dedicated 12V adapter) |
| **Primary Use Case** | Student capstone, lab demo, low-cost multi-room | Critical infrastructure, perimeter perimeter |

---

## 2. Architecture A: Ultra-Budget Wi-Fi Sentinel Node (ESP32-S3)

### A. How It Works
The ESP32-S3 acts as a lightweight physical sensor hub. It captures video frames from the OV2640 camera, samples 16kHz audio from the INMP441 microphone, and reads ambient temperature from the DHT22 sensor. It transmits this telemetry over Wi-Fi (HTTP MJPEG & WebSocket) to the SENTRIX backend server (your laptop or cloud). When a Level 5 Critical threat is determined by the TCI fusion engine, the server transmits a trigger packet back to the ESP32, which pulls GPIO 17 HIGH to sound the 110dB siren.

```
┌─────────────────────────────────────────────────────────────┐
│                  ESP32 Sentinel Edge Node                   │
│  (Total Cost: ~₹3,240 INR | Low Power: 2W)                  │
│                                                             │
│  • ESP32-S3 / ESP32-CAM (OV2640 Camera Module)             │
│  • INMP441 I2S Digital Microphone                           │
│  • DHT22 Temperature & Humidity Sensor                      │
│  • 5V Optocoupler Relay + 12V 110dB Siren                   │
└──────────────────────────────┬──────────────────────────────┘
                               │ Wi-Fi WebSocket / HTTP Stream
                               ▼
┌─────────────────────────────────────────────────────────────┐
│            SENTRIX Backend Server (Laptop / Cloud)          │
│                                                             │
│  • Ingests Video & 16kHz Audio Stream from ESP32            │
│  • Runs Neural Stack (YOLOv8 + ResNet18 + Acoustic CNN)     │
│  • Computes TCI (0.00 – 1.00) via XGBoost                   │
│  • If Level 5 Critical: Sends {"siren": true} to ESP32      │
└─────────────────────────────────────────────────────────────┘
```

### B. Architecture A Bill of Materials (BOM)

| Category | Component | Description / Specification | Source / Vendor | Approx Cost (INR) |
|---|---|---|---|---|
| **Microcontroller** | **ESP32-S3-WROOM-1 / ESP32-CAM** | Dual-core 240MHz, 8MB PSRAM, Wi-Fi + BLE, OV2640 Cam | Robu.in / Amazon India | **₹1,150** |
| **Microphone** | INMP441 I2S Digital Mic Module | Omnidirectional, 24-bit I2S digital output | Robu.in | **₹220** |
| **Thermal Sensor** | DHT22 (AM2302) Sensor | Temperature & Humidity digital sensor | Robu.in | **₹350** |
| **Relay Module** | 5V 1-Channel Optocoupler Relay | Optically isolated, triggers 12V 10A DC load | Robu.in | **₹120** |
| **Siren Actuator** | 12V 110dB Piezo Siren | High-decibel audible security alarm | Amazon India / Local | **₹450** |
| **Power Supply** | 12V 2A DC Adapter + LM2596 Buck Converter | Power SMPS stepped down to 5.0V for ESP32 | Robu.in / Amazon | **₹550** |
| **Housing & Misc** | Compact ABS Enclosure + Jumper Wires | 100x68x50mm waterproof housing | Local Hardware | **₹400** |
|---|---|---|---|---|
| **TOTAL BOM (OPTION A)** | | **Complete Wi-Fi Sentinel Node** | | **₹3,240 INR** |

### C. Architecture A Pinout & Wiring

```
  ESP32-S3 Pin          Connected Peripheral
  ────────────────────────────────────────────────────────
  3V3 (Pin 3.3V)   ───  INMP441 VDD & DHT22 VCC
  GND (Ground)     ───  Common Ground (Sensors, Relay, SMPS)
  GPIO 4           ───  DHT22 DATA Line
  GPIO 17          ───  5V Relay Module IN (Signal Trigger)
  GPIO 14 (I2S CLK)───  INMP441 SCK (Serial Clock)
  GPIO 15 (I2S WS) ───  INMP441 WS (Word Select / LRCLK)
  GPIO 16 (I2S SD) ───  INMP441 SD (Serial Data Out)
  5V (VIN)         ───  5.0V Output from LM2596 Buck Converter
```

---

## 3. Architecture B: Autonomous Edge Sentinel Node (Orange Pi 5 / RPi 4)

### A. How It Works
The single-board computer acts as a fully self-contained edge server. It runs the entire neural pipeline (YOLOv8, ResNet18, Acoustic CNN, XGBoost, and FastAPI backend) directly on-device. Even if external internet or local network connectivity is severed, the unit continues continuous physical threat surveillance, captures encrypted forensic evidence to its local MicroSD card, and triggers the hardware siren autonomously.

```
┌─────────────────────────────────────────────────────────────┐
│          Autonomous Edge Sentinel Unit (RPi 4 / OPi 5)      │
│  (Total Cost: ~₹14,850 INR | Complete On-Premise AI)        │
│                                                             │
│  • 1080p Sony IMX291 Low-Light Starvis Camera               │
│  • High-Sensitivity USB Boundary Microphone                 │
│  • DHT22 Temperature / Humidity Sensor (GPIO Pin 7)         │
│  • 110dB Piezo Siren Relay Actuator (GPIO Pin 11)           │
│  • On-Device Neural Stack (YOLOv8 + ResNet18 + TCI Fusion)  │
│  • Local AES-256-GCM Evidence Vault (MicroSD Card)          │
└─────────────────────────────────────────────────────────────┘
```

### B. Architecture B Bill of Materials (BOM)

| Category | Component | Description / Specification | Source / Vendor | Approx Cost (INR) |
|---|---|---|---|---|
| **Compute Core** | **Orange Pi 5 (4GB) / Raspberry Pi 4 (4GB)** | Rockchip RK3588S (8-core, 6 TOPS NPU) / BCM2711 | Robu.in / Amazon India | **₹7,500** |
| **MicroSD Storage** | SanDisk Extreme 64GB U3 A2 | High endurance OS + model storage | Amazon India | **₹750** |
| **Optical Ingestion** | **Sony IMX291 / IMX327 1080p UVC Camera** | 1080p @ 30 FPS, Starvis Low-Light, 120° FOV | Robu.in / Amazon | **₹3,200** |
| **Acoustic Ingestion** | USB Omnidirectional Boundary Mic | 16-bit 48kHz ADC, hardware noise cancellation | Amazon India / Robu.in | **₹850** |
| **Thermal Sensing** | DHT22 (AM2302) Sensor Module | Temperature (-40°C to +80°C), ±0.5°C accuracy | Robu.in | **₹350** |
| **Acoustic Deterrent** | 12V 110dB Piezo Security Siren | Compact single-tone high-decibel alarm | Amazon India / Local | **₹450** |
| **Actuator Relay** | 5V 1-Channel Optocoupler Relay Module | Optically isolated, triggers 12V 10A DC load | Robu.in | **₹120** |
| **Power Supply** | 12V 3A DC Adapter (SMPS) | Main system power source | Amazon India | **₹450** |
| **Step-Down Converter** | LM2596 / MP1584 Buck Converter (12V $\rightarrow$ 5.15V 3A) | Clean 5.15V USB-C power for SBC | Robu.in | **₹180** |
| **Enclosure & Wiring** | IP65 Weatherproof ABS Junction Box + Wires | 150x100x70mm enclosure with cable glands | Local Hardware | **₹650** |
| **Cooling & Accessories** | USB-C Pigtail Cable + Aluminum Heat Sinks | Passive thermal dissipation accessories | Robu.in | **₹350** |
|---|---|---|---|---|
| **TOTAL BOM (OPTION B)** | | **Complete Autonomous Edge Unit** | | **₹14,850 INR** |

### C. Architecture B Pinout & Wiring

```
  SBC 40-Pin Header     Connected Peripheral
  ────────────────────────────────────────────────────────
  Pin 1  (3.3V Power)   ───  DHT22 Temperature Sensor VCC
  Pin 2  (5.0V Power)   ───  5V Optocoupler Relay Module VCC
  Pin 7  (GPIO 4 / BCM4)───  DHT22 DATA Line
  Pin 9  (Ground)       ───  DHT22 GND
  Pin 11 (GPIO 17/BCM17)───  5V Relay Module IN (Signal Trigger)
  Pin 14 (Ground)       ───  Relay Module GND
  USB 3.0 Port          ───  Sony IMX291 1080p Optical Camera
  USB 2.0 Port          ───  USB Boundary Microphone
```

---

## 4. Selection Guidance for Students & Developers

* **Choose Architecture A (ESP32-S3 @ ₹3,240 INR)** if:
  * You are building a student capstone or laboratory proof-of-concept and want to minimize component expenditure.
  * You have an accessible laptop or desktop that can run the SENTRIX backend server on the same Wi-Fi network.

* **Choose Architecture B (Orange Pi / Raspberry Pi @ ₹14,850 INR)** if:
  * You need a self-contained, standalone device that operates autonomously on a wall or outdoor pole.
  * You require high-definition 1080p video with hardware night-vision optics and sub-20ms local response times.
