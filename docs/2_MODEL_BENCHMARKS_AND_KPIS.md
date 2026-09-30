# SENTRIX — Model Benchmarks, Training Results & KPIs

**Version 3.0 · Run ID 20260824_193729 · Updated: August 2026**

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

`models/`, resolved in preference order at startup. Verify with
`python tools/doctor.py`.

| Engine | File actually loaded | Size |
|---|---|---|
| Person detection | `yolov8n.pt` | 6.5 MB |
| Weapon | `v4_finetuned/weapon_detector_ft_v4.pt` (fine-tuned from v3) | 6.2 MB |
| Fire & smoke | `v4_finetuned/fire_smoke_detector_ft_v4.pt` (fine-tuned from v3) | 6.2 MB |
| Scene (normal/fire/weapon/assault/robbery) | `v4_finetuned/scene_classifier_ft_v4.pt` (fine-tuned from the v3 violence ResNet-18) | 44.8 MB |
| Violence | `v3_real/violence_classifier_real_v3.pt` — **not loaded** while a scene model is present | 44.8 MB |
| Audio | `v3_real/audio_classifier_real_v3.pt` | 4.9 MB |
| TCI fusion | `tci_xgboost.json` (synthetic) | 0.3 MB |

Retained but **not loaded by anything**: `violence_classifier.pt` and
`audio_classifier.pt` (V2 fallbacks), `anomaly_classifier.pt`,
`v2_real/ucf_crime_dvs/ucf_crime_dvs_anomaly_v1.pt`, `vosk-model/`.

> **Corrected from v2.1.** The previous deployed-weights table claimed
> `violence_classifier.pt` and `audio_classifier.pt` came from `runs_v3_real`.
> They did not — both were the V2 files, and the audio one is 0.37 MB rather than
> the stated 4.6 MB. The V3 promotion step in doc 4 §6 was never executed, and
> for audio it *could not have been*: the two checkpoints have incompatible
> architectures (25 vs 52 tensors, `conv.*`/`fc.*` vs `features.*`/`classifier.*`,
> 5 vs 4 classes), so copying the file over would have raised
> `load_state_dict` inside a bare `except` and silently disabled audio.
>
> `perception/audio.py` now builds its architecture from the checkpoint's own
> parameter names, so both load. **The V3 audio and violence models are serving
> for the first time.**

---

### v4: fine-tuned on the custom dataset

The v3 weapon, fire/smoke and violence models were fine-tuned on the team's own
labelled footage (765 frames, 10 videos). Every metric above is measured on the
corpus each model was trained on; on the custom footage the v3 models score
0.043 test accuracy. The fine-tuned set reaches 0.677 per image and 0.824 in
end-to-end replay on the held-out test video, and keeps its original skills
through rehearsal (RWF-2000 fight AUROC 0.886 vs 0.881; weapon mAP50 0.848 vs
0.939 and fire/smoke 0.648 vs 0.722 on the original validation sets). Full
protocol and results: [doc 6](6_CUSTOM_DATASET_EVALUATION.md).

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

Measured per frame through the full pipeline, RTX 4060 laptop, `inference_fps = 6`.
Regenerate with `python tools/evaluate.py`.

| Stage | Typical |
|---|---|
| YOLOv8n person + track | ~8 ms |
| YOLOv8n weapon | ~7 ms |
| YOLOv8n fire/smoke | ~7 ms |
| Violence window (1 Hz, 16 frames) | ~25 ms on hop ticks, 0 otherwise |
| Identity (ArcFace, when a face is present) | ~20 ms |
| Behaviour + motion | < 1 ms |
| Audio (read from capture thread) | < 1 ms |
| TCI + policy + incident | < 1 ms |
| Event log + state publish | ~1 ms |

> **Corrected from v2.1.** The previous latency table was measured on Apple M2
> MPS and included rows for a Roboflow cloud path ("cached every 10 frames") that
> no longer existed even then. It also reported a 15.83 ms total against a 30 FPS
> target, which conflated the capture rate with the inference rate.

---

## 8. KPI Status

Against the objectives approved at proposal stage.

| KPI | Target | Status |
|---|---|---|
| Violence F1 macro | > 80% | 83.2% — **on a contaminated split** |
| Weapon mAP@50 | > 80% | 90.8% — **on a contaminated split** |
| Fire/smoke mAP@50 | > 65% | 71.9% — **on a contaminated split** |
| Audio macro F1 | > 90% (proposal) / > 65% (v2.1) | ~0.58 honest — **not met** |
| Inference hot path | < 20 ms | Met |
| End-to-end detect → alert | < 2 s | **Not yet measured** — needs `tools/evaluate.py` on labelled footage |
| False alarms vs motion baseline | ≥ 60% reduction | **Not yet measured** — needs `tools/baseline.py` and a boring set |
| ReID rank-1 | ≥ 85% | **Not yet measured** |
| Evidence integrity | tamper-evident | Met — AES-256-GCM + HMAC chained manifest |

The three unmeasured rows are the ones that matter most in the final report, and
all three are blocked on the same thing: recorded footage. See doc 5 §2.
