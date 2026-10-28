# SENTRIX

**Edge-first home threat detection with an owner-approved SOS flow**

> **Capstone Project CPG-299** · Computer Science and Engineering, Thapar Institute of
> Engineering and Technology, Patiala · Mentor: Dr. Ashutosh Mishra

SENTRIX is two programs:

* **The device** (`python -m sentrix`). It runs at home, on a laptop today and on a
  Raspberry Pi 5 later. It reads a camera (and optionally a microphone), runs the
  perception models, turns their outputs into a **Threat Confidence Index (TCI,
  0–1)** and a five-level alarm, and keeps the incident records, the encrypted
  evidence and the face gallery. It serves the **Local Console (LC)**, the
  dashboard on `http://127.0.0.1:8000`. It also runs the **SOS engine**, the
  alert channels and the outbound link to the Portal.
* **The Portal** (`python -m sentrix_portal`). It runs in the cloud (planned:
  Oracle Always Free, India). It holds accounts, onboarding, the household and
  its emergency contacts, the SOS requests, the **SENTRIX Desk** (a simulated
  responder console) and the company admin console. It shows the device's own
  console pages through a tunnel, so the owner sees the same dashboard from any
  browser.

Detection stays on the device. The Portal stores profiles, metadata and one
consented snapshot per SOS request; faces, evidence and video stay on the device
and are only viewed on demand through the tunnel.

**Test mode is the default and the only mode this semester.** Nothing is sent to a
real person unless two switches are on, and even then only to an allowlist (see
[Configuration](#configuration)). SENTRIX never contacts 112 itself.

---

## Contents

1. [The product flow](#the-product-flow)
2. [Architecture](#architecture)
3. [Quickstart](#quickstart)
4. [Configuration](#configuration)
5. [Tests and evaluation](#tests-and-evaluation)
6. [Repository structure](#repository-structure)
7. [Documentation](#documentation)
8. [Status and limits](#status-and-limits)
9. [Team](#team)

---

## The product flow

| Step | Who | Where |
|---|---|---|
| 1. Provision a device: id, serial, claim code, label | company | `tools/provision_device.py` |
| 2. Create the customer; print the welcome letter (username, activation code, device serial and claim code) | company admin | Portal admin console |
| 3. Install the device; activate the account with the code and choose a password | customer | Portal `/activate` |
| 4. Onboarding wizard, 7 steps: profile, address, household, emergency contacts (each confirms by link), claim the device, alerts + SOS PIN + window, consent and SOS preview | customer | Portal `/onboarding` |
| 5. Daily use: the device's dashboard, live view, events, evidence, access (faces), SOS, node | customer | LC on the LAN, or the Portal through `/d/{device_id}/...` |
| 6. A **confirmed L3** of an alarm class (weapon, fire, or a fight confirmed by video *and* time-aligned aggression audio) opens an **assistance request**, pre-filled from the profile. An unrecognised person is an L2 only (no request) | device | SOS engine |
| 7. "Do you need assistance?" on the Portal, the LC, Telegram and e-mail (SMS only when offline or unseen for 10 min); window 30/45/60 min | owner | |
| 8. Approve → the Desk and the confirmed contacts get the dispatch. False alarm → PIN. No answer → **EXPIRED** ("unanswered", never "safe"); "Request help now" opens a linked request | owner | |
| 9. The Desk acknowledges and resolves; the incident verdict is recorded | Desk operator | Portal `/desk` |

Safety defaults (on unless the household turns them off): a corroborated L4/L5,
a confirmed fire (at T+2 min), or the device going offline during an open L3+
alarm (weapon, fire, a corroborated fight) alerts the confirmed contacts while
the owner is still being asked. An unrecognised person never opens an SOS, not
even when the device goes offline: the owner alone gets a notice. A single-source weapon L3 goes to the owner only. A duress
PIN "declines" on screen but alerts the contacts. The full rules are decisions
D8–D14 in [`CLAUDE.md`](CLAUDE.md) §6 and the state machine in
[`docs/7_PORTAL_AND_SOS.md`](docs/7_PORTAL_AND_SOS.md) §7.

---

## Architecture

```
 HOME (device, one process: python -m sentrix)         CLOUD (python -m sentrix_portal)
+--------------------------------------------------+   +-----------------------------------+
| Camera: webcam | file | RTSP | Pi camera | demo  |   | Accounts, activation, TOTP        |
|   |                                               |   | Onboarding wizard, household,     |
|   v  capture thread (camera rate)                 |   |   contacts (consent links)        |
| Pipeline (tick mode ~6 Hz, or 4 s clip mode)      |   | SOS pages, Desk, admin console    |
|   Detector: YOLO person (+ByteTrack), weapon,     |   | Privacy: export, erasure,         |
|     fire heads                                    |   |   retention worker                |
|   Scene classifier: DINOv2 ViT-B/14, 5 classes    |   | Channel router: Telegram, e-mail  |
|   Identity: InsightFace, per track, own thread    |   | Offline mirror: incidents, SOS    |
|   Audio (optional), motion, behaviour             |   |   requests, dispatch, heartbeat   |
|   -> evidence rules (k-of-n, person gate,         |   | Cloud SOS authority while the     |
|      corroboration) -> TCI -> level policy        |   |   device is offline               |
|   -> incidents, event log, evidence vault         |   |                                   |
|   -> SOS trigger (idempotent, per incident)       |   | Link hub  /device/v1/link         |
| SOS engine: machine, frozen policy, PINs, sweeper |   | Tunnel    /d/{device_id}/...      |
| Channels: router + outbox; modem SMS, Telegram,   |WSS|   (replayed in-process into the   |
|   e-mail, Twilio (all behind the send guard)      |==>|    device's own LC app; /video    |
| Link client: outbox, heartbeat, tunnel executor,  |out|    and /ws/threat relayed)        |
|   device-enforced command rules                   |   |                                   |
| LC web app (FastAPI) on :8000                     |   | SQLite (dev) / Postgres (deploy)  |
| SQLite var/sentrix.db, vault, faces               |   +-----------------------------------+
| Pi 5: siren relay, LEDs, PIR, door, mains sense,  |
|   node telemetry, watchdog, 4G modem              |
+--------------------------------------------------+
```

The device only opens outbound connections. Every page the Portal shows under
`/d/{device_id}/` is served by the device's own code, so the LAN console and the
remote one cannot drift apart. The device does not trust the Portal: it checks
consent flags for video and media, allows SMS only to numbers in the current
profile (30 a day), rate-limits siren tests, and has no command that disables
the siren or the evidence.

### How a detection becomes an alarm

The first live run on a laptop webcam produced about 171 false weapon alarms per
hour (CLAUDE.md §5). The rules in `sentrix/fusion/evidence.py` now stand between
a model score and the alarm:

| Rule | Default |
|---|---|
| Confirmation | a source must cross its threshold on 2 of its last 3 fresh evaluations |
| Person gate | weapon votes count only with a person seen in the last 2 s |
| Thresholds | the checkpoint's, raised to at least weapon 0.50 / fire 0.45; a per-room calibration may replace them |
| Modalities | video detector (YOLO heads), video classifier (scene / violence model: one modality), audio (only when its 4 s window overlaps the frame within 1.5 s; otherwise *unavailable*, never "calm") |
| One visual modality confirmed | L3 (snapshot and evidence; SOS prompt) |
| Two modalities confirmed for one class (detector + scene, or video + aligned audio, e.g. a pistol box and a gunshot) | L4; L5 when the weaker is well above threshold, or when all three agree |
| Audio alone (gunshot, smoke alarm) | at most L2; a scream alone is never a weapon |
| Fight (assault) | L2 (experimental) unless the video fight **and** aligned aggression audio (shouting, screaming) agree with 2+ people and motion: then L3. Aligned laughter or very calm talk (banter) keeps it at L2 - shown, never hidden; TV, music or radio make the audio neutral; the device's own siren is never heard as the room. Unvalidated until room recordings exist |
| Robbery, stick, smoke | experimental: logged and shown, never above L2 |
| Unknown person, PIR, door | presence context: an unrecognised person (3 s armed, 10 s at home), or armed-mode sensor activity with nobody recognised, is an **L2 only** in both modes |

Every tick records what each input did for the leading class (video detector,
scene model, audio, motion, people, identity, sensors) in the event log and on
the dashboard's Threat Analysis card (decision D15).

| Level | Side effects |
|---|---|
| 1 SECURE | log |
| 2 SUSPICIOUS | incident opened (log and dashboard) |
| 3 ELEVATED | snapshot, AES-256-GCM evidence bundle, SOS request for alarm classes |
| 4 HIGH | dispatch package (legacy SMS only when no SOS request owns the incident) |
| 5 CRITICAL | siren (legacy voice call only when no SOS request owns the incident) |

The TCI is the strongest confirmed class evidence. It is not smoothed: the
number on the gauge, in the log and on the incident is the number the level was
decided on. The synthetic XGBoost booster (`models/tci_xgboost.json`) is
retired.

---

## Quickstart

Runs on a laptop CPU: Windows, macOS or Linux, Python 3.10–3.14. Installing,
testing and evaluating need no GPU, camera or microphone: the repository ships
the models, the custom dataset and its ten source videos.

### 1. Get the code

```bash
git clone https://github.com/mehulperimal14/Sentrix.git
cd Sentrix
```

On Windows, clone to a short path such as `C:\src\Sentrix`; some files inside
the virtual environment are deeply nested.

### 2. Start the device

| Windows (PowerShell or cmd) | macOS / Linux |
|---|---|
| `.\run.bat` | `./run.sh` |

Both scripts create `.venv`, install `requirements.txt` (several minutes the
first time, then only when it changes), create `.env` with a generated console
password (**printed once**), session secret and evidence key, run
`tools/doctor.py` and start `python -m sentrix`. Open
**http://127.0.0.1:8000** and sign in with that password (`SENTRIX_PASSWORD` in
`.env`).

The first start downloads about 600 MB of public weights once: the DINOv2 base
of the scene classifier (Hugging Face cache) and InsightFace `buffalo_l`
(`~/.insightface`).

* **Linux:** `sudo apt install python3-venv libportaudio2 libgl1 libglib2.0-0`
  (and `ffmpeg`, optional on any OS: recordings keep their sound inside the video).
  On a CPU-only machine install the CPU build of PyTorch first
  (`pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu`).
* **NVIDIA GPU:** install the CUDA build of PyTorch before `requirements.txt`; the
  scene model moves to the GPU automatically.
* **Raspberry Pi 5:** `run.sh` detects the Pi and installs `requirements-pi.txt`
  into a venv with system site packages. Bring-up order, `install_pi.sh` and the
  relay test with an LED are in [`deploy/pi5/README.md`](deploy/pi5/README.md).

Manual install instead of the scripts:

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1    macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python tools/setup_env.py        # creates .env, prints the generated password
python -m sentrix                # --host / --port; --help starts nothing
```

### 3. No webcam? Use a bundled video

Add to `.env`:

```bash
CAMERA_SOURCES=training/datasets/sentrix_custom/videos/gun.mov   # loops; any video path works
SENTRIX_AUDIO=0         # keep the microphone closed
SENTRIX_SIREN=console   # print the siren instead of sounding it
```

### 4. Start the Portal (optional)

```bash
python -m sentrix_portal          # http://127.0.0.1:8100 (PORTAL_HOST / PORTAL_PORT)
python tools/provision_device.py admin --username admin    # first company admin + activation code
python tools/provision_device.py device --label label.html # a device identity, claim code, label
```

The Portal runs in test mode with SQLite in `var/portal/` by default. Its
background workers (hub watcher, cloud SOS timers, notification worker,
retention) run only with `PORTAL_WORKERS=1`. To link a device to it, the device
needs `SENTRIX_LINK_ENABLED=1`, `SENTRIX_PORTAL_URL` and the provisioned
identity (`SENTRIX_DEVICE_IDENTITY_DIR`); the SOS engine and channels need
`SENTRIX_SOS_ENABLED=1` and `SENTRIX_CHANNELS_ENABLED=1`.

### 5. The demo: Portal and device together

The demo tools set all of this up in a separate directory, in test mode, with
every live switch forced off:

```bash
python tools/demo_reset.py                 # demo world in var/demo (admin, Desk, "Riya Sharma (demo)",
                                           # confirmed contact, claimed device); logins printed once
python tools/demo_run.py                   # start Portal (portal.localhost:8100) + device (127.0.0.1:8000)
python tools/demo_run.py scene gun         # play the held-out handgun clip through the demo reel camera
python tools/demo_run.py scene fire        # held-out flame clip
python tools/demo_run.py trigger weapon    # disclosed scripted trigger (text says SIMULATED DETECTION)
python tools/demo_run.py close             # close the open incident
python tools/demo_run.py clips             # the scenes and their measured reliability
```

`demo_reset.py --live-steps 3` leaves the last wizard steps for the stage,
`--activation` leaves the customer to activate live, `--window` sets the demo SOS
window (default 90 s, test mode only). The demo camera is
`sentrix/io/demo_reel.py`: idle footage (vid07), then `gun.mov` or `4.mov` on
request. It refuses the test video `wa02`. Messages appear as SUPPRESSED rows in
the Portal's outbox, on the Desk and on the LC; nothing leaves the laptop.

### 6. Enrol faces

LC **Access** page → *Capture from camera* (uses the device's own camera frame,
with quality checks) or *Upload photos*. Until someone is enrolled everyone is
"unknown"; in home mode that is only logged.

### 7. Record training videos

The models need footage from the room they guard, filmed by the camera that
will guard it (ROADMAP.md §15). The device records it itself (decision D20):

1. Open the device's console and sign in with the device password
   (`SENTRIX_PASSWORD`). On the **Pi**, from a computer on the same Wi-Fi:
   `http://<device-ip>:8000` (the Pi profile binds the LAN, and
   `deploy/pi5/ufw.sh` limits the port to your subnet). On a **laptop**
   running SENTRIX, open `http://127.0.0.1:8000` on that laptop: the console
   binds loopback only by default (`SENTRIX_HOST=127.0.0.1`), and opening it
   to the network needs `SENTRIX_HOST` set deliberately plus a firewall rule
   (`docs/4_DEPLOYMENT_AND_RUNBOOK.md` §4.1). The owner can also start and
   stop a recording on the Portal's dashboard (after re-entering the Portal
   password), but downloads and deletes happen here on the local network
   only: the Portal never carries the files.
2. Dashboard → **Record video** → a label (optional) → **Start
   recording**. A red **REC** badge shows on the live video for everyone
   watching (it is not in the file). Get the consent of everyone filmed first.
3. **Stop recording**, wait for "Finishing the files…" to clear, then
   **Download video** and **Delete**. Download on your laptop: in a browser
   on the Pi itself the Download buttons are off (the video would land on the
   Pi's SD card) and the card shows the address to open on the laptop
   (`SENTRIX_RECORD_DOWNLOAD_ON_DEVICE`).

Files are **MJPEG AVI** at the camera's resolution and frame rate (VLC plays
them), with the microphone's sound (16 kHz mono) inside when a microphone was
connected and `ffmpeg` is installed; without ffmpeg the sound is a separate
`.wav` (**Download sound**). A USB microphone works in any port, plugged in
before or after start-up. The device stops a recording after 10 minutes,
keeps 2 GB of disk free, holds at most 20 GB of recordings and deletes them
after 14 days. Settings (in `.env`, documented in `.env.example`):

```bash
SENTRIX_RECORDING_ENABLED=1        # 0 turns recording off (the card says so)
SENTRIX_RECORD_MAX_SECONDS=600     # one recording stops by itself after this
SENTRIX_RECORD_MIN_FREE_MB=2048    # free space kept for evidence and logs
SENTRIX_RECORD_MAX_TOTAL_MB=20480  # all recordings together
SENTRIX_RECORD_KEEP_DAYS=14        # 0 keeps them until deleted
SENTRIX_RECORD_DOWNLOAD_ON_DEVICE=auto  # a browser on the Pi itself cannot download
SENTRIX_MIC_DEVICE=                # empty = automatic; or a device index or name part
```

Recordings live in `var/recordings/` on the device and are never committed
(`.gitignore`). `python tools/doctor.py` shows ffmpeg, the recordings' size and
free space, and the microphones the device can see (it opens none).

---

## Configuration

`.env.example` documents every setting; `tools/setup_env.py` creates `.env` from
it. Algorithmic constants (thresholds, windows, dwell times) live in
`sentrix/config.py`, in seconds rather than frames.

### Test mode and the two switches (decision D13)

Every outbound message passes `sentrix/alerting/guard.py`, which re-reads the
environment on each message:

| Rule | Effect |
|---|---|
| Emergency number (112, 100, 101, 102, 108, …) | always refused |
| `SENTRIX_LIVE_ALERTS` is not 1 | refused; the message is recorded as SUPPRESSED |
| The channel's own switch is not 1 (`SENTRIX_MODEM_LIVE`, `SENTRIX_TELEGRAM_LIVE`, `SENTRIX_EMAIL_LIVE`, `SENTRIX_TWILIO_ENABLED`) | refused |
| Test mode on (`SENTRIX_TEST_MODE`, on unless explicitly `0`/`false`/`no`/`off`) and the recipient is not in `SENTRIX_TEST_RECIPIENTS` | refused |
| Otherwise | sent; in test mode the text starts `[SENTRIX DEMO – TEST, NOT A REAL EMERGENCY]` |

In test mode no `tel:112` link is rendered; the Desk number is shown instead.
The legacy Twilio SMS/call path does nothing unless `SENTRIX_TWILIO_ENABLED=1`
and `SENTRIX_LIVE_ALERTS=1` (Twilio keys alone send nothing). The Portal uses the
same guard with `PORTAL_TEST_MODE`.

### Services are opt-in

| Flag | Turns on |
|---|---|
| `SENTRIX_SOS_ENABLED` | SOS engine and sweeper |
| `SENTRIX_CHANNELS_ENABLED`, `SENTRIX_MODEM_ENABLED` | channel router and outbox; the USB 4G modem |
| `SENTRIX_LINK_ENABLED` | outbound link to the Portal |
| `SENTRIX_GPIO_ENABLED`, `SENTRIX_NODE_ENABLED` | Pi GPIO (LEDs, PIR, door, mains); node telemetry |
| `SENTRIX_ARMED` | armed mode at start (the mode can also be set on the LC) |
| `SENTRIX_PIPELINE_MODE=clip`, `SENTRIX_FAST_LANE`, `SENTRIX_RUNTIME` | 4 s clip engine, its fast lane, torch/onnx/ncnn runtime |
| `PORTAL_WORKERS` | the Portal's background workers |

With none of them set, `python -m sentrix` behaves like the stand-alone console.

---

## Tests and evaluation

```bash
python -m pytest                     # 1647 tests, ~6.5 min on the laptop CPU; no camera, mic, network or GPU
python -m pytest -o addopts=""       # same, with the summary line
python tools/doctor.py               # pre-flight report; never opens a camera unless --probe-camera
python tools/bench_live.py           # live loop on a bundled video: video fps, AI Hz, latency
```

`tests/conftest.py` runs before any import: test mode on, every live switch
off, provider keys blanked, every service flag off, the device `var/` tree and the
Portal database in a temp directory, webcam and microphone blocked, and a socket
guard that allows loopback only. `tests/test_e2e_product.py` runs the whole
product in one process (Portal, device console with a fake pipeline, the real
SOS engine, the real link client into the Portal hub, fake Telegram/e-mail/modem
transports): 13 scenarios from provisioning to a closed request, decline,
expiry, safety defaults, duress, device offline, internet down, XSS and
cross-household isolation.

The custom-dataset evaluation (bundled frames and videos, side effects
disabled):

```bash
python tools/eval_custom_frames.py --splits train,val,test --tag local    # per image
python tools/replay_custom_videos.py --videos all --tag local             # 10 videos end to end
python tools/replay_custom_videos.py --videos all --mode clip --tag local_clip   # clip engine
python tools/custom_eval_table.py custom local                            # compare with a published run
python tools/gate_report.py --replay training/runs/custom_eval/local/replay_all.json
python tools/replay_decisions.py var/events/<session>.jsonl               # current rules on a recorded session
```

Your runs land in `training/runs/custom_eval/<tag>/` (ignored by git). The
published runs are `baseline`, `finetuned`, `custom`, `m1`, `m5` and `clip`.
Never tune or select on the test video `wa02`.

---

## Repository structure

| Folder | What it holds |
|---|---|
| `sentrix/` | The device: `perception/` (detector, scene, identity, audio, behaviour, motion, re-id), `fusion/` (evidence rules, TCI, level policy), `pipeline/` (engine, 4 s clip engine, fast lane, incidents, event log, state), `runtime/` (torch/onnx/ncnn backends), `sos/` (machine, policy, PINs, templates, sweeper, service), `alerting/` (send guard, dispatch, channels: router, modem, Telegram, e-mail, Twilio), `link/` (client, protocol, tunnel, command rules), `io/` (cameras, demo reel, siren, GPIO, node, Wi-Fi, watchdog), `storage/` (SQLite), `evidence/` (vault), `web/` (Local Console), `evaluation/` |
| `sentrix_portal/` | The Portal: `routes/` (auth, onboarding, home, household, contacts, consent, settings, SOS, Desk, admin, privacy, link hub, device proxy, hooks), `services/` (accounts, hub, SOS, notify, retention), templates and static files |
| `models/` | Weights the runtime loads: `v4_custom/` (default: DINOv2 scene classifier, YOLO11n weapon and fire heads), `v3_real/`, `v5_slim/` (slim scene heads, experimental), `yolov8n.pt`, and provenance checkpoints |
| `training/` | `custom/` trainers, `datasets/sentrix_custom/` (765 frames, 10 videos, ground truth), `notebooks/`, `scripts/` (legacy trainers), `runs/` (the reports the docs cite) |
| `notebooks/` | TCI analysis: feature contract, calibration, thresholds |
| `tools/` | doctor, setup_env, provision_device, demo_reset, demo_run, benchmarks (`bench_live`, `bench_clip`), replay and evaluation, gate report, model export, `portal_pg_check` |
| `tests/` | The test suite; `tests/e2e/` holds the product-flow world |
| `deploy/` | `pi5/` (install script, systemd units, watchdog, NetworkManager failover, firewall, `pi.env`), `portal/` (Dockerfile, compose with Postgres on an internal network, Caddy, backup and restore scripts, runbook) |
| `docs/` | Numbered specifications `1_`–`8_`, `capstone/` (proposal, mid-semester report, poster), `assets/` (diagrams) |
| `var/` | Runtime output: database, events, evidence, snapshots, faces. Gitignored, never web-served |

---

## Documentation

| Document | Contents |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Project memory: facts, git history, model scores, the live diagnosis, decisions D1–D14 and milestone status |
| [`ROADMAP.md`](ROADMAP.md) | Everything left before a working prototype, in order (cloud, Pi bring-up, Gate A, rehearsals, gates, demo), and the guide for training on more custom data |
| [`docs/README.md`](docs/README.md) | Index of the numbered docs: which are current and which sections are historical |
| [`docs/1_SYSTEM_ARCHITECTURE.md`](docs/1_SYSTEM_ARCHITECTURE.md) | Device architecture and data flow |
| [`docs/2_MODEL_BENCHMARKS_AND_KPIS.md`](docs/2_MODEL_BENCHMARKS_AND_KPIS.md) | Model training results |
| [`docs/3_HARDWARE_SPECIFICATION_AND_BOM.md`](docs/3_HARDWARE_SPECIFICATION_AND_BOM.md) | Hardware and bill of materials |
| [`docs/4_DEPLOYMENT_AND_RUNBOOK.md`](docs/4_DEPLOYMENT_AND_RUNBOOK.md) | Deployment and operations |
| [`docs/5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md`](docs/5_FUTURE_ADVANCEMENTS_AND_CLOUD_DEPLOYMENT.md) | Remaining work in short, and model research notes (the step-by-step plan is `ROADMAP.md`) |
| [`docs/6_CUSTOM_DATASET_EVALUATION.md`](docs/6_CUSTOM_DATASET_EVALUATION.md) | Custom-dataset protocol and results |
| [`docs/7_PORTAL_AND_SOS.md`](docs/7_PORTAL_AND_SOS.md) | Portal, onboarding, link, tunnel and SOS contract; §17 the live smoke run |
| [`docs/8_RASPBERRY_PI5_INTEGRATION.md`](docs/8_RASPBERRY_PI5_INTEGRATION.md) | Raspberry Pi 5 plan, clip engine, pin table |
| [`deploy/pi5/README.md`](deploy/pi5/README.md) | Pi bring-up and deployment |
| [`deploy/portal/README.md`](deploy/portal/README.md) | Portal deployment (Oracle Always Free; Render + Supabase fallback) |
| [`THIRD_PARTY.md`](THIRD_PARTY.md) | Licences of models, data and services SENTRIX uses |

---

## Status and limits

Everything below ran on the maintainer laptop (Windows 11, Python 3.14, CPU
torch). **Nothing has run on a Raspberry Pi, a real modem, a real Telegram bot,
a real e-mail account or a live Postgres yet.**

| Area | State | Evidence |
|---|---|---|
| Device pipeline, LC, records | working on the laptop | `tools/bench_live.py` on vid07 with four faces: preview 29.7 fps, AI 5.6 Hz (CLAUDE.md M2) |
| Portal + device as two processes | working on the laptop | docs/7 §17: provisioning → activation → wizard → claim → tunnel → real detection → approve → Desk → closed; decline, expiry, Portal outage |
| Detection to SOS request | working on bundled clips | docs/7 §17: PENDING weapon request 8.0–9.8 s after `scene gun`; fire 11.6–11.9 s after `scene fire` |
| Remote view through the tunnel | working, capped | docs/7 §17: MJPEG 29.9 fps direct, 13.6 fps tunnelled (cap 15) |
| Telegram, e-mail, modem SMS, Twilio | code and fake-transport tests only | no provider accounts or modem used |
| Raspberry Pi 5 hardware | code and mock-pin tests only | bench checks still to do (deploy/pi5/README.md) |
| 4 s clip engine | replay meets Gate A's detection criterion; too slow on the laptop | `training/runs/custom_eval/clip/`: weapon L3 9/12 with the fast lane, fire 3/3, 0 false onsets; ONNX + fast lane compute p95 2145 ms vs a 1.6 s budget |
| Training recordings on the dashboard (D20), a USB microphone in any port (D21) | working on the laptop | `tests/test_recorder.py` (real OpenCV and ffmpeg round trips), `tests/test_recording_*.py`; the card driven in a browser against a synthetic camera; the microphone chooser against a fake sound library only. Not yet run on the Pi or with a real microphone |
| Slim scene heads (`models/v5_slim/`) | not usable for alarms | `training/runs/custom/slim/REPORT.md`: LOVO macro-F1 0.229 (MobileNetV3-L) vs 0.440 (DINOv2) |
| Portal deployment | written, not run | `deploy/portal/` checked by `tests/test_deploy_portal.py`; Docker not run; no Alembic baseline |

Limits that matter for any claim:

* **No model is validated for a new room.** The custom dataset has two rooms and
  341 s of labelled footage. On its 10 videos (partly in-sample) the committed
  levels show 0 false onsets over 3.7 normal minutes — far too little normal
  footage to claim a rate — and catch weapon 9/12 and fire 3/3 at L3
  (`training/runs/custom_eval/m5/`). Gates G2 (episodes within 2 s) and G4
  (onset → L3 p95 ≤ 2 s) fail. Deployment-room recordings (M6) come first.
* **Assault and robbery cannot be validated** (2 videos / 1 event). They stay
  experimental, capped at L2.
* **No fitted fusion model.** The TCI is a published rule over confirmed
  evidence until real session logs exist (M8).
* **Darkness and faces closer than 1 m** are out of scope for an RGB webcam.
* **The sound model is general, not room-tested.** PANNs CNN10 (AudioSet,
  25 MB, ≈ 40 ms per 4 s window on the laptop CPU, its own onnxruntime threads) maps its labels to seven cues
  by a published rule; the cue thresholds are a priori, and the custom videos'
  soundtracks are phone recordings of staged scenes: the staged assault (vid05)
  sounds like calm talk (benign 0.91, aggression < 0.01), so no fight in the
  custom data can reach L3. Fight-vs-banter needs recordings of play-fighting,
  real staged fights, laughter and shouting (ROADMAP.md §15.1).
* **Training-corpus metrics are optimistic.** The v3 models' own validation
  splits share source videos or near-duplicate images with training
  (`tools/audit_splits.py`; CLAUDE.md §4.1).
* **Evidence is tamper-evident, not "court-admissible".** AES-256-GCM with an HMAC
  hash-chained manifest shows a change; admissibility is a legal question.
* Release gates P1–P8 (Portal and SOS) and M9 G0–G5 (detection) are defined but
  not yet measured on a final build (CLAUDE.md §6).

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

## License

SENTRIX's own code is MIT (see `LICENSE`). Models, data and services it uses
carry their own terms; see [`THIRD_PARTY.md`](THIRD_PARTY.md).
