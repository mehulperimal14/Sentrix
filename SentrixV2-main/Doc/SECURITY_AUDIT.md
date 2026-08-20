# Security Audit

This document summarizes the verified security posture of the active code. It intentionally tracks the findings already consolidated in [SENTRIX_MASTER_TECHNICAL_REPORT.md](../SENTRIX_MASTER_TECHNICAL_REPORT.md).

## Threat Model
SENTRIX is a security product, but the current implementation still trusts local cookies, writable static directories, and environment-driven secrets without production-grade controls.

## Highest-Risk Findings
| Risk | Location | Severity | Why it matters |
|---|---|---:|---|
| Password-as-cookie auth | [web/routes.py](../web/routes.py) | Critical | Anyone who learns the password can replay it as the cookie value |
| Default password fallback | [web/routes.py](../web/routes.py) | Critical | Missing `.env` can silently fall back to a weak secret |
| Unauthenticated upload | [web/routes.py](../web/routes.py) | Critical | An attacker can write into the face-enrollment directory |
| Path traversal in upload | [web/routes.py](../web/routes.py) | High | Client-controlled filenames can escape the intended directory |
| Unprotected telemetry endpoints | [web/routes.py](../web/routes.py), [web/streaming.py](../web/streaming.py) | High | Live state and video are exposed without explicit auth |
| Public evidence artifacts | `static/alerts`, `static/alerts/evidence` | High | Sensitive incident outputs are stored in web-served paths |
| Ephemeral evidence key | [core/encrypted_evidence.py](../core/encrypted_evidence.py) | High | Restarts can invalidate older evidence continuity |

## Practical Attack Paths
1. Guess or learn the login password, then replay it as the cookie value.
2. Upload a maliciously named file to the enrollment endpoint.
3. Pull telemetry or video without authenticating.
4. Trigger repeated alerts or calls if the trigger path is reachable.
5. Use the public static tree to infer or access incident artifacts.

## What Is Not Obviously Present
- No direct command execution path was identified in the active routes.
- No obvious SQL injection sink was identified in the current ORM usage.
- No pickle-style unsafe deserialization path was identified.

## Minimum Hardening Plan
- Replace cookie-password auth with signed sessions or tokens.
- Add CSRF protection.
- Require auth on mutating and telemetry endpoints.
- Sanitize filenames and store uploads outside the web root.
- Move evidence to private storage with signed access.
- Add rate limiting and abuse detection.
- Use a persistent evidence key strategy.
- Add access logging for evidence and dispatch actions.

## Verdict
The security model is acceptable for a demo or capstone appliance, but it is not ready for production or multi-user deployment.
