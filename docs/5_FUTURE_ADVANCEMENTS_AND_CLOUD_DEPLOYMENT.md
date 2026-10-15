# 5. SENTRIX — Roadmap & Remaining Work

**Describes branch `sentrix/portal` (October 2026).** What works, what is
built but unverified, and what is left, in priority order. The milestone
plan with its status boxes is `CLAUDE.md` §6 (M1–M10); the reviewed plan for
the product and the Pi is the team's "SENTRIX Prototype Roadmap v2" (October
2026). This document summarises both and keeps the model-research notes that
are still valid. The step-by-step plan to a working prototype (orders, cloud
setup, Pi bring-up, Gate A, rehearsals, gates, demo script) is
[`ROADMAP.md`](../ROADMAP.md). It replaces the August 2026 roadmap, whose cloud-GPU
deployment section no longer applies: detection stays on the device
(decisions D10–D12).

---

## 1. Current status

### Working and verified

| Capability | Evidence |
|---|---|
| Detection pipeline with confirmed per-class evidence, level policy, incidents | 0 false alarm onsets on the replayed first live session (home mode); 13 of 18 labelled episodes caught (doc 1 §5, doc 6 §6.8) |
| Smooth preview and honest rates on the laptop CPU | preview 29.7 fps, AI 5.6 Hz with four faces (doc 2 §7) |
| Evidence vault, records, verdicts, event log | `tests/test_records.py`; crash test lost 0 event rows (`CLAUDE.md` M3) |
| Per-track identity, enrolment from the monitoring camera | unit tests (`CLAUDE.md` M4); team acceptance still to do |
| Portal accounts, onboarding, claim, tunnel parity, SOS approve / decline / expiry / Request help now, Desk, offline takeover | e2e suite (13 scenarios) and one live two-process run (doc 7 §17) |
| Send guard: nothing sent by default, two switches, allowlist | guard tests; every message SUPPRESSED in the live run |

### Built, not yet verified on real hardware or accounts

| Item | What is missing |
|---|---|
| Raspberry Pi 5: siren relay, LEDs, inputs, telemetry, Pi camera, systemd, network failover | a Pi; bench test with an LED in place of the siren (docs 8, `deploy/pi5/README.md`) |
| 4G modem SMS | a modem and SIM; check PDU mode, delivery reports, the AT port |
| Telegram bot, Brevo / Resend e-mail | a bot token and provider accounts; one rehearsal message per channel to an allowlisted team account |
| Portal on Oracle / Render with Postgres | Docker run, a live Postgres check (`tools/portal_pg_check.py live`), DNS and TLS |
| Clip mode on ONNX | a Pi measurement; the laptop bench misses the 1.6 s budget at p95 (doc 2 §7) |
| NCNN backend, `buffalo_s` face pack | not installed; tested with fakes |

### Not built

| Item | Notes |
|---|---|
| Device retention caps and signed vault tombstones | doc 7 §3.8 |
| Alembic baseline for the Portal | needed before any column rename (doc 7 §16 q7) |
| `tools/bench_link.py` | remote fps, glass-to-glass latency, uplink, CPU |
| Fitted fusion model | M8; needs real labelled sessions |
| Re-identification beyond the histogram fallback | install `torchreid`, evaluate on Market-1501 (§3.4) |
| UCF-Crime anomaly model | not wired; polarity bug (doc 2 §6) |
| Voice SOS | not planned in this repository; the unwired Vosk model was removed in `e7ca89e` |
| Tier 2 product items | inbound SMS parsing, web push / PWA, owner TOTP, Hindi templates, automated export and erasure jobs, WebRTC video, multi-process Pi layout |

---

## 2. The critical path

### 2.1 Record deployment-room data (M6) — the blocker for every gate

Every remaining claim depends on footage the team does not have yet. The
custom dataset holds 341 s of labelled video from two rooms, and its 3.7
normal minutes bound false alarms only at ≤ 60 per hour (doc 6 §6.8).

The protocol (`CLAUDE.md` M6): the demo room R1 plus one other room R2; the
camera mounted at 1.8–2.2 m, tilted down, in a lit room; sessions S1/S2 for
training, S3 for validation, S4 for the test (day 5 or later, unseen
volunteers); scripted 20 s clips, 12 per class per room per session, with as
many hard negatives as positives (a phone or remote held like a pistol, an
unlit lighter, an aerosol can, fire on a screen, orange clothing); at least
3 h of ordinary footage in R1 on non-training days; replicas and flames only
with written permission and a safety notice.

This unlocks gates G0, G3 and G5 (`CLAUDE.md` M9), the SOS prompt budget P8
(doc 7 §15 item 7), and the data that M7 and M8 train on.

### 2.2 Models that fit the room (M7)

* One YOLO11n detector (person, pistol, knife, fire) at 640 px from COCO, with
  QC'd teacher boxes, rehearsal and all the hard negatives; it replaces three
  YOLO passes.
* A person-crop weapon classifier as the second vote; the scene classifier
  retrained with deployment negatives and an out-of-distribution guard.
* Slim scene heads did not replace DINOv2 (doc 2 §5.2). Next fallback to test
  for the Pi: DINOv2-S/14, which was outside the pre-registered candidates.

### 2.3 Fitted fusion (M8)

The synthetic XGBoost booster was retired in M1. A real model needs the feature
contract logged every tick on the M6 sessions, a per-class logistic fit on
session-grouped CV, a prior correction to the deployment base rate, and
thresholds from the cost matrix in `notebooks/03_tci_thresholds.ipynb` plus a
false-alarm budget. Define the target as a calibrated `P(threat | features)`,
so "TCI = 0.7" has a meaning and ECE is the headline metric. Dataset builders
must drop or relabel incidents behind a duress PIN (doc 7 §7.8).

### 2.4 Gates before the demo (M9 and P1–P8)

Measured once on the S4 test sessions and the final build:

| Gate | Pass when | Today |
|---|---|---|
| G0 | gate data exists and is untouched | not run |
| G1 | detector frame AUROC ≥ 0.90 per class | PASS on the custom videos (scene weapon 0.93, fire 0.999), partly in-sample |
| G2 | ≥ 27/30 episodes per class confirmed within 2 s | FAIL (weapon 8 of 12 within 2 s) |
| G3 | 0 L4/L5 in ≥ 3 h of normal footage and all hard negatives; ≤ 1 L3/h | not run (no footage) |
| G4 | loop ≥ 5 Hz; detect → alert p95 ≤ 2 s | FAIL (5.6 Hz, but onset → L3 p95 4.0 s) |
| G5 | 30-min live dry run, 10/10 scripted events, 0 unscripted L3+ | not run |
| P1–P8 | parity, isolation, SOS correctness, latency, offline, privacy, no alerts in tests, prompt budget | doc 7 §15 item 7 |

Gate results: `training/runs/custom_eval/m5/gates.json` (`tools/gate_report.py`).
A failing class is dropped from the promise or re-tested on a fresh session,
never tuned on S4.

### 2.5 Hardware bring-up

In order: the bench kit (doc 8 §8), the LED relay test, debounce checks on
real PIR, door and mains signals, Picamera2 on the Camera Module 3 Wide, the
clip-mode gate on the Pi with the link running (Gate A, doc 8 §7), then the
modem on the Pi and network failover with the actual 4G dongle.

### 2.6 Demo rehearsal

The 10 live pre-runs per demo clip (keep a clip only at ≥ 9 of 10; 3 of 3 so
far for `gun.mov` and `4.mov`, doc 7 §17), a rehearsal with real delivery to
allowlisted team phones (both switches on, test mode on), and the demo script
of the Roadmap v2 (admin, onboarding, approve, false alarm, no answer, internet
down).

---

## 3. Model research notes (still valid, not started)

### 3.1 Acoustic classifier — retrain

The current model is the weakest: 375 clips across 4 classes, `scream` with 8
validation samples, and a reported macro-F1 that is a single-epoch spike
(doc 2 §4). Audio is log-only under decision D1 until this changes.

Recommended 6 classes: `ambient`, `scream`, `glass_break`, `gunshot`,
`alarm_siren` and **`nuisance_impulsive`** (firecrackers, horns, door slams,
dropped objects). The last class gives the model somewhere to put loud but
harmless sounds; a firecracker-vs-gunshot confusion matrix closes research gap
RG-5 with evidence. Data: ≥ 200 clips per class from AudioSet, UrbanSound8K and
MIVIA. Architecture: **AST** (Audio Spectrogram Transformer) fine-tuned from
AudioSet weights; `perception/audio.py` builds its architecture from the
checkpoint, so it drops in.

### 3.2 Violence / action recognition

The v3 violence model is per-frame and only used without a scene model. A
temporal model (ResNet-18 + LSTM as proposed, or VideoMAE / TimeSformer; for
the Pi a MobileViT-class model) on RWF-2000 **re-split by video** plus
XD-Violence (aligned audio and video). Assault and robbery stay experimental
(≤ L2) until the M6 data can validate them.

### 3.3 Re-split and re-measure the v3 corpora

Doc 2 §3: split violence by source video, de-duplicate the detector image
sets, evaluate the unused 703-image weapon `test/` split. Report the corrected
figures with the de-duplication step described.

### 3.4 Re-identification

Install `torchreid` and evaluate on Market-1501 (published rank-1 protocol;
OSNet reaches about 94 %, above the ≥ 85 % objective). Upgrade path: CLIP-ReID
or DINOv2 embeddings behind the same cosine matcher.

### 3.5 Pose estimation

`yolov8n-pose.pt` is pretrained and `PoseEstimator` is written but not wired.
Keypoints beat box centroids for aggression, and fix the crawl heuristic that
fires on boxes clipped at the frame edge.

### 3.6 UCF-Crime anomaly

`python tools/diagnose_anomaly.py`: the inverted AUC is 0.806, so the likely
fix is one line (doc 2 §6). Only then consider wiring it.

---

## 4. Deferred (Tier 2 and later)

| Item | Notes |
|---|---|
| Inbound SMS (approve by reply) | the modem's read API exists; parsing is not built |
| Web push / PWA | needs the HTTPS Portal |
| Owner TOTP, Hindi message templates | designed, not built |
| WebRTC remote video | today: an MJPEG relay up to 15 fps |
| Multi-process Pi layout | deferred by the single-process decision; revisit if Gate A fails with the link running |
| Liveness for the face disarm | a photograph of a resident could clear the entry delay (doc 7 §16 q6) |
| End-to-end encryption of remote viewing | video passes through the Portal in memory over TLS, not end-to-end encrypted |
| Thermal input to fusion | telemetry exists; a fusion feature needs a contract version and data |

---

## 5. Indian deployment context

* **Acoustic false-positive suppression** — festive pyrotechnics, pressure
  horns, street celebrations — through the `nuisance_impulsive` class (§3.1).
* **Visual domain adaptation** — IDD (IIIT Hyderabad) for unstructured scenes
  and lighting.
* **Emergency services.** India's 112 (ERSS) has no public interface for
  private alarm companies, so SENTRIX never calls it: an approved SOS goes to
  the SENTRIX Desk and confirmed contacts, and every text says "SENTRIX does
  not contact 112 itself".
* **Data protection (DPDP Act 2023, Rules 2025).** Consent per purpose, data
  stored in India, hard retention caps, no minors' faces in the capstone
  (doc 7 §12, decision D12).
* **Regional SOS keywords** (Hindi *bachao*, *chor*, *aag*) would need a voice
  SOS engine, which is not planned in this repository.
