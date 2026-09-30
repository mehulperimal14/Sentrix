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
| App, tests, evaluation | `.venv\Scripts\python.exe` (3.14, **CPU** torch) | `python -m sentrix`, `python -m pytest -q tests/` |
| Training (GPU) | `C:\Users\PC\anaconda3\envs\sentrix\python.exe` (3.11, torch cu126, RTX 4060 8 GB) | has ultralytics, timm, transformers |

### Data locations

| What | Where |
|---|---|
| Custom frame dataset (765 reviewed frames, 5 classes) | `C:\Users\PC\OneDrive\Desktop\dataset` (`SENTRIX_CUSTOM_DATASET`) |
| Its 10 source videos | `C:\Users\PC\OneDrive\Desktop\Videos` (`SENTRIX_CUSTOM_VIDEOS`) |
| Vendored ground truth, boxes, QC rejects | `training/datasets/sentrix_custom/` |
| Original corpora (RWF-2000 frames, weapon, fire/smoke, audio, UCF-DVS) | `G:\Capstone\data` |

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
| `main` | `d4f37cd` | pre-session history (18 commits), untouched |
| `sentrix/baseline-v3` | `ab01e84` | staged rewrite committed (`f74996f`) + evaluation harness (`ebc1349`) + doc fixes |
| `sentrix/finetuned-v3` | `321cf7c` | baseline + scene integration + training tooling + fine-tuned v3 models |
| `sentrix/custom-v4` | `cfef092` | baseline + scene integration + training tooling + new DINOv2/YOLO11n models (recommended) |
| `sentrix/e2e-repair` | (this branch) | custom-v4 + this plan + the repair work |

Commits on `sentrix/custom-v4` since `main`:

```
cfef092 docs(readme): test count
9673ee8 feat(web): show the scene classifier on the dashboard and in /api/health
b9fea16 docs: baseline latency measured on an idle machine
3c362e6 docs(readme): latency of the three configurations
ed1bf30 docs: measured pipeline latency of all three branches, fire threshold note
a61a4f8 fix(doctor): report the weights each engine resolves, and load the scene model
6fc8fc3 feat(models): v4 custom models trained on the custom dataset
3963702 feat(scene): delta checkpoints for partially frozen backbones
bcaca05 feat(training): custom-dataset box annotation, training and model selection
88660db feat(perception): five-class scene classifier wired into the TCI
ebc1349 feat(eval): custom-dataset evaluation harness and v3 baseline results
f74996f refactor: restructure backend/ into the sentrix package (v3 current state)
```

Pre-session `main` (newest first): `d4f37cd` master engineering plan docs ·
`3c6a32c` custom-data/IoT docs · `ec6c27f` ignore runtime output · `42e63c6`
v3_real paths + clean-frame overlays · `535cb2c` relax YOLO box limits ·
`8aec357` YOLO thresholds 0.25, HSV flame, theft/harmful scores · `7a9e0b5` docs ·
`7d5d342` workspace restructure · `98f8a69` restructure, ML engines, reports ·
`a16ba85`…`2a63633` restructure series · `6a9485e`, `46e230a` Phase 2.5.

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
| Inference slow (median 479 ms, p95 2.25 s) | Identity runs the full 5-model InsightFace `buffalo_l` on the full frame **every tick** (29 % of pipeline time; 1.1 s with two faces); DINOv2-B scene model ~105–170 ms; 3 separate YOLO passes; no CPU thread budget (torch 16 threads + ORT spinning). Three test processes were also loading the CPU during minutes 0–10. |
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

### Milestones

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done (with commit).

**M1 — Containment: stop the false alarms (code only, days 1–2)**
- [ ] Scene evidence needs **k-of-n confirmation** counted in ticks (weapon/fire 2-of-3, max gap 3 s) and a **person seen in the last 2 s** (weapon); scene window becomes a tick count.
- [ ] **Per-deployment thresholds** (`var/calibration/scene.json` / `SENTRIX_SCENE_THRESHOLDS`), interim weapon 0.50, fire 0.45, plus margin over p(normal).
- [ ] Remove single-frame **hard overrides**; weapon/fire enter as per-class evidence with the source recorded (`weapon_source`, `fire_source`). Scene-only evidence capped at L3 (D3).
- [ ] Retire `tci_xgboost.json`: **transparent fusion** = max over per-class evidence (D6); incident type = strongest class; reasons name the actual source ("scene classifier p=0.52, 2/3 ticks").
- [ ] Presence/identity out of the score: drop `unknown_person_boost` and the intrusion override; armed-mode intrusion rule (D2).
- [ ] **LevelPolicy rewrite**: per-threshold timers (continuous time at/above each threshold), ≥ 2 observations to escalate, step-down after continuous time below, no reset trap. Update `tests/test_fusion.py` deliberately.
- [ ] **Acceptance**: replay of the logged live session → 0 weapon/fire incidents, 0 L4; held-out `gun.mov` and `4.mov` still detected (`tools/replay_custom_videos.py`); new unit tests (single spiked tick → no alarm; ramp across bands commits; flapping de-escalates in 5 s; scene-only never L4).

**M2 — Real time: smooth video, honest rates (days 2–4)**
- [ ] Decouple **capture / inference / preview**: inference on its own thread with a single-slot latest-frame handoff; `PreviewBroadcaster` composites the latest overlay on every captured frame, encodes once, wakes clients event-driven; `replay()`/harness stay synchronous.
- [ ] `CaptureSource`: no sleep after a blocking device read; frame seq + timestamp; request 640×480@30.
- [ ] CPU budget: `torch.set_num_threads`, ORT `intra_op_num_threads` without spinning, `cv2.setNumThreads`.
- [ ] Identity off the hot path: `allowed_modules=['detection','recognition']`, head-crop at det_size 320, per-track cache (new tracks / every 1 s unknown / every 10 s authorised), worker thread.
- [ ] HUD/state report **VIDEO fps · AI Hz · latency** separately; stale boxes hidden after 3 s; fix navbar health keys.
- [ ] **Acceptance**: preview ≥ 25 distinct fps; AI ≥ 3 Hz with 2 people on the idle laptop CPU; identity ≤ 5 ms p95 on the pipeline thread.

**M3 — Coherent state and trustworthy records (days 4–6)**
- [ ] One **committed alarm object** in state (level, since, incident type + headline reason from the incident's peak, de-escalation countdown) separate from the live block; badge = committed level + incident type.
- [ ] Incident **PeakSnapshot**: type/reason/TCI/features/authorised all from the same tick; L1 frames inside an open incident still counted; `authorized` persisted; schema migration guarded (`PRAGMA table_info` + `ALTER TABLE`).
- [ ] Evidence and snapshot from a **pre-roll ring buffer** at the trigger frame (max raw evidence), resident rule applied to the selected frame; sidecar records the trigger block (component, scene probs, YOLO maxima, identity, fusion mode) and the pipeline session.
- [ ] Event log **schema 3**: per-tick attribution (scene probs, YOLO maxima, override/source, identity sim, band, t_start); flush every row; persist open incident on shutdown and at open.
- [ ] Events page: verdict buttons (TRUE/FALSE), fixed snapshot/evidence links, local time; dispatch created after evidence and cancelled on FALSE.
- [ ] **Acceptance**: no persisted row mixes ticks (unit test on the replayed live log); `taskkill /F` loses ≤ 1 row and keeps the open incident.

**M4 — Identity and enrollment (days 5–7)**
- [ ] `POST /api/authorized/capture` from the pipeline's raw frame (auth, rate limit, quality gates: one face, size, sharpness, luma, duplicate check); raw preview endpoint; `authorized.html` uses them.
- [ ] Fix upload (`Path(name).stem`, per-file result), move reloads off the event loop, reload lock, incremental enroll, remove the extra `</div>`, per-tile status, gallery changes audited in the event log and `enrolled_faces`.
- [ ] **Per-track identity**: authorised/unknown/unverified per ByteTrack id; floor only when all tracks authorised; hold bound to the track; face-miss ≠ stranger.
- [ ] **Acceptance**: enrol the whole team while monitoring; 5-minute team sit-in → 0 incidents in home mode, identity flicker < 0.1 flips/person-minute.

**M5 — Evaluation that matches the product (days 6–8)**
- [ ] Replay metrics per **committed level**: onsets, false onsets/hour, time-at-level on normal, episode recall and latency per level.
- [ ] **Negative-session replay** mode (hours of normal footage → FA/h with Poisson 95 % bound); multi-rate replay (6/2/0.5 Hz); optional real identity in the harness.
- [ ] Gate report tool (G0–G5 below) with Clopper-Pearson / Poisson bounds.

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

### Owners (from the README team table)

Kartik — detector path, confirmation, unified YOLO (M1, M7) · Akshay — identity,
tracks, enrollment (M4) · Mehul — records, dashboard, eval/gate tooling (M3, M5,
M9) · Harshit — recording protocol, fusion (M6, M8) · Prashant — audio to
log-only, gunshot/glass-break data track (non-gating).

### Open questions for the team

Demo date and room; which camera (laptop webcam vs a mounted USB camera — data
must be recorded with it); permission for replica weapons and flames on campus;
whether darkness is a requirement (needs IR); ~34 person-hours of recording,
correction and QC in weeks 1–3; who deleted `Kartik.jpg` during the run.
