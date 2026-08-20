# Technical Debt Register

This register compresses the active debt list into priority bands. The full prioritized action plan is in [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Priority 0 - Immediate
| Debt | Evidence | Risk | Remediation |
|---|---|---|---|
| Cookie-password auth | [web/routes.py](../web/routes.py) | System compromise | Signed sessions, CSRF, and rate limits |
| Unauthenticated upload | [web/routes.py](../web/routes.py) | Filesystem compromise | Auth + filename sanitization + type checks |
| Daemon shutdown loss | [app.py](../app.py), [ai/audio_engine.py](../ai/audio_engine.py), [ai/voice_sos_engine.py](../ai/voice_sos_engine.py) | Lost work and leaked handles | Stop events and join logic |
| Sync DB in async path | [web/routes.py](../web/routes.py), [db/database.py](../db/database.py) | Event-loop blocking | Repository layer and better DB strategy |

## Priority 1 - High
| Debt | Evidence | Risk | Remediation |
|---|---|---|---|
| Monolithic orchestrator | [core/system_engine.py](../core/system_engine.py) | Hard to scale or test cleanly | Split into services and queues |
| Inline external IO | [core/alert_service.py](../core/alert_service.py), [core/dispatch_service.py](../core/dispatch_service.py) | Latency spikes | Worker queue for SMS/call/evidence/dispatch |
| No structured logging | App + all modules | No observability | Logging module + JSON formatter |
| No metrics/tracing | App + all modules | Operational blindness | Histograms, counters, and trace IDs |
| State accessed globally | [core/state.py](../core/state.py), [core/health_monitor.py](../core/health_monitor.py) | Race risk and tight coupling | Read-model snapshot + bus |

## Priority 2 - Medium
| Debt | Evidence | Risk | Remediation |
|---|---|---|---|
| SQLite single writer | [db/database.py](../db/database.py) | Throughput ceiling | Better database strategy and indexes |
| Unbounded evidence growth | `static/alerts`, `static/alerts/evidence` | Disk exhaustion | Retention job + object storage |
| ReID gallery growth | [ai/reid_engine.py](../ai/reid_engine.py) | Memory growth | TTL or capped gallery |
| Behaviour history growth | [ai/behaviour_engine.py](../ai/behaviour_engine.py) | Memory growth | Prune inactive tracks |
| Cloud frame skipping | [ai/cloud_engines.py](../ai/cloud_engines.py) | Stale confidence | Queue and freshness stamping |
| Legacy dead engine | [ai/system_engine.py](../ai/system_engine.py) | Confusion and drift | Delete or quarantine |
| Dead wrappers | [core/sms_service.py](../core/sms_service.py), [core/timeline_register.py](../core/timeline_register.py), [ai/motion_engine.py](../ai/motion_engine.py) | Maintenance overhead | Remove if unused |

## Priority 3 - Low
| Debt | Evidence | Risk | Remediation |
|---|---|---|---|
| UI/WebSocket duplication | [web/routes.py](../web/routes.py), [web/streaming.py](../web/streaming.py), [static/js/app.js](../static/js/app.js) | Repeated logic | Shared client state module |
| Templates depend on backend payload shape | `templates/*.html`, [static/js/app.js](../static/js/app.js) | Brittle UI coupling | Contract tests + DTO schema |
| Mixed naming conventions | Legacy docs and modules | Confusion | Standardize names |

## Remediation Order
1. Auth and upload hardening.
2. Graceful shutdown and thread cleanup.
3. Repository layer and DB improvements.
4. Logging and metrics.
5. Service extraction.
6. Queue-backed side effects.
7. Retention and evidence lifecycle.
8. Delete or quarantine dead legacy modules.
