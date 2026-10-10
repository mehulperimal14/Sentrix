# Third-party components and data

SENTRIX's own code is MIT-licensed (see `LICENSE`). Several components it uses
or downloads carry their own terms, which apply to anyone who runs or
redistributes them:

| Component | Where it comes from | Terms |
|---|---|---|
| Ultralytics YOLO (`ultralytics`, `yolov8n.pt`, the YOLO11n/YOLOv8n heads in `models/`) | PyPI / Ultralytics | AGPL-3.0 (commercial use needs an Ultralytics licence) |
| InsightFace `buffalo_l` face models | downloaded on first use into `~/.insightface` | non-commercial research use only |
| DINOv2 ViT-B/14 base weights (`timm`, `vit_base_patch14_dinov2.lvd142m`) | downloaded on first use from Hugging Face | Apache-2.0 |
| RWF-2000 (behind `models/v3_real/violence_classifier_real_v3.pt`) | public research dataset | research use only |
| Vosk small English model (`models/vosk-model/`) | Alpha Cephei | Apache-2.0 |
| PyTorch, torchvision, timm, OpenCV, FastAPI and the other packages in `requirements.txt` | PyPI | their respective open-source licences |

The custom dataset in `training/datasets/sentrix_custom/` (frames and source
videos) was recorded by the SENTRIX team for this project and is included so
the evaluation reproduces. It shows team members; do not use it for anything
other than evaluating or developing SENTRIX.
