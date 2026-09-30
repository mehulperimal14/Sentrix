# SENTRIX — Deployment Guide & Operations Runbook

**Version 3.0 · Updated: August 2026**

---

## 1. Prerequisites

| Requirement | Minimum | Recommended |
|---|---|---|
| Python | 3.10 | 3.12 |
| RAM | 4 GB | 8 GB |
| GPU | none (CPU works) | CUDA 12+ or Apple MPS |
| OS | Windows 10, Ubuntu 22.04, macOS 13 | any |
| Camera | any UVC webcam | 1080p UVC or RTSP |
| Microphone | optional | USB boundary mic |

Disk: ~2 GB for dependencies, ~200 MB for model weights.

---

## 2. Quick Start

**Windows**

```bat
run.bat
```

**macOS / Linux**

```bash
chmod +x run.sh && ./run.sh
```

Both scripts now:

1. create `.venv` if absent
2. `pip install -r requirements.txt`
3. copy `.env.example` → `.env` (and on Unix, generate `SESSION_SECRET` and
   `EVIDENCE_AES_KEY` with `openssl`)
4. run `tools/doctor.py`
5. start the server

Then set `SENTRIX_PASSWORD` in `.env` and open
**http://127.0.0.1:8000/dashboard**.

> **Corrected from v2.1.** The previous version of this section claimed both
> scripts created a virtual environment and installed dependencies. Neither did —
> they only copied `.env` and launched `python backend/app.py`. The root
> `requirements.txt` could not have started the application in any case: it was
> missing `jinja2`, `sqlalchemy`, `python-dotenv`, `cryptography`, `scipy`,
> `sounddevice` and `pandas`, and shipped `pycryptodome` (unused) in place of
> `cryptography` (actually imported). Both scripts and the requirements file have
> been fixed.

---

## 3. Manual Installation

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

# GPU PyTorch — install AFTER requirements if you want CUDA
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121

cp .env.example .env            # then set SENTRIX_PASSWORD
python tools/doctor.py
python -m sentrix
```

### Strongly recommended

```bash
pip install insightface onnxruntime
```

Without a face-embedding backend, identity **fails closed**: nobody is ever
authorised, and no alert is ever suppressed for a recognised resident. The system
still runs, but the authorisation feature does nothing. `tools/doctor.py` reports
this explicitly.

`insightface` is preferred over `face_recognition` because it needs no C++ build
chain on Windows and uses 512-d ArcFace embeddings rather than dlib's 128-d model.
The `buffalo_l` model pack (~280 MB) downloads once on first use.

### Optional

```bash
pip install torchreid     # appearance re-id; histogram fallback without it
pip install vosk          # voice SOS — not currently wired into the pipeline
pip install jupyterlab matplotlib   # notebooks
```

> Create the venv on an NTFS or APFS drive, never on FAT32 or a USB stick. FAT32
> handles thousands of small package files badly and `pip install` can hang for
> an hour.

---

## 4. Configuration

**Secrets and deployment specifics** live in `.env`. **Algorithmic thresholds**
live in [`sentrix/config.py`](../sentrix/config.py), version-controlled and
documented, so tuning is reviewable rather than buried in a local file.

| Variable | Required | Purpose |
|---|---|---|
| `SENTRIX_PASSWORD` | **Yes** | Dashboard password. Auth refuses if unset |
| `SESSION_SECRET` | Production | HMAC secret. `python -c "import secrets; print(secrets.token_hex(32))"` |
| `EVIDENCE_AES_KEY` | Production | Derives the AES **and** manifest signing keys. Unset ⇒ evidence is unreadable after restart |
| `CAMERA_SOURCES` | No | Comma-separated: index, RTSP/HTTP URL, or file path. Default `0` |
| `SENTRIX_HOST` / `SENTRIX_PORT` | No | Default `127.0.0.1:8000` |
| `SENTRIX_SECURE_COOKIES` | Behind TLS | `1` sets the `Secure` cookie flag |
| `SENTRIX_SIREN` | No | `auto` \| `gpio` \| `sound` \| `console` |
| `RETENTION_DAYS` | No | Default 30. Level 4–5 and unreviewed incidents are never pruned |
| `TWILIO_*`, `ALERT_PHONE_NUMBER` | No | Falls back to console logging |
| `SENTRIX_USER_*`, `SENTRIX_CAMERA_LOCATION` | No | Dispatch package fields |

Optional overrides that shadow `config.py` for quick experiments:
`SENTRIX_INFERENCE_FPS`, `SENTRIX_PERSON_CONF`, `SENTRIX_WEAPON_CONF`,
`SENTRIX_FIRE_CONF`, `SENTRIX_LOITER_SECONDS`, `SENTRIX_AUDIO_RMS_FLOOR`,
`SENTRIX_IDENTITY_FAIL_CLOSED`; threat evidence `SENTRIX_ARMED`,
`SENTRIX_THREAT_SOURCES`, `SENTRIX_CONFIRM_K`/`_N`, `SENTRIX_SCENE_THRESHOLDS`; face
cost `SENTRIX_FACE_DET_SIZE`, `SENTRIX_FACE_ORT_THREADS`. (`SENTRIX_EMA_TAU` was
removed with the display EMA.)

> **Removed since v2.1.** `ROBOFLOW_API_KEY` — there is no cloud inference path
> and the variable was already unread. `VOSK_MODEL_PATH` — voice SOS is not
> wired; its previous default of `"vosk-model"` resolved relative to the working
> directory and therefore never found the model, so the feature was silently
> disabled in every deployment.

---

## 5. Dashboard

| URL | Page | Auth |
|---|---|---|
| `/login` | Login | no |
| `/dashboard` | TCI gauge, live metrics, health | yes |
| `/live` | Full-screen MJPEG feed | yes |
| `/events` | Incident log with review verdicts | yes |
| `/alerts` | Snapshot gallery | yes |
| `/evidence` | Evidence explorer + chain status | yes |
| `/dispatch` | Dispatch packages | yes |
| `/authorized` | Enrolled face registry | yes |

### API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/metrics` | Live state |
| `GET` | `/api/health` | Per-subsystem status and latency |
| `GET` | `/api/config` | **New** — the live tunables |
| `GET` | `/api/incidents` | **New** — incident history and stats |
| `POST` | `/api/incidents/{id}/verdict` | **New** — operator feedback: `TRUE` / `FALSE` |
| `GET` | `/api/evidence/verify` | **New** — walk and verify the manifest chain |
| `GET` | `/api/dispatch/latest` | Latest pending package |
| `POST` | `/api/dispatch/{id}/send/{authority}` | Send (`POLICE` \| `FIRE`) |
| `POST` | `/authorized/upload` | Enrol a face |
| `DELETE` | `/api/authorized/{filename}` | Remove an enrolment |
| `GET` | `/media/snapshot/{name}` | **New** — authenticated snapshot |
| `GET` | `/media/face/{name}` | **New** — authenticated enrolled image |
| `GET` | `/media/evidence/{bundle_id}` | **New** — decrypt a bundle for review |
| `WS` | `/ws/threat` | State push at 2 Hz |
| `GET` | `/video` | MJPEG stream |

**The `/media/*` routes are a security fix, not a feature.** Evidence, snapshots
and enrolled faces were previously served from the `/static` mount, to which
FastAPI applies no authentication.

`POST /api/incidents/{id}/verdict` is the most valuable endpoint in the system
for the project's future: every reviewed incident becomes a labelled training row
for the fusion model.

---

## 6. Model Management

**No promotion step is required.** Each engine resolves its weights in preference
order and loads the first file present:

| Engine | Preference order |
|---|---|
| Weapon | `v3_real/weapon_detector_real_v3.pt` → `weapon_detector.pt` |
| Fire | `v3_real/fire_smoke_detector_real_v3.pt` → `fire_smoke_detector.pt` |
| Violence | `v3_real/violence_classifier_real_v3.pt` → `violence_classifier.pt` |
| Audio | `v3_real/audio_classifier_real_v3.pt` → `audio_classifier.pt` |

Confirm what actually loaded with `python tools/doctor.py`.

Violence and audio read class names, normalisation and operating thresholds from
the checkpoint itself, and the audio engine builds its architecture from the
checkpoint's parameter names. A new checkpoint with a different class count or
topology therefore loads without a code change.

> **Corrected from v2.1.** The previous §6 instructed operators to copy V3 files
> over the deployed names. For audio that would have **failed**: the V2 and V3
> checkpoints have incompatible architectures, so `load_state_dict` raised inside
> a bare `except`, leaving the model unloaded and audio silently returning
> `normal_ambient` forever. It was never executed in practice — the deployed
> violence and audio weights remained V2 through the mid-semester submission.

---

## 7. Training & Evaluation

Training scripts are in `training/scripts/`; training notebooks in
`training/notebooks/`. Analysis notebooks that do **not** train are in
`notebooks/`.

```bash
python training/scripts/train_violence_classifier.py
python training/scripts/train_audio_classifier.py
python training/scripts/train_weapon_detector.py <epochs>
python training/scripts/train_fire_smoke_detector.py <epochs>
python training/scripts/evaluate_models.py
```

`training/scripts/refit_xgboost.py` exists but **fits on synthetic labels**. Do
not use it to produce a number for the report — see doc 2 §4.

### Evaluation, in order

```bash
python tools/doctor.py                    # what is actually running
python tools/audit_splits.py manifest ... # check for split contamination FIRST
python tools/diagnose_anomaly.py          # UCF-Crime polarity check
python tools/baseline.py --clips data/clips     # motion-only comparator
python tools/replay.py --clips data/clips --no-audio   # labelled event logs
python tools/evaluate.py --json report.json     # the metrics table
jupyter lab notebooks/                    # calibration and thresholds
```

`tools/replay.py` drives the same `Pipeline.process` the live camera uses. There
is deliberately no separate evaluation path — the previous build had one, it had
already diverged, and every published number therefore came from code that was
not the product.

---

## 8. Operations

### Daily

- `/events` — review incidents, mark each `TRUE` or `FALSE`. This is the labelled
  dataset the fusion model needs.
- `/api/health` — confirm `identity_backend` is not `null` and `camera` is `true`.

### Weekly

- `GET /api/evidence/verify` — the chain must report `ok: true`.
- Check `var/` growth. Snapshots and event logs are the two that accumulate.

### Retention

`prune()` deletes incidents older than `RETENTION_DAYS` **only if** peak level < 4
**and** an operator has reviewed them. Unreviewed incidents are never deleted —
removing them would destroy the labelled data the retraining depends on.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Cannot log in | `SENTRIX_PASSWORD` unset | Set it in `.env`, restart |
| "Too many attempts" | Rate limiter, 8 per 5 min | Wait, or restart |
| No video | Wrong camera index | `python tools/doctor.py` lists working indices |
| Nobody is ever authorised | No face backend | `pip install insightface onnxruntime` |
| Enrolled face rejected | No detectable face in the image | Upload returns a warning; use a clear frontal photo |
| Evidence unreadable after restart | `EVIDENCE_AES_KEY` unset | Set a stable hex key. Prior evidence is unrecoverable |
| Chain verify fails | Bundle modified or deleted | Check `problems[]` in the response for the first break |
| Audio always `normal_ambient` | Below the RMS gate, or no mic | Lower `SENTRIX_AUDIO_RMS_FLOOR`; check doctor |
| Everyone is `GID-1` | Expected without `torchreid` | `pip install torchreid` |
| Siren does nothing | `ConsoleSiren` selected | Check `siren_is_real` in `/api/health` |
| High TCI on an empty room | Should not happen | Doctor asserts empty-scene TCI < 0.10. If it fails, check `SENTRIX_CALIBRATE_INPUTS` is not set to `1` |

---

## 10. Pre-Production Checklist

- [ ] `SENTRIX_PASSWORD` changed from `admin` to something strong
- [ ] `SESSION_SECRET` set to 32 random bytes
- [ ] `EVIDENCE_AES_KEY` set to 32 random bytes and **backed up** — losing it makes every bundle unreadable
- [ ] `pip install insightface onnxruntime`, residents enrolled, verified via `/authorized`
- [ ] `python tools/doctor.py` reports zero failures
- [ ] `pytest tests/` passes
- [ ] Behind an HTTPS reverse proxy; `SENTRIX_SECURE_COOKIES=1`
- [ ] Bound to LAN or VPN — never expose port 8000 publicly
- [ ] Twilio configured, or accept console-only alerting
- [ ] Level bands reviewed against `notebooks/03_tci_thresholds.ipynb`
- [ ] `GET /api/evidence/verify` returns `ok: true`

**Known gap.** The deployed TCI fusion model is trained on synthetic labels
(doc 2 §4). It works — hard overrides and hysteresis carry the safety-critical
paths — but the fused score is not yet calibrated against real events. Treat
levels 2 and 3 as advisory until `notebooks/02` has been run on real labelled
footage.
