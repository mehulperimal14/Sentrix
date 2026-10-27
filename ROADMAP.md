# SENTRIX roadmap: from integrated software to a working prototype

This file lists everything still left to do before SENTRIX is a real, working
prototype on a Raspberry Pi 5, connected to the cloud Portal and shown to the
teachers. It is written for the five-person team. Read it top to bottom: the
sections are in the order the work should happen. The last section is the guide
for training the video models on more custom data after the prototype.

**What it is based on**

* The reviewed plan `SENTRIX_Prototype_Roadmap_v2.pdf` (tiers 0–2, weeks
  W0–W8, gates P1–P8, Gate A, the demo script), minus what is now built.
* The code on branch `sentrix/portal` (head `e7ca89e` when this was written).
* `docs/7_PORTAL_AND_SOS.md` §17 (the live smoke run of 2026-10-15),
  `docs/8_RASPBERRY_PI5_INTEGRATION.md`, `deploy/pi5/README.md`,
  `deploy/portal/README.md`, `CLAUDE.md` M6–M10, and the reports under
  `training/runs/`.

**How to read the status column**

| Mark | Meaning |
|---|---|
| **Done** | Built and tested. The evidence column says where. |
| **Partly** | Built, but not yet run on real hardware, real accounts or the final build. |
| **Not started** | Nothing in the repository yet. |

Effort is in person-days and is our own estimate (*est.*), not a measurement.
Add 30–50 % contingency, as the v2 plan says. Prices are from the v2 plan,
checked on 14 Oct 2026; **≈** marks a price that could not be confirmed. Check
every price again before you order.

**Rules that apply to every step** (from `CLAUDE.md` and the accepted
decisions D8–D14)

* Test mode is the default and the only mode this semester
  (`SENTRIX_TEST_MODE=1`). Nothing is sent unless `SENTRIX_LIVE_ALERTS=1`
  **and** the channel's own switch are both on, and even then only to numbers
  and addresses in `SENTRIX_TEST_RECIPIENTS`. Emergency numbers are always
  refused.
* No test, replay, benchmark or evaluation may send a real alert. `.env` may
  hold live Twilio keys.
* Never tune or choose anything on the test video `wa02`, or later on the test
  session S4.
* `var/` is private runtime data. Never commit it, never copy it to the cloud.
* Never open a webcam or microphone in a diagnostic or a test.
* Commit on the working branch; never push unless the team decides to.

---

## 1. Where we are

The full product runs on one laptop. Hardware, real accounts and real
deployment-room data are what is missing.

| Area | Status | Evidence |
|---|---|---|
| Detection pipeline (per-frame mode), confirmed evidence, levels, incidents, evidence vault | Done | `CLAUDE.md` M1–M5; `training/runs/custom_eval/m5/` |
| 4 s clip engine, fast lane, ONNX/NCNN runtime backends, model export | Partly | `sentrix/pipeline/clip.py`, `fastlane.py`, `sentrix/runtime/`, `tools/export_models.py`. ONNX tested; NCNN only against a fake module (not installed) |
| Device Local Console (LC) with CSP, CSRF, base-aware URLs, new SOS / Node / Wi-Fi setup pages | Done | `sentrix/web/`; `tests/test_web_*.py` |
| SOS engine (states, safety defaults, PINs, duress, expiry, "Request help now") | Done | `sentrix/sos/`; `tests/test_sos_*.py`; docs/7 §17 |
| Channel router and outbox; Telegram, e-mail (Brevo/Resend), modem SMS, Twilio wrapper | Partly | `sentrix/alerting/channels/`; tested with fake transports only. No real bot, e-mail account or modem used yet |
| Device link, tunnel, Portal hub, `/d/{id}` proxy, video and state relay, SOS mirror | Partly | `sentrix/link/`, `sentrix_portal/`; 100 forced disconnects lose 0 events; two-process loopback run on the laptop (docs/7 §17). Not yet over TLS, the Internet or 4G |
| Portal accounts, activation codes, TOTP for admin and Desk, admin console, 7-step wizard, contacts, privacy, retention | Done (laptop) | `sentrix_portal/`; `tests/test_acct_*.py`; docs/7 §17 |
| SENTRIX Desk and contact pages | Done (laptop) | `sentrix_portal/routes/desk.py`, `contacts.py`; docs/7 §17 |
| Raspberry Pi 5 hardware I/O (siren, GPIO, LEDs, PIR, door, mains sense, Picamera2, nmcli, watchdog) | Partly | `sentrix/io/`, `deploy/pi5/`; tested against fake pins, a fake `picamera2` and a fake `vcgencmd`. **Nothing has run on a real Pi** |
| Portal deployment files (Docker, Caddy, Postgres, backups) | Partly | `deploy/portal/`; checked by `tests/test_deploy_portal.py`. Docker never run; no live Postgres run |
| Demo tools (`demo_reset.py`, `demo_run.py`, demo reel camera) | Done (laptop) | docs/7 §17 |
| End-to-end product tests | Done | `tests/test_e2e_product.py` (13 scenarios, commit `7aca186`) |
| Slim scene model for the Pi | Done, **failed its bar** | `training/runs/custom/slim/REPORT.md`: best slim LOVO macro-F1 0.229 vs DINOv2 0.440; DINOv2 stays |
| Training recordings from the dashboard (D20: raw video + the microphone's sound, downloaded on the LAN) and a USB microphone in any port (D21) | Done (laptop) | `sentrix/io/recorder.py`, `sentrix/web/recording_routes.py`, `sentrix/perception/audio.py`; `tests/test_recorder.py`, `tests/test_recording_*.py`, `tests/test_mic_hotplug.py`. Not yet run on the Pi or with a real microphone |
| Deployment-room data (M6), retrained models (M7), fitted fusion (M8), gates on S4 (M9) | Not started (the recording mode exists) | `CLAUDE.md` M6–M9 |

The test suite collects 1,647 tests (`python -m pytest --collect-only -q` on
`e7ca89e`).

### Measured numbers you may quote

Only these, with their source:

| Number | Value | Source |
|---|---|---|
| False onsets, committed level, 10 custom videos (per-frame mode) | 0 at every level over 3.7 normal minutes (95 % bound still ≤ 60/h) | `training/runs/custom_eval/m5/replay_all.json`, `CLAUDE.md` M5 |
| Weapon / fire episodes caught at L3 (per-frame mode) | 9/12, 3/3 | same |
| Clip mode + fast lane, L3 catches | weapon 9/12, fire 3/3, 0 false onsets | `training/runs/custom_eval/clip/replay_all_fastlane.json` |
| Clip compute on the laptop at 4 threads, ONNX + fast lane | p50 1060 ms, p95 2145 ms (budget 1.6 s) | `training/runs/custom_eval/clip/bench_clip_onnx_fastlane.json` |
| Live preview / AI rate, laptop, per-frame mode | 29.7 fps / 5.6 Hz | `training/runs/custom_eval/m5/bench_live.json`, `CLAUDE.md` M2 |
| Gates G0–G5 on the 10 videos (partly in-sample) | G1 PASS, G2 FAIL (weapon 8/12 within 2 s), G4 FAIL (onset → L3 p95 4.0 s), G0/G3/G5 NOT RUN | `training/runs/custom_eval/m5/gates.json` |
| Laptop smoke run: detection → PENDING request; approve → device DISPATCHED; MJPEG through the tunnel | 8.0–9.8 s after the scene command; 0.01–0.28 s; 13.6 fps (cap 15) | docs/7 §17 |

All of these come from the laptop. Not one number has been measured on a
Raspberry Pi.

---

## 2. Status of the v2 plan, item by item

### 2.1 Tiers

| Tier | Item | Status | Evidence / what is left |
|---|---|---|---|
| 0 | Admin creates customer, welcome letter, activation code | Done | docs/7 §17 row 1–2 |
| 0 | Activation, own password, 7-step wizard, contacts with confirmation, 6-digit PIN, window | Done | docs/7 §17 |
| 0 | Claim the device by code or QR | Partly | Code claim tested. QR on the printed label is generated (`tools/provision_device.py --label`), not yet scanned with a phone |
| 0 | Portal tunnel to the device dashboard | Partly | Laptop loopback only. Needs HTTPS, the Internet and 4G |
| 0 | L3 opens an SOS with countdown on the Portal and the LC | Done | docs/7 §17 |
| 0 | Telegram to the owner and contacts | Partly | Fake transport only. No bot token yet |
| 0 | Approve, decline with PIN, expiry, safety defaults | Done | docs/7 §17; `tests/test_e2e_product.py` |
| 0 | Minimal Desk (queue, acknowledge, resolve) | Done | docs/7 §17 |
| 0 | SOS preview, per-request audit trail, test mode | Done | `tests/test_e2e_product.py` |
| 1 | Modem SMS out (offline prompt, SOS to contacts and Desk) | Partly | Driver tested against a fake serial port. No modem bought |
| 1 | Offline approval on the LC | Done | docs/7 §17 (Portal killed, LC approved) |
| 1 | E-mail channel | Partly | Fake transport and SUPPRESSED live run. No Brevo/Resend account |
| 1 | Remote viewer limits and adaptive video rate | Partly | 13.6 fps on loopback. Not measured on 4G (C7: ≥ 5 fps, target 10, p95 ≤ 1.5 s) |
| 1 | Node page | Partly | Built; readings from fakes only |
| 2 | Inbound SMS parsing, web push / PWA, owner TOTP, Hindi templates, general merge, automated export/erasure, WebRTC | Not started (by design) | Designed only. A read API for inbound SMS exists (`AT+CMGL`); export and erasure exist as manual actions on the Portal privacy page |

### 2.2 Device work packages (v2 §11, A–I)

| # | Work | Status | Evidence / what is left |
|---|---|---|---|
| A | Installer on the Pi (`--system-site-packages`, headless OpenCV, `lgpio`) | Partly | `deploy/pi5/install_pi.sh`, `run.sh`. Never run on a Pi |
| B | `PiCameraSource` (1280×720 main, 640×480 analysis) | Partly | `sentrix/io/picamera.py`. Needs the real Camera Module 3 |
| C | GPIO on gpiozero (siren, LEDs, PIR, door, mains) | Partly | `sentrix/io/gpio.py`, `actuators.py`. Needs the bench tests (§6) |
| D | Clip engine, fast lane, combined YOLO, small scene model, smaller face model, NCNN | Partly | Clip engine and fast lane done. Combined YOLO not started (M7). Slim scene model failed its bar. `buffalo_s` and NCNN not installed |
| E | Channel router with outbox and retries | Done (fakes) | `sentrix/alerting/channels/router.py` |
| F | Network failover (Wi-Fi metric 600, modem 700) | Partly | `deploy/pi5/networkmanager/`. Needs the dongle |
| G | Node page | Partly | As above |
| H | systemd, watchdog, `var/` partition, read-only system, cloned spare card | Partly | Units and watchdog in `deploy/pi5/systemd/`. Read-only root is a documented option. No spare card cloned |
| I | Pi process layout | Decided: one process | docs/7 §16 q5: single process kept; revisit only if Gate A fails with the link running |

### 2.3 Portal work packages (v2 §11, C0–C11)

| ID | Package | Status | What is left |
|---|---|---|---|
| C0 | Decisions D8–D14; Oracle card owner; domain; bot; e-mail API; modem + SIM | Partly | Decisions recorded. Accounts, domain, modem and SIM not done (§4, §5) |
| C9 | Hosting: VM, Docker, Caddy HTTPS, backups + tested restore | Partly | Files in `deploy/portal/`. Nothing deployed (§5) |
| C1 | Portal core (accounts, roles, sessions, Argon2id, TOTP, CSRF, back-off, audit, admin) | Done | Isolation and CSRF tests in `tests/test_acct_*.py` |
| C2 | Device link, tunnel, pairing, setup Wi-Fi page | Partly | Done on loopback. Not over TLS / CGNAT / 4G. Setup Wi-Fi page untested with real nmcli |
| C3 | Parity hardening | Done | `tests/test_web_*.py`; docs/7 §17 parity rows |
| C4 | Onboarding wizard, household, contact confirmation, profile push | Done | docs/7 §17 |
| C5 | SOS engine | Done | `tests/test_sos_*.py`; live-session replay: home 0 requests, armed 1 |
| C6 | Channels | Partly | Real accounts and modem missing |
| C7 | Remote live view on 4G | Partly | Needs measuring on a phone on 4G. `tools/bench_link.py` (docs/7 §15 item 3) is not written |
| C8 | Desk and contact pages | Done | docs/7 §17 |
| C10 | Demo pack | Partly | Tools done. Clip reliability: 3 of 3 runs per clip so far, not 10 of 10 |
| C11 | Privacy and docs | Partly | Consent notice not yet reviewed by the Thapar legal cell |

### 2.4 Known gaps in the built software

These are small, but each one must be closed or stated in the report.

| Gap | Where it is recorded | Needed for |
|---|---|---|
| No Alembic baseline; `create_all` does not add columns to an existing Portal database | docs/7 §16 q7; `sentrix_portal/db.py` | Any schema change after the first cloud deploy |
| No live Postgres run (psycopg not installed) | `CLAUDE.md` M10 | Cloud deploy |
| `tools/bench_link.py` not written (remote fps, glass-to-glass p95, uplink, CPU) | docs/7 §15 item 3 | C7, P4 |
| ~~No "record normal session" mode in the app~~ **done** (D20, 2026-10-28: the dashboard's *Record for training*, §15.1); still no `tools/build_custom_dataset.py` | `CLAUDE.md` M6, D20 | P8, G3, model training |
| `demo_run.py` always forces every live switch off | `tools/demo_run.py` | Real-channel rehearsals need their own start-up (§8) |
| An approved request on a device without a Portal never auto-closes | track notes (T-SOS) | Rehearsals: close it from the Desk |
| PIN failure counts reset on restart (kept in memory) | track notes (T-SOS) | State in the report |
| No liveness check on the authorised-face disarm (a photo could clear the entry delay) | docs/7 §16 q6 | State in the report |
| `buffalo_s` face pack not on disk; `deploy/pi5/pi.env` selects it | docs/7 §16 q10 | Pi identity; match threshold must be re-checked on enrolled team faces |
| Template wording nit "near opposite the SBI ATM" | docs/7 §17 | Polish before the demo |
| The custom weapon detector knows `pistol` and `stick` only; there is no knife footage in the custom dataset | `models/v4_custom/weapon_detector_custom_v4_metadata.json` | D1 promises knife: record knife data (§15) or drop knife from the promise |
| Fight vs banter (D15) is a published rule over an AudioSet tagger, never measured on room audio; the cue thresholds are a priori | `CLAUDE.md` D15, M11 | Record the audio sessions in §15.1; check the cue thresholds on S3, report S4 once |

---

## 3. The order of work

```
W0  Decide + order parts ──┬─► W1 Cloud setup (VM, domain, HTTPS, bot, e-mail)
                           ├─► W1 Modem + SIM tested on the laptop
                           └─► W1–W2 Pi bring-up (when parts arrive)
W2  Bench tests: power, relay (LED first), siren, modem on the Pi
W3  Gate A on the real Pi  ─────► go/no-go 1 (fail → Hailo HAT or laptop hub)
W3  Real device ↔ Portal over the Internet; M6 recording starts
W4  Real-channel rehearsals in test mode to team phones; power-cut test
W5  P4/P5/C7 measured on 4G; normal-footage hours collected  ─► go/no-go 2
W6  Box and mount; 24 h burn-in; code freeze at the end of W6
W7  Gates P1–P8 measured on the final build; rehearsals 1–2
W8  Rehearsal 3; recorded backup video; the teachers' demo
After the demo: model training on the new data (§15)
```

The week labels follow the v2 plan: W0 is the week the parts are ordered.
Software that the v2 plan placed in W1–W5 is already built, so that time is now
buffer for hardware and data.

### Owners and effort at a glance

Owners follow the v2 plan. Change them here if the team agrees otherwise.

| Phase | Lead | Helpers | Effort (est.) | Depends on |
|---|---|---|---|---|
| §4 Decisions and ordering | Mehul | all | 1 | — |
| §5 Cloud setup | Harshit | Mehul | 3 | card owner, domain |
| §6 Pi bring-up and bench tests | Mehul | Kartik | 5 | parts |
| §7 Gate A on the Pi | Kartik | Akshay, Harshit | 5 | §6 |
| §8 Real device ↔ Portal | Harshit | Mehul | 2 | §5, §6 |
| §9 Real-channel rehearsals | Prashant | Akshay | 2 | §5, §6 modem, §8 |
| §10 Gates P1–P8 | Mehul | all | 4 | §7–§9 |
| §11 Demo rehearsals and demo | Mehul | all | 4 | §10 |
| §15 M6 recording (overlaps W3–W5) | Harshit | all | ≈ 34 person-hours (`CLAUDE.md` open questions) | Pi camera mounted |

---

## 4. Decisions and ordering (W0)

**Owner:** Mehul, with the whole team. **Effort:** 1 day (est.).

1. Answer the open decisions in §14 that block buying: Pi 8 GB vs 4 GB,
   Camera Module 3 Wide vs Standard, night operation (NoIR) yes/no.
2. Name the Oracle account and card owner, the SIM holder (KYC in their name;
   Airtel or Vi, **not Jio**) and who recharges it.
3. Place Order 1 and Order 2 (below). Order the 4G modem in W0: it takes the
   longest to test.
4. Write the team phones, e-mail addresses and Telegram chat ids into a private
   note, not into the repository. They go into `SENTRIX_TEST_RECIPIENTS` and
   the `SENTRIX_DEMO_*` variables only on the machines that need them.

**Done when:** parts ordered; the card owner and SIM holder are named; the
decisions are written into `CLAUDE.md`.

### Bill of materials (summary of v2 §9)

| Order | Contents | Price |
|---|---|---|
| **Order 1: bench kit** | Raspberry Pi 5 8 GB (₹19,852), official 27 W PSU (₹1,255), Active Cooler (₹531), SanDisk High Endurance 128 GB (₹3,699), Camera Module 3 Wide (₹4,379), Pi 5 camera cable 22-pin Standard–Mini (₹117), ML-2020 RTC cell (≈ ₹500), USB microphone (₹289), relay module + transistors + diodes (≈ ₹150), PIR HC-SR505 (₹73), MC-38 door reed switch (₹52), breadboard, jumpers, LEDs, resistors (≈ ₹350); no buzzer or speaker (D19) | **≈ ₹31,250** |
| **Order 2: integration kit** | BIS-marked 12 V ≥ 4 A adapter (₹566), Resonate router mini UPS 12 V 3 A (₹2,399), XY-3606 step-down 12 V → 5.2 V (₹119), short thick USB-C cable (≈ ₹150), 12 V siren SQR117 (₹194), key/toggle arming switch (≈ ₹50), 3 inline fuses + PC817 optocoupler (≈ ₹80), 7Semi Quectel EC200U-CN 4G modem (₹2,236), Airtel/Vi prepaid SIM (≈ ₹300), IP65 ABS box + label (₹300–899), fan, mesh, cable glands (≈ ₹150), mount, perfboard, terminals (≈ ₹450) | **≈ ₹7,300** |
| Tools | multimeter, soldering kit, wire stripper, heat-shrink, micro-HDMI cable, card reader, 12 V panel meter, earplugs | ≈ ₹2,000 |
| **Device total** | Order 1 + Order 2 + tools | **≈ ₹40,500** (cheapest ≈ ₹36,000) |
| Running cost | SIM ≈ ₹200/month; demo hotspot data ≈ ₹200–300/month (est.); `.in` domain ≈ ₹500–800/year; Portal on free tiers ₹0 | |
| Buy only if needed | AI HAT+ 13/26 TOPS (₹9,749 / ₹14,999) if Gate A fails; Camera Module 3 NoIR + 850 nm IR light (₹3,379 + ≈ ₹200–2,000) if night is required; Geekworm X1202 UPS HAT (₹5,299 + cells) if the router UPS fails; MQ-2 + ADS1115 (₹98 + ₹142) for a log-only smoke signal; powered USB hub (≈ ₹800 est.) if the modem browns out | |
| Do not buy | a Jio SIM for the modem; A7670E modems (no band B40); unbranded 18650 cells; open-frame 230 V supplies; an AI HAT before Gate A; Coral or Intel NCS2 | |

Cheaper options from v2: Camera Module 3 Standard (₹3,245) saves ₹1,134; a
64 GB card (₹2,449) saves ₹1,250; a 4 GB Pi 5 at KSP Electronics for
₹12,000–12,300 is unconfirmed (the only confirmed 4 GB costs ≈ ₹18,490).
Strongly recommended: a spare 64 GB High Endurance card (₹2,449) for a cloned
copy of the finished setup.

`docs/8` §8 has an older price table (2026-10-10, base kit ≈ ₹30,500). The
v2 figures above are newer.

---

## 5. Cloud setup (W0–W1)

**Owner:** Harshit, with Mehul. **Effort:** 3 days (est.). **Runbook:**
`deploy/portal/README.md`.

| Step | Status | Done when |
|---|---|---|
| 1. Oracle Cloud account, India region (Mumbai or Hyderabad), pay-as-you-go with a ₹0 budget alert (README §1) | Not started | VM created; budget alert e-mail received |
| 2. Network: only 80/443 open (README §2); `deploy/portal/check_ports.sh` | Not started | the port check passes from outside |
| 3. `.in` domain and DNS A record (README §3). Not DuckDNS: campus filters may block it | Not started | the name resolves from 4G **and** from the demo room |
| 4. `docker compose up` with Postgres on the internal network and Caddy as the only published service (README §4) | Not started | `https://<domain>/healthz` answers over HTTPS with a valid certificate |
| 5. First run on Postgres. Install psycopg and run `tools/portal_pg_check.py` first | Not started | the Portal starts on Postgres; sign-in works |
| 6. Alembic baseline before any later schema change (docs/7 §16 q7) | Not started | `alembic upgrade head` on an empty and on the deployed database |
| 7. Backups and the restore test: `deploy/portal/backup.sh`, `restore_test.sh` (README §5) | Not started | a restore into a scratch database is tested and timed |
| 8. Fallback: Render + Supabase (Mumbai), pre-warmed (README §6) | Not started | fallback deploy in ≤ 30 min, rehearsed once |
| 9. First company admin: `python tools/provision_device.py admin ...`; TOTP set at first sign-in | Not started | admin signs in with TOTP on the VM |
| 10. Telegram bot from @BotFather; `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `SENTRIX_TELEGRAM_MODE=webhook` on the VM (polling on a laptop) | Not started | every team phone has pressed **Start** on the bot |
| 11. E-mail API account (Brevo or Resend over HTTPS, not SMTP); `SENTRIX_EMAIL_PROVIDER`, the API key, `SENTRIX_EMAIL_FROM`; verify the sender domain | Not started | the provider shows the domain verified |

All secrets go into the VM's `.env` (names only in
`deploy/portal/.env.example`). Never commit them.

**Done when:** the Portal answers on HTTPS from a phone on 4G and from the demo
room; a backup has been restored; the bot and e-mail accounts exist; live
switches are still **off**.

---

## 6. Pi bring-up and bench tests (W1–W2)

**Owner:** Mehul (wiring, enclosure), with Kartik. **Effort:** 5 days (est.).
**Runbook:** `deploy/pi5/README.md` (follow its bring-up order exactly).

### 6.1 Bring-up

| Step | Done when |
|---|---|
| OS: Raspberry Pi OS 64-bit Lite on the High Endurance card; Active Cooler; ML-2020 RTC cell; SSH keys only | boots; `vcgencmd measure_temp` reads |
| Camera on CAM/DISP 0 with the 22-pin cable | `rpicam-hello --list-cameras` lists it |
| `sudo deploy/pi5/install_pi.sh --boot-config --ufw <LAN>/24`, reboot | `vcgencmd get_throttled` = `0x0`; `pinctrl get 17` shows `op dl … lo` |
| Device identity: `python tools/provision_device.py device --out /etc/sentrix --label label.html` on the VM's Portal database, copied to the card | `/etc/sentrix/identity.json` (mode 0600) on the Pi |
| Doctor: `sudo -u sentrix /opt/sentrix/.venv/bin/python /opt/sentrix/tools/doctor.py` | every hardware row passes or is explained |
| Inputs with the siren **not** connected: PIR, door, mains pull, watched on `GET /api/node` | each input changes state once per action; one `node_alert` row per mains pull |
| LEDs: lamp test at start; green / amber / red as in the README | matches the README table |
| Picamera2 checks still open from T-HW: the lores format, the timestamp clock, CPU cost at 15 fps | recorded in `docs/8` |
| USB microphone plugged in **while SENTRIX runs**, then moved to another port (D21) | the Node page's Microphone row names it within a minute each time; `tools/doctor.py` lists it as the input that would open |
| A 1-minute *Record for training* with someone talking, downloaded on a laptop on the same Wi-Fi (D20) | VLC plays the `.avi` with sound in sync; the sidecar shows few repeated or dropped frames |

### 6.2 Power test

| Test | Pass when |
|---|---|
| `PSU_MAX_CURRENT=5000` set with `sudo rpi-eeprom-config --edit` (non-PD supply) | without it USB gets 600 mA and the modem browns out |
| Full load: clip engine running, siren pulsing (LED stand-in first), modem sending | EXT5V (`vcgencmd pmic_read_adc`) ≥ 4.8 V; `get_throttled` = `0x0` |
| 10 mains pulls on the UPS | no reboot; 10 `node_alert` rows; LC shows `mains: false` each time |
| Run time on the UPS | measured and written into `docs/8` (v2 says ≈ 2 h+, to be measured) |

Safety: sealed BIS-marked adapters only; never rewire 230 V; keep the UPS and
its lithium cells outside the box.

### 6.3 Alarm (relay and siren) test

Wire an LED with a 1 kΩ resistor where the siren will go. The LED must stay
off through: a cold start, `sudo reboot`, `systemctl stop sentrix`,
`kill -9` of the service, and a mains → UPS switch. "Test siren" on the Node
page must light it for 1 s, at most once a minute (`deploy/pi5/README.md`,
"The relay test"). Only then connect the 12 V siren on its own fuse.

Real siren tests: earplugs, 3 m or more away, 5 s bursts, daytime only (110 dB:
the safe daily dose is ≈ 90 s at the rated distance). With the key switch
off, a siren test must answer 409.

### 6.4 Modem test

1. **On the laptop first (W1):** plug in the modem with the Airtel/Vi SIM. Set
   `SENTRIX_MODEM_ENABLED=1` and `SENTRIX_CHANNELS_ENABLED=1` and leave
   `SENTRIX_MODEM_LIVE=0`. Check signal and registration on `GET
   /api/channels` (the driver reads `AT+CSQ`, `AT+CREG?`, `AT+CEREG?`).
2. Then one live SMS to one allowlisted team phone (both switches:
   `SENTRIX_LIVE_ALERTS=1`, `SENTRIX_MODEM_LIVE=1`; the number in
   `SENTRIX_TEST_RECIPIENTS`) with `POST /api/channels/test` as the device
   admin. The text must start with the test banner.
3. Still open from T-CH: PDU mode on the real module, delivery-report routing,
   the AT port number, and whether the module registers for SMS on the SIM.
4. **On the Pi:** the same test, then network failover:
   `install_pi.sh --no-apt --no-service --modem-apn airtelgprs.com` (or
   `--modem-ecm`); turn off the hotspot's mobile data; within 30 s
   `ip route show default` must go through the modem.
5. Keep a spare Android phone as the documented fallback SMS sender.

**Done when:** one test SMS has reached a team phone from the laptop and from
the Pi; failover moves the default route to the modem.

---

## 7. Gate A on the real Pi (W3)

**Owner:** Kartik (models and exports), with Akshay (face model) and Harshit
(clip engine). **Effort:** 5 days (est.).

Gate A (`docs/8` §7) passes when, **on the Pi, with the link and the LC
running in the single-process layout**:

* clip compute p95 ≤ 1.6 s at a 2 s hop, ≤ 2 % clips dropped over 1 h;
* fast lane detect → L3 p95 ≤ 2 s; clip detect → L4 p95 ≤ 6 s;
* on the 10 custom videos in clip mode: L3 catches ≥ the M5 result minus one
  (weapon 9/12, fire 3/3) and 0 false onsets;
* 1 h soak: CPU ≤ 75 °C, no throttling flags, memory flat.

Today: the replay criterion is met on the laptop; the laptop bench at 4
threads is **over** the budget (p95 2145 ms). The slow stages are three YOLO
passes per keyframe and DINOv2-B (`docs/8` §7). So expect Gate A to fail
with the current models unless NCNN closes the gap.

Steps:

1. Export on the laptop: `python tools/export_models.py --scene` (ONNX). For
   NCNN install `ncnn` and `pnnx` in a separate environment first, then
   `python tools/export_models.py --ncnn`. Copy `models/exported/` to the Pi.
2. Face model: get the `buffalo_s` pack, enrol consenting team members, and
   re-check its match threshold (docs/7 §16 q10). Otherwise keep `buffalo_l`
   and measure its cost.
3. Bench on the Pi: `python tools/bench_clip.py --fast-lane --runtime ncnn
   --seconds 3600 --json gateA_bench.json` (and `--runtime onnx`).
4. Replay on the Pi: `python tools/replay_custom_videos.py --videos all --mode
   clip --fast-lane --threads 4 --tag pi_gateA`.
5. Soak: run the service 1 h with the link on; log `vcgencmd measure_temp` and
   `get_throttled` every minute.

**If Gate A fails** (decide at go/no-go 1, end of W3):

| Option | Cost | Note |
|---|---|---|
| Fewer passes now: one keyframe per clip for DINOv2, YOLO on fewer keyframes | ₹0 | config only; re-run the clip replay to check the catches |
| T3: the Pi as sensor node, the laptop as hub | ₹0 | full accuracy; not the autonomous unit the project promises |
| T2: AI HAT+ (Hailo) | ₹9,749–14,999 | compiler work; Gate B in `docs/8` §7 |
| M7 unified YOLO (one pass instead of three) | data + training | after the prototype (§15) |

**Done when:** Gate A is PASS on the Pi, or the team has chosen a fallback and
written down why.

---

## 8. Connect the real device to the Portal (W3)

**Owner:** Harshit, with Mehul. **Effort:** 2 days (est.).

1. On the Pi `.env`: `SENTRIX_LINK_ENABLED=1`, the Portal's `wss://` URL,
   `SENTRIX_SOS_ENABLED=1`, `SENTRIX_CHANNELS_ENABLED=1`, test mode on, live
   switches off. `deploy/pi5/pi.env` has the keys.
2. Admin on the VM: create the demo customer, assign the provisioned serial,
   print the welcome letter.
3. Customer on a phone on 4G: activate, wizard, claim by **scanning the QR on
   the label** (the first real QR claim).
4. Check the LED goes green when claimed; check `GET /api/link` on the LC.
5. Through `/d/{id}/` on the phone: every LC page, the live video with the
   "remote N fps" label, the Node page.
6. Write `tools/bench_link.py` (docs/7 §15 item 3: remote fps, glass-to-glass p95,
   uplink, CPU) and measure C7 on 4G: ≥ 5 fps (target 10), p95 ≤ 1.5 s, Gate A
   numbers unchanged.
7. Pull the Pi's network for 60 s while a request is open: the Portal must
   mark it offline after 45 s and raise the tamper signal; on reconnect the
   outbox drains with nothing lost.

**Done when:** the phone on 4G shows the same dashboard as the LC, a claimed
device stays online for 24 h, and C7 is measured.

---

## 9. Real-channel rehearsals in test mode (W4)

**Owner:** Prashant (channels), with Akshay. **Effort:** 2 days (est.).

Test mode stays on. Only allowlisted team phones and addresses receive
anything, and every message starts "[SENTRIX DEMO – TEST, NOT A REAL
EMERGENCY]" (D13).

`tools/demo_run.py` forces every live switch off on purpose, so rehearsals run
on the VM Portal and the Pi with their own `.env`:

| Machine | Switches for the rehearsal |
|---|---|
| Portal (VM) | `SENTRIX_TEST_MODE=1`, `SENTRIX_LIVE_ALERTS=1`, `SENTRIX_TELEGRAM_LIVE=1`, `SENTRIX_EMAIL_LIVE=1`, `SENTRIX_TEST_RECIPIENTS=<team phones, e-mails, chat ids>` |
| Device (Pi) | `SENTRIX_TEST_MODE=1`, `SENTRIX_LIVE_ALERTS=1`, `SENTRIX_MODEM_LIVE=1`, the same `SENTRIX_TEST_RECIPIENTS`; `SENTRIX_TWILIO_ENABLED=0` |

Order:

1. One message per channel to one team member: Telegram, e-mail, modem SMS
   (`POST /api/channels/test`).
2. Link each team member's Telegram from the Portal (`/start <code>`).
3. Each SOS path once, with a scripted trigger
   (`POST /api/sos/demo/trigger`, labelled SIMULATED DETECTION) and once with a
   real clip played to the camera: approve, decline with the PIN, expiry,
   duress, safety default for fire, internet down (hotspot data off → modem
   SMS).
4. Check the Desk, the contact page and the audit trail after each path.
   Close approved requests from the Desk (they never auto-close).
5. Turn the live switches **off** again afterwards and write down that you did.

**Done when:** each channel has delivered to a team phone; each path ended in
the expected state; no message reached anyone outside the allowlist.

---

## 10. Gates before the demo (W5–W7)

Measure each gate once, on the final build, after the code freeze.

| Gate | Pass when | Status today |
|---|---|---|
| **Gate A** | see §7 | Not measured on a Pi; laptop bench over budget |
| **P1 Parity** | every LC route directly and through the tunnel gives the same status, key DOM ids and API fields, with production CSP and CSRF | Partly: laptop smoke run and e2e suite pass; repeat on the VM over HTTPS |
| **P2 Isolation** | 0 cross-customer leaks; only login, static, contact links and the Telegram webhook open, on the Portal and the device | Partly: tests pass (`tests/test_acct_isolation.py`, e2e); repeat on the deployed Portal |
| **P3 SOS correctness** | 20 scripted runs of each path with fake transports end in the expected state with a full audit trail | Partly: 13 e2e scenarios and the SOS tests; the 20-runs-per-path script is not written |
| **P4 Latency** | L3 → phone prompt p95 ≤ 5 s online; approve → Desk ≤ 3 s; offline L3 → SMS p95 ≤ 60 s | Not passed: approve → device DISPATCHED 0.01–0.28 s on loopback; phone prompt and SMS not measured |
| **P5 Offline** | uplink pulled: LC prompt and modem SMS work; 100 % of queued events sync | Partly: Portal-kill run drained the outbox (5 rows); modem path not run |
| **P6 Privacy** | consents, withdrawal and erasure verified; 0 face images or evidence on the Portal; no credentials in link messages | Partly: by design and in tests; check the deployed database and logs |
| **P7 No alerts in tests** | socket guard green; evaluation opens 0 requests; device-side allowlist enforced | Done in the suite (`tests/conftest.py`, `tests/test_sos_*.py`, `tests/test_ch_*.py`); re-run on the final build |
| **P8 Prompt budget** | ≤ 1 false SOS prompt per 3 h of normal footage from the M6 sessions; unlocks `contacts_for_single_source_l3` | Not started: needs ≥ 3 h of deployment-room normal footage |
| **G0–G5** (M9) | see §15.10 | G1 PASS, G2 FAIL, G4 FAIL, G0/G3/G5 not run (`training/runs/custom_eval/m5/gates.json`) |

How to measure P8 and G3 with the existing tools:

```
python tools/replay_custom_videos.py --negatives <folder of normal recordings> --mode clip --fast-lane --tag p8
python tools/gate_report.py --replay ... --negatives training/runs/custom_eval/p8/negatives.json ...
```

A false SOS prompt is an L3 onset of a D1 class (or an armed intrusion) on
normal footage. Until P8 passes, `contacts_for_single_source_l3` stays
`False`.

---

## 11. Demo rehearsal and the teachers' demo (W6–W8)

**Owner:** Mehul (narrator), with everyone. **Effort:** 4 days (est.).

### 11.1 Roles (v2 §14)

| Role | Person | Device |
|---|---|---|
| Narrator | Mehul | projector |
| Company admin | Harshit | laptop A (also shows the LC) |
| Customer "Riya" | Akshay | phone on 4G, mirrored |
| Emergency contact | Prashant | phone with Telegram |
| SENTRIX Desk operator | Kartik | laptop B, Desk console |

### 11.2 The day before

* Portal on the VM in test mode; allowlist = team phones; Twilio off; demo
  window 90 s. On the VM, create the demo household through the admin console
  and the wizard (as in §8). `tools/demo_reset.py --window 90 --live-steps 2`
  builds the same world for the laptop-only setup (its own SQLite Portal in a
  marked demo directory, the only thing it ever wipes); it does not write to
  the VM's Postgres.
* Detection input: a video file played to the device, clips chosen only by
  measured reliability (**≥ 9 of 10 triggers**; never wa02; thresholds never
  changed to make a clip fire). `python tools/demo_run.py clips` lists the
  scenes. Today only 3 of 3 runs per clip are measured (docs/7 §17): run the
  10 pre-runs per clip.
* Between scenes: `close` (test mode) or ≥ 25 s of normal footage, so each
  scene opens a new incident.
* Network: our own hotspot phone with its own data plan, **never campus
  Wi-Fi**. Modem SIM has credit. Every phone has started the bot. A recorded
  video of the whole flow is ready.

### 11.3 Script (15 minutes)

| Time | Scene | What the teachers see |
|---|---|---|
| 0:00–1:00 | The problem | one slide: the 9-step flow and the architecture |
| 1:00–2:30 | Company sells | admin creates "Riya Sharma (demo)", prints the welcome letter, shows the device label |
| 2:30–5:00 | Onboarding | activation, password, contacts (Prashant confirms by link), claim by QR (LED green), PIN and window, SOS preview |
| 5:00–6:30 | Same dashboard | Portal on the phone next to the LC: gauge, scores, scene card, live feed, events, evidence, HOME/ARMED |
| 6:30–9:00 | Path A: approve | handgun clip → L3 → Telegram + prompt → "Alert my contacts and the SENTRIX desk" → Desk acknowledges → contact page → resolve |
| 9:00–10:00 | Path B: false alarm | next trigger → False alarm + PIN → DECLINED, verdict FALSE |
| 10:00–12:30 | No answer | safety default off: EXPIRED (unanswered) at 90 s, "Request help now"; safety default on: the contact is alerted at once |
| 12:30–14:30 | Internet down | hotspot data off → "device offline" on the Portal → trigger → LC prompt + modem SMS → approve locally → data back on → nothing lost |
| 14:30–15:00 | Close | what is simulated (112, the Desk), what was measured (the gates), privacy |

Backup plans: the disclosed scripted trigger (labelled SIMULATED DETECTION);
the recorded video; the laptop-only demo (`tools/demo_run.py`, verified in
docs/7 §17); the pre-warmed fallback host.

**Rehearsals:** three clean dry runs in the demo room with the real network,
timed. Fix only bugs, never thresholds, after the freeze.

**Done when:** three clean dry runs; G5 (30-min live dry run, 10/10 scripted
events, 0 unscripted L3+) recorded with `tools/gate_report.py --dry-run-log
... --script script.csv`.

---

## 12. Integration checklist (step by step)

Tick each line in order. Each one depends on the lines above it.

**Laptop (now)**

- [ ] `python -m pytest` green on `sentrix/portal`.
- [ ] `python tools/doctor.py --no-camera` passes.
- [ ] `python tools/demo_reset.py` + `python tools/demo_run.py`: the full
      script runs on one laptop (already verified once, docs/7 §17).
- [ ] Fix the template nit ("near opposite").
- [ ] Write `tools/bench_link.py` and the P3 scripted-runs driver.

**Accounts and cloud**

- [ ] Oracle VM in India, ₹0 budget alert, ports 80/443 only.
- [ ] `.in` domain resolves from 4G and from the demo room.
- [ ] Portal on Postgres behind Caddy over HTTPS; Alembic baseline.
- [ ] Backup restored once; fallback host deployed once.
- [ ] First admin with TOTP; Desk operator with TOTP.
- [ ] Telegram bot (webhook + secret); e-mail API with a verified sender.

**Hardware**

- [ ] Pi boots; cooler, RTC cell; `install_pi.sh` done; doctor passes.
- [ ] Camera, PIR, door, mains, LEDs checked.
- [ ] Relay test with an LED passed; then the siren with the key switch.
- [ ] Power test: EXT5V ≥ 4.8 V under load, 10 mains pulls, UPS run time.
- [ ] Modem: SMS to one team phone; failover to 4G.
- [ ] Gate A (or the chosen fallback).

**Device ↔ Portal**

- [ ] Device provisioned on the VM; identity on the card; claimed by QR.
- [ ] Tunnel pages and video on a phone on 4G; C7 measured.
- [ ] Offline test: link down → tamper signal → reconnect → 0 lost events.

**Channels (test mode, allowlist)**

- [ ] One message per channel to one team phone.
- [ ] Every SOS path once with real channels; live switches off afterwards.

**Data and gates**

- [ ] Mounted at 1.8–2.2 m in the demo room; ≥ 3 h normal footage recorded.
- [ ] P8 measured; then decide `contacts_for_single_source_l3`.
- [ ] P1–P8 and G0–G5 measured once on the frozen build.

**Demo**

- [ ] 10 pre-runs per demo clip (≥ 9 of 10).
- [ ] Three clean dry runs; recorded backup video.
- [ ] Teachers' demo.
- [ ] After the demo: delete enrolled faces and demo data (v2 §5).

---

## 13. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Gate A fails on the Pi (laptop bench is already over budget) | High | NCNN first; fewer keyframes; T3 hub or Hailo HAT as fallback; decide at go/no-go 1 |
| Hardware arrives late or a part is wrong (camera cable, modem bands) | Medium | order in W0; the laptop-only demo works today |
| Modem SMS fails (Jio IMS, power, daily cap) | Medium | Airtel/Vi SIM; test on the laptop in W1; powered hub; spare phone as sender |
| Oracle signup, card or capacity problems | Medium | start in W0; Render + Supabase fallback rehearsed |
| Campus network blocks the Portal or Telegram | Medium | own hotspot phone; domain tested from the room in W1 |
| False alarms in the demo room (the models were trained in two other rooms) | High | `CLAUDE.md` §5: the room is out of distribution; record deployment-room data; P8 before contacts get single-source alerts |
| Knife is promised (D1) but no custom knife data exists | High | record knife clips (§15) or drop knife from the demo promise |
| A clip misses its trigger in the demo | Medium | ≥ 9/10 pre-runs; disclosed scripted trigger; recorded video |
| Schema change after the first cloud deploy breaks the Portal database | Medium | Alembic baseline before the first deploy |
| Users think SENTRIX calls the police | Low | button wording, "Call 112 now" first, the disclaimer in every SOS |
| Siren noise or mains/lithium accident | Low | LED tests first, earplugs, BIS adapters, cells outside the box |
| Live keys in `.env` reach a test | Low | guard, two switches, allowlist, socket guard in tests; never copy `.env` between machines |
| Scope: data recording competes with demo preparation | High | code freeze end of W6; model retraining is after the prototype |

---

## 14. Decisions still open

| # | Question | Recommended default | Blocks |
|---|---|---|---|
| 1 | Pi 5 8 GB (₹19,852) or a 4 GB from KSP (unconfirmed ₹12,300) | 8 GB unless KSP confirms | Order 1 |
| 2 | Camera Module 3 Wide or Standard; night operation (NoIR + IR light)? | Wide; night out of scope (D5) unless the teachers require it | Order 1, data recording |
| 3 | SOS default window 30 or 60 min; accept the safety defaults (D9)? | 30 min; safety defaults on | demo settings |
| 4 | Should a corroborated fire sound the siren at L4 instead of L5? | no, until fire-on-screen hard negatives are tested | — |
| 5 | Who plays the Desk; who owns the Oracle account and card; who holds and recharges the SIM | Kartik plays the Desk (v2 §14) | §4, §5 |
| 6 | Demo date and room; English only or Hindi too | English only (Hindi templates are Tier 2) | §11 |
| 7 | Permission for replica weapons and flames on campus; consent notice to the legal cell | ask in W0 | data recording, demo |
| 8 | If Gate A fails: Hailo HAT, laptop hub, or fewer passes | decide at go/no-go 1 | §7 |
| 9 | Owners may log in to the LC directly? (now: no; LC keeps `SENTRIX_PASSWORD`) | keep no | — |
| 10 | CUDA torch in the app environment (moves DINOv2 to the RTX 4060) | not needed for the demo | laptop speed only |
| 11 | Knife in the promise without knife data | drop it unless knife clips are recorded and pass G1/G2 | demo promise |
| 12 | Who deleted `Kartik.jpg` during the first live run (no audit then; gallery changes are audited now) | — | — |

---

## 15. Training the video models on more custom data (after the prototype)

This is the practical guide for M6–M9: collect data in the rooms where SENTRIX
will run, with the Pi camera itself, then retrain, calibrate, evaluate and
gate. Why it is needed: the first live run (`CLAUDE.md` §5) showed the shipped
models are far outside their training data in a new room (nearest-neighbour
cosine 0.25–0.39 vs 0.91–0.97 on val/test). Threshold tuning cannot close a
gap that large. Today there are 341 s of labelled footage from two rooms
(62.6 s handgun, 11.4 s flame, 2 s robbery).

### 15.1 What to record

**Where.** The demo room R1 and at least one other room R2. More rooms are
better: a model that has seen one room learns the room, not the threat.

**With what.** The Raspberry Pi with the Camera Module 3 you will deploy,
through the same capture path the product uses (`PiCameraSource`, 640×480
analysis stream, 1280×720 main stream). Data from a laptop webcam or a phone
does not transfer well.

**How it is mounted.** 1.8–2.2 m high, tilted 10–15° down, the same lens and
the same mount as in deployment.

**Light.** A lit room by default. Record each session at a few light levels
(daylight, evening lamps). If night operation is required, buy the NoIR
camera and an 850 nm IR light and **record the training data in IR**: the
models have never seen grayscale IR images (`docs/8` §11). Darkness without IR
stays out of scope (D5).

**Sessions.**

| Session | Use | Rule |
|---|---|---|
| S1, S2 | train | team members as actors |
| S3 | validation (thresholds, calibration checks) | a different day |
| S4 | test, measured **once** | day 5 or later, **unseen volunteers**, never used to choose anything |
| S5 | re-test | only if a class fails on S4; fresh day and people |

**Scripted clips.** 20 s each, 12 per class per room per session. Start and end
each clip with a few seconds of normal activity so episodes have a clear onset.
Vary the distance (1–4 m), the side of the frame, the hand, and how long the
object is visible. Classes:

| Class | In the alarm path (D1) | Examples |
|---|---|---|
| handgun (replica) | yes | drawn from a pocket, pointed, held at the side |
| knife | yes | kitchen knife held, raised, half hidden |
| visible flame | yes | lighter flame, small controlled flame in a tray |
| assault (staged fight) | L3 only with aligned aggression audio (D15) | **with sound**: shouting, screaming, a slap on a cushion; actors agree a script and a stop word |
| play-fighting / banter | must stay ≤ L2, labelled banter (D15 banter rule) | the same moves as the staged fight, with laughter and calm talk; as many clips as the fight |
| robbery, stick, smoke | no, experimental (≤ L2) | record a few for later work only |

**Audio with every clip (D15).** The microphone you will deploy (USB), at
the device, recorded together with the video so the two stay in sync: the
dashboard's *Record for training* (D20, below) puts the microphone's 16 kHz
track into the same AVI, sample 0 under frame 0. Plan: silent recordings first
(phase 1, now); with sound once the Scalebee USB microphone is in (arrives
2026-11-03), for the 2026-11-30 evaluation. For each room and session also record:
laughter and calm conversation (5 min), a raised-voice argument without
contact (2 min), shouting and screaming (staged, 2 min), TV/music in the
background (5 min), a smoke-alarm test beep, and gunshot / glass-break sounds
played from a speaker (never real). These check the cue thresholds
(`settings.audio.cue_thresholds`) and the fight and banter rules; until then
they are a priori values.

**Hard negatives: as many as positives.** Things that look like a threat but
are not:

* a phone, TV remote, stapler or hair dryer held like a pistol;
* a closed umbrella, broom or rolled newspaper held like a stick;
* a spoon, spatula or pen held like a knife;
* an unlit lighter; an aerosol can without a flame;
* a fire video playing on a TV or phone screen; a candle photo;
* red or orange clothing, a red bag, an orange lamp, sunset light;
* hugging, play-fighting, stretching, fast walking, carrying boxes;
* people facing away, people very close to the lens, an empty room.

**Normal footage.** At least **3 hours** of ordinary use in R1 on days with no
scripted recording (people reading, cooking, cleaning, walking past, lights
switched on and off). This is what P8 and G3 are measured on. More hours give
a tighter bound: 0 false onsets in 3 h still allows up to about 1 per hour at
95 %.

**Tools: record from the dashboard (D20).** The device records through its
own capture path, so no script and no second program holds the camera:

1. Open the device's console and sign in with the device password (the
   device admin): on the Pi from a computer on the **same network**,
   `http://<device-ip>:8000` (the Pi profile binds the LAN; `ufw.sh` limits
   the port to the subnet); on a laptop running SENTRIX, `http://127.0.0.1:8000`
   on that laptop (the console binds loopback only unless `SENTRIX_HOST` is
   set deliberately, with a firewall rule). The Portal does not offer
   recording, and never carries the files.
2. On the dashboard, *Record for training*: type a label (e.g.
   `S1 R1 handgun 001`; it becomes part of the file name), *Start recording*.
   A red **REC mm:ss** shows on the live video for everyone watching; it is
   not in the file.
3. Act the clip, *Stop recording*, wait until "Finishing the files…" clears.
4. *Download video* (an MJPEG AVI at the camera's resolution and frame rate,
   with the microphone's sound when one was connected and ffmpeg is installed;
   VLC plays it) and, when listed, *Download sound* (the `.wav`, without
   ffmpeg). Then *Delete* it on the device.

One recording is at most 10 minutes (a long "normal" session is several
recordings); the device keeps 2 GB free, holds at most 20 GB of recordings
and deletes them after 14 days. Each recording's `.json` sidecar records the
time, frame rate, frames repeated or dropped, audio gaps and the camera; still
write one line per clip into a session sheet: file, session, room, actor id,
class, scripted onset/offset, lighting, notes. `tools/build_custom_dataset.py`
(sessions → the `frames.csv` schema) is still to write (`CLAUDE.md` M6).

**Safety and permission.**

* Written permission from the department and campus security before any
  replica weapon is brought on campus. Replicas only, clearly marked; tell
  people in the area before recording.
* Flames: an extinguisher at hand, a non-flammable surface, the smallest flame
  that is visible, a second person watching. Never near the Pi box or cables.
  In the demo, fire comes from a video file.
* Consent: written consent from every person filmed, **before** pressing
  *Start recording*; no minors; volunteers may withdraw and their clips are
  deleted. Footage stays on the device only until it is downloaded (delete it
  there at once), then on team machines, out of git (`.gitignore` ignores
  `*.avi`, `*.mkv`, `*.wav` and `rec_*.json`) and out of the repository's
  `var/`.

**Effort.** About 34 person-hours of recording, correction and QC over three
weeks (`CLAUDE.md` open questions).

### 15.2 Labelling

1. **Per-video timeline labels.** For every clip, write segments in the
   existing `segments.csv` format: `video_id, video_path, start_time,
   end_time, label, confidence, annotation_method, split, notes`
   (`training/datasets/sentrix_custom/segments.csv`). Mark the uncertain
   seconds around an onset with a lower confidence: the evaluation scores them
   with a tolerance (`--tolerance`, `--grace` in
   `tools/replay_custom_videos.py`). The timeline drives every episode metric
   (`sentrix/evaluation/custom_dataset.py`, `Timeline.episodes`).
2. **Frame sampling.** Sample frames from each clip (the current set took
   every 10th frame of 30 fps video, then dropped 522 near-duplicates and
   kept 765), drop near-duplicates, record blur and
   brightness, and write `frames.csv` in the current schema: `image_path,
   class, class_id, source_video, source_video_id, source_frame, timestamp,
   split, width, height, blur_score, brightness_score, duplicate_status,
   label_confidence, annotation_method, status, reason`. This is the job of
   the planned `tools/build_custom_dataset.py`.
3. **Boxes.** `training/custom/annotate_boxes.py` proposes boxes with
   GroundingDINO, but only on frames whose reviewed label already says the
   object is there, and a weapon box must overlap a person (held objects).
   Add prompts for knife and for the new rooms to its `PROMPTS` table first.
   Then review every contact sheet under `--qc-dir` and list rejected boxes in
   `box_qc_rejects.txt` (`--reject`). A positive frame without an accepted box
   is excluded from detector training, never turned into a negative.
   ```
   python training/custom/annotate_boxes.py --out <dataset>/boxes.csv --qc-dir training/runs/custom/annotation_qc
   ```
4. **Human QC.** Two people check each class: one labels, one reviews. Keep
   the QC notes with the dataset (`dataset_report.txt`).

### 15.3 Dataset layout and versioning

Keep the layout of `training/datasets/sentrix_custom/`:

```
<dataset>/
  DATASET_README.md   what, where, who, consent, sessions
  classes.json
  videos.csv          video_id, file, split, fps, frames, duration_s
  video_split.csv     per-video split and classes present
  segments.csv        timeline labels
  frames.csv          sampled frames (schema above)
  labels.csv          per-image labels
  boxes.csv           accepted boxes; box_qc_rejects.txt
  frames/             images
  videos/             source clips
```

* Give each version a name and a date (for example `sentrix_room_v1`) and
  point the tools at it with `SENTRIX_CUSTOM_DATASET` and
  `SENTRIX_CUSTOM_VIDEOS`, or `--dataset`.
* Write a SHA-256 manifest of S4 the day it is recorded
  (`tools/gate_report.py --data <S4> --manifest gate_manifest.json`: gate G0).
* Decide with the team whether new footage goes into git. It contains
  people's faces: by default keep it on team storage only, with the manifest
  and labels in git.

### 15.4 Splits

Split by **session, room and person**, never by frame. Frames from one clip
are near-copies, so a frame-level split leaks: it inflated every v3 score
(`CLAUDE.md` §4.1; `tools/audit_splits.py` measures it). Rules:

* S1/S2 → `train`, S3 → `val`, S4 → `test`, in the `split` column.
* No person in S4 appears in S1–S3.
* Model selection uses grouped cross-validation over train + val. Today
  `train_scene_classifier.py cv` holds out one **video** at a time
  (leave-one-video-out). With sessions, group the folds by session × room
  instead (a small code change in the trainer).
* Check every new split with `python tools/audit_splits.py manifest --train
  ... --val ...` and `... images` before training.

### 15.5 Training recipes

Use the GPU environment (`C:\Users\PC\anaconda3\envs\sentrix\python.exe`,
Python 3.11, CUDA). Write down the command, the commit and the power source
with every run.

**a) Unified YOLO11n detector (M7, Kartik).** One detector for person, pistol,
knife and fire at 640, replacing today's three YOLO passes (the largest Pi
cost, `docs/8` §7). Start from COCO `yolo11n.pt`; add the QC'd teacher boxes;
add rehearsal images from the public corpora (pistol, knife, fire only; smoke
dropped; `SENTRIX_WEAPON_DATA`, `SENTRIX_FIRE_DATA`); add **all** hard
negatives as background images. `training/custom/train_detector.py` trains one
task at a time today:

```
python training/custom/train_detector.py --task weapon --init yolo11n.pt --classes pistol,stick --rehearsal 1500 --retention 400 --tag new_weapon --out models/v4_custom/weapon_detector_custom_v4.pt
```

The unified detector needs a new task in that script (all four classes and the
person class in one YOLO dataset). Keep `--retention` to check the public
classes are not forgotten. Then use the detector with ByteTrack, the held-object
association and k-of-n confirmation (`sentrix/fusion/evidence.py`), and a
flicker check for fire.

**b) Person-crop weapon classifier (M7).** A second, different vote for
weapons: crop each tracked person and classify "armed / not armed". A probe of
this idea reached held-out AUROC 0.888 against 0.749 for the full-frame
classifier (`CLAUDE.md` §5). Start with a DINOv2-S linear probe, then train the
last blocks. Not in the repository yet.

**c) Scene / clip classifier retraining.** Same trainer, now with deployment
negatives:

```
python training/custom/train_scene_classifier.py cv --arch timm:vit_base_patch14_dinov2.lvd142m --init pretrained --train-last-blocks 4 --lr 2e-5 --head-lr-mult 50 --tag room_dino_last4
python training/custom/train_scene_classifier.py fit --arch timm:vit_base_patch14_dinov2.lvd142m --init pretrained --train-last-blocks 4 --lr 2e-5 --head-lr-mult 50 --cv-tag room_dino_last4 --delta --out models/<version>/scene_classifier_<version>.pt
```

(The settings are those of the shipped `dino_b_last4` run,
`training/runs/custom/scene_cv/dino_b_last4/summary.json`.) Add an
out-of-distribution guard (a floor on the nearest-neighbour cosine to the
training set) and run it at ≤ 1 Hz.

**Slim backbones for the Pi.** On today's data neither slim head passed its
pre-registered bar (MobileNetV3-L 0.229, EfficientNet-B0 0.211 vs DINOv2
0.440 LOVO macro-F1; `training/runs/custom/slim/REPORT.md`). Retry them on the
larger dataset with the same commands (REPORT.md lists them), for example:

```
python training/custom/train_scene_classifier.py cv --arch timm:mobilenetv3_large_100 --init pretrained --tag mnv3l_full_room
python training/custom/train_scene_classifier.py cv --arch timm:efficientnet_b0 --init pretrained --teacher dino_b_last4 --mixup 1.0 --epochs 40 --tag effb0_kd_room
```

Set the pass bar **before** the runs (as `slim/PROTOCOL.md` did). The next
untested candidate is DINOv2-S/14. Use `training/custom/screen_backbones.py`
to screen backbones in seconds before fine-tuning any of them.

**d) Audio (Prashant, not gating).** Since D15 the sound model is the
pretrained PANNs CNN10 AudioSet tagger (`sentrix/perception/sound_tagger.py`,
`models/audio/`, run on its own onnxruntime threads), mapped to seven cues by a published rule; audio alone stays at
most L2, and it corroborates a visual source only when its window is aligned
with the frame. Nothing is fine-tuned yet. With the §15.1 audio sessions:
measure each cue's AUROC per room (laughter/calm vs shouting/screaming;
gunshot-from-speaker vs normal), set the cue thresholds on S3, report S4 once.
`training/scripts/train_audio_classifier.py` (the old log-mel CNN, the
fallback) remains for a deployment-specific head if the tagger fails.

### 15.6 Calibration and thresholds

* Thresholds and temperature come from **out-of-fold** predictions, never from
  the test session. `train_scene_classifier.py cv` writes `oof.csv`, fits a
  temperature and per-class thresholds, and `fit` applies them.
* Runtime floors stay (weapon 0.50, fire 0.45, assault/robbery 0.60;
  `ThreatConfig.scene_threshold_floor` in `sentrix/config.py`).
* Per-deployment thresholds go into `var/calibration/scene.json` or
  `SENTRIX_SCENE_THRESHOLDS`, fitted on S3 of that room only.
* The old thresholds were tuned for macro-F1 at 41 % threat prevalence. In a
  home, threats are rare: choose thresholds under a false-alarm budget (P8:
  ≤ 1 false prompt per 3 h), not for F1.

### 15.7 Fitted fusion (M8)

The TCI today is the strongest confirmed class evidence, by published rules
(D6). Replace it only with a fitted model that beats those rules:

1. Log feature contract v3 on every tick (the event log already records the
   votes, schema 3/4).
2. Cross-fit the perception outputs (each training row scored by a model that
   did not see it).
3. Fit a per-class logistic model with session-grouped CV; correct the prior
   to the deployment base rate.
4. Thresholds from the cost matrix in `notebooks/03_tci_thresholds.ipynb`
   **and** the false-alarm budget; calibration with
   `sentrix/fusion/calibration.py` and `notebooks/02_tci_calibration.ipynb`.
5. A monotonicity contract test: more evidence never lowers the TCI.
6. Ship only if it beats the rules on S3 and then passes on S4.

### 15.8 Evaluation that matches the product

Measure what users experience: the **committed level**, per episode, over long
normal sessions.

```
python tools/replay_custom_videos.py --videos all --mode clip --fast-lane --threads 4 --tag room_v1
python tools/replay_custom_videos.py --negatives <normal folder> --mode clip --fast-lane --tag room_v1
python tools/custom_eval_table.py room_v1
python tools/eval_custom_frames.py --splits val,test --tag room_v1
```

Every report includes `metrics.level_report`: onsets, false onsets per hour
with an exact Poisson 95 % interval, episodes caught at each level with the
right type, Clopper-Pearson recall intervals, median/p95 latency. Quote only
these generated numbers, with the commit.

* **P8, prompts per hour:** L3 onsets of D1 classes (and armed intrusions) per
  hour on the normal footage. Pass: ≤ 1 per 3 h.
* Replay the first live session as a regression test:
  `python tools/replay_decisions.py` on its event log (tick mode only; it
  refuses clip-mode logs).
* Replay at the Pi's real rate (`--hz`) and with `--threads 4`.

### 15.9 Export and Pi latency checks

```
python tools/export_models.py --scene            # ONNX + parity check
python tools/export_models.py --ncnn             # needs ncnn and pnnx
python training/custom/export_scene_onnx.py models/<version>/scene_classifier_<version>.pt
python training/custom/bench_scene_latency.py models/<version>/*.pt --threads 4 --out <run>/latency.json
python tools/bench_clip.py --fast-lane --runtime ncnn --json <run>/bench_pi.json      # on the Pi
```

* Parity: the export must match PyTorch on real frames (today: YOLO ≤ 2.5e-5,
  scene 2e-6; `training/runs/custom_eval/clip/export_parity.json`).
* Resize with PIL bilinear as in training: a cv2 `INTER_AREA` resize moved
  probabilities by up to 0.13 and flipped 1.6 % of labels
  (`training/runs/custom/slim/REPORT.md`).
* Gate A again on the Pi with every new model.
* OpenVINO export for the laptop's Intel CPU is optional (M7).

### 15.10 Gates before deployment (G0–G5)

Measured once, on S4, with `tools/gate_report.py`:

| Gate | Pass when |
|---|---|
| G0 | S4 exists and is untouched (SHA-256 manifest) |
| G1 | per promised class, frame AUROC ≥ 0.90 for at least one source |
| G2 | per promised class, ≥ 27/30 episodes caught at L3+ within 2 s |
| G3 | 0 L4/L5 over ≥ 3 h of normal footage and all hard negatives; ≤ 1 L3 onset per hour |
| G4 | per-frame: loop ≥ 5 Hz and onset → L3 p95 ≤ 2 s. Clip mode: fast-lane L3 p95 ≤ 2 s, a clip decision every ≤ 2 s, L4/L5 p95 ≤ 6 s |
| G5 | 30-min live dry run: 10/10 scripted events reach L3+, 0 unscripted L3+ |

```
python tools/gate_report.py --replay training/runs/custom_eval/gate/replay_all.json --negatives training/runs/custom_eval/gate/negatives.json --bench bench.json --data <S4 folder> --manifest gate_manifest.json --dry-run-log <events jsonl> --script script.csv
```

G2 needs at least 30 episodes per class in S4, so plan S4 with ≥ 30 scripted
events per promised class. A class that fails is **dropped from the promise**
or re-tested on a fresh S5. It is never tuned on S4.

### 15.11 Iteration loop with operator verdicts

After deployment, the people who use SENTRIX produce the best labels:

1. Every incident gets a **Real / False alarm** verdict on the Events page (or
   from the SOS decline). The event log keeps the votes behind each decision.
2. Use verdicts for training **only with consent**, and only from
   team-recorded sessions this semester (v2 §5). Customer data is never used
   for training without a separate opt-in.
3. Drop incidents whose FALSE label came from a **duress** decline: they look
   like a real decline on purpose (T-SOS note).
4. Every few weeks: export the verdict-labelled clips around each incident,
   add them as a new session (never into an old test session), retrain,
   re-run §15.8–§15.10, and ship only if every gate still passes.
5. Keep a changelog of model versions with their gate reports, and keep the
   previous model ready to roll back.
