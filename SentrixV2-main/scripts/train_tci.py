import os
import json
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, classification_report

MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
os.makedirs(MODELS_DIR, exist_ok=True)
MODEL_PATH = os.path.join(MODELS_DIR, "tci_xgboost.json")

def generate_synthetic_data(num_samples=5000):
    """
    Generates synthetic evaluation data based on realistic model failure modes.
    Features: [cal_vision, cal_audio, cal_motion, cal_identity, is_night]
    Target: is_threat (0 or 1)
    """
    np.random.seed(42)
    
    # 0 = Normal/Benign, 1 = Threat
    y = np.random.choice([0, 1], size=num_samples, p=[0.8, 0.2])
    
    X = np.zeros((num_samples, 5))
    
    for i in range(num_samples):
        is_threat = y[i]
        is_night = np.random.choice([0, 1], p=[0.6, 0.4])
        
        if is_threat:
            # Threat scenarios (High vision/motion, maybe audio)
            v_conf = np.random.uniform(0.6, 0.95)
            a_conf = np.random.uniform(0.1, 0.9)
            m_conf = np.random.uniform(0.7, 1.0)
            i_conf = np.random.uniform(0.0, 0.3) # Unauthorized usually
        else:
            # Benign scenarios (e.g. family walking, pets, wind noise)
            v_conf = np.random.uniform(0.0, 0.5)
            a_conf = np.random.uniform(0.0, 0.6) # Occasional high audio (false positive)
            m_conf = np.random.uniform(0.0, 0.8) # Pets cause motion
            i_conf = np.random.uniform(0.5, 1.0) # Often authorized
            
            # Simulate a specific False Positive: Wind noise at night
            if is_night and np.random.random() < 0.1:
                a_conf = np.random.uniform(0.7, 0.9) # High audio
                
            # Simulate a specific False Positive: Owner walking
            if np.random.random() < 0.2:
                v_conf = np.random.uniform(0.7, 0.9)
                m_conf = np.random.uniform(0.7, 0.9)
                i_conf = np.random.uniform(0.8, 1.0) # Owner!
                
        X[i] = [v_conf, a_conf, m_conf, i_conf, is_night]
        
    return X, y

def train():
    print("[Train TCI] Generating synthetic SENTRIX-EVAL data...")
    X, y = generate_synthetic_data()
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    print("[Train TCI] Training XGBoost Late Fusion Classifier...")
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        use_label_encoder=False,
        eval_metric='logloss'
    )
    
    model.fit(X_train, y_train)
    
    # Evaluate
    preds_proba = model.predict_proba(X_test)[:, 1]
    preds = model.predict(X_test)
    
    auc = roc_auc_score(y_test, preds_proba)
    print(f"\n[Validation] XGBoost ROC-AUC: {auc:.4f}")
    print("[Validation] Classification Report:")
    print(classification_report(y_test, preds))
    
    # Save
    model.save_model(MODEL_PATH)
    print(f"[Success] Saved XGBoost model to {MODEL_PATH}")

if __name__ == "__main__":
    train()
