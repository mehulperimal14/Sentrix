# 3. SENTRIX — Original Hardware Plan & Bill of Materials

> **Superseded.** This is the hardware plan from the mid-semester report
> (August 2026): an ESP32-S3 node with a laptop (Architecture A) or an Orange
> Pi 5 / Raspberry Pi 4 edge unit (Architecture B). The team has since chosen a
> **Raspberry Pi 5** unit; its parts list, prices (checked October 2026),
> wiring, pin table and software are in
> [doc 8](8_RASPBERRY_PI5_INTEGRATION.md) and `deploy/pi5/README.md`.
> §1–§4 below are kept as the record of the original plan; their prices were
> not re-checked. §5–§7 describe the code on branch `sentrix/portal`.
>
> **No hardware is attached yet.** SENTRIX runs on a laptop with a webcam, a
> video file or the demo reel as its camera. The Pi hardware code exists and is
> tested against fakes only.

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
[`sentrix/io/actuators.py`](../sentrix/io/actuators.py). `build_siren()` picks
the strongest backend available at startup and logs which one it chose:

| Backend | When selected | Behaviour |
|---|---|---|
| `GpioSiren` | a real pin factory resolves (lgpio on a Pi 5), or `SENTRIX_SIREN=gpio` (then mock pins off a Pi, `siren_is_real` false) | Drives the relay through gpiozero; a timer releases it |
| `SoundSiren` | host audio available | Plays `web/static/sounds/siren.wav` |
| `ConsoleSiren` | neither | Logs a warning; **no physical deterrent** |

Override with `SENTRIX_SIREN=auto|gpio|sound|console` (`auto`, the value
`.env.example` ships, picks the best backend; before M10 it gave the console
stub). `/api/health` exposes `siren_backend` and `siren_is_real`, so the
dashboard cannot claim a deterrent that does not exist; `GET /api/node` adds
the arming state.

Rules in the code (doc 8 §6 has the wiring and the pin table):

* **Pin and polarity.** GPIO17, **active-high** by default (an NPN transistor
  driver, `gpio=17=op,dl` in `config.txt` keeps it off at boot). Opto relay
  boards: `SENTRIX_SIREN_ACTIVE_HIGH=0` with `gpio=17=op,dh`. RPi.GPIO does
  not work on a Pi 5, which is why the code uses gpiozero.
* **Safety.** The pin starts off, a timer releases it, and one on-period never
  exceeds `SENTRIX_SIREN_MAX_ON_SECONDS` (90 s). The systemd unit forces the
  pin off after any exit (`deploy/pi5/`).
* **Arming.** A siren test is refused (409) while the arming switch
  (`SENTRIX_SIREN_ARM_PIN`, or `SENTRIX_SIREN_ARMED=0`) is off; a real alarm
  still drives the relay, and the physical switch is what cuts the sound.
* **Tests.** `POST /api/node/siren-test`: 1 s, at most once a minute, shared
  with the Portal's `siren.test` command (owner with a fresh step-up).
* **Timing.** `AlertConfig`: `siren_duration_seconds = 5.0`,
  `siren_cooldown_seconds = 60.0`; the siren fires from level 5 only and is
  claimed once per incident.

Still to verify on the bench with an LED in place of the siren
(`deploy/pi5/README.md`).

> **History.** v2.1 showed a `GPIO.output(GPIO_SIREN_PIN, GPIO.HIGH)` snippet
> captioned "hardware/siren.py (simplified)". No such code existed; that file
> was a desktop beep player. v3 then wrote an RPi.GPIO siren defaulting to
> active-low, which would have fallen back to the console stub on a Pi 5.

---

## 6. Camera Configuration

`CAMERA_SOURCES` in `.env`, comma-separated, handled by
[`sentrix/io/sources.py`](../sentrix/io/sources.py):

| Value | Source |
|---|---|
| `0`, `1` | USB webcam by index (`0,1` tiles two) |
| `rtsp://user:pass@host/stream`, `http://…/video` | IP camera, ESP32-S3 node, phone IP-webcam app |
| a video file path | file played as a camera at its own rate (the demo and the benches use this) |
| `picamera:0` | Raspberry Pi camera through Picamera2: a 1280×720 main stream and a 640×480 analysis stream (`sentrix/io/picamera.py`) |
| `demo-reel:<reel.json>` | the demo camera: idle footage in a loop, scenes on request (`sentrix/io/demo_reel.py`, doc 7 §15 item 3) |

Each source runs its own reader thread that keeps the capture buffer drained;
without it OpenCV queues frames and latency grows without bound on RTSP.
Credentials in a camera URL are redacted wherever the name appears (health,
logs, link messages).

> **Trade-off.** `MultiSource` tiles feeds into one mosaic for a single
> inference pass. Tiling four 1080p streams into one 640 px frame quarters each
> stream's resolution, so small-object recall (a knife at three metres) drops
> sharply. Beyond two cameras, per-camera inference is the right design.

---

## 7. What is built for hardware today

| Part | Code | State |
|---|---|---|
| Siren relay | `io/actuators.py` | gpiozero, tested on mock pins |
| Status LEDs (GPIO5/12/13); no buzzer (D19) | `io/gpio.py` | tested on mock pins; off unless `SENTRIX_GPIO_ENABLED=1` |
| PIR (GPIO23), door contact (GPIO24), mains sense (GPIO16), tamper, arming key | `io/gpio.py` | debounced inputs; mains lost/restored and tamper go to the event log and the Portal |
| CPU temperature, throttling, 5 V supply, BME280 / DHT22 | `io/node.py`, `io/sensors.py` | tested against a fake `vcgencmd` and `/sys` tree; off unless `SENTRIX_NODE_ENABLED=1` |
| Pi camera | `io/picamera.py` | tested with a fake `picamera2` |
| Wi-Fi setup | `io/network.py`, `/setup/wifi` | nmcli; only in setup mode |
| systemd watchdog, unit, installer, network failover | `io/watchdog.py`, `deploy/pi5/` | not run on a Pi |
| 4G modem SMS | `alerting/channels/modem.py` | tested against a fake AT serial port |

Bring-up order, wiring and the LED test: `deploy/pi5/README.md` and doc 8.
Thermal telemetry is reported on the Node page but is still not a fusion
feature: adding a feature means a new contract version and a refit on data
that includes it.
