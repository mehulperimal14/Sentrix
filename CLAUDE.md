# CLAUDE.md — SENTRIX

Project memory and working plan. Read this first in every session. Sections 1–4
are facts; section 5 is the diagnosis of the live product; section 6 is the
repair plan and its progress. Keep section 6's status boxes current.

---

## 1. What this project is

**SENTRIX** — a local, edge-first multimodal threat-detection system (Capstone
CPG-299, CSED, Thapar Institute; mentor Dr. Ashutosh Mishra; team: Kartik Garg,
Akshay Ranveer, Prashant Gagneja, Harshit Mishra, Mehul Perimal).

A camera and a microphone feed a stack of perception engines; their scores are
fused into a **Threat Confidence Index (TCI, 0–1)** that drives a five-level
escalation ladder:

| Level | TCI band (asserted) | Side effects |
|---|---|---|
| 1 SECURE | 0.00–0.20 | log |
| 2 SUSPICIOUS | 0.20–0.40 | + snapshot, incident opened |
| 3 ELEVATED | 0.40–0.65 | + AES-256-GCM evidence bundle |
| 4 HIGH | 0.65–0.85 | + SMS, dispatch package |
| 5 CRITICAL | 0.85–1.00 | + siren, voice call |

Everything runs locally; FastAPI dashboard at `http://127.0.0.1:8000/dashboard`.

### Architecture (one frame, `sentrix/pipeline/engine.py` → `Pipeline.process`)

```
FrameSource (io/sources.py: webcam | file | RTSP)
  └─ Detector (perception/detector.py)   YOLO person (+ByteTrack) / weapon / fire heads
  └─ SceneEngine (perception/scene.py)   5-class classifier: normal fire weapon assault robbery
       └─ replaces ViolenceEngine (perception/violence.py) when a scene model is installed
  └─ BehaviourEngine, MotionEngine, IdentityEngine (insightface, fail-closed), ReIDEngine
  └─ AudioEngine (own thread, mic)
  → FeatureVector (fusion/features.py, CONTRACT_V2: 8 features)
  → TCIEngine (fusion/tci.py): hard overrides → XGBoost (synthetic, 5-feature v1) → boosts → EMA
  → LevelPolicy (fusion/policy.py): hysteresis (0.6 s up / 5 s down)
  → IncidentManager (pipeline/incidents.py) → side effects (_act): snapshot, evidence, SMS, siren, dispatch
  → EventLog (var/events/*.jsonl), SystemState (dashboard websocket), HUD overlay
```

All tunables: `sentrix/config.py` (seconds, never frames). Runtime output:
`var/` (gitignored, never web-mounted).

### Environments

| Purpose | Python | Notes |
|---|---|---|
| App, tests, evaluation | `.venv` (Python 3.10–3.14; maintainer PC: 3.14, **CPU** torch) | `run.bat` / `run.sh`, or `python -m sentrix`; `python -m pytest` |
| Training (GPU) | `C:\Users\PC\anaconda3\envs\sentrix\python.exe` (3.11, torch cu126, RTX 4060 8 GB) | has ultralytics, timm, transformers |

### Data locations

| What | Where |
|---|---|
| Custom frame dataset (765 reviewed frames, 5 classes) | `training/datasets/sentrix_custom/frames/` (tracked; `SENTRIX_CUSTOM_DATASET` overrides) |
| Its 10 source videos | `training/datasets/sentrix_custom/videos/` (tracked; `SENTRIX_CUSTOM_VIDEOS` overrides) |
| Vendored ground truth, boxes, QC rejects | `training/datasets/sentrix_custom/` |
| Original public corpora (RWF-2000 frames, weapon, fire/smoke, audio, UCF-DVS) | not in git; `data/` or `SENTRIX_VIOLENCE_DATA` / `SENTRIX_WEAPON_DATA` / `SENTRIX_FIRE_DATA` (maintainer PC: `G:\Capstone\data`) |

### Rules for working here

* **Never let a test or evaluation send an alert.** Evaluation goes through
  `sentrix/evaluation/harness.py` (`build_eval_pipeline`: `_act`/`_submit` are
  no-ops, identity neutral, no mic). `.env` may hold live Twilio keys.
* **Never tune on the test video** (`wa02`). Select with leave-one-video-out CV
  over the nine train+val videos, confirm on val, report test once.
* Quote numbers from generated reports (`tools/custom_eval_table.py`), with the
  commit that produced them.
* Branches carry configurations; commit, never push unless asked.

---

## 2. What has been done

### Before this work (main, 2026-08-21 → 08-25)

V3 restructure and model training in notebooks: violence ResNet-18 (RWF-2000),
audio log-mel CNN (AudioSet clips), weapon and fire/smoke YOLOv8n (Roboflow
corpora), UCF-Crime DVS anomaly model (not wired). TCI fusion booster fitted to a
synthetic formula. A staged (uncommitted) rewrite moved `backend/` into the
`sentrix/` package with 15 documented fixes (see README "What changed in v3").

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

---

## 3. Git history

| Branch | Head | Contents |
|---|---|---|
| `main` | = `sentrix/e2e-repair` | the published state: custom-v4 models + M1–M5 + fresh-clone setup (pre-session head was `0f75a0e`) |
| `sentrix/baseline-v3` | `d8629eb` | staged rewrite committed (`9e0037a`) + evaluation harness (`05a04fe`) + doc fixes |
| `sentrix/finetuned-v3` | `66accb9` | baseline + scene integration + training tooling + fine-tuned v3 models |
| `sentrix/custom-v4` | `3772acf` | baseline + scene integration + training tooling + new DINOv2/YOLO11n models (recommended) |
| `sentrix/e2e-repair` | same as `main` | custom-v4 + this plan + M1–M5 + fresh-clone setup (commits below) |
| `sentrix/portal` | (M10) | main + HARDWARE.md + Portal/SOS foundation; tracks branch from it (M10 below) |

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

Commits on `sentrix/custom-v4` since `main`:

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

## 6. Repair plan

Principles: **measure before and after every change** (the replayed live log is a
regression test); **a single modality never reaches L4 alone**; **presence is
context, not threat**; **promise only what the data can validate**; the test
video/session is never used for tuning.

### Decisions taken (recommended defaults — change here if the team disagrees)

| # | Decision | Why |
|---|---|---|
| D1 | Alarm classes for the demo: **handgun (replica), knife, visible flame**. Assault, robbery, stick, smoke and audio are *experimental*: logged and shown, never above L2. | They exist in 1–3 clips; unvalidatable classes only add false alarms. |
| D2 | **Home mode** (default): an unknown person is logged, never escalates. **Armed mode** (`SENTRIX_ARMED=1`): a *confirmed-unknown* track sustained ≥ 3 s opens an intrusion incident at L3. | Team members and guests are "unknown" by default. |
| D3 | A scene-classifier verdict alone is capped at **L3** (snapshot + evidence, no SMS/siren). L4+ needs corroboration (detector box of that class, or a second modality). | The classifier is the least reliable input and out of domain. |
| D4 | Snapshots move from L2 to **L3**; L2 is UI/log only. | Privacy of residents; L2 becomes "unconfirmed". |
| D5 | Darkness and faces < 1 m are **out of scope** for an RGB webcam: the HUD shows `DEGRADED` and threat typing is held. | Physics, not model tuning. |
| D6 | Fusion becomes **transparent per-class evidence** (published rules) until a fitted, calibrated model exists from ≥ 4 recorded sessions. The synthetic booster is retired. | It is polarity-inverted and ranks worse than its inputs. |
| D7 | The 7 incidents of session `c27258a25457` are false alarms; the team labels them through the new verdict buttons (M3). They become the first real fusion labels. | Operator feedback loop. |
| D8 | (amends D3/D4) A **confirmed L3** of an alarm class (weapon, fire) or an armed intrusion after a 30 s entry delay opens an SOS assistance request; the owner is prompted on Portal, LC, Telegram, e-mail; SMS only offline or unseen after 10 min; no siren at L3. | The team's SOS flow without prompt storms. |
| D9 | Window 30/45/60 min (default 30); no answer = EXPIRED ("unanswered", never "safe"); "Request help now" opens a linked request. Safety defaults ON (corroborated L4/L5, fire at T+2 min, armed intrusion, device offline during an open L2+ incident → confirmed contacts); single-source weapon L3 → owner only until gate P8. Duress PIN; policy frozen per request. | Silence must not fail the user who cannot answer. |
| D10 | Parity by **tunnel**: Portal `/d/{id}/...` replays into the device's own LC app in-process; `/video` and `/ws/threat` relayed; remote video adaptive ≤ 15 fps. | One code path, no drift. |
| D11 | Oracle Always Free (India) + `.in` domain + Caddy; Render + Supabase fallback; e-mail by HTTPS API. | ₹0 hosting, data in India. |
| D12 | Cloud keeps profiles, metadata and one consented SOS snapshot per request (30 d); faces, evidence and video stay on the device (viewed on demand only). | Minimisation. |
| D13 | **Test mode is the default and the only mode this semester**; sending needs `SENTRIX_LIVE_ALERTS=1` + the channel switch, and only to `SENTRIX_TEST_RECIPIENTS` in test mode; the legacy Twilio path is a no-op unless enabled; link, modem, SOS sweeper opt-in. | `.env` may hold live keys. |
| D14 | Accounts by username + single-use activation code; admin/Desk TOTP; the LC keeps `SENTRIX_PASSWORD`; Portal password hashes never reach the device. | No admin takeover, no hashes on the SD card. |

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

**M6 — Deployment-room data (team, weeks 1–3)** — protocol in the strategy
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

**M8 — Fitted fusion (week 3–4)**: feature contract v3 logged every tick;
cross-fitted perception outputs; per-class logistic on session-grouped CV;
prior correction to the deployment base rate; thresholds from the notebook-03
cost matrix *and* an FA budget; monotonicity contract test; notebooks 02/03 on
real logs.

**M9 — Gates before any demo (week 4)** — measured once on S4:
G0 data exists and is untouched · G1 detector frame AUROC ≥ 0.90 per class ·
G2 ≥ 27/30 episodes per class confirmed within 2 s · G3 0 L4/L5 in ≥ 3 h
normal + all hard negatives, ≤ 1 L3/h · G4 loop ≥ 5 Hz, detect→alert p95 ≤ 2 s ·
G5 30-min live dry run, 10/10 scripted events, 0 unscripted L3+. A failing class
is dropped from the promise or re-tested on a fresh S5 — never tuned on S4.

**M10 — Customer Portal, onboarding and SOS (branch `sentrix/portal`)** —
contract: `docs/7_PORTAL_AND_SOS.md` (ownership, config, schemas, link protocol
v1, tunnel + auth bridge, SOS state machine, channels, roles, endpoints).
- [x] **Foundation** (commit "feat(foundation): contracts, config, guard, schema, services, portal skeleton"): `SosConfig`/`LinkConfig`/`ChannelConfig`/`GpioConfig`/`ClipConfig`/`NodeConfig`/`WebConfig`; send guard `sentrix/alerting/guard.py` (legacy Twilio SMS/call now a no-op unless `SENTRIX_TWILIO_ENABLED=1` + `SENTRIX_LIVE_ALERTS=1` + allowlist in test mode); additive device tables (assistance_requests, request_events, link_outbox, profile_cache, notifications, remote_audit) and columns; `set_verdict` by request state; services registry + lifespan; pipeline SOS hook (`NullSosService` default, asserted by the eval harness and tools); state blocks (sos, link, node, channels, mode, remote_viewers, test_mode); runtime HOME/ARMED; link protocol + tunnel auth; channel interfaces; LC extension routers; camera-URL redaction; `/docs` off by default; `sentrix_portal` skeleton (all tables, Argon2id, sessions, CSRF, `require_role`, audit chain, `/healthz`). 266 tests pass.
- [ ] Tracks (parallel, own worktrees): T-HW (Pi I/O, deploy), T-CLIP (clip engine), T-SOS (device SOS engine), T-CH (channels), T-WEB (LC hardening + SOS/Node pages), T-LINK (link client, Portal hub/tunnel/SOS/Desk/contacts), T-ACCT (accounts, admin, onboarding, household, settings, privacy), T-MODEL (slim scene model).
- [ ] Integration: merge order T-HW, T-CLIP, T-CH, T-SOS, T-WEB, T-ACCT, T-LINK; end-to-end tests; demo tools; Docker/Caddy deploy; README; gates P1–P8.

### Owners (from the README team table)

Kartik — detector path, confirmation, unified YOLO (M1, M7) · Akshay — identity,
tracks, enrollment (M4) · Mehul — records, dashboard, eval/gate tooling (M3, M5,
M9) · Harshit — recording protocol, fusion (M6, M8) · Prashant — audio to
log-only, gunshot/glass-break data track (non-gating).

### Open questions for the team

**Owner decisions surfaced by M1–M5:** (1) CUDA torch in the app env (Python 3.14 needs a cp314 CUDA wheel, or move the app env to 3.11/3.12) — moves DINOv2 to the RTX 4060; not needed for 5 Hz on CPU. (2) A folder of ≥ 3 h normal footage from the demo room for `--negatives` (G3). (3) Pre-roll evidence clips need a multi-frame vault format (M3 deferral). (4) Re-test live on the real webcam: video should now run at camera rate and the TCI should match the level.

Demo date and room; which camera (laptop webcam vs a mounted USB camera — data
must be recorded with it); permission for replica weapons and flames on campus;
whether darkness is a requirement (needs IR); ~34 person-hours of recording,
correction and QC in weeks 1–3; who deleted `Kartik.jpg` during the run.
