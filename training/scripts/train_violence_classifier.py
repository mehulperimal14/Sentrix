# training/scripts/train_violence_classifier.py
#
# Trains a PyTorch ResNet18 vision classifier on data/violence_data (fight vs nofight)
# Output saved to backend/models/violence_classifier.pt

import os
import sys
import time
from pathlib import Path
import pandas as pd
from PIL import Image
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms, models

ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = ROOT_DIR / "data" / "violence_data"
BACKEND_MODELS_DIR = ROOT_DIR / "backend" / "models"
BACKEND_MODELS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_MODEL = BACKEND_MODELS_DIR / "violence_classifier.pt"

class ViolenceDataset(Dataset):
    def __init__(self, csv_file, data_dir, transform=None):
        self.data_dir = Path(data_dir)
        df = pd.read_csv(csv_file)
        self.samples = []
        self.transform = transform
        
        for _, row in df.iterrows():
            orig_path = row["frame_path"]
            label_str = str(row["label"]).lower()
            label = 1 if "fight" == label_str or "violence" in label_str else 0
            
            # Extract filename and locate inside fight/ or nofight/
            fname = Path(orig_path).name
            subfolder = "fight" if label == 1 else "nofight"
            local_path = self.data_dir / subfolder / fname
            
            if local_path.exists():
                self.samples.append((local_path, label))
                
        print(f"[Dataset] Loaded {len(self.samples)} valid samples from {csv_file.name}")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        path, label = self.samples[idx]
        img = Image.open(path).convert("RGB")
        if self.transform:
            img = self.transform(img)
        return img, torch.tensor(label, dtype=torch.long)

def build_model():
    model = models.resnet18(weights=models.ResNet18_Weights.DEFAULT)
    # Binary classification head (0: nofight, 1: fight)
    num_ftrs = model.fc.in_features
    model.fc = nn.Sequential(
        nn.Dropout(0.3),
        nn.Linear(num_ftrs, 2)
    )
    return model

def get_device():
    if torch.backends.mps.is_available():
        return torch.device("mps")
    elif torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")

def train(epochs=5, batch_size=32, lr=1e-4):
    print("=" * 60)
    print("SENTRIX: Training Violence Classifier (ResNet18)")
    print(f"Data: {DATA_DIR}")
    print(f"Target: {OUTPUT_MODEL}")
    device = get_device()
    print(f"Hardware Acceleration Device: {device}")
    print("=" * 60)

    train_tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    val_tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    train_ds = ViolenceDataset(DATA_DIR / "train.csv", DATA_DIR, transform=train_tf)
    val_ds = ViolenceDataset(DATA_DIR / "val.csv", DATA_DIR, transform=val_tf)

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=2)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=2)

    model = build_model().to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-3)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_acc = 0.0

    for epoch in range(1, epochs + 1):
        t0 = time.time()
        model.train()
        running_loss, correct, total = 0.0, 0, 0
        
        for images, labels in train_loader:
            images, labels = images.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(images)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            
            running_loss += loss.item() * images.size(0)
            _, preds = torch.max(outputs, 1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)

        scheduler.step()
        train_loss = running_loss / max(total, 1)
        train_acc = correct / max(total, 1)

        # Validation
        model.eval()
        v_loss, v_correct, v_total = 0.0, 0, 0
        with torch.no_grad():
            for images, labels in val_loader:
                images, labels = images.to(device), labels.to(device)
                outputs = model(images)
                loss = criterion(outputs, labels)
                v_loss += loss.item() * images.size(0)
                _, preds = torch.max(outputs, 1)
                v_correct += (preds == labels).sum().item()
                v_total += labels.size(0)

        val_loss = v_loss / max(v_total, 1)
        val_acc = v_correct / max(v_total, 1)
        dur = time.time() - t0

        print(f"Epoch {epoch}/{epochs} ({dur:.1f}s) | Train Loss: {train_loss:.4f} Acc: {train_acc*100:.2f}% | Val Loss: {val_loss:.4f} Acc: {val_acc*100:.2f}%")

        if val_acc > best_val_acc or epoch == 1:
            best_val_acc = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "arch": "resnet18_binary",
                "classes": ["nofight", "fight"],
                "val_acc": val_acc
            }, OUTPUT_MODEL)
            print(f"  --> Saved new best checkpoint (Val Acc: {val_acc*100:.2f}%)")

    print(f"\n✅ Violence classifier training complete. Final model: {OUTPUT_MODEL}")

if __name__ == "__main__":
    epochs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    train(epochs=epochs)
