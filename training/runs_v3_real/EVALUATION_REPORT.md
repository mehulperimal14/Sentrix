```text
======================================================================
SENTRIX V3 REAL-DATA — CONSOLIDATED EVALUATION
======================================================================
Run ID   : 20260824_193729
Data root: G:\Capstone\data
Models   : G:\Sentrix\backend\models\v3_real

Module status
----------------------------------------
  violence    : TRAINED
  audio       : TRAINED
  weapon      : TRAINED
  fire_smoke  : TRAINED
  fusion      : SKIPPED

VIOLENCE
----------------------------------------
  Accuracy            : 0.8326
  Precision (macro)   : 0.8340
  Recall (macro)      : 0.8314
  F1 (macro)          : 0.8319
  PR-AUC              : 0.8827
  False Negative Rate : 0.1325
  Operating threshold : 0.398
  Confusion matrix    : [[1511, 389], [269, 1761]]

AUDIO
----------------------------------------
  Accuracy   : 0.7200
  Macro F1   : 0.7036
    speech_normal   P 0.700  R 0.875  F1 0.778  n=24
    siren           P 0.933  R 0.538  F1 0.683  n=26
    fire            P 0.684  R 0.765  F1 0.722  n=17
    scream          P 0.545  R 0.750  F1 0.632  n=8
  Confusion matrix: [[21, 0, 2, 1], [4, 14, 4, 4], [4, 0, 13, 0], [1, 1, 0, 6]]

WEAPON
----------------------------------------
  mAP50     : 0.9080
  mAP50-95  : 0.6156
  Precision : 0.9147
  Recall    : 0.8517
    knife       : 0.5349
    long_gun    : 0.6313
    pistol      : 0.6807

FIRE_SMOKE
----------------------------------------
  mAP50     : 0.7189
  mAP50-95  : 0.4038
  Precision : 0.7232
  Recall    : 0.6680
    fire        : 0.4730
    smoke       : 0.3346

FUSION (TCI)
----------------------------------------
  NOT TRAINED - no real labelled multimodal event dataset was found.
  Synthetic data generation is disabled by design.

```
