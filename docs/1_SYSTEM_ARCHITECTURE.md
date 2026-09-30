# SENTRIX — System Architecture & Technical Reference

**Version 3.0 · Updated: August 2026 · Status: software-complete, hardware deferred**

> This document describes the code as it exists. Where a number appears here it is
> either read from [`sentrix/config.py`](../sentrix/config.py) or reproducible with
> `python tools/doctor.py`. Version 2.1 of this document described several
> subsystems that were not running; those claims are corrected below and the
> corrections are called out explicitly, because they affect the mid-semester
> report.

---

## 1. Executive Summary

SENTRIX ingests video and audio, runs a stack of neural models over both, and
fuses their outputs into a continuous **Threat Confidence Index (TCI)** in
`[0.00, 1.00]`. The TCI drives a five-level escalation ladder from passive
logging through encrypted evidence capture to siren, SMS and a pre-populated
emergency dispatch package.

All inference is local. There is no cloud dependency and no subscription.

**What actually runs today** (verify with `python tools/doctor.py`):

| Capability | State |
|---|---|
| Person, weapon, fire/smoke detection + tracking | Working — one YOLOv8 component |
| TCI fusion, hysteresis, incident grouping | Working |
| AES-256-GCM evidence vault with signed manifest | Working |
| Scene classification (normal/fire/weapon/assault/robbery), 0.5 s window | Working — see doc 6 |
| Violence classification over a 2 s window | Fallback only — superseded by the scene classifier when installed |
| Acoustic classification over a 4 s window | Working (model under-trained — see doc 2) |
| Face authorisation | Working with `insightface`; **fails closed** without it |
| Cross-camera re-identification | Degraded — histogram fallback unless `torchreid` installed |
| Voice SOS | **Not wired.** Vosk model retained under `models/vosk-model/` |
| GPIO siren relay | **Interface only** — no hardware attached |
| UCF-Crime anomaly model | **Not wired.** See doc 2 §6 for why |

---

## 2. Repository Structure

```
Sentrix/
├── sentrix/                    importable package — all runtime code
│   ├── config.py               every tunable, in seconds not frames
│   ├── paths.py                filesystem layout
│   ├── app.py                  FastAPI factory + live capture loop
│   ├── io/
│   │   ├── sources.py          FrameSource: webcam | file | RTSP | multi
│   │   └── actuators.py        Actuator: console | sound | GPIO relay
│   ├── perception/
│   │   ├── detector.py         YOLOv8 — person + weapon + fire + ByteTrack
│   │   ├── scene.py            5-class scene classifier (normal/fire/weapon/assault/robbery)
│   │   ├── violence.py         sliding-window action classification (used only without a scene model)
│   │   ├── audio.py            checkpoint-adaptive acoustic CNN
│   │   ├── identity.py         fail-closed face authorisation
│   │   ├── behaviour.py        kinematics, resolution-independent
│   │   ├── reid.py             appearance re-identification
│   │   └── motion.py           frame differencing + is_night
│   ├── fusion/
│   │   ├── features.py         the versioned feature contract
│   │   ├── calibration.py      Platt / isotonic — fitted, not guessed
│   │   ├── tci.py              TCI computation
│   │   └── policy.py           level bands + hysteresis
│   ├── pipeline/
│   │   ├── engine.py           single process() path — live and replay
│   │   ├── incidents.py        incident grouping
│   │   ├── events.py           JSONL event log
│   │   ├── escalation.py       level -> action plan
│   │   └── state.py            live telemetry
│   ├── evidence/vault.py       AES-256-GCM + hash-chained manifest
│   ├── alerting/               Twilio SMS/voice, dispatch packages
│   ├── storage/                SQLite — one row per incident
│   ├── web/                    routes, streaming, security, templates, static
│   └── utils/logging.py        structured logging + telemetry
│
├── models/                     deployed weights (see doc 2 §5)
├── notebooks/                  TCI analysis — contract, calibration, thresholds
├── training/
│   ├── scripts/                training as .py
│   ├── notebooks/              training as .ipynb
│   └── runs/                   v1_legacy, v2_real, v3_real, ucf_crime_dvs
├── tools/                      doctor, replay, baseline, evaluate, auditors
├── tests/                      46 regression tests
├── docs/                       this folder
└── var/                        runtime output — gitignored, never web-served
```

> **Corrected from v2.1.** The previous structure described `backend/` and
> `frontend/` trees that no longer exist, and listed 15 module files that were
> renamed or removed. `frontend/` was a stale duplicate describing a Next.js
> application that was never built; it has been deleted.

---

## 3. Data Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│  FrameSource (io/sources.py)                                          │
│  webcam index │ video file │ RTSP URL │ tiled multi-camera            │
│  — the hardware seam: an ESP32-S3 node is a new subclass, not a       │
│    pipeline change                                                     │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  BGR frame, pristine copy
┌──────────────────────────────────────────────────────────────────────┐
│  Perception — every scorer maps sensor data to [0, 1]                 │
│                                                                        │
│   detector.py    YOLOv8: persons + weapons + fire, ByteTrack ids      │
│   scene.py       5-class scene probabilities, 0.5 s mean, mapped onto │
│                  the weapon/fire/violence/robbery scales              │
│   violence.py    16 frames over 2.0 s (only without a scene model)    │
│   audio.py       4.0 s log-mel window, 1.0 s hop, RMS-gated           │
│   identity.py    ArcFace 512-d against the enrolled gallery           │
│   behaviour.py   kinematics in frame-diagonals per second             │
│   motion.py      frame differencing + luminance -> is_night           │
│   reid.py        cosine matching over an appearance gallery           │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  FeatureVector (CONTRACT_V2, 8 features)
┌──────────────────────────────────────────────────────────────────────┐
│  Fusion — fusion/tci.py                                               │
│    1. hard overrides (fire, weapon, intrusion, authorised resident)   │
│    2. XGBoost over the contract the booster declares                  │
│    3. contextual boosters (unknown person, suspicious behaviour)      │
│    4. EMA, alpha = 1 - exp(-dt/tau), tau = 1.2 s                      │
│    5. optional fitted output calibrator -> probability                │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  TCI
┌──────────────────────────────────────────────────────────────────────┐
│  Policy — fusion/policy.py                                            │
│  band lookup + asymmetric hysteresis (0.6 s up, 5.0 s down)           │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  committed level
┌──────────────────────────────────────────────────────────────────────┐
│  Incidents — pipeline/incidents.py                                    │
│  contiguous episodes above level 1. Side effects bind HERE, not to    │
│  frames, and each fires at most once per incident.                    │
└───────┬──────────────────────────────────────────────┬───────────────┘
        ▼                                              ▼
┌────────────────────────┐              ┌──────────────────────────────┐
│ evidence/vault.py      │              │ alerting/  + io/actuators.py │
│ AES-256-GCM + HMAC     │              │ SMS, voice, dispatch, siren  │
│ hash-chained manifest  │              └──────────────────────────────┘
└────────────────────────┘
        │
        ▼  every scored frame, always
┌──────────────────────────────────────────────────────────────────────┐
│  pipeline/events.py — JSONL event log in var/events/                  │
│  audit trail · fusion training set · evaluation input                 │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 4. Threat Escalation Matrix

Bands from `PolicyConfig.thresholds`; actions from `AlertConfig` and
`EvidenceConfig` via `pipeline/escalation.py`.

| TCI | Level | Status | Actions |
|---|---|---|---|
| 0.00 – 0.20 | 1 | `SECURE` | Log only |
| 0.20 – 0.40 | 2 | `SUSPICIOUS` | Incident opened; snapshot captured |
| 0.40 – 0.65 | 3 | `ELEVATED` | + AES-256-GCM encrypted evidence bundle |
| 0.65 – 0.85 | 4 | `HIGH` | + SMS; dispatch package pre-populated |
| 0.85 – 1.00 | 5 | `CRITICAL` | + siren; automated voice call |

**Hard overrides** (bypass fusion; `FusionConfig`):

| Condition | Result |
|---|---|
| `fire ≥ 0.70` | TCI 0.95, incident type `fire` |
| `weapon ≥ 0.70` | TCI 0.92, incident type `weapon` |
| `weapon ≥ 0.55` | TCI 0.78 |
| `intrusion ≥ 0.80` | TCI 0.78, where `intrusion = 0.5·identity + 0.5·motion` |
| authorised resident, unarmed, identity backend healthy | TCI 0.10 |

Two properties of the last row matter. It requires `trusted_authorization` —
authorisation from a *working* identity backend — so a degraded face check cannot
suppress an alarm. And it does not apply when a weapon is visible.

> **Corrected from v2.1.** The previous matrix said level 2 was a UI badge only,
> level 3 saved evidence, level 4 sent SMS and level 5 fired the siren. The
> shipped code sent SMS at level 2 and fired the siren at level 3 — two steps more
> aggressive than documented. Combined with the fusion defect described in §5,
> that produced 1,489 evidence bundles and 2,415 snapshots from a single sustained
> false positive.

**Hysteresis.** A level change is committed only once the candidate level has
persisted: 0.6 s to escalate, 5.0 s to de-escalate. The asymmetry is deliberate —
fast response, slow clearing. This, not EMA smoothing, is what actually rejects
single-frame false positives: a large spike still drags an average across a
threshold, it merely takes longer.

---

## 5. TCI Formulation

The contract is defined once, in `fusion/features.py`, and versioned:

```python
CONTRACT_V2 = ("vision", "audio", "motion", "behaviour",
               "identity", "weapon", "fire", "is_night")   # canonical
CONTRACT_V1 = ("vision", "audio", "motion", "identity", "is_night")  # legacy
```

`TCIEngine` reads `booster.num_features()` and selects the matching contract, so a
retrained 8-feature model drops in with no code change. The currently deployed
`models/tci_xgboost.json` declares 5 features, so it runs on `CONTRACT_V1`; weapon,
fire and behaviour are folded in outside the model using the published
`fallback_weights`, and a warning is logged at startup.

Temporal smoothing:

```
alpha = 1 - exp(-dt / tau)          tau = 1.2 s
TCI_t = TCI_{t-1} + alpha * (TCI_raw - TCI_{t-1})
```

`dt` is real elapsed time, so smoothing is identical at 3 Hz and 30 Hz.

> **Corrected from v2.1.** Three claims in the previous §5 were wrong.
>
> 1. The weight vector given as `vision 0.30, audio 0.25, motion 0.20,
>    identity 0.15, behaviour 0.10` never existed in the code.
> 2. The formula showed model inputs passing through `calibrate(...)`. That
>    function applied an unfitted sigmoid whose own docstring called its constants
>    "dummy params for capstone demonstration". Because the booster was fitted on
>    raw scores in `[0, 1]` and served scores compressed to `[0.378, 0.731]`, the
>    model never saw its training distribution. An empty room scored **0.425** —
>    level 2, permanently — and the usable range of TCI had collapsed to about
>    `[0.40, 0.84]`. Input calibration is now off by default; an empty room scores
>    **0.012**.
> 3. `I_night` was listed as an input but nothing in the codebase ever computed
>    it, so one of five model features was a hard-wired zero. It is now derived
>    from smoothed frame luminance in `perception/motion.py`.

Measured behaviour after the fix, converged through the real engine and policy:

| Scene | TCI | Level |
|---|---|---|
| Empty room, day | 0.012 | 1 SECURE |
| Empty room, night | 0.054 | 1 SECURE |
| Unknown person, still | 0.292 | 2 SUSPICIOUS |
| Enrolled resident | 0.100 | 1 SECURE |
| Fight in progress | 0.869 | 5 CRITICAL |
| Knife visible | 0.920 | 5 CRITICAL |
| Fire detected | 0.950 | 5 CRITICAL |

---

## 6. Evidence Chain of Custody

1. **Encryption.** AES-256-GCM over the JPEG. Nonce is prepended to the file.
2. **Key derivation.** HKDF-SHA256 from `EVIDENCE_AES_KEY` produces two
   independent keys via `info` separation — one for encryption, one for signing —
   so possession of the AES key does not permit forging the manifest. Without the
   env var an ephemeral key is generated and the system warns that evidence will
   not survive a restart.
3. **Naming.** `ev_<date>_<ms>_L<level>_<counter>_<random>`. Millisecond
   resolution plus a monotonic counter plus a random suffix.
4. **Manifest.** Every bundle is appended to `var/evidence/manifest.jsonl`. Each
   entry carries the digest of the previous entry and an HMAC-SHA256 signature
   over the chain value. Modifying *or deleting* any bundle breaks the chain at
   that point and at every point after it.
5. **Verification.** `GET /api/evidence/verify`, or
   `EvidenceVault.verify_chain()`. Digests are compared against the **signed
   manifest**, never against the unsigned sidecar.

> **Corrected from v2.1.** The previous scheme named files at one-second
> resolution, so multiple writes inside the same second silently overwrote each
> other — a system claiming chain of custody was destroying evidence through its
> own naming. It also stored the SHA-256 in an unsigned JSON sidecar beside the
> ciphertext; an attacker able to rewrite the evidence could rewrite the hash next
> to it, so the check detected disk corruption but not tampering.

**On terminology.** The correct claim is **tamper-evident**, not
"court-admissible". Admissibility is a legal determination about process and
custody, not a property that a cipher confers. The mid-semester report should be
amended.

---

## 7. Concurrency

| Thread | Responsibility | Rate |
|---|---|---|
| `sentrix-pipeline` | `Pipeline.tick()` | capture 30 Hz, **inference 6 Hz** |
| `sentrix-cam-<n>` | one per source; keeps the capture buffer drained | source rate |
| `sentrix-audio` | rolling 4 s buffer, classify on each 1 s hop | 1 Hz |
| `sentrix-tasks` | snapshot, evidence, SMS, dispatch, DB writes | on demand |
| FastAPI event loop | HTTP, WebSocket at 2 Hz | async |

Capture and inference are decoupled: the preview stays smooth at 30 FPS while the
neural stack runs at 6 Hz. Human actions play out over hundreds of milliseconds,
so 30 Hz of YOLO buys nothing and starves an edge device.

Shared state is guarded by `threading.Lock`. The task queue is bounded at 64 and
drops with a warning rather than growing without limit.

> **Corrected from v2.1.** The previous table claimed the processing thread ran
> at ~30 FPS. It did not: `inference_skip = 6` meant the neural stack ran at about
> 5 Hz while only the capture loop ran at 30. The distinction now appears
> explicitly in configuration as `capture_fps` and `inference_fps`.

---

## 8. Security Model

| Surface | Control |
|---|---|
| Dashboard, all pages | HMAC-SHA256 signed session cookie, 12 h expiry |
| All `/api/*`, `/ws/threat`, `/video` | Same |
| Evidence, snapshots, enrolled faces | Authenticated `/media/*` routes only |
| `/static` | Package assets only — CSS, JS, images, siren sound |
| Login | Rate limited, 8 attempts per 5 minutes per client |
| Uploads | Extension, MIME **and** magic-byte validation; 10 MB cap |
| Path handling | Sanitised, then resolved strictly inside a fixed base directory |
| Default bind | `127.0.0.1` |

> **Corrected from v2.1.** The previous build mounted `/static` on a directory
> that contained the evidence vault, incident snapshots and enrolled face
> photographs. FastAPI applies no authentication to static mounts, so all of it
> was readable by anyone who could reach the port. Runtime output now lives in
> `var/`, which is never mounted.

---

## 9. Deliberate Non-Goals

Stated so they are not mistaken for oversights.

- **No automatic emergency dispatch.** Packages are prepared and require explicit
  operator confirmation. Auto-dispatching emergency services on an ML score is
  neither responsible nor lawful.
- **No cloud inference.** Removed entirely; `ROBOFLOW_API_KEY` is unread.
- **No frame-level rows in SQL.** Frame detail goes to the JSONL event log; the
  database holds one row per incident.
- **No identity fallback.** Without a face-embedding backend the system reports
  "not authorised" rather than guessing. A degraded identity check must fail
  towards raising alarms.
