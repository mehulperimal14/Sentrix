# Concurrency Audit

This document records the active thread and async boundaries in the live app. It is the shorter operating companion to [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Inventory of Threads and Async Boundaries
### Native threads
- Startup background processor in [app.py](../app.py).
- Audio capture loop in [ai/audio_engine.py](../ai/audio_engine.py).
- Voice SOS listener in [ai/voice_sos_engine.py](../ai/voice_sos_engine.py).
- Siren activation thread in [hardware/siren.py](../hardware/siren.py).

### Async tasks/coroutines
- WebSocket in [web/routes.py](../web/routes.py).
- MJPEG generator in [web/streaming.py](../web/streaming.py).
- Dispatch send wrapper using `asyncio.to_thread()` in [web/routes.py](../web/routes.py).

## Main Concurrency Risks
1. Shared state is read and written across threads and async handlers.
2. The entire frame pipeline is serialized in a single background loop.
3. Camera reads can block the whole processing thread.
4. Audio and voice services mutate background state from their own threads.
5. The legacy engine file contains inconsistent symbols and should not be activated.

## Practical Consequences
- Dashboard jitter under load.
- Video and telemetry lag when processing slows down.
- Shutdown leakage for camera and microphone resources.
- Stale or partial state during heavy activity.

## Recommended Fix Direction
- Use bounded queues between capture, inference, and side effects.
- Add explicit stop events and joins for background workers.
- Keep async handlers thin and read-only.
- Retire or quarantine the stale legacy engine.

## Verdict
The concurrency model is acceptable for a demo appliance, but it needs worker isolation before the app can be trusted under sustained load.
