# SENTRIX

**Multimodal edge threat intelligence and physical security system**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.4-EE4C2C.svg)](https://pytorch.org/)
[![YOLOv8](https://img.shields.io/badge/YOLOv8-ultralytics-00BFA5.svg)](https://docs.ultralytics.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-89%20passing-brightgreen.svg)](tests/)

> **Capstone Project CPG-299** · Computer Science and Engineering, Thapar Institute of
> Engineering and Technology, Patiala · Mentor: Dr. Ashutosh Mishra

SENTRIX watches a camera and a microphone, runs a stack of neural models over both,
and fuses their outputs into a single continuous **Threat Confidence Index (TCI)** in
`[0, 1]`. The TCI drives a five-level escalation ladder — from passive logging through
encrypted evidence capture to siren, SMS and a pre-populated emergency dispatch package.

Everything runs locally. There is no cloud inference and no subscription.

---

## Status

Software-only. Testing runs on a laptop webcam; no hardware is required or attached.
The hardware interfaces (`sentrix/io/`) are written and selected automatically, so
adding an ESP32 node or a GPIO relay later is a configuration change, not a rewrite.

| Subsystem | State | Notes |
|---|---|---|
| Person / weapon / fire detection | **Working** | One YOLO component, three heads, ByteTrack; weapon and fire heads are YOLO11n trained on the custom dataset and draw boxes, but by default do not drive the alarm (see *Custom dataset evaluation*) |
| TCI fusion + escalation | **Working** | Hysteresis, incident grouping, time-based smoothing |
| Encrypted evidence vault | **Working** | AES-256-GCM, HMAC hash-chained manifest |
| Scene classification | **Working** | normal / fire / weapon / assault / robbery; DINOv2 ViT-B/14 trained on the custom dataset (`models/v4_custom/`) |
| Violence classification | **Superseded** | The RWF-2000 model calls the test corridor a fight; replaced by the scene classifier's assault head whenever a scene model is installed |
| Acoustic classification | **Working** | Model is under-trained — see *Known limitations* |
| Face authorisation | **Working** | ArcFace 512-d via `insightface`; fails closed without it |
| Cross-camera re-identification | **Degraded** | Histogram fallback; install `torchreid` |
| Voice SOS | **Not wired** | Deferred; Vosk model retained |
| Hardware actuation | **Interface only** | No hardware attached |

Run `python tools/doctor.py` to see the live state of your own install rather than
trusting this table.

---

## Quickstart

```bash
git clone <repo> && cd Sentrix
```

**Windows**

```bash
run.bat
```

**macOS / Linux**

```bash
chmod +x run.sh && ./run.sh
```

Both scripts create `.venv`, install dependencies, seed `.env`, run pre-flight
checks and start the server. Set `SENTRIX_PASSWORD` in `.env`, then open
**http://127.0.0.1:8000/dashboard**.

Manual install:

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env          # then set SENTRIX_PASSWORD
python tools/doctor.py
python -m sentrix
```

**First run downloads the scene classifier's base weights.** The v4 scene model
is stored as a 57 MB delta on top of the public DINOv2 ViT-B/14 weights, which
`timm` fetches once (about 350 MB) into the Hugging Face cache. The engine
verifies them against a SHA-256 fingerprint before use. On a machine that will
run offline, cache them while online:

```bash
python -c "import timm; timm.create_model('vit_base_patch14_dinov2.lvd142m', pretrained=True)"
```

Without them the scene engine refuses to load and the pipeline falls back to
the v3 violence model, logging an error.

Strongly recommended — without a face-embedding backend, nobody can ever be
authorised:

```bash
pip install insightface onnxruntime
```

---

## Project layout

```
Sentrix/
├── sentrix/                    importable package — all runtime code
│   ├── config.py               ★ every tunable, in seconds not frames
│   ├── paths.py                filesystem layout
│   ├── app.py                  FastAPI factory + live loop
│   ├── io/                     ★ hardware seams
│   │   ├── sources.py          webcam | file | RTSP | multi-camera
│   │   └── actuators.py        console | sound | GPIO relay
│   ├── perception/             one score per modality
│   │   ├── detector.py         ★ YOLOv8: person + weapon + fire + tracking
│   │   ├── scene.py            ★ 5-class scene classifier, threshold-aligned to fusion
│   │   ├── violence.py         sliding-window action classification (fallback)
│   │   ├── audio.py            checkpoint-adaptive acoustic CNN
│   │   ├── identity.py         ★ fail-closed face authorisation
│   │   ├── behaviour.py        kinematics, resolution-independent
│   │   ├── reid.py             appearance re-identification
│   │   └── motion.py           frame differencing + is_night
│   ├── fusion/
│   │   ├── features.py         ★ the versioned feature contract
│   │   ├── calibration.py      Platt / isotonic, fitted not guessed
│   │   ├── tci.py              ★ TCI computation
│   │   └── policy.py           ★ level bands + hysteresis
│   ├── pipeline/
│   │   ├── engine.py           ★ single process() path — live and replay
│   │   ├── incidents.py        ★ incident grouping
│   │   ├── events.py           ★ JSONL event log
│   │   ├── escalation.py       level → action plan
│   │   └── state.py            live telemetry
│   ├── evidence/vault.py       ★ AES-256-GCM + hash-chained manifest
│   ├── alerting/               Twilio SMS, voice, dispatch packages
│   ├── storage/                SQLite, one row per incident
│   ├── web/                    routes, streaming, security, templates, static
│   ├── evaluation/             ★ custom-dataset ground truth, metrics, harness
│   └── utils/                  logging and telemetry
│
├── models/                     deployed weights
├── notebooks/                  ★ TCI analysis (contract, calibration, thresholds)
├── training/
│   ├── datasets/sentrix_custom/ ground truth of the custom frame dataset
│   ├── custom/                 ★ custom-dataset box annotation, training, screening
│   ├── scripts/                training as .py
│   ├── notebooks/              training as .ipynb
│   └── runs/                   all run outputs (v1_legacy, v2_real, v3_real, ucf_crime_dvs)
├── tools/                      doctor, replay, baseline, evaluate, auditors,
│                               custom-dataset evaluation
├── tests/                      89 regression tests
├── docs/                       specifications
├── Capstone Docs/              submissions
└── var/                        runtime output — gitignored, never web-served
```

★ marks files that carry the fixes described below.

---

## Configuration: the numbers, and why

Every tunable lives in [`sentrix/config.py`](sentrix/config.py), expressed in
**seconds or fractions of the frame diagonal** — never in frames or pixels. That
matters: the previous build used a per-frame EMA coefficient and pixel-per-frame
speed thresholds, so changing the inference rate or the camera resolution silently
changed the system's behaviour.

### Sliding windows

| Window | Value | Rationale |
|---|---|---|
| Violence clip | **2.0 s, 16 frames** | Action recognition converged on 2–3 s clips. I3D uses 64 frames @ 25 fps (2.56 s); VideoMAE and TimeSformer use 16 frames; RWF-2000 is built from 5 s clips |
| Violence hop | **1.0 s** (50% overlap) | An action straddling a boundary is still caught whole by the neighbouring window |
| Audio window | **4.0 s** | Fixed by `PRODUCTION_PREPROCESSING_CONTRACT.json`. Long enough to identify tonal events (siren, alarm) |
| Audio hop | **1.0 s** (75% overlap) | Short enough that an impulsive event (gunshot, glass break) lands fully inside at least one window |
| Inference rate | **6 Hz** | Human actions play out over hundreds of ms. 30 Hz of YOLO buys nothing and starves the edge device |

### Temporal thresholds

| Parameter | Value | Rationale |
|---|---|---|
| EMA time constant `τ` | **1.2 s** | `α = 1 − exp(−Δt/τ)`, recomputed from real elapsed time. The old fixed `α = 0.30` at ~5 Hz was `τ = 0.56 s` |
| Escalate dwell | **0.6 s** | Response latency is a product requirement — confirm fast |
| De-escalate dwell | **5.0 s** | Asymmetric on purpose: an intruder stepping behind a pillar must not clear the alarm |
| Loiter dwell | **30 s** | Video-analytics convention is 30–60 s stationary in a zone |
| Incident close | **20 s** quiet | Groups a contiguous episode into one incident |
| Evidence re-capture | **30 s** | Caps bundles within a long incident |
| Evidence pre/post roll | **8 s / 15 s** | VMS convention (5–10 s before, 10–30 s after) |
| SMS cooldown | **300 s** | Plus once-per-incident claiming |

**Hysteresis is what actually suppresses false alarms, not smoothing.** A single
large spike still drags an EMA across a threshold; it only takes slightly longer.
Requiring the candidate level to persist for a dwell period rejects it outright.

### Detection confidence

| Head | Threshold | Rationale |
|---|---|---|
| Person | 0.35 | Cheap to get wrong either way |
| Weapon | **0.55** | Feeds a hard override that can trigger a siren. The previous build shipped 0.25 |
| Fire / smoke | **0.50** | Same reasoning |

### Escalation ladder

| Level | TCI | Actions |
|---|---|---|
| 1 SECURE | 0.00–0.20 | Log only |
| 2 SUSPICIOUS | 0.20–0.40 | + snapshot, incident opened |
| 3 ELEVATED | 0.40–0.65 | + AES-256-GCM encrypted evidence |
| 4 HIGH | 0.65–0.85 | + SMS, dispatch package prepared |
| 5 CRITICAL | 0.85–1.00 | + siren, voice call |

The bands are currently asserted, matching the mid-semester report.
[`notebooks/03_tci_thresholds.ipynb`](notebooks/03_tci_thresholds.ipynb) derives them
from an explicit cost matrix instead, which is the defensible version.

---

## Why YOLOv8 does most of the vision work

One model family covers nearly the whole vision surface, and consolidating onto it
removed a lot of redundant machinery:

| Task | How |
|---|---|
| Person localisation | `yolov8n.pt`, class 0 |
| Weapon localisation | fine-tuned 3-class head — knife, long_gun, pistol |
| Fire & smoke localisation | fine-tuned 2-class head |
| **Multi-object tracking** | `model.track(persist=True)` with **ByteTrack**, built in |
| Pose / keypoints | `yolov8n-pose.pt` — implemented in `PoseEstimator`, not yet wired |

The tracking row replaced an entire dependency. `deep-sort-realtime` was never
installed, so the old tracker silently degraded to `id = index + 1` per frame —
track IDs were reused by different people between frames, which corrupted every
kinematic feature downstream. Loitering and running were computed over trajectories
that mixed several people together.

Consolidation also fixed a straightforward waste: the old code loaded `yolov8n.pt`
**twice** (once in `VisionEngine`, once as a COCO fallback inside
`CloudThreatEngine`) plus two custom detectors — four model loads for three jobs,
on separate schedules, so a weapon could be attributed to a frame in which the
person had already moved.

**Next step:** wire up `PoseEstimator`. Bounding-box centroids are a weak proxy for
what a person is doing; 17 keypoints make limb velocity and body configuration
directly observable. It also fixes the crawl heuristic, which currently infers
"crawling" from box aspect ratio and therefore fires on anyone the detector clips at
the frame edge.

---

## Where Vision Transformers fit

Short answer: **yes, in three specific places, and one of them is a clear win.**

### 1. Audio — the strongest case

**AST (Audio Spectrogram Transformer)** is a ViT applied to mel-spectrogram patches,
pretrained on AudioSet — which is exactly the corpus this project's audio classifier
is trained on. AST reports ~0.485 mAP on AudioSet against a 3-layer CNN's far lower
ceiling, and fine-tuning from AudioSet weights needs far less data than training from
scratch. With only 375 usable clips, transfer learning is not a nice-to-have; it is
the only realistic path to a usable acoustic model.

`sentrix/perception/audio.py` builds its architecture from the checkpoint's own
parameter names, so an AST checkpoint drops in behind the same interface.

### 2. Violence / action recognition

**VideoMAE** or **TimeSformer** are the SOTA replacements for the current per-frame
ResNet-18 — they model time directly instead of aggregating independent frames.
The caveat is real: a ViT-B is roughly 10× the compute of ResNet-18 and will not hold
6 Hz on an Orange Pi. Practical middle ground for edge: **MobileViT** or
**EfficientFormer**, or run VideoMAE only on frames the cheap path already flagged
(cascade).

### 3. Re-identification

**CLIP-ReID** and DINOv2 embeddings outperform OSNet on cross-camera matching and
need no ReID-specific training. `ReIDEngine` already normalises and cosine-matches,
so swapping the embedding function is a one-line change.

### Where ViTs do *not* belong here

Object detection. YOLOv8 at 640px is faster and more accurate than DETR-family
detectors at this scale, and small-object recall — a knife at 3 m — is precisely
where transformer detectors are weakest.

**Recommended order:** AST for audio (biggest gain per unit effort) → pose keypoints
for behaviour → temporal head for violence → ViT re-id last.

---

## Evaluation workflow

No training is involved. The point is to measure what already exists, honestly.

```bash
# 1. Is anything actually broken or missing?
python tools/doctor.py

# 2. Check your datasets for train/val contamination BEFORE quoting any metric
python tools/audit_splits.py manifest \
    --train training/runs/v3_real/violence/sample_manifest_train.csv \
    --val   training/runs/v3_real/violence/sample_manifest_val.csv

python tools/audit_splits.py images \
    --train data/weapon_data/train/images \
    --val   data/weapon_data/valid/images

# 3. Measure the motion-only baseline — the thing you claim to beat
python tools/baseline.py --clips data/clips

# 4. Run the same footage through SENTRIX, writing labelled event logs
python tools/replay.py --clips data/clips --no-audio

# 5. Produce the metrics table
python tools/evaluate.py --json report.json

# 6. Analyse
jupyter lab notebooks/
```

`tools/replay.py` uses the identical `Pipeline.process` that the live camera uses.
There is deliberately no separate evaluation path — the previous build had one, and
it had already drifted, which meant every published number came from code that was
not the product.

### Custom dataset evaluation

The team's own footage — 765 reviewed frames from 10 videos, classes `normal`,
`fire`, `weapon`, `assault`, `robbery` — is the one evaluation set that looks
like a real deployment. It is scored two ways, both through the product
pipeline with every side effect disabled:

```bash
python tools/eval_custom_frames.py --splits train,val,test   # per image
python tools/replay_custom_videos.py --videos all            # source videos, end to end
python tools/custom_eval_table.py                            # markdown tables
```

On this footage the shipped v3 models score **0.043 accuracy on the test video**:
the violence classifier calls the entire test corridor a fight, the weapon
detector is near chance (AUROC 0.54) and the fire detector finds none of the
flame jets.

Three configurations are kept on separate branches:

| Branch | Models |
|---|---|
| `sentrix/baseline-v3` | the shipped v3 models, unchanged |
| `sentrix/finetuned-v3` | the V3-notebook models fine-tuned on the custom data (ResNet-18 scene model, YOLOv8n heads, with rehearsal) |
| `sentrix/custom-v4` (this branch) | new models trained on the custom data: a DINOv2 ViT-B/14 scene classifier and YOLO11n heads |

| Held-out result | baseline | finetuned | **custom (this branch)** |
|---|---|---|---|
| Val, per image — accuracy / macro-F1 | 0.417 / 0.281 | 0.583 / 0.401 | **0.679 / 0.768** |
| Val, video replay — episodes detected, false alarms | 1 of 2, 0 | 1 of 2, 4 | **2 of 2, 0** |
| Test, per image — accuracy / macro-F1 | 0.043 / 0.114 | 0.677 / 0.460 | 0.720 / 0.419 |
| Test, video replay — episodes detected | 3 of 3 | 3 of 3 | **0 of 3** |
| False alarms per normal minute, all 10 videos | 13.96 | 7.12 | **0.79** |
| Pipeline latency per tick on CPU, mean / p95 | 121 / 205 ms | 117 / 132 ms | 213 / 245 ms |

This branch is the most accurate on everything that could be used to choose a
model (validation, cross-validation, false alarms) and is the recommended
configuration. Its blind spot is the stick swung in the test corridor, which
the fine-tuned branch catches: 39 training frames of a stick *carried* did not
teach a stick *swung*. Assault is not detected by either branch — it appears in
two videos in two different rooms, which no model generalises across. On CPU the
DINOv2 scene model (about 105 ms per frame) holds the live loop to about 4.7 Hz
instead of 6; with CUDA it runs on the GPU automatically. Protocol,
model selection and the full breakdown are in
[`docs/6_CUSTOM_DATASET_EVALUATION.md`](docs/6_CUSTOM_DATASET_EVALUATION.md).

### The TCI notebooks

| Notebook | What it does |
|---|---|
| [`01_tci_feature_contract.ipynb`](notebooks/01_tci_feature_contract.ipynb) | Documents the 8-feature contract, verifies the deployed booster, and reproduces the train/serve skew |
| [`02_tci_calibration.ipynb`](notebooks/02_tci_calibration.ipynb) | Fits an isotonic calibrator so TCI becomes a probability; reliability diagram and ECE |
| [`03_tci_thresholds.ipynb`](notebooks/03_tci_thresholds.ipynb) | Derives level bands from a cost matrix; computes false alarms per hour |

Both 02 and 03 refuse to write anything when no real labelled event logs are present.
They fall back to a synthetic demo set and say so loudly.

---

## What changed in v3

Fifteen defects, each pinned by a regression test in [`tests/`](tests/).

### Critical

**1. TCI alarmed on an empty room.** The booster was fitted on raw scores in `[0, 1]`
but served through an unfitted sigmoid (`A=-1.5, B=0.5`, its own docstring called them
"dummy params") that compressed inputs to `[0.378, 0.731]`. The model never saw its
own training distribution.

| Scene | Before | After |
|---|---|---|
| Empty room, day | 0.422 → **L2 SUSPICIOUS** | **0.012 → L1 SECURE** |
| Unknown person present | 0.437 | 0.292 |
| Usable range | `[0.42, 0.84]` | `[0.01, 0.95]` |

An empty room and an occupied one differed by **0.015**. TCI was effectively a
constant.

**2. Alert storm.** Level 2 sent an SMS and level 3 sounded the siren — two steps more
aggressive than every document — while the system sat permanently at level 2. Result
on disk: **1,489 evidence bundles, 2,415 snapshots, 642 MB.** Side effects now bind to
**incidents**, not frames, and each fires once per incident.

**3. Evidence was destroying itself.** Filenames had one-second resolution, so writes
inside the same second silently overwrote each other. Now millisecond + counter + random
suffix, plus an HMAC hash-chained manifest — deleting or altering a bundle breaks the
chain at that point and everywhere after it.

**4. Everything under `/static` was unauthenticated.** FastAPI applies no auth to static
mounts, and the mount contained the evidence vault, incident snapshots and enrolled face
photographs. Runtime output moved to `var/` and is served only through authenticated
`/media/*` routes.

**5. Face "recognition" was a colour histogram.** With `face_recognition` not installed,
the live path compared a 512-bin HSV histogram of the centre of the frame at cosine
≥ 0.68. Two frames of the same room routinely pass that. Since authorisation suppresses
*all* alerting, it was an authentication bypass. Now fails closed.

### Correctness

**6.** The violence classifier was never invoked — `classify()` ran it only
`if frame is not None`, and both call sites passed two arguments. 42 MB loaded, zero
inferences. Now windowed and wired.

**7.** `is_night` — one of five model inputs — was never computed. Always zero.

**8.** Weapon, fire and behaviour could not reach the score. With the 5-feature booster,
a knife at 0.5 confidence and no knife at all produced an identical TCI.

**9.** EMA smoothing was frame-rate dependent, and every hard override returned early
without updating the smoother, so the EMA resumed from a stale pre-incident value.

**10.** Re-identification compared unit vectors against an L2 distance threshold of
120.0 — unreachable, since unit vectors are at most 2.0 apart. Every person collapsed
onto `GID-1`.

**11.** Tracking degraded to per-frame indices, corrupting all kinematics.

**12.** `process()` and `process_eval_frame()` were duplicated and had drifted.

**13.** Telemetry no-opped unless `SENTRIX_EVAL_MODE=1`, so the "operational per-engine
logging" produced nothing in normal operation.

### Infrastructure

**14.** `requirements.txt` could not start the app — missing `jinja2`, `sqlalchemy`,
`python-dotenv`, `cryptography`, `scipy`, `sounddevice`, `pandas`; shipped
`pycryptodome` (unused) instead of `cryptography` (imported). `run.bat`/`run.sh` never
created a venv or installed anything, despite the docs saying they did.

**15.** `hardware/siren.py` contained no GPIO code at all — it was a desktop beep player
whose sound path pointed at a pre-restructure directory. `docs/3` showed a GPIO snippet
captioned as that file's contents.

### Removed

`frontend/` (stale duplicate of a Next.js app that does not exist), `next_prompt.md`,
`anomaly_classifier.pt` (a ResNet-18 with `val_acc = 0.39` at epoch 1, never loaded by
anything), and 642 MB of false-positive runtime output.

---

## Known limitations

Stated plainly, because they will come up in review.

**The TCI fusion model is still synthetic.** `training/scripts/refit_xgboost.py`
generates its own labels from a formula, so its `R² = 0.984` measures only that XGBoost
can memorise that formula. It is not a model result. The path out is
`tools/replay.py` → `notebooks/02` — and note that the V3 retraining notebook was right
to refuse to fabricate this dataset.

**Every model was measured on a contaminated split.** `tools/audit_splits.py`
reproduces it:

| Dataset | Contamination |
|---|---|
| Violence (RWF-2000) | **52.7% of validation rows** come from source videos also in training |
| Weapon | 12.2% of validation images have a train near-duplicate at cos > 0.99 |
| Fire / smoke | 20.0% at cos > 0.99 |

The published mAP and accuracy figures are optimistic by an unknown margin. Re-split by
source and re-measure before the final report.

**The audio model is under-trained.** 375 usable clips across 4 classes; `scream` has 8
validation samples. The reported macro-F1 of 0.7036 is a single-epoch spike selected
from a noisy series whose surrounding epochs average ~0.58 — selection on a 75-sample
validation set, not a converged result. It also has no `gunshot` and no `glass_break`
class, both of which the proposal promises, and `gunshot` is required for the
firecracker-discrimination research gap. See `docs/5` §3.1.

**The UCF-Crime anomaly model is not wired in, and its test metric is a sign error.**
`python tools/diagnose_anomaly.py` shows normal footage scoring 0.923 and anomalous
footage 0.685 — the ranking is inverted. Inverted, AUC is 0.806 against a validation
AUC of 0.826.

**"Court-admissible" is not a claim this system can make.** Admissibility is a legal
determination about process and custody, not a property a cipher confers. The
defensible term is **tamper-evident**, and the docs should be softened accordingly.

**The false-alarm reduction claim is not yet measured.** `tools/baseline.py` provides
the comparator, but the missing ingredient is hours of continuous unattended footage
from a real deployment. Public CCTV datasets are trimmed and curated and cannot give a
rate per hour of an empty room.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/1_SYSTEM_ARCHITECTURE.md`](docs/1_SYSTEM_ARCHITECTURE.md) | Architecture and data flow |
| [`docs/2_MODEL_BENCHMARKS_AND_KPIS.md`](docs/2_MODEL_BENCHMARKS_AND_KPIS.md) | Training results |
| [`docs/3_HARDWARE_SPECIFICATION_AND_BOM.md`](docs/3_HARDWARE_SPECIFICATION_AND_BOM.md) | Bill of materials, pinouts |
| [`docs/4_DEPLOYMENT_AND_RUNBOOK.md`](docs/4_DEPLOYMENT_AND_RUNBOOK.md) | Deployment and operations |
| [`docs/5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md`](docs/5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md) | Roadmap |
| [`docs/6_CUSTOM_DATASET_EVALUATION.md`](docs/6_CUSTOM_DATASET_EVALUATION.md) | Custom-dataset protocol and results |

All five were rewritten for v3. Each carries explicit **"Corrected from v2.1"**
callouts where the previous version stated something that was not true of the
code — those corrections need to be carried into the final report, because an
examiner comparing the docs against `training/runs/` will find them.

---

## Team

| Member | Roll No. | Subsystem |
|---|---|---|
| Kartik Garg | 102303478 | System architecture, vision engines, backend core |
| Akshay Ranveer | 102303453 | Biometric identity, re-identification, tracking |
| Prashant Gagneja | 102353011 | Acoustic classification, digital signal processing |
| Harshit Mishra | 102319039 | Behavioural intelligence, anomaly classification |
| Mehul Perimal | 102315144 | Evidence cryptography, database, dashboard |

**Mentor:** Dr. Ashutosh Mishra, Associate Professor, CSED, TIET Patiala

---

## Standards

BIS IS 16910 / IEC 62676 (video surveillance) · NIST SP 800-38D / FIPS 197 (AES-GCM) ·
FIPS 180-4 (SHA-256) · CERT-In cybersecurity directions · MoHUA Smart Cities ICCC
video-analytics guidelines · OSHA 1910.95 / NFPA 72 (audible alarm levels)

## License

MIT
