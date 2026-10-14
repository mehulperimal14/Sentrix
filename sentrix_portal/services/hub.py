"""Connected devices: the link hub (docs/7 §4.2, §5).

One Portal worker process; the hub keeps every device connection in memory on
the event loop (``app.state.link_hub``, made on first use by :func:`hub_for`).
The module functions with the frozen signatures (``is_online``,
``send_command``, ``push_profile``, ``issue_token``, ``unpair``, ``start``)
act on the hub of the app that used it last (one app in production).

What a connection goes through (``routes/link_hub.py`` hands the socket over):

1. **Auth** (handshake ``Authorization``): ``Bearer <token>`` is looked up by
   its HMAC digest; ``Bootstrap <device_id>:<secret>`` is accepted for a device
   that is not claimed (a limited session: hello, heartbeat, error; the Portal
   shows "online, unclaimed"), and for a claimed device only until it has
   connected once with its token (so a token issued while it was away still
   reaches it). Anything else closes with 4001.
2. **hello** -> device row updated (online, firmware, sanitised hello and
   health), an outbox *reset* detected (a factory-reset device numbers its
   events from 1 again: the old dedupe rows are dropped), then **welcome**
   (claimed, profile version, ``resend_from_seq`` = the highest contiguous seq
   stored, current subscriptions).
3. After the welcome: the token if one is waiting, the profile if the device's
   copy is older, queued commands, ``sos.sync`` for requests the cloud
   advanced while the device was away, and the viewers' subscriptions.
4. **Events** are stored as ``device_events`` (primary key (device, seq)) in
   the same transaction as their mirror effect, then acknowledged (batched,
   within 100 ms). A duplicate is re-acked and not re-applied. SOS events go to
   ``services.sos`` (per-request order, gaps buffered). Every heartbeat is
   answered with ``ack{upto}``, which the device uses as Portal liveness.
5. **state** and FRAME messages are fanned out to the relays' viewers;
   ``tunnel.response`` and ``command.result`` resolve the waiting callers.

A device silent for ``heartbeat_lost_seconds`` (45 s) is marked offline by
the watcher (:func:`start`): ``device.offline`` audit and, through
``services.sos.on_device_offline``, the cloud SOS authority and the tamper
signal for an open L2+ incident.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sentrix.link import protocol
from sentrix.utils.redact import redact_url
from sentrix_portal.models import (Device, DeviceCommand, DeviceEvent, DispatchMirror,
                                   EvidenceMetaMirror, Household, IncidentMirror,
                                   SnapshotMirror, utcnow)

log = logging.getLogger("sentrix_portal.hub")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: Incident types the mirror accepts from a device (anything else is "other").
INCIDENT_TYPES = frozenset({"normal", "weapon", "fire", "intrusion", "assault", "robbery",
                            "duress", "smoke", "stick", "unknown"})
#: Message types a bootstrap (unclaimed) session may send.
BOOTSTRAP_TYPES = frozenset({protocol.HELLO, protocol.HEARTBEAT, protocol.ERROR})
#: Waiting for the device's hello after the handshake (s).
HELLO_TIMEOUT = 15.0
#: Acks are batched for at most this long (s), or until this many are pending.
ACK_DELAY = 0.1
ACK_BATCH = 32
#: The watcher's period (s).
WATCH_EVERY = 5.0
#: Subscription renewals happen this often (s), well inside the 75 s lease.
RENEW_EVERY = 30.0
#: Commands queued for an offline device expire this long after delivery (s).
QUEUED_COMMAND_TTL = 60.0


def _cfg():
    from sentrix.config import settings
    return settings


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _uuid(value: Any) -> str | None:
    text = str(value or "").lower()
    return text if UUID_RE.match(text) else None


def _incident_type(value: Any) -> str:
    text = str(value or "").lower()
    return text if text in INCIDENT_TYPES else "unknown"


def _redacted(node: Any, depth: int = 0) -> Any:
    """Device-supplied JSON with every string URL-redacted (docs/7 §12)."""
    if depth > 6:
        return None
    if isinstance(node, dict):
        return {str(k)[:64]: _redacted(v, depth + 1) for k, v in list(node.items())[:200]}
    if isinstance(node, list):
        return [_redacted(v, depth + 1) for v in node[:200]]
    if isinstance(node, str):
        return redact_url(node[:2000])
    if isinstance(node, (int, float, bool)) or node is None:
        return node
    return str(node)[:200]


# ── connections, viewers ──────────────────────────────────────────────────────
@dataclass(eq=False)
class Viewer:
    """One browser connection to a relay. ``key`` groups the connections of
    one Portal session (a dashboard tab opens both relays: one viewer)."""

    key: str
    role: str
    actor: dict
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))

    def offer(self, item: Any) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


@dataclass
class TunnelReply:
    status: int
    headers: list
    body: bytes
    elapsed_ms: int = 0
    code: str = "ok"


class _TunnelCall:
    def __init__(self) -> None:
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.head: dict | None = None
        self.chunks: list[bytes] = []
        self.size = 0


class DeviceSession:
    def __init__(self, device_id: str, conn: Any, auth: str, claimed: bool,
                 household_id: str | None) -> None:
        self.device_id = device_id
        self.conn = conn
        self.auth = auth
        self.claimed = claimed
        self.household_id = household_id
        self.hello: dict = {}
        self.heartbeat: dict = {}
        self.connected_at = time.monotonic()
        self.last_seen = time.monotonic()
        self.commands: dict[str, asyncio.Future] = {}
        self.tunnels: dict[int, _TunnelCall] = {}
        self.next_stream = 1
        self.state_payload: dict | None = None
        self.state_n = 0
        self.frame: tuple[int, int, bytes] | None = None
        self.frame_times: deque = deque(maxlen=30)
        self.delay_ms: int | None = None
        self.ack_pending: set[int] = set()
        self.ack_task: asyncio.Task | None = None
        self.watermark = 0
        self.errors: list[float] = []
        self.send_lock = asyncio.Lock()
        self.video_errors: dict[str, dict] = {}
        self.state_sub: str | None = None
        self.video_sub: str | None = None
        self.closed = False

    @property
    def full(self) -> bool:
        return self.claimed and self.auth == "bearer"

    def remote_fps(self) -> float:
        times = [t for t in self.frame_times if time.monotonic() - t < 5.0]
        if len(times) < 2:
            return 0.0
        return round((len(times) - 1) / max(times[-1] - times[0], 1e-3), 1)


# ── the hub ───────────────────────────────────────────────────────────────────
class LinkHub:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.sessions: dict[str, DeviceSession] = {}
        self.pending_tokens: dict[str, str] = {}
        self.state_viewers: dict[str, set[Viewer]] = {}
        self.video_viewers: dict[str, set[Viewer]] = {}
        #: The last state payload per device, kept after it disconnects so the
        #: relay can show it greyed out (camera offline, AI 0 Hz, link red).
        self.last_state: dict[str, dict] = {}
        self._renewers: dict[str, asyncio.Task] = {}
        self._idle_stops: dict[str, asyncio.Task] = {}
        self._background: set[asyncio.Task] = set()
        #: The event loop the device sockets run on (set by the first one).
        self.loop: asyncio.AbstractEventLoop | None = None

    @property
    def db(self):
        return self.app.state.db

    @property
    def settings(self):
        return self.app.state.settings

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def schedule(self, coro) -> None:
        """Run ``coro`` on the hub's loop from any thread (workers, listeners)."""
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (loop is None or running is loop):
            self._spawn(coro)
        elif loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()

    def online(self, device_id: str) -> bool:
        session = self.sessions.get(device_id)
        return session is not None and session.full and not session.closed

    # ── the socket ────────────────────────────────────────────────────────────
    async def serve(self, conn: Any, authorization: str | None) -> None:
        """Run one device connection until it closes."""
        self.loop = asyncio.get_running_loop()
        found = await asyncio.to_thread(self._authenticate, authorization or "")
        if found is None:
            await conn.close(protocol.CLOSE_UNAUTHORISED, "unauthorised")
            return
        if found == "retired":
            await conn.close(protocol.CLOSE_REVOKED, "device retired")
            return
        device_id, auth, claimed, household_id = found
        session = DeviceSession(device_id, conn, auth, claimed, household_id)
        try:
            first = await asyncio.wait_for(conn.receive(), HELLO_TIMEOUT)
            hello = protocol.decode(first) if isinstance(first, str) else None
        except protocol.ProtocolError as exc:
            code = (protocol.CLOSE_UNSUPPORTED if exc.code == "unsupported"
                    else protocol.CLOSE_UNAUTHORISED)
            await conn.close(code, str(exc))
            return
        except Exception:
            await conn.close(protocol.CLOSE_NORMAL, "no hello")
            return
        if hello is None or hello.get("type") != protocol.HELLO:
            await conn.close(1008, "hello first")
            return
        if hello.get("protocol") != protocol.VERSION:
            await conn.close(protocol.CLOSE_UNSUPPORTED, "protocol version not supported")
            return
        if str(hello.get("device_id") or "").lower() != device_id:
            await conn.close(protocol.CLOSE_UNAUTHORISED, "hello for another device")
            return

        previous = self.sessions.get(device_id)
        if previous is not None and not previous.closed:
            previous.closed = True
            await previous.conn.close(protocol.CLOSE_REPLACED, "replaced by a newer connection")
            self._fail_waiters(previous, "replaced")
        session.hello = hello
        welcome = await asyncio.to_thread(self._on_hello, session, hello)
        self.sessions[device_id] = session
        try:
            await self._send(session, welcome)
            await self._after_welcome(session)
            await self._receive_loop(session)
        except Exception as exc:
            if not _is_closed(exc):
                log.error("device %s: link error: %s", device_id, exc, exc_info=True)
        finally:
            session.closed = True
            if self.sessions.get(device_id) is session:
                self.sessions.pop(device_id, None)
            self._fail_waiters(session, "disconnected")
            if session.ack_task is not None:
                session.ack_task.cancel()
            await self._flush_acks(session, final=True)
            try:
                await conn.close(protocol.CLOSE_NORMAL, "")
            except Exception:
                pass

    def _authenticate(self, header: str):
        """(device_id, auth, claimed, household_id), "retired", or None."""
        from sentrix_portal import security
        scheme, _, credential = header.strip().partition(" ")
        credential = credential.strip()
        with self.db.session() as db:
            if scheme == protocol.AUTH_BEARER and credential:
                device = (db.query(Device)
                          .filter_by(token_hash=security.token_digest(credential)).first())
                if device is None:
                    return None
                if device.state == "RETIRED":
                    return "retired"
                if device.state != "CLAIMED":
                    return None
                return device.id, "bearer", True, device.household_id
            if scheme == protocol.AUTH_BOOTSTRAP and ":" in credential:
                device_id, _, secret = credential.partition(":")
                device = db.get(Device, device_id.lower())
                if device is None or not device.bootstrap_hash or not secret:
                    return None
                if not hmac.compare_digest(device.bootstrap_hash, security.token_digest(secret)):
                    return None
                if device.state == "RETIRED":
                    return "retired"
                hello = device.last_hello or {}
                if device.state == "CLAIMED" and hello.get("token_confirmed"):
                    return None                    # bootstrap revoked once the token worked
                return device.id, "bootstrap", device.state == "CLAIMED", device.household_id
        return None

    def _on_hello(self, session: DeviceSession, hello: dict) -> dict:
        from sentrix_portal import security
        outbox = hello.get("outbox") if isinstance(hello.get("outbox"), dict) else {}
        with self.db.session() as db:
            device = db.get(Device, session.device_id)
            previous = dict(device.last_hello or {})
            epoch = outbox.get("epoch") if isinstance(outbox.get("epoch"), str) else None
            tail = outbox.get("tail_seq") if isinstance(outbox.get("tail_seq"), int) else None
            known_epoch = previous.get("outbox_epoch")
            reset = ((epoch and known_epoch and epoch != known_epoch)
                     or (tail is not None and tail < (device.last_acked_seq or 0)))
            if reset and (device.last_acked_seq or 0) > 0:
                dropped = db.query(DeviceEvent).filter_by(device_id=device.id).delete()
                device.last_acked_seq = 0
                security.audit(db, "device.outbox_reset", actor_type="device",
                               actor_id=device.id, household_id=device.household_id,
                               target_type="device", target_id=device.id,
                               detail={"dropped": dropped, "epoch": epoch, "tail_seq": tail})
            record = {key: _redacted(hello.get(key)) for key in (
                "serial", "firmware", "app_version", "protocol", "session_id",
                "profile_version", "outbox", "capabilities", "test_mode", "armed", "health")}
            record.update(auth=session.auth, received_at=utcnow().isoformat(),
                          outbox_epoch=epoch or known_epoch,
                          token_confirmed=bool(previous.get("token_confirmed"))
                          or session.auth == "bearer")
            device.last_hello = record
            device.firmware = _text(hello.get("firmware"), 64)
            device.armed = bool(hello.get("armed"))
            device.last_seen_at = utcnow()
            if not device.online:
                device.online = True
                security.audit(db, "device.online", actor_type="device", actor_id=device.id,
                               household_id=device.household_id, target_type="device",
                               target_id=device.id, detail={"auth": session.auth})
            session.watermark = device.last_acked_seq or 0
            version = 0
            if device.household_id:
                household = db.get(Household, device.household_id)
                version = int(household.profile_version or 0) if household else 0
        return protocol.envelope(
            protocol.WELCOME, device_id=session.device_id, claimed=session.claimed,
            server_time_ms=protocol.now_ms(), profile_version=version,
            resend_from_seq=session.watermark if session.full else 0,
            subscriptions={"state": bool(self.state_viewers.get(session.device_id))
                           and session.full, "video": None},
            heartbeat_seconds=_cfg().link.heartbeat_seconds)

    async def _after_welcome(self, session: DeviceSession) -> None:
        device_id = session.device_id
        if session.auth == "bootstrap" and session.claimed:
            token = self.pending_tokens.pop(device_id, None)
            if token is None:                # issued while the Portal restarted: mint again
                token = await asyncio.to_thread(self._mint_token, device_id)
            await self._send(session, protocol.envelope(
                protocol.TOKEN_ISSUE, token=token, issued_at=utcnow().isoformat()))
            return
        if not session.full:
            return
        version = await asyncio.to_thread(self._profile_needed, session)
        if version:
            await self._push_profile_now(session)
        for name, args, actor, row_id in await asyncio.to_thread(self._queued_commands,
                                                                 device_id):
            self._spawn(self._deliver_queued(session, row_id, name, args, actor))
        from sentrix_portal.services import sos as sos_service
        for message in await asyncio.to_thread(self._with_db, sos_service.sync_messages,
                                               device_id):
            await self._send(session, message)
        await self._resubscribe(session)
        await self._send_viewer_count(device_id)

    def _with_db(self, fn, *args):
        with self.db.session() as db:
            return fn(db, *args)

    def _profile_needed(self, session: DeviceSession) -> int:
        with self.db.session() as db:
            device = db.get(Device, session.device_id)
            household = db.get(Household, device.household_id) if device.household_id else None
            latest = int(household.profile_version or 0) if household else 0
        active = session.hello.get("profile_version")
        active = active if isinstance(active, int) else 0
        return latest if latest > active else 0

    # ── receiving ─────────────────────────────────────────────────────────────
    async def _receive_loop(self, session: DeviceSession) -> None:
        while True:
            data = await session.conn.receive()
            session.last_seen = time.monotonic()
            if isinstance(data, (bytes, bytearray)):
                await self._on_binary(session, bytes(data))
                continue
            if len(data) > protocol.MAX_MESSAGE_BYTES:
                await self._protocol_error(session, "too_large", "message too large", None)
                continue
            try:
                message = protocol.decode(data)
            except protocol.ProtocolError as exc:
                if exc.code == "unsupported":
                    await session.conn.close(protocol.CLOSE_UNSUPPORTED, str(exc))
                    return
                await self._protocol_error(session, exc.code, str(exc), None)
                continue
            kind = message["type"]
            if not session.full and kind not in BOOTSTRAP_TYPES:
                await self._protocol_error(session, "unauthorised",
                                           f"{kind} needs a claimed device", message.get("id"))
                continue
            handler = {
                protocol.HEARTBEAT: self._on_heartbeat, protocol.EVENT: self._on_event,
                protocol.STATE: self._on_state, protocol.TUNNEL_RESPONSE: self._on_tunnel_response,
                protocol.COMMAND_RESULT: self._on_command_result,
                protocol.PROFILE_ACK: self._on_profile_ack, protocol.ERROR: self._on_error,
            }.get(kind)
            if handler is None:
                await self._protocol_error(session, "unknown_type", f"unknown type {kind!r}",
                                           message.get("id"))
                continue
            try:
                await handler(session, message)
            except (ValueError, TypeError, KeyError) as exc:
                await self._protocol_error(session, "invalid_args", str(exc), message.get("id"))

    async def _protocol_error(self, session: DeviceSession, code: str, text: str,
                              ref: str | None) -> None:
        now = time.monotonic()
        session.errors = [t for t in session.errors if now - t < 10.0] + [now]
        await self._send(session, protocol.error(code, text, ref))
        if len(session.errors) >= 3:
            await session.conn.close(1008, "too many protocol errors")
            raise ConnectionError("too many protocol errors")

    async def _send(self, session: DeviceSession, message: dict) -> None:
        text = protocol.encode(message)
        async with session.send_lock:
            await session.conn.send_text(text)

    async def _send_bytes(self, session: DeviceSession, data: bytes) -> None:
        async with session.send_lock:
            await session.conn.send_bytes(data)

    async def _on_heartbeat(self, session: DeviceSession, message: dict) -> None:
        session.heartbeat = message
        await asyncio.to_thread(self._store_heartbeat, session, message)
        await self._send(session, protocol.envelope(protocol.ACK, seqs=[],
                                                    upto=session.watermark))

    def _store_heartbeat(self, session: DeviceSession, message: dict) -> None:
        keep = ("camera_online", "video_fps", "ai_hz", "frame_age_ms", "audio", "armed",
                "alarm", "open_request_id", "node", "modem", "channels", "outbox_depth",
                "remote_viewers", "uptime_s", "profile_version", "time_ms")
        record = {key: _redacted(message.get(key)) for key in keep}
        record["received_at"] = utcnow().isoformat()
        with self.db.session() as db:
            device = db.get(Device, session.device_id)
            if device is None:
                return
            device.last_heartbeat = record
            device.last_seen_at = utcnow()
            device.armed = bool(message.get("armed"))
            if not device.online:
                from sentrix_portal import security
                device.online = True
                security.audit(db, "device.online", actor_type="device", actor_id=device.id,
                               household_id=device.household_id, target_type="device",
                               target_id=device.id, detail={"via": "heartbeat"})

    async def _on_event(self, session: DeviceSession, message: dict) -> None:
        seq = message.get("seq")
        kind = message.get("kind")
        if not isinstance(seq, int) or seq < 1:
            raise ValueError("event without a positive integer seq")
        if kind not in protocol.EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        followups = await asyncio.to_thread(self._ingest, session, seq, kind, payload,
                                            message.get("at"))
        session.ack_pending.add(seq)
        if len(session.ack_pending) >= ACK_BATCH:
            await self._flush_acks(session)
        elif session.ack_task is None or session.ack_task.done():
            session.ack_task = asyncio.ensure_future(self._ack_later(session))
        for name, args in followups:
            self._spawn(self.send_command(session.device_id, name, args,
                                          _cloud_actor(), timeout=10,
                                          queue_if_offline=True))

    async def _ack_later(self, session: DeviceSession) -> None:
        await asyncio.sleep(ACK_DELAY)
        await self._flush_acks(session)

    async def _flush_acks(self, session: DeviceSession, final: bool = False) -> None:
        seqs = sorted(session.ack_pending)
        session.ack_pending.clear()
        if not seqs or session.closed and not final:
            return
        try:
            await self._send(session, protocol.envelope(protocol.ACK, seqs=seqs,
                                                        upto=session.watermark))
        except Exception:
            pass                                  # unacked: the device sends them again

    def _ingest(self, session: DeviceSession, seq: int, kind: str, payload: dict,
                at: Any) -> list[tuple[str, dict]]:
        """Store one event and apply its effect in one transaction. Returns
        device commands to send after the commit (e.g. ``delivered``)."""
        from sentrix_portal import security
        followups: list[tuple[str, dict]] = []
        try:
            with self.db.session() as db:
                device = db.get(Device, session.device_id)
                existing = db.get(DeviceEvent, (session.device_id, seq))
                if existing is not None:
                    if existing.kind == kind and existing.payload == payload:
                        self._advance_watermark(db, device, session)
                        return []
                    # Same number, different event: the device's outbox was
                    # reset without the hello saying so. Keep the new one.
                    db.delete(existing)
                    db.flush()
                db.add(DeviceEvent(device_id=session.device_id, seq=seq, kind=kind,
                                   received_at=utcnow(), payload=payload))
                db.flush()
                followups = self._apply(db, device, kind, payload, at) or []
                self._advance_watermark(db, device, session)
            return followups
        except Exception as exc:
            log.error("device %s event %s (%s) could not be applied: %s",
                      session.device_id, seq, kind, exc, exc_info=True)
        # Keep the raw event (nothing is lost) and acknowledge it.
        with self.db.session() as db:
            device = db.get(Device, session.device_id)
            if db.get(DeviceEvent, (session.device_id, seq)) is None:
                db.add(DeviceEvent(device_id=session.device_id, seq=seq, kind=kind,
                                   received_at=utcnow(), payload=payload))
            security.audit(db, "device.event_failed", actor_type="device",
                           actor_id=session.device_id,
                           household_id=device.household_id if device else None,
                           target_type="device", target_id=session.device_id,
                           detail={"seq": seq, "kind": kind})
            db.flush()
            if device is not None:
                self._advance_watermark(db, device, session)
        return []

    @staticmethod
    def _advance_watermark(db, device: Device, session: DeviceSession) -> None:
        mark = device.last_acked_seq or 0
        seqs = [row[0] for row in db.query(DeviceEvent.seq)
                .filter(DeviceEvent.device_id == device.id, DeviceEvent.seq > mark)
                .order_by(DeviceEvent.seq)]
        for value in seqs:
            if value != mark + 1:
                break
            mark = value
        device.last_acked_seq = mark
        session.watermark = mark

    def _apply(self, db, device: Device, kind: str, payload: dict,
               at: Any) -> list[tuple[str, dict]] | None:
        from sentrix_portal import security
        from sentrix_portal.services import sos as sos_service
        household_id = device.household_id
        if kind in protocol.SOS_EVENT_KINDS:
            return sos_service.ingest_device_event(db, device, kind, payload)
        if kind == "incident.upsert":
            incident_id = _uuid(payload.get("id"))
            if incident_id is None:
                raise ValueError("incident without a UUID id")
            row = db.get(IncidentMirror, incident_id) or IncidentMirror(id=incident_id)
            started = _parse_dt(payload.get("started_at"))
            row.device_id, row.household_id = device.id, household_id
            row.started_at = started
            duration = payload.get("duration_seconds")
            if started and isinstance(duration, (int, float)) and not payload.get("ongoing"):
                row.ended_at = started + timedelta(seconds=float(duration))
            row.peak_level = int(payload.get("peak_level") or 0)
            row.peak_tci = float(payload.get("peak_tci") or 0.0)
            row.incident_type = _incident_type(payload.get("incident_type"))
            row.reason = _text(payload.get("reason"), 255)
            row.data = _redacted({k: payload.get(k) for k in (
                "session_id", "ongoing", "duration_seconds", "frames", "evidence_count",
                "snapshot_path", "evidence_bundle", "evidence_hash", "person_present",
                "authorized", "actions")})
            row.updated_at = utcnow()
            db.merge(row)
        elif kind == "dispatch.upsert":
            package_id = _uuid(payload.get("id"))
            if package_id is None:
                raise ValueError("dispatch package without a UUID id")
            row = db.get(DispatchMirror, package_id) or DispatchMirror(id=package_id)
            row.device_id, row.household_id = device.id, household_id
            row.incident_id = _uuid(payload.get("incident_id"))
            row.request_id = _uuid(payload.get("request_id"))
            row.status = _text(payload.get("status"), 16)
            row.authority = _text(payload.get("authority"), 10)
            row.chosen_authority = _text(payload.get("chosen_authority"), 10)
            row.created_at = _parse_dt(payload.get("created_iso") or payload.get("created_at"))
            row.sent_at = _parse_dt(payload.get("sent_at"))
            row.sent_by = _text(payload.get("sent_by"), 64)
            row.data = _redacted({k: payload.get(k) for k in (
                "incident_type", "level", "tci", "camera_location", "evidence_hash",
                "sent_to", "error", "expires_at")})
            db.merge(row)
        elif kind == "verdict.set":
            incident_id = _uuid(payload.get("incident_id"))
            if incident_id is None:
                raise ValueError("verdict without a UUID incident id")
            row = db.get(IncidentMirror, incident_id)
            if row is None:
                row = IncidentMirror(id=incident_id, device_id=device.id,
                                     household_id=household_id)
                db.add(row)
            verdict = str(payload.get("verdict") or "").upper()
            row.verdict = verdict if verdict in ("TRUE", "FALSE") else None
            row.verdict_by = _text(payload.get("by"), 64)
            row.verdict_at = _parse_dt(payload.get("at")) or utcnow()
            row.request_id = _uuid(payload.get("request_id")) or row.request_id
            row.updated_at = utcnow()
            security.audit(db, "verdict.set", actor_type="device", actor_id=device.id,
                           household_id=household_id, target_type="incident",
                           target_id=incident_id,
                           detail={"verdict": row.verdict, "by": row.verdict_by,
                                   "effect": _text(payload.get("effect"), 32)})
        elif kind == "evidence.meta":
            from sentrix.web.security import sanitize_filename
            bundle = sanitize_filename(str(payload.get("bundle_id") or ""))[:128]
            if not bundle:
                raise ValueError("evidence without a bundle id")
            row = db.get(EvidenceMetaMirror, bundle) or EvidenceMetaMirror(bundle_id=bundle)
            row.device_id, row.household_id = device.id, household_id
            row.incident_id = _uuid(payload.get("incident_id"))
            row.created_at = _parse_dt(payload.get("created_at"))
            row.level = int(payload.get("level") or 0)
            row.sha256 = _text(payload.get("sha256"), 64)
            detections = [{"label": _text(d.get("label"), 32), "conf": d.get("conf")}
                          for d in payload.get("detections") or [] if isinstance(d, dict)]
            row.data = {"tci": payload.get("tci"), "camera_id": _text(payload.get("camera_id"), 32),
                        "incident_type": _incident_type(payload.get("incident_type")),
                        "detections": detections[:50]}
            db.merge(row)
        elif kind == "snapshot.meta":
            from sentrix.web.security import sanitize_filename
            name = sanitize_filename(str(payload.get("name") or ""))[:128]
            if not name:
                raise ValueError("snapshot without a name")
            row = db.get(SnapshotMirror, (name, device.id)) or SnapshotMirror(
                name=name, device_id=device.id)
            row.household_id = household_id
            row.incident_id = _uuid(payload.get("incident_id"))
            row.created_at = _parse_dt(payload.get("created_at"))
            db.merge(row)
        elif kind == "gallery.audit":
            security.audit(db, "gallery.audit", actor_type="device", actor_id=device.id,
                           household_id=household_id, target_type="device",
                           target_id=device.id,
                           detail={k: _text(payload.get(k), 100) for k in
                                   ("event", "person", "file", "via", "by", "reason")})
        elif kind == "mode.changed":
            device.armed = bool(payload.get("armed"))
            security.audit(db, "mode.set", actor_type="device", actor_id=device.id,
                           household_id=household_id, target_type="device",
                           target_id=device.id,
                           detail={"armed": device.armed, "by": _text(payload.get("by"), 64),
                                   "at": _text(payload.get("at"), 40)})
        elif kind == "node.alert":
            security.audit(db, "device.node_alert", actor_type="device", actor_id=device.id,
                           household_id=household_id, target_type="device",
                           target_id=device.id,
                           detail={"kind": _text(payload.get("kind"), 32),
                                   "at": _text(payload.get("at"), 40),
                                   "detail": _redacted(payload.get("detail"))})
        # remote.audit and notification.status: the device_events row is the record.
        return None

    async def _on_state(self, session: DeviceSession, message: dict) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("state without a payload")
        session.state_payload = payload
        session.state_n = int(message.get("n") or 0)
        self.last_state[session.device_id] = payload
        viewers = self.state_viewers.get(session.device_id)
        if viewers:
            out = self._decorate(session, payload)
            for viewer in list(viewers):
                viewer.offer(out)

    def _decorate(self, session: DeviceSession, payload: dict) -> dict:
        """The relay's additions: the remote video rate and delay."""
        out = dict(payload)
        if self.video_viewers.get(session.device_id):
            out["remote_video"] = {"fps": session.remote_fps(), "delay_ms": session.delay_ms}
        return out

    async def _on_binary(self, session: DeviceSession, data: bytes) -> None:
        try:
            header, payload = protocol.unpack_binary(data)
        except ValueError as exc:
            await self._protocol_error(session, "invalid_args", str(exc), None)
            return
        if not session.full:
            await self._protocol_error(session, "unauthorised", "binary needs a claimed device",
                                       None)
            return
        if header.kind == protocol.KIND_FRAME:
            if len(payload) > protocol.MAX_FRAME_JPEG_BYTES or not payload.startswith(b"\xff\xd8"):
                await self._protocol_error(session, "invalid_args", "not a JPEG frame", None)
                return
            session.frame = (header.a, header.b, payload)
            session.frame_times.append(time.monotonic())
            session.delay_ms = max(0, protocol.now_ms() - int(header.b))
            for viewer in list(self.video_viewers.get(session.device_id, ())):
                viewer.offer(session.frame)
            return
        call = session.tunnels.get(header.a)
        if call is None or call.head is None:
            await self._protocol_error(session, "invalid_args",
                                       f"no chunked response on stream {header.a}", None)
            return
        call.chunks.append(payload)
        call.size += len(payload)
        if call.size > _cfg().link.tunnel_max_response_bytes:
            session.tunnels.pop(header.a, None)
            if not call.future.done():
                call.future.set_result(_error_reply("too_large", "response too large"))
            return
        if header.flags & protocol.FLAG_LAST:
            session.tunnels.pop(header.a, None)
            if not call.future.done():
                call.future.set_result(self._reply(call.head, b"".join(call.chunks)))

    async def _on_tunnel_response(self, session: DeviceSession, message: dict) -> None:
        stream = int(message["stream"])
        call = session.tunnels.get(stream)
        if call is None:
            return                                   # cancelled or timed out
        if message.get("chunked"):
            call.head = message
            if int(message.get("body_len") or 0) == 0:
                session.tunnels.pop(stream, None)
                call.future.set_result(self._reply(message, b""))
            return
        session.tunnels.pop(stream, None)
        try:
            body = base64.b64decode(message.get("body_b64") or "", validate=True)
        except Exception:
            body = None
        if not call.future.done():
            call.future.set_result(self._reply(message, body) if body is not None
                                   else _error_reply("internal", "bad tunnel body"))

    @staticmethod
    def _reply(head: dict, body: bytes) -> TunnelReply:
        headers = [[str(k), str(v)] for k, v in head.get("headers") or []
                   if isinstance(k, str) and isinstance(v, str)]
        return TunnelReply(int(head.get("status") or 502), headers, body,
                           int(head.get("elapsed_ms") or 0))

    async def _on_command_result(self, session: DeviceSession, message: dict) -> None:
        future = session.commands.pop(str(message.get("ref")), None)
        if future is not None and not future.done():
            future.set_result(message)

    async def _on_profile_ack(self, session: DeviceSession, message: dict) -> None:
        await asyncio.to_thread(self._store_profile_ack, session, message)

    def _store_profile_ack(self, session: DeviceSession, message: dict) -> None:
        from sentrix_portal import security
        version = message.get("version")
        with self.db.session() as db:
            device = db.get(Device, session.device_id)
            if message.get("ok") and isinstance(version, int):
                device.profile_version_acked = max(device.profile_version_acked or 0, version)
            security.audit(db, "profile.pushed", actor_type="device", actor_id=device.id,
                           household_id=device.household_id, target_type="device",
                           target_id=device.id,
                           detail={"version": version, "ok": bool(message.get("ok")),
                                   "error": _text(message.get("error"), 120)})

    async def _on_error(self, session: DeviceSession, message: dict) -> None:
        ref = str(message.get("ref") or "")
        if ref in session.video_errors:
            session.video_errors[ref] = {"code": message.get("code"),
                                         "message": message.get("message")}
        log.info("device %s error %s: %s", session.device_id, message.get("code"),
                 message.get("message"))

    def _fail_waiters(self, session: DeviceSession, why: str) -> None:
        for future in session.commands.values():
            if not future.done():
                future.set_result(None)
        session.commands.clear()
        for call in session.tunnels.values():
            if not call.future.done():
                call.future.set_result(_error_reply("device_offline", f"device {why}"))
        session.tunnels.clear()

    # ── commands ──────────────────────────────────────────────────────────────
    async def send_command(self, device_id: str, name: str, args: dict, actor: dict, *,
                           timeout: float = 10.0,
                           queue_if_offline: bool = True) -> dict[str, Any]:
        if name not in protocol.COMMANDS:
            return _command_result(False, "unknown_command", f"{name} is not a command")
        session = self.sessions.get(device_id)
        if session is None or not session.full or session.closed:
            if queue_if_offline:
                row_id = await asyncio.to_thread(self._queue_command, device_id, name, args,
                                                 actor)
                if row_id is not None:
                    return _command_result(False, "device_offline",
                                           "the device is offline: queued, will sync",
                                           queued=True, command_id=row_id)
            return _command_result(False, "device_offline", "the device is offline")
        row_id = await asyncio.to_thread(self._record_command, device_id, name, args, actor,
                                         "SENT")
        message = protocol.envelope(protocol.COMMAND, name=name, args=args, actor=actor,
                                    issued_at=protocol.now_ms(),
                                    expires_at=protocol.now_ms() + int(timeout * 1000))
        future = asyncio.get_running_loop().create_future()
        session.commands[message["id"]] = future
        try:
            await self._send(session, message)
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            session.commands.pop(message["id"], None)
            await asyncio.to_thread(self._finish_command, row_id, "FAILED",
                                    {"code": "timeout"})
            return _command_result(False, "timeout", "the device did not answer in time",
                                   command_id=row_id)
        except Exception as exc:
            session.commands.pop(message["id"], None)
            if not _is_closed(exc):
                log.error("command %s to %s failed: %s", name, device_id, exc)
            await asyncio.to_thread(self._finish_command, row_id, "FAILED",
                                    {"code": "device_offline"})
            return _command_result(False, "device_offline", "the device went offline",
                                   command_id=row_id)
        if reply is None:
            await asyncio.to_thread(self._finish_command, row_id, "FAILED",
                                    {"code": "device_offline"})
            return _command_result(False, "device_offline", "the device went offline",
                                   command_id=row_id)
        result = _command_result(bool(reply.get("ok")), str(reply.get("code") or "internal"),
                                 str(reply.get("message") or ""), result=reply.get("result"),
                                 status=reply.get("status"), command_id=row_id)
        await asyncio.to_thread(self._finish_command, row_id,
                                "DONE" if result["ok"] else "FAILED",
                                {"code": result["code"], "message": result["message"]})
        return result

    @staticmethod
    def _stored_args(args: dict, actor: dict) -> dict:
        """What ``device_commands`` keeps: never a PIN."""
        kept = {k: v for k, v in (args or {}).items() if k not in ("pin", "text")}
        kept["_actor"] = actor
        return kept

    def _record_command(self, device_id: str, name: str, args: dict, actor: dict,
                        status: str) -> str | None:
        with self.db.session() as db:
            if db.get(Device, device_id) is None:
                return None
            row = DeviceCommand(device_id=device_id, name=name,
                                args=self._stored_args(args, actor),
                                user_id=_text((actor or {}).get("id"), 36), status=status,
                                sent_at=utcnow() if status == "SENT" else None)
            db.add(row)
            db.flush()
            return row.id

    def _queue_command(self, device_id: str, name: str, args: dict, actor: dict) -> str | None:
        if args and ("pin" in args or name == "sms.send"):
            # A PIN or a message text is never stored for later; the caller
            # asks again when the device is back.
            args = {k: v for k, v in args.items() if k != "pin"}
            if name == "sms.send":
                return None
        return self._record_command(device_id, name, args, actor, "QUEUED")

    def _finish_command(self, row_id: str | None, status: str, result: dict) -> None:
        if row_id is None:
            return
        with self.db.session() as db:
            row = db.get(DeviceCommand, row_id)
            if row is not None:
                row.status = status
                row.result = result

    def _queued_commands(self, device_id: str) -> list[tuple[str, dict, dict, str]]:
        with self.db.session() as db:
            rows = (db.query(DeviceCommand).filter_by(device_id=device_id, status="QUEUED")
                    .order_by(DeviceCommand.created_at).all())
            out = []
            for row in rows:
                args = dict(row.args or {})
                actor = args.pop("_actor", None) or _cloud_actor()
                out.append((row.name, args, actor, row.id))
                row.status = "SENT"
                row.sent_at = utcnow()
            return out

    async def _deliver_queued(self, session: DeviceSession, row_id: str, name: str,
                              args: dict, actor: dict) -> None:
        message = protocol.envelope(protocol.COMMAND, name=name, args=args, actor=actor,
                                    issued_at=protocol.now_ms(),
                                    expires_at=protocol.now_ms() + int(QUEUED_COMMAND_TTL * 1000))
        future = asyncio.get_running_loop().create_future()
        session.commands[message["id"]] = future
        try:
            await self._send(session, message)
            reply = await asyncio.wait_for(future, QUEUED_COMMAND_TTL)
        except Exception:
            session.commands.pop(message["id"], None)
            reply = None
        status = "DONE" if reply and reply.get("ok") else "FAILED"
        await asyncio.to_thread(self._finish_command, row_id, status,
                                {"code": (reply or {}).get("code", "device_offline"),
                                 "message": (reply or {}).get("message", "")})

    # ── tunnel ────────────────────────────────────────────────────────────────
    async def tunnel(self, device_id: str, *, method: str, path: str, query: str,
                     headers: list, body: bytes, actor: dict, base: str, csrf: str | None,
                     timeout: float) -> TunnelReply:
        session = self.sessions.get(device_id)
        if session is None or not session.full or session.closed:
            return _error_reply("device_offline", "the device is offline")
        stream = session.next_stream
        session.next_stream = (session.next_stream % 0xFFFFFFFF) + 1
        call = _TunnelCall()
        session.tunnels[stream] = call
        cfg = _cfg().link
        fields: dict[str, Any] = {"stream": stream, "method": method, "path": path,
                                  "query": query, "headers": headers, "actor": actor,
                                  "base": base, "csrf": csrf or "",
                                  "timeout_ms": int(timeout * 1000)}
        chunked = len(body) > cfg.tunnel_inline_body_bytes
        if chunked:
            fields.update(body_len=len(body), chunked=True)
        else:
            fields["body_b64"] = base64.b64encode(body).decode("ascii")
        try:
            await self._send(session, protocol.envelope(protocol.TUNNEL_REQUEST, **fields))
            if chunked:
                size = cfg.tunnel_chunk_bytes
                parts = [body[i:i + size] for i in range(0, len(body), size)]
                for index, part in enumerate(parts):
                    await self._send_bytes(session, protocol.pack_body_chunk(
                        stream, index, part, last=index == len(parts) - 1))
            return await asyncio.wait_for(asyncio.shield(call.future), timeout + 2.0)
        except asyncio.TimeoutError:
            session.tunnels.pop(stream, None)
            await self._cancel_stream(session, stream)
            return _error_reply("timeout", "the device did not answer in time")
        except asyncio.CancelledError:
            session.tunnels.pop(stream, None)
            await self._cancel_stream(session, stream)
            raise
        except Exception as exc:
            session.tunnels.pop(stream, None)
            if not _is_closed(exc):
                log.error("tunnel to %s failed: %s", device_id, exc)
            return _error_reply("device_offline", "the device went offline")

    async def _cancel_stream(self, session: DeviceSession, stream: int) -> None:
        try:
            await self._send(session, protocol.envelope(protocol.TUNNEL_CANCEL, stream=stream))
        except Exception:
            pass

    # ── profile, token, unpair ────────────────────────────────────────────────
    async def _push_profile_now(self, session: DeviceSession) -> int:
        built = await asyncio.to_thread(self._build_profile, session.device_id)
        if built is None:
            return 0
        version, profile = built
        await self._send(session, protocol.envelope(
            protocol.PROFILE_PUSH, version=version, sha256=protocol.sha256_hex(profile),
            profile=profile))
        return version

    def _build_profile(self, device_id: str) -> tuple[int, dict] | None:
        from sentrix_portal.services import accounts
        with self.db.session() as db:
            device = db.get(Device, device_id)
            if device is None or not device.household_id:
                return None
            household = db.get(Household, device.household_id)
            version = int(household.profile_version or 0) if household else 0
            if version <= 0:
                return None
            try:
                profile = accounts.build_profile(db, device.household_id, device_id)
            except NotImplementedError:
                log.warning("profile for %s not pushed: accounts.build_profile is missing",
                            device_id)
                return None
            except Exception as exc:
                log.error("profile for %s could not be built: %s", device_id, exc)
                return None
        profile = dict(profile)
        profile["version"] = version
        return version, profile

    def _mint_token(self, device_id: str) -> str:
        from sentrix_portal import security
        token = security.new_token(32)
        with self.db.session() as db:
            device = db.get(Device, device_id)
            device.token_hash = security.token_digest(token)
            device.token_rotated_at = utcnow()
            hello = dict(device.last_hello or {})
            hello["token_confirmed"] = False
            device.last_hello = hello
            security.audit(db, "device.token_rotated", actor_type="system",
                           household_id=device.household_id, target_type="device",
                           target_id=device.id, detail={"reason": "redelivery"})
        return token

    # ── relays ────────────────────────────────────────────────────────────────
    def viewer_count(self, device_id: str) -> int:
        viewers = (self.state_viewers.get(device_id, set())
                   | self.video_viewers.get(device_id, set()))
        return len({v.key for v in viewers})

    async def _send_viewer_count(self, device_id: str) -> None:
        session = self.sessions.get(device_id)
        if session is not None and session.full and not session.closed:
            try:
                await self._send(session, protocol.envelope(
                    protocol.VIEWERS, count=self.viewer_count(device_id)))
            except Exception:
                pass

    async def add_state_viewer(self, device_id: str, viewer: Viewer) -> None:
        viewers = self.state_viewers.setdefault(device_id, set())
        first = not viewers
        viewers.add(viewer)
        session = self.sessions.get(device_id)
        if session is not None and session.state_payload is not None:
            viewer.offer(self._decorate(session, session.state_payload))
        if first:
            await self._subscribe_state(device_id)
        self._ensure_renewer(device_id)
        await self._send_viewer_count(device_id)

    async def remove_state_viewer(self, device_id: str, viewer: Viewer) -> None:
        viewers = self.state_viewers.get(device_id, set())
        viewers.discard(viewer)
        if not viewers:
            self.state_viewers.pop(device_id, None)
            session = self.sessions.get(device_id)
            if session is not None and session.state_sub and not session.closed:
                try:
                    await self._send(session, protocol.envelope(protocol.STATE_UNSUBSCRIBE,
                                                                sub=session.state_sub))
                except Exception:
                    pass
                session.state_sub = None
        await self._send_viewer_count(device_id)

    async def add_video_viewer(self, device_id: str, viewer: Viewer, *,
                               wait: float = 2.0) -> dict | None:
        """Register a video viewer; returns the device's refusal (``{code,
        message}``) when it refuses the subscription, else None."""
        viewers = self.video_viewers.setdefault(device_id, set())
        viewers.add(viewer)
        stop = self._idle_stops.pop(device_id, None)
        if stop is not None:
            stop.cancel()
        session = self.sessions.get(device_id)
        refusal = None
        if session is not None and session.full:
            if session.video_sub is None or viewer.role == "operator":
                ref = await self._subscribe_video(device_id, viewer.actor)
                refusal = await self._wait_video(session, ref, wait)
            if refusal is None and session.frame is not None:
                viewer.offer(session.frame)
        self._ensure_renewer(device_id)
        await self._send_viewer_count(device_id)
        if refusal is not None:
            await self.remove_video_viewer(device_id, viewer, idle_stop=False)
        return refusal

    async def _wait_video(self, session: DeviceSession, ref: str | None,
                          wait: float) -> dict | None:
        if ref is None:
            return None
        deadline = time.monotonic() + wait
        seq = session.frame[0] if session.frame else None
        while time.monotonic() < deadline:
            refusal = session.video_errors.get(ref)
            if refusal:
                session.video_errors.pop(ref, None)
                if session.video_sub and not self.video_viewers.get(session.device_id):
                    session.video_sub = None
                return refusal
            if session.frame is not None and session.frame[0] != seq:
                break
            await asyncio.sleep(0.05)
        session.video_errors.pop(ref, None)
        return None

    async def remove_video_viewer(self, device_id: str, viewer: Viewer, *,
                                  idle_stop: bool = True) -> None:
        viewers = self.video_viewers.get(device_id, set())
        viewers.discard(viewer)
        if not viewers:
            self.video_viewers.pop(device_id, None)
            if idle_stop:
                self._idle_stops[device_id] = asyncio.ensure_future(self._idle_stop(device_id))
            else:
                await self._unsubscribe_video(device_id)
        await self._send_viewer_count(device_id)

    async def _idle_stop(self, device_id: str) -> None:
        await asyncio.sleep(_cfg().link.video_idle_stop_seconds)
        self._idle_stops.pop(device_id, None)
        if not self.video_viewers.get(device_id):
            await self._unsubscribe_video(device_id)

    async def _unsubscribe_video(self, device_id: str) -> None:
        session = self.sessions.get(device_id)
        if session is not None and session.video_sub and not session.closed:
            try:
                await self._send(session, protocol.envelope(protocol.VIDEO_UNSUBSCRIBE,
                                                            sub=session.video_sub))
            except Exception:
                pass
            session.video_sub = None

    async def _subscribe_state(self, device_id: str) -> None:
        session = self.sessions.get(device_id)
        if session is None or not session.full or session.closed:
            return
        session.state_sub = session.state_sub or f"state-{uuid.uuid4().hex[:12]}"
        cfg = _cfg().link
        await self._send(session, protocol.envelope(
            protocol.STATE_SUBSCRIBE, sub=session.state_sub, hz=cfg.state_hz_subscribed,
            lease_s=cfg.subscription_lease_seconds))

    async def _subscribe_video(self, device_id: str, actor: dict) -> str | None:
        session = self.sessions.get(device_id)
        if session is None or not session.full or session.closed:
            return None
        session.video_sub = session.video_sub or f"video-{uuid.uuid4().hex[:12]}"
        cfg = _cfg().link
        message = protocol.envelope(protocol.VIDEO_SUBSCRIBE, sub=session.video_sub,
                                    max_fps=cfg.video_max_fps,
                                    lease_s=cfg.subscription_lease_seconds, actor=actor)
        session.video_errors[message["id"]] = {}
        await self._send(session, message)
        return message["id"]

    async def _resubscribe(self, session: DeviceSession) -> None:
        device_id = session.device_id
        if self.state_viewers.get(device_id):
            await self._subscribe_state(device_id)
        viewers = self.video_viewers.get(device_id)
        if viewers:
            await self._subscribe_video(device_id, next(iter(viewers)).actor)

    def _ensure_renewer(self, device_id: str) -> None:
        task = self._renewers.get(device_id)
        if task is None or task.done():
            self._renewers[device_id] = asyncio.ensure_future(self._renew(device_id))

    async def _renew(self, device_id: str) -> None:
        """Keep the device's subscription leases alive while viewers watch."""
        try:
            while self.state_viewers.get(device_id) or self.video_viewers.get(device_id):
                await asyncio.sleep(RENEW_EVERY)
                if self.state_viewers.get(device_id):
                    await self._subscribe_state(device_id)
                viewers = self.video_viewers.get(device_id)
                if viewers:
                    await self._subscribe_video(device_id, next(iter(viewers)).actor)
        except Exception as exc:
            if not _is_closed(exc):
                log.error("subscription renewal for %s failed: %s", device_id, exc)
        finally:
            self._renewers.pop(device_id, None)

    def latest_state(self, device_id: str) -> dict | None:
        """The last state payload; while the device is offline it is
        overridden: ``camera_online=false``, ``ai_hz=0`` and the Link dot red
        (``link.connected=false``)."""
        session = self.sessions.get(device_id)
        if session is not None and session.full and not session.closed:
            return (self._decorate(session, session.state_payload)
                    if session.state_payload else None)
        return offline_state(self.last_state.get(device_id))

    # ── watcher ───────────────────────────────────────────────────────────────
    async def check_heartbeats(self, now: datetime | None = None) -> list[str]:
        """Devices silent for ``heartbeat_lost_seconds`` go offline (audit,
        cloud SOS authority, tamper signal). Returns their ids."""
        limit = float(_cfg().sos.heartbeat_lost_seconds)
        for device_id, session in list(self.sessions.items()):
            if time.monotonic() - session.last_seen > limit and not session.closed:
                session.closed = True
                await session.conn.close(protocol.CLOSE_GOING_AWAY, "heartbeat lost")
        lost, followups = await asyncio.to_thread(self._mark_offline, now or utcnow(), limit)
        for device_id, name, args in followups:
            self._spawn(self.send_command(device_id, name, args, _cloud_actor()))
        return lost

    def _mark_offline(self, now: datetime, limit: float) -> tuple[list[str], list]:
        from sentrix_portal import security
        from sentrix_portal.services import sos as sos_service
        lost: list[str] = []
        with self.db.session() as db:
            for device in db.query(Device).filter(Device.online.is_(True)).all():
                if device.id in self.sessions and not self.sessions[device.id].closed:
                    continue
                seen = _aware(device.last_seen_at)
                if seen is not None and (now - seen).total_seconds() < limit:
                    continue
                device.online = False
                lost.append(device.id)
                security.audit(db, "device.offline", actor_type="system",
                               household_id=device.household_id, target_type="device",
                               target_id=device.id,
                               detail={"last_seen_at": seen.isoformat() if seen else None})
                sos_service.on_device_offline(db, device, now=now)
        return lost, []


# ── module API (frozen signatures) ────────────────────────────────────────────
_current: LinkHub | None = None


def hub_for(app: Any) -> LinkHub:
    """The app's hub (made on first use); it becomes the current one."""
    global _current
    hub = getattr(app.state, "link_hub", None)
    if hub is None:
        hub = LinkHub(app)
        app.state.link_hub = hub
    _current = hub
    return hub


def current_hub() -> LinkHub | None:
    return _current


def reset() -> None:
    """Forget the current hub (tests)."""
    global _current
    _current = None


def is_online(device_id: str) -> bool:
    hub = _current
    return bool(hub is not None and hub.online(device_id))


def _command_result(ok: bool, code: str, message: str, *, result: Any = None,
                    status: int | None = None, queued: bool = False,
                    command_id: str | None = None) -> dict[str, Any]:
    if status is None:
        status = 200 if ok else (503 if code == "device_offline"
                                 else protocol.ERROR_CODES.get(code, 400))
    return {"ok": ok, "code": code, "status": int(status), "message": message,
            "result": result, "queued": queued, "command_id": command_id}


def _offline(message: str, *, queued: bool = False) -> dict[str, Any]:
    return _command_result(False, "device_offline", message, queued=queued)


def _error_reply(code: str, message: str) -> TunnelReply:
    status = 503 if code == "device_offline" else protocol.ERROR_CODES.get(code, 502)
    return TunnelReply(status, [["content-type", "application/json"],
                                ["cache-control", "no-store"]],
                       json.dumps({"detail": message, "code": code}).encode(), 0, code)


def offline_state(payload: dict | None) -> dict:
    """A state payload for an offline device (docs/7 §5.7 relays)."""
    out = dict(payload or {})
    link = dict(out.get("link") or {})
    link.update(connected=False, state="offline")
    out.update(camera_online=False, camera_available=False, ai_hz=0.0, fps=0.0,
               video_fps=0.0, link=link, link_online=False, device_offline=True)
    return out


def cloud_actor() -> dict:
    """The actor of the Portal's own automated commands."""
    from sentrix.actor import Actor
    return Actor.cloud().to_dict()


def _cloud_actor() -> dict:
    from sentrix.actor import Actor
    return Actor.cloud().to_dict()


_CLOSED_NAMES = frozenset({"WebSocketDisconnect", "ConnectionClosed", "ConnectionClosedOK",
                           "ConnectionClosedError", "LinkClosed", "ClosedResourceError"})


def _is_closed(exc: BaseException) -> bool:
    """A normal end of a connection (not worth an error in the log)."""
    if any(cls.__name__ in _CLOSED_NAMES for cls in type(exc).__mro__):
        return True
    return isinstance(exc, (ConnectionError, RuntimeError)) and "close" in str(exc).lower()


async def send_command(device_id: str, name: str, args: dict, actor: dict, *,
                       timeout: float = 10.0, queue_if_offline: bool = True) -> dict[str, Any]:
    """``actor`` is ``sentrix.actor.Actor.to_dict()`` of the Portal user."""
    hub = _current
    if hub is None:
        return _offline("the device is offline")
    return await hub.send_command(device_id, name, args, actor, timeout=timeout,
                                  queue_if_offline=queue_if_offline)


async def push_profile(db, device_id: str) -> int:
    """Push the household's profile at ``households.profile_version`` (0 when
    the device is offline: it goes out after the next welcome)."""
    hub = _current
    if hub is None or not hub.online(device_id):
        return 0
    return await hub._push_profile_now(hub.sessions[device_id])


async def issue_token(db, device_id: str) -> None:
    """Mint the device token (only its digest is stored; ``db`` is committed so
    the device's reconnect finds it) and send ``token.issue`` now, or right
    after the device's next bootstrap hello."""
    from sentrix_portal import security
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(f"unknown device {device_id}")
    token = security.new_token(32)
    device.token_hash = security.token_digest(token)
    device.token_rotated_at = utcnow()
    hello = dict(device.last_hello or {})
    hello["token_confirmed"] = False
    device.last_hello = hello
    security.audit(db, "device.token_rotated", actor_type="system",
                   household_id=device.household_id, target_type="device",
                   target_id=device.id, detail={"reason": "issued"})
    db.commit()
    hub = _current
    session = hub.sessions.get(device_id) if hub is not None else None
    if session is not None and not session.closed:
        await hub._send(session, protocol.envelope(protocol.TOKEN_ISSUE, token=token,
                                                   issued_at=utcnow().isoformat()))
    elif hub is not None:
        hub.pending_tokens[device_id] = token


async def unpair(db, device_id: str, actor: dict) -> dict[str, Any]:
    """``unpair`` command (the device wipes token and profile), token revoked,
    socket closed with 4003, ``device.unpaired`` audit. A device that is
    online and refuses (its own owner + step-up rule) stays paired."""
    from sentrix_portal import security
    device = db.get(Device, device_id)
    if device is None:
        return _command_result(False, "not_found", "unknown device")
    hub = _current
    result = None
    if hub is not None and hub.online(device_id):
        result = await hub.send_command(device_id, "unpair", {}, actor, timeout=10,
                                        queue_if_offline=False)
        if not result["ok"] and result["code"] != "device_offline":
            return result
    device.token_hash = None
    device.token_rotated_at = utcnow()
    device.state = "ASSIGNED" if device.household_id else "UNASSIGNED"
    device.claimed_at = None
    hello = dict(device.last_hello or {})
    hello["token_confirmed"] = False
    device.last_hello = hello
    security.audit(db, "device.unpaired", actor_type="user",
                   actor_id=_text((actor or {}).get("id"), 64),
                   household_id=device.household_id, target_type="device",
                   target_id=device.id,
                   detail={"device_confirmed": bool(result and result["ok"])})
    db.commit()
    if hub is not None:
        session = hub.sessions.pop(device_id, None)
        if session is not None:
            session.closed = True
            await session.conn.close(protocol.CLOSE_REVOKED, "unpaired")
    return _command_result(True, "ok", "unpaired")


def start(app):
    """Background: the heartbeat watcher (``heartbeat_lost_seconds``)."""
    hub = hub_for(app)

    async def watch() -> None:
        while True:
            try:
                await hub.check_heartbeats()
            except Exception as exc:
                log.error("heartbeat watcher: %s", exc, exc_info=True)
            await asyncio.sleep(WATCH_EVERY)

    task = asyncio.ensure_future(watch())

    async def stop() -> None:
        task.cancel()
        for session in list(hub.sessions.values()):
            try:
                await session.conn.close(protocol.CLOSE_GOING_AWAY, "portal stopping")
            except Exception:
                pass
    return stop


__all__ = ["is_online", "send_command", "push_profile", "issue_token", "unpair", "start",
           "LinkHub", "DeviceSession", "Viewer", "TunnelReply", "hub_for", "current_hub",
           "reset", "offline_state", "UUID_RE"]
