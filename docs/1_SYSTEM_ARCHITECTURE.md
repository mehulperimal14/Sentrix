# 1. SENTRIX — System Architecture

**Describes branch `sentrix/portal` (October 2026): the device, the Local
Console and the cloud Portal.**

> This document describes the code as it exists. Numbers here are read from
> [`sentrix/config.py`](../sentrix/config.py), checked with
> `python tools/doctor.py`, or quoted from a committed report with its source.
> The Portal, SOS and device link have their own contract document,
> [doc 7](7_PORTAL_AND_SOS.md); this one covers the device and how the parts
> fit. Earlier versions of this document (v2.1, August 2026) described several
> subsystems that were not running; the corrections that matter for the
> mid-semester report are kept in the "History" notes.

---

## 1. Summary

SENTRIX watches a camera and a microphone, runs a stack of neural models over
both, and turns their outputs into a **Threat Confidence Index (TCI)** in
`[0, 1]`. The TCI drives a five-level ladder: logging, evidence capture,
SOS request, SMS and dispatch package, siren.

Detection runs on the device (a laptop today, a Raspberry Pi 5 later). There is
no cloud inference. Since this version the device can also link to the
**SENTRIX Portal**, a website that shows the same console remotely, onboards
the customer and runs the SOS flow with a responder desk and emergency
contacts. The Portal is optional: without it the device works as before.

**What runs today** (check with `python tools/doctor.py`):

| Capability | State |
|---|---|
| Person detection + ByteTrack; weapon and fire heads (YOLO11n, custom data) | Working. Detector votes count next to the scene classifier's (`SENTRIX_THREAT_SOURCES=both`, the default); only the two together reach L4/L5 |
| Scene classifier (normal / fire / weapon / assault / robbery), DINOv2 ViT-B/14 | Working; the main threat signal. See doc 6 |
| Per-class evidence: k-of-n confirmation, person gate, corroboration caps | Working (`fusion/evidence.py`) |
| Level policy, incidents, side effects bound to incidents | Working |
| AES-256-GCM evidence vault with signed, hash-chained manifest | Working |
| Face recognition, per tracked person | Working with `insightface`; **fails closed** without it |
| Acoustic classifier, 4 s window | Working; under-trained (doc 2). Never above L2 (decision D1) |
| Violence classifier | Fallback only, used when no scene model is installed |
| Cross-camera re-identification | Histogram fallback unless `torchreid` is installed |
| 4-second clip engine + fast lane (`SENTRIX_PIPELINE_MODE=clip`) | Built for the Pi; off by default (doc 8 T1, doc 6 §6.10) |
| SOS assistance requests | Built; off unless `SENTRIX_SOS_ENABLED=1` (doc 7 §7) |
| Channels: modem SMS, Telegram, e-mail, legacy Twilio | Built, tested with fake transports only; every send needs two switches (doc 7 §8) |
| Device link to the Portal, tunnel, remote video | Built; off unless `SENTRIX_LINK_ENABLED=1` (doc 7 §5) |
| Pi hardware: gpiozero siren relay, LEDs, PIR, door, mains, telemetry, Pi camera | Built, tested against fakes only; no Pi has run it (doc 8) |
| Voice SOS | Not wired. The Vosk model is not shipped |
| UCF-Crime anomaly model | Not wired (doc 2 §6) |

---

## 2. Repository structure

```
Sentrix/
├── sentrix/                    the device application (python -m sentrix)
│   ├── config.py               every tunable, in seconds or ticks, never frames
│   ├── app.py                  FastAPI app, lifespan, live capture loop
│   ├── services.py             optional services: node, gpio, channels, sos, link
│   ├── mode.py                 HOME / ARMED, persisted in var/mode.json
│   ├── io/                     camera sources (webcam, file, RTSP, picamera:, demo-reel:),
│   │                           siren, GPIO, node telemetry, sensors, Wi-Fi, watchdog
│   ├── perception/             detector, scene, violence, audio, identity, behaviour,
│   │                           motion, reid
│   ├── fusion/                 features (contract), evidence, tci, policy, calibration
│   ├── pipeline/               engine (tick loop), clip + fastlane (clip mode),
│   │                           incidents, escalation, events, state
│   ├── runtime/                torch / ONNX Runtime / NCNN backends for exported models
│   ├── evidence/vault.py       AES-256-GCM + hash-chained, signed manifest
│   ├── sos/                    SOS state machine, policy, texts, PINs, sweeper, service
│   ├── alerting/               send guard, channels (router, modem, Telegram, e-mail,
│   │                           Twilio), dispatch packages, legacy notifier
│   ├── link/                   link protocol, client, tunnel, tunnel auth, command rules
│   ├── storage/                SQLite: incidents, dispatch, requests, outboxes, audit
│   ├── evaluation/             side-effect-free harness, custom dataset, metrics
│   └── web/                    Local Console: routes, auth, hardening, templates, JS
├── sentrix_portal/             the cloud Portal (python -m sentrix_portal)
├── models/                     weights (doc 2 §5)
├── tools/                      doctor, replay, evaluation, bench, demo, provisioning
├── training/                   training scripts, notebooks, datasets, run reports
├── notebooks/                  TCI analysis notebooks
├── deploy/                     portal/ (Docker, Caddy, Postgres), pi5/ (systemd)
├── tests/                      1647 tests
├── docs/                       this folder (index: docs/README.md)
└── var/                        runtime output; gitignored, never web-served
```

---

## 3. Data flow on the device

```
┌──────────────────────────────────────────────────────────────────────┐
│ Capture (sentrix-pipeline thread, camera rate)                        │
│  FrameSource: webcam │ file │ RTSP │ picamera: │ demo-reel: │ tiled    │
│  every frame → preview with the latest overlay → MJPEG /video         │
│  newest frame → single-slot handoff to the inference thread           │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  (frames it cannot keep up with are dropped)
┌──────────────────────────────────────────────────────────────────────┐
│ Perception (inference thread, ≤ 6 Hz in tick mode)                    │
│   detector.py   YOLO person + ByteTrack; weapon and fire heads        │
│   scene.py      5-class probabilities, mean over window_ticks (1)     │
│   identity.py   faces on their own thread (≤ 1 check/s), per track    │
│   audio.py      4 s log-mel window, 1 s hop (own thread)              │
│   behaviour.py, motion.py, reid.py                                    │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  FeatureVector (CONTRACT_V2, 8 features)
┌──────────────────────────────────────────────────────────────────────┐
│ Evidence (fusion/evidence.py), per class: weapon fire assault robbery │
│   scene and detector votes; 2 of 3 fresh evaluations; person gate     │
│   one source ≤ L3; classifier + detector → L4/L5; experimental ≤ L2   │
│ TCI (fusion/tci.py) = strongest class evidence; armed-mode intrusion  │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  TCI (no smoothing)
┌──────────────────────────────────────────────────────────────────────┐
│ Level policy (fusion/policy.py): per-threshold timers, 5 s step-down  │
└───────────────────────────────┬──────────────────────────────────────┘
                                ▼  committed level
┌──────────────────────────────────────────────────────────────────────┐
│ Incidents (pipeline/incidents.py) → action plan (escalation.py)       │
│ side effects on the sentrix-tasks FIFO, each at most once per incident│
└───┬──────────────┬───────────────────┬──────────────────┬────────────┘
    ▼              ▼                   ▼                  ▼
 snapshot,      SOS trigger         legacy SMS / call,   siren
 evidence       (L3, if the SOS     dispatch package     (L5)
 vault (L3)     service wants it)   (skipped once SOS
                → doc 7 §7          owns the incident)
    │
    ▼  every decision, always
 pipeline/events.py — JSONL event log in var/events/ (schema 3 ticks, schema 4 clips)
 storage/database.py — one SQLite row per incident; listeners feed the link outbox
```

**Clip mode** (`SENTRIX_PIPELINE_MODE=clip`, built for the Pi): the inference
thread keeps a 4 s ring buffer and every 2 s analyses the last 4 s as one clip
(keyframes for vision, the whole window for audio and motion). A fast lane runs
YOLO at 320 px on every 4th frame and can raise L3 between clips. The same
evidence, TCI, policy and incident code decides. Details: doc 8 T1; results:
doc 6 §6.10.

**One process.** The Local Console, the SOS service, the channel router and
the link client run in the same process as the pipeline. The pipeline only
enqueues an SOS trigger; the services react through change listeners
(doc 7 §3.6, §6.3).

---

## 4. Threat levels and actions

Bands from `PolicyConfig.thresholds`; actions from `AlertConfig`,
`EvidenceConfig` and `SosConfig` via `pipeline/escalation.py`.

| TCI | Level | Status | Actions |
|---|---|---|---|
| 0.00 – 0.20 | 1 | `SECURE` | Log only |
| 0.20 – 0.40 | 2 | `SUSPICIOUS` | Incident opened; log and dashboard only (decision D4) |
| 0.40 – 0.65 | 3 | `ELEVATED` | + snapshot, AES-256-GCM evidence bundle; + SOS request for weapon, fire and armed intrusion when the SOS service runs (D8). No siren |
| 0.65 – 0.85 | 4 | `HIGH` | + SMS and dispatch package (legacy path); with SOS, the request turns urgent and the safety defaults alert contacts |
| 0.85 – 1.00 | 5 | `CRITICAL` | + siren; voice call (legacy path) |

Once the SOS service owns an incident, the legacy L4 SMS, L4 package and L5
call are skipped; their job moves into the request's `rise4` / `rise5` steps.
The siren stays (doc 7 §7.2). Every send goes through the send guard, which
refuses everything by default (doc 7 §8.1).

**Evidence rules** (`ThreatConfig`; they replaced the single-frame hard
overrides after the first live run, `CLAUDE.md` §5 and M1):

| Rule | Result |
|---|---|
| a source crosses its threshold on 2 of the last 3 fresh evaluations (gap ≤ 3 s) | the source is *confirmed* for that class |
| weapon vote with no person seen in the last 2 s | does not count |
| one confirmed source | class evidence 0.45–0.62 → L3 |
| a classifier **and** a detector confirmed | 0.70 + 0.25 × the weaker side's margin over its threshold → L4; L5 from a margin of about 0.6 |
| assault, robbery (experimental, D1) | capped at 0.35 → L2 |
| a cached value (no new evaluation this tick) | carried with its window state, never counted again |
| a non-finite value, or a threshold outside (0, 1) | recorded as gated, contributes nothing |
| armed mode, person unrecognised for 3 s | intrusion at 0.45 → L3 (a missed detection within 2 s does not restart it) |

Scene thresholds come from `var/calibration/scene.json` or
`SENTRIX_SCENE_THRESHOLDS` when set, else from the checkpoint raised to the
floors weapon 0.50, fire 0.45, assault and robbery 0.60.

**Identity** has no input into threat evidence. It feeds the armed-mode
intrusion rule, the SOS entry-delay disarm and the display. It is **per
tracked person**: each face is attached to the ByteTrack box that contains it,
and each track is `authorised` (matched, held for 60 s unless its face then
fails twice), `unknown` or `unverified`. The frame counts as authorised only
when everyone in view is. A degraded backend never authorises. In home mode
presence alone never raises the level.

**Hysteresis** (per threshold): L2 and L3 commit as soon as the TCI reaches
them, because it only gets there on confirmed evidence. L4 and L5 need the
threshold held for 0.6 s **and** 2 observations (1 clip in clip mode). A level
steps down once the TCI has not touched its threshold for 5 s. There is no
display smoothing: the gauge, the event log, the SMS and the incident record
carry the value the level was decided on.

> **History.** v2.1 documented L4 SMS and an L5 siren while the shipped code
> sent SMS at L2 and fired the siren at L3. With the fusion defect below, that
> produced 1,489 evidence bundles and 2,415 snapshots from one sustained false
> positive. The first live run (`CLAUDE.md` §5) then showed single-frame
> overrides firing on one tick of the scene classifier; M1 replaced them with
> the rules above.

---

## 5. TCI

The feature contract is defined once, in `fusion/features.py`, and versioned:

```python
CONTRACT_V2 = ("vision", "audio", "motion", "behaviour",
               "identity", "weapon", "fire", "is_night")   # canonical
CONTRACT_V1 = ("vision", "audio", "motion", "identity", "is_night")  # legacy
```

The feature vector is logged every tick. `weapon` and `fire` carry the
per-class evidence scores (TCI scale), and the full votes sit in
`extras["evidence"]` and in the event log (`votes`).

**TCI = the strongest confirmed class evidence**, a published rule anyone can
re-derive from an event row. No fitted model is served:
`models/tci_xgboost.json` (synthetic labels, 5-feature `CONTRACT_V1`) was
retired because it was fitted with the opposite meaning of `identity` and
ranked threats worse than its own inputs (AUROC 0.793 vs 0.848 for the scene
probability on labelled replay, `CLAUDE.md` §5). A fitted, calibrated model on
real session logs is milestone M8. `tools/doctor.py` asserts that an empty
scene scores below 0.10.

Measured effect of the evidence rules (`CLAUDE.md` M1, `tools/replay_decisions.py`
on the first live session and `tools/replay_custom_videos.py --tag m1`):

| | Before M1 | After M1 |
|---|---|---|
| Live session, home mode: L2+ / L3+ / L4+ alarm onsets | 30 / 9 / 6 | 0 / 0 / 0 |
| Labelled replay, 10 videos: episodes caught | 13 / 18 | 13 / 18 |
| Labelled replay: false alarms per normal minute | 0.79 | 0.00 |
| Held-out handgun: detection latency | 2.4 s | 6.3 s |

> **History (v2.1).** The weight vector `vision 0.30, audio 0.25, motion
> 0.20, identity 0.15, behaviour 0.10` never existed in the code. Inputs passed
> through an unfitted sigmoid that compressed scores to `[0.378, 0.731]`, so an
> empty room scored 0.425 (level 2, permanently). `I_night` was never computed;
> it is now derived from frame luminance in `perception/motion.py`.

---

## 6. Evidence chain of custody

1. **Encryption.** AES-256-GCM over the JPEG; the nonce is prepended to the
   file.
2. **Key derivation.** HKDF-SHA256 from `EVIDENCE_AES_KEY` gives two
   independent keys (`info` separation), one for encryption and one for
   signing, so the AES key alone cannot forge the manifest. Without the
   variable an ephemeral key is generated and the system warns that evidence
   will not survive a restart.
3. **Naming.** `ev_<date>_<ms>_L<level>_<counter>_<random>`.
4. **Manifest.** Every bundle is appended to `var/evidence/manifest.jsonl`.
   Each entry carries the digest of the previous entry and an HMAC-SHA256
   signature over the chain value. Modifying or deleting any bundle breaks the
   chain there and at every later point.
5. **What the sidecar records.** The decision behind the frame: evidence
   votes, scene probabilities and thresholds, identity verdict and face counts,
   the person gate, the pipeline session; detections as `{label, conf, bbox,
   track_id}`.
6. **Clip bundles.** In clip mode `EvidenceVault.save_clip` seals the seconds
   before and after the trigger into the same chain.
7. **Verification.** `GET /api/evidence/verify` or
   `EvidenceVault.verify_chain()`, against the signed manifest.

Evidence never leaves the device. The Portal sees only metadata
(`evidence.meta`, no image) and can view a bundle on demand through the
tunnel, in memory, without storing it (doc 7 §12). The retention caps of doc 7
§3.8 and signed tombstones for pruned bundles are not built yet.

The correct claim is **tamper-evident**, not "court-admissible": admissibility
is a legal question about process and custody, not a property of a cipher.

---

## 7. Concurrency

| Thread | Work | Rate |
|---|---|---|
| `sentrix-pipeline` | capture loop: read, composite the latest overlay, JPEG once per frame | camera rate (30 fps measured) |
| `sentrix-inference-<strategy>` | `tick` loop (perception → decision) or the clip engine | ≤ 6 Hz tick cap; one clip per 2 s |
| `sentrix-fastlane` | clip mode only: YOLO at 320 px on every 4th frame | |
| `sentrix-cam-<source>` | one per source; keeps the capture buffer drained | source rate |
| `sentrix-identity` | face recognition worker | ≤ 1 check/s, sooner when more people appear |
| `sentrix-audio` | rolling 4 s buffer, classify on each 1 s hop | 1 Hz |
| `sentrix-tasks` | snapshot, evidence, SOS trigger, SMS, dispatch, DB writes (FIFO, bounded) | on demand |
| `sentrix-channels`, SOS sweeper, `sentrix-node`, `sentrix-gpio` | the optional services (doc 7 §3.6) | on their own timers |
| FastAPI event loop | HTTP, `/ws/threat` at 2 Hz, the link client | async |

Capture and inference are decoupled: frames that inference cannot keep up
with are dropped, never queued, so the preview runs at the camera rate whatever
the AI rate is. Measured with `tools/bench_live.py` (live configuration, vid07
with four faces as a 30 fps camera, laptop CPU; report
`training/runs/custom_eval/m5/bench_live.json`, commit `8f21f47`): preview
29.7 fps, AI 5.6 Hz, tick p50 168 ms / p95 226 ms, frame age p50 198 ms.

---

## 8. Security model

**Local Console** (`sentrix/web/`):

| Surface | Control |
|---|---|
| Session | `lc_session` cookie, HMAC-signed, HttpOnly, SameSite=Lax, 12 h; `Secure` with `SENTRIX_SECURE_COOKIES=1` |
| Pages | sign-in required (`routes.require_auth`, 307 to `/login`) |
| APIs, media, `/video`, `/ws/threat` | sign-in required (`auth.require_lc`, 401 JSON); websocket Origin check |
| State-changing requests | `X-CSRF-Token` header, bound to the session; cross-site requests refused |
| Scripts | external JS only; CSP with a per-response nonce; no `innerHTML` with data |
| Evidence, snapshots, faces | authenticated `/media/*` routes only; `/static` holds package assets only |
| Login | rate limited, 8 attempts per 5 minutes per client |
| Uploads | extension, MIME **and** magic-byte checks; 10 MB cap; paths confined to a fixed base |
| Camera URLs | credentials redacted in names, health, logs and link payloads |
| Tunnelled requests from the Portal | a per-process secret marks verified identities; roles, consents, step-up and rate limits checked on the device (`link/policy.py`); every request audited |
| If the hardening module fails to load | every state-changing request answers 503; pages and video keep working |
| Default bind | `127.0.0.1` |

**Portal** (`sentrix_portal/`): Argon2id passwords and PINs, per-login session
ids stored as HMACs, CSRF on every POST, TOTP for admin and Desk accounts,
back-off instead of lockout, tenant isolation on every query, an HMAC-chained
audit log, column encryption for phones, medical notes and TOTP secrets
(doc 7 §10–§13).

**Sending**: test mode is on by default; nothing is sent unless
`SENTRIX_LIVE_ALERTS=1` and the channel's own switch are set; in test mode only
to `SENTRIX_TEST_RECIPIENTS`; emergency numbers are always refused (doc 7
§8.1).

> **History (v2.1).** `/static` was mounted on a directory that held the
> evidence vault, snapshots and enrolled face photos, with no authentication.
> Runtime output now lives in `var/`, which is never mounted.

---

## 9. Deliberate non-goals

- **SENTRIX never contacts 112.** An approved SOS goes to the SENTRIX Desk (a
  simulated responder console) and the confirmed emergency contacts. For
  serious events the safety defaults alert contacts without waiting for the
  owner (doc 7 §7.5); emergency services are never contacted automatically.
- **No cloud inference.** Detection, levels, siren and evidence stay on the
  device and keep working without the Portal.
- **No frame-level rows in SQL.** Frame detail goes to the JSONL event log; the
  database holds one row per incident.
- **No identity fallback.** Without a face-embedding backend the system reports
  "not authorised". A degraded identity check fails towards raising alarms.
- **No continuous video, faces or evidence in the cloud** (decision D12).
