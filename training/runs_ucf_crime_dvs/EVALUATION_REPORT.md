```text
==================================================================
SENTRIX — UCF-CRIME-DVS EVENT ANOMALY MODEL
==================================================================
run id        : 20260824_132146
dataset       : G:\event_frame_duration533326\event_frame_duration533326
split source  : official train_split.txt / test_split.txt
backend       : spiking (SpikingJelly LIF)

videos        : train 1316 | val 233 | test 287

val   ROC-AUC : 0.8263   AP 0.8587
test  ROC-AUC : 0.1940   AP 0.3372
test  P/R/F1  : 0.4538 / 0.8429 / 0.5900  @ threshold 0.151
test  FNR     : 0.1571
frame ROC-AUC : not evaluated (no official GT)
category acc  : 0.0767  (weak labels - context only)

SENTRIX contract: TCI consumes anomaly_score ONLY.
==================================================================
```