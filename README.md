# SENTRIX

**Multimodal edge threat intelligence and physical security system**

[![Python 3.10–3.14](https://img.shields.io/badge/Python-3.10--3.14-blue.svg)](https://www.python.org/)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.4-EE4C2C.svg)](https://pytorch.org/)
[![YOLOv8](https://img.shields.io/badge/YOLOv8-ultralytics-00BFA5.svg)](https://docs.ultralytics.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-215%20passing-brightgreen.svg)](tests/)

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
| TCI fusion + escalation | **Working** | Confirmed per-class evidence, hysteresis, incident grouping |
| Encrypted evidence vault | **Working** | AES-256-GCM, HMAC hash-chained manifest; each sidecar records the evidence votes, scene probabilities and identity behind the frame |
| Incident records | **Working** | Every field from the incident's peak tick; written at open so a crash keeps it; operator verdicts on the Events page (a false alarm cancels its pending dispatch) |
| Scene classification | **Working** | normal / fire / weapon / assault / robbery; DINOv2 ViT-B/14 trained on the custom dataset (`models/v4_custom/`) |
| Violence classification | **Superseded** | The RWF-2000 model calls the test corridor a fight; replaced by the scene classifier's assault head whenever a scene model is installed |
| Acoustic classification | **Working** | Model is under-trained — see *Known limitations* |
| Face authorisation | **Working** | ArcFace 512-d via `insightface`, per tracked person, on its own thread (about once a second); enrol from the monitoring camera with quality checks; fails closed without it |
| Cross-camera re-identification | **Degraded** | Histogram fallback; install `torchreid` |
| Voice SOS | **Not wired** | Deferred; Vosk model retained |
| Hardware actuation | **Interface only** | No hardware attached |

Run `python tools/doctor.py` to see the live state of your own install rather than
trusting this table.

---

## Quickstart

Runs on a laptop CPU — Windows, macOS or Linux, **Python 3.10–3.14** (fresh
installs verified on 3.12 and 3.14). Installing, testing and evaluating need no
GPU, camera or microphone: the repository ships the models, the team's custom
dataset and its ten source videos.

### 1. Get the code

```bash
git clone https://github.com/mehulperimal14/Sentrix.git
cd Sentrix
```

The clone is about 0.8 GB (weights, dataset and videos are tracked). On Windows,
clone to a short path such as `C:\src\Sentrix`: long-path support is off by
default and some files inside the virtual environment are deeply nested.

### 2. Start it — one command

| Windows (PowerShell or cmd) | macOS / Linux |
|---|---|
| `.\run.bat` | `./run.sh` |

Both scripts create `.venv`, install `requirements.txt` (several minutes the first
time, then only when it changes), create `.env` with a generated dashboard
password — **printed once in the terminal** — plus generated session and evidence
keys, run the pre-flight checks and start the server. Open
**http://127.0.0.1:8000** and log in with that password (it is `SENTRIX_PASSWORD`
in `.env`).

The first start downloads about 600 MB of public weights, once: the DINOv2 base of
the scene classifier (~330 MB, Hugging Face cache) and InsightFace `buffalo_l`
(~280 MB, `~/.insightface`). The dashboard answers once the log prints
`dashboard: http://…`.

* **Linux:** `sudo apt install python3-venv libportaudio2 libgl1 libglib2.0-0`.
  On a CPU-only machine, install the CPU build of PyTorch first to skip ~2.5 GB of
  CUDA wheels:
  `pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu`.
* **NVIDIA GPU:** install the CUDA build of PyTorch before `requirements.txt`
  (`--index-url https://download.pytorch.org/whl/cu126`); the scene model moves to
  the GPU automatically.

**Manual install**, instead of the scripts:

```powershell
# Windows PowerShell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python tools\setup_env.py        # creates .env, prints the generated password
python -m sentrix
```

```bash
# macOS / Linux (Git Bash on Windows: source .venv/Scripts/activate)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python tools/setup_env.py        # creates .env, prints the generated password
python -m sentrix
```

### 3. No webcam? Use a bundled video

Add to `.env`:

```bash
CAMERA_SOURCES=training/datasets/sentrix_custom/videos/gun.mov   # loops; any video path works
SENTRIX_AUDIO=0         # keep the microphone closed
SENTRIX_SIREN=console   # the weapon clips reach L5: print the siren instead of sounding it
```

Leave the Twilio fields in `.env` blank and every SMS or call is only printed to
the console. Other cameras: `CAMERA_SOURCES=1` (second webcam),
`rtsp://user:pw@host/stream` (IP camera), or several separated by commas.

### 4. Test and evaluate

```bash
python -m pytest                     # 215 tests, ~20 s; no camera, mic, network or GPU
python tools/doctor.py               # pre-flight report (also probes webcams 0-2)
python tools/bench_live.py           # live loop on a bundled video: video fps, AI Hz, latency

# The custom-dataset evaluation, on the bundled frames and videos
python tools/eval_custom_frames.py --splits train,val,test --tag local    # per image
python tools/replay_custom_videos.py --videos all --tag local             # 10 videos end to end (~6 min)
python tools/custom_eval_table.py custom local                            # compare with the published run
python tools/gate_report.py --replay training/runs/custom_eval/local/replay_all.json
```

Your runs land in `training/runs/custom_eval/<tag>/` (ignored by git); the
published runs quoted in `docs/6` are `baseline`, `custom`, `finetuned`, `m1` and
`m5`. Every evaluation tool builds the pipeline with side effects disabled — no
alert, siren or database row, whatever `.env` contains.

### 5. Enrol faces

**Access** page → *Capture from camera* (2–3 photos per person, facing the camera)
or *Upload photos*. Until someone is enrolled, everyone is "unknown"; in home
mode (the default) that is only logged.

`.env.example` documents every setting (camera, armed mode, thresholds, alerts);
the algorithmic constants live in `sentrix/config.py`.

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
│   ├── datasets/sentrix_custom/ custom dataset: 765 frames, 10 source videos, ground truth
│   ├── custom/                 ★ custom-dataset box annotation, training, screening
│   ├── scripts/                training as .py
│   ├── notebooks/              training as .ipynb
│   └── runs/                   run outputs (v1_legacy … v3_real, custom, custom_eval)
├── tools/                      doctor, setup_env, bench_live, replay tools, evaluation,
│                               gate_report, auditors
├── tests/                      215 regression tests
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
| Inference rate | **6 Hz** cap | Human actions play out over hundreds of ms. 30 Hz of YOLO buys nothing and starves the edge device. Inference runs on its own thread and always takes the newest frame; the preview runs at the camera's rate whatever the AI rate |

### Temporal thresholds

| Parameter | Value | Rationale |
|---|---|---|
| Evidence confirmation | **2 of 3** fresh evaluations | Rejects single-frame spikes before they reach the TCI; no display EMA (it made the gauge lag the level) |
| Escalate dwell | **0 s** L2/L3, **0.6 s + 2 obs** L4/L5 | Evidence is already confirmed; the costly levels keep a second guard |
| De-escalate dwell | **5.0 s** | Asymmetric on purpose: an intruder stepping behind a pillar must not clear the alarm |
| Loiter dwell | **30 s** | Video-analytics convention is 30–60 s stationary in a zone |
| Incident close | **20 s** quiet | Groups a contiguous episode into one incident |
| Evidence re-capture | **30 s** | Caps bundles within a long incident |
| Evidence pre/post roll | **8 s / 15 s** | VMS convention (5–10 s before, 10–30 s after) |
| SMS cooldown | **300 s** | Plus once-per-incident claiming |

**Confirmation is what suppresses false alarms, not smoothing.** A single large
spike still drags an EMA across a threshold; it only takes slightly longer. Requiring
a source to cross on 2 of its last 3 evaluations rejects it outright.

### Detection confidence

| Head | Threshold | Rationale |
|---|---|---|
| Person | 0.35 | Cheap to get wrong either way |
| Weapon | **0.55** | Threshold of the *detector vote*. One confirmed source can reach L3 at most; L4+ needs the scene classifier to agree. Only `pistol`/`knife`/`long_gun` boxes vote; the custom head's `stick` class (test mAP50 0.003) is drawn, never counted |
| Fire | **0.50** | Same. Only boxes labelled `fire` vote; smoke is not in the alarm path (public smoke heads read a backlit webcam as smoke) |

### How evidence becomes an alarm (`fusion/evidence.py`)

The first live run produced ~171 false weapon alarms per hour, each one tick of
the scene classifier barely over its threshold in a room it had never seen
(CLAUDE.md §5). Every threat class now passes four rules before it can move the
level:

| Rule | Default | Why |
|---|---|---|
| Confirmation | a source must cross on **2 of the last 3 ticks** (counted, not timed) | at 0.5 Hz a time window held one frame |
| Person gate | weapon votes count only with a person seen in the last **2 s** (skipped, and said so in the reason, if the person detector is down) | 65 % of live weapon alarms had nobody in view |
| Fresh evaluations only | a cached value (a failed inference, the violence model between hops) is carried, never counted again | one evaluation must not confirm itself |
| Corroboration cap | one confirmed source lands in **L3** (0.45–0.62); a classifier **and** a detector box, both confirmed, score 0.70 + 0.25 × the weaker side's margin: **L4** at threshold, **L5** once both are ~60 % of the way to certainty | the least reliable source cannot text or siren alone; two classifiers are not independent |
| Experimental cap | assault and robbery stop at **L2** | they exist in 1–3 clips and cannot be validated |

Scene thresholds are the checkpoint's raised to at least weapon 0.50 / fire 0.45
(the checkpoint's were tuned at 41 % threat prevalence), unless a deployment
calibration names a class (`var/calibration/scene.json` or
`SENTRIX_SCENE_THRESHOLDS`, values in (0, 1); other classes keep their floors).
**Identity never relaxes threat evidence**: frame-level identity cannot tell a
resident from a stranger next to them, so a weapon counts the same either way until
per-track identity exists (CLAUDE.md M4).

**Presence is context, not a threat.** In home mode (default) an unknown person
is logged only. `SENTRIX_ARMED=1` raises an intrusion at L3 once someone has stayed
unrecognised for 3 s (brief detector misses do not restart the clock). The TCI is
the strongest confirmed class evidence — a transparent rule — and it is not
smoothed: the number on the gauge, in the log, in the SMS and on the incident is
the number the level was decided on. Persistence is counted once, in the evidence
layer, so L2/L3 commit at the first confirmed tick; L4/L5 (SMS, siren) also need
0.6 s and 2 observations over their threshold. A level steps down after 5 s without
touching its threshold. Replaying the live session with these rules: 30 L2+ / 6 L4
alarm onsets become 0 (`python tools/replay_decisions.py var/events/<session>.jsonl`
re-runs the current rules on any session you have recorded).

### Escalation ladder

| Level | TCI | Actions |
|---|---|---|
| 1 SECURE | 0.00–0.20 | Log only |
| 2 SUSPICIOUS | 0.20–0.40 | Incident opened (log and dashboard only) |
| 3 ELEVATED | 0.40–0.65 | + snapshot, AES-256-GCM encrypted evidence |
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
Steps 2–4 need the public corpora the v3 models were trained on (`data/…`, not
in this repository); everything on the bundled custom dataset is in the
Quickstart, step 4.

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
python tools/replay.py --clips data/clips

# 5. Produce the metrics table
python tools/evaluate.py --json report.json

# 6. Analyse
jupyter lab notebooks/
```

`tools/replay.py` uses the identical `Pipeline.process` that the live camera uses,
with every side effect disabled and no camera opened.
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
| `sentrix/custom-v4` (the models on `main`) | new models trained on the custom data: a DINOv2 ViT-B/14 scene classifier and YOLO11n heads |

| Held-out result | baseline | finetuned | **custom (on `main`)** |
|---|---|---|---|
| Val, per image — accuracy / macro-F1 | 0.417 / 0.281 | 0.583 / 0.401 | **0.679 / 0.768** |
| Val, video replay — episodes detected, false alarms | 1 of 2, 0 | 1 of 2, 4 | **2 of 2, 0** |
| Test, per image — accuracy / macro-F1 | 0.043 / 0.114 | 0.677 / 0.460 | 0.720 / 0.419 |
| Test, video replay — episodes detected | 3 of 3 | 3 of 3 | **0 of 3** |
| False alarms per normal minute, all 10 videos | 13.96 | 7.12 | **0.79** |
| Pipeline latency per tick on CPU, mean / p95 | 121 / 205 ms | 117 / 132 ms | 213 / 245 ms |

The custom models are the most accurate on everything that could be used to
choose a model (validation, cross-validation, false alarms) and ship on `main`.
This table predates the end-to-end repair: it scores each frame with the
checkpoint's own thresholds. With the repaired defaults (thresholds raised to
safety floors, 2-of-3 confirmation) a fresh `eval_custom_frames.py` gives val
0.607 / 0.718, and the video replay catches the same 13 of 18 episodes with
**0 false alarms** at every level (docs/6 §6.8). Its blind spot is the stick swung in the test corridor, which
the fine-tuned branch catches: 39 training frames of a stick *carried* did not
teach a stick *swung*. Assault is not detected by either branch — it appears in
two videos in two different rooms, which no model generalises across. The latency
row comes from the evaluation harness, which stubs identity and audio. The live
configuration (`python tools/bench_live.py`: real face recognition, a 30 fps video
as the camera, side effects stubbed) measured on the laptop CPU with four faces in
view: preview **29.7 fps**, AI **5.6 Hz** (cap 6), tick p50 168 ms, frame age p50
198 ms (DINOv2 scene 92 ms, three YOLO heads 72 ms; face recognition runs on its own
thread about once a second). The first live test ran at 0.32 Hz with the video
frozen at that rate (CLAUDE.md §5, M2). Protocol,
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
and 642 MB of false-positive runtime output. `models/anomaly_classifier.pt` (a
ResNet-18 with `val_acc = 0.39` at epoch 1) is no longer loaded by anything; the
file is kept only for provenance.

---

## Known limitations

Stated plainly, because they will come up in review.

**There is no fitted fusion model yet.** The shipped `models/tci_xgboost.json` was
fitted to a synthetic formula (`R² = 0.984` measured only memorisation) in which
`identity` meant *familiarity*, the opposite of what the runtime feeds; it scored an
authorised resident ~5x higher than a stranger. It is retired: the TCI is now the
strongest confirmed class evidence, a published rule. A calibrated model returns only
once real session logs exist (CLAUDE.md M8).

**No model is validated for a new room.** The first live run (dark bedroom, faces at
< 1 m) was far outside the two rooms the models were trained on. The M1 rules stop the
false alarms; making detection *work* there needs footage from that room (CLAUDE.md
M6–M7).

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

All six were rewritten for v3. Each carries explicit **"Corrected from v2.1"**
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
