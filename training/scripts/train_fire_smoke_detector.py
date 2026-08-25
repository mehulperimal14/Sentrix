# training/scripts/train_fire_smoke_detector.py
#
# Fast-Track YOLOv8n Training for Fire & Smoke Detection
# Optimized for Apple Silicon MPS (skips slow intermediate CPU-NMS validation).

import os
import shutil
import sys
from pathlib import Path

# Enable CPU fallback for unsupported MPS ops
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
os.environ["POLARS_SKIP_CPU_CHECK"] = "1"

import torch

ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
BACKEND_MODELS_DIR = ROOT_DIR / "backend" / "models"
BACKEND_MODELS_DIR.mkdir(parents=True, exist_ok=True)

DATA_YAML = ROOT_DIR / "data" / "fire_smoke_data" / "data.yaml"
OUTPUT_MODEL = BACKEND_MODELS_DIR / "fire_smoke_detector.pt"

def get_device():
    if torch.backends.mps.is_available():
        return "mps"
    elif torch.cuda.is_available():
        return "0"
    return "cpu"

def train(epochs=3, batch=16, imgsz=416):
    print("=" * 60)
    print("SENTRIX: Fast-Track Fire & Smoke Detector (YOLOv8n)")
    print(f"Data: {DATA_YAML}")
    print(f"Target: {OUTPUT_MODEL}")
    device = get_device()
    print(f"Hardware Acceleration Device: {device} | Epochs: {epochs}")
    print("=" * 60)

    from ultralytics import YOLO
    
    base_model_path = BACKEND_MODELS_DIR / "yolov8n.pt"
    if not base_model_path.exists():
        model = YOLO("yolov8n.pt")
    else:
        model = YOLO(str(base_model_path))

    project_dir = ROOT_DIR / "training" / "runs" / "fire_smoke"
    
    # Train with val=False to skip the 2-hour intermediate CPU NMS bottleneck
    results = model.train(
        data=str(DATA_YAML),
        epochs=epochs,
        imgsz=imgsz,
        batch=batch,
        device=device,
        project=str(project_dir),
        name="train",
        exist_ok=True,
        workers=2,
        val=False,
        plots=False,
        save=True,
        patience=5
    )

    # Save output model weights
    best_weights = project_dir / "train" / "weights" / "best.pt"
    last_weights = project_dir / "train" / "weights" / "last.pt"
    
    saved_model = None
    if best_weights.exists():
        saved_model = best_weights
    elif last_weights.exists():
        saved_model = last_weights

    if saved_model:
        shutil.copy(str(saved_model), str(OUTPUT_MODEL))
        print(f"\n✅ Training complete! Saved model to {OUTPUT_MODEL}")
    else:
        print(f"\n⚠️ Checkpoint not found at {best_weights}")

if __name__ == "__main__":
    epochs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    train(epochs=epochs)
