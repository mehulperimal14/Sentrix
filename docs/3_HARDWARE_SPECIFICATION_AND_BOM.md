# SENTRIX — Hardware Specifications & Bill of Materials
**Version 3.0 · Updated: August 2026**

> **No hardware is currently attached.** SENTRIX runs today on a laptop webcam.
> Everything in this document is a build plan, and the software interfaces it
> plugs into already exist — see §5 and §7.
>
> The bill of materials in §2 and §3 is unchanged from v2.1 and remains accurate.
> Section 5 has been corrected: the previous version presented GPIO code that did
> not exist anywhere in the repository.

---

## 1. Hardware Architecture Overview

SENTRIX supports two deployment architectures depending on budget and autonomy requirements:

| Architecture | Mode | Primary Use Case | Total Cost (INR) |
|---|---|---|---|
| **A — Wi-Fi Sentinel Node** | ESP32-S3 + Laptop Backend | Student capstone / lab PoC | ~₹3,240 |
| **B — Autonomous Edge Unit** | Orange Pi 5 / RPi 4 (standalone) | Production / field deployment | ~₹14,850 |

---

## 2. Architecture A: Wi-Fi Sentinel Node (ESP32-S3 + Laptop)

### How It Works
The ESP32-S3 handles sensor ingestion (camera + mic + environmental sensors) and streams data over Wi-Fi to the SENTRIX FastAPI backend running on a local laptop or PC. The laptop runs all neural inference (YOLOv8, ResNet18, Acoustic CNN, XGBoost TCI).

```
┌─────────────────────────────────────────────────────────┐
│               ESP32-S3 Sentinel Node                     │
│  OV2640 Camera → RTSP Stream → Backend Laptop           │
│  INMP441 Mic  → PCM Audio   → Backend Laptop            │
│  DHT22 Sensor → Telemetry   → Backend Laptop            │
│  Relay Output ← GPIO17      ← Backend (if Level 5)      │
└─────────────────────────────────────────────────────────┘
                         │ Wi-Fi LAN
                         ▼
┌─────────────────────────────────────────────────────────┐
│         SENTRIX Backend Server (Laptop / PC)             │
│  Runs: YOLOv8 + ResNet18 + Acoustic CNN + XGBoost       │
│  Computes TCI (0.00–1.00) and triggers actuators        │
└─────────────────────────────────────────────────────────┘
```

### Architecture A — Bill of Materials

| Category | Component | Specification | Source | Cost (INR) |
|---|---|---|---|---|
| **Microcontroller** | ESP32-S3-WROOM-1 / ESP32-CAM | Dual-core 240MHz, 8MB PSRAM, Wi-Fi + BLE, OV2640 | Robu.in / Amazon India | ₹1,150 |
| **Microphone** | INMP441 I2S Digital Mic | Omnidirectional, 24-bit I2S, 16kHz capable | Robu.in | ₹220 |
| **Thermal Sensor** | DHT22 (AM2302) | Temperature & Humidity, ±0.5°C accuracy | Robu.in | ₹350 |
| **Relay Module** | 5V 1-Channel Optocoupler Relay | Optically isolated, triggers 12V 10A | Robu.in | ₹120 |
| **Siren Actuator** | 12V 110dB Piezo Siren | High-decibel audible security alarm | Amazon India / Local | ₹450 |
| **Power Supply** | 12V 2A DC Adapter + LM2596 Buck Converter | Stepped down to 5.0V for ESP32 | Robu.in / Amazon | ₹550 |
| **Housing** | Compact ABS Enclosure + Jumper Wires | 100×68×50mm | Local Hardware | ₹400 |
| **TOTAL** | | **Complete Wi-Fi Sentinel Node** | | **₹3,240 INR** |

### Architecture A — Pinout

```
  ESP32-S3 Pin            Connected Peripheral
  ─────────────────────────────────────────────────────
  3.3V (Pin 3V3)     →   INMP441 VDD & DHT22 VCC
  GND                →   Common ground (all sensors)
  GPIO 4             →   DHT22 DATA
  GPIO 17            →   5V Relay IN (siren trigger)
  GPIO 14 (I2S CLK)  →   INMP441 SCK
  GPIO 15 (I2S WS)   →   INMP441 WS (LRCLK)
  GPIO 16 (I2S SD)   →   INMP441 SD (data out)
  OV2640 Camera      →   Built-in (ESP32-CAM module)
```

---

## 3. Architecture B: Autonomous Edge Unit (Orange Pi 5 / Raspberry Pi 4)

### How It Works
The single-board computer (SBC) is a fully self-contained edge server. It runs the complete SENTRIX neural stack (YOLOv8, ResNet18, Acoustic CNN, XGBoost) directly on-device. Operation continues even if internet or LAN is severed — local evidence is encrypted to MicroSD and the siren triggers autonomously.

```
┌─────────────────────────────────────────────────────────┐
│     Autonomous Edge Sentinel Unit (RPi 4 / OPi 5)       │
│                                                          │
│  ● Sony IMX291 1080p Starvis Low-Light Camera (USB)     │
│  ● USB Omnidirectional Boundary Microphone               │
│  ● DHT22 Temperature Sensor (GPIO Pin 7)                 │
│  ● 110dB Piezo Siren Relay (GPIO Pin 11)                 │
│  ● Full Neural Stack on-device (YOLOv8 + ResNet18)      │
│  ● Local AES-256-GCM Evidence Vault (MicroSD)            │
└─────────────────────────────────────────────────────────┘
```

### Architecture B — Bill of Materials

| Category | Component | Specification | Source | Cost (INR) |
|---|---|---|---|---|
| **Compute Core** | Orange Pi 5 (4GB) / Raspberry Pi 4 (4GB) | RK3588S (8-core, 6 TOPS NPU) / BCM2711 | Robu.in / Amazon | ₹7,500 |
| **MicroSD Storage** | SanDisk Extreme 64GB U3 A2 | High endurance, OS + model storage | Amazon India | ₹1,750 |
| **Optical Sensor** | Sony IMX291 / IMX327 1080p UVC Camera | 1080p @ 30 FPS, Starvis low-light, 120° FOV | Robu.in / Amazon | ₹3,200 |
| **Acoustic Sensor** | USB Omnidirectional Boundary Mic | 16-bit 48kHz, hardware noise cancellation | Amazon India | ₹850 |
| **Thermal Sensor** | DHT22 (AM2302) Module | −40°C to +80°C, ±0.5°C accuracy | Robu.in | ₹350 |
| **Siren Actuator** | 12V 110dB Piezo Security Siren | High-decibel compact alarm | Amazon / Local | ₹450 |
| **Relay Module** | 5V 1-Channel Optocoupler Relay | Optically isolated, 12V 10A | Robu.in | ₹120 |
| **Power Supply** | 12V 3A DC Adapter (SMPS) | Main system power | Amazon India | ₹450 |
| **Step-Down Converter** | LM2596 / MP1584 Buck (12V → 5.15V 3A) | Clean 5.15V USB-C power for SBC | Robu.in | ₹180 |
| **Enclosure** | IP65 ABS Junction Box + Cable Glands | 150×100×70mm weatherproof | Local Hardware | ₹650 |
| **Cooling** | USB-C Pigtail + Aluminum Heat Sinks | Passive thermal dissipation | Robu.in | ₹350 |
| **TOTAL** | | **Complete Autonomous Edge Unit** | | **₹14,850 INR** |

### Architecture B — Pinout

```
  SBC 40-Pin GPIO Header    Connected Peripheral
  ──────────────────────────────────────────────────────
  Pin 1  (3.3V Power)    →  DHT22 Temperature Sensor VCC
  Pin 2  (5.0V Power)    →  5V Optocoupler Relay VCC
  Pin 7  (GPIO 4 / BCM4) →  DHT22 DATA Line
  Pin 9  (GND)           →  DHT22 GND
  Pin 11 (GPIO 17/BCM17) →  5V Relay IN (siren trigger)
  Pin 14 (GND)           →  Relay GND
  USB 3.0 Port           →  Sony IMX291 1080p Camera
  USB 2.0 Port           →  USB Boundary Microphone
```

---

## 4. Architecture Selection Guide

| Criteria | Choose Architecture A (₹3,240) | Choose Architecture B (₹14,850) |
|---|---|---|
| **Budget** | Minimal hardware cost | Higher budget for standalone device |
| **Inference** | Offloaded to laptop/PC | On-device (no laptop required) |
| **Autonomy** | Depends on network + PC | Fully offline capable |
| **Video Quality** | OV2640 (640×480) | Sony IMX291 1080p low-light |
| **Best for** | Student capstone / PoC demo | Production installation / field |

---

## 5. Siren Relay Control

Actuation sits behind the `Actuator` interface in
[`sentrix/io/actuators.py`](../sentrix/io/actuators.py). `build_siren()` selects
the strongest available backend at startup and logs which one it chose, so the
running system reports its real capability rather than an aspirational one:

| Backend | When selected | Behaviour |
|---|---|---|
| `GpioSiren` | `RPi.GPIO` importable | Drives the relay, auto-releases after the burst |
| `SoundSiren` | Host audio available | Plays `web/static/sounds/siren.wav` |
| `ConsoleSiren` | Neither | Logs a warning; **no physical deterrent** |

Override with `SENTRIX_SIREN=gpio|sound|console` in `.env`.

`/api/health` exposes `siren_backend` and `siren_is_real`, so the dashboard
cannot claim a deterrent that does not exist.

### Wiring (when the relay arrives)

```
  SBC 40-pin header        Peripheral
  ──────────────────────────────────────────────
  Pin 2  (5.0V)        →   Relay VCC
  Pin 11 (GPIO17/BCM17)→   Relay IN
  Pin 14 (GND)         →   Relay GND
  Pin 1  (3.3V)        →   DHT22 VCC
  Pin 7  (GPIO4/BCM4)  →   DHT22 DATA
  Pin 9  (GND)         →   DHT22 GND
```

> **Active-low matters.** Most inexpensive optocoupler relay boards are
> active-low, so driving the pin HIGH *releases* the relay. `GpioSiren` defaults
> to `active_low=True`. Getting this backwards produces a siren that runs
> continuously until level 5 and then stops — the exact opposite of intended
> behaviour, and it will not be obvious from the logs.

Timing comes from `AlertConfig`: `siren_duration_seconds = 5.0`,
`siren_cooldown_seconds = 60.0`, and the siren fires from level 5 only. It is
also claimed once per incident, so a flapping signal cannot retrigger it.

> **Corrected from v2.1.** The previous §5 showed a `GPIO.output(GPIO_SIREN_PIN,
> GPIO.HIGH)` snippet captioned "hardware/siren.py (simplified)". No such code
> existed. That file was a desktop beep player (`winsound.Beep` / `afplay` /
> `paplay`) whose sound path pointed at a directory that no longer existed after
> an earlier restructure, so even the beep fell through to a fallback tone.
> Doc 5 v2.1 additionally listed the GPIO relay as operational.

---

## 6. Camera Configuration

SENTRIX supports multiple simultaneous camera sources via `CAMERA_SOURCES` in `.env`:

```bash
# Single USB webcam
CAMERA_SOURCES=0

# Multiple cameras
CAMERA_SOURCES=0,1

# RTSP IP camera
CAMERA_SOURCES=rtsp://admin:password@192.168.1.100:554/stream

# Mixed
CAMERA_SOURCES=0,rtsp://192.168.1.101:554/stream1
```

Sources are handled by [`sentrix/io/sources.py`](../sentrix/io/sources.py). Each
runs its own reader thread that keeps the capture buffer drained — without that,
OpenCV queues frames internally and latency grows without bound on an RTSP feed.

A phone running any IP-webcam app is a valid second camera and needs no purchase:

```bash
CAMERA_SOURCES=0,http://192.168.1.20:8080/video
```

That is the cheapest route to a genuine two-view, non-overlapping setup for
cross-camera re-identification.

> **Trade-off worth stating in the report.** `MultiSource` tiles feeds into one
> mosaic for a single inference pass. Tiling four 1080p streams into one 640 px
> inference frame quarters each stream's effective resolution, so small-object
> recall — a knife at three metres — degrades sharply. Beyond two cameras,
> per-camera inference is the correct design. Tiling is retained for demo parity
> with the mid-semester submission.

---

## 7. Adding Hardware Later — What Is Already Built

The software was restructured so that attaching hardware is an integration task,
not a rewrite. Two interfaces carry the whole boundary.

**Input** — [`sentrix/io/sources.py`](../sentrix/io/sources.py). `FrameSource` has
four implementations today (`CaptureSource` for webcam and RTSP, `FileSource` for
replay, `MultiSource` for tiling). An ESP32-S3 pushing RTSP already works through
`CaptureSource` with no new code — it is a URL in `CAMERA_SOURCES`.

**Output** — [`sentrix/io/actuators.py`](../sentrix/io/actuators.py). `GpioSiren`
is written, tested for import safety on non-Pi hosts, and selected automatically
the moment `RPi.GPIO` becomes importable.

Realistic bring-up sequence:

| Step | Work | Depends on |
|---|---|---|
| 1 | Flash ESP32-S3, expose an RTSP stream | Hardware only |
| 2 | Point `CAMERA_SOURCES` at it | Config line |
| 3 | Wire the relay per §5, install `RPi.GPIO` | Hardware only |
| 4 | Verify with `python tools/doctor.py` — `siren_is_real` must read `True` | — |
| 5 | DHT22 telemetry | **New code.** No thermal input exists in the feature contract |

Step 5 is the only one requiring development. Thermal telemetry is deliberately
absent from `CONTRACT_V2`: with no sensor attached, adding the feature would mean
feeding the fusion model a constant, which is exactly the defect that `is_night`
represented in the previous build. Add the feature when the sensor exists, and
retrain the fusion model against a contract that includes it.
