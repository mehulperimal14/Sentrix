#!/usr/bin/env python3
"""
train_anomaly_classifier.py
===========================
PyTorch script to train MobileNetV2 for anomaly detection (Model F).
Binary classification: Normal vs Anomalous.
Designed for RTX 4060 GPU. Incremental training supported.

Changes from original:
- Added validation loop with accuracy metrics after every epoch
- Per-epoch checkpoint saving: models/anomaly_ep{N:02d}.pt  (no overwrites)
- Best model tracking saved to --output (only overwrites if val_acc improves)
"""

import os
import argparse
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import torchvision.models as models
from torchvision import transforms
import pandas as pd
from PIL import Image


class AnomalyDataset(Dataset):
    def __init__(self, csv_file, transform=None):
        self.df = pd.read_csv(csv_file)
        self.transform = transform
        
    def __len__(self):
        return len(self.df)
        
    def __getitem__(self, idx):
        row = self.df.iloc[idx]
        img_path = row['frame_path']
        img = Image.open(img_path).convert('RGB')
        
        if self.transform:
            img = self.transform(img)
            
        label = 1 if row['label'] == 'anomalous' else 0
        return img, label


def get_model():
    model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
    model.classifier[1] = nn.Linear(model.last_channel, 2)
    return model


def evaluate(model, loader, criterion, device):
    """Run validation; return (avg_loss, accuracy)."""
    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    with torch.no_grad():
        for imgs, labels in loader:
            imgs, labels = imgs.to(device), labels.to(device)
            outputs = model(imgs)
            loss = criterion(outputs, labels)
            total_loss += loss.item()
            preds = outputs.argmax(dim=1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)
    return total_loss / len(loader), correct / total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',   type=str, required=True)
    parser.add_argument('--output', type=str, default='anomaly_classifier.pt')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch',  type=int, default=32)
    parser.add_argument('--device', type=str, default='cuda')
    args = parser.parse_args()
    
    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")
    if device.type == 'cuda':
        print(f"  GPU: {torch.cuda.get_device_name(0)}")

    ckpt_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(ckpt_dir, exist_ok=True)
    
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    train_ds = AnomalyDataset(os.path.join(args.data, 'train.csv'), transform)
    val_ds   = AnomalyDataset(os.path.join(args.data, 'val.csv'),   transform)
    
    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True,  num_workers=2, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch, shuffle=False, num_workers=2, pin_memory=True)
    
    print(f"Train frames: {len(train_ds)}  |  Val frames: {len(val_ds)}")
    
    model = get_model().to(device)
    
    # Resume if best checkpoint exists
    if os.path.exists(args.output):
        print(f"Resuming from checkpoint: {args.output}")
        model.load_state_dict(torch.load(args.output, map_location=device))
        
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=1e-4)
    scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=8, gamma=0.5)
    
    best_val_acc = 0.0
    history = []

    for epoch in range(1, args.epochs + 1):
        # --- Train ---
        model.train()
        total_loss = 0.0
        for imgs, labels in train_loader:
            imgs, labels = imgs.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(imgs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        
        train_loss = total_loss / len(train_loader)

        # --- Validate ---
        val_loss, val_acc = evaluate(model, val_loader, criterion, device)
        scheduler.step()

        print(
            f"Epoch [{epoch:02d}/{args.epochs}]  "
            f"train_loss={train_loss:.4f}  "
            f"val_loss={val_loss:.4f}  "
            f"val_acc={val_acc:.4f}"
        )

        history.append({'epoch': epoch, 'train_loss': train_loss, 'val_loss': val_loss, 'val_acc': val_acc})

        # --- Per-epoch checkpoint (no overwrite — unique filename) ---
        ep_ckpt = os.path.join(ckpt_dir, f"anomaly_ep{epoch:02d}.pt")
        torch.save(model.state_dict(), ep_ckpt)

        # --- Best model (only updates when val_acc improves) ---
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), args.output)
            print(f"  ✓ New best val_acc={best_val_acc:.4f} — saved to {args.output}")

    print(f"\nTraining complete. Best val_acc: {best_val_acc:.4f}")
    print(f"Best model: {args.output}")

    hist_path = os.path.join(ckpt_dir, "anomaly_history.csv")
    pd.DataFrame(history).to_csv(hist_path, index=False)
    print(f"History saved: {hist_path}")


if __name__ == "__main__":
    main()
