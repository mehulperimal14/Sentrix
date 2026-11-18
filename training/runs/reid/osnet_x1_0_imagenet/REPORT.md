# Body re-id on the custom videos (osnet_x1_0_imagenet)

Videos: vid02, vid03, vid05, vid06, vid07, wa01, wa03, vid04, gun (train + val; wa02 never used). Sampled at 6 Hz, person boxes >= 80 px, same-person pairs >= 2 s apart on one track.

| Backend | same / different pairs | AUROC | median same / different | thr @ 1 % different | same accepted | thr @ 0.1 % | same accepted |
|---|---|---|---|---|---|---|---|
| osnet | 4524 / 2498 | 0.8306 | 0.7506 / 0.6148 | 0.7702 | 0.4346 | 0.8356 | 0.2518 |
| histogram | 4524 / 2498 | 0.7283 | 0.9345 / 0.8221 | 0.9692 | 0.3011 | 0.9779 | 0.2321 |

IntrusionSentry's threshold 0.50 on OSNet: accepts 97.8% of same-person and 97.2% of different-people pairs.

## Identity rule (D22): 20 looks, two checks 1 s apart, OSNet

1194 same-person / 23546 different-people cases, AUROC 0.9555.

| bar | same accepted | different accepted |
|---|---|---|
| 0.8106 (different 1.0%) | 65.3% | 1.0% |
| 0.8228 (different 0.5%) | 60.1% | 0.5% |
| 0.8432 (different 0.1%) | 43.0% | 0.1% |
| 0.80 | 69.9% | 1.57% |
| 0.85 | 37.3% | 0.04% |
| 0.88 | 16.5% | 0.00% |
| 0.90 | 10.6% | 0.00% |
