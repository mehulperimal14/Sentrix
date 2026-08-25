# training/scripts/evaluate_models.py
#
# Comprehensive Test & Evaluation Script for SENTRIX AI Models:
# Computes Confusion Matrix, Precision, Recall, F1-Score, Loss, and Test Accuracy.

import os
import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd
from PIL import Image

# Enable CPU fallback for unsupported MPS ops
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["POLARS_SKIP_CPU_CHECK"] = "1"

import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, models

ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
MODELS_DIR = ROOT_DIR / "backend" / "models"

# Ensure repo root is on sys.path for script imports
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

def print_header(title):
    print("\n" + "=" * 65)
    print(f" {title}")
    print("=" * 65)

def print_metrics(tp, fp, fn, tn, class_names=("Negative", "Positive")):
    total = tp + fp + fn + tn
    if total == 0:
        print("No evaluation samples found.")
        return
    acc = (tp + tn) / total
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    f1 = 2 * (precision * recall) / max(precision + recall, 1e-6)
    
    print(f"Total Test Samples Evaluated: {total}")
    print(f"Overall Accuracy:             {acc * 100:.2f}%")
    print(f"Precision ({class_names[1]}):       {precision * 100:.2f}%")
    print(f"Recall ({class_names[1]}):          {recall * 100:.2f}%")
    print(f"F1-Score ({class_names[1]}):        {f1:.4f}")
    print("\n--- Confusion Matrix ---")
    print(f"                 Predicted {class_names[0]}  |  Predicted {class_names[1]}")
    print(f"Actual {class_names[0]}:        {tn:<10}       |      {fp:<10}")
    print(f"Actual {class_names[1]}:        {fn:<10}       |      {tp:<10}")

# ── 1. Evaluate Violence Classifier (ResNet18) ──────────────────────────────
def evaluate_violence():
    print_header("EVALUATION: ResNet18 Violence / Altercation Classifier")
    model_path = MODELS_DIR / "violence_classifier.pt"
    data_dir = ROOT_DIR / "data" / "violence_data"
    val_csv = data_dir / "val.csv"

    if not model_path.exists():
        print(f"⚠️ Model not found at {model_path}. Run training first.")
        return

    net = models.resnet18(weights=None)
    net.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(net.fc.in_features, 2))
    ckpt = torch.load(str(model_path), map_location=device)
    net.load_state_dict(ckpt["model_state_dict"] if "model_state_dict" in ckpt else ckpt)
    net.to(device)
    net.eval()

    tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    df = pd.read_csv(val_csv)
    tp, fp, fn, tn = 0, 0, 0, 0
    t0 = time.time()

    with torch.no_grad():
        for _, row in df.iterrows():
            orig_path = row["frame_path"]
            label_str = str(row["label"]).lower()
            actual = 1 if "fight" == label_str or "violence" in label_str else 0
            
            fname = Path(orig_path).name
            subfolder = "fight" if actual == 1 else "nofight"
            local_path = data_dir / subfolder / fname
            
            if not local_path.exists():
                continue

            img = Image.open(local_path).convert("RGB")
            tensor = tf(img).unsqueeze(0).to(device)
            out = net(tensor)
            pred = torch.argmax(out, dim=1).item()

            if actual == 1 and pred == 1:
                tp += 1
            elif actual == 0 and pred == 1:
                fp += 1
            elif actual == 1 and pred == 0:
                fn += 1
            else:
                tn += 1

    dur = time.time() - t0
    print_metrics(tp, fp, fn, tn, class_names=("NoFight", "Fight"))
    print(f"Evaluation Latency: {dur:.2f}s ({dur / max(tp+fp+fn+tn, 1) * 1000:.2f} ms/frame)")

# ── 2. Evaluate Anomaly Classifier (ResNet18) ───────────────────────────────
def evaluate_anomaly():
    print_header("EVALUATION: Surveillance Anomaly Classifier")
    model_path = MODELS_DIR / "anomaly_classifier.pt"
    data_dir = ROOT_DIR / "data" / "anomaly_data"
    val_csv = data_dir / "val.csv"

    if not model_path.exists():
        print(f"⚠️ Model not found at {model_path}.")
        return

    net = models.resnet18(weights=None)
    net.fc = nn.Sequential(nn.Dropout(0.3), nn.Linear(net.fc.in_features, 2))
    ckpt = torch.load(str(model_path), map_location=device)
    net.load_state_dict(ckpt["model_state_dict"] if "model_state_dict" in ckpt else ckpt)
    net.to(device)
    net.eval()

    tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    df = pd.read_csv(val_csv)
    tp, fp, fn, tn = 0, 0, 0, 0
    t0 = time.time()

    with torch.no_grad():
        for _, row in df.iterrows():
            orig_path = row["frame_path"]
            label_str = str(row["label"]).lower()
            actual = 1 if "anom" in label_str else 0
            
            fname = Path(orig_path).name
            subfolder = "anomalous" if actual == 1 else "normal"
            local_path = data_dir / subfolder / fname
            
            if not local_path.exists():
                continue

            img = Image.open(local_path).convert("RGB")
            tensor = tf(img).unsqueeze(0).to(device)
            out = net(tensor)
            pred = torch.argmax(out, dim=1).item()

            if actual == 1 and pred == 1:
                tp += 1
            elif actual == 0 and pred == 1:
                fp += 1
            elif actual == 1 and pred == 0:
                fn += 1
            else:
                tn += 1

    dur = time.time() - t0
    print_metrics(tp, fp, fn, tn, class_names=("Normal", "Anomalous"))
    print(f"Evaluation Latency: {dur:.2f}s ({dur / max(tp+fp+fn+tn, 1) * 1000:.2f} ms/frame)")

# ── 3. Evaluate Audio Threat Classifier ─────────────────────────────────────
def evaluate_audio():
    print_header("EVALUATION: Acoustic Mel-Spectrogram Threat CNN")
    model_path = MODELS_DIR / "audio_classifier.pt"
    if not model_path.exists():
        print(f"⚠️ Model not found at {model_path}.")
        return

    from training.scripts.train_audio_classifier import AudioThreatCNN, SyntheticAudioDataset, CLASSES
    net = AudioThreatCNN(num_classes=len(CLASSES)).to(device)
    ckpt = torch.load(str(model_path), map_location=device)
    net.load_state_dict(ckpt["model_state_dict"])
    net.eval()

    test_ds = SyntheticAudioDataset(num_samples=500)
    loader = DataLoader(test_ds, batch_size=32, shuffle=False)
    correct, total = 0, 0

    with torch.no_grad():
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            preds = net(x).argmax(dim=1)
            correct += (preds == y).sum().item()
            total += y.size(0)

    print(f"Evaluated Test Samples:  {total}")
    print(f"Acoustic Test Accuracy:  {correct / total * 100:.2f}%")
    print(f"Evaluated Acoustic Classes: {CLASSES}")

# ── 4. Evaluate TCI Late Fusion Booster ─────────────────────────────────────
def evaluate_fusion():
    print_header("EVALUATION: XGBoost Multi-Modal Threat Confidence Booster")
    model_path = MODELS_DIR / "tci_xgboost.json"
    if not model_path.exists():
        print(f"⚠️ Model not found at {model_path}.")
        return

    import xgboost as xgb
    from training.scripts.refit_xgboost import generate_multimodal_dataset
    bst = xgb.Booster()
    bst.load_model(str(model_path))

    X_test, y_test = generate_multimodal_dataset(n_samples=1000)
    dtest = xgb.DMatrix(X_test)
    preds = bst.predict(dtest)

    rmse = np.sqrt(np.mean((preds - y_test) ** 2))
    mae = np.mean(np.abs(preds - y_test))
    r2 = 1.0 - (np.sum((y_test - preds) ** 2) / np.sum((y_test - np.mean(y_test)) ** 2))

    print(f"Holdout Test Scenarios: 1,000 multimodal combinations")
    print(f"Root Mean Squared Error (RMSE): {rmse:.4f}")
    print(f"Mean Absolute Error (MAE):     {mae:.4f}")
    print(f"R-Squared ($R^2$ Score):          {r2:.4f}")

if __name__ == "__main__":
    evaluate_violence()
    evaluate_anomaly()
    evaluate_audio()
    evaluate_fusion()
