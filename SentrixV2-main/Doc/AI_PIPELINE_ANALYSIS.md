# AI Pipeline Analysis

This document summarizes the active inference path. It is intentionally aligned with the verified runtime in [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Pipeline Overview
The live AI pipeline is orchestrated by [core/system_engine.py](../core/system_engine.py). It combines vision, motion, behaviour, face verification, tracking, ReID, cloud threat inference, fusion, audio, and voice override.

## Active Inference Order
1. Capture and tile frames.
2. Run YOLO person detection in [ai/vision_engine.py](../ai/vision_engine.py).
3. Compute frame-diff motion score in the same vision engine.
4. Run behaviour heuristics in [ai/behaviour_engine.py](../ai/behaviour_engine.py).
5. Run face authorization in [ai/face_engine.py](../ai/face_engine.py).
6. Run tracking and ReID in [ai/tracking_engine.py](../ai/tracking_engine.py) and [ai/reid_engine.py](../ai/reid_engine.py).
7. Run cloud threat inference in [ai/cloud_engines.py](../ai/cloud_engines.py).
8. Fuse scores in [ai/fusion_engine.py](../ai/fusion_engine.py).
9. Apply voice override in [ai/voice_sos_engine.py](../ai/voice_sos_engine.py).
10. Escalate, persist, and render the HUD.

## What Is Working Well
- YOLO detection and motion scoring are integrated and cached.
- Face authorization is hot-reloadable after uploads.
- Cloud inference is optional and can fall back cleanly.
- The fusion layer has hard safety overrides for fire and weapon confidence.

## What Is Still Prototype-Grade
- Behaviour classification is heuristic, not model-trained.
- ReID is fallback-based if torchreid is unavailable.
- Audio and voice are background-thread helpers rather than managed services.
- Cloud inference is synchronous and serialized.
- TCI weights are policy constants, not a calibrated production model.

## Duplicated or Stale Paths
- [ai/system_engine.py](../ai/system_engine.py) is a stale alternate orchestrator and should not be considered active.
- [ai/motion_engine.py](../ai/motion_engine.py) is a legacy helper that is not wired into the live flow.

## Execution Risks
- The pipeline reuses the same frame for multiple expensive steps.
- Cloud inference and evidence writes are inline with the frame loop.
- Any long-running step can stall the whole system.

## Recommended Direction
The next architecture should separate capture, inference, and side effects using queues or workers. That would preserve the current model mix while removing the main latency and shutdown risks.
