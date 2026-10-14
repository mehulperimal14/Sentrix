# HARDWARE.md — Integrating SENTRIX with a Raspberry Pi 5

**Status: planning.** No hardware has been bought yet. This file is the working plan
for moving SENTRIX from a laptop onto a camera module + Raspberry Pi 5 edge unit.
It gives the targets, the measured compute problem, four integration techniques
(each complete enough to build), their priority order, the parts list per
technique, the wiring, the software changes, and the test plan that can start
before any part arrives.

Read with: `CLAUDE.md` (project state, milestones M6–M9), `docs/3_HARDWARE_SPECIFICATION_AND_BOM.md`
(the original ESP32 / Orange Pi plan this supersedes), `sentrix/io/` (the code seams
hardware plugs into). Facts were checked on 2026-10-10; numbers in `[n]` point to
the sources at the end. "est." marks our own arithmetic, "unverified" marks a fact
we could not confirm — re-check both when the parts arrive.

---

## 0. Decision summary

| | |
|---|---|
| Core idea | Analyse a **rolling 4-second context window** (video + audio + sensors) as one unit, instead of running every model on every frame |
| Main track (Priority 1) | **T1 — Clip Engine on the Pi 5 CPU**: one decision every 2 s from the last 4 s, a cheap per-frame "fast lane", slimmed models in NCNN/ONNX |
| Safety net (Priority 2) | **T3 — Pi as sensor node, laptop as hub**: the unchanged SENTRIX stack runs on the laptop; the Pi captures, gates, records and drives the siren |
| Upgrade path (Priority 3) | **T2 — Pi 5 + AI HAT+ (Hailo NPU)**: per-frame detection at 15–30 Hz; buy only if T1 misses its gate (§7) |
| Add-on (Priority 4) | **T4 — Event-triggered smart camera** (AI Camera IMX500 / PIR wake): the Pi sleeps until a person appears; for 24/7 power and heat |
| Buy first | Pi 5 (8 GB) + 27 W PSU + Active Cooler + Camera Module 3 + USB mic + relay/siren/PIR/LEDs (≈ ₹30,500, §8). Everything for T1 and T3 |
| Do before buying anything | Build the clip engine and the model exports on the laptop, emulating a 4-core budget (§10, H1) |

The original `₹20,000` budget (mid-semester report) is no longer reachable with a
Pi 5: memory-driven price rises took the 8 GB board to about ₹19,600–20,000 in India
on its own [7][8][9]. §8 gives a 4 GB variant for a tighter budget.

---

## 1. What the unit must do

From the proposal, the mid-semester report and the CLAUDE.md gates:

| Requirement | Target | Source |
|---|---|---|
| Detect handgun (replica), knife, visible flame; log experimental classes | per class ≥ 27/30 episodes caught | CLAUDE.md D1, gate G2 |
| False alarms in an ordinary home | 0 L4/L5 in ≥ 3 h normal footage, ≤ 1 L3/h | gate G3 |
| Detect → alert | p95 ≤ 2 s (L3); revisited for clip mode in §7 | gate G4 |
| Autonomy | Siren and evidence keep working with no network or PC | report §1.5 |
| Inputs | Camera (1080p capable, low light), microphone 16 kHz, PIR, door contact, temperature | report §2.2.3.2, docs/3 |
| Outputs | 12 V 110 dB siren via relay, status LEDs/buzzer, dashboard, SMS (when online) | docs/3 §5 |
| Evidence | AES-256-GCM sealed clip around the trigger, correct time even offline | M3 |
| Privacy | Raw video stays on the device or LAN; snapshots only from L3 | D4 |

---

## 2. The compute problem, measured

SENTRIX's live stack today (`main`): YOLOv8n person (+ByteTrack), YOLO11n weapon and
fire heads, DINOv2 ViT-B/14 scene classifier, InsightFace `buffalo_l`
(SCRFD-10G + ArcFace R50), a 4-class audio CNN, all on a laptop CPU at 5.6 Hz.

Measured on the laptop (Intel i7-14650HX) **limited to 4 threads** to mimic the
Pi 5's 4 cores, against published Raspberry Pi 5 figures:

| Model (as shipped) | Compute | Laptop, 4 threads | Pi 5 CPU | Pi 5 + Hailo-8 |
|---|---|---|---|---|
| YOLOv8n person @640 | 8.9 GFLOPs, 3.2 M params | 24 ms (12 ms @320) | **94 ms NCNN**, 509 ms PyTorch [U8] | ≈ 60 FPS app-level [IJ] |
| YOLO11n weapon / fire @640 | 6.5 GFLOPs, 2.6 M | 21–24 ms (10–11 @320) | **81–88 ms** OpenVINO/NCNN, 405 ms PyTorch [U11]; @320 ≈ 25–35 ms est. | 105 FPS measured on a Pi [HF105] |
| YOLO26n @640 (newer) | — | — | **67 ms NCNN** [U26] | — |
| DINOv2 ViT-B/14 scene @224 | 43.9 GFLOPs, 85.7 M | 83 ms | **≈ 0.6–1.05 s** est. [NCNN][ARM] | not in the Hailo zoo; generic vit_base 22–29 FPS on an x86 host [HZ] — risky |
| DINOv2 ViT-S/14 @224 | 11.0 GFLOPs, 21.6 M | 26 ms | ≈ 0.15–0.27 s est. | compiles only with a community script [HFdino] |
| EfficientNet-B0 @224 | 0.77 GFLOPs, 4.0 M | 11 ms | **12.3 ms** ncnn [NCNN] | > 1000 FPS [HZ] |
| MobileNetV3-L @224 | 0.43 GFLOPs, 4.2 M | 7 ms | **7.4 ms** ncnn [NCNN] | > 1900 FPS [HZ] |
| ResNet-18 (violence), per frame | 3.6 GFLOPs | 12 ms (126 ms for 16) | 20 ms ncnn → 0.32 s for 16 [NCNN] | > 1000 FPS [HZ] |
| SCRFD-10G face det @640 + ArcFace R50 | ~10 GFLOPs + R50 | 54 ms + 58 ms/face | `buffalo_l` ≈ 0.5–0.6 FPS with several faces [THESIS] | 440 / 113 FPS zoo [HZ] |
| SCRFD-500M + MobileFaceNet (`buffalo_s`) | ~0.5 GFLOPs + small | — | ≈ 1.8–2.4 FPS with several faces [THESIS] | 1332 / 5191 FPS zoo [HZ] |
| Audio CNN, 4 s log-mel | 1.2 M params | < 5 ms | < 20 ms est. | — (CPU) |

What this means:

1. **The current per-frame stack would run at ≈ 0.5–0.7 Hz on a Pi 5 CPU** (est.:
   three YOLO passes ≈ 0.25 s + DINOv2-B 0.6–1 s + faces ≈ 0.5 s) — the rate at which
   the first live test failed (CLAUDE.md §5).
2. **Convolutional nets port well** (≈ 4× slower than the laptop's 4 threads with
   NCNN). **Transformers port badly** (≈ 8–12×): DINOv2-B is the single biggest
   obstacle. Every technique below either shrinks it, runs it rarely, or moves it off
   the Pi.
3. **Runtime matters as much as the model**: the same YOLO is 5× faster in NCNN than
   in PyTorch on the Pi [U8][U11]. PyTorch is for development only on the Pi.
4. **Cooling is not optional**: uncooled, the Pi 5 sits above 85 °C and throttles
   continuously under 4-core load; with the Active Cooler it peaks at 62–63 °C [RPB].

---

## 3. Platform facts that shape the design

| Fact | Consequence for SENTRIX |
|---|---|
| Pi 5: 4× Cortex-A76 @ 2.4 GHz, RAM 1–16 GB, RTC on board, 5 V/5 A USB-C PD [1] | 8 GB recommended (torch for development + models + buffers). Fit the RTC battery so evidence timestamps survive power loss offline |
| **RPi.GPIO does not work on the Pi 5**; gpiozero uses lgpio (≥ 2.0.1.post3), or the `rpi-lgpio` shim [16][17][18] | `sentrix/io/actuators.py` `GpioSiren` imports `RPi.GPIO` → it would silently fall back to a console siren. Rewrite on gpiozero (§9) |
| **OpenCV cannot open the CSI camera** on the Pi 5; capture with Picamera2 `capture_array()` [21][22][23] | New `PiCameraSource(FrameSource)`; USB webcams still work through the existing `CaptureSource` |
| **No hardware H.264 encoder** on the Pi 5; software encode only [13][15] | Streaming/recording costs CPU. T3 streams one modest stream (720p) or sends event clips; T1 records evidence clips as JPEG sequences or low-rate MP4 |
| One PCIe 2.0 x1 lane (Gen 3 possible, uncertified) [1][11] | An AI HAT+ and an NVMe SSD cannot share it without a switch board [45]. With T2, put evidence on a USB 3 SSD or high-endurance microSD |
| 27 W PSU needed; on a 3 A supply USB is capped at 600 mA [11] | Use the official 27 W supply; the siren gets its own 12 V supply |
| Throttling 80–85 °C; Active Cooler $5 keeps 62–63 °C [11][RPB] | Active Cooler mandatory; thermal state goes to `/api/health` and the dashboard |
| Raspberry Pi OS Trixie (Debian 13, Python 3.13) is current; Hailo now requires Trixie [19][39] | SENTRIX already supports Python 3.10–3.14. Picamera2 comes from apt → create the venv with `--system-site-packages` |
| No analogue audio jack [12] | Microphone over USB (recommended) or I2S (INMP441 recipe exists but is unverified on the Pi 5 [52]) |
| GPIO is 3.3 V, not 5 V tolerant [56]; 5 V opto relays may stick on at 3.3 V [60] | Relay with separate JD-VCC or a transistor driver (§6) |
| Coral USB TPU: PyCoral only for Python ≤ 3.9, repo archived 2026 [47][48]; Intel NCS2 discontinued [50] | Not options. The accelerator choice is Hailo (AI HAT+ / AI HAT+ 2) or the IMX500 AI Camera |

---

## 4. The common architecture (all techniques)

Every technique uses the same unit and the same software seams; they differ in
**where** the models run and **how often**.

```
                         Raspberry Pi 5 edge unit
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ Capture process (always on, light)                                        │
 │   Camera Module 3 ──Picamera2──► main stream 1280×720 (preview/evidence)  │
 │                               └► lores stream 640×480 (analysis)          │
 │   USB mic 16 kHz mono ─────────► audio ring buffer                        │
 │   PIR / door reed / BME280 ────► sensor events + readings                 │
 │   Ring buffer: last 4–8 s of frames (+ JPEG main frames), audio, sensors  │
 │   Motion energy per frame (cheap) ─► activity flag                        │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ Analysis (technique-specific)                                             │
 │   T1: Clip Engine on CPU (NCNN/ONNX)   T2: per-frame on Hailo NPU         │
 │   T3: forward stream/clips to the hub  T4: wake on IMX500/PIR person      │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ Decision: evidence → TCI → LevelPolicy → IncidentManager (existing code)  │
 │ Actions: gpiozero siren relay, LEDs, buzzer; evidence vault; SMS if online│
 │ Web: FastAPI dashboard + /api/health (thermal, throttling, rates)         │
 └──────────────────────────────────────────────────────────────────────────┘
```

**Three processes, not threads**, so Python's GIL does not let capture starve
inference: `sentrix-capture` (Picamera2, audio, GPIO inputs, ring buffer in shared
memory), `sentrix-infer` (models), `sentrix-web` (dashboard). Each is a systemd
service with `Restart=always`; the Pi's hardware watchdog reboots a hung unit.

**What already exists and is reused unchanged:** the evidence layer
(`fusion/evidence.py`), TCI (`fusion/tci.py`), level policy, incidents, evidence
vault, event log, dashboard, per-track identity, the side-effect-free evaluation
harness. **What is new** is listed per technique and collected in §9.

---

## 5. The techniques

Each technique below is complete: how it works, the per-cycle budget, latency, the
parts, the code, how to test it, its risks, and when to choose it.

### T1 — Clip Engine: 4-second context window on the Pi 5 CPU  (Priority 1)

**Idea.** Keep the last 4 s of everything in a ring buffer. Every **hop of 2 s**,
cut the window `[t−4 s, t]` into a *clip* and analyse the clip as one unit: a few
well-chosen keyframes for vision, the whole 4 s for audio and motion, the sensor
events in the window. Each clip produces one set of scores; consecutive clips
overlap by 2 s, so nothing falls between them. This turns "run every model 30 times
a second" (impossible on the Pi) into "run each model a handful of times every 2 s"
(fits, §2).

It also fits SENTRIX's models naturally: the audio classifier already scores a
4.0 s window with a 1 s hop, and the violence model already samples 16 frames over
2 s (`config.py` `WindowConfig`).

```
time ─────────────────────────────────────────────────────────────────►
frames   |||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||||  (15 fps)
clip n   [────────────── 4 s ──────────────]
clip n+1           [────────────── 4 s ──────────────]        hop = 2 s
decision                                    ▲ n            ▲ n+1
```

**Stage cascade per clip** (cheap stages decide whether expensive ones run):

| Stage | Runs when | Model (Pi runtime) | Calls / clip | Pi 5 time / clip |
|---|---|---|---|---|
| S0 gate | always | motion energy (frame diff on 320×240, computed during capture), audio RMS, PIR/door state | — | ≈ 0 (precomputed) |
| S1 objects | S0 active | **one unified YOLO11n/YOLO26n** (person, pistol, knife, fire) NCNN @640 | 6 keyframes | 0.40–0.55 s |
| S2 scene | S1 saw a person or a fire candidate | EfficientNet-B0 scene head (or DINOv2-S/14) | 2 keyframes | 0.03 s (0.3–0.55 s) |
| S3 identity | persons present; ≤ 1 per 2 s | SCRFD-500M + MobileFaceNet (`buffalo_s`) on person crops | 2 keyframes | ≈ 0.1–0.15 s est. |
| S4 action | ≥ 2 persons and high motion | ResNet-18 per frame, 16 frames (existing violence model) | 16 | ≈ 0.32 s |
| S5 audio | always | log-mel + audio CNN over the full 4 s | 1 | < 0.02 s |
| S6 fusion | always | evidence → TCI → policy (existing) | 1 | < 0.01 s |
| **Total** | idle room / 1–2 people / fight | | | **≈ 0.05 / 0.6–0.8 / 1.1–1.6 s** |

**Keyframe selection.** 6 keyframes per clip: 4 uniformly spaced (2 fps) plus the 2
frames with the highest motion energy (catches a brief gesture between the uniform
ones). Keyframes come from the `lores` 640×480 stream; evidence uses the matching
`main` frames.

**Confirmation moves inside the clip.** Today a source must cross its threshold on
2 of 3 *ticks*. In clip mode the 4 s window already supplies time: a class is
*confirmed in a clip* when it crosses on **≥ 2 of the 6 keyframes on the same
ByteTrack track** (or, for fire, in the same image region). Overlapping clips are
not independent, so the cross-clip rule is only the existing level policy (step
down after 5 s untouched). Set `min_escalate_observations=1` for L4/L5 in clip
mode: one confirmed clip already contains ≥ 2 observations. The corroboration rules
of M1 (one source ≤ L3; classifier + detector → L4/L5) are unchanged.

**Fast lane (meets the 2 s L3 target).** Between clips, the capture process runs
YOLO @320 (≈ 25–35 ms est.) on every 4th frame (≈ 4 Hz at 15 fps). Two consecutive
weapon/fire hits on one track raise **L3 immediately** (snapshot + evidence); the
clip confirms, types and escalates to L4/L5. Cost ≈ 12–15 % of one core.

**Latency** (event starts at t₀, hop H = 2 s, clip compute P ≈ 0.6–1.6 s):

| Alert | Path | Typical | Worst |
|---|---|---|---|
| L3 (snapshot, evidence) | fast lane, 2 hits at 4 Hz | ≈ 0.6–1.0 s | ≈ 1.5 s |
| L3 (no fast lane) | first clip with ≥ 2 positive keyframes | ≈ 2.5–3.5 s | ≈ 4.5 s |
| L4/L5 (SMS, siren) | clip-confirmed + corroborated | ≈ 3–4.5 s | ≈ 6 s |

**Budget rule.** P must stay below 80 % of H (1.6 s) at p95, leaving one core for
capture, the fast lane and the dashboard. If a clip runs late, the scheduler drops
it and analyses the newest window (the same "newest frame wins" rule as the live
loop today); dropped clips are counted on the dashboard.

**Model changes needed for T1** (all trainable/exportable on the laptop now):
- Merge the three YOLO passes into **one YOLO11n/26n detector** (person, pistol,
  knife, fire) — already milestone M7; export to NCNN.
- Replace DINOv2-B with an **EfficientNet-B0 or MobileNetV3 scene head** trained on
  the same custom data (or DINOv2-S/14 if accuracy demands it, then on 1 keyframe).
  Validate with LOVO CV exactly as docs/6 did; never select on `wa02`.
- Switch identity to **`buffalo_s`** (SCRFD-500M + MobileFaceNet).
- Keep the audio CNN; consider YAMNet / EfficientAT later (M-audio track).

**Evidence for free.** The ring buffer *is* the pre-roll the vault never had
(CLAUDE.md M3 deferral): when L3 commits, seal the 4 s clip before the trigger and
the next 4–8 s after it.

**Parts:** base kit only (§8). **Extra cost: ₹0.**

**Code:** `sentrix/pipeline/clip.py` (`ClipBuffer`, `ClipScheduler`, `ClipEngine`,
`ClipScores`), `sentrix/io/picamera.py`, NCNN/ONNX runtime backends,
`tools/export_models.py`, `tools/bench_clip.py`, `replay_custom_videos.py --mode clip`,
event-log rows per clip (schema 4). Details in §9.

**Test before hardware:** run the clip engine on the laptop with 4 threads and
NCNN/ONNX exports; replay the 10 custom videos and the live session log in clip
mode; compare episodes, false onsets and latency with the M5 run
(`training/runs/custom_eval/m5/`). Scale laptop timings by the factors in §2 until
the Pi is measured.

**Risks:** smaller scene model loses accuracy (mitigation: fast lane carries the
detector; DINOv2-S fallback); alerts are seconds, not sub-second; clips can hide
very short events (< 0.5 s) — the motion-peak keyframes and the fast lane cover
most of these.

**Choose it when:** you want a fully autonomous Pi with no extra hardware cost.
**It is the main track.**

---

### T2 — Pi 5 + AI HAT+ (Hailo NPU): accelerated per-frame streaming  (Priority 3)

**Idea.** Put the neural networks on a Hailo NPU over the Pi's PCIe lane and keep
SENTRIX's per-frame architecture: detector at 15–30 Hz, faces and scene at a few Hz,
the CPU doing tracking, audio, fusion, evidence and the web server.

| Option | Chip | Price | Notes |
|---|---|---|---|
| AI HAT+ 13 TOPS | Hailo-8L | $70, ₹7,465 in stock [36][10] | enough for YOLO + faces |
| **AI HAT+ 26 TOPS** (recommended) | Hailo-8 | $110, ₹11,309 [36][10] | ≈ 2× the 8L on CNNs; YOLO11n 105 FPS measured on a Pi [HF105] |
| AI HAT+ 2 | Hailo-10H, 8 GB on board | $200 now [38] | vision ≈ the 26 TOPS HAT [38]; ≈ 2× Hailo-8 on ViTs [HZ]; early software issues [40] |

**Per-frame budget at 10 Hz** (Hailo-8, est. from measured Pi results at 30–60 % of
zoo FPS):

| Network | Where | Rate | Cost |
|---|---|---|---|
| Unified YOLO11n @640 | NPU | 10–15 Hz | ≈ 10–16 ms each [HF105][IJ] |
| SCRFD-2.5G + MobileFaceNet | NPU | 1–2 Hz on new tracks | ≈ 1–3 ms each [HZ] |
| Scene head (EfficientNet-Lite0 or vit_small) | NPU | 2–5 Hz | ≈ 1 ms / ≈ 10–20 ms [HZ] |
| Action (r3d_18 16×112 or ResNet-18 per frame) | NPU | 1 Hz | ≈ 25 ms / 1 ms [HZ] |
| Audio CNN, tracking, fusion, evidence | CPU | as today | ≈ 45 % of the CPU for host pre/post-processing [IJ] |

**Latency:** detect → L3 ≈ 0.3–0.8 s, detect → L4/L5 ≈ 1–1.5 s — **meets G4 as
written.**

**What it takes:**
- Compile every model to Hailo's `.hef` with the **Dataflow Compiler**: Linux x86-64
  only (WSL2 on the laptop), 16 GB RAM minimum / 32 GB recommended, calibration
  images from the custom dataset [42][43] (unverified against Hailo's guide).
- **DINOv2 is the main risk**: not in the model zoo, DINOv2-S compiles only with a
  community script, no FPS reported [HFdino]. Plan to retrain the scene head on a
  Hailo-friendly backbone (EfficientNet-Lite, ResNet, vit_small) — the same model
  change T1 needs.
- Several networks share one NPU through HailoRT's scheduler; switching costs
  throughput, and models the compiler splits ("multi-context") suffer most on the
  single PCIe lane [HFgen][HFctx].
- Software: `hailo-all` (AI HAT+) and `hailo-h10-all` (AI HAT+ 2) are mutually
  exclusive and need Raspberry Pi OS Trixie [39].
- PCIe is taken by the HAT: evidence storage on microSD (high endurance) or a USB 3
  SSD, not NVMe — or a PCIe switch board [45].

**Parts:** base kit + AI HAT+ 26 TOPS (≈ ₹11,300) → ≈ ₹42,000 total (§8).
**Code:** `sentrix/runtime/hailo.py` (HailoRT backend behind the same engine
interfaces), HEF build scripts in `training/hailo/`, NPU temperature and load in
`/api/health`. **Test:** compile and validate HEF accuracy against the PyTorch
models on the custom frames *before* buying (the compiler runs without the
hardware); then bench on the Pi.

**Risks:** toolchain effort and lock-in; quantisation (INT8) accuracy loss on small
objects; Hailo-10H thermal throughput drop reported [H10T].

**Choose it when:** T1 fails its gate (§7), or the demo must meet "≤ 2 s to
siren" with every model per frame.

---

### T3 — Sensor node + local hub (split compute)  (Priority 2)

**Idea.** Let each machine do what it is good at. The **Pi 5** is the sensor and
actuator node: camera, mic, PIR, door contact, temperature, siren relay, LEDs, the
ring buffer, motion gating and audio scoring (cheap). The **hub** (the team laptop
with the RTX 4060, or any PC on the LAN) runs the full, unchanged SENTRIX stack on
the Pi's stream — the configuration measured in M5.

```
  Pi 5 node                                        Hub (laptop / PC)
  ┌─────────────────────────┐   RTSP 720p H.264    ┌───────────────────────────┐
  │ Picamera2 → MediaMTX    │ ───────────────────► │ CAMERA_SOURCES=rtsp://pi… │
  │ mic → audio scores      │   MQTT  scores/events│ full SENTRIX (DINOv2-B,   │
  │ PIR, door, temp         │ ───────────────────► │ YOLO, buffalo_l, fusion)  │
  │ ring buffer (evidence)  │                      │ dashboard, incidents, SMS │
  │ gpiozero siren/LEDs     │ ◄─────────────────── │ siren / evidence commands │
  │ local fallback rules    │   MQTT  commands     └───────────────────────────┘
  └─────────────────────────┘
```

- **Stream:** MediaMTX serves the Pi camera over RTSP (port 8554) [70]; the Pi 5
  encodes H.264 in software [13], so stream 720p at 15 fps (the hub analyses at
  6 Hz anyway). SENTRIX already accepts `rtsp://` in `CAMERA_SOURCES` — no new code
  on the hub to receive video.
- **Messages:** Mosquitto MQTT [69]; topics `sentrix/<node>/status` (retained, with
  a last-will message so the hub sees the node die), `…/audio`, `…/sensor`,
  `…/cmd/siren`, `…/cmd/seal`. The hub sends "seal clip from t₁ to t₂" and the node
  seals its own ring buffer, so evidence is the node's full-quality footage.
- **Autonomy fallback:** if the hub is silent for 10 s, the node enters
  *degraded-local* mode: PIR + motion + loud audio (or T1's clip engine, once it
  exists) raise a local L3 with snapshot and buzzer. The siren stays off in home
  mode and is allowed only in armed mode — a lost Wi-Fi link must not become a false
  siren.
- **Latency:** network ≈ 50–200 ms + the laptop's measured 0.2 s frame age → detect
  → L3 ≈ 0.5–1.5 s.

**Parts:** base kit (a 4 GB Pi is enough here) + the existing laptop. **Extra
cost: ₹0.** **Code:** `sentrix/node/` (node agent: MediaMTX config, MQTT bridge,
heartbeat, command handler) and a hub-side `RemoteNode` (MQTT → actuator calls and
sensor/audio inputs). Mostly glue; ≈ 1–2 days.

**Why it is Priority 2 even though it is the easiest:** it is not edge-autonomous
(the project's stated goal), but it is the **guaranteed demo**, it measures the
full-accuracy reference on Pi-camera footage that T1's slimmed models must match,
and it is the **recording tool for M6** (the deployment-room dataset must be
recorded with the Pi camera, not the laptop webcam).

**Risks:** Wi-Fi drops; software-encode CPU on the Pi (keep 720p15); two machines
to run at the demo.

---

### T4 — Event-triggered smart camera (AI Camera IMX500 / PIR wake)  (Priority 4)

**Idea.** Most of a home's day is an empty room. Let a near-zero-cost sensor decide
*when* to think: the **Raspberry Pi AI Camera (IMX500)** runs a person detector on
the sensor itself (YOLO11n @640 ≈ 59 ms per inference, ≈ 16 FPS, no host CPU)
[UIMX][29], or a **PIR** triggers on body heat. The Pi keeps only the ring buffer
running; when a person appears it wakes the analysis — T1's clip engine (or the T3
stream, or T2) — and goes back to idle after 30 s without a person.

| | IMX500 AI Camera | PIR (HC-SR501) |
|---|---|---|
| Cost | $70 [29] | ≈ ₹100 |
| Signal | person boxes at ≈ 16 FPS from the camera's view | body heat in a cone, no location |
| Limits | 8 MB for weights + activations, int8, input ≤ 640×640, one network at a time (unverified), first model load takes minutes, no NoIR version [30][31] | false triggers from heaters/sunlight; nothing for fire |
| Use | wake + per-frame person tracks for free | wake only |

**Gain:** idle power ≈ 3 W instead of ≈ 8 W under load [68], cooler board, longer
UPS runtime, fewer clips to analyse (fewer false-alarm opportunities). **Rule that
keeps it safe:** fire is never gated on a person — the flame path (audio, motion,
and a once-per-10 s scene check) keeps running while idle.

**Parts:** AI Camera instead of Camera Module 3 (+ ≈ ₹4,500 vs CM3, est.), or a
PIR (+ ₹100). **Code:** IMX500 tensor reader in `PiCameraSource` (Picamera2 exposes
the output tensors as metadata) and a wake/sleep state in `ClipScheduler`.

**Choose it when:** T1 or T2 works and the unit must run 24/7 on a UPS, or the board
runs hot.

---

### Not chosen (and why)

| Option | Why not |
|---|---|
| Full current stack on the Pi CPU, per frame | ≈ 0.5–0.7 Hz (§2) — the failure mode of the first live test |
| Coral USB TPU, Intel NCS2 | dead ends: PyCoral ≤ Python 3.9, Coral repo archived; NCS2 discontinued [47][48][50] |
| ESP32-S3 + laptop (old Architecture A) | OV2640 640×480, weak mic path; T3 does the same job with a far better camera and on-node buffering |
| Jetson Orin Nano Super | YOLO11n in 4–5 ms with TensorRT [UJ] and the lowest risk if DINOv2-B must stay unchanged, but now $399 [CNX] and outside "camera + Raspberry Pi 5". Keep as plan B for M7 if the scene model cannot shrink |
| Jetson Nano (JetPack 4.6) | Python 3.6, CUDA 10.2, end of life; SENTRIX needs Python ≥ 3.10 |

---

## 6. Wiring and pinout (Pi 5 40-pin header)

Same physical header as earlier Pis; GPIO is 3.3 V and **not** 5 V tolerant [56].

| Pin | GPIO (BCM) | Connects to | Notes |
|---|---|---|---|
| 1 | 3V3 | BME280 VCC, relay *VCC* (logic side) | |
| 2 / 4 | 5V | PIR VCC, relay *JD-VCC* (coil side) | coil power separate from logic, see below |
| 3 / 5 | GPIO2 / GPIO3 (I2C1 SDA/SCL) | BME280 (temp/humidity/pressure) | preferred over DHT22 (§ below) |
| 7 | GPIO4 | DHT22 DATA (if used) | kernel `dht11` overlay supports DHT22 [55] |
| 11 | GPIO17 | siren relay through an NPN transistor (1 kΩ base, 4.7 kΩ base pull-down, 1N4007 across the coil) | **active-high** (`SENTRIX_SIREN_ACTIVE_HIGH=1`); `gpio=17=op,dl` in config.txt holds it off from power-on. An active-low opto board: `SENTRIX_SIREN_ACTIVE_HIGH=0` and `op,dh` |
| 16 | GPIO23 | PIR OUT | HC-SR501 output is 3.3 V [57] |
| 18 | GPIO24 | door reed switch → GND | internal pull-up; contact closed = door closed |
| 36 | GPIO16 | mains sense (PC817 output → GND) | internal pull-up; low while mains is present |
| 29 / 32 / 33 | GPIO5 / 12 / 13 | status LEDs green / amber / red | 330 Ω each; amber kept off GPIO6 so a UPS HAT stays possible |
| 37 | GPIO26 | active buzzer (via NPN transistor) | local chirp for L2/L3 |
| any free | e.g. GPIO25 | siren arming key's second contact → GND (optional) | `SENTRIX_SIREN_ARM_PIN`; tests refused while the key is off |
| 12 / 35 / 38 | GPIO18 / 19 / 20 | INMP441 BCLK / LRCL / DOUT (only if I2S mic) | unverified on Pi 5 [52]; USB mic preferred |
| 6, 9, 14, 20, 25, 30, 34, 39 | GND | grounds | |
| CAM/DISP 0 | — | Camera Module 3 | needs the **Standard-to-Mini** 22-pin cable [24] |
| PCIe FFC | — | AI HAT+ (T2) | HAT passes the GPIO header through |

**Relay and siren.** Settled (docs/7 §16 q3): the siren relay is driven through an
NPN transistor with a flyback diode, which makes it **active-high**: GPIO17 HIGH =
siren on, and the 4.7 kΩ base pull-down keeps it off whenever the pin floats. A
relay module with a JD-VCC jumper (logic VCC from 3.3 V, coil JD-VCC from 5 V, so
a 3.3 V level really switches it [60]) is usually active-low: then set
`SENTRIX_SIREN_ACTIVE_HIGH=0` and `gpio=17=op,dh`. The siren runs from its own
**12 V supply** through the relay's NO contact; never from the Pi. The code side
(gpiozero, pin held off at claim, 90 s maximum on-time, release timer, pin forced
off by the systemd unit after any exit) is `sentrix/io/actuators.py`; the
bring-up procedure is `deploy/pi5/README.md`.

**Safety during bring-up:** test the relay with an LED in place of the siren
(110 dB at close range damages hearing) through a cold start, a reboot,
`systemctl stop`, `kill -9` and the mains-to-UPS switchover
(`deploy/pi5/README.md`); keep the 12 V wiring in the enclosure, fused; the UPS's
lithium cells need a ventilated, non-flammable spot.

---

## 7. Priority order and decision gates

**Scores** (5 = best) for this team, this budget, this codebase:

| Criterion (weight) | T1 Clip Engine | T2 Hailo | T3 Hub | T4 Trigger (add-on) |
|---|---|---|---|---|
| Meets alert targets (3) | 3 (with fast lane) | 5 | 4 | 3 |
| Keeps model accuracy (3) | 3 (slimmed models) | 4 (INT8, scene retrain) | 5 (unchanged) | 3 |
| Edge autonomy, the project goal (3) | 5 | 5 | 2 | 5 |
| Extra cost (2) | 5 (₹0) | 2 (+₹7.5–11.3k) | 5 (₹0) | 3 (+₹100–4.5k) |
| Engineering effort (2) | 3 | 2 (compiler, HEFs) | 5 (glue) | 3 |
| Risk (2) | 4 | 2 (DINOv2 on Hailo, toolchain) | 4 | 3 |
| Reuses existing code (1) | 4 | 3 | 5 | 3 |
| **Weighted total (/80)** | **61** | **57** | **66** | **54** |

T3 scores highest because it is cheap and certain, but it does not deliver the
edge unit the project promises; T1 is the best *autonomous* option. Hence:

1. **T1 — Clip Engine** (main track). Build and validate it on the laptop **now**;
   it needs no purchase beyond the base kit.
2. **T3 — Hub** in the same week as hardware bring-up: guaranteed demo, the
   full-accuracy reference, and the M6 recording tool.
3. **T2 — Hailo** only if T1 fails Gate A.
4. **T4 — Trigger** once T1 or T2 works, for 24/7 running.

**Gate A — T1 on the Pi (decides whether to buy an AI HAT+):**
- clip compute p95 ≤ 1.6 s at hop 2 s, ≤ 2 % clips dropped over 1 h;
- fast lane detect → L3 p95 ≤ 2 s; clip detect → L4 p95 ≤ 6 s;
- on the 10 custom videos in clip mode: episodes caught at L3 ≥ the M5 result
  minus one (weapon 9/12, fire 3/3) and 0 false onsets;
- 1 h soak: CPU ≤ 75 °C, no throttling flags (`vcgencmd get_throttled`), memory flat.

**Gate B — T2 (if bought):** all networks on the NPU at ≥ 10 Hz combined, detect →
L4 p95 ≤ 2 s, HEF accuracy within 2 points of the PyTorch models on val.

**Gates for clip mode.** CLAUDE.md G4 ("loop ≥ 5 Hz, detect → alert p95 ≤ 2 s")
assumes per-frame analysis. For T1 it becomes: fast-lane L3 p95 ≤ 2 s, clip
decision every ≤ 2 s, L4/L5 p95 ≤ 6 s. Record the change in CLAUDE.md M9 when T1 is
adopted.

---

## 8. Bill of materials

Prices: US list from Raspberry Pi where available; India from retailers on
2026-10-10 [7][8][10]; rows marked ≈ are estimates — check robu.in, thingbits.in,
robocraze.com before ordering.

**Base kit — needed by every technique**

| Part | Choice | Price |
|---|---|---|
| Compute | Raspberry Pi 5 **8 GB** | ₹19,600–20,000 [7][8] ($175) |
| Power | Official 27 W USB-C PSU | ≈ ₹1,200 ($12) [12] |
| Cooling | Official Active Cooler | ≈ ₹500 ($5) [12] |
| Storage | 128 GB high-endurance microSD (OS + clips) | ≈ ₹1,500 |
| RTC | ML-2020 rechargeable cell for the RTC connector | ≈ ₹300 |
| Camera | Camera Module 3 **Wide** (102°) + Standard–Mini cable | ≈ ₹3,500 ($35) [25] |
| Microphone | USB omnidirectional mic | ≈ ₹850 (docs/3) |
| Siren path | 5 V relay module with JD-VCC + 12 V 110 dB siren + 12 V 2 A adapter | ≈ ₹1,000 (docs/3) |
| Sensors | HC-SR501 PIR, door reed switch, BME280 | ≈ ₹600 |
| Indicators | 3 LEDs + resistors, active buzzer + NPN | ≈ ₹150 |
| Housing | ABS enclosure, wall mount at 1.8–2.2 m, cable glands, wires | ≈ ₹1,000 |
| **Base total** | | **≈ ₹30,200–30,600** |

**Per technique**

| Technique | Adds | Total |
|---|---|---|
| T1 Clip Engine | nothing | ≈ ₹30,500 |
| T3 Hub | nothing (uses the laptop); a **4 GB** Pi (≈ ₹14,500 [7]) is enough | ≈ ₹25,000–30,500 |
| T2 Hailo | AI HAT+ 26 TOPS ≈ ₹11,300 (or 13 TOPS ≈ ₹7,500) [10] | ≈ ₹37,700–41,900 |
| T4 Trigger | AI Camera instead of CM3 (≈ +₹4,500 est.) or a PIR (already in base) | ≈ ₹30,500–35,000 |
| Recommended options for 24/7 | UPS HAT (Waveshare UPS HAT (E) or Geekworm X1202) [63][64]; 850 nm IR illuminator + CM3 **NoIR** for night | ≈ +₹4,000–6,000 |

**Tighter budget:** Pi 5 4 GB + T1/T3 (≈ ₹25,000) works if development (PyTorch)
happens on the laptop and the Pi runs only NCNN/ONNX models.

---

## 9. Software changes (repository map)

| Change | Where | For |
|---|---|---|
| `PiCameraSource(FrameSource)`: Picamera2 main + lores streams, sensor timestamps, optional IMX500 tensors | `sentrix/io/picamera.py` | all |
| Replace `RPi.GPIO` with gpiozero (lgpio): siren relay, LEDs, buzzer; inputs PIR, door | `sentrix/io/actuators.py`, new `sentrix/io/gpio.py` | all |
| Sensors: BME280 (I2C) / DHT22 (kernel IIO); telemetry only — not in the feature contract until data exists (docs/3 §7) | `sentrix/io/sensors.py` | all |
| Ring buffer in shared memory (frames, JPEG main frames, audio, sensor events) | `sentrix/pipeline/ringbuffer.py` | all |
| Clip engine: `ClipBuffer`, `ClipScheduler`, `ClipEngine` (stage cascade), `ClipScores`; within-clip confirmation reusing `ConfirmationWindow` | `sentrix/pipeline/clip.py`, `fusion/evidence.py` | T1, T4 |
| Fast lane (YOLO @320 every 4th frame in the capture process) | `sentrix/pipeline/fastlane.py` | T1 |
| Model runtime backends: torch (dev), onnxruntime, ncnn, hailort — selected per engine in config | `sentrix/runtime/` | T1, T2 |
| Model exports and slimming: unified YOLO → NCNN; scene head EfficientNet-B0 / DINOv2-S → ONNX/NCNN; `buffalo_s` | `tools/export_models.py`, `training/custom/` | T1, T2 |
| HEF compilation scripts and calibration sets | `training/hailo/` | T2 |
| Node agent (MediaMTX config, MQTT bridge, heartbeat, commands, degraded-local mode) and hub-side `RemoteNode` | `sentrix/node/` | T3 |
| Evidence clips from the ring buffer (pre-roll + post-roll), sealed as one bundle | `sentrix/evidence/vault.py` | all |
| Profiles: `SENTRIX_PROFILE=laptop | pi5-clip | pi5-hailo | pi5-node | hub` | `sentrix/config.py` | all |
| Event log schema 4: one row per clip with stage timings and keyframe votes | `sentrix/pipeline/events.py` | T1 |
| Health: CPU temp, throttle flags, PSU current, clip latency, dropped clips, NPU temp/load, node heartbeat | `/api/health`, `tools/doctor.py` | all |
| Bench and replay: `tools/bench_clip.py`; `replay_custom_videos.py --mode clip` | `tools/` | T1 |
| systemd units, hardware watchdog, log rotation, read-only root option | `deploy/pi5/` | all |

**Dashboard work this implies** (input for the next task): a *Node* panel —
temperature, throttling, power, clip/fast-lane latency, dropped clips, NPU load,
sensor readings (PIR, door, temperature), relay state, last heartbeat — and a
"test siren" button that requires confirmation and drives the relay for 1 s.

**Security on the unit:** SSH keys only, `ufw` allowing the dashboard on the LAN
only (or behind Tailscale); dashboard password and evidence key generated by
`tools/setup_env.py` and stored readable by the service user only; the Pi has no
TPM, so keep a copy of `EVIDENCE_AES_KEY` off the device (losing it makes every
bundle unreadable); updates through `apt` and pinned `requirements.txt`.

---

## 10. Roadmap

| Phase | When | Work | Done when |
|---|---|---|---|
| **H0 Plan** | now | this file; choose T1 → T3 → (T2) → T4; order the base kit | parts ordered |
| **H1 Laptop prep** (no hardware) | 1–2 weeks | clip engine + fast lane; unified YOLO and slim scene head trained/exported (M7 overlap); NCNN/ONNX backends; gpiozero actuators with a mock pin factory; `PiCameraSource` with a `FileSource` fallback; profiles; clip-mode replay and bench | clip mode on the 10 videos ≥ M5 results minus one episode, 0 false onsets; 4-thread clip compute within the scaled budget |
| **H2 Bring-up** | days 1–3 after parts | Trixie 64-bit, cooler, RTC cell; `rpicam-hello`, `arecord`, gpiozero tests with an LED in place of the siren; install SENTRIX (`run.sh`); `doctor` hardware checks | camera, mic, PIR, door, relay, LEDs verified; laptop profile runs (slowly) on the Pi |
| **H3 T1 on the Pi** | week 1 | NCNN models, clip engine, fast lane, evidence clips; `bench_clip` and 1 h soak | **Gate A** |
| **H4 T3 hub** | week 1 (parallel) | MediaMTX + MQTT node agent; hub `RemoteNode`; degraded-local mode | laptop dashboard drives the Pi siren; node death detected in ≤ 10 s |
| **H5 Data** | weeks 1–3 | record M6 sessions **with the Pi camera** (T3 makes this easy): scripted clips, hard negatives, ≥ 3 h normal | dataset in `frames.csv` schema |
| **H6 Upgrade or optimise** | after Gate A | fail → T2 (compile HEFs, Gate B); pass → T4 for 24/7 | gate passed |
| **H7 Deploy** | week 4 | enclosure, mounting 1.8–2.2 m, UPS, M9 gates on S4 | G0–G5 (clip-mode G4) |

---

## 11. Open questions for the team

1. **Budget:** is ≈ ₹30,500 for the base kit approved (the old ₹20,000 cap predates
   the Pi 5 price rises)? With T2, ≈ ₹42,000.
2. **Darkness:** D5 says darkness is out of scope for RGB. If night operation is
   required, choose Camera Module 3 **NoIR** + an IR illuminator — and record
   training data in IR, because the models have never seen grayscale IR.
3. **Alert latency:** is "L3 within 2 s, siren within ≈ 6 s" acceptable for the demo
   (T1), or must the siren fire within 2 s (→ T2)?
4. **Hub at the demo:** may the laptop be present (T3 as fallback), or must the unit
   run alone?
5. **Who owns what:** suggested — Mehul: node agent, GPIO, wiring, enclosure (the
   report's hardware owner); Kartik: unified YOLO + exports; Akshay: `buffalo_s` +
   identity on the Pi; Harshit: clip engine fusion + recording protocol; Prashant:
   audio path on the Pi.

---

## Sources

[1] https://www.raspberrypi.com/products/raspberry-pi-5/ ·
[7] https://kspelectronics.in/product-category/raspberry-pi/raspberry-pi-boards/raspberry-pi-5-boards/ ·
[8] https://thingbits.in/t/brands/raspberry-pi/boards ·
[9] https://robocraze.com/products/raspberry-pi-5-8gb ·
[10] https://robocraze.com/collections/ai-hats ·
[11] https://www.raspberrypi.com/documentation/computers/raspberry-pi.html ·
[12] https://www.raspberrypi.com/news/introducing-raspberry-pi-5/ ·
[13] https://www.raspberrypi.com/documentation/computers/camera_software.html ·
[15] https://forums.raspberrypi.com/viewtopic.php?t=378329 ·
[16] https://gpiozero.readthedocs.io/en/stable/api_pins.html ·
[17] https://gpiozero.readthedocs.io/en/stable/changelog.html ·
[18] https://pypi.org/project/rpi-lgpio/ ·
[19] https://www.raspberrypi.com/news/trixie-the-new-version-of-raspberry-pi-os/ ·
[21] https://github.com/opencv/opencv/issues/21653 ·
[22] https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf ·
[23] https://forum.opencv.org/t/rpi5-camera-doesnt-work/17303/4 ·
[24] https://www.raspberrypi.com/documentation/accessories/camera.html ·
[25] https://www.raspberrypi.com/news/new-autofocus-camera-modules/ ·
[29] https://www.raspberrypi.com/news/raspberry-pi-ai-camera-on-sale-now/ ·
[30] https://www.raspberrypi.com/documentation/accessories/ai-camera.html ·
[31] https://www.waveshare.com/raspberry-pi-ai-camera.htm ·
[36] https://www.raspberrypi.com/news/raspberry-pi-ai-hat/ ·
[38] https://www.raspberrypi.com/products/ai-hat-plus-2/ ·
[39] https://www.raspberrypi.com/documentation/computers/ai.html ·
[40] https://jeffgeerling.com/blog/2026/raspberry-pi-ai-hat-2 ·
[42] https://community.hailo.ai/t/how-to-install-the-hailo-dataflow-compiler-dfc-on-wsl2/2890 ·
[43] https://www.macnica.co.jp/en/business/semiconductor/articles/hailo/144988/ ·
[45] https://www.elektor.com/products/seeed-studio-pcie-3-0-to-dual-m-2-hat-for-raspberry-pi-5 ·
[47] https://coral.ai/docs/accelerator/get-started/ ·
[48] https://github.com/google-coral/edgetpu ·
[50] https://www.intel.com/content/www/us/en/support/articles/000093181.html ·
[52] https://learn.adafruit.com/adafruit-i2s-mems-microphone-breakout/raspberry-pi-wiring-test ·
[55] https://github.com/raspberrypi/linux/blob/rpi-6.12.y/arch/arm/boot/dts/overlays/README ·
[56] https://pinout.xyz/pinout/gpio ·
[57] https://components101.com/sensors/hc-sr501-pir-sensor ·
[60] https://forum.core-electronics.com.au/t/5v-4-channel-relay-module-10a-ce05279/2921 ·
[63] https://www.cnx-software.com/2024/08/03/waveshare-ups-hat-e-for-raspberry-pi-5-4-3b-takes-four-21700-lithium-batteries-supports-usb-pd-3-0/ ·
[64] https://geekworm.com/collections/raspberry-pi/products/x1202 ·
[68] https://raspberry.tips/en/raspberrypi-tutorials/raspberry-pi-power-consumption-update-2026-all-models-compared ·
[69] https://mosquitto.org/download/ ·
[70] https://github.com/bluenviron/mediamtx

[U8] https://raw.githubusercontent.com/ultralytics/ultralytics/v8.2.50/docs/en/guides/raspberry-pi.md ·
[U11] https://raw.githubusercontent.com/ultralytics/ultralytics/v8.3.150/docs/en/guides/raspberry-pi.md ·
[U26] https://docs.ultralytics.com/guides/raspberry-pi/ ·
[UIMX] https://docs.ultralytics.com/integrations/sony-imx500/ ·
[UJ] https://raw.githubusercontent.com/ultralytics/ultralytics/v8.3.150/docs/en/guides/nvidia-jetson.md ·
[HZ] https://github.com/hailo-ai/hailo_model_zoo/tree/master/docs/public_models ·
[HF105] https://community.hailo.ai/t/official-fps-benchmark-on-hailo-8-using-raspberry-pi-5/18873 ·
[HFgen] https://community.hailo.ai/t/hailo8-pi5-on-pcie-gen2-and-gen3-hats-no-inference-speed-difference/17692 ·
[HFctx] https://community.hailo.ai/t/the-performance-on-the-raspberry-pi-5-with-the-hailo-8-chip-seems-not-good-as-he-official-results/17473 ·
[HFdino] https://community.hailo.ai/t/deployment-of-dinov2-model-from-torch-hub/7720 ·
[H10T] https://community.hailo.ai/t/hailo-10h-throughput-degrades-irreversibly-within-minutes-of-continuous-use-125-41-fps-only-host-reboot-recovers/19218/5 ·
[IJ] https://www.ijert.org/comparative-benchmarking-of-cpu-gpu-and-npu-architectures-for-real-time-yolov8n-inference-on-embedded-edge-platforms-ijertv15is030232 ·
[NCNN] https://github.com/Tencent/ncnn/blob/master/benchmark/README.md ·
[ARM] https://huggingface.co/Arm/deit-tiny-int8-xnnpack-executorch ·
[THESIS] https://sipora.polije.ac.id/55744/1/Abstract.pdf ·
[RPB] https://www.raspberrypi.com/news/heating-and-cooling-raspberry-pi-5/ ·
[CNX] https://cnx-software.com/2026/07/22/nvidia-increases-the-price-of-jetson-modules-and-devkits-by-up-to-101

Laptop measurements: `torch.set_num_threads(4)` / ONNX Runtime 4 intra-op threads on
an Intel i7-14650HX, median of 12 runs, models as shipped on `main` (2026-10-10).
