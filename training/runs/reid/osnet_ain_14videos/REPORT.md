# Body re-id on the custom videos (osnet_ain_14videos)

Videos: vid02, vid03, vid05, vid06, vid07, wa01, wa03, mmtheft, mmassault, mmgun, mmknife, nvroom2, vid04, gun (train + val; wa02 never used). Sampled at 6 Hz, person boxes >= 80 px, same-person pairs >= 2 s apart on one track.

| Backend | same / different pairs | AUROC | median same / different | thr @ 1 % different | same accepted | thr @ 0.1 % | same accepted |
|---|---|---|---|---|---|---|---|
| osnet_ain_x1_0_msmt17 | 6891 / 2908 | 0.9349 | 0.725 / 0.5 | 0.661 | 0.6469 | 0.7313 | 0.485 |
| histogram | 6891 / 2908 | 0.6806 | 0.9067 / 0.8073 | 0.9686 | 0.2582 | 0.9773 | 0.2024 |

IntrusionSentry's threshold 0.50 on osnet_ain_x1_0_msmt17: accepts 95.8% of same-person and 50.0% of different-people pairs.

## Identity rule (D22): 20 looks, two checks 1 s apart

Bar: the smallest 0.01 step accepting <= 0.05% of different-people cases; chosen: the most same-person cases at its bar (fixed before the comparison).

| weights | cases same / different | AUROC | bar | same accepted | different accepted | 0.85: same / different |
|---|---|---|---|---|---|---|
| osnet_ain_x1_0_msmt17 **(chosen)** | 1500 / 25408 | 0.9658 | 0.87 | 30.1% | 0.020% | 40.3% / 0.189% |
