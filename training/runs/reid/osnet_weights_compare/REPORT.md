# Body re-id on the custom videos (osnet_weights_compare)

Videos: vid02, vid03, vid05, vid06, vid07, wa01, wa03, vid04, gun (train + val; wa02 never used). Sampled at 6 Hz, person boxes >= 80 px, same-person pairs >= 2 s apart on one track.

| Backend | same / different pairs | AUROC | median same / different | thr @ 1 % different | same accepted | thr @ 0.1 % | same accepted |
|---|---|---|---|---|---|---|---|
| osnet_x1_0_imagenet | 4524 / 2498 | 0.8306 | 0.7506 / 0.6148 | 0.7702 | 0.4346 | 0.8356 | 0.2518 |
| osnet_x1_0_msmt17 | 4524 / 2498 | 0.9172 | 0.7033 / 0.4637 | 0.638 | 0.6538 | 0.6918 | 0.5243 |
| osnet_ain_x1_0_msmt17 | 4524 / 2498 | 0.931 | 0.7274 / 0.5067 | 0.6638 | 0.6368 | 0.7321 | 0.4903 |
| histogram | 4524 / 2498 | 0.7283 | 0.9345 / 0.8221 | 0.9692 | 0.3011 | 0.9779 | 0.2321 |

IntrusionSentry's threshold 0.50 on osnet_x1_0_imagenet: accepts 97.8% of same-person and 97.2% of different-people pairs.
IntrusionSentry's threshold 0.50 on osnet_x1_0_msmt17: accepts 89.3% of same-person and 29.4% of different-people pairs.
IntrusionSentry's threshold 0.50 on osnet_ain_x1_0_msmt17: accepts 96.1% of same-person and 54.4% of different-people pairs.

## Identity rule (D22): 20 looks, two checks 1 s apart

Bar: the smallest 0.01 step accepting <= 0.05% of different-people cases; chosen: the most same-person cases at its bar (fixed before the comparison).

| weights | cases same / different | AUROC | bar | same accepted | different accepted | 0.85: same / different |
|---|---|---|---|---|---|---|
| osnet_x1_0_imagenet | 1194 / 23546 | 0.9555 | 0.85 | 37.3% | 0.038% | 37.3% / 0.038% |
| osnet_x1_0_msmt17 | 1194 / 23546 | 0.984 | 0.84 | 39.0% | 0.030% | 33.4% / 0.008% |
| osnet_ain_x1_0_msmt17 **(chosen)** | 1194 / 23546 | 0.9848 | 0.86 | 42.3% | 0.042% | 47.1% / 0.106% |
