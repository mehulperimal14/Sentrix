# SENTRIX Phase 3 Training — Antigravity Agent Prompt

> **Context for AI Agent:** You are continuing a Capstone project (SENTRIX). The previous agent has already performed comprehensive data restructuring and preprocessing on a Mac. This directory is now on an RTX 4060 machine. Your goal is to execute the ML training phase under a strict 3-hour constraint and document the process in a Jupyter Notebook. 

## 1. Directory Structure Overview
You are operating in the root of the Capstone project. The relevant directories are:
* `data/` — Contains all preprocessed datasets ready for training.
  * `weapon_data/` (7K images for YOLOv8)
  * `fire_smoke_data/` (10K images for YOLOv8)
  * `violence_data/` (~19K frames for PyTorch ResNet18+LSTM)
  * `anomaly_data/` (~3.2K frames for PyTorch MobileNetV2)
  * `audio_data/` (AudioSet manifest and download script)
* `SentrixV2-main/` — The main codebase.
  * `SentrixV2-main/scripts/` contains the training scripts.
  * `SentrixV2-main/models/` is where all final `.pt` weights must be saved.

## 2. Your Immediate Tasks

**Task 1: Set up the Training Environment & Notebook**
1. Ensure the Python environment meets the requirements (PyTorch with CUDA 12.1/11.8 support, `ultralytics`, `xgboost`, `yt-dlp`, etc.). 
2. Create a Jupyter Notebook named `Sentrix_Phase3_Training.ipynb` in the root directory. You will use this notebook to execute the training commands, stream the logs, and plot the training curves (loss/accuracy/mAP).

**Task 2: Train Model A (Weapon Detection)**
* Dataset: `data/weapon_data/data.yaml`
* Architecture: YOLOv8n (`yolov8n.pt`)
* Hyperparameters: 50 epochs, batch 16, imgsz 640
* Output: `SentrixV2-main/models/weapon_detector.pt`

**Task 3: Train Model B (Fire & Smoke Detection)**
* Dataset: `data/fire_smoke_data/data.yaml`
* Architecture: YOLOv8n (`yolov8n.pt`)
* Hyperparameters: 30 epochs, batch 16, imgsz 640
* Output: `SentrixV2-main/models/fire_smoke_detector.pt`

**Task 4: Train Model C (Violence Classifier)**
* Dataset: `data/violence_data/`
* Script: `SentrixV2-main/scripts/train_violence_classifier.py`
* Architecture: ResNet18 + LSTM (incremental)
* Hyperparameters: 20 epochs, batch 8
* Output: `SentrixV2-main/models/violence_classifier.pt`

**Task 5: Train Model F (Anomaly Detector)**
* Dataset: `data/anomaly_data/`
* Script: `SentrixV2-main/scripts/train_anomaly_classifier.py`
* Architecture: MobileNetV2 (binary classifier)
* Hyperparameters: 20 epochs, batch 32
* Output: `SentrixV2-main/models/anomaly_classifier.pt`

**Task 6 (Conditional): Train Model E (Audio CNN)**
* If time allows (< 3 hours total), run the bash script `data/audio_data/download_audio.sh` to download the AudioSet clips.
* Once downloaded, create `train_audio_classifier.py` (Log-Mel Spectrogram -> CNN) and train it on the downloaded `.wav` files. 
* Output: `SentrixV2-main/models/audio_classifier.pt`

**Task 7: Refit XGBoost Fusion Engine**
* Once Models A, B, C, F are trained and replacing the cloud/heuristic engines in SENTRIX, run the end-to-end evaluation harness on the test datasets to gather actual model confidences. 
* Use these real confidences to refit `tci_xgboost.json` (requires 100+ samples).

## 3. Strict Rules for this Session
1. **Time Constraint:** The RTX 4060 has a 3-hour budget. Monitor time per epoch and use early stopping. 
2. **Reproducibility:** Log all final evaluation metrics clearly in the Jupyter Notebook.
3. **No Fabricated Data:** The previous agent ensured zero data leakage. Maintain this integrity. 
4. **Integration:** Ensure the SENTRIX codebase correctly loads the new `.pt` models instead of using the Roboflow API or heuristic functions. Modify `cloud_engines.py`, `behaviour_engine.py`, etc., as needed.

*Begin by analyzing the directory structure and creating the Jupyter Notebook.*
