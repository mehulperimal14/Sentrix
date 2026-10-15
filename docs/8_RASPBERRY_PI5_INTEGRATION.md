# Integrating SENTRIX with a Raspberry Pi 5

**Status (2026-10-15, branch `sentrix/portal`): the software is written; no Pi
has been bought or run yet.** Every hardware path is tested on the laptop
against fakes (gpiozero mock pins acting as a Pi 5, a fake `picamera2`, fake
`vcgencmd`, `nmcli` and `/sys`). The clip engine has been replayed and
benchmarked on the laptop under a 4-core budget. Nothing in this file is a
Pi measurement unless it says so.

This file is the hardware guide: what the unit must do, why the Pi needs a
different analysis design (the clip engine), what code exists for each part,
the wiring and power design, the parts list with prices, and Gate A — the
test that decides whether the Pi CPU is enough. The step-by-step bring-up is
[`deploy/pi5/README.md`](../deploy/pi5/README.md); the cloud side is
[`deploy/portal/README.md`](../deploy/portal/README.md) and
[`docs/7_PORTAL_AND_SOS.md`](7_PORTAL_AND_SOS.md).

Read with: `CLAUDE.md` (project state, milestones M6–M10, gates G0–G5),
`docs/3_HARDWARE_SPECIFICATION_AND_BOM.md` (the original ESP32 / Orange Pi
plan, superseded by this file), the SENTRIX Prototype Roadmap v2 (the
reviewed plan; prices checked 2026-10-14). Numbers in `[n]` point to the
sources at the end; "est." marks our own arithmetic.

---

## 0. Summary

| | |
|---|---|
| Core idea | Analyse a **rolling 4-second window** (video + audio + sensors) as one clip every 2 s, instead of running every model on every frame (§5 T1) |
| Built | Clip engine + fast lane + torch/ONNX/NCNN runtimes; Picamera2 source; gpiozero siren, LEDs, buzzer, PIR, door, mains sense; board telemetry and systemd watchdog; Wi-Fi setup and 4G failover profiles; modem SMS driver; `deploy/pi5/` installer and units (§9) |
| Measured on the laptop | Clip mode keeps the replay results (weapon at L3 9/12 with the fast lane, fire 3/3, 0 false onsets). Clip compute p95 **2145 ms**, over the 1.6 s budget, already on the laptop's 4 CPUs (§7) |
| Not yet measured | Anything on a Pi: camera, GPIO, temperature, the clip budget, the modem, power |
| Slim scene model | Tried and **rejected**: MobileNetV3-L and EfficientNet-B0 reach LOVO macro-F1 0.229 / 0.211 vs DINOv2 0.440. DINOv2-B stays (§5 T1) |
| Main track | **T1 — clip engine on the Pi 5 CPU**. Fallbacks if Gate A fails: **T2** Hailo AI HAT+, or **T3** the laptop as a local hub (both plans only) |
| Process layout | **One process** (`python -m sentrix`) owns the pipeline, Local Console, SOS, channels and the Portal link (§4) |
| Cost | Device ≈ **₹40,500** once (bench kit + integration kit + tools); SIM ≈ ₹200/month; `.in` domain ≈ ₹500–800/year; Portal ₹0 on free tiers (§8) |

---

## 1. What the unit must do

| Requirement | Target | Source |
|---|---|---|
| Detect handgun (replica), knife, visible flame; log experimental classes | per class ≥ 27/30 episodes caught | CLAUDE.md D1, gate G2 |
| False alarms in an ordinary home | 0 L4/L5 in ≥ 3 h normal footage, ≤ 1 L3/h | gate G3 |
| Detect → alert (clip mode) | fast-lane L3 p95 ≤ 2 s; a clip decision every ≤ 2 s; L4/L5 p95 ≤ 6 s | CLAUDE.md M9 (G4 for clip mode) |
| Autonomy | Detection, siren, evidence and SOS creation keep working with no Internet or PC | roadmap v2 §7 |
| Inputs | Camera (Camera Module 3 Wide), USB microphone, PIR, door contact, mains sense; optional BME280 / DHT22 | roadmap v2 §10 |
| Outputs | 12 V siren via relay, three status LEDs, buzzer, Local Console, SMS from a 4G modem | roadmap v2 §10, docs/7 §8 |
| Evidence | AES-256-GCM bundles, with the clip before the trigger; correct time offline (RTC cell) | M3, T1 |
| Privacy | Faces, evidence and continuous video stay on the device; the Portal sees them only through the tunnel | D12 |

---

## 2. The compute problem

The live stack (laptop, `main`): YOLOv8n person (+ByteTrack), YOLO11n weapon and
fire heads, DINOv2 ViT-B/14 scene classifier, InsightFace `buffalo_l`, a 4-class
audio CNN — 5.6 Hz on the laptop CPU (M2).

Planning measurements: laptop (Intel i7-14650HX) **limited to 4 threads** to mimic
the Pi 5's 4 cores (2026-10-10, median of 12 runs), next to published Pi 5
figures:

| Model (as shipped) | Compute | Laptop, 4 threads | Pi 5 CPU (published / est.) | Pi 5 + Hailo-8 |
|---|---|---|---|---|
| YOLOv8n person @640 | 8.9 GFLOPs | 24 ms (12 ms @320) | 94 ms NCNN, 509 ms PyTorch [U8] | ≈ 60 FPS [IJ] |
| YOLO11n weapon / fire @640 | 6.5 GFLOPs | 21–24 ms (10–11 @320) | 81–88 ms NCNN/OpenVINO, 405 ms PyTorch [U11] | 105 FPS [HF105] |
| DINOv2 ViT-B/14 scene @224 | 43.9 GFLOPs | 83 ms | ≈ 0.6–1.05 s est. [NCNN][ARM] | not in the Hailo zoo [HFdino] |
| DINOv2 ViT-S/14 @224 | 11.0 GFLOPs | 26 ms | ≈ 0.15–0.27 s est. | community script only [HFdino] |
| EfficientNet-B0 @224 | 0.77 GFLOPs | 11 ms | 12.3 ms ncnn [NCNN] | > 1000 FPS [HZ] |
| MobileNetV3-L @224 | 0.43 GFLOPs | 7 ms | 7.4 ms ncnn [NCNN] | > 1900 FPS [HZ] |
| SCRFD-10G + ArcFace R50 (`buffalo_l`) | ~10 GFLOPs + R50 | 54 ms + 58 ms/face | ≈ 0.5–0.6 FPS, several faces [THESIS] | zoo 440 / 113 FPS [HZ] |
| SCRFD-500M + MobileFaceNet (`buffalo_s`) | ~0.5 GFLOPs | — | ≈ 1.8–2.4 FPS, several faces [THESIS] | — |

What follows:

1. The per-frame stack would run at **≈ 0.5–0.7 Hz on a Pi 5 CPU** (est.) — the
   rate at which the first live test failed (CLAUDE.md §5).
2. CNNs port well (≈ 4× slower than the laptop's 4 threads with NCNN);
   transformers port badly (≈ 8–12×). DINOv2-B is the biggest obstacle.
3. The runtime matters as much as the model: the same YOLO is ≈ 5× faster in
   NCNN than in PyTorch on the Pi [U8][U11]. PyTorch on the Pi is for
   development only.
4. Cooling is mandatory: uncooled, the Pi 5 throttles at 80–85 °C under load;
   with the Active Cooler it peaks at 62–63 °C [RPB].

Later laptop measurement (slim report, `training/runs/custom/slim/latency.json`,
4 threads, 200 runs): DINOv2-B 96.9 ms in PyTorch / 106.4 ms in ONNX Runtime;
MobileNetV3-L 6.6 / 2.4 ms; EfficientNet-B0 9.5 / 4.3 ms.

---

## 3. Platform facts that shape the design

| Fact | What the code does about it |
|---|---|
| Pi 5: 4× Cortex-A76 @ 2.4 GHz, RTC on board, 5 V / 5 A USB-C PD [1] | 8 GB board; ML-2020 RTC cell charged by `dtparam=rtc_bbat_vchg` |
| **RPi.GPIO does not work on the Pi 5**; gpiozero uses lgpio [16][17] | Siren and GPIO service on gpiozero (`sentrix/io/actuators.py`, `gpio.py`); lgpio from apt |
| **OpenCV cannot open the CSI camera** on the Pi 5 [21][22] | `PiCameraSource` on Picamera2 (`sentrix/io/picamera.py`); USB webcams still use `CaptureSource` |
| Picamera2 and lgpio come from apt, not pip | The venv is made with `--system-site-packages` (`run.sh`, `install_pi.sh`); doctor fails the check otherwise |
| ultralytics pulls the full `opencv-python` next to the headless build; both provide `cv2` | `run.sh` and `install_pi.sh` remove the full build so one headless OpenCV < 5 remains |
| No hardware H.264 encoder on the Pi 5 [13] | No continuous stream to the cloud; remote video is MJPEG frames over the link, on demand, ≤ 15 fps |
| On a non-PD 5 V supply USB is capped at 600 mA [11] | `PSU_MAX_CURRENT=5000` in the EEPROM, or the 4G modem browns out; doctor and the installer warn |
| Throttling at 80–85 °C [11][RPB] | Active Cooler; temperature, throttle flags and EXT5V on the Node page and `/api/health` |
| GPIO is 3.3 V, not 5 V tolerant [56] | Relay through an NPN driver (§6); never feed 5 V into a GPIO pin |
| No analogue audio jack [12] | USB microphone |
| Coral USB TPU (PyCoral ≤ Python 3.9, archived) and Intel NCS2 (discontinued) [47][48][50] | Not options. Accelerator choice is Hailo or the IMX500 AI Camera |

---

## 4. Architecture as built

One device process, the same one the laptop runs (decision in docs/7 §1.2):

```
python -m sentrix  (systemd: Type=notify, Restart=always, WatchdogSec=30)
├─ capture thread      PiCameraSource (Picamera2): main 1280×720 → preview, HUD, evidence
│                      (lores 640×480 stream available; see "open" below)
├─ clip strategy       on_frame: downscale to 640 wide, motion thumbnail → ClipBuffer (15 fps)
│   ├─ inference thread   one clip per 2 s hop, stages S0–S6 (§5 T1), newest clip wins
│   └─ fast-lane thread   YOLO @320 on every 4th buffered frame, may raise L3
├─ identity worker     face recognition off the hot path (M2)
├─ preview / MJPEG     every captured frame composited with the latest overlay
├─ decision            evidence → TCI → level policy → incidents → side effects (unchanged)
├─ web (FastAPI)       Local Console on the LAN :8000, /api/node, /api/health
├─ services            gpio (LEDs, buzzer, inputs), node (telemetry, watchdog),
│                      sos, channels (outbox, modem SMS), link (Portal WSS + tunnel)
└─ siren               GpioSiren on GPIO17 (relay), pin forced low by the unit after any exit
```

Rules that matter for hardware:

* The pipeline only enqueues an idempotent SOS trigger per incident; the SOS
  service, outbox and link run in the same process.
* The siren pin is held low by `config.txt` from power-on, set low again by the
  unit's `ExecStartPre` and `ExecStopPost` (also after `kill -9`), and released
  by a timer inside the process (one on-period ≤ 90 s).
* The node service tells systemd it is ready and pets its watchdog only while
  the web event loop answers and the live loop runs. The board's hardware
  watchdog (10 s) reboots a hung kernel.
* The device accepts no inbound connection from the Internet. The Portal link
  is outbound `wss://`; the firewall admits SSH and the console from the LAN
  (and the setup hotspot) only.

The three-process layout of the first plan (capture, inference, web in
separate processes with a shared-memory ring buffer) was **not built**. It is
the fallback if Gate A fails with the link and console running (docs/7 §16 q5).

---

## 5. The techniques

T1 is built. T2, T3 and T4 are plans; none of their code exists.

### T1 — Clip engine on the Pi 5 CPU (Priority 1, built)

**Idea.** Keep the last 4 s in a ring buffer. Every hop of 2 s, analyse the window
`[t−4 s, t]` as one clip: a few keyframes for vision, the whole window for motion
and audio, the sensor events in it. Consecutive clips overlap by 2 s.

```
frames   |||||||||||||||||||||||||||||||||||||||||||||||||||||||  (15 fps buffered)
clip n   [────────────── 4 s ──────────────]
clip n+1           [────────────── 4 s ──────────────]   hop = 2 s
```

**Code:** `sentrix/pipeline/clip.py` (`ClipBuffer`, `select_keyframes`,
`ClipScheduler`, `ClipEngine`, `ClipPolicy`, `ClipRunner`, `ClipStrategy`),
`sentrix/pipeline/fastlane.py`, `sentrix/runtime/` (torch, onnxruntime and ncnn
backends; a detector on exported heads with ByteTrack ids), within-clip
confirmation in `fusion/evidence.py`, clip rows (event-log schema 4) in
`pipeline/events.py`, clip evidence bundles in `evidence/vault.py`. Settings:
`ClipConfig` in `sentrix/config.py`. Switched on by `SENTRIX_PIPELINE_MODE=clip`
(default `tick`, today's loop).

**Stage cascade per clip** (as implemented, `ClipConfig` defaults):

| Stage | Runs when | What |
|---|---|---|
| S0 gate | always | motion energy (160 px thumbnail difference), audio RMS, PIR/door events; held open 10 s after activity, during an alarm, and a full cascade at least every 10 s |
| S1 objects | gate open | person, weapon and fire heads on the keyframes not analysed by the previous clip (the shared half is reused). 6 keyframes: 4 uniform + 2 highest-motion |
| S2 scene | a person or a fire candidate | DINOv2 ViT-B/14 on up to **2** new keyframes (`scene_keyframes`) |
| S3 identity | persons present | the identity check, at most once per 2 s (a new track at once) |
| S4 action | ≥ 2 persons and clip motion ≥ 0.10 | the violence model over the two 2 s halves |
| S5 audio | always | the audio engine's own 4 s result |
| S6 fusion | always | within-clip confirmation, then the unchanged evidence → TCI → level policy → incident path |

**Confirmation.** A class is confirmed in a clip on **2 keyframes of one track**
(fire: one image region). The M1 corroboration rules are unchanged: one source
stays in the L3 band; a classifier and a detector box together reach L4/L5;
experimental classes stop at L2. `ClipPolicy` commits L4/L5 on one clip
(`min_escalate_observations=1`), with the 0.6 s dwell measured inside the clip.

**Fast lane.** The detector heads at 320 px on every 4th buffered frame
(≈ 4 Hz at 15 fps). Two consecutive hits on one track raise **L3** at once
(snapshot and evidence); a weapon hit also needs a person seen recently. It is
one detector source, so it never passes L3 alone. The heads are loaded a second
time on the configured runtime and run on their own thread. `SENTRIX_FAST_LANE=1`.

**Evidence.** The ring buffer is the pre-roll the vault never had: when an
incident seals evidence, the clip around it is sealed (5 fps), then 8 s after it
(`EvidenceVault.save_clip`).

**Runtimes and exports.** `SENTRIX_RUNTIME=torch|onnx|ncnn`. `tools/export_models.py`
writes the exports to `models/exported/` (not committed; built on the laptop and
copied) and checks parity. Measured parity (`training/runs/custom_eval/clip/export_parity.json`):
YOLO raw scores within 2.5e-5, scene probabilities within 2e-6 end to end. A
missing export falls back to torch. ONNX sessions never spin-wait. The NCNN
backend is tested only against a fake `ncnn` module: `ncnn`/`pnnx` were not
installed, so no NCNN export exists yet.

**Faces.** `SENTRIX_FACE_MODEL=buffalo_s` (SCRFD-500M + MobileFaceNet) is
supported and is the Pi default in `pi.env`. The pack was not on disk, so it is
tested only with a fake loader; its match threshold must be re-checked on
enrolled team faces.

**Slim scene model (tried, rejected).** Track T-MODEL trained MobileNetV3-L and
EfficientNet-B0 scene heads under a rule committed before any run
(`training/runs/custom/slim/PROTOCOL.md`, commit `5f8b589`). Results
(`training/runs/custom/slim/REPORT.md`):

| | LOVO macro-F1 | Val acc / macro-F1 | Weapon AUROC (OOF) | 4 threads, PyTorch / ONNX |
|---|---|---|---|---|
| DINOv2 ViT-B/14 (shipped) | **0.440** | 0.679 / 0.768 | 0.750 | 96.9 / 106.4 ms |
| MobileNetV3-L `mnv3l_full` | 0.229 | 0.357 / 0.229 | 0.607 | 6.6 / 2.4 ms |
| EfficientNet-B0 `effb0_kd` | 0.211 | 0.405 / 0.271 | 0.483 | 9.5 / 4.3 ms |

The pre-registered bar was 0.396. Distillation from DINOv2 did not help. Neither
slim model recognises fire in a video it has not seen. **S2 stays DINOv2 ViT-B/14.**
The checkpoints are in `models/v5_slim/` for development only (never in the
alarm path). DINOv2 ViT-S/14 is the untested fallback and needs its own LOVO run.

**Still open in T1:**
- The clip engine downscales the main frame; it does not yet read
  `PiCameraSource.latest_lores()`.
- S1 runs three YOLO passes per keyframe. M7's single detector (person, pistol,
  knife, fire) is not trained yet.
- Live audio: S5 uses the audio engine's 4 s result rather than buffering the
  microphone itself.

### T2 — Pi 5 + AI HAT+ (Hailo NPU) (Priority 3, plan)

Per-frame analysis on a Hailo NPU over the Pi's PCIe lane: detector at 10–15 Hz,
faces and scene at a few Hz, the CPU for tracking, audio, fusion and the web.
Expected detect → L3 ≈ 0.3–0.8 s (est.). It needs every model compiled to `.hef`
with the Dataflow Compiler (Linux x86-64, WSL2 on the laptop) and a Hailo-friendly
scene backbone (DINOv2 is not in the zoo [HFdino]). The HAT takes the PCIe lane,
so evidence stays on the microSD card. `hailo-all` needs Raspberry Pi OS Trixie
[39]. **Buy only if T1 fails Gate A.** Price in §8.

### T3 — Pi as sensor node, laptop as hub (Priority 2, plan)

The laptop runs the full, unchanged stack on a stream from the Pi (MediaMTX RTSP
720p15 [70]; SENTRIX already accepts `rtsp://` in `CAMERA_SOURCES`), and the Pi
keeps the siren, sensors and a degraded-local mode. It would be the guaranteed
demo and the M6 recording tool. **No code exists** (no MediaMTX config, MQTT
bridge or hub-side `RemoteNode`). If Gate A fails, the quickest form is the
laptop reading a USB camera itself with the Pi unused.

### T4 — Event-triggered camera (Priority 4, plan)

An IMX500 AI Camera or the PIR wakes the analysis only when a person appears;
fire is never gated on a person. Today S0 already skips S1–S4 in a still, empty
room and opens on PIR/door events. The IMX500 tensor reader is **not built**.

### Not chosen

| Option | Why not |
|---|---|
| Full per-frame stack on the Pi CPU | ≈ 0.5–0.7 Hz (§2) |
| Coral USB TPU, Intel NCS2 | Dead ends [47][48][50] |
| ESP32-S3 + laptop (docs/3) | Weak camera and microphone path |
| Jetson Orin Nano Super | Lowest risk if DINOv2-B must stay, but $399 [CNX] and not a Pi |
| Cloud inference (Pi streams video) | ₹11,200–40,600/month for a GPU + ≈ ₹1,500 of 4G data; blind when the Internet is down; everyone's footage held by the company (roadmap v2 §3) |

---

## 6. Wiring, pinout and power

### 6.1 Pinout (Pi 5 40-pin header)

The pin numbers are `settings.gpio` in `sentrix/config.py`; the code's mock pin
factory uses the same map, so a laptop run and the tests exercise it.

| Pin | GPIO | Connects to | Notes |
|---|---|---|---|
| 11 | 17 | Siren relay through an NPN transistor (1 kΩ base, 4.7 kΩ base pull-down to GND, 1N4007 across the coil) | **Active-high** (`SENTRIX_SIREN_ACTIVE_HIGH=1`, default). `gpio=17=op,dl` holds it off from power-on |
| 16 | 23 | PIR output (HC-SR505 or HC-SR501) | PIR powered from 5 V; its output is 3.3 V [57]. High = motion |
| 18 | 24 | Door reed switch (MC-38) → GND | Internal pull-up; contact closed = door closed |
| 36 | 16 | Mains sense: PC817 output → GND | Internal pull-up; low while mains is present |
| 29 / 32 / 33 | 5 / 12 / 13 | Green / amber / red LEDs, 330 Ω each | Amber kept off GPIO6 so a UPS HAT stays possible |
| 37 | 26 | Active buzzer through an NPN transistor | Chirps on level rises; `SENTRIX_BUZZER_ENABLED` |
| free pin | `SENTRIX_SIREN_ARM_PIN` | Arming key's second contact → GND (optional) | Closed = armed; a siren test is refused (409) while it is off |
| free pin | `SENTRIX_TAMPER_PIN` | Enclosure lid switch → GND (optional) | Open = tamper alert |
| 3 / 5 | 2 / 3 (I2C1) | BME280 (optional) | Kernel IIO driver; `SENTRIX_SENSOR_BME280=1` |
| 7 | 4 | DHT22 DATA (optional) | Kernel `dht11` overlay [55]; `SENTRIX_SENSOR_DHT22=1` |
| 2 / 4, 1, GND | 5 V, 3.3 V | PIR power, relay power | An opto relay board instead of the NPN driver: VCC 3.3 V, JD-VCC 5 V, jumper off |
| CAM/DISP 0 | — | Camera Module 3 Wide | Needs the **Standard–Mini 22-pin** cable; the camera's own cable does not fit a Pi 5 [24] |
| USB | — | Microphone, 4G modem | Modem needs `PSU_MAX_CURRENT=5000` (§6.3) |

Room sensors are telemetry only (Node page); they are not in the feature
contract until data exists.

### 6.2 Relay polarity and the siren

Settled (docs/7 §16 q3): with the NPN driver the relay is **active-high** —
GPIO17 high = siren on — and the 4.7 kΩ base pull-down keeps it off whenever the
pin floats. An optocoupler relay board that switches on a *low* input is
active-low: set `SENTRIX_SIREN_ACTIVE_HIGH=0` and `gpio=17=op,dh`
(`install_pi.sh` then renders `pinctrl ... dh` into the unit too). Getting this
backwards means a siren that sounds from boot.

What the code guarantees (`GpioSiren`, `sentrix/io/actuators.py`): the pin starts
off; every activation is released by a timer; one on-period never exceeds
`SENTRIX_SIREN_MAX_ON_SECONDS` (90 s; alarms use 5 s); the relay is switched off
on a normal exit; the systemd unit drives the pin low after any exit. A siren
test (`POST /api/node/siren-test`, or the link's `siren.test`) is 1 s, at most
once per minute (one shared limiter), and refused while the arming key reads off.
A real alarm drives the relay either way; the key switch on the 12 V line is
what physically silences it.

The siren runs from the fused 12 V bus through the relay's NO contact, never
from the Pi. **Before connecting the siren**, prove the relay never closes with an
LED in its place through a cold start, a reboot, `systemctl stop`, `kill -9` and
the mains-to-UPS switchover (`deploy/pi5/README.md`). 110 dB at close range
damages hearing: ear protection for every siren test.

### 6.3 Power design

```
230 V wall → surge strip → 12 V adapter (BIS-marked, ≥ 4 A) → router mini UPS 12 V 3 A (outside the box)
                                    │                                │
                                    └─ PC817 opto → GPIO16           └─ fused 12 V bus (UPS-backed)
                                       (taps the adapter output,          ├─ F1 3 A → XY-3606 buck 12 V → 5.2 V, 6 A
                                        before the UPS: mains present)    │           → short thick USB-C (5 A) → Pi 5
                                                                          └─ F2 1 A → arming key → relay NO → 12 V siren
```

* The siren shares the UPS-backed bus, so it still works in a power cut; each
  branch has its own fuse. 230 V stays inside sealed adapters; everything in the
  box is 12 V or below. Keep the UPS and its lithium cells outside the box.
* The buck converter is not a USB-PD supply, so set `PSU_MAX_CURRENT=5000` with
  `sudo rpi-eeprom-config --edit`. Without it the Pi limits USB devices to
  600 mA and the 4G modem browns out. If it still browns out, add a powered USB
  hub (≈ ₹800 est.).
* Bench bring-up uses the official 27 W supply; the 12 V path comes in W4.
* **Pass test:** EXT5V (`vcgencmd pmic_read_adc`) ≥ 4.8 V under full load with the
  siren pulsing and the modem sending; `vcgencmd get_throttled` = `0x0`; 10 mains
  pulls with no reboot. UPS runtime ≈ 2 h+ is an estimate, to be measured.
* Mains loss: amber LED fast blink, one long beep, a `node_alert` event, and
  `node.alert` to the Portal. Over-temperature and under-voltage raise one alert
  each until they clear.

### 6.4 The 4G modem

| | |
|---|---|
| Modem | 7Semi Quectel **EC200U-CN** USB dongle (Indian bands B1/3/5/8/40/41 + GSM). Alternative: Waveshare SIM7600G-H. Not A7670E (no B40) |
| SIM | **Airtel or Vi** prepaid in a team member's name. **Not Jio**: Jio is 4G/VoLTE only, and SMS from these modules may fail without IMS registration |
| Data (failover) | NetworkManager profiles: Wi-Fi route metric 600, modem 700, set explicitly (an ECM dongle would otherwise be an Ethernet device at metric 100 and carry everything). The connectivity check adds +20000 to a link without Internet, so 4G takes over. `install_pi.sh --modem-apn <apn>` (ModemManager, e.g. Airtel `airtelgprs.com`, Vi `www`) or `--modem-ecm` |
| SMS (Tier 1) | `sentrix/alerting/channels/modem.py`: AT commands over pyserial, PDU mode (GSM-7, Hindi as UCS-2, long messages joined, delivery reports); `SENTRIX_MODEM_PORT` empty = probe, a device path, or `mmcli` when ModemManager owns the port. Only numbers in the current profile (owner, confirmed contacts, Desk), ≤ 30 segments a day |
| Switches | `SENTRIX_MODEM_ENABLED=1` builds the worker. A real send needs `SENTRIX_LIVE_ALERTS=1` **and** `SENTRIX_MODEM_LIVE=1`; in test mode (the only mode this semester) only `SENTRIX_TEST_RECIPIENTS` receive anything |
| Tested | Against a fake serial port that speaks AT. Not on a real modem: PDU mode, delivery-report routing, Jio IMS and the AT port number are to check in W1 on the laptop |

Keep a spare Android phone as a documented fallback SMS sender.

### 6.5 LEDs and buzzer

At start the three LEDs light for 0.5 s (lamp test). Then (`led_patterns`,
`sentrix/io/gpio.py`):

| LED | Off | Slow blink | Fast blink | Solid |
|---|---|---|---|---|
| Green | camera offline, or link enabled and not connected | connected, not yet claimed by an owner | — | camera online and link off or claimed |
| Amber | all well | Wi-Fi setup mode | fault: camera offline, critical temperature, under-voltage, on battery, tamper | link enabled but not connected |
| Red | below L3 | committed L3, or an SOS request open | L4 | L5, or an approved SOS that could not be delivered |

Buzzer: 1/2/3 short chirps on a rise to L2/L3/L4 (L5 is the siren's job), one
chirp when the Portal claims the device, one long tone when an approved SOS
fails, one long beep on mains loss, three on tamper. A restart is quiet.

---

## 7. Gates

**Gate A — T1 on the Pi (decides whether to buy an AI HAT+), W3:**
1. clip compute p95 ≤ 1.6 s at hop 2 s; ≤ 2 % clips dropped over 1 h;
2. fast-lane detect → L3 p95 ≤ 2 s; clip detect → L4 p95 ≤ 6 s;
3. on the 10 custom videos in clip mode: episodes caught at L3 ≥ the M5 result
   minus one (M5: weapon 9/12, fire 3/3) and 0 false onsets;
4. 1 h soak: CPU ≤ 75 °C, no throttling flags, memory flat;
5. all of it **with the link and the Local Console running** in the one-process
   layout.

Fail → Hailo AI HAT+ (T2) or laptop-hub mode (T3), and revisit the multi-process
layout.

**Status on 2026-10-15.** Laptop numbers from `training/runs/custom_eval/clip/`
(code `575e463`, shipped models, 4 threads, evaluation only, nothing tuned; the
bench ran while other training jobs loaded the machine, and timings swung about
2× between runs):

| Criterion | Measured on the laptop (4 CPUs) | Pi |
|---|---|---|
| 3. Replay episodes and false onsets | **Met.** Weapon at L3 9/12 with the fast lane (8/12 without; M5 9/12), fire 3/3, 0 false onsets at every level. Weapon at L5 5/12 (M5 2/12). Robbery no longer reaches L2 (M5: L2) | re-run on Pi exports |
| 1. Clip compute p95 ≤ 1.6 s | **Not met.** ONNX + fast lane: p50 1060 / p95 2145 ms (60 s bench, `7.mov` as the camera). Torch + fast lane: p95 6473 ms, 70 % dropped. ONNX without the fast lane: p95 3797 ms, 38 % dropped (a loaded run) | not measured |
| 1. ≤ 2 % dropped over 1 h | 0 dropped in the 60 s ONNX + fast-lane run; no 1 h run | not measured |
| 2. Fast-lane L3 p95 ≤ 2 s | **Not met in replay.** Weapon L3 median 1.00 s, p95 7.21 s, 8 of 12 within 2 s (clip-end stamps; live adds compute) | not measured |
| 2. Clip L4 p95 ≤ 6 s | Met in replay: weapon p95 3.46 s (6/12 caught), fire 0.46 s (2/3); compute not included | not measured |
| 4. 1 h soak | — | needs the Pi |
| 5. With link and console | — | needs the Pi |

Where the time goes (ONNX + fast lane bench): S1 objects p50 636 / p95 1344 ms
(three YOLO passes per keyframe), S2 scene p50 401 / p95 507 ms (DINOv2-B on 2
keyframes), fast-lane pass p50 61 / p95 115 ms at 3.75 passes/s. Scaled to the
Pi (§2), the current models will not fit: Gate A needs M7's single detector,
probably one scene keyframe per clip (the slim report's suggestion), and NCNN.
Tools for the Pi run: `tools/bench_clip.py --runtime onnx --fast-lane --seconds
3600 --json ...` (side effects stubbed, refuses to run with live services) and
`tools/replay_custom_videos.py --mode clip --fast-lane --threads 4`.

**Gate B — T2 (if bought):** all networks on the NPU at ≥ 10 Hz combined, detect
→ L4 p95 ≤ 2 s, HEF accuracy within 2 points of the PyTorch models on val.

The product gates P1–P8 (parity, isolation, SOS, latency, offline, privacy,
no alerts in tests, prompt budget) are in docs/7 and roadmap v2 §13. P5
(offline: local prompt and modem SMS work; reconnect syncs every queued event)
needs the modem on the Pi.

---

## 8. Bill of materials

Prices from Indian retailers, checked on 2026-10-14 (roadmap v2 §9); ≈ marks a
price that could not be confirmed. Re-check before ordering.

**Order 1 — bench kit (order first)**

| Item | Where | Price |
|---|---|---|
| Raspberry Pi 5 **8 GB** | thingbits (in stock) | ₹19,852 |
| Official 27 W USB-C power supply | Robocraze | ₹1,255 |
| Official Active Cooler (mandatory) | thingbits | ₹531 |
| microSD SanDisk **High Endurance** 128 GB | Flipkart | ₹3,699 |
| Camera Module 3 **Wide** (102°) | Robocraze | ₹4,379 |
| Pi 5 camera cable, 22-pin Standard–Mini | thingbits | ₹117 |
| RTC cell ML-2020 (rechargeable; never a CR2032) | — | ≈ ₹500 |
| USB microphone | Robocraze | ₹289 |
| 1-channel relay module + 2N2222/BC547 transistors + 1N4007 diodes | Robocraze | ≈ ₹150 |
| PIR HC-SR505 (or HC-SR501, ₹54–59) | Robocraze / thingbits | ₹73 |
| MC-38 door reed switch | Robocraze | ₹52 |
| Breadboard, jumpers, LEDs, resistors, active buzzer | — | ≈ ₹350 |
| **Subtotal** | | **≈ ₹31,250** |

Cheaper: Camera Module 3 Standard (₹3,245) saves ₹1,134; a 64 GB card (₹2,449)
saves ₹1,250. A 4 GB Pi 5 at ₹12,000–12,300 (KSP Electronics, unconfirmed) would
save ≈ ₹7,500; the only 4 GB confirmed in stock costs ≈ ₹18,490.

**Order 2 — integration kit (power backup, siren, 4G modem, box); order the modem in W0**

| Item | Where | Price |
|---|---|---|
| 12 V adapter, ≥ 4 A, **BIS-marked** (check its R-number on crsbis.in) | e.g. NOVEL 12 V 5 A | ₹566 |
| Router mini UPS **12 V 3 A** | Resonate CRU12V3A Pro | ₹2,399 |
| Step-down converter 12 V → 5.2 V, 6 A (fixed output) | XY-3606, Robocraze | ₹119 |
| Short, thick USB-C cable, converter to Pi (5 A) | — | ≈ ₹150 |
| 12 V siren, low current (≈ 110 mA) | SQR117, Robocraze | ₹194 |
| Key or toggle **arming switch** for the siren line | — | ≈ ₹50 |
| 3 inline fuse holders + fuses; PC817 optocoupler | — | ≈ ₹80 |
| **4G USB modem**, Indian bands | 7Semi Quectel EC200U-CN, Evelta / Robocraze | ₹2,236 |
| Airtel or Vi prepaid SIM (not Jio) | ₹199 / 28 days (2 GB + 100 SMS/day) | ≈ ₹300 |
| ABS box, IP65, ≥ 200 × 120 × 75 mm; serial/QR label | — | ₹300–899 |
| 40 mm 5 V fan, insect mesh, 4 cable glands | Robocraze (fan ₹49) | ≈ ₹150 |
| Wall/camera mount, perfboard, screw terminals | — | ≈ ₹450 |
| **Subtotal** | | **≈ ₹7,300** |

Tools (multimeter ₹515, soldering kit ₹510, wire stripper, heat-shrink,
micro-HDMI cable ₹449, card reader, 12 V panel meter ₹125, earplugs): ≈ ₹2,000.
Strongly recommended: a spare 64 GB High Endurance card (₹2,449) for a clone of
the finished setup.

**Running costs**

| Cost | One-time | Per month / year |
|---|---|---|
| Device (Order 1 + Order 2 + tools) | **≈ ₹40,500** (cheapest ≈ ₹36,000; ≈ ₹28,300 with a confirmed ₹12,300 4 GB Pi) | — |
| SIM in the modem | — | ≈ ₹200/month |
| Demo hotspot phone data (never campus Wi-Fi) | — | ≈ ₹200–300/month (est.) |
| Portal: Oracle Always Free VM, Postgres, Caddy HTTPS | ₹0 | ₹0 |
| `.in` domain (DuckDNS may be blocked by campus filters) | — | ≈ ₹500–800/year |
| E-mail API, Telegram bot; fallback Render + Supabase | ₹0 | ₹0 |
| Powered USB hub, only if the modem browns out | ≈ ₹800 (est.) | — |

**Buy later, only if needed**

| Item | Price | Buy only when |
|---|---|---|
| AI HAT+ 13 / 26 TOPS (Probots) | ₹9,749 / ₹14,999 | The Pi fails Gate A |
| Camera Module 3 NoIR + 850 nm IR illuminator | ₹3,379 + ≈ ₹200–2,000 | Night operation is required (and IR training data) |
| Geekworm X1202 UPS HAT + branded cells | ₹5,299 + cells | The router UPS fails its power tests |
| MQ-2 smoke sensor + ADS1115 converter | ₹98 + ₹142 | A second fire signal is wanted (log-only first) |

**Do not buy:** a Jio SIM for the modem; A7670E modems (no band B40); unbranded
18650 cells; open-frame 230 V power supplies; an AI HAT before Gate A; Coral or
Intel NCS2.

---

## 9. Software (repository map)

| Part | Where | State |
|---|---|---|
| Picamera2 source: main 1280×720 + lores 640×480 from one frame, sensor timestamps, reconnect, Pi 4 YUV fallback; `CAMERA_SOURCES=picamera:0` | `sentrix/io/picamera.py`, `io/sources.py` | built; fake `picamera2` only |
| Siren on gpiozero: polarity, timer release, 90 s cap, arming, `siren_test` | `sentrix/io/actuators.py` | built; mock pins only |
| GPIO service: LEDs, buzzer, PIR, door, mains, arming key, tamper; 50 ms software debounce; mock pins and a visible error when lgpio fails | `sentrix/io/gpio.py` | built; debounce to check on real inputs |
| Board telemetry: CPU temperature, throttle flags, EXT5V, uptime, BME280/DHT22 (IIO); alerts; systemd ready + watchdog | `sentrix/io/node.py`, `sensors.py`, `watchdog.py` | built; `pmic_read_adc` format to check on the firmware |
| Wi-Fi scan/connect/status for `/setup/wifi` | `sentrix/io/network.py` | built; fake `nmcli` only |
| Local Console API: `GET /api/node`, `POST /api/node/siren-test`; Node page (telemetry, inputs, relay, clip status, test siren) | `sentrix/web/node_routes.py`, templates | built |
| Clip engine, fast lane, runtimes, exports, bench | `pipeline/clip.py`, `pipeline/fastlane.py`, `sentrix/runtime/`, `tools/export_models.py`, `tools/bench_clip.py`, `replay_custom_videos.py --mode clip` | built; laptop only; NCNN untested |
| Modem SMS and ringing call | `sentrix/alerting/channels/modem.py` | built; fake serial port only |
| Doctor hardware section (venv, camera enumerated not opened, OpenCV, temperature, throttling, EXT5V, config.txt lines, `PSU_MAX_CURRENT`, unit, network) | `tools/doctor.py`, `node.hardware_report` | built; one "skipped" row on a laptop |
| Installer, systemd unit, journald caps, watchdog, config.txt snippet, Pi env, NetworkManager, polkit, firewall | `deploy/pi5/`, `run.sh` (Pi path), `requirements-pi.txt` | built; checked by `tests/test_hw_deploy.py` |
| Profile label `SENTRIX_PROFILE=laptop\|pi5-clip\|pi5-hailo\|pi5-node\|hub` | `sentrix/config.py` (`NodeConfig`) | a label on the Node page and a doctor hint only; it changes no behaviour |
| Shared-memory ring buffer, three processes | — | not built (one-process decision) |
| Hailo backend, HEF scripts | — | not built (T2) |
| Node agent (MediaMTX, MQTT), hub `RemoteNode` | — | not built (T3) |
| IMX500 tensor reader | — | not built (T4) |
| Single YOLO detector (person, pistol, knife, fire) | — | not trained (M7) |

All hardware tests: `tests/test_hw_*.py`, `tests/test_clip_*.py`,
`tests/test_model_slim.py`.

**Security on the unit:** SSH keys only; ufw admits SSH and the console from the
LAN (and the setup hotspot); the console is plain HTTP on the LAN (accepted for
the demo, stated in the report); remote access only through the Portal tunnel.
The Pi has no TPM: keep a copy of `EVIDENCE_AES_KEY` and `.env` off the device
(losing the key makes every bundle unreadable). Device identity lives in
`/etc/sentrix` (outside `var/`, kept by a factory reset).

---

## 10. Roadmap

Device track and the connectivity-and-cloud phase, mapped to the 9-week plan of
roadmap v2 (W0–W8).

| Phase | Week | Work | Done when | State |
|---|---|---|---|---|
| H0 Plan | W0 | this file; decisions D8–D14; order parts incl. 4G modem + SIM | parts ordered | plan done; parts not ordered |
| H1 Laptop prep | W1 | clip engine + fast lane; ONNX exports; gpiozero with mock pins; `PiCameraSource`; clip replay and bench; modem tested on the laptop with an Airtel/Vi SIM | clip replay ≥ M5 minus one, 0 false onsets; SMS out from the laptop | software done (§9); replay met; budget not met; modem not tested |
| C Connectivity and cloud | W0–W1 | Oracle VM, `.in` domain, Docker, Caddy HTTPS, backups + tested restore; reachable from 4G and the demo room (`deploy/portal/README.md`) | HTTPS live, restore tested, fallback deploy ≤ 30 min | files written and tested statically; nothing deployed |
| H2 Bench bring-up | W2 | OS, cooler, RTC; camera, mic, PIR, door, LEDs; relay test with an LED; doctor; "record normal session" | every input and output verified on the bench | not started |
| H3 Gate A | W3 | NCNN/ONNX models on the Pi, clip engine, fast lane, link running; `bench_clip` and a 1 h soak | **Gate A**; go/no-go 1 | not started |
| H4 Power and alarm | W4 | 12 V UPS path, buck, fuses, siren relay; modem on the Pi; offline SMS path | power pass test (§6.3); offline SMS works on the bench | not started |
| H5 Data | W3–W5 | M6 sessions recorded **with the Pi camera** | dataset in the `frames.csv` schema | not started |
| H6 Upgrade or optimise | after Gate A | fail → T2 (HEFs, Gate B) or T3; pass → M7 single YOLO, T4 for 24/7 | gate passed | — |
| H7 Deploy | W6–W8 | box and mount at 1.8–2.2 m, burn-in, code freeze end of W6, M9 gates on S4, rehearsals | G0–G5 (clip-mode G4), P1–P8 | — |

---

## 11. Open questions for the team

1. **Budget:** is ≈ ₹40,500 for the device approved (bench kit ≈ ₹31,250 +
   integration kit ≈ ₹7,300 + tools)? Phone KSP about the 4 GB Pi first?
2. **Card and SIM owner:** who holds the Oracle account and card, and the SIM
   (KYC, recharges)?
3. **Darkness:** D5 keeps darkness out of scope. Night operation needs the NoIR
   camera, an IR illuminator and IR training data.
4. **Alert latency:** is "L3 within ≈ 2 s, siren within ≈ 6 s" acceptable for the
   demo, or must the siren fire within 2 s (→ T2)?
5. **Hub at the demo:** may the laptop be present as a fallback?
6. **Arming key:** which free GPIO carries its second contact (optional)?

---

## Sources

[1] https://www.raspberrypi.com/products/raspberry-pi-5/ ·
[11] https://www.raspberrypi.com/documentation/computers/raspberry-pi.html ·
[12] https://www.raspberrypi.com/news/introducing-raspberry-pi-5/ ·
[13] https://www.raspberrypi.com/documentation/computers/camera_software.html ·
[16] https://gpiozero.readthedocs.io/en/stable/api_pins.html ·
[17] https://gpiozero.readthedocs.io/en/stable/changelog.html ·
[21] https://github.com/opencv/opencv/issues/21653 ·
[22] https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf ·
[24] https://www.raspberrypi.com/documentation/accessories/camera.html ·
[39] https://www.raspberrypi.com/documentation/computers/ai.html ·
[47] https://coral.ai/docs/accelerator/get-started/ ·
[48] https://github.com/google-coral/edgetpu ·
[50] https://www.intel.com/content/www/us/en/support/articles/000093181.html ·
[55] https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/README ·
[56] https://pinout.xyz/pinout/gpio ·
[57] https://components101.com/sensors/hc-sr501-pir-sensor ·
[70] https://github.com/bluenviron/mediamtx

[U8] https://raw.githubusercontent.com/ultralytics/ultralytics/v8.2.50/docs/en/guides/raspberry-pi.md ·
[U11] https://raw.githubusercontent.com/ultralytics/ultralytics/v8.3.150/docs/en/guides/raspberry-pi.md ·
[HZ] https://github.com/hailo-ai/hailo_model_zoo/tree/master/docs/public_models ·
[HF105] https://community.hailo.ai/t/official-fps-benchmark-on-hailo-8-using-raspberry-pi-5/18873 ·
[HFdino] https://community.hailo.ai/t/deployment-of-dinov2-model-from-torch-hub/7720 ·
[IJ] https://www.ijert.org/comparative-benchmarking-of-cpu-gpu-and-npu-architectures-for-real-time-yolov8n-inference-on-embedded-edge-platforms-ijertv15is030232 ·
[NCNN] https://github.com/Tencent/ncnn/blob/master/benchmark/README.md ·
[ARM] https://huggingface.co/Arm/deit-tiny-int8-xnnpack-executorch ·
[THESIS] https://sipora.polije.ac.id/55744/1/Abstract.pdf ·
[RPB] https://www.raspberrypi.com/news/heating-and-cooling-raspberry-pi-5/ ·
[CNX] https://cnx-software.com/2026/07/22/nvidia-increases-the-price-of-jetson-modules-and-devkits-by-up-to-101

Prices (roadmap v2 appendix, checked 2026-10-14): Pi 5 8 GB
https://thingbits.in/products/raspberry-pi-5-computer · Active Cooler
https://thingbits.in/products/official-active-cooler-for-raspberry-pi-5 · 27 W
supply https://robocraze.com/products/raspberry-pi-5-27w-usb-c-power-supply-black-colour ·
Camera Module 3 Wide https://robocraze.com/products/raspberry-pi-camera-module-3-wide ·
camera cable https://www.thingbits.in/products/raspberry-pi-5-camera-cable-22-pin-to-15-pin ·
XY-3606 https://robocraze.com/products/24v-12v-to-5v-5a-power-module-dc-dc-xy-3606-step-down-converter-with-usb-fast-charging ·
router UPS https://www.flipkart.com/search?q=resonate+routerups · EC200U-CN
https://evelta.com/7semi-ec200u-4g-lte-cat-1-dongle-gnss-positioning/ · AI HAT+
https://probots.co.in/official-raspberry-pi-ai-hat-13-tops.html · `.in` tariff
https://registry.in/tariff · BIS registration https://www.crsbis.in/BIS/products-bis.do ·
Jio SMS on LTE modules
https://forums.quectel.com/t/ec25-connected-with-jio-sim-its-not-sending-and-receiving-messages/7008

Laptop measurements in §2: `torch.set_num_threads(4)` / ONNX Runtime 4 intra-op
threads on an Intel i7-14650HX, median of 12 runs, models as shipped on `main`
(2026-10-10). Clip and slim numbers: the report files named in §5 and §7.
