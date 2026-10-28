# 4. SENTRIX — Deployment Guide & Operations Runbook

**Describes branch `sentrix/portal` (October 2026).** How to install and run
the device (with its Local Console), the Portal, and the two together for a
demo; how to configure, evaluate and operate them. The Portal and SOS contract
is [doc 7](7_PORTAL_AND_SOS.md); the Raspberry Pi is
[doc 8](8_RASPBERRY_PI5_INTEGRATION.md) and `deploy/pi5/README.md`.

> **Safety first.** Test mode is on by default and is the only mode this
> semester. Nothing is sent unless `SENTRIX_LIVE_ALERTS=1` **and** the
> channel's own switch are set, and in test mode only to
> `SENTRIX_TEST_RECIPIENTS`. Emergency numbers are always refused. Tests,
> replays, benchmarks and the demo tools force the switches off. `.env` may
> hold live provider keys: keys alone send nothing (doc 7 §8.1).

---

## 1. Prerequisites

| Requirement | Minimum | Recommended |
|---|---|---|
| Python | 3.10 | 3.12–3.14 (the maintainer laptop runs 3.14) |
| RAM | 4 GB | 8 GB |
| GPU | none (CPU works; the app environment uses CPU torch) | CUDA moves the scene model to the GPU automatically when torch has it |
| OS | Windows 10, Ubuntu 22.04, macOS 13 | any; Raspberry Pi OS Trixie for the Pi |
| Camera | a video file is enough | UVC webcam, RTSP, or the Pi camera |
| Microphone | optional | USB microphone, in any port, plugged in before or after start-up (D21) |
| ffmpeg | optional | on `PATH`: a training recording keeps its sound inside the video (else a separate `.wav`), and replays hear a video's soundtrack |

Disk: the clone is about 0.8 GB (weights, custom dataset and videos are
tracked); dependencies take 2–5 GB; the first start downloads about 600 MB of
public weights (the DINOv2 base and InsightFace `buffalo_l`).

---

## 2. Quick start: the device

**Windows** `.\run.bat` · **macOS / Linux** `./run.sh`

Both scripts:

1. create `.venv` if absent (on a Pi, with `--system-site-packages` and
   `requirements-pi.txt`);
2. `pip install -r requirements.txt` (again only when it changes);
3. run `tools/setup_env.py`: copy `.env.example` → `.env` and fill every blank
   secret — the console password (printed once), `SESSION_SECRET` and
   `EVIDENCE_AES_KEY`;
4. run `tools/doctor.py`;
5. start `python -m sentrix`.

Open **http://127.0.0.1:8000** and sign in with the printed password. No
webcam: set `CAMERA_SOURCES=training/datasets/sentrix_custom/videos/gun.mov`
(a bundled clip, looped) and `SENTRIX_AUDIO=0` in `.env`.

This starts the console as before: the SOS service, the channel router, the
Portal link and the hardware services stay off until their flags are set (§4).

---

## 3. Manual installation

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt   # includes insightface, onnxruntime, timm, httpx,
                                  # pyserial, argon2-cffi, pyotp, gpiozero
python tools/setup_env.py         # creates .env with generated secrets
python tools/doctor.py            # never opens a camera unless --probe-camera
python -m sentrix --help          # --host, --port; starts nothing
python -m sentrix
```

GPU torch is optional: install the CUDA wheel after the requirements. It
replaces the app's torch install, and Python 3.14 needs a cp314 CUDA wheel
(an owner decision in `CLAUDE.md`).

Without a face-embedding backend identity **fails closed**: nobody is ever
authorised. `insightface` is in the requirements; the `buffalo_l` pack (about
280 MB) downloads on first use. `tools/doctor.py` reports which backend loaded.

Optional: `pip install torchreid` (appearance re-identification; histogram
fallback without it), `jupyterlab matplotlib` (notebooks). Voice SOS (Vosk) is
not wired and its model is not shipped.

> Create the venv on an NTFS or APFS drive, never on FAT32 or a USB stick:
> `pip install` can hang for an hour on thousands of small files.

---

## 4. Configuration

**Secrets and deployment values** live in `.env` (`.env.example` lists every
variable with a comment). **Algorithmic thresholds** live in
[`sentrix/config.py`](../sentrix/config.py), version-controlled, so tuning is
reviewable.

### 4.1 Device: the basics

| Variable | Required | Purpose |
|---|---|---|
| `SENTRIX_PASSWORD` | **Yes** | Console password (the device admin). Sign-in refuses if unset |
| `SESSION_SECRET` | Production | HMAC secret for the `lc_session` cookie |
| `EVIDENCE_AES_KEY` | Production | Derives the AES **and** manifest-signing keys. Unset ⇒ evidence is unreadable after a restart |
| `CAMERA_SOURCES` | No | index, RTSP/HTTP URL, file path, `picamera:0`, `demo-reel:<json>` (doc 3 §6). Default `0` |
| `SENTRIX_HOST` / `SENTRIX_PORT` | No | Default `127.0.0.1:8000`. Do not bind `0.0.0.0` without a reverse proxy and TLS |
| `SENTRIX_SECURE_COOKIES` | Behind TLS | `1` sets the `Secure` cookie flag |
| `SENTRIX_AUDIO` | No | `0` disables the microphone |
| `SENTRIX_MIC_DEVICE` | No | Which input to open: empty = automatic (USB, a newer one first, then another input plugged in while running, then the default; headsets, Bluetooth hands-free and virtual microphones never), or a PortAudio index or name substring (`python tools/doctor.py` lists them) |
| `SENTRIX_RECORDING_ENABLED`, `SENTRIX_RECORD_*` | No | Training recordings on the dashboard (§9 "Recordings and disk"; doc 7 §3.4) |
| `SENTRIX_ARMED` | No | ARMED at first start (later changes persist in `var/mode.json`) |
| `SENTRIX_SIREN` | No | `auto` \| `gpio` \| `sound` \| `console` |
| `RETENTION_DAYS` | No | Default 30 (§8) |
| `SENTRIX_USER_*`, `SENTRIX_CAMERA_LOCATION` | No | Dispatch package fields for a standalone device |

Tuning overrides that shadow `config.py` (prefer editing `config.py`):
`SENTRIX_INFERENCE_FPS`, `SENTRIX_PERSON_CONF`, `SENTRIX_WEAPON_CONF`,
`SENTRIX_FIRE_CONF`, `SENTRIX_LOITER_SECONDS`, `SENTRIX_AUDIO_RMS_FLOOR`,
`SENTRIX_IDENTITY_FAIL_CLOSED` (leave at 1), `SENTRIX_THREAT_SOURCES`,
`SENTRIX_CONFIRM_K` / `_N`, `SENTRIX_SCENE_THRESHOLDS`,
`SENTRIX_FACE_DET_SIZE`, `SENTRIX_FACE_ORT_THREADS`, `SENTRIX_FACE_MODEL`.

### 4.2 Device: the optional services

Each is off unless its flag is `1` (doc 7 §3.6). The full tables, with every
field and default, are doc 7 §3.1–§3.4.

| Flag | Service | Also needs |
|---|---|---|
| `SENTRIX_SOS_ENABLED` | SOS assistance requests at a confirmed L3 | a PIN: the Portal profile's, or `SENTRIX_SOS_PIN_HASH` on a standalone device (else the console password is accepted at the PIN prompt) |
| `SENTRIX_CHANNELS_ENABLED` | channel router and outbox | `SENTRIX_MODEM_ENABLED` (+ `SENTRIX_MODEM_PORT`) for the 4G modem; Telegram / e-mail secrets only for a standalone device |
| `SENTRIX_LINK_ENABLED` | link to the Portal | `SENTRIX_PORTAL_URL`; an `identity.json` from `tools/provision_device.py` in `SENTRIX_DEVICE_IDENTITY_DIR` (default `/etc/sentrix`, else `var/link`) |
| `SENTRIX_NODE_ENABLED`, `SENTRIX_GPIO_ENABLED` | telemetry; LEDs and inputs | a Pi (mock pins elsewhere) |
| `SENTRIX_PIPELINE_MODE=clip` | the 4-second clip engine | `SENTRIX_RUNTIME=onnx` and exports from `tools/export_models.py` for speed |

### 4.3 Sending: test mode and the two switches

| Variable | Default | Meaning |
|---|---|---|
| `SENTRIX_TEST_MODE` | on | Only an explicit `0` / `false` / `no` / `off` turns it off; any other value keeps it on |
| `SENTRIX_LIVE_ALERTS` | 0 | Switch 1 of 2, for every channel |
| `SENTRIX_MODEM_LIVE`, `SENTRIX_TELEGRAM_LIVE`, `SENTRIX_EMAIL_LIVE`, `SENTRIX_TWILIO_ENABLED` | 0 | Switch 2, per channel |
| `SENTRIX_TEST_RECIPIENTS` | empty | The allowlist in test mode (E.164 numbers, e-mail addresses, `tg:<chat id>`) |

**Breaking change (M10):** the legacy Twilio L4 SMS, L5 call and dispatch "Send
now" need `SENTRIX_TWILIO_ENABLED=1` plus `SENTRIX_LIVE_ALERTS=1` (and, in
test mode, `ALERT_PHONE_NUMBER` in the allowlist). Twilio keys alone send
nothing.

### 4.4 Portal

`python -m sentrix_portal` reads `PORTAL_*` variables (doc 7 §3.5; names in
`.env.example`). In test mode (the default, `PORTAL_TEST_MODE=1`) it generates
`secret_key` and `data_key` once into `PORTAL_DATA_DIR` (default
`var/portal`); back them up, because without them no device, login or code
verifies. With test mode off both keys are required from the environment.
`PORTAL_WORKERS=1` starts the background modules (heartbeat watcher, cloud SOS
timers, notification worker, retention); they are off by default and in tests.

---

## 5. The Local Console

| URL | Page |
|---|---|
| `/login`, `/logout` (POST; GET shows a confirm page) | sign-in |
| `/dashboard` | TCI gauge, engine scores, scene card, pipeline rates, evidence state, event log, SOS panel; the *Record video* card: the device admin signed in on the LAN starts, stops, downloads and deletes; the owner through the Portal starts and stops (after a step-up) and sees the list, while downloads and deletes stay on the LAN (D20) |
| `/live` | full-screen video with the HUD |
| `/events` | incidents with filters, paging, Real / False verdicts |
| `/alerts` | snapshot gallery |
| `/evidence` | evidence bundles and chain status |
| `/dispatch` | "Dispatch & SOS": packages, their requests, "Request help now" on closed ones |
| `/authorized` | face gallery: capture from the camera, upload, remove |
| `/sos` | the open assistance request, history, PIN answers; scripted trigger for the device admin in test mode |
| `/node` | profile, telemetry, GPIO, HOME/ARMED, siren test, link, channels, clip status, the microphone in use (or "not connected") |
| `/setup/wifi` | Wi-Fi setup, only while `SENTRIX_SETUP_MODE=1` |

The JSON API, with methods and error codes, is doc 7 §9.2 (new routes) and
`sentrix/web/routes.py`; the main reads are `GET /api/health`,
`/api/metrics`, `/api/config`, `/api/incidents`, `/api/evidence/verify`,
`/api/sos`, `/api/node`, `/api/link`, `/api/channels`, and `/api/recordings`
(the device admin, and the owner through the Portal). `/docs` exists only with
`SENTRIX_API_DOCS=1`. Every state-changing request needs the
`X-CSRF-Token` header that the pages send.

---

## 6. Model management

**No promotion step.** Each engine loads the first file present in its
preference order:

| Engine | Preference order |
|---|---|
| Scene | `v4_custom/scene_classifier_custom_v4.pt` → `v4_finetuned/scene_classifier_ft_v4.pt` (`SENTRIX_SCENE_WEIGHTS` overrides) |
| Weapon | `v4_custom/weapon_detector_custom_v4.pt` → `v4_finetuned/weapon_detector_ft_v4.pt` → `v3_real/weapon_detector_real_v3.pt` |
| Fire | `v4_custom/fire_detector_custom_v4.pt` → `v4_finetuned/fire_smoke_detector_ft_v4.pt` → `v3_real/fire_smoke_detector_real_v3.pt` |
| Violence | `v3_real/violence_classifier_real_v3.pt` → `violence_classifier.pt` (only without a scene model) |
| Audio | `v3_real/audio_classifier_real_v3.pt` → `audio_classifier.pt` |

`v4_finetuned/` is not on this branch (doc 2 §5). Confirm what loaded with
`python tools/doctor.py`. Checkpoints carry their class names, normalisation
and thresholds, so a new checkpoint with a different class count loads without
a code change.

Clip mode on ONNX: `python tools/export_models.py` writes the exports to
`models/exported/` with a parity check; `SENTRIX_RUNTIME=onnx` (or
`SENTRIX_SCENE_RUNTIME` for the scene model only) serves them. NCNN is
supported in code but untested (the converter is not installed).

---

## 7. Training and evaluation

Training scripts: `training/scripts/` (v3), `training/custom/` (v4 and v5,
GPU environment); notebooks in `training/notebooks/`; analysis notebooks in
`notebooks/`. `training/scripts/refit_xgboost.py` fits on synthetic labels and
must not produce a number for the report (doc 2 §4).

On the bundled custom dataset (no external data needed):

```bash
python tools/eval_custom_frames.py --splits train,val,test --tag local
python tools/replay_custom_videos.py --videos all --tag local [--hz 2] [--mode clip --fast-lane]
python tools/bench_live.py --json var/bench.json
python tools/bench_clip.py --seconds 60 --runtime onnx --fast-lane --json var/bench_clip.json
python tools/gate_report.py --replay training/runs/custom_eval/local/replay_all.json --bench var/bench.json
```

General tools: `tools/doctor.py` (what is running), `tools/audit_splits.py`
(split contamination, before quoting any metric), `tools/diagnose_anomaly.py`
(the UCF-Crime polarity check), `tools/baseline.py` (motion-only comparator),
`tools/replay.py` and `tools/evaluate.py` (labelled event logs and metrics on
your own clips), `tools/replay_decisions.py` (re-decide a recorded tick log;
refuses clip-mode logs).

Every replay drives the same pipeline code the live camera uses, through
`sentrix/evaluation/harness.py`, which disables every side effect, keeps the
null SOS service and asserts no service runs (`assert_measurement_safe`).
Never tune or select on the test video `wa02`.

Tests: `python -m pytest` (1647 tests; add `-o addopts=""` for the summary
line). The suite runs in temporary directories, with a loopback-only socket
guard and every send switch forced off.

---

## 8. Running the Portal and a demo

**Locally, two processes:**

```bash
python -m sentrix_portal                 # http://portal.localhost:8100
SENTRIX_LINK_ENABLED=1 SENTRIX_SOS_ENABLED=1 SENTRIX_CHANNELS_ENABLED=1 \
SENTRIX_PORTAL_URL=http://127.0.0.1:8100 python -m sentrix
```

The device needs a provisioned identity first: `python
tools/provision_device.py admin --username <name>` (the first company admin,
activation code printed once) and `python tools/provision_device.py device
--out <identity dir> --label label.html`. Use distinct host names in the
browser (`portal.localhost` and `127.0.0.1`) so the two session cookies do
not collide.

**The demo world** (all data in a marked demo directory, never `var/`):

```bash
python tools/demo_reset.py --dir var/demo --window 90      # accounts, household, device
python tools/demo_run.py --dir var/demo start               # Portal + device, test mode forced
python tools/demo_run.py --dir var/demo scene gun           # play a held-out clip on the demo reel
python tools/demo_run.py --dir var/demo close               # close the incident before the next scene
python tools/demo_run.py --dir var/demo trigger weapon      # disclosed scripted trigger
```

What each option does, and how the scenes were chosen: doc 7 §15 item 3. The
results of a full live run with these tools: doc 7 §17.

**Deployment:** the Portal on a VM with Docker, Caddy and Postgres —
`deploy/portal/README.md` (Oracle Always Free in India, or Render + Supabase;
not yet run). The device on a Raspberry Pi 5 — `deploy/pi5/README.md` (not yet
run on hardware).

---

## 9. Operations

### Daily

- `/events`: review incidents and mark each Real or False. Reviewed incidents
  are the labelled data the fusion model needs (M8). With an open SOS request,
  False needs the SOS PIN.
- `/api/health`: `identity_backend` is not `null`, `camera` is `true`, the
  `services` you enabled are running, `send_guard` shows what may be sent.

### Weekly

- `GET /api/evidence/verify`: the chain must report `ok: true`.
- Check `var/` growth; snapshots and event logs accumulate, and training
  recordings most of all (below).
- Portal: run `deploy/portal/restore_test.sh` against the latest backup.

### Retention

Device: `prune()` deletes incidents older than `RETENTION_DAYS` only if their
peak level is below 4 **and** they have been reviewed. The hard caps of doc 7
§3.8 (unreviewed 180 days, TRUE 1 year, SOS snapshot 30 days) are applied by
the Portal's retention worker to its own tables; on the device they are not
built yet.

### Recordings and disk

Training recordings (D20) are the largest files the device writes: raw
MJPEG frames at the camera's resolution, up to 10 minutes each. They live in
`var/recordings/` (`rec_<date>_<time>[_<label>].avi`, a `.wav` when ffmpeg is
missing, and a `.json` sidecar).

- **Make one:** sign in to the console with the device password - on the Pi
  from a computer on the same network (`http://<device-ip>:8000`; `pi.env`
  binds the LAN and `ufw.sh` limits the port to your subnet), on a laptop on
  the laptop itself (`http://127.0.0.1:8000`: the console binds loopback
  only unless `SENTRIX_HOST` is set deliberately, §4.1) → dashboard →
  *Record video* → a label → *Start recording*; *Stop recording*, wait
  for "Finishing the files…" to clear, then *Download video* (and *Download
  sound* when there is a separate `.wav`) and *Delete*. Through the Portal
  the owner sees the same card with *Start* / *Stop* (after re-entering the
  Portal password) and the list, without *Download* or *Delete*: the Portal
  refuses those routes and never carries the files.
- **Limits that stop or refuse a recording** (`SENTRIX_RECORD_*`, doc 7
  §3.4): 10 min each; 2 GB free kept for evidence, snapshots and logs (no start
  below it, and a running recording stops at it); 20 GB for all recordings
  (download and delete before the next start); 1.9 GB per video; a camera gone
  for 5 s. The ffmpeg mux writes the video a second time, so it runs only
  when that copy leaves the 2 GB floor intact; otherwise the sound stays a
  separate `.wav` and the sidecar's `error` says why. Finished recordings are
  deleted after 14 days, checked every hour whether or not anyone opens the
  dashboard.
- **Check:** `python tools/doctor.py` prints ffmpeg, the recordings' count and
  size against the 20 GB cap, and free space against the 2 GB floor.
- **After a crash, a power cut or a failed save** (on Windows a virus scanner
  can hold a file just closed) the `.part` files are finished at once, before
  the next recording, within the hour or at the next start: a save that had
  written its sidecar keeps its own figures; otherwise the sidecar says
  `interrupted` and, with ffmpeg, the video is remuxed and the sound put back
  in. No action is needed.
- **Never commit a recording.** `.gitignore` ignores `*.avi`, `*.mkv`, `*.wav`
  and `rec_*.json` outside the tracked siren sound; keep downloads on team
  machines, with the consent forms of everyone filmed (ROADMAP §15.1).

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Cannot sign in | `SENTRIX_PASSWORD` unset | Set it in `.env`, restart |
| "Too many attempts" | console limiter, 8 per 5 min | Wait, or restart |
| POST answers 403 | missing `X-CSRF-Token` (a script or old page) | Reload the page; scripts must send the header from `<meta name="csrf-token">` |
| Every POST answers 503 | the hardening module failed to load (fail-closed fallback) | Check the startup log |
| No video | wrong camera index | `python tools/doctor.py --probe-camera` reads one frame from the configured index |
| Nobody is ever authorised | no face backend | Check `identity_backend` in `/api/health` |
| Evidence unreadable after restart | `EVIDENCE_AES_KEY` unset | Set a stable key. Earlier evidence is unrecoverable |
| Chain verify fails | bundle modified or deleted | `problems[]` in the response shows the first break |
| Nothing is sent | test mode / switches / allowlist (by design) | `send_guard` in `/api/health`; a SUPPRESSED row in the outbox says why |
| Red banner "no outbound channel" | SOS on but no channel can send | Enable channels and a transport, or accept the LC prompt only |
| Device shows "offline" on the Portal | link down, wrong `SENTRIX_PORTAL_URL`, or no identity | `/api/link` on the device; `tools/doctor.py` link rows |
| Portal sign-in lands back on `/login` | (fixed) session commit after the redirect | Fixed in `640e713` (doc 7 §17) |
| Siren test answers 409 | siren line disarmed (`SENTRIX_SIREN_ARMED=0` or the arming key) | Arm it |
| Siren does nothing | `ConsoleSiren` selected | `siren_is_real` in `/api/health` |
| High TCI in an empty room | should not happen | `doctor` asserts empty-scene TCI < 0.10; the TCI leaves 0 only on confirmed evidence, so read the reason on the dashboard |
| No *Record video* card | signed in as a member or the Desk on the Portal (only the owner and the device admin get it), or the device runs a build from before 2026-10-28 | Sign in as the owner, or on the device's console with the device password (§9 "Recordings and disk"); update the device |
| *Start recording* on the Portal says to re-enter your password | the owner's step-up is older than 5 minutes (a start needs a fresh one; a stop never does) | Re-enter the password on the Portal's *Account* page (`/account`), then press it again |
| No *Download* or *Delete* buttons | the card is open through the Portal (the files never go through it) | Open the device's console on the home Wi-Fi at the address the card names |
| The card says recording is turned off | `SENTRIX_RECORDING_ENABLED=0` | Set it to `1` and restart |
| *Start recording* refused | camera offline or no frame yet; already recording or saving; under 2 GB free; recordings over 20 GB | The message names which; download and delete old recordings |
| A recording has no sound | no microphone delivered sound while it ran, or ffmpeg is missing (the sound is then the separate `.wav`) | Node page "Microphone"; `tools/doctor.py` ffmpeg and microphone rows |
| "Microphone: not connected" with one plugged in | PortAudio has not listed it yet (a new USB microphone is held by the sound server for a few seconds), or it is not an input SENTRIX chooses | Wait up to a minute; `tools/doctor.py` lists the inputs and which one would open; set `SENTRIX_MIC_DEVICE` to its index or name |

---

## 11. Pre-production checklist

- [ ] `SENTRIX_PASSWORD` strong; `SESSION_SECRET` and `EVIDENCE_AES_KEY` set to
      32 random bytes, the evidence key **backed up**
- [ ] Residents enrolled and verified on `/authorized` (adults only; no minors'
      faces in the capstone)
- [ ] `python tools/doctor.py` reports no failures; `python -m pytest` passes
- [ ] Console bound to the LAN (or behind TLS with `SENTRIX_SECURE_COOKIES=1`);
      never port 8000 on the Internet
- [ ] Test mode on; the allowlist holds only team phones and addresses
- [ ] Portal: keys from the environment and backed up, `PORTAL_SECURE_COOKIES=1`
      behind Caddy, only 80/443 reachable (`deploy/portal/check_ports.sh`), a
      passing restore test
- [ ] Device identity provisioned, claimed, link online
- [ ] `GET /api/evidence/verify` returns `ok: true`
- [ ] Gates of doc 7 §15 item 7 and `CLAUDE.md` M9 measured

**Known gap.** There is no fitted fusion model: the TCI is the strongest
confirmed per-class evidence, a published rule. It is not calibrated against
real events; treat levels 2 and 3 as advisory until real labelled sessions
exist (M6, M8).
