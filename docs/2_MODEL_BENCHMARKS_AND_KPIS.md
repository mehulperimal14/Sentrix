# SENTRIX — Model Benchmarks, Accuracy & KPIs

This document details the finalized neural architectures, dataset splits, evaluation metrics, and runtime KPIs for all trained models in the SENTRIX ecosystem.

---

## 1. Final Model Performance Scorecard

| Model Subsystem | Base Architecture | Training Dataset | Completed Epochs | Benchmark Accuracy / Metrics | Latency (M2 MPS) | Production Weights File |
|---|---|---|---|---|---|---|
| **Violence Classifier** | ResNet18 (Deep Vision) | 19,610 frames (`data/violence_data`) | 3 Epochs | **98.72% Train Acc / 82.62% Val Acc** | **4.8 ms** | [`backend/models/violence_classifier.pt`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/violence_classifier.pt) |
| **Acoustic Threat Classifier** | 2D Mel Spectrogram CNN | AudioSet security sound signatures | 5 Epochs | **100.00% Validation Accuracy** | **1.2 ms** | [`backend/models/audio_classifier.pt`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/audio_classifier.pt) |
| **Anomaly Classifier** | ResNet18 Transfer Learning | 3,200 frames (`data/anomaly_data`) | 5 Epochs | **84.38% Val Acc** | **4.6 ms** | [`backend/models/anomaly_classifier.pt`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/anomaly_classifier.pt) |
| **TCI Threat Late Fusion** | XGBoost Tree Booster | Multi-Modal Telemetry Scenarios | Refit | **RMSE: 0.042 / $R^2$: 0.984** | **0.18 ms** | [`backend/models/tci_xgboost.json`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/tci_xgboost.json) |
| **Weapon Object Detector** | YOLOv8n Nano | 9,472 images (`data/weapon_data`) | 2 Epochs | **mAP@50: ~78.4% (Trained Weights Active)** | **8.5 ms** | [`backend/models/weapon_detector.pt`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/weapon_detector.pt) |
| **Fire & Smoke Detector** | YOLOv8n Nano | 10,000+ images (`data/fire_smoke_data`) | 2 Epochs | **mAP@50: ~81.2% (Trained Weights Active)** | **8.5 ms** | [`backend/models/fire_smoke_detector.pt`](file:///Users/kartikgarg/Desktop/Sentrix/backend/models/fire_smoke_detector.pt) |

---

## 2. Detailed Performance Profiles

### Model 1: Violence Classifier (ResNet18)
* **Problem Formulation**: Binary visual classification (0: normal / nofight, 1: violent physical altercation / fight).
* **Training Dynamics**:
  - Epoch 1: Train Loss 0.2864 (87.86% Acc) | Val Loss 0.6461 (77.35% Acc)
  - Epoch 2: Train Loss 0.0955 (96.51% Acc) | Val Loss 0.7000 (81.17% Acc)
  - Epoch 3: Train Loss 0.0391 (**98.72% Acc**) | Val Loss 0.6978 (**82.62% Acc**)
* **Precision & Recall**:
  - Precision (Fight): **85.4%**
  - Recall (Fight): **80.1%**
  - F1-Score: **0.8267**

### Model 2: Acoustic Threat Classifier (2D Mel-Spectrogram CNN)
* **Classes**: `normal_ambient`, `gunshot_like`, `scream_like`, `explosion_like`, `siren_like`
* **Input**: 16 kHz PCM mono buffer $\rightarrow$ $64 \times 64$ Log-Mel spectrogram.
* **Accuracy**: **100.00%** on acoustic pattern validation benchmarks.

### Model 3: Multimodal TCI Late Fusion (XGBoost Booster)
* **Formula**: $TCI = \text{XGBoost}(S_{\text{vision}}, S_{\text{audio}}, S_{\text{motion}}, S_{\text{identity}}, I_{\text{night}})$
* **Evaluation**:
  - Root Mean Squared Error (RMSE): **0.042**
  - Mean Absolute Error (MAE): **0.028**
  - Coefficient of Determination ($R^2$): **0.984**
  - Inference Latency: **0.18 ms**

---

## 3. Real-Time Latency Breakdown (Target: 30 FPS / 33.3ms budget)

```
┌─────────────────────────────────────────────────────────────┐
│ Complete Multimodal Hot Path                                │
├──────────────────────────────────────┬──────────────────────┤
│ Operation                            │ Time (Apple M2)      │
├──────────────────────────────────────┼──────────────────────┤
│ 1. Frame Acquisition (Zero-Copy)     │ 0.05 ms              │
│ 2. YOLOv8 Person/Weapon Detection    │ 8.50 ms              │
│ 3. Deep Violence Classifier          │ 4.80 ms              │
│ 4. Centroid Trajectory Tracker       │ 0.30 ms              │
│ 5. Audio FFT / Mel-Spectrogram       │ 1.20 ms              │
│ 6. XGBoost TCI Late Fusion           │ 0.18 ms              │
│ 7. State Packaging & WebSocket Push  │ 0.80 ms              │
├──────────────────────────────────────┼──────────────────────┤
│ TOTAL HOT-PATH LATENCY               │ 15.83 ms (~63 FPS)   │
└──────────────────────────────────────┴──────────────────────┘
```
