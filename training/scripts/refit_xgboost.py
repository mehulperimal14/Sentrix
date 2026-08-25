# training/scripts/refit_xgboost.py
#
# Trains and refits the multi-modal late fusion XGBoost booster
# Output saved to backend/models/tci_xgboost.json

import os
import json
import numpy as np
from pathlib import Path
import xgboost as xgb

ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
BACKEND_MODELS_DIR = ROOT_DIR / "backend" / "models"
BACKEND_MODELS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_MODEL = BACKEND_MODELS_DIR / "tci_xgboost.json"

def generate_multimodal_dataset(n_samples=5000):
    """
    Generates synthetic grounded multi-modal data points representing realistic
    home security telemetry scenarios with varying sensor agreement and conflicts.
    Features: [cal_vision, cal_audio, cal_motion, cal_identity, is_night]
    Target: Continuous TCI in [0.0, 1.0]
    """
    np.random.seed(42)
    X = []
    y = []

    for _ in range(n_samples):
        # Scenario distribution
        scenario = np.random.choice(["normal", "suspicious", "elevated", "high_threat", "authorized"])
        is_night = np.random.choice([0.0, 1.0], p=[0.6, 0.4])

        if scenario == "normal":
            v = np.random.beta(1.5, 8.0) # low vision score
            a = np.random.beta(1.2, 8.0)
            m = np.random.beta(1.5, 7.0)
            i = np.random.beta(1.0, 5.0)
            target = 0.15 * v + 0.10 * a + 0.10 * m + 0.05 * is_night
        elif scenario == "authorized":
            v = np.random.beta(4.0, 2.0)
            a = np.random.beta(1.0, 6.0)
            m = np.random.beta(3.0, 3.0)
            i = np.random.beta(8.0, 1.5) # recognized resident
            target = 0.05 + 0.05 * v
        elif scenario == "suspicious":
            v = np.random.beta(3.0, 3.0)
            a = np.random.beta(2.5, 4.0)
            m = np.random.beta(4.0, 2.5)
            i = np.random.beta(1.0, 6.0) # stranger
            target = 0.35 + 0.20 * v + 0.15 * m + 0.10 * is_night
        elif scenario == "elevated":
            v = np.random.beta(5.0, 2.0)
            a = np.random.beta(4.5, 2.5)
            m = np.random.beta(5.0, 2.0)
            i = np.random.beta(1.0, 8.0)
            target = 0.55 + 0.20 * v + 0.15 * a + 0.10 * is_night
        else: # high_threat
            v = np.random.beta(7.0, 1.5)
            a = np.random.beta(7.0, 1.5)
            m = np.random.beta(6.0, 1.5)
            i = np.random.beta(0.5, 9.0)
            target = 0.75 + 0.15 * v + 0.10 * a + 0.05 * is_night

        target = float(np.clip(target, 0.0, 1.0))
        X.append([v, a, m, i, is_night])
        y.append(target)

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32)

def train():
    print("=" * 60)
    print("SENTRIX: Refitting Multi-Modal TCI XGBoost Model")
    print(f"Target: {OUTPUT_MODEL}")
    print("=" * 60)

    X, y = generate_multimodal_dataset(n_samples=8000)
    split = int(len(X) * 0.8)
    X_train, y_train = X[:split], y[:split]
    X_val, y_val = X[split:], y[split:]

    dtrain = xgb.DMatrix(X_train, label=y_train)
    dval = xgb.DMatrix(X_val, label=y_val)

    params = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "max_depth": 4,
        "eta": 0.05,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "tree_method": "hist"
    }

    evals = [(dtrain, "train"), (dval, "val")]
    bst = xgb.train(
        params,
        dtrain,
        num_boost_round=150,
        evals=evals,
        early_stopping_rounds=15,
        verbose_eval=25
    )

    bst.save_model(str(OUTPUT_MODEL))
    print(f"\n✅ XGBoost TCI Late Fusion model saved: {OUTPUT_MODEL}")

if __name__ == "__main__":
    train()
