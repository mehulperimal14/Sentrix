# SENTRIX frame-level image dataset

765 labelled frames extracted from 10 source surveillance videos, for
5-class single-label image classification.

```
0 normal   1 fire   2 weapon   3 assault   4 robbery
```

---

## How the class indices are allotted

The index is **part of the folder name**, so the mapping cannot drift:

| idx | class     | folder      |
|-----|-----------|-------------|
| 0   | normal    | `0_normal`  |
| 1   | fire      | `1_fire`    |
| 2   | weapon    | `2_weapon`  |
| 3   | assault   | `3_assault` |
| 4   | robbery   | `4_robbery` |

`normal = 0` is deliberate: index 0 is the negative / no-threat class, so
`pred > 0` means "something was detected".

An alphabetical listing of the class folders — which is exactly what
`torchvision.datasets.ImageFolder` sorts by — yields `0_normal, 1_fire,
2_weapon, 3_assault, 4_robbery`, i.e. the table above. No manual
`class_to_idx` override is needed. The same mapping is repeated in
`metadata/classes.json` and in the `class_id` column of `labels.csv`.

---

## Layout

```
dataset/
├── README.md                  <- this file
├── labels.csv                 <- one row per image: path, class, id, split, provenance
├── load_dataset.py            <- drop-in PyTorch loaders (both styles)
│
├── train/  val/  test/        <- split first, then class
│   ├── 0_normal/
│   ├── 1_fire/
│   ├── 2_weapon/
│   ├── 3_assault/
│   └── 4_robbery/
│
├── quality_check/             <- contact sheets, one per class, for visual QC
│   ├── 0_normal_samples.jpg   1_fire_samples.jpg   2_weapon_samples.jpg
│   ├── 3_assault_samples.jpg  4_robbery_samples.jpg
│   ├── transition_normal_to_threat.jpg
│   └── transition_threat_to_normal.jpg
│
└── metadata/
    ├── classes.json           <- canonical class list and index mapping
    ├── frames.csv             <- every sampled frame, kept or dropped, with the reason
    ├── segments.csv           <- the reviewed temporal segments labels came from
    ├── video_split.csv        <- which source video went into which split
    ├── run_config.json        <- every parameter of the extraction run
    ├── errors.csv             <- per-video failures (empty)
    ├── review_required.csv    <- unresolved frames (empty: queue is closed)
    └── dataset_report.txt     <- statistics, corrections, leakage check
```

The review queue is resolved and its folder has been removed; every frame
now carries a class.

## Quick start

```python
from load_dataset import CLASS_TO_IDX, manifest_split, imagefolder_split

train = manifest_split("train", transform=my_transforms)   # reads labels.csv
val   = imagefolder_split("val", transform=my_transforms)  # torchvision ImageFolder
```

Empty class folders contain a `.gitkeep` so the five-class structure is
uniform across all three splits.

## Filenames are traceable

```
wa03_t00019.961_f000499.jpg
└─┬─┘ └────┬───┘ └───┬────┘
  │        │         └── frame index in the source video
  │        └── timestamp in seconds
  └── source video id
```

---

## Counts

| class       | train | val | test | total |
|-------------|-------|-----|------|-------|
| 0 normal    | 373   | 23  | 67   | 463   |
| 1 fire      | 11    | 6   | 0    | 17    |
| 2 weapon    | 144   | 55  | 26   | 225   |
| 3 assault   | 43    | 0   | 0    | 43    |
| 4 robbery   | 17    | 0   | 0    | 17    |
| **total**   | 588   | 84  | 93   | 765   |

## Read this before training

* **Splitting is at source-video level.** Every frame of a video lives in
  exactly one split, so no frame of a training video appears in val or
  test. Verified: 0 videos in more than one split.
* **Labels never come from a folder or filename.** They come from
  `metadata/segments.csv`, a hand-reviewed table of temporal segments. A
  video contributes frames to several classes, and being in a "weapon"
  clip does not make a frame a weapon frame.
* **assault and robbery exist only in train.** Recall for those two
  classes cannot be measured on this dataset. Both classes also come from
  a single event each, so they are a seed for transfer learning, not
  trainable classes in their own right.
* **fire has no test frames** and only 17 frames total.
* **Only 2 physical locations** are represented (~74% one room, ~26% one
  corridor). A model trained on this alone will partly learn the room.
  The intended use is as a held-out evaluation set for models trained on
  the larger public corpora (RWF-2000, the Roboflow weapon and fire/smoke
  sets; see docs/2).
