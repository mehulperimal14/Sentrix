# CLAUDE.md — SENTRIX

Project memory and working plan. Read this first in every session. Sections 1–4
are facts; section 5 is the diagnosis of the first live run; section 6 is the
plan, the decisions and their progress. Keep section 6's status boxes current.

---

## 1. What this project is

**SENTRIX** — an edge-first home threat-detection product with an owner-approved
SOS flow (Capstone CPG-299, CSED, Thapar Institute; mentor Dr. Ashutosh Mishra;
team: Kartik Garg, Akshay Ranveer, Prashant Gagneja, Harshit Mishra, Mehul
Perimal).

Two programs:

* **Device** (`python -m sentrix`, one process; laptop today, Raspberry Pi 5
  later): camera (+ optional microphone) → perception → confirmed per-class
  evidence → **Threat Confidence Index (TCI, 0–1)** → five-level alarm →
  incidents, event log, encrypted evidence. It serves the **Local Console (LC)**
  on `http://127.0.0.1:8000` and runs the SOS engine, the channel router
  (modem SMS, Telegram, e-mail, Twilio — all behind the send guard) and the
  outbound link client. Pi I/O: siren relay, LEDs, buzzer, PIR, door, mains
  sense, node telemetry, watchdog.
* **Portal** (`python -m sentrix_portal`, `http://127.0.0.1:8100`; demo on
  `portal.localhost:8100`): accounts with activation codes, TOTP for admin and
  Desk, 7-step onboarding wizard, household and contacts with consent, SOS pages,
  the **SENTRIX Desk** (simulated responder console), admin console, privacy
  (export, erasure, retention), the link hub and the tunnel
  `/d/{device_id}/...` that replays requests into the device's own LC app.

| Level | Side effects (current code) |
|---|---|
| 1 SECURE | log |
| 2 SUSPICIOUS | incident opened (log and dashboard only; D4); the most an unrecognised person ever reaches (D15) |
| 3 ELEVATED | + snapshot, AES-256-GCM evidence bundle, SOS request for weapon, fire and a corroborated fight (D8 as amended by D15) |
| 4 HIGH | + dispatch package; legacy SMS only when no SOS request owns the incident |
| 5 CRITICAL | + siren; legacy voice call only when no SOS request owns the incident |

The TCI bands 0.20 / 0.40 / 0.65 / 0.85 are asserted, not fitted. One
confirmed visual modality lands in L3 (≤ 0.62); L4+ needs two independent
modalities confirmed for the same class - detector + scene classifier, or
video + time-aligned audio - and all three give L5 (D3, D15).

### Architecture (one tick, `sentrix/pipeline/engine.py` → `Pipeline.process`)

```
FrameSource (io/sources.py: webcam | file | RTSP | picamera:0 | demo-reel:<json>)
  capture thread at camera rate → newest-frame slot → inference thread (≤ 6 Hz)
  └─ Detector (perception/detector.py)   YOLO person (+ByteTrack) / weapon / fire heads
  └─ SceneEngine (perception/scene.py)   5-class: normal fire weapon assault robbery (DINOv2)
  └─ IdentityWorker + TrackIdentity      InsightFace on its own thread, per track, fail-closed
  └─ BehaviourEngine, MotionEngine, ReIDEngine, AudioEngine (optional, own thread;
     PANNs CNN10 AudioSet tagger → 5 cues, each window stamped; used only when aligned)
  → fusion/evidence.py: k-of-n confirmation, person gate, per-modality corroboration
    (video detector / video classifier / aligned audio), fight + banter rule, experimental cap
  → TCIEngine (fusion/tci.py): TCI = strongest fused class score (no booster, no EMA);
    unknown person / PIR / door = L2 context only; per-input contributions recorded
  → LevelPolicy (fusion/policy.py): per-threshold timers (L4/L5 0.6 s + 2 obs; 5 s down)
  → IncidentManager (pipeline/incidents.py, PeakSnapshot) → _act: snapshot, evidence, dispatch, siren
  → SOS hook: sos.trigger (idempotent per incident) → sentrix/sos (machine, sweeper, channels)
  → EventLog (var/events/*.jsonl, schema 3, flushed per row), SystemState (websocket), HUD
Clip mode (SENTRIX_PIPELINE_MODE=clip, pipeline/clip.py): 4 s window, 2 s hop, optional fast lane.
```

All tunables: `sentrix/config.py` (seconds, never frames; `SosConfig`,
`LinkConfig`, `ChannelConfig`, `GpioConfig`, `ClipConfig`, `NodeConfig`,
`WebConfig`, `RetentionConfig`). Runtime output: `var/` (gitignored, never
web-mounted; `SENTRIX_VAR_DIR` moves the whole tree). Contract for the Portal,
link, tunnel and SOS: `docs/7_PORTAL_AND_SOS.md`; Pi plan:
`docs/8_RASPBERRY_PI5_INTEGRATION.md`; remaining work in order and the
custom-data training guide: `ROADMAP.md`; doc index: `docs/README.md`.

### Environments

| Purpose | Python | Notes |
|---|---|---|
| App, Portal, tests, evaluation | `.venv` (Python 3.10–3.14; maintainer PC: 3.14, **CPU** torch) | `run.bat` / `run.sh`, or `python -m sentrix`; `python -m sentrix_portal`; `python -m pytest` |
| Training (GPU) | `C:\Users\PC\anaconda3\envs\sentrix\python.exe` (3.11, torch cu126, RTX 4060 8 GB) | has ultralytics, timm, transformers |
| Raspberry Pi 5 | `requirements-pi.txt`, venv with system site packages (`run.sh` detects the Pi) | not yet run on a Pi |

### Data locations

| What | Where |
|---|---|
| Custom frame dataset (765 reviewed frames, 5 classes) | `training/datasets/sentrix_custom/frames/` (tracked; `SENTRIX_CUSTOM_DATASET` overrides) |
| Its 10 source videos | `training/datasets/sentrix_custom/videos/` (tracked; `SENTRIX_CUSTOM_VIDEOS` overrides) |
| Vendored ground truth, boxes, QC rejects | `training/datasets/sentrix_custom/` |
| Original public corpora (RWF-2000 frames, weapon, fire/smoke, audio, UCF-DVS) | not in git; `data/` or `SENTRIX_VIOLENCE_DATA` / `SENTRIX_WEAPON_DATA` / `SENTRIX_FIRE_DATA` (maintainer PC: `G:\Capstone\data`) |
| Device runtime (database, events, evidence, snapshots, faces, calibration, `mode.json`) | `var/` (private; never modify `var/sentrix.db` from tools or tests) |
| Portal data (SQLite, `secret_key`, `data_key`) | `var/portal/` by default (`PORTAL_DATA_DIR`) |
| Device identity (`identity.json`, `profile.key`) | `/etc/sentrix` on a Pi, else `var/link` (`SENTRIX_DEVICE_IDENTITY_DIR`) |
| Demo world | `var/demo/` by default (`tools/demo_reset.py --dir`; only a marked directory is wiped) |

### Rules for working here

* **Never let a test, evaluation, replay or demo send an alert.** `.env` may hold
  live Twilio keys. Test mode is on by default (D13); every message passes
  `sentrix/alerting/guard.py`. Evaluation goes through
  `sentrix/evaluation/harness.py` (`build_eval_pipeline`: `_act`/`_submit` and the
  shutdown save are no-ops, identity neutral, no mic, `NullSosService`;
  measurement tools call `assert_measurement_safe`). `tests/conftest.py` forces
  test mode and every switch off before any import, moves `var/` and the Portal
  data to a temp dir, blocks webcam and microphone and allows loopback sockets
  only.
* **Never tune or select on the test video** (`wa02`). Select with
  leave-one-video-out CV over the nine train+val videos, confirm on val, report
  test once. The demo reel refuses `wa02`.
* **Never open a webcam or microphone** in diagnostics or tests
  (`tools/doctor.py` opens none unless `--probe-camera`).
* Quote numbers from generated reports (`tools/custom_eval_table.py`,
  `training/runs/**`, docs/7 §17), with the commit that produced them.
* Branches carry configurations; commit freely, **never push** unless asked; no
  attribution lines (Co-Authored-By, "Generated with") in commits. When several
  writers share the working tree, stage only your own files (never `git add -A`).
* The full suite stays green: `.venv\Scripts\python.exe -m pytest` (add
  `-o addopts=""` for the summary line).

---

## 2. What has been done

### Before this work (main, 2026-08-21 → 08-25)

V3 restructure and model training in notebooks: violence ResNet-18 (RWF-2000),
audio log-mel CNN (AudioSet clips), weapon and fire/smoke YOLOv8n (Roboflow
corpora), UCF-Crime DVS anomaly model (not wired). TCI fusion booster fitted to a
synthetic formula. A staged (uncommitted) rewrite moved `backend/` into the
`sentrix/` package with 15 documented fixes (README "What changed in v3", in the README up to `e7ca89e`).

### Custom-dataset work (2026-09-30)

1. **Baseline + evaluation harness** (`sentrix/baseline-v3`): the staged rewrite
   committed as-is; `sentrix/evaluation/` (vendored ground truth, a reviewed
   per-video label timeline, metric definitions, a side-effect-free harness);
   `tools/eval_custom_frames.py` (per image), `tools/replay_custom_videos.py`
   (10 source videos through `Pipeline.process` at 6 Hz with true timestamps),
   `tools/custom_eval_table.py`. Found: `lap` missing from requirements, so
   ByteTrack silently fell back to untracked detection.
2. **Scene classifier integration** (shared by the two model branches): 5-class
   `SceneEngine`, threshold-aligned onto the fusion scales (a probability at the
   class threshold lands on the modality's override); robbery behaviour label and
   incident type; delta checkpoints (store only trained tensors + SHA-256 of the
   frozen public base).
3. **Training tooling** (`training/custom/`): GroundingDINO box annotation gated
   by reviewed labels + held-object check + visual QC (167 boxes, 16 rejected);
   scene-classifier trainer with LOVO CV, temperature and thresholds from OOF;
   YOLO trainer with rehearsal and retention measurement; frozen-backbone screen.
4. **`sentrix/finetuned-v3`**: the notebook models fine-tuned (ResNet-18 scene
   model from the violence weights with RWF rehearsal; YOLOv8n weapon head +
   `stick`; YOLOv8n fire/smoke) — `models/v4_finetuned/`.
5. **`sentrix/custom-v4`**: new models trained on the custom data — DINOv2
   ViT-B/14 scene classifier (last 4 blocks trained, 57 MB delta), YOLO11n
   weapon/fire heads (boxes only by default) — `models/v4_custom/`.
6. Dashboard **Scene Classifier** card; `/api/health` reports scene model;
   `tools/doctor.py` reports resolved weights and loads the scene model.
7. `docs/6_CUSTOM_DATASET_EVALUATION.md`: protocol, selection evidence, results
   of all three configurations.

### Then: first live run on the laptop webcam (2026-09-30, user)

The product did not work as a whole — see section 5.

### End-to-end repair (2026-10-01 → 10-10, `sentrix/e2e-repair` = `main`)

M1–M5 (section 6): containment of the false alarms, real-time live loop,
coherent records, per-track identity and enrolment from the monitoring camera,
evaluation per committed level and a gate report. Published to GitHub with a
fresh-clone setup.

### Pi plan and Portal/SOS product (2026-10-10 → 10-15, `sentrix/portal`)

1. `docs/8_RASPBERRY_PI5_INTEGRATION.md` (was `HARDWARE.md`): 4 s clip engine,
   hub topology, Hailo option, event trigger, pin table.
2. The team's proposal (company provisions → customer activates → onboarding →
   same dashboard anywhere → SOS on a confirmed L3) turned into the reviewed
   plan (*SENTRIX Prototype Roadmap v2*) and decisions D8–D14, then the
   developer contract `docs/7_PORTAL_AND_SOS.md`.
3. Foundation and contract review, then eight parallel tracks (T-HW, T-CLIP,
   T-CH, T-SOS, T-WEB, T-ACCT, T-LINK, T-MODEL) in worktrees, merged in that
   order, integration notes applied (M10).
4. Demo tools (`tools/demo_reset.py`, `tools/demo_run.py`, demo reel camera) and
   `deploy/portal/`.
5. An in-process product end-to-end suite (13 scenarios), a live smoke run with
   the Portal and the device as two real processes (docs/7 §17, 5 bugs fixed),
   an adversarial review, and a repository clean-up (unused weights, the Vosk
   model and old run dumps removed; capstone submissions to `docs/capstone/`,
   diagrams to `docs/assets/`).

### Multimodal fusion and the talk-down speaker (2026-10-15, `sentrix/portal`, not pushed)

The owner's requirements: the TCI combines every input (video detectors, scene
and violence models, motion, people count, identity, audio, PIR/door) and the
levels match the scene; audio in sync with video; fight vs friendly banter
decided by video + audio; unknown people never above L2; the owner can speak
to the room. Decisions D15 and D16, D2/D8 amended; milestone M11 (section 6).

---

## 3. Git history

| Branch | Head | Contents |
|---|---|---|
| `main` | `594d91b` (pushed 2026-10-15) | the published state: the integrated product (device + Portal + SOS + Pi I/O + clip engine), docs and ROADMAP.md (previous head `eb82ff5`) |
| `sentrix/baseline-v3` | `d8629eb` | staged rewrite committed (`9e0037a`) + evaluation harness (`05a04fe`) + doc fixes |
| `sentrix/finetuned-v3` | `66accb9` | baseline + scene integration + training tooling + fine-tuned v3 models |
| `sentrix/custom-v4` | `3772acf` | baseline + scene integration + training tooling + new DINOv2/YOLO11n models |
| `sentrix/e2e-repair` | `eb82ff5` | custom-v4 + M1–M5 + fresh-clone setup + docs/8 (Pi plan); the state before M10 |
| `sentrix/portal` | `594d91b` (= `main`) + M11 (`30074ab` and the results/docs commits after it; `git log -1 sentrix/portal`); M11 **not pushed** | main + Portal/SOS foundation + the eight tracks + integration, demo tools, e2e tests, live smoke fixes, review fixes, clean-up (M10), multimodal fusion and the talk-down speaker (M11; commits below) |
| `wip/hw`, `wip/clip`, `wip/ch`, `wip/sos`, `wip/web`, `wip/acct`, `wip/link`, `wip/model` | `4418de3`, `cc53443`, `f19e056`, `0b703b6`, `c340f29`, `e9d251c`, `24b897e`, `3bfb441` | the track branches, all merged into `sentrix/portal`; local only, not pushed |

Published to `github.com/mehulperimal14/Sentrix` on 2026-10-10. Before publishing,
history was rewritten once: private runtime data that earlier commits had
carried (enrolled face photos, webcam alert snapshots, old evidence bundles and
their keys, under `backend/runtime/` and `SentrixV2-main/`) was removed from every
commit, so every hash changed. Commits on `sentrix/e2e-repair` since `custom-v4`:

```
8f21f47 chore: make a fresh clone install, run and test end to end
85262ca data: ship the custom dataset and its videos; repo-relative defaults; .gitignore for local tooling
ebcd957 docs(claude): M5 commit hash in the branch history
19d607d feat(eval): committed-level metrics, negative sessions, multi-rate replay, gate report
0b9e659 feat(identity): M4 - enrol from the monitoring camera, fixed uploads, per-track identity
00be662 feat(records): M3 - incident rows from one tick, crash-safe logs, committed alarm, usable events page
c78c79e perf(identity): run face recognition on its own thread in the live loop
f13e506 feat(live): decouple capture, inference and preview; report video and AI rates separately
056a1c4 perf(identity): stop onnxruntime spin-waiting and load only the face models used
ec2e936 fix(fusion): M1 containment - confirmed per-class evidence replaces single-frame overrides
4f17576 docs: CLAUDE.md - project memory, live diagnosis and end-to-end repair plan
```

Commits on `sentrix/portal` after `main` (`594d91b`), not pushed:

```
30074ab feat(fusion): multimodal time-aligned fusion, AudioSet sound model, talk-down speaker (D15, D16)
```
(then the M11 evaluation results and docs commit, `git log sentrix/portal`)

Commits on `sentrix/portal` up to `main` (first parent, newest first; each
merge brings in its track's commits from the `wip/*` branch):

```
594d91b docs: consistency pass and branch table for the published state
dbc83cd docs: refresh the docs folder for the integrated product
8f53087 docs: hardware and deployment guides for the integrated product
1ff8026 docs: README, CLAUDE.md and env template for the integrated product
72953fe docs: ROADMAP.md - remaining work and model-training guide
e7ca89e chore(repo): remove unused files and restructure
58f62c9 fix(review): adversarial review findings
640e713 fix(integration): live smoke findings
7aca186 test(e2e): product flow end to end
232f938 feat(demo): demo tools and portal deployment
4c7eca2 chore(integration): apply track integration notes
2fa40cf merge: T-MODEL (slim scene classifiers v5, ONNX export, slim report)
7710f18 merge: T-LINK (device link client, tunnel, commands, Portal hub, proxy, SOS mirror, Desk, contacts)
bd37675 merge: T-ACCT (Portal accounts, activation, TOTP, admin console, onboarding wizard, household, retention)
acf0b99 merge: T-WEB (LC hardening, CSP/CSRF, base-aware URLs, new LC pages)
7fc5cc9 merge: T-SOS (device SOS engine, policy, templates, PINs, sweeper, LC SOS API, dispatch)
43d396f merge: T-CH (channel router, outbox, Telegram, e-mail, modem SMS, Twilio wrapper)
1be98c2 merge: T-CLIP (4 s clip engine, fast lane, runtime backends, model export)
6626a9b merge: T-HW (Pi hardware I/O, siren, GPIO, node telemetry, deploy/pi5)
31c6c50 fix(foundation): contract review
5a7a9a7 docs(claude): M10 foundation commit hash
685df9b feat(foundation): contracts, config, guard, schema, services, portal skeleton
```

Commits on `main` since `sentrix/e2e-repair`'s M5 work (`8f21f47`):

```
eb82ff5 docs: architecture diagram as PNG (replaces the JPG)
3e8ecf4 docs: HARDWARE.md - Raspberry Pi 5 integration plan (4 s clip engine, hub, Hailo, event trigger)
739e98b docs: point cited commit hashes at the rewritten history
```

Commits on `sentrix/custom-v4` since the pre-session `main`:

```
3772acf docs(readme): test count
f3f30b1 feat(web): show the scene classifier on the dashboard and in /api/health
fee918a docs: baseline latency measured on an idle machine
688a6af docs(readme): latency of the three configurations
04744ca docs: measured pipeline latency of all three branches, fire threshold note
a6d5e0c fix(doctor): report the weights each engine resolves, and load the scene model
4b37863 feat(models): v4 custom models trained on the custom dataset
3d995ae feat(scene): delta checkpoints for partially frozen backbones
336fdd5 feat(training): custom-dataset box annotation, training and model selection
68b1241 feat(perception): five-class scene classifier wired into the TCI
05a04fe feat(eval): custom-dataset evaluation harness and v3 baseline results
9e0037a refactor: restructure backend/ into the sentrix package (v3 current state)
```

Pre-session `main` (newest first): `0f75a0e` master engineering plan docs ·
`fdfb9a9` custom-data/IoT docs · `30ca1b9` ignore runtime output · `5084f9e`
v3_real paths + clean-frame overlays · `ce1af62` relax YOLO box limits ·
`32155a0` YOLO thresholds 0.25, HSV flame, theft/harmful scores · `deb5116` docs ·
`4a7465a` workspace restructure · `6a9dbc1` restructure, ML engines, reports ·
`367c826`…`d20a3f3` restructure series · `8c915e8`, `e3eaf6b` Phase 2.5.

---

## 4. Model scores

### 4.1 On their own training corpora (v3; README/docs 2 — contaminated splits)

| Model | Metric | Note |
|---|---|---|
| Violence ResNet-18 (RWF-2000) | macro-F1 0.832 | 52.7 % of val rows share source videos with train |
| Weapon YOLOv8n | mAP50 0.908 | 12.2 % near-duplicate val images |
| Fire/smoke YOLOv8n | mAP50 0.719 | 20.0 % near-duplicate val images |
| Audio CNN (4 classes) | macro-F1 0.704 (single-epoch spike; ~0.58 honest) | 375 clips |
| TCI XGBoost | R² 0.984 | **synthetic labels — not a model result** |

### 4.2 On the custom dataset (held out; `docs/6`)

| | baseline-v3 | finetuned-v3 | custom-v4 |
|---|---|---|---|
| Val per-image accuracy / macro-F1 | 0.417 / 0.281 | 0.583 / 0.401 | **0.679 / 0.768** |
| Val replay: episodes / false alarms | 1 of 2 / 0 | 1 of 2 / 4 | **2 of 2 / 0** |
| Test per-image accuracy / macro-F1 | 0.043 / 0.114 | 0.677 / **0.460** | **0.720** / 0.419 |
| Test replay: stick episodes | 3 of 3 (alarms everything) | **3 of 3** | 0 of 3 |
| False alarms / normal minute, 10 videos | 13.96 | 7.12 | **0.79** |
| Replay latency per tick, CPU (identity + audio **off**) | 121 / 205 ms | 117 / 132 ms | 213 / 245 ms |

Component evidence: scene classifier LOVO macro-F1 0.210 (ResNet-18 fine-tuned)
vs 0.440 (DINOv2, last 4 blocks); weapon frame AUROC val 0.71 (fine-tuned) vs
0.65 (YOLO11n new); fire AUROC 0.91 vs 0.62. Assault and robbery **cannot be
validated** (2 videos / 1 event). Only two rooms are represented.

### 4.3 After the repair (committed-level replay, clip mode, slim heads)

| What | Result | Source |
|---|---|---|
| M5 tick-mode replay, 10 custom videos (partly in-sample) | 0 false onsets at every level over 3.7 normal min; L3 weapon 9/12, fire 3/3, robbery 0/1, assault 0/2; L5 weapon 2/12, fire 2/3 | `training/runs/custom_eval/m5/` (`19d607d`) |
| Clip mode, fast lane, same videos | weapon L3 9/12, fire 3/3, 0 false onsets (Gate A's detection criterion) | `training/runs/custom_eval/clip/replay_all_fastlane.json` (code `575e463`, results `cc53443`) |
| Clip-mode live bench, laptop, 4 threads, 7.mov | compute p95: torch 3381 ms, ONNX 3797 ms, torch + fast lane 6473 ms, **ONNX + fast lane 2145 ms**; budget 1.6 s, none within it (other training jobs ran concurrently: upper bounds) | `training/runs/custom_eval/clip/bench_clip_*.json` |
| Slim scene heads (LOVO macro-F1) | MobileNetV3-L `mnv3l_full` 0.229, EfficientNet-B0 `effb0_kd` 0.211, DINOv2 `dino_b_last4` 0.440; bar 0.396 fixed in advance; **neither passes**, DINOv2 stays; MobileNetV3-L ONNX 2.4 ms at 4 threads | `training/runs/custom/slim/REPORT.md` (protocol `5f8b589`, models `3bfb441`) |
| Live smoke, Portal + device as two processes, demo reel | `scene gun` → PENDING weapon request 8.0–9.8 s; `scene fire` → 11.6–11.9 s; approve → device DISPATCHED 0.01–0.28 s; MJPEG 29.9 fps direct, 13.6 fps tunnelled (cap 15) | docs/7 §17 (`640e713`) |
| M11 tick replay, 10 custom videos **with their own soundtracks** (time-aligned, PANNs CNN10) | L3 weapon 9/12, fire 3/3 (= M5); **L4 weapon 6 → 7/12** (vid05: scene weapon + aligned weapon_sound); L5 weapon 2/12, fire 2/3 (= M5); 0 false onsets at L3+; L2 false onsets 0 → 1 = the new unknown-person L2 (the harness scores everyone as unrecognised; with `SENTRIX_UNKNOWN_ALERT_HOME=0` 0 at every level); assault 0/2 (vid05's staged fight sounds like calm talk: aggression < 0.01, benign 0.91) | `training/runs/custom_eval/m11/replay_all.json`, `replay_all_noaudio.json`, `m11_homelog/` (code `30074ab`) |
| M11 clip mode + fast lane, same videos with audio | L3 weapon 9/12, fire 3/3 (= clip baseline `575e463`); L4 weapon 6 → 7/12; 0 false onsets at L3+; L2 0 → 3 (unknown-person L2) | `training/runs/custom_eval/m11/replay_all_fastlane.json` (`30074ab`) |
| M11 live session `c27258a25457` re-decided (no audio in that log) | home: L2+/L3+/L4+ onsets 30/9/6 as logged → **5/0/0** (5 = unknown-person L2, D15); armed: → **9/0/0** (M1 had 9 L3 intrusions) | `training/runs/custom_eval/m11/live_session_{home,armed}.json` |
| Sound model | PANNs CNN10 (AudioSet, 527 classes, 25 MB): **48.9 ms p50 / 52.0 ms p95** per 4 s window, laptop CPU, 2 threads | measured with `AudioEngine.classify_window` (`30074ab`) |

---

## 5. Live diagnosis (2026-09-30 → 10-01)

First live run: laptop webcam, dark bedroom, several team members, 18.3 min
(session `c27258a25457`, `var/events/events_20260930_232502_*.jsonl`). Seven
investigators (latency, preview, fusion, accuracy, identity, records, strategy)
reconstructed it; every high-severity bug claim was then attacked by an
independent skeptic — **10 of 10 survived** (with corrections, folded in below).
The event log replays exactly (TCI to 0.0003, levels 1300/1300), so every
number here is reproducible.

### What the user saw → why

| Symptom | Root cause (file:line on `sentrix/custom-v4`) |
|---|---|
| Video laggy, HUD "0.6 FPS" | The MJPEG stream only shows `Pipeline._annotated`, written once per **inference** (`engine.py:317`); `tick()` drops frames between inferences (`engine.py:332-333`) and `process()` runs inside the capture loop (`app.py:43`). Preview fps == inference Hz (0.47–2.7 Hz live). "FPS" on the HUD is 1/mean(inference latency). `CaptureSource` also sleeps a full period after a blocking read (`sources.py:140`); `asyncio.sleep` on the Windows Proactor loop overshoots (33 ms → 67 ms). |
| Inference slow (median 479 ms, p95 2.25 s) | **onnxruntime spin-waiting**: InsightFace's 5 ORT sessions busy-spin after every call (~20 of 24 cores), making the next YOLO call 10–30× slower (track 59 ms alone → 771 ms p50 after identity). Live config measured on the idle laptop: **3086 ms/tick (0.32 Hz)**; with `allow_spinning=0` alone **243 ms (4.1 Hz)**. Then: identity runs all 5 `buffalo_l` models at det 640 on every tick (100 ms + 70–80 ms per extra face); DINOv2-B 70–90 ms; two weapon/fire YOLO heads 40 ms. With nospin + det+rec at det 320 on a 1 Hz worker + scene every 2nd tick: **5.5 Hz with 6 faces**. The RTX 4060 is idle (`.venv` has CPU torch; on GPU DINOv2 fp16 6 ms, YOLO 7 ms). The previously published 213 ms came from the eval harness with identity and audio stubbed — it overstated the live rate ~10×. Three test processes also loaded the CPU in minutes 0–10. |
| False "fire" and "weapon" alarms | **All** came from the DINOv2 scene classifier, one tick each, **0.001–0.017 above a 0.35 threshold** (YOLO drew 0 fire boxes and 3 weapon boxes in 1340 frames). The room is far outside the training distribution (NN cosine to train 0.25–0.39 vs 0.91–0.97 for val/test; luminance 50 vs 144; faces fill 82 % vs 30 % of frame). Live normal p(weapon) median 0.295, 7.7 % of ticks ≥ 0.35 → ~171 weapon override episodes/hour; 65 % of them with **nobody in frame**. Thresholds were tuned for macro-F1 at 41 % threat prevalence; the 0.5 s scene window holds one frame at 2 Hz; one frame fires a hard override (`tci.py:216-226`). |
| Fire alarm row typed "intrusion", L4 | The L4 came from the **intrusion override** (unknown face 0.6 + motion saturated at 1.0 = 0.80) 15.8 s after `Kartik.jpg` was **deleted from the gallery during the run** (Access page; no audit trail). The fire frames came later at L1/L2. The incident row mixes fields from different frames: type frozen at first non-normal (`incidents.py:154-156`), reason/TCI from the peak frame, L1 frames ignored (`incidents.py:121`), `authorized` hard-coded False (`database.py:91`). |
| "TCI 0.11 L4 HIGH AUTHORISED"; L2 at TCI 0.758 | Committed hysteretic level shown next to per-frame TCI/reason/identity. `LevelPolicy` restarts its dwell whenever the band changes (`policy.py:78-80`): a rising ramp across bands never commits, flapping across 0.20 holds a level for 20.9 s. 45 % of rows show a level ≠ TCI band. Time-based EMA/dwell collapse at 0.5 Hz: one spike moves TCI 0.10 → 0.80. |
| TCI "very inaccurate" | The XGBoost booster is **synthetic and fitted with the opposite meaning of identity** (familiarity vs the runtime's unfamiliarity): it scores an authorised resident 0.363–0.438 and a stranger 0.081–0.087. All "unknown person" signal is a hand-set +0.15. On labelled replay the TCI ranks threats **worse than its own input** (AUROC 0.793 vs scene p(weapon) 0.848; ECE 0.148); 42.6 % of normal ticks sit ≥ 0.40. |
| Evidence "weapon" bundles show only person boxes | Sidecar stores YOLO detections (as repr strings) but not the scene verdict/probabilities/override source (`vault.py:198-220`). Evidence is sealed from the **current** frame when the lagging level allows it, not the trigger frame: 5/10 bundles are "normal", one is an authorised resident sealed at L4. `pre_roll`/`post_roll` are never read. |
| Snapshots of team members | Any unenrolled person gets +0.15 → L2 → snapshot (`tci.py:238`, `escalation.py:59`), taken at incident open, not at the trigger. |
| Webcam enrollment `NotReadableError` | Browser `getUserMedia` asks for the webcam the server holds via DirectShow (`authorized.html:60`, `sources.py:101-104`). Upload enrollment **always** reports "not enrolled" (`routes.py:324` compares `x.jpg` with stem `x`); `identity.reload()` runs on the event loop (freezes video/websocket 2–20 s); an extra `</div>` pushes the 2nd+ faces out of the grid (`authorized.html:41`). |
| Other | Event log flushes every 50 rows and shutdown never persists the open incident (40 rows + 1 incident lost). Dispatch packages created without evidence, never cancelled. Events page snapshot link broken, no verdict buttons, UTC vs IST mixed. Navbar health dots read keys `/api/health` never returns. Identity is **frame-global**: one matching face authorises everyone in view for 5 s (tailgating bypass); a one-tick face miss makes the resident an intruder. |

### What the offline numbers could not show

The val/test videos come from the **same two rooms** as training; "0 false
alarms on val" rests on 11 s of normal footage (95 % upper bound ≈ 946/h). The
evaluation harness neutralises identity and scores incident *type*, not the
committed *level* users experience. Total labelled footage is 341 s (62.6 s
handgun, 11.4 s flame, 2 s robbery). The fine-tuned v3 heads (branch
`finetuned-v3`) also false-fire on the clean live snapshots (pistol 0.85, smoke
0.87); the custom YOLO11n heads are silent but nearly blind. **No current model is
deployable in the live room; the fix is containment now and deployment-room data
next — threshold tuning alone cannot close a domain gap this large.**

Encouraging evidence: three runtime rules (threshold 0.45–0.50, 2-of-3 tick
confirmation, a person seen in the last 2 s) take the live session to **0**
weapon/fire episodes while still catching the held-out `gun.mov` and `4.mov`
episodes. A person-crop weapon probe reaches held-out AUROC **0.888** (shipped
full-frame classifier 0.749). Zero-shot GroundingDINO separates handgun frames
at AUROC 0.85 — a good *label-gated* teacher. A preview prototype held **30 fps**
with the real models inferring in the same process.

---

## 6. Plan, decisions and milestones

Principles: **measure before and after every change** (the replayed live log is a
regression test); **a single modality never reaches L4 alone**; **presence is
context, not threat**; **promise only what the data can validate**; the test
video/session is never used for tuning.

### Decisions taken (recommended defaults — change here if the team disagrees)

| # | Decision | Why |
|---|---|---|
| D1 | Alarm classes for the demo: **handgun (replica), knife, visible flame**. Assault, robbery, stick, smoke and audio are *experimental*: logged and shown, never above L2. *Knife is not covered yet: the custom weapon head knows `pistol` and `stick` only and the custom dataset has no knife footage (ROADMAP.md §2.4).* | They exist in 1–3 clips; unvalidatable classes only add false alarms. |
| D2 | *(amended by D15)* An unrecognised person reaches **L2 only, in both modes**: after 3 s in **armed mode** (`SENTRIX_ARMED=1`), after 10 s at **home** (`SENTRIX_UNKNOWN_HOME_DWELL`; `SENTRIX_UNKNOWN_ALERT_HOME=0` keeps the old "logged only"). Armed-mode PIR/door activity with nobody recognised is the same L2. L2 is log and dashboard only (D4): no snapshot, no SOS. Was: armed mode opened an intrusion at L3. | Levels 3-5 are critical and need a real threat; team members and guests are "unknown" by default. |
| D3 | A scene-classifier verdict alone is capped at **L3** (snapshot + evidence, no SMS/siren). L4+ needs corroboration (detector box of that class, or a second modality). *Amended by D8: a confirmed L3 of an alarm class also opens an SOS prompt.* | The classifier is the least reliable input and out of domain. |
| D4 | Snapshots move from L2 to **L3**; L2 is UI/log only. *Amended by D8: L3 also prompts the owner (no siren at L3).* | Privacy of residents; L2 becomes "unconfirmed". |
| D5 | Darkness and faces < 1 m are **out of scope** for an RGB webcam: the HUD shows `DEGRADED` and threat typing is held. | Physics, not model tuning. |
| D6 | Fusion becomes **transparent per-class evidence** (published rules) until a fitted, calibrated model exists from ≥ 4 recorded sessions. The synthetic booster is retired. | It is polarity-inverted and ranks worse than its inputs. |
| D7 | The 7 incidents of session `c27258a25457` are false alarms; the team labels them through the new verdict buttons (M3). They become the first real fusion labels. | Operator feedback loop. |
| D8 | (amends D3/D4; *amended by D15*) A **confirmed L3** of an alarm class - weapon, fire, or a fight corroborated by video and aligned aggression audio (`alarm_classes` adds `assault`; `machine.classify` also requires its evidence state `corroborated`) - opens an SOS assistance request; the owner is prompted on Portal, LC, Telegram, e-mail; SMS only offline or unseen after 10 min; no siren at L3. An unrecognised person opens **no** request (the armed-intrusion path with its 30 s entry delay stays behind `SENTRIX_SOS_INTRUSION=1`, off). | The team's SOS flow without prompt storms. |
| D9 | Window 30/45/60 min (default 30); no answer = EXPIRED ("unanswered", never "safe"); "Request help now" opens a linked request. Safety defaults ON (corroborated L4/L5, fire at T+2 min, armed intrusion, device offline during an open L2+ incident → confirmed contacts); single-source weapon L3 → owner only until gate P8. Duress PIN; policy frozen per request. | Silence must not fail the user who cannot answer. |
| D10 | Parity by **tunnel**: Portal `/d/{id}/...` replays into the device's own LC app in-process; `/video` and `/ws/threat` relayed; remote video adaptive ≤ 15 fps. | One code path, no drift. |
| D11 | Oracle Always Free (India) + `.in` domain + Caddy; Render + Supabase fallback; e-mail by HTTPS API. | ₹0 hosting, data in India. |
| D12 | Cloud keeps profiles, metadata and one consented SOS snapshot per request (30 d); faces, evidence and video stay on the device (viewed on demand only). | Minimisation. |
| D13 | **Test mode is the default and the only mode this semester**; sending needs `SENTRIX_LIVE_ALERTS=1` + the channel switch, and only to `SENTRIX_TEST_RECIPIENTS` in test mode; the legacy Twilio path is a no-op unless enabled; link, modem, SOS sweeper opt-in. | `.env` may hold live keys. |
| D14 | Accounts by username + single-use activation code; admin/Desk TOTP; the LC keeps `SENTRIX_PASSWORD`; Portal password hashes never reach the device. | No admin takeover, no hashes on the SD card. |
| D15 | **Multimodal, time-aligned fusion** (transparent rules, D6 stays). Modalities: *video detector* (YOLO heads, fast lane), *video classifier* (scene, violence: one modality), *audio* (PANNs CNN10 AudioSet tagger → cues aggression / weapon_sound / fire_sound / benign_social / siren; the 4-class CNN is the fallback). Every audio result carries its window (monotonic start/end, wall end); it votes for a frame/clip only if the window overlaps it within `SENTRIX_AUDIO_ALIGN_TOLERANCE` (1.5 s) - else audio is *unavailable*, never "calm"; one window votes once (fresh). Per class: one visual modality confirmed → L3; two modalities → L4 (weaker sets it), three → ≥ L5; audio alone ≤ L2; aggression corroborates a visible weapon but is never a weapon alone. Fight: experimental L2 unless video fight **and** aligned aggression audio confirm with ≥ 2 people and motion ≥ 0.02 → L3 (`SENTRIX_FIGHT_MAX_LEVEL`, 4 also needs a second visual source); aligned laughter/calm talk with no aggression = banter → held < L2 (confirms on one window: lowering needs less than raising). People count, motion, identity, PIR/door are gates/context, each recorded in `contributions` (state, event log schema 5, dashboard). Unknown person: D2. **Unvalidated** until room recordings with sound exist (M6, ROADMAP §15.1). | The owner's requirement: one decision from every input, audio in sync with video, fight vs banter, unknown people never critical. |
| D16 | **Talk-down speaker**: the owner (tunnel, step-up ≤ 300 s; members and the Desk refused by the path table) or the device admin sends a preset or ≤ 200 characters; offline TTS (pyttsx3 SAPI5/espeak-ng, or the `espeak-ng` command → `aplay -D`); off by default (`SENTRIX_SPEAKER_ENABLED`), 3 messages/min, one at a time, audited as `speaker_say`. Pi hardware: USB audio adapter + PAM8403 + 3 W speaker, or a USB speakerphone (docs/8 §6.6). | Let the owner deter or reassure without a siren; no cloud TTS, no audio leaves the house. |

### Milestones

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done (with commit).

**M1 — Containment: stop the false alarms (code only, days 1–2)**
- [x] Scene evidence needs **k-of-n confirmation** counted in ticks (2-of-3, max gap 3 s) and a **person seen in the last 2 s** (weapon); scene window is a tick count. `fusion/evidence.py`.
- [x] **Per-deployment thresholds** (`var/calibration/scene.json` / `SENTRIX_SCENE_THRESHOLDS`), else checkpoint raised to floors weapon 0.50, fire 0.45, assault/robbery 0.60. *(margin over p(normal) deferred: not recoverable from old logs to validate.)*
- [x] Single-frame **hard overrides removed**; every vote (source, value, threshold, hits, gating) recorded in `extras["evidence"]` and in the event log (`votes`, schema 3). One confirmed source ≤ 0.62 (L3); a classifier **and** a detector confirmed → own scale 0.70–0.95 set by the weaker side (L4; L5 once both are ~60 % of the way to certainty) (D3). Smoke and the weapon head's `stick` class not in the alarm path (D1).
- [x] `tci_xgboost.json` **retired**: TCI = strongest class evidence (D6); reasons are the evidence strings.
- [x] Presence out of the score; armed-mode intrusion rule (D2). Snapshots from L3 (D4).
- [x] **LevelPolicy rewrite** with per-threshold timers: L2/L3 commit on already-confirmed evidence, L4/L5 need 0.6 s **and** 2 observations; step-down after 5 s untouched. **No display EMA**: the TCI on the gauge, log, SMS and incident is the value the level was decided on.
- [x] **Adversarial review** (3 lenses × skeptic verification, 18 agents): 21 findings, 17 confirmed and fixed. Identity no longer relaxes threat evidence (frame-global identity let a stranger inherit a resident's authorisation; replay showed it bought nothing — per-track rule is M4). Only fresh model evaluations count toward k-of-n (a cached or failed inference used to confirm itself). A partial calibration keeps the other floors; thresholds outside (0, 1) and NaN votes are rejected visibly. Unavailable person detector turns the weapon gate off and says so. Armed intrusion survives a missed detection. Opening-tick incident reasons kept. `doctor` crash, `evaluate.py` alarm count on raw TCI, still-image scoring fixed.
- [x] **Acceptance** — `tools/replay_decisions.py` on the live session: L2+/L3+/L4+ alarm onsets **30/9/6 → 0/0/0** (home mode); armed mode 9 L3 intrusions, 0 L4. Labelled replay (`tools/replay_custom_videos.py --tag m1`, all 10 videos, after the review fixes): episodes **13/18 → 13/18**, false alarms/normal min **0.79 → 0.00**, threat FPR 0.028 → 0.010, val 2/2 episodes kept, corroborated train episodes now reach L5; cost: held-out handgun latency 2.4 s → 6.3 s (scene p(weapon) stays under the raised 0.50 floor for the first seconds). 149 tests pass.

**M2 — Real time: smooth video, honest rates (days 2–4)**
- [x] **ORT spinning off** for every InsightFace session (`session.intra_op.allow_spinning=0`, intra-op 4 threads, `SENTRIX_FACE_ORT_THREADS`), only `detection`+`recognition` loaded, `SENTRIX_FACE_DET_SIZE`. Measured on vid07 with 4 faces: person detector after identity 315 → 67 ms, identity + detector 912 → 387 ms (the diagnosis measured the full live tick 3086 → 243 ms).
- [x] Decouple **capture / inference / preview** (`Pipeline.run_live`): inference on its own thread with a single-slot newest-frame handoff (frames it cannot keep up with are dropped, never queued); every captured frame is composited with the latest overlay, JPEG-encoded once per frame for all viewers, streamed by sequence number; pipeline time = capture time; `replay()`/harness stay synchronous. `tests/test_live.py` drives it with a fake camera and a slow `process`.
- [x] `CaptureSource`: no sleep after a blocking device read; frame seq + timestamp (`FrameSource.latest`); requests 640×480@30; a file used as the camera is paced at the capture rate.
- [x] CPU budget: ORT threads logged at startup. *Scene every 2nd tick and throttled weapon/fire heads were not needed*: with identity off the tick the AI runs at 5.6 Hz against the 6 Hz cap, and a scene vote on every tick confirms evidence faster. Revisit on slower hardware (the fresh-evaluation rule in `evidence.py` already makes a strided model safe).
- [ ] Use the GPU when present: CUDA torch in the app env (needs a cp314 wheel or a 3.11/3.12 app env — **owner decision**, it replaces the app's torch install), fp16 DINOv2 (6 ms). *Done: the scene device is reported in `/api/health` (`scene_device`); CUDA is picked automatically when torch has it.* Not required for M2 acceptance on CPU.
- [x] `tools/bench_live.py`: live configuration (real identity and gallery, a video file as a 30 fps camera thread, no audio, side effects stubbed, event log to a temp dir) → preview fps, AI Hz, tick and frame-age p50/p95, per-stage p50/p95. After the decoupling, vid07 (4 faces): **preview 29.7 fps, AI 3.56 Hz**, tick p50 292 / p95 368 ms, frame age p50 313 ms; identity 125 ms, scene 94 ms, YOLO person/weapon/fire 25/23/22 ms.
- [x] Identity off the hot path: `allowed_modules=['detection','recognition']` and a recognition thread (`IdentityWorker`): re-check at most every 1 s, at once when more people come into view, every 5 s while everyone is recognised; fail closed before the first verdict and when the verdict is > 6 s old. Replays stay synchronous. *Per-track cache and head crops move to M4 with per-track identity.* Live bench: AI **3.56 → 5.60 Hz**, tick p50 292 → 168 ms, frame age p50 313 → 198 ms; recognition ~1.1 checks/s on its own thread.
- [x] HUD/state report **VIDEO fps · AI Hz · latency · frame age** separately (`video_fps`, `ai_hz`, `frame_age_ms` in state and `/api/health`); boxes older than 3 s are not drawn ("AI STALLED"); navbar dots read real keys (camera, mic, AI engine — the "cloud" dot and card were always-on placeholders).
- [x] **Acceptance** (`tools/bench_live.py`, vid07 with four faces, laptop CPU): preview **29.7 fps** (≥ 25); AI **5.6 Hz** (≥ 3); identity costs the pipeline thread one non-blocking read. Web smoke test: 105 distinct MJPEG frames in 5 s, dashboard shows video fps / AI Hz / frame age. **Still to confirm on the real webcam in the user's room.**

**M3 — Coherent state and trustworthy records (days 4–6)**
- [x] One **committed alarm object** in state (`alarm`: level, since, incident id, peak type + headline reason, peak TCI, de-escalation countdown) separate from the live block; badge and headline read it, the gauge stays live. The always-HIGH "confidence" badge now shows the evidence state (none / unconfirmed / confirmed / corroborated).
- [x] Incident **PeakSnapshot** (frozen): type/reason/TCI/features/authorised/person/trigger votes all from the peak tick; type follows the peak; L1 ticks inside an open incident counted; `authorized` persisted; `trigger_json` added by a guarded migration (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`, nothing dropped).
- [x] Evidence sidecar records the decision behind the frame (evidence votes, scene probabilities and thresholds, identity verdict and face counts, person gate, pipeline session) and detections as `{label, conf, bbox, track_id}` instead of repr strings. With L3 committing on the confirmed tick, the first capture is the trigger frame. *Deferred: a pre-roll clip (needs a multi-frame bundle format in the vault).*
- [x] Event log schema 3 (votes, person gate, face counts — done in M1); **flushed every row**; open incident written at open, on escalation and on peak moves (≤ every 5 s), and on shutdown; rows left open by a crash are closed at the next start.
- [x] Events page: local time, peak-tick reason, duration/ongoing, identity, snapshot and evidence links through the authenticated `/media` routes, **Real / False alarm** verdict buttons. Dispatch packages are queued after the evidence capture and carry its bundle and hash; a FALSE verdict cancels the incident's pending dispatch.
- [x] **Acceptance** — unit tests pin single-tick rows, open-row saving, migration, verdict → dispatch cancel (`tests/test_records.py`, temp SQLite). Live run on an exported tree (vid07 as camera, no Twilio, console siren): an L3→L5 weapon incident opened, `taskkill /F` lost **0** event rows and kept the incident row (all fields from one tick, trigger votes attached); the next start closed it as interrupted. 178 tests pass.

**M4 — Identity and enrollment (days 5–7)**
- [x] `POST /api/authorized/capture` from the pipeline's raw frame (auth, 20/min rate limit, quality gates with actionable reasons: exactly one face, ≥ 80 px, sharpness, brightness, not already enrolled under another name); `GET /api/authorized/preview.jpg` (raw frame, no HUD); `authorized.html` uses them instead of the browser webcam.
- [x] Upload fixed (compared `alice.jpg` with the stem `alice`, so every upload said "not enrolled"; now gated like capture, several files with a result each, rejected images are not stored), assessment off the event loop, reload lock, incremental enrol/remove (no full re-embed), extra `</div>` removed, per-tile enrolled/not-enrolled status, several photos per person (`alice__2.jpg`), gallery changes audited in the event log (`gallery_enrolled/removed/rejected`; the unused `enrolled_faces` table is left alone).
- [x] **Per-track identity** (`TrackIdentity`): each face attached to the person box containing it; authorised/unknown/unverified per ByteTrack id; match held on the track for 60 s unless its face fails twice in a row; a new track never inherits it; the frame is authorised only when everyone in view is (armed intrusion fails closed on unverified people); HUD labels boxes with the person's name or state. *Identity still does not relax threat evidence (M1); revisit with team data.*
- [ ] **Acceptance** (needs the team): enrol everyone while monitoring; 5-minute team sit-in → 0 incidents in home mode, identity flicker < 0.1 flips/person-minute. Unit tests cover tailgating, turning away, track swaps, one bad look, stale checks (196 tests); live bench unchanged at 5.5 Hz.

**M5 — Evaluation that matches the product (days 6–8)**
- [x] Replay metrics per **committed level** (`metrics.level_report`, in every `replay_custom_videos.py` report): onsets, false onsets/hour with an exact Poisson 95 % interval, normal time at level, episodes caught at each level *with the right type*, Clopper-Pearson recall intervals, median/p95 latency, caught within 2 s. Raw per-source values (scene/detector/violence) now in the replay CSV.
- [x] **Negative-session replay** (`--negatives DIR`: every video normal, FA/h with Poisson bound) and multi-rate replay (`--hz`). *Deferred: real identity in the harness (needs enrolled team footage).*
- [x] Gate report tool `tools/gate_report.py` (G0 manifest, G1 AUROC, G2 27/30 with a "cannot reach" early FAIL, G3 hours + onsets, G4 bench + onset→L3 p95, G5 scripted dry run) → PASS / FAIL / INSUFFICIENT / NOT RUN.
- [x] **Results** (`training/runs/custom_eval/m5/`, all 10 custom videos — partly in-sample, *not* the S4 gate set): committed-level false onsets **0** at every level over 3.7 normal minutes (95 % bound still ≤ 60/h: far too little normal footage to claim a rate); caught at L3 weapon 9/12, fire 3/3, robbery 0/1 (L2 1/1), assault 0/2; at L5 weapon 2/12, fire 2/3. At **2 Hz** the same episodes are caught (13/18) with 0 false onsets. Gates: G1 PASS (weapon scene AUROC 0.93, fire 0.999), **G2 FAIL** (weapon 8/12 within 2 s), **G4 FAIL** (5.6 Hz OK, but onset → L3 p95 4.0 s), G0/G3/G5 need the S4 recordings. These are the M6–M7 targets.
- Found by the M5 run and fixed: the eval harness and `bench_live` stubbed `_submit` but the pipeline's *shutdown* save still reached `var/sentrix.db`. It failed on the not-yet-migrated column, so no row was written (verified read-only: newest incident is the user's live run, schema unchanged); the connection close did checkpoint the user's own WAL into the main file. The shutdown save is now a hook both stub.

**M6 — Deployment-room data (team, weeks 1–3)** — `[ ]` not started (no
recordings, no `tools/build_custom_dataset.py`, no "record normal session"
mode yet). Protocol in the strategy
findings: demo room R1 + one other room R2; camera mounted 1.8–2.2 m, tilted
down, lit room; sessions S1/S2 train, S3 val, S4 test (≥ day 5, unseen
volunteers); scripted 20 s clips (12 per class per room per session) with
**hard negatives as many as positives** (phone/remote held like a pistol,
unlit lighter, aerosol can, fire video on a screen, orange clothing…);
≥ 3 h of "boring" footage in R1 on non-training days; replicas and flames with a
written safety notice. `tools/build_custom_dataset.py` (sessions → frames.csv
schema) and a "record normal session" mode in the app.

**M7 — Models that fit the room (weeks 2–3)**
- [ ] One **YOLO11n detector** (person, pistol, knife, fire) at 640 from COCO + QC'd teacher boxes + rehearsal (pistol/knife/fire only; smoke dropped) + all hard negatives; held-object association with ByteTrack; k-of-n confirmation; fire flicker check. Replaces 3 YOLO passes.
- [ ] Person-crop weapon classifier (DINOv2-S probe → last blocks) as the second vote; scene classifier retrained with deployment negatives, OOD guard (NN cosine floor) and ≤ 1 Hz.
- [ ] Optional OpenVINO export for the Intel CPU; YOLO11s fallback if knife/flame recall fails at 3–4 m.
- [x] *Related, from T-MODEL:* slim MobileNetV3-L / EfficientNet-B0 scene heads for the Pi (`models/v5_slim/`, `3bfb441`) — **failed** the pre-registered bar (LOVO macro-F1 0.229 / 0.211 vs DINOv2 0.440, §4.3); kept for clip-engine development only. ONNX/NCNN export path exists (`tools/export_models.py`); NCNN untested.

**M8 — Fitted fusion (week 3–4)** — `[ ]` not started (needs M6 logs): feature contract v3 logged every tick;
cross-fitted perception outputs; per-class logistic on session-grouped CV;
prior correction to the deployment base rate; thresholds from the notebook-03
cost matrix *and* an FA budget; monotonicity contract test; notebooks 02/03 on
real logs.

**M9 — Gates before any demo (week 4)** — `[ ]` not measured (tooling in M5;
needs S4): measured once on S4:
G0 data exists and is untouched · G1 detector frame AUROC ≥ 0.90 per class ·
G2 ≥ 27/30 episodes per class confirmed within 2 s · G3 0 L4/L5 in ≥ 3 h
normal + all hard negatives, ≤ 1 L3/h · G4 loop ≥ 5 Hz, detect→alert p95 ≤ 2 s ·
G5 30-min live dry run, 10/10 scripted events, 0 unscripted L3+. A failing class
is dropped from the promise or re-tested on a fresh S5 — never tuned on S4.
In clip mode (docs/8_RASPBERRY_PI5_INTEGRATION.md T1) G4 reads: fast-lane L3 p95 ≤ 2 s, a clip decision
every ≤ 2 s, L4/L5 p95 ≤ 6 s.

**M10 — Customer Portal, onboarding and SOS (branch `sentrix/portal`)** —
contract: `docs/7_PORTAL_AND_SOS.md` (ownership, config, schemas, link protocol
v1, tunnel + auth bridge, SOS state machine, channels, roles, endpoints).
- [x] **Foundation** (`685df9b`, "feat(foundation): contracts, config, guard, schema, services, portal skeleton"): `SosConfig`/`LinkConfig`/`ChannelConfig`/`GpioConfig`/`ClipConfig`/`NodeConfig`/`WebConfig`; send guard `sentrix/alerting/guard.py` (legacy Twilio SMS/call now a no-op unless `SENTRIX_TWILIO_ENABLED=1` + `SENTRIX_LIVE_ALERTS=1` + allowlist in test mode); additive device tables (assistance_requests, request_events, link_outbox, profile_cache, notifications, remote_audit) and columns; `set_verdict` by request state; services registry + lifespan; pipeline SOS hook (`NullSosService` default, asserted by the eval harness and tools); state blocks (sos, link, node, channels, mode, remote_viewers, test_mode); runtime HOME/ARMED; link protocol + tunnel auth; channel interfaces; LC extension routers; camera-URL redaction; `/docs` off by default; `sentrix_portal` skeleton (all tables, Argon2id, sessions, CSRF, `require_role`, audit chain, `/healthz`). 266 tests pass.
- [x] **Contract review** (`31c6c50`; 30 integration gaps, docs/7 §16 lists the ones answered differently): full `SosService` interface (`respond`, `cloud_event`, `sync`, `false_verdict`, `set_mode`, `approve_package`, `demo_*`, `current`; `SosResult`; null service keeps the pre-portal console behaviour) and `sentrix/actor.py`; one LC principal `sentrix/web/auth.py` (`require_lc`, 401 JSON for tunnel/API, `rate_key`, fail-closed fallback when hardening cannot load); once SOS owns an incident the legacy L4 SMS, L4 package and L5 call are skipped (siren stays); `REQUEST_LOCK` + `expected_version`, per-(request, origin) hash chains with a unique index, `import_event`, cloud-created requests, (DRAFTED, expire); `sentrix/listeners.py` (incident, dispatch, verdict, evidence, snapshot, audit, mode) and `sentrix.link.publish` (no-op without the link); `ChannelsService` + router listeners + `ready`, outbox meta round-trip, 64-byte Telegram callback data; profile accessor/cache keyed by `<identity_dir>/profile.key`; `Pipeline.observe/decide`, `InferenceStrategy`, `request_close_incident`, draining shutdown, lifespan order (pipeline drains before services stop; `close_interrupted` after services; ARMED persisted in `var/mode.json`); `siren_test` shared limiter; `RetentionConfig`; Portal key files, lifespan + background/middleware modules, CSP not on `/d/*`, `csrf="header"`, service stubs (hub, sos, accounts, notify, retention), `/consent/{token}`; `tests/conftest.py` (forced-off switches before any import, loopback-only socket guard, shared fixtures). 340 tests pass.
- [x] **Tracks** (parallel worktrees, merged in this order; no file touched by two tracks; nothing has run on real hardware or real provider accounts):
  - **T-HW**: gpiozero siren (active-high NPN default, 90 s max on-period, arming switch, 409 on a disarmed test, `SENTRIX_SIREN=auto` now honoured), GPIO service (LEDs 5/12/13, buzzer 26, PIR 23, door 24, mains 16, tamper), node telemetry + systemd watchdog, `PiCameraSource` (main 1280×720 + lores 640×480), nmcli Wi-Fi, `/api/node`, doctor hardware rows, `run.sh` Pi path, `deploy/pi5/**`, docs/8_RASPBERRY_PI5_INTEGRATION.md §6 pin table. Bench checks with an LED, real PIR/door/mains, Camera Module 3 and the 4G dongle still to do.
  - **T-CLIP**: 4 s clip engine (`pipeline/clip.py`), fast lane, torch/onnx/ncnn runtimes, `tools/export_models.py`, `tools/bench_clip.py`, `replay_custom_videos.py --mode clip`, `SENTRIX_FACE_MODEL`. Replay meets Gate A's criterion (weapon L3 9/12 with fast lane, fire 3/3, 0 false onsets); laptop bench p95 2145 ms > 1.6 s budget (docs/8_RASPBERRY_PI5_INTEGRATION.md T1). NCNN and `buffalo_s` untested (not installed).
  - **T-CH**: channel router + persistent outbox (dedupe, 30/90 s back-off, 24 h give-up, crash recovery), modem SMS (PDU, 30 segments/day, profile numbers only), Telegram, Brevo/Resend e-mail, Twilio wrapper, legacy notifier routed through the router, `/api/channels`, Portal outbox/worker/`/hooks/telegram`. Real modem/bot/e-mail accounts untested.
  - **T-SOS**: pure machine, frozen policy, templates (one English source), PINs (Argon2id, 5 / 10 min), sweeper, service, recovery, LC `/api/sos/*`, profile-filled dispatch packages. Live-session replay: home 0 requests, armed 1.
  - **T-WEB**: LC hardening (tunnel bridge, CSRF header, CSP nonces, no inline JS), base-aware URLs, every parity fix, `/sos`, `/node`, `/setup/wifi`, SOS prompt/banner, "remote view: N".
  - **T-ACCT**: activation codes, back-off, TOTP for admin/Desk, admin console + welcome letter + provisioning (`tools/provision_device.py`), 7-step wizard, household/contacts with consent, settings, privacy (export, erasure), retention worker, `build_profile`.
  - **T-LINK**: device link client (outbox, heartbeat, adaptive video), tunnel executor, device-enforced command rules and path table, Portal hub, `/d/{id}` proxy + relays, SOS mirror and cloud authority, Desk, contact pages. 100 forced disconnects lose 0 events; loopback end-to-end run green with the real hardening.
  - **T-MODEL**: MobileNetV3-L / EfficientNet-B0 scene heads (`models/v5_slim/`), ONNX export: **neither passes** (LOVO macro-F1 0.229 / 0.211 vs DINOv2 0.440); DINOv2 stays.
- [x] **Integration** (`4c7eca2`, merges `6626a9b` T-HW · `1be98c2` T-CLIP · `43d396f` T-CH · `7fc5cc9` T-SOS · `acf0b99` T-WEB · `bd37675` T-ACCT · `7710f18` T-LINK · `2fa40cf` T-MODEL): the eight merges; every hand-off note applied (config fields for T-HW and T-CLIP, `app.live_loop_alive`, idle `node`/`clip` blocks, redacted `ws_payload`, `cloud_online` from the link, `Pipeline.decide(extra=)` + clip-log refusal in `replay_decisions.py`, clip-mode "AI STALLED" limit, `AudioEngine.classify_window` + sample listeners wired into the clip buffer, incident filters in `database.py`, legacy `session_token` retired, `SendResult.retry_after`, Portal `telegram_mode` / `grievance_contact`, SOS delivery feedback from the Portal worker, `render_preview` seam, Portal SOS banner on every owner page, `link_outbox` AUTOINCREMENT, doctor export rows + `--no-camera`, Pi clip profile in `pi.env`); the link tests now run against the real LC hardening. **Found and fixed:** tests without `temp_db` opened the developer's `var/sentrix.db` (`SENTRIX_VAR_DIR` now moves the whole `var/` tree; conftest points it at a temp dir). Not applied (docs/7 §16 q7–q11): `bootstrap_revoked_at` column (needs Alembic), Portal SOS texts from the template module, bundled fonts, ncnn/`buffalo_s`. `python -m sentrix --help` and `python -m sentrix_portal --help` (new `--host`/`--port`) start nothing; both apps were smoke-started in temp dirs (LC on a video file, no microphone) and `tools/doctor.py --no-camera` passes on the laptop. 1538 tests pass.
- [x] **Demo tools and Portal deployment** (`232f938`): `tools/demo_reset.py` (demo world in a marked demo dir, never `var/`: admin + Desk operator with TOTP printed once, "Riya Sharma (demo)" fully onboarded or with `--live-steps 1-3` / `--activation` left for the stage, confirmed contact, provisioned + claimed device, `--safety-net`, 90 s window; team phones from `SENTRIX_DEMO_*` env only), `tools/demo_run.py` (Portal + device together: test mode, live switches forced off and provider keys blanked, `SENTRIX_AUDIO=0`, link to the local Portal, distinct hosts/cookies; `scene` / `close` / `trigger` / `clips`), the **demo reel camera** `sentrix/io/demo_reel.py` (`CAMERA_SOURCES=demo-reel:<json>`: idle vid07 0–60 s, scenes gun.mov / 4.mov held out (val), 2.mov / 6.mov in-sample backups; wa02 refused by name and split table), scripted trigger now **device-admin only** with a button on the LC SOS page. Live smoke run on the laptop: gun scene → PENDING weapon request ≈ 8 s after the scene start, mirrored on the Portal (e-mail SUPPRESSED by the guard), `close` cancels it (`test_reset`), `trigger` opens a `simulated` request whose text says SIMULATED DETECTION, 90 s window. `deploy/portal/` (python-slim non-root image, compose with Postgres on an internal network and only Caddy on 80/443, Caddyfile with HSTS + WebSockets + unbuffered MJPEG, names-only `.env.example`, backup/restore-test/port-check scripts, Oracle/Render+Supabase runbook) — Docker not run; checked by `tests/test_deploy_portal.py`. `tools/portal_pg_check.py`: DDL compiles for Postgres, the Portal tests pass under a VARCHAR length guard; psycopg not installed, so no live Postgres run yet. `deploy/pi5/pi.env` gained the test-mode, SOS, channels and link keys. Clip reliability is from the M5 replay only; the 10 live pre-runs per clip are still to do.
- [x] **Product end-to-end tests** (`7aca186`): `tests/test_e2e_product.py` + `tests/e2e/world.py` — Portal app, device LC with a fake pipeline, the real SOS engine on an injected clock, the real link client over an in-memory pipe into the Portal hub, fake Telegram/e-mail/modem transports, one event loop. 13 scenarios: provision → letter → activation → wizard → claim → tunnel parity on every LC page → L3 weapon to CLOSED; decline (PIN), expiry + Request help now, safety defaults, duress, device offline + reconnect, internet down SMS, Telegram and e-mail answers, XSS, cross-household isolation, admin view, test mode. Found: the Portal owner timeline dropped a duress "false alarm" row; conftest sets `YOLO_OFFLINE=1` (ultralytics probed public DNS on import).
- [x] **Live smoke run** (`640e713`, docs/7 §17): Portal and device as two real processes on the laptop, linked over a real WebSocket, driven over HTTP like a browser (test mode, demo reel, no audio, scratch dirs; `var/sentrix.db` untouched). Every LC page and 14 GET APIs gave the same status direct and tunnelled; numbers in §4.3. Five bugs fixed: Portal sign-in could land on /login (session committed after the 303 — routes now take `DB = Depends(get_db, scope="function")`); a Portal restart read as a lost device heartbeat (silence now counted from the hub's start at the earliest); a fire safety-default alert owed at T+2 min was dropped by the 90 s demo window (sent before the expiry now); LC showed 0 queued while offline; the scene model asked huggingface.co for a revision at every start (cached weights load from the cache file). Not verified live: Telegram (no bot), modem/SMS, delivery to allowlisted phones, 10 live pre-runs per clip (3/3 gun, 3/3 fire done).
- [x] **Adversarial review** (`58f62c9`): test mode fails closed on unrecognised values; conftest blocks webcam indexes and the microphone; the Portal call button never dials an emergency number and SOS texts never say "call 112" in test mode; siren stops after the requested duration; PIR edges reach the clip buffer; doctor opens no camera unless `--probe-camera`; SOS machine/service, onboarding step 5 and Portal SOS service fixes with tests (`tests/test_review_guards.py` and the track test files).
- [x] **Repository clean-up** (`e7ca89e`): unreachable v1 weapon/fire weights, the unwired Vosk model, the duplicate `yolov8n.pt`, v1/v2 run dumps, duplicate v3 checkpoints, old UCF-DVS probe files and the university templates removed; capstone submissions → `docs/capstone/`, `HARDWARE.md` → `docs/8_RASPBERRY_PI5_INTEGRATION.md`, diagrams → `docs/assets/`. Voice SOS (Vosk) is no longer planned in this repository.
- [x] **Docs for the integrated product** (`72953fe` `ROADMAP.md`; `1ff8026` README, this file, `.env.example` comments, `THIRD_PARTY.md`; `8f53087` docs/8 and the `deploy/pi5`, `deploy/portal` READMEs; `dbc83cd` docs 1–7 and the `docs/README.md` index; then a consistency pass across all of them). Full suite: **1647 passed** at `e7ca89e` (6 min 30 s) and again at `dbc83cd` (390.6 s) (laptop; the device and socket guards blocked 4 camera/mic and 2 network attempts by tests that check those guards).
- [ ] **Still to do:** Alembic baseline (and the `bootstrap_revoked_at` column it unblocks); a live Postgres run (`tools/portal_pg_check.py` only compiles the DDL; psycopg not installed); `deploy/portal/` on a real VM (Docker not run); hardware bench checks (LED relay test, PIR, door, mains, Camera Module 3, 4G modem); real Telegram bot, e-mail account and modem SMS to allowlisted team phones (both switches); `tools/bench_link.py` (named in docs/7 §15 item 3, not written); Portal SOS texts from the device template module, bundled fonts, NCNN and `buffalo_s` (docs/7 §16 q7–q11); 10 live pre-runs per demo clip; **gates P1–P8** measured once on the final build (P1, P2, P3, P7 have partial evidence in the test suite; P4, P5, P8 need hardware or recordings).

**M11 — Multimodal, time-aligned fusion and the talk-down speaker (D15, D16; `sentrix/portal`, not pushed)**
- [x] **Sound model** (`30074ab`): PANNs CNN10 AudioSet tagger (`sentrix/perception/sound_tagger.py`; the published `Cnn10` module with its parameter names, STFT and mel bank from the checkpoint, so only torch + scipy); weights tracked in `models/audio/` (25 MB, SHA-256 checked by `tools/fetch_models.py`); 527 labels → cues aggression, weapon_sound, fire_sound, benign_social (laughter, music, TV, calm talk scaled by 1 − aggression), siren; the 4-class CNN is the fallback (`SENTRIX_AUDIO_MODEL`); 48.9 ms per 4 s window. Considered: AST (346 MB, needs transformers, 10 s input), CNN14 (327 MB).
- [x] **Time alignment** (`30074ab`): every `AudioResult` carries its window (monotonic start/end on the pipeline clock, wall end); `AudioEngine.result_for(t)` / `audio.alignment` use it only if it overlaps `t ± 1.5 s`, else *unavailable* (never "calm"); one window votes once (fresh). Clip mode classifies the clip's own buffered window (aligned by construction) or the engine's window if aligned. Replays hear the video's own soundtrack (`FileAudio`, ffmpeg decode, live 4 s / 1 s grid; `replay_custom_videos.py --audio auto|off`). Alignment recorded in state, event log (schema 5) and contributions.
- [x] **Fusion** (`30074ab`, `fusion/evidence.py`, `tci.py`): modalities video detector / video classifier / audio; one visual modality → L3, two → L4 (weaker sets it), three → ≥ L5 (0.86); audio alone ≤ L2; aggression (`audio_aggression`) corroborates a visible weapon but never stands alone; fight rule and banter rule (D15); weapon person gate applies to audio too; unknown person and armed PIR/door activity → `intrusion_level` 0.30 (clamped under L3) in both modes; every result carries `contributions` (video detector, scene model, audio or why unavailable, people, motion, identity, sensors) shown on the dashboard's Threat Analysis card; gpio PIR/door events reach `Pipeline.note_sensor`. SOS: `assault` joins `alarm_classes` (corroborated only), intrusion requests behind `SENTRIX_SOS_INTRUSION`.
- [x] **Talk-down speaker** (`30074ab`, D16): `sentrix/io/speaker.py` (pyttsx3 / espeak-ng → aplay / console; injectable player), `GET /api/speaker`, `POST /api/speaker/say` (device admin; owner + step-up via the path table class `speaker`; members and the Desk refused; ≤ 200 chars or a preset; 3/min; single flight; audited `speaker_say`); dashboard card (external `speaker.js`, CSRF header, base-aware URL; visible through the tunnel to the owner) and the Node page's speaker row; conftest forces it off and blocks the real players and sounddevice playback. Smoke-tested in the browser on a temp `var/` (gun.mov as camera, console backend): preset spoken and audited, contributions rendered.
- [x] **Tests**: `tests/test_multimodal.py` (alignment, stale audio unavailable, banter keeps a visual fight at L1, fight + aggression → L3, one person is not a fight, gunshot + weapon box → L4, three modalities → L5, a scream alone is not a weapon, unknown person L2 in both modes and never configurable into L3, armed sensors L2, contributions per input, clip-mode audio, pipeline wiring and the event-log row) and `tests/test_speaker.py`; SOS and fusion tests updated to the amended D2/D8. Full suite: **1716 passed** (516.8 s, laptop; the guards blocked 5 camera/mic/speaker and 2 network attempts by tests that check those guards).
- [x] **Evaluation** (no tuning; cue thresholds a priori, wa02 replayed only inside `--videos all`, as M5 did, and never used to choose anything): section 4.3. At L3+ nothing changed against M5 or the clip baseline; audio added one L4 (vid05); the only new onsets are the by-design unknown-person L2s.
- [ ] **Not done / honest limits**: no room audio exists - the custom videos are phone recordings of staged scenes, and the only staged fight (vid05) sounds like calm talk, so the fight → L3 path and the banter rule are exercised by unit tests only; `weapon_sound` fires on replica-gun handling ("Cap gun" 0.2–0.8 in gun.mov) and has never seen door slams, claps or TV gunfire (hard negatives in ROADMAP §15.1); `fight_motion_floor` 0.02 only excludes a still scene (motion on these handheld videos is 0.1–0.4); live mic alignment and the speaker are untested on the Pi (USB adapter, espeak-ng) and on the real webcam; pyttsx3 installed in `.venv` but never played (tests and smoke used the console backend).

### Owners (from the README team table)

Kartik — detector path, confirmation, unified YOLO (M1, M7) · Akshay — identity,
tracks, enrollment (M4) · Mehul — records, dashboard, eval/gate tooling (M3, M5,
M9) · Harshit — recording protocol, fusion (M6, M8) · Prashant — audio to
log-only, gunshot/glass-break data track (non-gating).

### Open questions for the team

**Owner decisions surfaced by M1–M5:** (1) CUDA torch in the app env (Python 3.14 needs a cp314 CUDA wheel, or move the app env to 3.11/3.12) — moves DINOv2 to the RTX 4060; not needed for 5 Hz on CPU. (2) A folder of ≥ 3 h normal footage from the demo room for `--negatives` (G3). (3) Pre-roll evidence clips need a multi-frame vault format (M3 deferral). (4) Re-test live on the real webcam: video should now run at camera rate and the TCI should match the level.

**Owner decisions surfaced by M11:** (5) Home mode now raises an L2 "unrecognised person" after 10 s (D2 as amended, owner requirement 4): every unenrolled guest becomes an L2 incident on the dashboard - keep it, lengthen `SENTRIX_UNKNOWN_HOME_DWELL`, or set `SENTRIX_UNKNOWN_ALERT_HOME=0` (logged only). (6) Talk-down speaker hardware: USB adapter + PAM8403 + 3 W speaker (≈ ₹450) or a USB speakerphone (≈ ₹1,500–2,500, also the microphone). (7) Record the audio sessions of ROADMAP §15.1 (play-fighting vs staged fights with sound, laughter, shouting, TV, alarm beeps, gunshot/glass sounds from a speaker) before any fight or audio claim is made.

Demo date and room; which camera (laptop webcam vs a mounted USB camera — data
must be recorded with it); permission for replica weapons and flames on campus;
whether darkness is a requirement (needs IR); ~34 person-hours of recording,
correction and QC in weeks 1–3; who deleted `Kartik.jpg` during the run.

**Owner decisions from the Portal/SOS roadmap (v2, §17):** Pi 5 8 GB now or
4 GB; Camera Module 3 Wide or Standard, and whether night operation (NoIR + IR)
is needed; default SOS window 30 or 60 min and whether the D9 safety defaults
stay on; whether a corroborated fire sounds the siren at L4 instead of L5
(validate on fire-on-screen hard negatives first); who plays the Desk, owns the
Oracle account and card, and holds and recharges the SIM; demo room, hotspot
phone, English only or Hindi too; permission for replica weapons and flames and
the consent notice to the legal cell.
