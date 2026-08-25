# training/scripts/train_audio_classifier.py
#
# Trains a 1D/2D Log-Mel Spectrogram PyTorch CNN on security acoustic events
# Output saved to backend/models/audio_classifier.pt

import os
import sys
import math
import numpy as np
from pathlib import Path
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = ROOT_DIR / "data" / "audio_data"
BACKEND_MODELS_DIR = ROOT_DIR / "backend" / "models"
BACKEND_MODELS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_MODEL = BACKEND_MODELS_DIR / "audio_classifier.pt"

CLASSES = ["normal_ambient", "gunshot_like", "scream_like", "explosion_like", "siren_like"]
NUM_CLASSES = len(CLASSES)

class AudioThreatCNN(nn.Module):
    """Compact 2D CNN operating on Mel-spectrogram feature representations."""
    def __init__(self, num_classes=5):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(1, 16, kernel_size=3, padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(),
            nn.MaxPool2d(2, 2),

            nn.Conv2d(16, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2, 2),

            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((4, 4))
        )
        self.fc = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(64 * 4 * 4, 64),
            nn.ReLU(),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        # x shape: (B, 1, n_mels, time_steps)
        feat = self.conv(x)
        feat = feat.view(feat.size(0), -1)
        return self.fc(feat)

class SyntheticAudioDataset(Dataset):
    """
    Generates representative acoustic feature matrices (16kHz Mel spectrograms)
    calibrated with physical characteristics (harmonic peaks, high-energy transients,
    frequency modulations, and ambient Gaussian noise profiles).
    """
    def __init__(self, num_samples=1200, n_mels=64, time_steps=64):
        self.samples = []
        np.random.seed(42)
        
        for _ in range(num_samples):
            cls = np.random.choice(NUM_CLASSES)
            # Base background noise
            spec = np.random.normal(0.1, 0.05, (n_mels, time_steps))
            
            if cls == 1: # Gunshot (sharp impulse + high-freq burst)
                t_imp = np.random.randint(10, 30)
                spec[:, t_imp:t_imp+4] += np.random.uniform(1.2, 2.5, (n_mels, 4))
                spec[35:, t_imp:t_imp+8] += np.random.uniform(0.8, 1.8, (n_mels-35, 8))
            elif cls == 2: # Scream (sustained 500-2500Hz energy)
                t_start = np.random.randint(5, 20)
                t_len = np.random.randint(20, 35)
                spec[20:45, t_start:t_start+t_len] += np.random.uniform(0.9, 1.9, (25, t_len))
            elif cls == 3: # Explosion (low-frequency resonance + broadband shock)
                t_imp = np.random.randint(5, 15)
                spec[:25, t_imp:t_imp+40] += np.random.uniform(1.0, 2.2, (25, 40))
            elif cls == 4: # Siren (alternating sinusoidal frequency sweeps)
                for t in range(time_steps):
                    freq_bin = int(30 + 15 * math.sin(t * 0.3))
                    freq_bin = max(0, min(n_mels - 1, freq_bin))
                    spec[freq_bin-2:freq_bin+3, t] += 1.4
            
            spec = np.clip(spec, 0.0, 3.0).astype(np.float32)
            spec_tensor = torch.tensor(spec).unsqueeze(0) # (1, 64, 64)
            self.samples.append((spec_tensor, cls))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]

def train(epochs=10, batch_size=32):
    print("=" * 60)
    print("SENTRIX: Training Audio Threat Classifier (Acoustic CNN)")
    print(f"Target: {OUTPUT_MODEL}")
    print(f"Classes: {CLASSES}")
    print("=" * 60)

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    train_ds = SyntheticAudioDataset(num_samples=1600)
    val_ds = SyntheticAudioDataset(num_samples=400)

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False)

    model = AudioThreatCNN(num_classes=NUM_CLASSES).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)

    best_acc = 0.0
    for epoch in range(1, epochs + 1):
        model.train()
        running_loss, correct, total = 0.0, 0, 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            out = model(x)
            loss = criterion(out, y)
            loss.backward()
            optimizer.step()
            running_loss += loss.item() * x.size(0)
            preds = out.argmax(dim=1)
            correct += (preds == y).sum().item()
            total += y.size(0)

        # Validation
        model.eval()
        v_correct, v_total = 0, 0
        with torch.no_grad():
            for x, y in val_loader:
                x, y = x.to(device), y.to(device)
                preds = model(x).argmax(dim=1)
                v_correct += (preds == y).sum().item()
                v_total += y.size(0)

        val_acc = v_correct / max(v_total, 1)
        print(f"Epoch {epoch}/{epochs} | Train Acc: {correct/total*100:.2f}% | Val Acc: {val_acc*100:.2f}%")

        if val_acc > best_acc or epoch == 1:
            best_acc = val_acc
            torch.save({
                "model_state_dict": model.state_dict(),
                "classes": CLASSES,
                "val_acc": val_acc
            }, OUTPUT_MODEL)

    print(f"\n✅ Audio threat classifier training complete. Final model: {OUTPUT_MODEL}")

if __name__ == "__main__":
    epochs = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    train(epochs=epochs)
