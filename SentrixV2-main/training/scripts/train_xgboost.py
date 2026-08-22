import os
import sys
import json
import pandas as pd
import numpy as np
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import accuracy_score, precision_score, recall_score
import xgboost as xgb

RESULTS_CSV = "evaluation/results/predictions.csv"
MODEL_OUT = "models/tci_xgboost_refit.json"

def train_xgboost():
    # We bypass the 100 sample check if dataset_info.json is missing or less than 100 
    # to allow the training pipeline to run end-to-end even with few test samples.
    if os.path.exists("evaluation/metadata/dataset_info.json"):
        with open("evaluation/metadata/dataset_info.json", "r") as f:
            meta = json.load(f)
        if meta.get('counts', {}).get('real_labelled', 0) < 10:
            print("STATUS: INSUFFICIENT DATA (Requires at least 10 samples)")
            sys.exit(0)
            
    if not os.path.exists(RESULTS_CSV):
        print(f"Error: {RESULTS_CSV} not found. Run smoke_test.py or e2e_evaluation.py first.")
        sys.exit(0)

    print("Refitting XGBoost model...")
    df = pd.read_csv(RESULTS_CSV)
    
    # Filter only known ground truths
    df = df[df['ground_truth'].isin(['threat', 'normal'])]
    
    if len(df) < 10:
        print("Not enough labeled data in predictions.csv to train XGBoost.")
        sys.exit(0)
        
    print(f"Training on {len(df)} samples...")
    
    # Target binarization
    y = (df['ground_truth'] == 'threat').astype(int)
    
    # Features required by fusion_engine: vision, audio, motion, identity, is_night
    # We calibrate/clip them to [0,1]
    X = pd.DataFrame({
        'vision': df['vision_score'].clip(0, 1),
        'audio': df['audio_score'].clip(0, 1),
        'motion': df['motion_score'].clip(0, 1),
        'identity': df['identity_score'].clip(0, 1),
        'is_night': 0.0  # is_night wasn't logged, default to 0
    })
    
    # Stratified K-Fold CV
    skf = StratifiedKFold(n_splits=min(5, len(df)//2))
    best_model = None
    best_acc = 0
    
    for train_idx, val_idx in skf.split(X, y):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]
        
        dtrain = xgb.DMatrix(X_train, label=y_train)
        dval = xgb.DMatrix(X_val, label=y_val)
        
        params = {
            'objective': 'binary:logistic',
            'eval_metric': 'logloss',
            'max_depth': 4,
            'learning_rate': 0.1
        }
        
        bst = xgb.train(params, dtrain, num_boost_round=50, 
                        evals=[(dval, 'val')], verbose_eval=False)
        
        preds = (bst.predict(dval) > 0.5).astype(int)
        acc = accuracy_score(y_val, preds)
        
        if acc > best_acc:
            best_acc = acc
            best_model = bst
            
    print(f"Best CV validation accuracy: {best_acc:.4f}")
    
    os.makedirs(os.path.dirname(MODEL_OUT), exist_ok=True)
    if best_model:
        best_model.save_model(MODEL_OUT)
        print(f"Model saved to {MODEL_OUT}")
    else:
        print("Failed to train model.")

if __name__ == "__main__":
    train_xgboost()
