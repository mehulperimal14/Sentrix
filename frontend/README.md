# SENTRIX Frontend & Cloud Surveillance Dashboard

## Vision & Architecture Roadmap

The SENTRIX platform is engineered for a two-tier edge-to-cloud security topology:

```
┌──────────────────────────────────────────────────────────┐
│             Physical Edge Sentinel Unit                 │
│  • Wide-Angle Optical Sensor (1080p @ 30 FPS)            │
│  • Directional Acoustic Array (16 kHz PCM Mono)          │
│  • DHT22/DS18B20 Environmental Temperature Sensor        │
│  • 110dB Active Hardware Siren Relay Actuator            │
│  • Local On-Device Neural Pipeline (YOLOv8 + ResNet18)   │
└────────────────────────────┬─────────────────────────────┘
                             │ Encrypted Telemetry / TLS WebSocket
                             ▼
┌──────────────────────────────────────────────────────────┐
│             SENTRIX Cloud Processing Layer               │
│  • FastAPI Multimodal Telemetry Hub                      │
│  • Late Fusion Threat Orchestration Engine (TCI ML)      │
│  • AES-256-GCM Encrypted Evidence Vault                  │
│  • Twilio Multi-Channel Emergency Dispatch Orchestrator  │
└────────────────────────────┬─────────────────────────────┘
                             │ State WebSocket & Low-Latency Stream
                             ▼
┌──────────────────────────────────────────────────────────┐
│         Next.js Web Command Dashboard (Frontend)         │
│  • Real-Time Threat Confidence Index Gauge (L1-L5)       │
│  • Multi-Camera Ultra-Low Latency Video Wall             │
│  • Acoustic Spectrogram & Anomaly Visualizer             │
│  • Facial Authorization Registry & Audit Vault           │
│  • Pre-Populated Emergency 911 / Police Dispatch UI      │
└──────────────────────────────────────────────────────────┘
```

---

## Hardware Edge Unit Specifications

| Subsystem | Component | Protocol / Interface | Function |
|---|---|---|---|
| **Optical Ingestion** | 1080p UVC / RTSP Sony Starvis IMX327 | USB 3.0 / RTSP 554 | Low-light night vision & threat object tracking |
| **Acoustic Ingestion** | Omnidirectional Condenser Mic | 3.5mm / I2S ADC | Acoustic anomaly (gunshot, scream, explosion) capture |
| **Thermal Sensing** | DHT22 / DS18B20 Temp Sensor | GPIO / 1-Wire | Fire risk pre-ignition ambient temperature monitoring |
| **Deterrent Siren** | 110dB Piezo Siren + Optocoupler | 5V TTL GPIO Pin 17 | Automated or manual acoustic escalation deterrent |
| **Compute Controller** | Raspberry Pi 4 / Jetson Orin / Mac Host | ARM64 / x86_64 | Local inference & encrypted payload dispatch |

---

## Real-Time WebSocket API Specification

Endpoint: `/ws/threat`
Payload Format: JSON

```json
{
  "tci": 0.88,
  "level": 4,
  "status": "HIGH",
  "reason": "Confirmed threat indicator: weapon or intrusion",
  "incident_type": "weapon",
  "scores": {
    "vision": 0.92,
    "audio": 0.65,
    "motion": 0.40,
    "weapon": 0.88,
    "fire": 0.00,
    "identity": 0.10
  },
  "uncertainty": 0.05,
  "top_factors": [
    { "name": "weapon", "score": 0.88, "weight": 0.15, "contribution": 0.132 }
  ],
  "authorized": false,
  "fps": 28.5,
  "latency_p95": 0.035
}
```

---

## Local Development Dashboard

Currently, SENTRIX includes a built-in server-rendered command dashboard available at `http://127.0.0.1:8000/dashboard` powered by Jinja2 + WebSocket streaming for zero-dependency local edge operation.
