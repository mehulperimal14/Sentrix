import os
import sys
import json
import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score, confusion_matrix, roc_auc_score

RESULTS_CSV = "evaluation/results/predictions.csv"
METRICS_JSON = "evaluation/results/baseline_metrics.json"

def calculate_metrics():
    if not os.path.exists(RESULTS_CSV):
        print(f"ERROR: {RESULTS_CSV} not found. Run e2e_evaluation.py first.")
        sys.exit(1)

    df = pd.read_csv(RESULTS_CSV)
    
    # Filter out unlabelled / synthetic data. 
    # In a real scenario, synthetic/demo images wouldn't be used for baseline.
    # Currently we only have 3 synthetic images, so real_labelled count is 0.
    
    with open("evaluation/metadata/dataset_info.json", "r") as f:
        meta = json.load(f)
        
    if meta['counts']['real_labelled'] == 0:
        print("STATUS: INSUFFICIENT DATA")
        print("Cannot calculate baseline metrics: 0 real labelled samples found.")
        print("Please populate evaluation/data/ with labelled images and re-run harness.")
        sys.exit(0)

    # (Code below would run if real data existed)
    print("Calculating metrics...")
    
    y_true = df['ground_truth'].apply(lambda x: 1 if x in ['threat', 'weapon', 'fire', 'suspicious'] else 0)
    y_pred = df['final_decision'].apply(lambda x: 1 if x in ['ELEVATED', 'HIGH', 'CRITICAL'] else 0)

    precision = precision_score(y_true, y_pred, zero_division=0)
    recall = recall_score(y_true, y_pred, zero_division=0)
    f1 = f1_score(y_true, y_pred, zero_division=0)
    
    metrics = {
        "system_level": {
            "precision": precision,
            "recall": recall,
            "f1": f1
        },
        "individual_models": {
            "vision": "NOT VERIFIED",
            "audio": "NOT VERIFIED", 
            "behaviour": "NOT VERIFIED",
            "identity": "NOT VERIFIED",
            "weapon": "NOT VERIFIED",
            "fire": "NOT VERIFIED"
        }
    }
    
    with open(METRICS_JSON, "w") as f:
        json.dump(metrics, f, indent=2)
        
    print(f"Metrics saved to {METRICS_JSON}")

if __name__ == "__main__":
    calculate_metrics()
