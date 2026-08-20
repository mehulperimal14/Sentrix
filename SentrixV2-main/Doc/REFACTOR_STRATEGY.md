# Refactor Strategy

This is the recommended modernization path for SENTRIX. It is intentionally based on the verified issues in [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Target Architecture
Move from a monolithic synchronous orchestrator to a service-oriented, queue-backed, async-first system with explicit boundaries.

### Proposed service boundaries
- Capture service: camera acquisition and frame normalization.
- Inference service: vision, motion, face, tracking, cloud, audio cache, voice cache.
- Fusion service: confidence aggregation and threat policy.
- Escalation service: snapshot, evidence, SMS, call, siren, dispatch.
- Repository layer: events, dispatch packages, authorized faces, retention jobs.
- API/UI layer: routes, WebSocket, MJPEG streaming, templates.
- Observability layer: logs, metrics, traces, audit events.

## Dependency Injection Model
### Current
- Module-level singletons.
- Eager construction inside `SystemEngine.__init__()`.

### Target
- Explicit constructor injection.
- A bootstrap container that owns all dependencies.
- Easy test replacement of any subsystem.

## Event Bus Architecture
### Why
Inline side effects currently block the inference loop. SMS, calls, evidence writes, and dispatch creation should be consumers, not hot-path code.

### Suggested events
- `threat.detected`
- `evidence.capture.requested`
- `notification.sms.requested`
- `notification.call.requested`
- `dispatch.package.requested`
- `dispatch.sent`
- `state.updated`
- `health.changed`

### Suggested transport
- Redis Streams or RabbitMQ for queueing.
- PostgreSQL for durable audit records.
- Prometheus for runtime counters and histograms.

## Migration Sequence
1. Add logging, metrics, and shutdown hooks.
2. Replace cookie-password auth.
3. Move DB access behind repository interfaces.
4. Isolate evidence, alerting, and dispatch behind workers.
5. Split capture, inference, and fusion.
6. Retire the daemon-thread model once queues are stable.

## Compatibility Layer
Preserve the current dashboard contract while the backend changes.
- Keep `state` as a read-model façade.
- Keep `/api/metrics` and `/ws/threat` stable during migration.
- Preserve dispatch payload shape until the UI is updated.

## Cleanup Targets
- [ai/system_engine.py](../ai/system_engine.py)
- [core/sms_service.py](../core/sms_service.py)
- [core/timeline_register.py](../core/timeline_register.py)
- [ai/motion_engine.py](../ai/motion_engine.py)

## Outcome
This refactor path keeps the current product concept intact while removing the main technical blockers to production readiness.
