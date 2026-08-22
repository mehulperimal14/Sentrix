#!/usr/bin/env python3
"""
train_violence_classifier.py
============================
PyTorch script to train ResNet18 + LSTM for violence detection.
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
            
        frames = torch.stack(frames) # (seq_len, C, H, W)
        label = 1 if group['label'].iloc[0] == 'fight' else 0
        return frames, label

class ResNetLSTM(nn.Module):
    def __init__(self, hidden_size=256, num_layers=2, num_classes=2):
        super().__init__()
        resnet = models.resnet18(pretrained=True)
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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=str, required=True)
    parser.add_argument('--output', type=str, default='violence_classifier.pt')
    parser.add_argument('--epochs', type=int, default=20)
    parser.add_argument('--batch', type=int, default=8)
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
    
    train_ds = ViolenceDataset(os.path.join(args.data, 'train.csv'), transform)
    val_ds = ViolenceDataset(os.path.join(args.data, 'val.csv'), transform)
    
    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch, shuffle=False)
    
    model = ResNetLSTM().to(device)
    
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
