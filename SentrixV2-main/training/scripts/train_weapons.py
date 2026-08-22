import os
import shutil
from ultralytics import YOLO

def main():
    print("=== Training Model A: Weapon Detector (YOLOv8n) ===")
    
    WEAPON_YAML = 'G:/Capstone/data/weapon_data/data.yaml'
    BASE_MODEL  = 'G:/Capstone/SentrixV2-main/models/yolov8n.pt'
    RUN_DIR     = 'G:/Capstone/SentrixV2-main/runs/weapon'
    OUT_BEST    = 'G:/Capstone/SentrixV2-main/models/weapon_detector.pt'

    if not os.path.exists(WEAPON_YAML):
        print(f"Error: {WEAPON_YAML} not found!")
        return
        
    if not os.path.exists(BASE_MODEL):
        print(f"Error: Base model {BASE_MODEL} not found!")
        return

    m = YOLO(BASE_MODEL)
    
    print(f"Starting training for 50 epochs...")
    m.train(
        data=WEAPON_YAML, 
        epochs=50, 
        batch=8, 
        workers=2,
        imgsz=640, 
        device=0,
        project=RUN_DIR, 
        name='train', 
        exist_ok=True,
        patience=10, 
        save=True, 
        save_period=5, 
        verbose=True
    )
    
    best_weights = f'{RUN_DIR}/train/weights/best.pt'
    if os.path.exists(best_weights):
        shutil.copy(best_weights, OUT_BEST)
        print(f"Training complete! Saved best model to: {OUT_BEST}")
    else:
        print("Training failed to produce best.pt")

if __name__ == "__main__":
    main()
