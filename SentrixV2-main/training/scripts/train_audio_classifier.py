#!/usr/bin/env python3
"""
train_audio_classifier.py
==========================
PyTorch CNN to classify audio into 5 threat-relevant categories:
    explosion, fire, scream, siren, speech_normal

Input:  .wav files from data/audio_data/audioset_clips/<label>/
Output: models/audio_classifier.pt (best model) + per-epoch checkpoints

Architecture: Log-Mel Spectrogram → 3× Conv+BN+ReLU+MaxPool → FC → 5 classes

Per-epoch checkpoints: models/audio_ep{N:02d}.pt  (no overwrites)
Best model:           models/audio_classifier.pt  (only updates on val_acc improvement)
"""

import os
import re
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

try:
    import librosa
    _LIBROSA_AVAILABLE = True
except ImportError:
    _LIBROSA_AVAILABLE = False


CLASSES = ['explosion', 'fire', 'scream', 'siren', 'speech_normal']
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASSES)}
N_MELS      = 128
HOP_LENGTH  = 512
N_FFT       = 2048
SAMPLE_RATE = 22050
FIXED_LEN   = 128  # fixed time dimension (pad/trim)


class AudioDataset(Dataset):
    """Loads .wav files from a directory tree: audioset_clips/<label>/*.wav"""

    def __init__(self, clips_dir: str, split: str = 'train', val_ratio: float = 0.15):
        self.samples = []  # list of (wav_path, label_idx)

        clips_path = Path(clips_dir)
        all_samples = []
        for label in CLASSES:
            label_dir = clips_path / label
            if not label_dir.exists():
                continue
            for wav_file in sorted(label_dir.glob("*.wav")):
                all_samples.append((str(wav_file), CLASS_TO_IDX[label]))

        # Deterministic train/val split
        np.random.seed(42)
        idx = np.random.permutation(len(all_samples))
        split_point = int(len(all_samples) * (1 - val_ratio))
        train_idx = idx[:split_point]
        val_idx   = idx[split_point:]
        chosen = train_idx if split == 'train' else val_idx
        self.samples = [all_samples[i] for i in chosen]

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        wav_path, label_idx = self.samples[idx]
        spec = self._load_spectrogram(wav_path)
        return torch.FloatTensor(spec).unsqueeze(0), label_idx  # (1, N_MELS, FIXED_LEN)

    @staticmethod
    def _load_spectrogram(wav_path: str) -> np.ndarray:
        y, sr = librosa.load(wav_path, sr=SAMPLE_RATE, mono=True)
        mel = librosa.feature.melspectrogram(
            y=y, sr=sr, n_fft=N_FFT, hop_length=HOP_LENGTH, n_mels=N_MELS
        )
        log_mel = librosa.power_to_db(mel, ref=np.max)
        # Normalize to [-1, 1]
        log_mel = (log_mel + 80.0) / 80.0 * 2.0 - 1.0

        # Pad or trim to FIXED_LEN columns
        T = log_mel.shape[1]
        if T < FIXED_LEN:
            pad = np.zeros((N_MELS, FIXED_LEN - T), dtype=np.float32)
            log_mel = np.concatenate([log_mel, pad], axis=1)
        else:
            log_mel = log_mel[:, :FIXED_LEN]
        return log_mel.astype(np.float32)


class AudioCNN(nn.Module):
    """Lightweight CNN for Log-Mel spectrograms."""

    def __init__(self, num_classes: int = 5):
        super().__init__()
        self.features = nn.Sequential(
            # Block 1
            nn.Conv2d(1, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),   # → (32, 64, 64)

            # Block 2
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),   # → (64, 32, 32)

            # Block 3
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2, 2),   # → (128, 16, 16)

            nn.AdaptiveAvgPool2d((4, 4)),  # → (128, 4, 4)
        )
        self.classifier = nn.Sequential(
            nn.Dropout(0.4),
            nn.Linear(128 * 4 * 4, 256),
            nn.ReLU(inplace=True),
            nn.Dropout(0.3),
            nn.Linear(256, num_classes),
        )

    def forward(self, x):
        x = self.features(x)
        x = x.view(x.size(0), -1)
        return self.classifier(x)


def evaluate(model, loader, criterion, device):
    model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    with torch.no_grad():
        for specs, labels in loader:
            specs  = specs.to(device)
            labels = torch.tensor(labels, dtype=torch.long, device=device) if not isinstance(labels, torch.Tensor) else labels.to(device)
            outputs = model(specs)
            loss = criterion(outputs, labels)
            total_loss += loss.item()
            preds = outputs.argmax(dim=1)
            correct += (preds == labels).sum().item()
            total += labels.size(0)
    return total_loss / max(len(loader), 1), correct / max(total, 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',   type=str, required=True,
                        help='Path to audioset_clips/ directory')
    parser.add_argument('--output', type=str, default='audio_classifier.pt')
    parser.add_argument('--epochs', type=int, default=15)
    parser.add_argument('--batch',  type=int, default=32)
    parser.add_argument('--device', type=str, default='cuda')
    args = parser.parse_args()

    if not _LIBROSA_AVAILABLE:
        raise ImportError("librosa not installed. Run: pip install librosa")

    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    print(f"Using device: {device}")
    if device.type == 'cuda':
        print(f"  GPU: {torch.cuda.get_device_name(0)}")

    ckpt_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(ckpt_dir, exist_ok=True)

    train_ds = AudioDataset(args.data, split='train')
    val_ds   = AudioDataset(args.data, split='val')
    print(f"Train samples: {len(train_ds)}  |  Val samples: {len(val_ds)}")

    if len(train_ds) == 0:
        print("ERROR: No training audio files found. Run download_audioset.py first.")
        return

    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True,  num_workers=2, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch, shuffle=False, num_workers=2, pin_memory=True)

    model = AudioCNN(num_classes=len(CLASSES)).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=1e-3)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    best_val_acc = 0.0
    history = []

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for specs, labels in train_loader:
            specs  = specs.to(device)
            labels = labels.to(device) if isinstance(labels, torch.Tensor) else torch.tensor(labels, dtype=torch.long, device=device)
            optimizer.zero_grad()
            outputs = model(specs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        train_loss = total_loss / len(train_loader)
        val_loss, val_acc = evaluate(model, val_loader, criterion, device)
        scheduler.step()

        print(
            f"Epoch [{epoch:02d}/{args.epochs}]  "
            f"train_loss={train_loss:.4f}  "
            f"val_loss={val_loss:.4f}  "
            f"val_acc={val_acc:.4f}"
        )

        history.append({'epoch': epoch, 'train_loss': train_loss, 'val_loss': val_loss, 'val_acc': val_acc})

        # Per-epoch checkpoint (no overwrite)
        ep_ckpt = os.path.join(ckpt_dir, f"audio_ep{epoch:02d}.pt")
        torch.save(model.state_dict(), ep_ckpt)

        # Best model
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            torch.save(model.state_dict(), args.output)
            print(f"  ✓ New best val_acc={best_val_acc:.4f} — saved to {args.output}")

    print(f"\nTraining complete. Best val_acc: {best_val_acc:.4f}")
    print(f"Best model: {args.output}")
    print(f"Classes: {CLASSES}")

    hist_path = os.path.join(ckpt_dir, "audio_history.csv")
    pd.DataFrame(history).to_csv(hist_path, index=False)
    print(f"History saved: {hist_path}")


if __name__ == "__main__":
    main()
