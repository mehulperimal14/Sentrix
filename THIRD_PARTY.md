# Third-party components and data

SENTRIX's own code is MIT-licensed (see `LICENSE`). Several components it uses,
downloads or talks to carry their own terms, which apply to anyone who runs or
redistributes them.

## Models and data

| Component | Where it comes from | Terms |
|---|---|---|
| Ultralytics YOLO (`ultralytics`, `yolov8n.pt`, the YOLO11n/YOLOv8n heads in `models/`, their ONNX exports) | PyPI / Ultralytics | AGPL-3.0 (commercial use needs an Ultralytics licence) |
| InsightFace `buffalo_l` (and the optional `buffalo_s`) face models | downloaded on first use into `~/.insightface` | non-commercial research use only |
| DINOv2 ViT-B/14 base weights (`timm`, `vit_base_patch14_dinov2.lvd142m`), the base of `models/v4_custom/scene_classifier_custom_v4.pt` | downloaded on first use from Hugging Face | Apache-2.0 |
| EfficientNet-B0 and MobileNetV3-Large ImageNet weights (`timm`), the bases of the experimental `models/v5_slim/` heads | downloaded from Hugging Face by `timm` | see each model card; the ImageNet data has its own non-commercial terms |
| RWF-2000 (behind `models/v3_real/violence_classifier_real_v3.pt`) | public research dataset | research use only |
| GroundingDINO (used offline to propose boxes for the custom dataset, `training/custom/annotate_boxes.py`) | Hugging Face `transformers` | Apache-2.0 |
| PANNs CNN10 AudioSet tagger (`models/audio/panns_cnn10_audioset.pth`), the general sound model (D15); Kong, Cao, Iqbal, Wang, Wang, Plumbley, *PANNs: Large-Scale Pretrained Audio Neural Networks for Audio Pattern Recognition* (2020) | Zenodo record 3987831 (`tools/fetch_models.py`) | CC BY 4.0 (attribution required) |
| AudioSet ontology labels (`models/audio/audioset_labels.csv`) | Google AudioSet | CC BY 4.0 |

The custom dataset in `training/datasets/sentrix_custom/` (frames and source
videos) was recorded by the SENTRIX team for this project and is included so
the evaluation reproduces. It shows team members; do not use it for anything
other than evaluating or developing SENTRIX.

## Software

| Component | Used for | Terms |
|---|---|---|
| PyTorch, torchvision, torchaudio, timm, OpenCV, onnxruntime, FastAPI, uvicorn, SQLAlchemy, NumPy, scikit-learn and the other packages in `requirements.txt` / `requirements-pi.txt` | the device and the Portal | their own open-source licences |
| gpiozero, pyserial, argon2-cffi, pyotp, qrcode, httpx | Pi GPIO, the USB modem, passwords and PINs, TOTP, device labels, HTTPS APIs and the tunnel | BSD or MIT |
| pyttsx3; espeak-ng and alsa-utils (apt, on the Pi; not bundled) | talk-down speaker text-to-speech (D16) | MPL-2.0; GPL-3.0 and GPL-2.0 (installed as system packages, not redistributed) |
| Caddy, PostgreSQL (the `deploy/portal/` images) | Portal hosting | Apache-2.0; PostgreSQL Licence |

## Services

The channels and the hosting plan use third-party services, each under its own
terms of service and pricing: Telegram Bot API, Brevo or Resend (e-mail by HTTPS
API), Twilio (legacy SMS/call path), a mobile operator's SIM for the 4G modem,
Oracle Cloud Always Free (planned host) and Render + Supabase (fallback). None
is contacted in test mode unless both send switches are on (`README.md`,
*Configuration*), and the test suite blocks every non-loopback connection.
