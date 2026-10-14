# Slim scene classifiers for the Raspberry Pi — report

Track T-MODEL (HARDWARE.md §2 and §5 T1, "replace DINOv2-B with an
EfficientNet-B0 or MobileNetV3 scene head"). The protocol, candidates,
selection rule and acceptance bar were committed **before any run** in
[`PROTOCOL.md`](PROTOCOL.md) (commit `5f8b589`). Every table between
`BEGIN`/`END` markers is generated from the run files by
`python training/custom/slim_report.py`; nothing is transcribed.

## Verdict

**Neither slim head can replace the DINOv2 scene classifier.** The best one
(MobileNetV3-Large, `mnv3l_full`) reaches LOVO macro-F1 **0.229** against
DINOv2's **0.440**, far below the pre-registered bar of 0.396; on val it scores
0.357 / 0.229 (accuracy / macro-F1) against 0.679 / 0.768. Out of fold its
weapon-vs-normal AUROC is 0.61 (DINOv2 0.75) and it never predicts fire
correctly in a video it has not seen. Both slim checkpoints are committed in
the `SceneEngine` format, with an ONNX export of the recommended one, because
they are *fast* (2.4 ms in ONNX Runtime at 4 threads, about 40x less than
DINOv2) and the clip engine can be developed against them — but they must not
be promoted to the alarm path.

Distillation did not close the gap: the per-fold DINOv2 teachers reproduce
their own cv run exactly (OOF macro-F1 0.440), yet the distilled students land
where plain fine-tuning does (0.211 / 0.224). Matching the teacher on the
eight training videos teaches an ImageNet CNN those videos, not what the
teacher knows about a new room. DINOv2's cross-video generalisation lives in
its self-supervised features, which these backbones do not have.

## 1. Leave-one-video-out (selection)

Pooled out-of-fold predictions over the nine train + val videos; `wa02` is
never loaded. Thresholds tuned on OOF (robbery fixed at 0.60). The AUROC
columns are threshold-free: `p(class)` separating that class's frames from
`normal` frames. The first three rows are references (docs/6 §6.4–6.6).

<!-- BEGIN lovo -->
| Candidate | Backbone | Trained | Loss | OOF acc | **OOF macro-F1** | normal F1 | fire F1 | weapon F1 | assault F1 | robbery F1 | Threat FPR | AUROC weapon | AUROC fire |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `dino_b_last4` | DINOv2 ViT-B/14 | last 4 blocks + head | CE, 15 ep | 0.696 | **0.440** | 0.779 | 0.828 | 0.593 | 0.000 | 0.000 | 0.114 | 0.750 | 0.919 |
| `dino_b_frozen` | DINOv2 ViT-B/14 | head only | CE, 15 ep | 0.661 | **0.399** | 0.752 | 0.692 | 0.550 | 0.000 | 0.000 | 0.151 | 0.677 | 0.958 |
| `r18_imagenet` | ResNet-18 | all | CE, 15 ep | 0.562 | **0.215** | 0.704 | 0.000 | 0.371 | 0.000 | 0.000 | 0.204 | 0.526 | 0.732 |
| `effb0_full` | EfficientNet-B0 | all | CE, 15 ep | 0.561 | **0.204** | 0.703 | 0.054 | 0.262 | 0.000 | 0.000 | 0.144 | 0.509 | 0.613 |
| `effb0_last3` | EfficientNet-B0 | last 3 stages + head | CE, 15 ep | 0.540 | **0.194** | 0.671 | 0.000 | 0.300 | 0.000 | 0.000 | 0.210 | 0.456 | 0.761 |
| `effb0_kd` | EfficientNet-B0 | all | KD from dino_b_last4 (w 0.5, T 2.0), mixup 1.0, 40 ep | 0.556 | **0.211** | 0.691 | 0.000 | 0.364 | 0.000 | 0.000 | 0.220 | 0.483 | 0.439 |
| `mnv3l_full` | MobileNetV3-L | all | CE, 15 ep | 0.595 | **0.229** | 0.717 | 0.000 | 0.430 | 0.000 | 0.000 | 0.194 | 0.607 | 0.332 |
| `mnv3l_last3` | MobileNetV3-L | last 3 stages + head | CE, 15 ep | 0.519 | **0.216** | 0.653 | 0.035 | 0.394 | 0.000 | 0.000 | 0.323 | 0.559 | 0.788 |
| `mnv3l_kd` | MobileNetV3-L | all | KD from dino_b_last4 (w 0.5, T 2.0), mixup 1.0, 40 ep | 0.595 | **0.224** | 0.718 | 0.000 | 0.401 | 0.000 | 0.000 | 0.164 | 0.523 | 0.427 |
<!-- END lovo -->

Temperature and thresholds from the OOF predictions (what each fitted
checkpoint carries):

<!-- BEGIN calibration -->
| Candidate | Temperature | fire thr | weapon thr | assault thr | robbery thr |
|---|---|---|---|---|---|
| `dino_b_last4` | 1.48 | 0.35 | 0.35 | 0.85 | 0.60 |
| `effb0_full` | 2.74 | 0.30 | 0.45 | 0.85 | 0.60 |
| `effb0_last3` | 3.41 | 0.35 | 0.40 | 0.85 | 0.60 |
| `effb0_kd` | 1.77 | 0.25 | 0.35 | 0.85 | 0.60 |
| `mnv3l_full` | 2.76 | 0.85 | 0.30 | 0.85 | 0.60 |
| `mnv3l_last3` | 2.38 | 0.20 | 0.30 | 0.85 | 0.60 |
| `mnv3l_kd` | 2.00 | 0.35 | 0.35 | 0.85 | 0.60 |
<!-- END calibration -->

Accuracy per held-out video (each fold's video, scored by the model that never
saw it):

<!-- BEGIN per_video -->
| Candidate | gun | vid02 | vid03 | vid04 | vid05 | vid06 | vid07 | wa01 | wa03 |
|---|---|---|---|---|---|---|---|---|---|
| `dino_b_last4` | 0.60 | 0.51 | 0.85 | 0.88 | 0.18 | 0.77 | 0.89 | 0.47 | 0.66 |
| `effb0_full` | 0.15 | 0.56 | 0.63 | 0.75 | 0.10 | 0.68 | 0.72 | 0.44 | 0.64 |
| `effb0_last3` | 0.18 | 0.36 | 0.65 | 0.75 | 0.10 | 0.65 | 0.74 | 0.46 | 0.47 |
| `effb0_kd` | 0.18 | 0.39 | 0.65 | 0.75 | 0.10 | 0.65 | 0.76 | 0.46 | 0.53 |
| `mnv3l_full` | 0.48 | 0.39 | 0.65 | 0.75 | 0.10 | 0.61 | 0.80 | 0.46 | 0.51 |
| `mnv3l_last3` | 0.35 | 0.39 | 0.60 | 0.75 | 0.12 | 0.68 | 0.65 | 0.46 | 0.45 |
| `mnv3l_kd` | 0.23 | 0.39 | 0.65 | 0.75 | 0.10 | 0.65 | 0.85 | 0.46 | 0.55 |
<!-- END per_video -->

The distillation teachers, retrained per fold on exactly the student's
training frames, reproduce the shipped recipe:

<!-- BEGIN teachers -->
| Student | Teacher | Teacher OOF macro-F1, retrained per fold | Teacher's own cv run | Student OOF macro-F1 |
|---|---|---|---|---|
| `effb0_kd` | `dino_b_last4` | 0.440 | 0.440 | 0.211 |
| `mnv3l_kd` | `dino_b_last4` | 0.440 | 0.440 | 0.224 |
<!-- END teachers -->

### What the numbers say

* **Fire does not transfer.** Fire exists in two videos (`vid06`, `vid04`);
  each fold holding one out must learn it from the other. DINOv2 does (fire F1
  0.83); no CNN candidate gets above 0.05. MobileNetV3's OOF fire threshold
  went to 0.85, i.e. off.
* **Weapon barely transfers.** EfficientNet-B0's OOF weapon AUROC is
  0.46–0.51, chance; MobileNetV3's 0.52–0.61. The `gun` video (a handgun in a
  new context) shows it most: 15–48 % accuracy against DINOv2's 60 %.
* **Freezing early stages does not help** (it did for DINOv2): low-level CNN
  features are not where the room is memorised.
* Assault and robbery are 0 for every model, DINOv2 included (docs/6 §6.4: two
  rooms, one event; they cannot be validated on this dataset).

## 2. Selection (rule from PROTOCOL.md, applied mechanically)

| Backbone | Candidates (OOF macro-F1 / threat FPR) | Winner | Why |
|---|---|---|---|
| EfficientNet-B0 | full 0.2038 / 0.144; last3 0.1943 / 0.210; kd 0.2109 / 0.220 | `effb0_kd` | highest macro-F1; margin 0.0071 is outside the 0.005 tie band |
| MobileNetV3-L | full 0.2293 / 0.194; last3 0.2164 / 0.323; kd 0.2238 / 0.164 | `mnv3l_full` | highest macro-F1; margin 0.0055 is outside the 0.005 tie band |
| Recommended slim model | `mnv3l_full` 0.229 vs `effb0_kd` 0.211 | **`mnv3l_full`** | higher OOF macro-F1 |
| Acceptance (≥ 0.396 and FPR ≤ 0.15) | 0.229, FPR 0.194 | **not met** | clearly worse than DINOv2 |

## 3. Held-out val and test (one fit each, reported once)

Each winner was fitted once on the train split with its OOF temperature and
thresholds. Test (`wa02`) was computed by that single fit and used for
nothing. "Served thresholds" are the checkpoint's raised to the runtime floors
(`ThreatConfig.scene_threshold_floor`: weapon 0.50, fire 0.45), which is what
`SceneEngine` actually applies.

<!-- BEGIN heldout -->
| Model | Split | n | Accuracy | Macro-F1 | Threat recall | Threat FPR | weapon F1 | fire F1 | AUROC weapon | AUROC fire | Macro-F1 at served thresholds |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `dino_b_last4` | val | 84 | 0.679 | 0.768 | 0.557 | 0.000 | 0.675 | 1.000 | 0.777 | 1.000 | 0.700 |
| `dino_b_last4` | test | 93 | 0.720 | 0.419 | 0.000 | 0.000 | 0.000 | - | 0.559 | - | 0.419 |
| `effb0_kd` | val | 84 | 0.405 | 0.271 | 0.180 | 0.000 | 0.333 | 0.000 | 0.400 | 0.638 | 0.206 |
| `effb0_kd` | test | 93 | 0.720 | 0.419 | 0.000 | 0.000 | 0.000 | - | 0.408 | - | 0.419 |
| `mnv3l_full` | val | 84 | 0.357 | 0.229 | 0.115 | 0.000 | 0.226 | 0.000 | 0.620 | 0.862 | 0.143 |
| `mnv3l_full` | test | 93 | 0.720 | 0.419 | 0.000 | 0.000 | 0.000 | - | 0.467 | - | 0.419 |
<!-- END heldout -->

Val confirms the LOVO ranking: both slim heads lose most of DINOv2's val
macro-F1 (0.768 → 0.271 / 0.229) and miss every val fire frame (the
MobileNetV3's fire *ranking* on val is decent, AUROC 0.86, but its OOF fire
threshold is 0.85). Test is identical for all three (0.720 / 0.419): nobody
detects the stick swung in the corridor, so every model calls `wa02` normal
(docs/6 §6.6).

### Through the product pipeline (held-out videos)

`tools/replay_custom_videos.py --videos heldout` with `SENTRIX_SCENE_WEIGHTS`
pointing at each checkpoint (evaluation harness: no side effects, identity
neutral, no microphone), current M1–M5 pipeline, committed level L3 (the level
that seals evidence and opens an assistance request):

<!-- BEGIN replay -->
| Scene model | Split | Ticks | Accuracy | Macro-F1 | Caught at L3: fire | Caught at L3: weapon | Median weapon latency | False L3 onsets | Normal minutes |
|---|---|---|---|---|---|---|---|---|---|
| `scene_classifier_custom_v4.pt` | val | 153 | 0.686 | 0.713 | 1/1 | 1/1 | 6.3 s | 0 | 0.18 |
| `scene_classifier_custom_v4.pt` | test | 250 | 0.824 | 0.452 | - | 0/3 | - | 0 | 0.69 |
| `scene_classifier_mnv3l_v5.pt` | val | 153 | 0.444 | 0.222 | 0/1 | 1/1 | 6.8 s | 0 | 0.18 |
| `scene_classifier_mnv3l_v5.pt` | test | 250 | 0.824 | 0.452 | - | 0/3 | - | 0 | 0.69 |

Replayed at commit(s) 44390fa, febcafc.
<!-- END replay -->

With the slim head the val fire episode is no longer caught; the val handgun
episode still is. False L3 onsets are 0 for both, but over 0.9 normal minutes:
no false-alarm rate can be claimed from this.

## 4. Latency at the Pi 5's thread budget

<!-- BEGIN latency -->
Measured 2026-10-15T02:33:43 on Intel(R) Core(TM) i7-14650HX (on mains power), **4 threads**, batch 1, 200 timed runs per model in interleaved rounds (CPU load before the run: 10.0 %); torch 2.13.0+cpu, onnxruntime 1.29.0.

| Model | Checkpoint | Params | GFLOPs | File | PyTorch median / p95 | ONNX Runtime median / p95 |
|---|---|---|---|---|---|---|
| EfficientNet-B0 | `scene_classifier_effb0_v5.pt` | 4.0 M | 0.77 | 16.3 MB (full) | 9.5 / 10.3 ms | 4.3 / 5.3 ms |
| MobileNetV3-L | `scene_classifier_mnv3l_v5.pt` | 4.2 M | 0.43 | 17.0 MB (full) | 6.6 / 7.5 ms | 2.4 / 3.1 ms |
| DINOv2 ViT-B/14 | `scene_classifier_custom_v4.pt` | 85.7 M | 43.89 | 57.1 MB (delta) | 96.9 / 104.9 ms | 106.4 / 110.0 ms |

Preprocessing (BGR 640x480 frame -> normalised 224x224 tensor, as `SceneEngine` does it): 1.43 ms median, 1.56 ms p95.
<!-- END latency -->

Scaled to the Pi 5 with HARDWARE.md §2 (published Pi 5 NCNN: EfficientNet-B0
12.3 ms, MobileNetV3-L 7.4 ms; DINOv2-B ≈ 0.6–1.05 s est.): a slim head costs
≈ 10 ms per keyframe on the Pi, DINOv2-B roughly 100x more. ONNX Runtime halves
the slim models' PyTorch time on the laptop and does not help the ViT.

The ONNX export of the recommended model (`scene_classifier_mnv3l_v5.onnx`,
opset 17) reproduces PyTorch on 64 train-split frames to a maximum probability
difference of 2.5e-6, with identical labels. A cv2 `INTER_AREA` resize instead
of the engine's PIL bilinear resize moves probabilities by up to 0.13, so a
runtime backend must reproduce the PIL resize (recorded in the metadata).

## 5. What to do instead (for the T1 clip engine)

1. Keep **DINOv2** as the scene model and run it **rarely**: HARDWARE.md T1
   already runs the scene stage only when S1 saw a person or a fire candidate;
   at ≈ 0.6–1 s per keyframe on the Pi CPU one keyframe per clip fits the
   1.6 s budget, two do not.
2. HARDWARE.md's named fallback, **DINOv2 ViT-S/14** (11 GFLOPs), scored well
   below ViT-B in the frozen-feature screen (LOVO macro-F1 0.276 vs 0.378). It
   needs its own LOVO run under this protocol before anyone relies on it — not
   done here (outside this track's pre-registered candidates).
3. The slim heads are useful as a **stand-in** for clip-engine development and
   Pi benchmarking (same input/output contract), never as the alarm-path
   classifier.
4. The real fix is the M6 deployment-room data: every class that fails here is
   seen in one or two videos.

## 6. Files

| File | What |
|---|---|
| `models/v5_slim/scene_classifier_mnv3l_v5.pt` (+ `_metadata.json`, `.onnx`) | recommended slim head (`mnv3l_full`), full fp32 checkpoint, `timm:mobilenetv3_large_100` |
| `models/v5_slim/scene_classifier_effb0_v5.pt` (+ `_metadata.json`) | EfficientNet-B0 winner (`effb0_kd`), full fp32, `timm:efficientnet_b0` |
| `training/runs/custom/scene_cv/{effb0,mnv3l}_{full,last3,kd}/` | LOVO summaries and OOF logits |
| `training/runs/custom/scene/{effb0_kd,mnv3l_full}/` | held-out metrics and per-image predictions |
| `training/runs/custom/slim/latency.json`, `replay/*.json` | latency and pipeline replays |

Both checkpoints load in `SceneEngine` unchanged (`SENTRIX_SCENE_WEIGHTS=…`)
and need no download (full storage).

## 7. Commands

```bash
# selection, GPU env (C:\Users\PC\anaconda3\envs\sentrix\python.exe); wa02 never loaded
python training/custom/train_scene_classifier.py cv --arch timm:efficientnet_b0 --init pretrained --tag effb0_full
python training/custom/train_scene_classifier.py cv --arch timm:mobilenetv3_large_100 --init pretrained --tag mnv3l_full
python training/custom/train_scene_classifier.py cv --arch timm:efficientnet_b0 --init pretrained --train-last-blocks 3 --tag effb0_last3
python training/custom/train_scene_classifier.py cv --arch timm:mobilenetv3_large_100 --init pretrained --train-last-blocks 3 --tag mnv3l_last3
python training/custom/train_scene_classifier.py cv --arch timm:efficientnet_b0 --init pretrained --teacher dino_b_last4 --mixup 1.0 --epochs 40 --tag effb0_kd
python training/custom/train_scene_classifier.py cv --arch timm:mobilenetv3_large_100 --init pretrained --teacher dino_b_last4 --mixup 1.0 --epochs 40 --tag mnv3l_kd

# one fit per backbone winner (val + test reported once)
python training/custom/train_scene_classifier.py fit --arch timm:efficientnet_b0 --init pretrained --teacher dino_b_last4 \
    --mixup 1.0 --epochs 40 --cv-tag effb0_kd --tag effb0_kd --out models/v5_slim/scene_classifier_effb0_v5.pt
python training/custom/train_scene_classifier.py fit --arch timm:mobilenetv3_large_100 --init pretrained \
    --cv-tag mnv3l_full --tag mnv3l_full --out models/v5_slim/scene_classifier_mnv3l_v5.pt

# app env (.venv: CPU torch, onnx, onnxruntime)
python training/custom/export_scene_onnx.py models/v5_slim/scene_classifier_mnv3l_v5.pt
python training/custom/bench_scene_latency.py models/v5_slim/scene_classifier_effb0_v5.pt \
    models/v5_slim/scene_classifier_mnv3l_v5.pt models/v4_custom/scene_classifier_custom_v4.pt \
    --threads 4 --out training/runs/custom/slim/latency.json
SENTRIX_SCENE_WEIGHTS=models/v4_custom/scene_classifier_custom_v4.pt python tools/replay_custom_videos.py --videos heldout --tag slim_ref_dino
SENTRIX_SCENE_WEIGHTS=models/v5_slim/scene_classifier_mnv3l_v5.pt python tools/replay_custom_videos.py --videos heldout --tag slim_mnv3l
python training/custom/slim_report.py
```

Notes on the runs: the GPU runs overlapped (two or three at a time), and for
about an hour the laptop ran on battery with the GPU power-capped (~240 MHz);
that changed wall-clock time only. The latency run was made afterwards on
mains power at 10 % CPU load. The DINOv2 reference replay ran during the
battery period, so its pipeline latency is not comparable; its detections are.
