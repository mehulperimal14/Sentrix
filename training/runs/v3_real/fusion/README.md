# Fusion run — deliberately empty

The V3 retraining notebook searched for a real labelled multimodal event dataset,
found none, and stopped rather than manufacturing one:

```
FUSION TRAINING NOT EXECUTED
Reason: No real multimodal ground-truth dataset was found.
Synthetic data generation is disabled by design.
```

That was the right call, and this directory is left in place as the record of it.

The deployed `models/tci_xgboost.json` therefore still comes from
`training/scripts/refit_xgboost.py`, which generates its own labels from a
formula. Its reported `R² = 0.984` measures only that XGBoost can memorise that
formula — it is not a model result and should not be quoted as one.

To fill this directory with a real model:

1. `python tools/replay.py --clips data/clips`   → writes `var/events/*.jsonl`
2. `notebooks/02_tci_calibration.ipynb`          → calibrate
3. `notebooks/03_tci_thresholds.ipynb`           → derive level bands

The feature contract to train against is `CONTRACT_V2` in
`sentrix/fusion/features.py` (8 features). Do not train against the legacy
5-feature contract.
