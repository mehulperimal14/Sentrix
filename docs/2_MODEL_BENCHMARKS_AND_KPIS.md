# 2. SENTRIX — Model Benchmarks, Training Results & KPIs

**What this document covers.** §1–§4 and §6 are the **v3 training results**
(run `20260824_193729`, August 2026), measured on the public corpora each model
was trained on. They are historical and still true as statements about those
runs. §5 (the weights the device loads, with v4 and v5), §7 (latency) and §8
(KPIs) describe branch `sentrix/portal` (October 2026). For results on the
team's own footage see [doc 6](6_CUSTOM_DATASET_EVALUATION.md).

> **Read this first.** Version 2.1 of this document contained several factual
> errors about our own training runs. They are corrected below, each with the
> source artifact that establishes the correct value. These corrections must be
> carried into the final report — an examiner comparing this document against
> `training/runs/` will find them.
>
> Reproduce every check here with:
>
> ```
> python tools/audit_splits.py manifest --train <train.csv> --val <val.csv>
> python tools/audit_splits.py images   --train <train/images> --val <valid/images>
> python tools/diagnose_anomaly.py
> ```

---

## 1. Summary of Corrections

| # | v2.1 claimed | Actually | Source |
|---|---|---|---|
| 1 | Weapon trained ~180 epochs, fire ~250 | **50 epochs each** | `training/runs/v3_real/*/train/results.csv` — 51 rows including header |
| 2 | "Per-class mAP@50" for weapon and fire | Those figures are **mAP@50-95** | Their mean equals the reported overall mAP@50-95 exactly |
| 3 | Deployed violence and audio weights are the V3 files | They were the **V2** files. `audio_classifier.pt` is **0.37 MB**, not 4.6 MB | `models/` file sizes and checkpoint contents |
| 4 | `anomaly_classifier.pt` is the UCF-Crime spiking network | It is a **ResNet-18**, `val_acc = 0.392`, `epoch = 1` | `torch.load` on the checkpoint |
| 5 | UCF-Crime test AUC 0.194 caused by domain shift | **Score polarity is inverted** | `python tools/diagnose_anomaly.py` |
| 6 | Metrics reported without qualification | All measured on **contaminated splits** | `python tools/audit_splits.py` |
| 7 | Audio macro-F1 0.7036 | A **single-epoch spike** on n=75 | `training/runs/v3_real/audio/history.csv` |

---

## 2. Training Summary

| Model | Status | Dataset | Epochs | Best | Time |
|---|---|---|---|---|---|
| Violence (ResNet-18) | Trained | RWF-2000 frame corpus | 15 | 13 | 44.9 min |
| Audio CNN (log-mel) | Trained | AudioSet subset, 375 clips | 17 | 10 | 42 s |
| Weapon (YOLOv8n) | Trained | Custom 3-class | **50** | 50 | 98.1 min |
| Fire & smoke (YOLOv8n) | Trained | D-Fire + open flame | **50** | 50 | 160.2 min |
| TCI fusion (XGBoost) | **Not trained on real data** | — | — | — | — |
| UCF-Crime anomaly | Trained, **not deployed** | UCF-Crime-DVS | 18 of 30 | — | separate run |

Training durations are unchanged from v2.1 and are correct — only the epoch
counts were wrong.

---

## 3. Split Contamination — read before quoting any metric

Every figure in §4 was measured against a validation set that overlaps its
training set. Three different shapes of the same mistake: **splitting at the
wrong granularity.**

### Violence — split at frame level, not video level

```
$ python tools/audit_splits.py manifest \
    --train training/runs/v3_real/violence/sample_manifest_train.csv \
    --val   training/runs/v3_real/violence/sample_manifest_val.csv

train : 15,680 rows from 3,177 sources
val   :  3,930 rows from   885 sources
sources in BOTH splits : 157 (17.7% of val sources)
val rows affected      : 2,070 (52.7% of val rows)
VERDICT: LEAKED.
```

**More than half the validation set** comes from source videos that also appear
in training. Concretely, `rwf_train_fight_1O4KGHbRt3M`: clips 0 and 2 went to
train, clip 1 to validation — same fight, same people, same lighting, seconds
apart.

Separately, the split was drawn from RWF-2000's *official train* set and
re-partitioned randomly. The official test split was never used, so 83.26% is not
comparable to any published RWF-2000 baseline. Published clip-level results are
around 85–90%.

### Weapon and fire/smoke — near-duplicate images across splits

Mean-centred cosine similarity on 128×128 thumbnails, 400 validation images
against a random 3,000-image sample of train. **Lower bounds**, since only part
of train was searched.

| Dataset | cos > 0.99 | cos > 0.97 | cos > 0.95 |
|---|---|---|---|
| `weapon_data` | **12.2%** | 22.0% | 26.5% |
| `fire_smoke_data` | **20.0%** | 33.5% | 37.0% |

Confirmed pairs:

| cos | validation | train |
|---|---|---|
| **1.0000** | `17_png.rf.c845f9….jpg` | `17_png_jpg.rf.caf166….jpg` |
| 0.9996 | `170_jpg.rf.fe4b11….jpg` | `171_jpg.rf.e8ff57….jpg` |
| 0.9998 | `WEB09901.jpg` | `WEB01668.jpg` |

The first is the same source file present in both splits under two Roboflow
processing names. The second is consecutive frames from one video.

*Caveat to state in the report:* fire imagery is visually homogeneous, so
mid-range similarity is naturally inflated (median nearest-neighbour 0.875).
Trust `cos > 0.99`; treat 0.90–0.95 as a prompt to inspect.

**Consequence.** The mAP and accuracy figures below are optimistic by an unknown
margin. Re-split by source and re-measure before the final report. A documented
de-duplication step reads as rigour; an unreported one does not.

---

## 4. Model Scorecard

All figures below are **validation** numbers on contaminated splits. Read §3 first.

### Violence Classifier — ResNet-18

| Metric | Value |
|---|---|
| Val accuracy | 83.26% |
| Macro F1 | 83.19% |
| PR-AUC | 0.8827 |
| ROC-AUC | 0.8941 |
| False negative rate | 13.25% |
| Best threshold | 0.3976 |

Per class: `nofight` P 84.9 / R 79.5 / F1 82.1 (n=1,900) · `fight` P 81.9 / R 86.7
/ F1 84.3 (n=2,030).

**Structural limitation beyond the split.** This is a *per-frame* classifier. A
still image of two people standing close together is genuinely ambiguous — the
discriminative signal for a fight is motion. The original proposal specified
ResNet-18 **+ LSTM**, and the mid-semester class diagram still shows
`frame_buffer: deque`; the temporal head was dropped somewhere.

The runtime compensates without retraining: `perception/violence.py` samples 16
frames across a 2.0 s window, scores each, and aggregates the top-quartile mean.
That recovers some temporal benefit but is not a substitute for a trained
temporal model.

### Acoustic Classifier — log-mel CNN

Classes: `speech_normal`, `siren`, `fire`, `scream`.

| Metric | Reported | Honest reading |
|---|---|---|
| Val accuracy | 72.00% | n = 75 |
| Macro F1 | 0.7036 | **~0.58** — see below |

`training/runs/v3_real/audio/history.csv`, `val_macro_f1` across 17 epochs:

```
0.24 0.40 0.45 0.44 0.51 0.61 0.62 0.43 0.55 [0.70] 0.62 0.53 0.59 0.58 0.61 0.60 0.55
                                               ^ reported as "best"
```

0.7036 is a single-epoch spike selected as the maximum of a noisy series on a
**75-sample** validation set. Surrounding epochs average about 0.58. With n=75
and `scream` at n=8, one sample flipping moves that class's F1 by roughly 6
points. Meanwhile `train_acc` peaked at 0.76, so the model is *simultaneously
under-fit and over-selected*.

Root cause is scale: 517 clips on disk, 476 resolved, **375** reaching the splits
(300 train / 75 val) across 4 classes.

**Provenance issue.** The notebook cell that filters clips and builds the split
(`CELL 19`) errored and never completed — `RuntimeError` in
`torchaudio.functional.resample`. The shipped split came from an earlier,
deleted execution, so the audio split is not reproducible from the notebook as it
stands.

### Weapon Detector — YOLOv8n, 50 epochs

| Metric | Value |
|---|---|
| mAP@50 | 90.80% |
| mAP@50-95 | 61.56% |
| Precision | 91.47% |
| Recall | 85.17% |

Per class, **mAP@50-95** (not mAP@50 as v2.1 stated): `knife` 53.5% ·
`long_gun` 63.1% · `pistol` 68.1%. Mean = 61.56% = the overall mAP@50-95 above,
which is how the mislabelling is detectable.

Converged cleanly — mAP@50 moved 0.9077 → 0.9082 over the final three epochs with
LR annealed to 4e-5.

`test/` split (703 images) exists and was never evaluated. Doing so, after
de-duplication, is the cheapest credible improvement available.

### Fire & Smoke Detector — YOLOv8n, 50 epochs

| Metric | Value |
|---|---|
| mAP@50 | 71.89% |
| mAP@50-95 | 40.38% |
| Precision | 72.32% |
| Recall | 66.80% |

Per class, **mAP@50-95**: `fire` 47.3% · `smoke` 33.5%. Mean = 40.4% = the
overall mAP@50-95.

Smoke is genuinely the harder class — high visual ambiguity against fog, steam
and dust — but the "33.5% mAP@50" figure in v2.1 overstated the problem by
comparing a stricter metric against a looser one.

### TCI Fusion — XGBoost

**Not trained on real data.** `training/scripts/refit_xgboost.py` generates its
own labels from a formula:

```python
target = 0.35 + 0.20 * v + 0.15 * m + 0.10 * is_night     # "suspicious" scenario
```

then fits XGBoost to reproduce it. `R² = 0.984` measures only that a gradient-
boosted tree can memorise a linear formula. **It is not a model result and must
not be quoted as one.**

The V3 retraining notebook searched for a real labelled multimodal dataset, found
none, and stopped rather than fabricating one — the right call, and worth saying
so explicitly in the report.

The booster also declares only 5 inputs, so it cannot see weapon, fire or
behaviour. See doc 1 §5.

---

## 5. Deployed Weights

`models/`, resolved in preference order at startup (the first file present
wins; doc 4 §6). Verify with `python tools/doctor.py`.

| Engine | File loaded on this branch | Size |
|---|---|---|
| Person detection | `yolov8n.pt` (COCO) | 6.5 MB |
| Weapon | `v4_custom/weapon_detector_custom_v4.pt` (YOLO11n, custom data) | 5.5 MB |
| Fire | `v4_custom/fire_detector_custom_v4.pt` (YOLO11n, custom data) | 5.5 MB |
| Scene (normal / fire / weapon / assault / robbery) | `v4_custom/scene_classifier_custom_v4.pt` (DINOv2 ViT-B/14, delta on the public weights) | 57.1 MB |
| Violence | `v3_real/violence_classifier_real_v3.pt` — **not loaded** while a scene model is present | 44.8 MB |
| Audio | `v3_real/audio_classifier_real_v3.pt` | 4.9 MB |
| Faces | InsightFace `buffalo_l` (detection and recognition only; downloaded on first use, not in git); `SENTRIX_FACE_MODEL=buffalo_s` for the Pi | — |
| TCI fusion | none: the TCI is the strongest confirmed class evidence (doc 1 §5) | — |

The `v4_finetuned/` weights that the preference order also names live on branch
`sentrix/finetuned-v3`, not here. On the device the detector votes count next
to the scene classifier's (`SENTRIX_THREAT_SOURCES=both`, the default since
M1), and only the two together reach L4/L5.

Retained but **not loaded by anything**: `tci_xgboost.json` (synthetic labels,
retired in M1), `violence_classifier.pt` and `audio_classifier.pt` (V2
fallbacks), `anomaly_classifier.pt`, `v2_real/ucf_crime_dvs/ucf_crime_dvs_anomaly_v1.pt`,
`v3_real/weapon_detector_real_v3.pt` and `v3_real/fire_smoke_detector_real_v3.pt`
(fallbacks behind v4), and `v5_slim/` (§5.2). The Vosk model and the v1 flat
`weapon_detector.pt` / `fire_smoke_detector.pt` were removed from the tree (see
git history). Exported ONNX/NCNN copies for clip mode are written to
`models/exported/` by `tools/export_models.py` and are not tracked.

> **Corrected from v2.1.** The previous deployed-weights table claimed
> `violence_classifier.pt` and `audio_classifier.pt` came from `runs_v3_real`.
> They did not — both were the V2 files, and the audio one is 0.37 MB rather than
> the stated 4.6 MB. The V3 promotion step in doc 4 §6 was never executed, and
> for audio it *could not have been*: the two checkpoints have incompatible
> architectures (25 vs 52 tensors, `conv.*`/`fc.*` vs `features.*`/`classifier.*`,
> 5 vs 4 classes), so copying the file over would have raised
> `load_state_dict` inside a bare `except` and silently disabled audio.
> `perception/audio.py` now builds its architecture from the checkpoint's own
> parameter names, so both load.

### 5.1 v4: models trained on the custom dataset

Every metric in §4 is measured on the corpus each model was trained on; on the
team's own labelled footage (765 frames, 10 videos) the v3 models score 0.043
test accuracy. The v4 scene classifier — DINOv2 ViT-B/14 with its last four
blocks trained on that footage, chosen by leave-one-video-out cross-validation
— reaches val macro-F1 0.768 and cuts false alarms across all ten videos from
13.96 to 0.79 per normal minute, but misses the stick swung in the test video.
Full protocol, the comparison with the fine-tuned v3 models, and every
limitation: [doc 6](6_CUSTOM_DATASET_EVALUATION.md).

### 5.2 v5: slim scene classifiers for the Pi (not promoted)

Two small CNN scene heads were trained for the Raspberry Pi under a protocol
fixed before any run (`training/runs/custom/slim/PROTOCOL.md`, commit
`5f8b589`). Numbers from `training/runs/custom/slim/REPORT.md` and
`latency.json` (commit `3bfb441`; laptop CPU, 4 threads, on mains power):

| | LOVO macro-F1 | Val acc / macro-F1 | Test acc / macro-F1 | CPU latency, PyTorch / ONNX Runtime |
|---|---|---|---|---|
| DINOv2 ViT-B/14 (shipped) | 0.440 | 0.679 / 0.768 | 0.720 / 0.419 | 96.9 / 106.4 ms |
| MobileNetV3-L `mnv3l_full` | 0.229 | 0.357 / 0.229 | 0.720 / 0.419 | 6.6 / 2.4 ms |
| EfficientNet-B0 `effb0_kd` | 0.211 | 0.405 / 0.271 | 0.720 / 0.419 | 9.5 / 4.3 ms |

The bar set in advance was LOVO macro-F1 ≥ 0.396 with threat FPR ≤ 0.15. The
best slim head reached 0.229 with FPR 0.194, so **neither replaces DINOv2**.
The test row is the same for all three because no model detects the stick in
the test corridor: each calls every `wa02` frame normal.
No slim head recognises fire in a video it has not seen; their weapon AUROC is
0.46–0.61 against 0.75 for DINOv2. Distillation from per-fold DINOv2 teachers
did not close the gap (students 0.211 and 0.224). The checkpoints
(`models/v5_slim/`, plus an ONNX export of `mnv3l`) load through
`SENTRIX_SCENE_WEIGHTS` for development only and are not in the preference
order.

---

## 6. UCF-Crime Anomaly — a polarity bug, not domain shift

| Split | ROC-AUC | AP |
|---|---|---|
| Validation | 0.8263 | 0.8587 |
| Test | **0.1940** | 0.3372 |

Version 2.1 attributed the gap to "category-level domain shift in the official
test split". That is wrong, and `python tools/diagnose_anomaly.py` shows why in
about a second:

```
video-level score (as reported)   AUC=0.1940  (inverted AUC=0.8060)
mean of clip scores               AUC=0.1966  (inverted AUC=0.8034)
clip level                        AUC=0.2477  (inverted AUC=0.7523)

Score distribution by true class
  normal    n= 147  mean=0.9230  median=0.9574
  anomalous n= 140  mean=0.6853  median=0.8772
```

**Normal videos receive a higher anomaly score than anomalous videos.** Chance is
0.5; 0.194 is not a weak model, it is a model ranking backwards. Inverted it
scores 0.806, sitting right beside the validation AUC of 0.826. The network
learned the task and something downstream flipped the sign.

Check, in order: how `true_binary` is built for the test split; which logit index
is read as the anomaly probability; whether `test_split.txt` is parsed with the
same convention as `train_split.txt`.

Version 2.1 proposed replacing SpikingJelly with a MobileNetV3 on optical flow —
roughly a month of work for what is very likely a one-line fix.

**This model is not wired into the runtime.** There is no `anomaly` key in the
feature contract and nothing loads either checkpoint. Doc 1 v2.1 and doc 5 both
listed it as an operational modality; it never was.

Separately, `models/anomaly_classifier.pt` is **not** the spiking network. It is
a ResNet-18 with `val_acc = 0.392` saved at epoch 1 — an abandoned artifact. It
is retained but should not be referenced as a result.

---

## 7. Latency

Measured on the maintainer laptop (Intel i7-14650HX, `.venv` with CPU torch;
the RTX 4060 is not used by the app environment). Each row cites its report.

| Measurement | Result | Source |
|---|---|---|
| Live configuration, tick mode (vid07 as a 30 fps camera, 4 faces, real identity and gallery, no audio) | preview 29.7 fps; AI 5.6 Hz; tick p50 168 / p95 226 ms; frame age p50 198 ms | `training/runs/custom_eval/m5/bench_live.json` (`tools/bench_live.py`, commit `8f21f47`) |
| Per stage in that run (p50) | identity 156 ms (own thread); scene 92 ms; YOLO person / weapon / fire 25 / 24 / 23 ms | same |
| Clip mode, ONNX + fast lane, 4 threads pinned to 4 CPUs | clip compute p50 1060 / p95 2145 ms against a 1600 ms budget (missed at p95); 0 clips dropped; fast lane p50 61 ms | `training/runs/custom_eval/clip/bench_clip_onnx_fastlane.json` (commit `cc53443`; other training jobs ran on the machine, and timings varied about 2x between runs) |
| Scene classifier alone, 4 threads | DINOv2 96.9 ms (PyTorch) / 106.4 ms (ONNX); MobileNetV3-L 6.6 / 2.4 ms | `training/runs/custom/slim/latency.json` (commit `3bfb441`) |
| Replay through the harness (identity and audio off) | custom-v4 mean 213 / p95 245 ms per tick | doc 6 §6.7 |

Pi 5 figures exist only as estimates from published benchmarks (doc 8 §2).
Nothing has been measured on a Pi.

> **Removed.** The previous table listed ~8 ms per YOLO head and ~20 ms for
> identity on an RTX 4060. No committed report produced it, and the app
> environment runs CPU torch, so it did not describe the product. v2.1's table
> before it was measured on Apple M2 MPS and included a Roboflow cloud path
> that no longer existed.

---

## 8. KPI Status

Against the objectives approved at proposal stage.

| KPI | Target | Status |
|---|---|---|
| Violence F1 macro | > 80% | 83.2% — **on a contaminated split** |
| Weapon mAP@50 | > 80% | 90.8% — **on a contaminated split** |
| Fire/smoke mAP@50 | > 65% | 71.9% — **on a contaminated split** |
| Audio macro F1 | > 90% (proposal) / > 65% (v2.1) | ~0.58 honest — **not met** |
| Inference hot path | < 20 ms | **Not met on CPU**: tick p50 168 ms (§7); each YOLO head 23–25 ms |
| End-to-end detect → alert | < 2 s | **Not met**: episode onset → committed L3 p95 4.0 s on the custom videos (gate G4 FAIL, `training/runs/custom_eval/m5/gates.json`); weapon 8 of 12 episodes within 2 s |
| False alarms vs motion baseline | ≥ 60% reduction | **Not yet measured** — needs `tools/baseline.py` and a boring set |
| ReID rank-1 | ≥ 85% | **Not yet measured** |
| Evidence integrity | tamper-evident | Met — AES-256-GCM + HMAC chained manifest |

The unmeasured rows, and a false-alarm rate with a useful bound (the 3.7
normal minutes in the custom videos bound it only at ≤ 60 false L3 onsets per
hour, doc 6 §6.8), are blocked on the same thing: recorded footage from the
deployment room (`CLAUDE.md` M6). See doc 5 §2.
