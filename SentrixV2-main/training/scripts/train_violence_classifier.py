#!/usr/bin/env python3
"""
train_violence_classifier.py
============================
PyTorch script to train ResNet18 + LSTM for violence detection.
Designed for RTX 4060 GPU. Incremental training supported.

Changes from original:
- Added validation loop with accuracy metrics after every epoch
- Per-epoch checkpoint saving: models/violence_ep{N:02d}.pt  (no overwrites)
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


class ViolenceDataset(Dataset):
    def __init__(self, csv_file, transform=None):
        self.df = pd.read_csv(csv_file)
        # Group by clip
        self.clips = self.df.groupby('clip')
        self.clip_names = list(self.clips.groups.keys())
        self.transform = transform
        
    def __len__(self):
        return len(self.clip_names)
        
    def __getitem__(self, idx):
        clip_name = self.clip_names[idx]
        group = self.clips.get_group(clip_name)
        
        frames = []
        for img_path in group['frame_path'].values:
            img = Image.open(img_path).convert('RGB')
            if self.transform:
                img = self.transform(img)
            frames.append(img)
            
        frames = torch.stack(frames)  # (seq_len, C, H, W)
        label = 1 if group['label'].iloc[0] == 'fight' else 0
        return frames, label


class ResNetLSTM(nn.Module):
    def __init__(self, hidden_size=256, num_layers=2, num_classes=2):
        super().__init__()
        resnet = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)
        self.feature_extractor = nn.Sequential(*list(resnet.children())[:-1])
        
        self.lstm = nn.LSTM(512, hidden_size, num_layers, batch_first=True)
        self.fc = nn.Sequential(
            nn.Linear(hidden_size, 128),
            nn.ReLU(),
            nn.Linear(128, num_classes)
        )
        
    def forward(self, x):
        batch_size, seq_len, c, h, w = x.size()
        x = x.view(batch_size * seq_len, c, h, w)
        
        features = self.feature_extractor(x)
        features = features.view(batch_size, seq_len, -1)
        
        lstm_out, _ = self.lstm(features)
        last_out = lstm_out[:, -1, :]
        return self.fc(last_out)


def evaluate(model, loader, criterion, device):
    """Run validation; return (avg_loss, accuracy)."""
    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    with torch.no_grad():
        for frames, labels in loader:
            frames, labels = frames.to(device), labels.to(device)
            outputs = model(frames)
            loss = criterion(outputs, labels)
            total_loss += loss.item()
            preds = outputs.argmax(dim=1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)
    return total_loss / len(loader), correct / total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',   type=str, required=True)
    parser.add_argument('--output', type=str, default='violence_classifier.pt')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch',  type=int, default=8)
    parser.add_argument('--device', type=str, default='cuda')
    args = parser.parse_args()
    
    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")
    if device.type == 'cuda':
        print(f"  GPU: {torch.cuda.get_device_name(0)}")

    # Checkpoint dir mirrors the output directory
    ckpt_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(ckpt_dir, exist_ok=True)
    
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    train_ds = ViolenceDataset(os.path.join(args.data, 'train.csv'), transform)
    val_ds   = ViolenceDataset(os.path.join(args.data, 'val.csv'),   transform)
    
    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True,  num_workers=2, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch, shuffle=False, num_workers=2, pin_memory=True)
    
    print(f"Train clips: {len(train_ds)}  |  Val clips: {len(val_ds)}")
    
    model = ResNetLSTM().to(device)
    
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
        for frames, labels in train_loader:
            frames, labels = frames.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(frames)
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
        ep_ckpt = os.path.join(ckpt_dir, f"violence_ep{epoch:02d}.pt")
        torch.save(model.state_dict(), ep_ckpt)

        # --- Best model (only updates when val_acc improves) ---
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), args.output)
            print(f"  ✓ New best val_acc={best_val_acc:.4f} — saved to {args.output}")

    print(f"\nTraining complete. Best val_acc: {best_val_acc:.4f}")
    print(f"Best model: {args.output}")

    # Save training history
    hist_path = os.path.join(ckpt_dir, "violence_history.csv")
    pd.DataFrame(history).to_csv(hist_path, index=False)
    print(f"History saved: {hist_path}")


if __name__ == "__main__":
    main()
