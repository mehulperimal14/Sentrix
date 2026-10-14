"""The tunnel executor: Portal HTTP requests replayed into the LC in-process.

Parity by tunnel (decision D10): the Portal forwards ``/d/{device_id}/...``
over the link as ``tunnel.request`` messages and this executor replays each
one into the device's own Local Console app with
``httpx.ASGITransport(app=lc_app, client=tunnel_auth.TUNNEL_CLIENT)``, so the
same route code serves the LAN and the Portal. The path is replayed unchanged
(no ``root_path``); the URL prefix travels in the ``x-sentrix-base`` bridge
header, from which the console builds its links (``tunnel_auth.base_for``).

Order of checks for every request (docs/7 §5.7, §5.8):

1. parse the message (actor, method, path, base): ``invalid_args`` otherwise;
2. the device's own path table (:mod:`sentrix.link.policy`): role, consent,
   step-up and the media rate limit;
3. size (request <= 11 MiB) and concurrency (<= 8 in flight, else 503
   ``device_busy``);
4. the in-process call with the forwarded header allowlist plus the bridge
   headers of :func:`tunnel_auth.build_headers` (the per-process secret
   proves to the console that the identity was checked here), under a timeout
   (20 s; 60 s for uploads and captures);
5. the response: hop-by-hop headers and every ``set-cookie`` dropped, body
   <= 16 MiB.

Every request, served or refused, leaves a ``remote_audit`` row.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sentrix.actor import Actor
from sentrix.link import tunnel_auth
from sentrix.link.policy import Decision, TunnelPolicy, normalise_path
from sentrix.link.protocol import ERROR_CODES
from sentrix.utils.logging import get_logger

log = get_logger("link.tunnel")

#: Request headers the device accepts from the Portal (the Portal forwards
#: only these; anything else, cookies and bridge headers included, is dropped).
FORWARDED_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "content-type", "x-csrf-token",
    "x-requested-with", "if-none-match", "user-agent"})
#: Response headers never sent back over the link.
DROPPED_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "set-cookie",
    "content-length", "content-encoding"})
_BASE = re.compile(r"^/d/[A-Za-z0-9_-]{1,64}$")
#: Paths whose handler may legitimately take long (enrolment from the camera).
_SLOW_PATHS = ("/authorized/upload", "/api/authorized/capture")


@dataclass
class TunnelResult:
    status: int
    headers: list[list[str]] = field(default_factory=list)
    body: bytes = b""
    elapsed_ms: int = 0
    code: str = "ok"

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None


def error_result(code: str, message: str, *, retry_after: float | None = None,
                 started: float | None = None) -> TunnelResult:
    """A refused request: the console's JSON error shape ``{detail, code}``."""
    headers = [["content-type", "application/json"], ["cache-control", "no-store"]]
    if retry_after:
        headers.append(["retry-after", str(int(max(1, retry_after)))])
    elapsed = int((time.monotonic() - started) * 1000) if started else 0
    return TunnelResult(ERROR_CODES.get(code, 400), headers,
                        json.dumps({"detail": message, "code": code}).encode(),
                        elapsed, code)


class TunnelExecutor:
    """Replays ``tunnel.request`` messages into ``app`` (the LC FastAPI app).

    ``audit`` writes one ``remote_audit`` row per request (default
    ``link_store.record_remote``; it runs in a worker thread). The executor
    is used from one event loop; ``close`` releases the HTTP client.
    """

    def __init__(self, app: Any, *, policy: TunnelPolicy | None = None, cfg: Any = None,
                 audit: Callable[..., Any] | None = None) -> None:
        if cfg is None:
            from sentrix.config import settings
            cfg = settings.link
        if audit is None:
            from sentrix.storage import link_store
            audit = link_store.record_remote
        self.app = app
        self.cfg = cfg
        self.policy = policy or TunnelPolicy(media_per_minute=cfg.media_requests_per_minute)
        self._audit = audit
        self._client = None
        self.inflight = 0
        self.served = 0
        self.refused = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def _http(self):
        if self._client is None:
            import httpx
            transport = httpx.ASGITransport(app=self.app, client=tunnel_auth.TUNNEL_CLIENT)
            self._client = httpx.AsyncClient(transport=transport,
                                             base_url="http://sentrix.tunnel",
                                             follow_redirects=False)
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ── the request ───────────────────────────────────────────────────────────
    async def execute(self, message: dict[str, Any], body: bytes | None = None) -> TunnelResult:
        """Serve one ``tunnel.request`` (``body`` already reassembled from
        BODY chunks, or None to use ``body_b64``). Never raises except
        ``asyncio.CancelledError`` (``tunnel.cancel``)."""
        started = time.monotonic()
        method = str(message.get("method") or "").upper()
        path = message.get("path")
        target = f"{method} {path if isinstance(path, str) else '?'}"[:255]
        try:
            actor = Actor.from_dict(message.get("actor"))
        except ValueError as exc:
            raw = message.get("actor") if isinstance(message.get("actor"), dict) else {}
            code = ("forbidden_role" if (raw.get("type") or raw.get("role")) == "company_admin"
                    else "invalid_args")
            result = error_result(code, f"bad actor: {exc}", started=started)
            await self._record(None, target, False, result, str(exc), message)
            return result
        base = str(message.get("base") or "")
        if not _BASE.match(base):
            result = error_result("invalid_args", "bad base", started=started)
            await self._record(actor, target, False, result, "bad base", message)
            return result

        decision: Decision = self.policy.check_request(method, path, actor)
        if not decision:
            result = error_result(decision.code, decision.reason,
                                  retry_after=decision.retry_after, started=started)
            await self._record(actor, target, False, result, decision.reason, message)
            return result

        if body is None:
            body = _inline_body(message)
            if body is None:
                result = error_result("invalid_args", "body_b64 is not base64", started=started)
                await self._record(actor, target, False, result, "bad body", message)
                return result
        if len(body) > self.cfg.tunnel_max_request_bytes:
            result = error_result("too_large", "request body too large", started=started)
            await self._record(actor, target, False, result, "request too large", message)
            return result
        if self.inflight >= self.cfg.tunnel_max_inflight:
            result = error_result("device_busy", "too many tunnelled requests in flight",
                                  retry_after=1, started=started)
            await self._record(actor, target, False, result, "busy", message)
            return result

        self.inflight += 1
        try:
            result = await self._replay(message, method, path, body, actor, base, started)
        finally:
            self.inflight -= 1
        await self._record(actor, target, True, result,
                           None if result.code == "ok" else result.code, message)
        return result

    def timeout_for(self, message: dict[str, Any], method: str, path: str) -> float:
        """The request's own ``timeout_ms`` capped by the configured limit
        (uploads and captures get the longer one)."""
        clean = normalise_path(path) or path
        slow = method == "POST" and (clean in _SLOW_PATHS
                                     or "multipart/" in _header(message, "content-type"))
        cap = (self.cfg.tunnel_upload_timeout_seconds if slow
               else self.cfg.tunnel_timeout_seconds)
        try:
            asked = float(message.get("timeout_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            asked = 0.0
        return min(cap, asked) if asked > 0 else cap

    async def _replay(self, message: dict[str, Any], method: str, path: str, body: bytes,
                      actor: Actor, base: str, started: float) -> TunnelResult:
        headers = {name: value for name, value in _headers(message)
                   if name in FORWARDED_REQUEST_HEADERS}
        headers.update(tunnel_auth.build_headers(
            user_id=actor.id or "?", user_name=actor.name or "", role=actor.type, base=base,
            step_up_at=actor.step_up_at, request_id=str(message.get("id") or ""),
            csrf=str(message.get("csrf") or "") or None))
        query = str(message.get("query") or "")
        url = path + (f"?{query}" if query else "")
        timeout = self.timeout_for(message, method, path)
        try:
            response = await asyncio.wait_for(
                self._http().request(method, url, headers=headers, content=body or None),
                timeout)
        except asyncio.TimeoutError:
            return error_result("timeout", f"the console did not answer within {timeout:.0f} s",
                                started=started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("tunnel %s %s failed: %s", method, path, exc)
            return error_result("internal", "the console failed to answer", started=started)
        content = response.content
        if len(content) > self.cfg.tunnel_max_response_bytes:
            return error_result("too_large", "response too large for the tunnel",
                                started=started)
        kept = [[k, v] for k, v in response.headers.multi_items()
                if k.lower() not in DROPPED_RESPONSE_HEADERS
                and k.lower() not in tunnel_auth.BRIDGE_HEADERS]
        self.served += 1
        return TunnelResult(response.status_code, kept, content,
                            int((time.monotonic() - started) * 1000))

    async def _record(self, actor: Actor | None, target: str, allowed: bool,
                      result: TunnelResult, reason: str | None,
                      message: dict[str, Any]) -> None:
        if not allowed:
            self.refused += 1
        try:
            await asyncio.to_thread(
                self._audit, kind="tunnel", target=target, allowed=allowed,
                user_id=actor.id if actor else None, user_name=actor.name if actor else None,
                role=actor.type if actor else None, status=result.status, reason=reason,
                request_id=str(message.get("id") or "") or None)
        except Exception as exc:              # the audit must never break the answer
            log.error("remote audit failed: %s", exc)


def _headers(message: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    for pair in message.get("headers") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            name, value = pair
            if isinstance(name, str) and isinstance(value, str):
                out.append((name.lower(), value[:4096]))
    return out


def _header(message: dict[str, Any], name: str) -> str:
    for key, value in _headers(message):
        if key == name:
            return value.lower()
    return ""


def _inline_body(message: dict[str, Any]) -> bytes | None:
    import base64
    import binascii
    raw = message.get("body_b64")
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None


__all__ = ["TunnelExecutor", "TunnelResult", "error_result", "FORWARDED_REQUEST_HEADERS",
           "DROPPED_RESPONSE_HEADERS"]
