# INTEGRATION_NOTES — T-MODEL (slim scene classifiers)

Branch `wip/model`. Owned files only: `training/**`, `models/v5_slim/**`,
`tests/test_model_slim.py`. No shared or other-track file was edited.
Full results: `training/runs/custom/slim/REPORT.md`; protocol fixed first in
`training/runs/custom/slim/PROTOCOL.md`.

## M10 status

* Done: EfficientNet-B0 and MobileNetV3-Large scene heads trained and selected
  by leave-one-video-out CV (wa02 never used for selection), one fit each with
  val/test reported once, ONNX export of the recommended one, 4-thread CPU
  latency (PyTorch and ONNX Runtime), report.
* **Result: neither slim head passes the pre-registered acceptance bar.** Best
  LOVO macro-F1 0.229 (MobileNetV3-L) vs DINOv2 0.440; val macro-F1 0.229 vs
  0.768. Distillation from per-fold DINOv2 teachers did not help (0.211 / 0.224).
  **Do not make a slim head the default scene model.**

## Requests for other owners

1. **T-CLIP, `sentrix/perception/scene.py` — no change required to load the
   checkpoints.** Both are full fp32 `torch.save` dicts in the v4 format
   (`architecture` `timm:mobilenetv3_large_100` / `timm:efficientnet_b0`,
   `storage: "full"`, classes, `image_size` 224, ImageNet normalisation,
   `temperature`, OOF `thresholds`); `SceneEngine` loads them as-is via
   `SENTRIX_SCENE_WEIGHTS=models/v5_slim/scene_classifier_mnv3l_v5.pt`
   (tested in `tests/test_model_slim.py`). Keep them **out of `_WEIGHTS`**
   (do not prefer `v5_slim` over `v4_custom`): they are clearly worse.
2. **T-CLIP, ONNX runtime backend** (`settings.clip.runtime == "onnx"`), if it
   loads `models/v5_slim/scene_classifier_mnv3l_v5.onnx` for Pi benchmarking:
   input `input` float32 `(N, 3, 224, 224)` normalised RGB, output `logits`
   `(N, 5)`; probabilities = `softmax(logits / temperature)`. Everything is in
   the ONNX `metadata_props` (`classes`, `image_size`, `normalization`,
   `temperature`, `thresholds`, `source_sha256`) and in the `onnx` block of
   `scene_classifier_mnv3l_v5_metadata.json`. **Resize with PIL bilinear (the
   engine's `transforms.Resize`)**: cv2 `INTER_AREA` moves probabilities by up
   to 0.13 and flips 1.6 % of labels (measured, recorded under
   `onnx.parity`). Thresholds must still go through the floors /
   deployment calibration exactly as in `SceneEngine._apply_threshold_policy`.
   `training/custom/export_scene_onnx.py` (`load_checkpoint`, `export`,
   `preprocess`, `parity`) can be imported by `tools/export_models.py`.
3. **Integration, docs** (`docs/6`, README, `CLAUDE.md`, HARDWARE.md §5 T1):
   HARDWARE.md's T1 table lists "EfficientNet-B0 scene head" for stage S2 at
   0.03 s; on this data that head is not accurate enough. Suggested wording:
   S2 stays DINOv2 ViT-B/14 on at most one keyframe per clip (≈ 0.6–1 s on the
   Pi CPU, est.), with DINOv2-S/14 as an untested fallback that needs its own
   LOVO run. Numbers to quote: REPORT.md §1–4.
4. No `.gitignore` change needed: distillation teachers are cached outside the
   repository (`~/.cache/sentrix/scene_teachers`, `SENTRIX_TEACHER_CACHE`).

## Not done / remaining

* NCNN export (no `ncnn`/`pnnx` in either environment); ONNX opset 17 is the
  input such converters take.
* Pi 5 latency: measured on the laptop at 4 threads only; Pi figures in
  REPORT.md are HARDWARE.md scalings.
* DINOv2-S/14 under this protocol (outside the pre-registered candidates).
