import os
import json
import numpy as np
from sklearn.metrics import roc_curve, auc, classification_report

class EvaluationRunner:
    def __init__(self, dataset_path="data/SENTRIX-EVAL/"):
        self.dataset_path = dataset_path
        self.results = {}
        # Ensure the mock eval directories exist
        os.makedirs(self.dataset_path, exist_ok=True)

    def evaluate_fusion(self):
        print("Evaluating Fusion Engine & TCI...")
        
        # In a real run, this would iterate over self.dataset_path
        # For demonstration, we generate synthetic testing points
        np.random.seed(42)
        num_samples = 500
        y_true = np.random.choice([0, 1], size=num_samples, p=[0.8, 0.2])
        
        naive_scores = []
        tci_scores = []
        
        try:
            import xgboost as xgb
            model_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "tci_xgboost.json")
            if os.path.exists(model_path):
                xgb_model = xgb.Booster()
                xgb_model.load_model(model_path)
            else:
                xgb_model = None
        except Exception:
            xgb_model = None

        for is_threat in y_true:
            v_conf = np.random.uniform(0.6, 0.95) if is_threat else np.random.uniform(0.0, 0.5)
            a_conf = np.random.uniform(0.1, 0.9) if is_threat else np.random.uniform(0.0, 0.6)
            m_conf = np.random.uniform(0.7, 1.0) if is_threat else np.random.uniform(0.0, 0.8)
            i_conf = np.random.uniform(0.0, 0.3) if is_threat else np.random.uniform(0.5, 1.0)
            is_night = np.random.choice([0, 1], p=[0.6, 0.4])
            
            # Baseline: Naive (matches old fusion logic)
            naive_base = (v_conf * 0.20) + (a_conf * 0.15) + (m_conf * 0.15) + (i_conf * 0.15)
            naive_scores.append(max(0.0, min(1.0, naive_base)))
            
            # Proposed: XGBoost TCI
            if xgb_model:
                def cal(score): return 1.0 / (1.0 + np.exp(-1.5 * score + 0.5))
                x_in = xgb.DMatrix([[cal(v_conf), cal(a_conf), cal(m_conf), cal(i_conf), float(is_night)]])
                tci_scores.append(float(xgb_model.predict(x_in)[0]))
            else:
                tci_scores.append(naive_base) # Fallback

        # Calculate ROC and AUC
        fpr_naive, tpr_naive, _ = roc_curve(y_true, naive_scores)
        self.results['auc_naive'] = auc(fpr_naive, tpr_naive)
        
        if xgb_model:
            fpr_tci, tpr_tci, _ = roc_curve(y_true, tci_scores)
            self.results['auc_tci'] = auc(fpr_tci, tpr_tci)
            print(f"Naive AUC:   {self.results['auc_naive']:.3f}")
            print(f"XGBoost AUC: {self.results['auc_tci']:.3f}")
        else:
            print("[Warning] XGBoost model not loaded. Skipping XGBoost evaluation.")

if __name__ == "__main__":
    runner = EvaluationRunner()
    runner.evaluate_fusion()
