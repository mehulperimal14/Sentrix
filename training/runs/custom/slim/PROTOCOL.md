# Slim scene classifiers for the Raspberry Pi — protocol

Written and committed **before any slim-model run**, so the selection rule
cannot follow the results. The results are in [`REPORT.md`](REPORT.md).

## Question

Can an EfficientNet-B0 or MobileNetV3-Large scene head (0.4 GFLOPs / 4 M
parameters; docs/8_RASPBERRY_PI5_INTEGRATION.md §2: 7–12 ms on a Pi 5 CPU) replace the shipped DINOv2
ViT-B/14 scene classifier (44 GFLOPs / 86 M; ≈ 0.6–1 s on a Pi 5 CPU) without
losing what made it the recommended model (docs/6 §6.6)?

Reference, from `training/runs/custom/scene_cv/dino_b_last4` and
`models/v4_custom/scene_classifier_custom_v4_metadata.json`:
LOVO macro-F1 **0.440**, val accuracy / macro-F1 **0.679 / 0.768**, test
**0.720 / 0.419**.

## Data and splits (unchanged from docs/6)

765 reviewed frames, five classes, split by source video. Selection uses
**leave-one-video-out (LOVO) cross-validation over the nine train + val
videos** (`gun vid02 vid03 vid04 vid05 vid06 vid07 wa01 wa03`). The test video
`wa02` is never loaded by any selection step. Temperature and per-class
thresholds come from the pooled out-of-fold (OOF) predictions (NLL-optimal
temperature; coordinate ascent on OOF macro-F1 for fire, weapon, assault;
robbery fixed at 0.60). `fit` trains on the train split only and reports val
and test with those OOF values.

## Candidates

All at 224 px, seed 42, batch 32, AdamW (body lr 2e-4, head ×10, weight decay
0.05), label smoothing 0.1, class-balanced sampling and the augmentation of
docs/6 §6.5, from the architecture's default timm ImageNet weights
(`ra_in1k`). Every candidate that is run is listed in the report; none is
dropped.

| Tag | Backbone | What is trained | Loss | Epochs |
|---|---|---|---|---|
| `effb0_full`, `mnv3l_full` | efficientnet_b0 / mobilenetv3_large_100 | everything | cross-entropy | 15 |
| `effb0_last3`, `mnv3l_last3` | same | last 3 stages + 1×1 head conv + classifier; stem and first 4 stages frozen with their BatchNorm statistics | cross-entropy | 15 |
| `effb0_kd`, `mnv3l_kd` | same | everything | distillation from the DINOv2 ViT-B/14 last-4 recipe (`dino_b_last4`), retrained **per fold on that fold's training videos only**; teacher and student see the same augmented, mixed-up batch (function matching); 0.5 · CE + 0.5 · τ² · KL at τ = 2 | 40 |

## Selection rule

1. Per backbone: highest OOF macro-F1 under the OOF-tuned thresholds. A tie
   (within 0.005) goes to the lower OOF threat false-positive rate.
2. The winner of each backbone is fitted on the train split; val and test are
   reported **once**, from that single fit.
3. The recommended slim model is the fitted candidate with the higher OOF
   macro-F1 (same tie rule). Val confirms; it does not re-rank. Test is
   reported and never used.
4. **Acceptance** as the Pi scene model: OOF macro-F1 ≥ 0.396 (90 % of
   DINOv2's 0.440) *and* OOF threat FPR ≤ 0.15. A candidate that misses either
   is reported as clearly worse than DINOv2, and the best checkpoint is still
   committed.

## Latency

CPU, the laptop (Intel i7-14650HX), **4 threads** to mimic the Pi 5's four
cores, batch 1 at 224 px: PyTorch (the app's `.venv`) and ONNX Runtime
(`intra_op_num_threads=4`), median and p95 over 200 timed runs after warm-up,
for both slim models and the shipped DINOv2. The ONNX export of the
recommended model is checked against PyTorch on real frames.
