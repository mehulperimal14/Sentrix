# INTEGRATION_NOTES_clip.md — track T-CLIP hand-off

Branch `wip/clip` (based on `sentrix/portal` @ `31c6c50`). Scope: the 4-second
clip engine, the fast lane, runtime backends, model export, `bench_clip`,
clip-mode replay, `SENTRIX_FACE_MODEL` (HARDWARE.md T1, §2, §9; docs/7 §2.3,
§2.4, §3.4).

## 1. What landed

| File | Owner | What |
|---|---|---|
| `sentrix/pipeline/clip.py` | T-CLIP (new) | `ClipBuffer`, `select_keyframes`, `ClipScheduler`, `ClipEngine` (S0–S6), `ClipScores`, `ClipPolicy`, `ClipRunner`, `ClipStrategy`, `build_strategy(pipeline)`, `prepare_eval_pipeline(pipeline)`, `replay_video_clip(...)`, `read_clip_bundle(...)` |
| `sentrix/pipeline/fastlane.py` | T-CLIP (new) | `FastLane` (heads at 320 px on every 4th buffered frame, two consecutive hits on one track), `fast_lane_evidence` |
| `sentrix/runtime/{__init__,yolo,classifier,detector,tracking}.py` | T-CLIP (new) | torch / onnxruntime / ncnn detection heads and classifiers, `RuntimeDetector` (drop-in for `Detector` on exported heads, ByteTrack ids), box/region/track association, `configure_threads` |
| `sentrix/perception/scene.py` | T-CLIP | serves `<name>.onnx` / `<name>_ncnn_model` (next to the checkpoint, T-MODEL's format, or `models/exported/`) with `<name>_metadata.json` when `SENTRIX_RUNTIME` (or `SENTRIX_SCENE_RUNTIME`) is onnx/ncnn; no torch/timm import on that path; falls back to torch |
| `sentrix/perception/identity.py` | T-CLIP | reads `settings.identity.face_model` (`buffalo_l` default, `buffalo_s`); `FACE_MODELS`, `face_model_name`, `IdentityEngine.model_name` |
| `sentrix/fusion/evidence.py` | seam (append only) | `ClipObservation`, `KeyframeVote(Vote)`, `ClipConfirmation` — see §3 |
| `sentrix/pipeline/events.py` | seam (append only) | `EventLog.write_clip(...)`, `CLIP_SCHEMA_VERSION = 4` |
| `sentrix/evidence/vault.py` | seam (append only) | `EvidenceVault.save_clip(...)`, `CLIP_FORMAT` |
| `tools/export_models.py`, `tools/bench_clip.py` | T-CLIP (new) | exports + parity; 4-core live benchmark (`assert_measurement_safe`) |
| `tools/replay_custom_videos.py` | T-CLIP | `--mode clip`, `--fast-lane`, `--threads`, `--compare` (tick mode unchanged) |
| `models/exported/.gitignore` | T-CLIP (new) | exports are rebuilt by the tool, never committed (the scene export alone is 342 MB) |
| `tests/test_clip_{buffer,engine,fastlane,live,runtime}.py` | T-CLIP (new) | 145 tests |
| `training/runs/custom_eval/clip/` | T-CLIP (new) | clip-mode replay of the 10 videos, comparison with M5, bench, export parity |

## 2. Changes needed in files T-CLIP does not own

1. **`sentrix/config.py` — `ClipConfig` (foundation).** Add the clip tunables the
   engine reads through `clip.option(name)` (a field of the same name takes
   over without a code change; today they come from `clip.DEFAULTS`):
   `buffer_fps: float = 15.0`, `analysis_width: int = 640`,
   `scene_keyframes: int = 2`, `gate_motion_floor: float = 0.015`,
   `gate_hold_seconds: float = 10.0`, `idle_refresh_seconds: float = 10.0`,
   `identity_interval_seconds: float = 2.0`, `action_motion_floor: float = 0.10`,
   `flame_fraction: float = 0.0005`, `evidence_fps: float = 5.0`,
   `post_roll_seconds: float = 8.0`. Reason: "all tunables in config.py";
   docs/7 §3.4 lists them then. Also document `SENTRIX_SCENE_RUNTIME`
   (per-model override of `SENTRIX_RUNTIME` for the scene classifier).
2. **`sentrix/pipeline/engine.py` — `Pipeline._draw` (foundation).** In clip
   mode the overlay is refreshed once per hop plus the clip compute, so with
   `stale_overlay_seconds = 3.0` the HUD shows "AI STALLED" between clips
   whenever a clip takes > 1 s (any Pi run). Change: when
   `self._strategy.name == "clip"`, use
   `max(stale_overlay_seconds, clip.hop_seconds * (1 + clip.budget_fraction))`
   (3.6 s). No change for tick mode.
3. **`sentrix/pipeline/engine.py` — `Pipeline.decide` (foundation).** Add an
   optional `extra: dict | None = None` passed through to `self.events.write`.
   The clip runner would pass `{"mode": "clip", "clip": seq}` (or
   `{"mode": "fast_lane"}`), so a tick row written by a clip decision can be
   told from a tick-mode row without joining on the clip row's `decision_seq`.
   Until then `tools/replay_decisions.py` (which re-applies the *tick* k-of-n
   rule to the logged votes) must not be run on a clip-mode session: skip a
   session file that contains any `"kind": "clip"` row.
4. **`sentrix/pipeline/state.py` (foundation).** Add the `clip` block to
   `_INITIAL` (`CLIP_IDLE = {"mode": "tick", "clips": 0, "dropped": 0, ...}`)
   and to docs/7 §9.4 (writer: T-CLIP, `ClipStrategy` calls
   `state.update(clip=runner.status())` after every clip). Today the key
   appears only once clip mode has run.
5. **`sentrix/perception/audio.py` (no track listed).** Expose
   `AudioEngine.classify_window(samples, rms) -> AudioResult` (today the private
   `_classify`, which S5 falls back to) and a sample listener
   (`add_sample_listener(callback(samples, end_at))`) so the live strategy can
   feed `ClipBuffer.add_audio`. Today, live, S0 treats a non-zero audio score
   as activity and S5 uses `AudioEngine.result()` — the engine's own 4 s
   window, re-scored every second — instead of the clip's exact window.
6. **`tests/test_foundation_contract.py::TestPipelineSeams::test_clip_mode_falls_back_to_ticks_without_the_engine`
   (foundation).** Now that `sentrix.pipeline.clip` exists, the test passes
   only because `build_strategy` falls back to `TickStrategy` on a bare
   pipeline (missing perception components — a legitimate fallback, logged).
   To keep testing the "module missing" branch it should
   `monkeypatch.setitem(sys.modules, "sentrix.pipeline.clip", None)`.
7. **`sentrix/web/routes.py` (T-WEB) / Node page.** Show the `state.clip`
   block (clip compute p50/p95 vs budget, dropped clips, fast-lane ms and
   passes, gate open share) on the Node panel (HARDWARE.md §9 "clip/fast-lane
   latency, dropped clips"); add `identity_model` (`pipeline.identity.model_name`)
   and `scene_runtime` (`pipeline.scene.runtime`) to `health_payload()`.
8. **`requirements-pi.txt`, `run.sh`, `deploy/pi5/` (T-HW).** The Pi profile
   (`SENTRIX_PROFILE=pi5-clip`) should set `SENTRIX_PIPELINE_MODE=clip`,
   `SENTRIX_RUNTIME=onnx` (or `ncnn` once installed), `SENTRIX_FAST_LANE=1`,
   `SENTRIX_FACE_MODEL=buffalo_s`, `SENTRIX_FACE_DET_SIZE=320`; it needs the
   aarch64 `onnxruntime` wheel and optionally `ncnn`. Exports are built on the
   laptop (`python tools/export_models.py --scene [--ncnn]`) and copied to
   `models/exported/`. `PiCameraSource.latest_lores()` is not used yet: the
   clip buffer downscales the main frame to 640 px on capture (≈1 ms at
   1080p); wiring the lores stream into `ClipStrategy.on_frame` removes that.
9. **`tools/doctor.py` (T-HW).** When `SENTRIX_RUNTIME` is onnx/ncnn, report
   which exports exist (`sentrix.runtime.exported_path`) and which models will
   fall back to torch; print `models/exported/manifest.json` parity.
10. **`CLAUDE.md`, `HARDWARE.md` (integration).** Record the clip-mode results
    of §5 under M10/T-CLIP and the clip-mode gate wording of HARDWARE.md §7
    (fast-lane L3 p95 ≤ 2 s, a decision every ≤ 2 s, L4/L5 p95 ≤ 6 s) in M9.

## 3. The seams, as appended

* `ClipConfirmation(confirm_keyframes=None, cfg=None).update(observations)`
  takes `ClipObservation(source, cls, value, threshold, at, group, person_recent)`
  rows and returns `{class: ClassEvidence}` scored by the accumulator's own
  `_score` (corroboration caps, experimental cap, `ThreatConfig.sources`), so
  `decide` and the TCI are unchanged. A class is confirmed on
  `confirm_keyframes` distinct keyframes of **one group** (`("track", id)` for
  a weapon held by a person, `("region", id)` for a fire or an unattributed
  weapon, None for a classifier); the weapon gate applies per keyframe.
  `KeyframeVote(Vote)` adds `span` (seconds between the first and last
  confirming keyframe — the clip policy's dwell), `group` and `unit`
  ("keyframes"; "frames" for the fast lane). Three names were appended
  rather than one because the votes must describe themselves in keyframes,
  not ticks; nothing existing changed.
* `EventLog.write_clip(clip, *, decision_seq=None, label=None)`: one row per
  analysed clip, `"schema": 4, "kind": "clip"`, never a `features` key (so
  `load_dataset`, `replay_decisions.py` and `replay.py` skip it); tick rows
  stay schema 3.
* `EvidenceVault.save_clip(frames, *, level, status, tci, incident_type,
  reason, features, incident_id, kind, trigger_at, camera_id, context)`: the
  JPEGs concatenated in one AES-GCM bundle (`format: jpeg-concat`; the
  sidecar lists each frame's offset, length and time from the trigger),
  chained into the same manifest, `evidence` listener emitted. A viewer that
  decodes the blob as one JPEG shows the first frame; `clip.read_clip_bundle`
  splits it. Sealed only through `Pipeline._submit` (no-op in every
  measurement pipeline): pre-roll (the ring buffer, ≤ 6 s at 5 fps) when
  `_act` seals the incident's evidence, post-roll (8 s) once captured.

## 4. Behaviour of clip mode (for reviewers)

* `SENTRIX_PIPELINE_MODE=clip` → `run_live` uses `build_strategy(pipeline)`:
  torch/OpenCV held to `SENTRIX_CLIP_THREADS`, `ClipPolicy` installed,
  `pipeline.clip_runner` set. Capture, preview, the identity worker and the HUD
  stay in `run_live`; `on_frame` only buffers (downscale + 160 px thumbnail
  difference) and hands every 4th frame to the fast-lane thread.
* One decision per clip through the unchanged `Pipeline.decide`; fast-lane
  decisions too. All decisions go through one lock; pipeline time never runs
  backwards (a clip decision after a later fast-lane decision is stamped at
  the later time).
* `ClipPolicy`: L4/L5 commit on one clip (`min_escalate_observations = 1`);
  the 0.6 s dwell is measured inside the clip (`KeyframeVote.span`), so two
  confirming keyframes 1 s apart commit at once, two 0.1 s apart wait for
  the next clip.
* `request_close_incident` (demo reset) is honoured: after the decision that
  applies it, the engine forgets every cached analysis, so the next clip only
  analyses frames newer than the close and the scene is confirmed afresh.
* S4 (action) runs only when `pipeline.violence` exists, i.e. without a scene
  model (the scene classifier's assault class supersedes the RWF model, as in
  tick mode).

## 5. Results

`training/runs/custom_eval/clip/` (code commit `575e463`; `runs.json` lists the
commands). All 10 custom videos, shipped models (v4_custom DINOv2 scene,
YOLO11n heads, yolov8n person), 4 torch threads; evaluation only, nothing
tuned (wa02 included only as in M5). Committed levels, M5 per-frame → clip
(→ clip + fast lane):

| | M5 tick @ 6 Hz | clip | clip + fast lane |
|---|---|---|---|
| False onsets L2–L5 (3.7 normal min) | 0 | 0 | 0 |
| Weapon caught at L3 (of 12) | 9 | 8 | 9 |
| Fire caught at L3 (of 3) | 3 | 3 | 3 |
| Weapon at L4 / L5 | 6 / 2 | 6 / 5 | 6 / 5 |
| Fire at L4 / L5 | 2 / 2 | 2 / 2 | 2 / 2 |
| Weapon L3 latency median / p95 (s) | 0.17 / 4.04 | 1.29 / 8.42 | 1.00 / 7.21 |
| Weapon L3 within 2 s | 8 | 5 | 8 |
| Fire L3 latency median (s) | 0.08 | 1.21 | 0.15 |

Gate A's replay criterion (L3 ≥ M5 minus one: weapon ≥ 8/12, fire 3/3, 0 false
onsets) is met by both. Robbery reached L2 in M5 (1/1) and not in clip mode;
assault 0/2 in both. Replay latencies are stamped at the clip end; live adds
the clip compute.

Bench (`tools/bench_clip.py`, 60 s of 7.mov as a 30 fps camera, pinned to 4
CPUs, 4 threads, while other tracks trained models on the same machine —
numbers vary ±2x run to run): ONNX runtime + fast lane: 30 clips, 0 dropped,
compute p50 1060 / p95 2145 ms (budget 1600 ms: over at p95), fast lane p50
61 / p95 115 ms at 3.75 passes/s, preview 29.5 fps. Torch without fast lane:
p50 1624 / p95 3381 ms, 6.9 % dropped. Torch + fast lane collapses (70 %
dropped): two torch thread pools on 4 cores — on a Pi the fast lane must run
on onnx/ncnn. ONNX without fast lane measured 3151 ms p50 in a busier window,
which shows the load noise. Dominant stages: S1 (3 YOLO passes per new
keyframe, ~3.7 new keyframes/clip) and S2 (DINOv2-B, 2 per clip, ~400 ms);
HARDWARE §2 puts DINOv2-B at 0.6–1.05 s per frame on a Pi 5, so the Pi budget
needs T-MODEL's slim scene head and M7's single YOLO.

Export parity (`export_parity.json`): YOLO heads at 320/640, raw score error
≤ 2.5e-5, box error ≤ 0.004 px; scene ONNX logit error 9e-6, end-to-end
probability error 2e-6 (Pillow preprocessing vs torchvision).

## 6. What remains, and why

* **NCNN** — the `ncnn` and `pnnx` packages are not installed in `.venv`, and
  ultralytics' NCNN export downloads the `pnnx` binary; nothing was
  downloaded. `NcnnHead`/`NcnnClassifier` are implemented and tested with a
  fake `ncnn` module; `tools/export_models.py --ncnn` exports and checks
  parity once `pip install ncnn pnnx` is done.
* **`buffalo_s`** — supported and tested with a fake `FaceAnalysis`; the pack
  is not in `~/.insightface/models` here, and insightface downloads it on first
  use. Its match threshold (`SENTRIX_FACE_THRESHOLD`, tuned for `buffalo_l`)
  must be re-checked on enrolled team faces (M4 acceptance).
* **Pi measurements** — no hardware: bench numbers are the laptop held to 4
  threads / 4 CPUs, under load from other tracks' training jobs (see the
  bench JSON); HARDWARE.md §2 factors apply until Gate A is measured.
* **Slim scene model** — T-MODEL's EfficientNet-B0/MobileNetV3 checkpoints load
  through the existing `timm:` path (tested with a tiny MobileNetV3 + its ONNX
  twin); none is promoted: `SENTRIX_SCENE_WEIGHTS=models/v5_slim/<name>.pt`
  selects one for evaluation. Promotion is an evaluation decision, not a code
  change.
