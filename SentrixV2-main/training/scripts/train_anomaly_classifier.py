#!/usr/bin/env python3
"""
train_anomaly_classifier.py
===========================
PyTorch script to train MobileNetV2 for anomaly detection (Model F).
Binary classification: Normal vs Anomalous.
Designed for RTX 4060 GPU. Incremental training supported.
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
    model = models.mobilenet_v2(pretrained=True)
    model.classifier[1] = nn.Linear(model.last_channel, 2)
    return model

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, required=True)
    parser.add_argument('--output', type=str, default='anomaly_classifier.pt')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch', type=int, default=32)
    parser.add_argument('--device', type=str, default='cuda')
    args = parser.parse_args()
    
    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")
    
    transform = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])
    
    train_ds = AnomalyDataset(os.path.join(args.data, 'train.csv'), transform)
    val_ds = AnomalyDataset(os.path.join(args.data, 'val.csv'), transform)
    
    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch, shuffle=False)
    
    model = get_model().to(device)
    
    if os.path.exists(args.output):
        print(f"Resuming from checkpoint {args.output}")
        model.load_state_dict(torch.load(args.output))
        
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=1e-4)
    
    for epoch in range(args.epochs):
        model.train()
        total_loss = 0
        for frames, labels in train_loader:
            frames, labels = frames.to(device), labels.to(device)
            
            optimizer.zero_grad()
            outputs = model(frames)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            
            total_loss += loss.item()
            
        print(f"Epoch {epoch+1}/{args.epochs} - Loss: {total_loss/len(train_loader):.4f}")
        torch.save(model.state_dict(), args.output)

if __name__ == "__main__":
    main()
