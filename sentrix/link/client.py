"""The device's link to the Portal: one outbound WebSocket (docs/7 §5).

``build_service`` returns the ``link`` service when ``SENTRIX_LINK_ENABLED=1``
(off by default; replays, benchmarks and the evaluation harness assert it is
not running). The service runs on the LC's event loop and:

* dials ``wss://<portal>/device/v1/link`` (``ws://`` for an http:// dev
  Portal) with ``Authorization: Bearer <device token>``, or ``Bootstrap
  <device_id>:<secret>`` before the claim; reconnects with full-jitter
  exponential back-off (1 s .. 60 s, reset after 60 s connected; the maximum
  after a 4001); on 4003 it wipes the token and the profile cache;
* says ``hello`` (identity, outbox summary, the redacted ``/api/health``
  fields) and sends a ``heartbeat`` every 10 s; a Portal silent for 30 s is
  treated as lost (the Portal acks every heartbeat);
* pumps the persistent outbox (``link_outbox``, at-least-once): pending rows
  in (priority, seq) order, at most 64 unacknowledged, re-sent after every
  reconnect until acked;
* pushes ``state`` (2 Hz while subscribed, else every 10 s) and, while a video
  subscription is leased, FRAME messages from ``pipeline.latest_jpeg`` at the
  stream's quality (the once-per-frame encode is shared with the LC's MJPEG),
  adaptively up to 15 fps from the measured uplink;
* serves ``tunnel.request`` through :class:`~sentrix.link.tunnel.TunnelExecutor`
  and ``command`` through :class:`~sentrix.link.commands.CommandHandler`,
  stores ``profile.push`` (encrypted, ``sentrix.link.profile``), keeps the
  ``token.issue`` token, applies ``sos.sync`` and ``viewers``;
* turns the change listeners (incident, dispatch, verdict, evidence, snapshot,
  gallery audit, mode, channel status) into outbox events, and batches
  ``remote_audit`` rows into ``remote.audit`` events.

Transport is behind a two-method connection (``send_text``/``send_bytes``,
``receive``, ``close``) so tests drive the same session code over an
in-memory pipe; the default connector uses the ``websockets`` client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from sentrix.link import protocol
from sentrix.utils.logging import get_logger
from sentrix.utils.redact import mask_recipient, redact_url

log = get_logger("link.client")

IDENTITY_FILE = "identity.json"
TOKEN_FILE = "token.json"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
#: Capabilities this build offers in ``hello``.
CAPABILITIES = ("tunnel", "video", "state", "sms", "siren")
#: Gallery audit events mirrored as ``gallery.audit`` (no images, ever).
GALLERY_EVENTS = frozenset({"gallery_enrolled", "gallery_removed", "gallery_rejected"})
#: How often the pump looks for new outbox rows (s); producers on other
#: threads also wake it.
OUTBOX_POLL_SECONDS = 0.25
#: Three protocol errors in this window close the socket (1008).
ERROR_WINDOW_SECONDS = 10.0
#: Prune acknowledged outbox rows this often (s).
PRUNE_EVERY_SECONDS = 3600.0
#: Close code this module uses when it replaces its own connection.
CLOSE_POLICY = 1008


# ── identity ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DeviceIdentity:
    """``<identity_dir>/identity.json`` (written by ``tools/provision_device.py``)."""

    device_id: str
    serial: str
    bootstrap_secret: str
    setup_code_digest: str | None = None
    provisioned_at: str | None = None

    @classmethod
    def load(cls, directory: Path) -> "DeviceIdentity | None":
        try:
            data = json.loads((directory / IDENTITY_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.error("unreadable %s: %s", directory / IDENTITY_FILE, exc)
            return None
        device_id = str(data.get("device_id") or "").lower()
        if not _UUID.match(device_id) or not data.get("bootstrap_secret"):
            log.error("%s needs a UUID4 device_id and a bootstrap_secret",
                      directory / IDENTITY_FILE)
            return None
        return cls(device_id=device_id, serial=str(data.get("serial") or ""),
                   bootstrap_secret=str(data["bootstrap_secret"]),
                   setup_code_digest=data.get("setup_code_digest"),
                   provisioned_at=data.get("provisioned_at"))


def _write_private(path: Path, data: str) -> None:
    """Atomic replace, mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(data)
    os.replace(temp, path)


def load_token(directory: Path) -> str | None:
    try:
        token = json.loads((directory / TOKEN_FILE).read_text(encoding="utf-8")).get("token")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.error("unreadable %s: %s", directory / TOKEN_FILE, exc)
        return None
    return str(token) if token else None


def save_token(directory: Path, token: str, issued_at: Any = None) -> None:
    _write_private(directory / TOKEN_FILE,
                   json.dumps({"token": token, "issued_at": issued_at}))


def wipe_token(directory: Path) -> bool:
    try:
        (directory / TOKEN_FILE).unlink()
        return True
    except FileNotFoundError:
        return False


def link_url(portal_url: str) -> str | None:
    """``https://host`` -> ``wss://host/device/v1/link`` (``http`` -> ``ws``)."""
    url = (portal_url or "").strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + protocol.PATH
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):] + protocol.PATH
    if url.startswith(("wss://", "ws://")):
        return url if url.endswith(protocol.PATH) else url + protocol.PATH
    return None


def firmware() -> str:
    """The git short hash of this build (``SENTRIX_FIRMWARE`` overrides)."""
    explicit = os.getenv("SENTRIX_FIRMWARE", "").strip()
    if explicit:
        return explicit[:40]
    from sentrix.paths import ROOT_DIR
    git = ROOT_DIR / ".git"
    try:
        if git.is_file():                       # a worktree: "gitdir: <path>"
            git = Path(git.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head.split(" ", 1)[1].strip()
        for root in (git, git.parent.parent if git.parent.name == "worktrees" else git):
            ref_file = root / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip()[:7]
            packed = root / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line[:7]
    except (OSError, IndexError):
        pass
    return "unknown"


# ── transport ─────────────────────────────────────────────────────────────────
class LinkClosed(Exception):
    """The connection ended; ``code`` is the WebSocket close code (or None)."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        super().__init__(f"link closed ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


class WebsocketsConnection:
    """The ``websockets`` asyncio client behind the session's interface."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._send(data)

    async def _send(self, data: str | bytes) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            await self._ws.send(data)
        except ConnectionClosed as exc:
            raise LinkClosed(_close_code(exc), "send failed") from exc

    async def receive(self) -> str | bytes:
        from websockets.exceptions import ConnectionClosed
        try:
            return await self._ws.recv()
        except ConnectionClosed as exc:
            raise LinkClosed(_close_code(exc)) from exc

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code, reason)
        except Exception:
            pass


def _close_code(exc: Any) -> int | None:
    rcvd = getattr(exc, "rcvd", None)
    return getattr(rcvd, "code", None)


async def websockets_connect(url: str, headers: dict[str, str]) -> WebsocketsConnection:
    """The production connector (``websockets`` asyncio client)."""
    from websockets.asyncio.client import connect
    from sentrix import __version__
    ws = await connect(url, additional_headers=headers,
                       max_size=protocol.MAX_MESSAGE_BYTES, ping_interval=20,
                       ping_timeout=20, open_timeout=15, close_timeout=5,
                       user_agent_header=f"sentrix-device/{__version__}")
    return WebsocketsConnection(ws)


Connector = Callable[[str, dict], Awaitable[Any]]


# ── adaptive remote video ─────────────────────────────────────────────────────
class AdaptiveRate:
    """Remote video rate from the measured uplink (docs/7 §5.7: 2..15 fps).

    Each frame's send time (the socket applies back-pressure once its buffer
    fills) gives an uplink estimate (EWMA of bytes per second of sending);
    the rate is the fps that spends at most ``budget`` of the uplink on video,
    clamped to ``[min_fps, max_fps]`` and to what the viewer asked for.
    """

    def __init__(self, min_fps: float = 2.0, max_fps: float = 15.0, *,
                 budget: float = 0.8, alpha: float = 0.3) -> None:
        self.min_fps = float(min_fps)
        self.max_fps = float(max_fps)
        self.budget = float(budget)
        self.alpha = float(alpha)
        self.uplink_bps: float | None = None
        self.frame_bytes: float | None = None
        self.fps = float(max_fps)

    def on_sent(self, nbytes: int, seconds: float) -> float:
        seconds = max(seconds, 1e-4)
        bps = nbytes / seconds
        a = self.alpha
        self.uplink_bps = bps if self.uplink_bps is None else (1 - a) * self.uplink_bps + a * bps
        self.frame_bytes = (nbytes if self.frame_bytes is None
                            else (1 - a) * self.frame_bytes + a * nbytes)
        affordable = self.budget * self.uplink_bps / max(self.frame_bytes, 1.0)
        self.fps = max(self.min_fps, min(self.max_fps, affordable))
        return self.fps

    def interval(self, requested_fps: float | None = None) -> float:
        fps = self.fps if requested_fps is None else min(self.fps, max(requested_fps,
                                                                       self.min_fps))
        return 1.0 / max(fps, self.min_fps)


# ── per-connection state ──────────────────────────────────────────────────────
class _Session:
    def __init__(self, conn: Any, auth: str) -> None:
        self.conn = conn
        self.auth = auth                         # bearer | bootstrap
        self.welcomed = asyncio.Event()
        self.claimed = False
        #: A claimed device on its own token: outbox, state, video, commands and
        #: the tunnel. A bootstrap session only says hello and heartbeats.
        self.full = False
        self.heartbeat_seconds: float | None = None
        self.in_flight: set[int] = set()
        self.state_subs: dict[str, tuple[float, float]] = {}    # sub -> (expires, hz)
        self.video_subs: dict[str, tuple[float, float]] = {}    # sub -> (expires, max_fps)
        self.uploads: dict[int, dict] = {}       # stream -> {message, chunks, size}
        self.streams: dict[int, asyncio.Task] = {}
        self.errors: list[float] = []
        self.state_n = 0
        self.last_message = time.monotonic()
        self.send_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.close_code: int | None = None
        self.reconnect_now = False
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


# ── the service ───────────────────────────────────────────────────────────────
class LinkService:
    """The ``link`` service (docs/7 §3.6). ``start``/``stop`` return at once;
    the session runs on ``loop`` (the LC's event loop in the live app, or a
    private loop thread when none is given)."""

    name = "link"

    def __init__(self, ctx: Any, *, connector: Connector | None = None,
                 identity_dir: Path | None = None, cfg: Any = None,
                 random_fn: Callable[[float, float], float] = random.uniform) -> None:
        if cfg is None:
            cfg = ctx.settings.link
        if identity_dir is None:
            from sentrix.link import profile
            identity_dir = profile.identity_dir()
        self.ctx = ctx
        self.cfg = cfg
        self.identity_dir = Path(identity_dir)
        self.url = link_url(cfg.portal_url)
        self.connector = connector or websockets_connect
        self._random = random_fn
        self._loop: asyncio.AbstractEventLoop | None = (ctx.extras or {}).get("loop")
        self._own_loop_thread: threading.Thread | None = None
        self._future = None
        self._main: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._kick: asyncio.Event | None = None
        self._session: _Session | None = None
        self._started_at: float | None = None
        self._executor = None
        self._policy = None
        self._commands = None
        self._force_bootstrap = False
        self._audit_cursor: int | None = None
        self._last_prune = 0.0
        self._listeners: list[tuple[str, Callable]] = []
        self._channels_listener = None
        self.identity: DeviceIdentity | None = None
        self.reconnects = 0
        self.last_close_code: int | None = None
        self._status: dict[str, Any] = {}
        self._depth = 0
        self._set_state("stopped")

    # ── Service protocol ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._future is not None:
            return
        self._started_at = time.monotonic()
        self._register_listeners()
        loop = self._loop
        if loop is None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._own_loop_thread = threading.Thread(target=loop.run_forever, daemon=True,
                                                     name="sentrix-link")
            self._own_loop_thread.start()
        self._future = asyncio.run_coroutine_threadsafe(self._run(), loop)

    def stop(self) -> None:
        future, self._future = self._future, None
        self._unregister_listeners()
        if future is None:
            return
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._request_stop)
            on_loop = _running_loop() is loop
            if not on_loop:
                try:
                    future.result(timeout=5.0)
                except Exception:
                    future.cancel()
            if self._own_loop_thread is not None:
                loop.call_soon_threadsafe(loop.stop)
                self._own_loop_thread.join(timeout=5.0)
                self._own_loop_thread = None
        self._flush_remote_audit()
        self._set_remote_viewers(0)
        self._set_state("stopped")

    def health(self) -> dict[str, Any]:
        status = dict(self._status)
        status.update(reconnects=self.reconnects, last_close_code=self.last_close_code,
                      uptime_s=round(self.uptime(), 1))
        if self._executor is not None:
            status["tunnel"] = {"inflight": self._executor.inflight,
                                "served": self._executor.served,
                                "refused": self._executor.refused}
        return status

    # ── public helpers ────────────────────────────────────────────────────────
    def uptime(self) -> float:
        return 0.0 if self._started_at is None else time.monotonic() - self._started_at

    def is_online(self) -> bool:
        """Connected, welcomed and heard from within ``offline_after_seconds``
        (the SOS engine's offline ladder reads this)."""
        session = self._session
        return (session is not None and session.welcomed.is_set()
                and time.monotonic() - session.last_message < self.cfg.offline_after_seconds)

    def reconnect_now(self) -> bool:
        """Drop the current connection (if any) and dial again at once."""
        loop = self._loop
        if loop is None or self._future is None:
            return False
        loop.call_soon_threadsafe(self._kick_now)
        return True

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    # ── state block ───────────────────────────────────────────────────────────
    def _set_state(self, state_name: str, **fields: Any) -> None:
        """Update the ``state.link`` block (single writer: this service)."""
        from sentrix.pipeline.state import LINK_IDLE
        previous = self._status.get("state")
        block = {**LINK_IDLE, **self._status}
        block.update(enabled=True, state=state_name, connected=state_name == "connected",
                     device_id=self.identity.device_id if self.identity else None,
                     portal=redact_url(self.cfg.portal_url) or None,
                     outbox_depth=self._depth)
        if state_name != previous or not block.get("since"):
            block["since"] = datetime.now(timezone.utc).isoformat()
        block.update(fields)
        self._status = block
        self._push_status()

    def _push_status(self) -> None:
        try:
            self.ctx.state.update(link=dict(self._status))
        except Exception:                     # the state object is optional in tests
            pass

    def _set_remote_viewers(self, count: int) -> None:
        setter = getattr(self.ctx.state, "set_remote_viewers", None)
        if setter is not None:
            try:
                setter(count)
            except Exception:
                pass

    # ── listeners -> outbox ───────────────────────────────────────────────────
    def _register_listeners(self) -> None:
        from sentrix import listeners
        pairs = (("incident", self._on_incident), ("dispatch", self._on_dispatch),
                 ("verdict", self._on_verdict), ("evidence", self._on_evidence),
                 ("snapshot", self._on_snapshot), ("audit", self._on_audit),
                 ("mode", self._on_mode))
        for kind, callback in pairs:
            listeners.add_listener(kind, callback)
            self._listeners.append((kind, callback))
        registry = getattr(self.ctx, "registry", None)
        channels = registry.get("channels") if registry is not None else None
        if channels is not None and hasattr(channels, "add_listener"):
            channels.add_listener(self._on_notification)
            self._channels_listener = channels

    def _unregister_listeners(self) -> None:
        from sentrix import listeners
        for kind, callback in self._listeners:
            listeners.remove_listener(kind, callback)
        self._listeners.clear()
        if self._channels_listener is not None:
            try:
                self._channels_listener.remove_listener(self._on_notification)
            except Exception:
                pass
            self._channels_listener = None

    def _publish(self, kind: str, payload: dict, dedupe_key: str | None = None) -> None:
        from sentrix import link
        if link.publish(kind, payload, dedupe_key=dedupe_key) is not None:
            self._wake_pump()

    def _wake_pump(self) -> None:
        loop, session = self._loop, self._session
        if loop is not None and session is not None and not loop.is_closed():
            loop.call_soon_threadsafe(session.wake.set)

    def _on_incident(self, payload: dict) -> None:
        self._publish("incident.upsert", payload)

    def _on_dispatch(self, payload: dict) -> None:
        self._publish("dispatch.upsert", payload)

    def _on_verdict(self, payload: dict) -> None:
        self._publish("verdict.set", payload)

    def _on_evidence(self, payload: dict) -> None:
        bundle = payload.get("bundle_id")
        self._publish("evidence.meta", payload,
                      dedupe_key=f"evidence:{bundle}" if bundle else None)

    def _on_snapshot(self, payload: dict) -> None:
        name = payload.get("name")
        self._publish("snapshot.meta", {k: payload.get(k) for k in
                                        ("name", "incident_id", "created_at")},
                      dedupe_key=f"snapshot:{name}" if name else None)

    def _on_audit(self, payload: dict) -> None:
        if payload.get("event") not in GALLERY_EVENTS:
            return
        self._publish("gallery.audit", {k: payload.get(k) for k in
                                        ("event", "person", "file", "via", "by", "reason")})

    def _on_mode(self, payload: dict) -> None:
        self._publish("mode.changed", {k: payload.get(k) for k in
                                       ("armed", "by", "at", "source")})

    def _on_notification(self, item: Any) -> None:
        recipient, message = item.recipient, item.message
        self._publish("notification.status", {
            "id": item.id, "request_id": message.request_id, "channel": recipient.channel,
            "recipient_ref": recipient.ref, "recipient_masked": mask_recipient(recipient.address),
            "step": message.step, "status": item.status, "error": item.error,
            "attempts": item.attempts, "sent_at": item.sent_at})

    def _flush_remote_audit(self) -> int:
        """Batch new ``remote_audit`` rows into one ``remote.audit`` event."""
        from sentrix.storage import link_store
        try:
            if self._audit_cursor is None:
                last = link_store.last_payload("remote.audit")
                self._audit_cursor = (int(last.get("to_id") or 0) if last
                                      else link_store.last_remote_id())
                return 0
            rows = link_store.remote_since(self._audit_cursor, 200)
            if not rows:
                return 0
            first, last_id = rows[0]["id"], rows[-1]["id"]
            from sentrix import link
            link.publish("remote.audit", {"rows": rows, "from_id": first, "to_id": last_id},
                         dedupe_key=f"remote.audit:{last_id}")
            self._audit_cursor = last_id
            return len(rows)
        except Exception as exc:
            log.error("remote audit batch failed: %s", exc)
            return 0

    # ── main loop ─────────────────────────────────────────────────────────────
    def _request_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._kick is not None:
            self._kick.set()
        session = self._session
        if session is not None:
            asyncio.ensure_future(session.conn.close(protocol.CLOSE_GOING_AWAY, "device stopping"))

    def _kick_now(self) -> None:
        session = self._session
        if session is not None:
            session.reconnect_now = True
            asyncio.ensure_future(session.conn.close(protocol.CLOSE_NORMAL, "reconnect"))
        if self._kick is not None:
            self._kick.set()

    async def _run(self) -> None:
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()
        await asyncio.to_thread(self._flush_remote_audit)
        try:
            await self._dial_forever()
        finally:
            if self._executor is not None:
                await self._executor.close()
            self._session = None

    async def _dial_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not self.url:
                self._set_state("unconfigured")
                await self._sleep(60.0)
                continue
            self.identity = await asyncio.to_thread(DeviceIdentity.load, self.identity_dir)
            if self.identity is None:
                self._set_state("unprovisioned")
                await self._sleep(30.0)
                continue
            token = await asyncio.to_thread(load_token, self.identity_dir)
            use_bootstrap = token is None or self._force_bootstrap
            auth = ("bootstrap" if use_bootstrap else "bearer")
            header = (f"{protocol.AUTH_BOOTSTRAP} {self.identity.device_id}:"
                      f"{self.identity.bootstrap_secret}" if use_bootstrap
                      else f"{protocol.AUTH_BEARER} {token}")
            self._set_state("connecting")
            connected_at = None
            session = None
            try:
                conn = await self.connector(self.url, {"Authorization": header})
                connected_at = time.monotonic()
                session = _Session(conn, auth)
                self._session = session
                await self._serve(session)
            except asyncio.CancelledError:
                raise
            except LinkClosed as exc:
                if session is not None and session.close_code is None:
                    session.close_code = exc.code
            except Exception as exc:
                log.info("link: cannot reach the Portal: %s", exc)
            finally:
                self._session = None
                self._set_remote_viewers(0)
            code = session.close_code if session is not None else None
            self.last_close_code = code
            if self._stop.is_set():
                break
            self.reconnects += 1
            if connected_at is not None and time.monotonic() - connected_at >= 60.0:
                attempt = 0
            delay = await self._after_close(code, auth, session)
            if delay is None:
                delay = self._random(0.0, min(self.cfg.reconnect_max_seconds,
                                              self.cfg.reconnect_min_seconds * (2 ** attempt)))
                attempt += 1
            self._set_state("offline", next_attempt_s=round(delay, 2), last_close_code=code)
            await self._sleep(delay)

    async def _after_close(self, code: int | None, auth: str,
                           session: _Session | None) -> float | None:
        """The back-off a close code imposes (None = the normal jitter)."""
        if session is not None and session.reconnect_now:
            return 0.0
        if code == protocol.CLOSE_REVOKED:
            await asyncio.to_thread(self._wipe_credentials, "revoked by the Portal (4003)")
            self._force_bootstrap = False
            return None
        if code == protocol.CLOSE_UNAUTHORISED:
            # A rejected token may mean the device was unpaired while it was
            # offline: try the bootstrap secret next time (and vice versa).
            self._force_bootstrap = auth == "bearer"
            return float(self.cfg.reconnect_max_seconds)
        if code == protocol.CLOSE_UNSUPPORTED:
            return float(self.cfg.reconnect_max_seconds)
        return None

    def _wipe_credentials(self, why: str) -> None:
        from sentrix.link import profile
        wipe_token(self.identity_dir)
        removed = profile.wipe()
        log.warning("link: %s: token and %d cached profile(s) wiped", why, removed)

    async def _sleep(self, seconds: float) -> None:
        self._kick.clear()
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    # ── one connection ────────────────────────────────────────────────────────
    async def _serve(self, session: _Session) -> None:
        await self._send_json(session, await asyncio.to_thread(self._hello, session))
        workers = [session.spawn(self._receive_loop(session)),
                   session.spawn(self._heartbeat_loop(session)),
                   session.spawn(self._outbox_loop(session)),
                   session.spawn(self._state_loop(session)),
                   session.spawn(self._frame_loop(session)),
                   session.spawn(self._watchdog(session))]
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if isinstance(exc, LinkClosed) and session.close_code is None:
                    session.close_code = exc.code
                elif exc is not None and not isinstance(exc, LinkClosed):
                    log.error("link session task failed: %s", exc, exc_info=exc)
        finally:
            for task in list(session.tasks):
                task.cancel()
            await asyncio.gather(*session.tasks, return_exceptions=True)
            await session.conn.close(protocol.CLOSE_NORMAL, "")

    def _hello(self, session: _Session) -> dict[str, Any]:
        from sentrix import mode
        from sentrix.config import settings
        from sentrix.link import profile
        from sentrix.storage import link_store
        identity = self.identity
        try:
            summary = link_store.summary()
            self._depth = summary["depth"]
        except Exception as exc:
            log.error("outbox summary failed: %s", exc)
            summary = {"head_seq": None, "tail_seq": 0, "depth": 0, "epoch": None}
        return protocol.envelope(
            protocol.HELLO, device_id=identity.device_id, serial=identity.serial,
            firmware=firmware(), app_version=_app_version(),
            protocol=protocol.VERSION, session_id=_pipeline_session(),
            profile_version=_safe(profile.current_version, 0), outbox=summary,
            capabilities=list(CAPABILITIES), test_mode=bool(settings.test_mode),
            armed=_safe(mode.is_armed, False), health=self._health_payload(),
            time_ms=protocol.now_ms())

    @staticmethod
    def _health_payload() -> dict[str, Any]:
        """The redacted ``/api/health`` fields, only when the live pipeline
        exists (the link never builds one: that would open the camera)."""
        if _existing_pipeline() is None:
            return {}
        try:
            from sentrix.web.routes import health_payload
            return json.loads(json.dumps(health_payload(), default=str))
        except Exception as exc:
            log.debug("hello without health: %s", exc)
            return {}

    async def _send_json(self, session: _Session, message: dict[str, Any]) -> None:
        text = protocol.encode(message)
        async with session.send_lock:
            await session.conn.send_text(text)

    async def _send_bytes(self, session: _Session, data: bytes) -> None:
        async with session.send_lock:
            await session.conn.send_bytes(data)

    async def _receive_loop(self, session: _Session) -> None:
        while True:
            data = await session.conn.receive()
            session.last_message = time.monotonic()
            self._status["last_message_at"] = datetime.now(timezone.utc).isoformat()
            if isinstance(data, (bytes, bytearray)):
                await self._on_binary(session, bytes(data))
                continue
            try:
                message = protocol.decode(data)
            except protocol.ProtocolError as exc:
                if exc.code == "unsupported":
                    session.close_code = protocol.CLOSE_UNSUPPORTED
                    await session.conn.close(protocol.CLOSE_UNSUPPORTED, str(exc))
                    raise LinkClosed(protocol.CLOSE_UNSUPPORTED, str(exc))
                await self._protocol_error(session, exc.code, str(exc), None)
                continue
            await self._dispatch(session, message)

    async def _protocol_error(self, session: _Session, code: str, text: str,
                              ref: str | None) -> None:
        now = time.monotonic()
        session.errors = [t for t in session.errors if now - t < ERROR_WINDOW_SECONDS] + [now]
        await self._send_json(session, protocol.error(code, text, ref))
        if len(session.errors) >= 3:
            session.close_code = CLOSE_POLICY
            await session.conn.close(CLOSE_POLICY, "too many protocol errors")
            raise LinkClosed(CLOSE_POLICY, "too many protocol errors")

    async def _dispatch(self, session: _Session, message: dict[str, Any]) -> None:
        kind = message["type"]
        handler = {
            protocol.WELCOME: self._on_welcome, protocol.ACK: self._on_ack,
            protocol.PROFILE_PUSH: self._on_profile, protocol.COMMAND: self._on_command,
            protocol.TUNNEL_REQUEST: self._on_tunnel_request,
            protocol.TUNNEL_CANCEL: self._on_tunnel_cancel,
            protocol.VIDEO_SUBSCRIBE: self._on_video_subscribe,
            protocol.VIDEO_UNSUBSCRIBE: self._on_video_unsubscribe,
            protocol.STATE_SUBSCRIBE: self._on_state_subscribe,
            protocol.STATE_UNSUBSCRIBE: self._on_state_unsubscribe,
            protocol.VIEWERS: self._on_viewers, protocol.TOKEN_ISSUE: self._on_token,
            protocol.SOS_SYNC: self._on_sos_sync, protocol.ERROR: self._on_error,
        }.get(kind)
        if handler is None:
            await self._protocol_error(session, "unknown_type", f"unknown type {kind!r}",
                                       message.get("id"))
            return
        if kind != protocol.WELCOME and not session.welcomed.is_set():
            await self._protocol_error(session, "invalid_args", "welcome first",
                                       message.get("id"))
            return
        try:
            await handler(session, message)
        except (ValueError, TypeError, KeyError) as exc:
            await self._protocol_error(session, "invalid_args", str(exc), message.get("id"))

    # ── handlers ──────────────────────────────────────────────────────────────
    async def _on_welcome(self, session: _Session, message: dict) -> None:
        if message.get("device_id") not in (None, self.identity.device_id):
            raise ValueError("welcome for another device")
        session.claimed = bool(message.get("claimed"))
        session.full = session.claimed and session.auth == "bearer"
        if not session.claimed:
            # Unclaimed: a token or profile left from an earlier claim means
            # the device was unpaired while it was offline.
            if await asyncio.to_thread(load_token, self.identity_dir) is not None:
                await asyncio.to_thread(self._wipe_credentials, "the Portal says unclaimed")
        if session.auth == "bearer":
            self._force_bootstrap = False
        try:
            session.heartbeat_seconds = float(message.get("heartbeat_seconds")
                                              or self.cfg.heartbeat_seconds)
        except (TypeError, ValueError):
            session.heartbeat_seconds = self.cfg.heartbeat_seconds
        resend_from = message.get("resend_from_seq")
        if session.full and isinstance(resend_from, int) and resend_from > 0:
            from sentrix.storage import link_store
            await asyncio.to_thread(link_store.ack, (), resend_from)
        subs = message.get("subscriptions") or {}
        if session.full and subs.get("state"):
            session.state_subs["welcome"] = (time.monotonic()
                                             + self.cfg.subscription_lease_seconds,
                                             self.cfg.state_hz_subscribed)
        session.welcomed.set()
        session.wake.set()
        self._set_state("connected", claimed=session.claimed,
                        profile_version=_safe(_profile_version, None))
        log.info("link connected (%s, %s)", session.auth,
                 "claimed" if session.claimed else "unclaimed")

    async def _on_ack(self, session: _Session, message: dict) -> None:
        from sentrix.storage import link_store
        seqs = [int(s) for s in message.get("seqs") or [] if isinstance(s, int)]
        upto = message.get("upto")
        upto = int(upto) if isinstance(upto, int) and upto > 0 else None
        if seqs or upto is not None:
            await asyncio.to_thread(link_store.ack, seqs, upto)
        session.in_flight.difference_update(seqs)
        if upto is not None:
            session.in_flight = {s for s in session.in_flight if s > upto}
        session.wake.set()

    async def _on_profile(self, session: _Session, message: dict) -> None:
        from sentrix.link import profile
        if not session.full:
            raise ValueError("profile.push needs a claimed device on its token")
        version = int(message["version"])
        ack = await asyncio.to_thread(profile.store, message.get("profile"),
                                      version=version, sha256=message.get("sha256"))
        await self._send_json(session, protocol.envelope(protocol.PROFILE_ACK, **ack))
        if ack.get("ok") and not ack.get("error"):
            _pipeline_audit("profile_applied", version=version)
            self._set_state("connected", claimed=True, profile_version=version)

    async def _on_command(self, session: _Session, message: dict) -> None:
        if not session.full:
            raise ValueError("commands need a claimed device on its token")
        handler = self._command_handler()
        result = await asyncio.to_thread(handler.handle, message)
        await self._send_json(session, protocol.envelope(
            protocol.COMMAND_RESULT, ref=message.get("id"), **result))

    def _command_handler(self):
        if self._commands is None:
            from sentrix.link.commands import CommandHandler, CommandHooks
            registry = getattr(self.ctx, "registry", None)
            self._commands = CommandHandler(CommandHooks(
                get_pipeline=self.ctx.get_pipeline,
                channels=lambda: registry.get("channels") if registry is not None else None,
                wipe_token=lambda: wipe_token(self.identity_dir),
                uptime=self.uptime))
        return self._commands

    def _get_policy(self):
        if self._policy is None:
            from sentrix.link.policy import TunnelPolicy
            self._policy = TunnelPolicy(media_per_minute=self.cfg.media_requests_per_minute)
        return self._policy

    def _tunnel(self):
        if self._executor is None:
            app = (self.ctx.extras or {}).get("app")
            if app is None:
                return None
            from sentrix.link.tunnel import TunnelExecutor
            self._executor = TunnelExecutor(app, cfg=self.cfg, policy=self._get_policy())
        return self._executor

    async def _on_tunnel_request(self, session: _Session, message: dict) -> None:
        if not session.full:
            raise ValueError("the tunnel needs a claimed device on its token")
        stream = int(message["stream"])
        if stream in session.streams or stream in session.uploads:
            raise ValueError(f"stream {stream} is already open")
        if message.get("chunked"):
            size = int(message.get("body_len") or 0)
            if size > self.cfg.tunnel_max_request_bytes:
                from sentrix.link.tunnel import error_result
                await self._respond_tunnel(session, stream,
                                           error_result("too_large", "request body too large"))
                return
            session.uploads[stream] = {"message": message, "chunks": [], "size": 0,
                                       "expected": size}
            return
        session.streams[stream] = session.spawn(self._run_tunnel(session, stream, message, None))

    async def _run_tunnel(self, session: _Session, stream: int, message: dict,
                          body: bytes | None) -> None:
        from sentrix.link.tunnel import error_result
        try:
            executor = self._tunnel()
            if executor is None:
                result = error_result("unsupported", "this device has no console to tunnel")
            else:
                result = await executor.execute(message, body)
            await self._respond_tunnel(session, stream, result)
        except asyncio.CancelledError:
            pass
        finally:
            session.streams.pop(stream, None)

    async def _respond_tunnel(self, session: _Session, stream: int, result) -> None:
        body = result.body or b""
        fields: dict[str, Any] = {"stream": stream, "status": result.status,
                                  "headers": result.headers, "elapsed_ms": result.elapsed_ms}
        chunk = int(self.cfg.tunnel_chunk_bytes)
        if len(body) <= int(self.cfg.tunnel_inline_body_bytes):
            fields["body_b64"] = base64.b64encode(body).decode("ascii")
            await self._send_json(session, protocol.envelope(protocol.TUNNEL_RESPONSE, **fields))
            return
        fields.update(body_len=len(body), chunked=True)
        await self._send_json(session, protocol.envelope(protocol.TUNNEL_RESPONSE, **fields))
        parts = [body[i:i + chunk] for i in range(0, len(body), chunk)]
        for index, part in enumerate(parts):
            await self._send_bytes(session, protocol.pack_body_chunk(
                stream, index, part, last=index == len(parts) - 1))

    async def _on_binary(self, session: _Session, data: bytes) -> None:
        try:
            header, payload = protocol.unpack_binary(data)
        except ValueError as exc:
            await self._protocol_error(session, "invalid_args", str(exc), None)
            return
        if header.kind != protocol.KIND_BODY:
            await self._protocol_error(session, "invalid_args", "only BODY chunks go "
                                       "cloud -> device", None)
            return
        upload = session.uploads.get(header.a)
        if upload is None:
            await self._protocol_error(session, "invalid_args",
                                       f"no chunked request on stream {header.a}", None)
            return
        upload["chunks"].append(payload)
        upload["size"] += len(payload)
        if upload["size"] > self.cfg.tunnel_max_request_bytes:
            from sentrix.link.tunnel import error_result
            session.uploads.pop(header.a, None)
            await self._respond_tunnel(session, header.a,
                                       error_result("too_large", "request body too large"))
            return
        if header.flags & protocol.FLAG_LAST:
            session.uploads.pop(header.a, None)
            body = b"".join(upload["chunks"])
            session.streams[header.a] = session.spawn(
                self._run_tunnel(session, header.a, upload["message"], body))

    async def _on_tunnel_cancel(self, session: _Session, message: dict) -> None:
        stream = int(message["stream"])
        session.uploads.pop(stream, None)
        task = session.streams.pop(stream, None)
        if task is not None:
            task.cancel()

    async def _on_video_subscribe(self, session: _Session, message: dict) -> None:
        from sentrix.actor import Actor
        from sentrix.storage import link_store
        if not session.full:
            raise ValueError("video needs a claimed device on its token")
        sub = str(message["sub"])[:64]
        renewal = sub in session.video_subs
        try:
            actor = Actor.from_dict(message.get("actor"))
        except ValueError as exc:
            await self._send_json(session, protocol.error("forbidden_role", str(exc),
                                                          message.get("id")))
            return
        decision = self._get_policy().check_video(actor)
        if not renewal or not decision:
            await asyncio.to_thread(
                link_store.record_remote, kind="video", target="video.subscribe",
                allowed=bool(decision), user_id=actor.id, user_name=actor.name,
                role=actor.type, status=decision.status, reason=decision.reason or None,
                request_id=sub)
        if not decision:
            session.video_subs.pop(sub, None)
            await self._send_json(session, protocol.error(decision.code, decision.reason,
                                                          message.get("id")))
            return
        lease = min(float(message.get("lease_s") or self.cfg.subscription_lease_seconds),
                    self.cfg.subscription_lease_seconds)
        fps = min(float(message.get("max_fps") or self.cfg.video_max_fps),
                  self.cfg.video_max_fps)
        session.video_subs[sub] = (time.monotonic() + lease, max(fps, self.cfg.video_min_fps))

    async def _on_video_unsubscribe(self, session: _Session, message: dict) -> None:
        session.video_subs.pop(str(message.get("sub")), None)

    async def _on_state_subscribe(self, session: _Session, message: dict) -> None:
        sub = str(message["sub"])[:64]
        lease = min(float(message.get("lease_s") or self.cfg.subscription_lease_seconds),
                    self.cfg.subscription_lease_seconds)
        hz = min(float(message.get("hz") or self.cfg.state_hz_subscribed),
                 self.cfg.state_hz_subscribed)
        session.state_subs[sub] = (time.monotonic() + lease, max(hz, 0.1))
        session.wake.set()

    async def _on_state_unsubscribe(self, session: _Session, message: dict) -> None:
        session.state_subs.pop(str(message.get("sub")), None)

    async def _on_viewers(self, session: _Session, message: dict) -> None:
        self._set_remote_viewers(int(message.get("count") or 0))

    async def _on_token(self, session: _Session, message: dict) -> None:
        token = message.get("token")
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("token.issue without a token")
        await asyncio.to_thread(save_token, self.identity_dir, token, message.get("issued_at"))
        self._force_bootstrap = False
        log.info("link: device token issued; reconnecting with it")
        session.reconnect_now = True
        session.close_code = protocol.CLOSE_NORMAL
        await session.conn.close(protocol.CLOSE_NORMAL, "token issued")
        raise LinkClosed(protocol.CLOSE_NORMAL, "token issued")

    async def _on_sos_sync(self, session: _Session, message: dict) -> None:
        from sentrix.sos import current_service
        events = [e for e in message.get("events") or [] if isinstance(e, dict)]
        result = await asyncio.to_thread(
            current_service().sync, str(message["request_id"]),
            cloud_state=str(message.get("cloud_state") or ""),
            cloud_version=int(message.get("cloud_version") or 0),
            authority_side=str(message.get("authority_side") or "cloud"), events=events)
        if not result.get("ok"):
            await self._send_json(session, protocol.error(
                result.get("code", "internal"), result.get("message", ""), message.get("id")))

    async def _on_error(self, session: _Session, message: dict) -> None:
        log.warning("Portal error %s: %s (ref %s)", message.get("code"),
                    message.get("message"), message.get("ref"))

    # ── periodic work ─────────────────────────────────────────────────────────
    async def _heartbeat_loop(self, session: _Session) -> None:
        from sentrix.storage import link_store
        await session.welcomed.wait()
        while True:
            self._depth = await asyncio.to_thread(_safe, link_store.depth, self._depth)
            await self._send_json(session, self._heartbeat())
            await asyncio.to_thread(self._flush_remote_audit)
            if time.monotonic() - self._last_prune > PRUNE_EVERY_SECONDS:
                self._last_prune = time.monotonic()
                await asyncio.to_thread(_safe, link_store.prune, 0)
            self._status["outbox_depth"] = self._depth
            self._push_status()
            await asyncio.sleep(session.heartbeat_seconds or self.cfg.heartbeat_seconds)

    def _heartbeat(self) -> dict[str, Any]:
        from sentrix import mode
        live = _safe(self.ctx.state.get, {}) if self.ctx.state is not None else {}
        alarm = live.get("alarm") or {}
        node = live.get("node") or {}
        channels = live.get("channels") or {}
        sos = live.get("sos") or {}
        return protocol.envelope(
            protocol.HEARTBEAT, camera_online=bool(live.get("camera_online")),
            video_fps=live.get("video_fps", 0.0), ai_hz=live.get("ai_hz", 0.0),
            frame_age_ms=live.get("frame_age_ms"), audio=bool(live.get("mic_available")),
            armed=_safe(mode.is_armed, False),
            alarm={"level": alarm.get("level", 1), "incident_id": alarm.get("incident_id"),
                   "incident_type": alarm.get("incident_type")},
            open_request_id=sos.get("request_id") if sos.get("active") else None,
            node={k: node.get(k) for k in ("cpu_temp_c", "throttled", "under_voltage",
                                           "mains", "ext5v_v")},
            modem=channels.get("modem"),
            channels={"outbound_ready": bool(channels.get("outbound_ready"))},
            outbox_depth=self._depth, remote_viewers=int(live.get("remote_viewers") or 0),
            uptime_s=round(self.uptime(), 1), profile_version=_safe(_profile_version, 0),
            time_ms=protocol.now_ms())

    async def _outbox_loop(self, session: _Session) -> None:
        from sentrix.storage import link_store
        await session.welcomed.wait()
        if not session.full:
            return await asyncio.Event().wait()          # nothing leaves before the claim
        window = int(self.cfg.outbox_window)
        while True:
            session.wake.clear()
            free = window - len(session.in_flight)
            if free > 0:
                rows = await asyncio.to_thread(link_store.pending, free,
                                               sorted(session.in_flight))
                sent = []
                for row in rows:
                    await self._send_json(session, self._event_message(row))
                    session.in_flight.add(row["seq"])
                    sent.append(row["seq"])
                if sent:
                    await asyncio.to_thread(link_store.mark_sent, sent)
            try:
                await asyncio.wait_for(session.wake.wait(), timeout=OUTBOX_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _event_message(row: dict[str, Any]) -> dict[str, Any]:
        message = protocol.envelope(protocol.EVENT, seq=row["seq"], kind=row["kind"],
                                    priority=row["priority"], at=row["created_at"],
                                    payload=row["payload"])
        try:
            protocol.encode(message)
        except ValueError:
            log.error("outbox event %s (%s) is too large for the link; sending a stub",
                      row["seq"], row["kind"])
            message["payload"] = {"dropped": "too_large", "kind": row["kind"]}
        return message

    async def _state_loop(self, session: _Session) -> None:
        await session.welcomed.wait()
        if not session.full:
            return await asyncio.Event().wait()
        last_sent = 0.0
        while True:
            now = time.monotonic()
            for sub, (expires, _hz) in list(session.state_subs.items()):
                if expires < now:
                    session.state_subs.pop(sub, None)
            hz = max((h for _e, h in session.state_subs.values()), default=None)
            interval = 1.0 / hz if hz else float(self.cfg.state_idle_seconds)
            if now - last_sent >= interval and self.ctx.state is not None:
                session.state_n += 1
                payload = json.loads(self.ctx.state.ws_payload())
                await self._send_json(session, protocol.envelope(
                    protocol.STATE, n=session.state_n, payload=payload))
                last_sent = now
            await asyncio.sleep(min(interval, 0.5) if hz else 0.5)

    async def _frame_loop(self, session: _Session) -> None:
        await session.welcomed.wait()
        if not session.full:
            return await asyncio.Event().wait()
        rate = AdaptiveRate(self.cfg.video_min_fps, self.cfg.video_max_fps)
        quality = int(self.cfg.video_jpeg_quality)
        last_seq = None
        while True:
            now = time.monotonic()
            for sub, (expires, _fps) in list(session.video_subs.items()):
                if expires < now:
                    session.video_subs.pop(sub, None)
            if not session.video_subs:
                last_seq = None
                await asyncio.sleep(0.2)
                continue
            requested = max(fps for _e, fps in session.video_subs.values())
            try:
                pipeline = self.ctx.get_pipeline()
            except Exception as exc:
                log.debug("no pipeline for remote video: %s", exc)
                await asyncio.sleep(1.0)
                continue
            if pipeline is None or getattr(pipeline, "preview_seq", None) == last_seq:
                await asyncio.sleep(0.02)
                continue
            seq, jpeg = await asyncio.to_thread(pipeline.latest_jpeg, quality)
            if jpeg is None or seq == last_seq or len(jpeg) > protocol.MAX_FRAME_JPEG_BYTES:
                last_seq = seq
                await asyncio.sleep(0.02)
                continue
            last_seq = seq
            started = time.monotonic()
            await self._send_bytes(session, protocol.pack_frame(seq, protocol.now_ms(), jpeg))
            rate.on_sent(len(jpeg), time.monotonic() - started)
            self._status["video_fps"] = round(min(rate.fps, requested), 1)
            await asyncio.sleep(rate.interval(requested))

    async def _watchdog(self, session: _Session) -> None:
        """A Portal silent for ``offline_after_seconds`` is gone (it acks every
        heartbeat): close and reconnect rather than wait for TCP."""
        limit = float(self.cfg.offline_after_seconds)
        while True:
            await asyncio.sleep(min(1.0, limit / 4))
            if time.monotonic() - session.last_message > limit:
                log.warning("link: Portal silent for %.0f s, reconnecting", limit)
                session.close_code = protocol.CLOSE_GOING_AWAY
                await session.conn.close(protocol.CLOSE_GOING_AWAY, "portal silent")
                raise LinkClosed(protocol.CLOSE_GOING_AWAY, "portal silent")


# ── helpers ───────────────────────────────────────────────────────────────────
def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _profile_version() -> int:
    from sentrix.link import profile
    return profile.current_version()


def _app_version() -> str:
    from sentrix import __version__
    return __version__


def _existing_pipeline() -> Any:
    """The live pipeline if one was built, else None (never builds one)."""
    engine = sys.modules.get("sentrix.pipeline.engine")
    return getattr(engine, "_pipeline", None) if engine else None


def _pipeline_session() -> str | None:
    return getattr(_existing_pipeline(), "session_id", None)


def _pipeline_audit(event: str, **detail: Any) -> None:
    """An audit row in the live pipeline's event log, when one runs."""
    events = getattr(_existing_pipeline(), "events", None)
    if events is not None:
        try:
            events.audit(event, **detail)
        except Exception as exc:
            log.error("audit %s failed: %s", event, exc)


# ── service factory and doctor checks ─────────────────────────────────────────
def build_service(ctx: Any) -> LinkService | None:
    """The ``link`` service, or None while ``SENTRIX_LINK_ENABLED`` is off."""
    if not ctx.settings.link.enabled:
        return None
    return LinkService(ctx)


def preflight(settings: Any) -> list[dict[str, str]]:
    """Cheap local checks for ``tools/doctor.py`` (no network)."""
    from sentrix.link import profile
    rows: list[dict[str, str]] = []
    cfg = settings.link
    if not cfg.enabled:
        return [{"status": "ok", "name": "link", "detail": "off (SENTRIX_LINK_ENABLED=0)"}]
    url = link_url(cfg.portal_url)
    if url is None:
        rows.append({"status": "fail", "name": "link portal",
                     "detail": "SENTRIX_PORTAL_URL must be https://host (http:// in development)"})
    elif url.startswith("ws://") and not settings.test_mode:
        rows.append({"status": "warn", "name": "link portal",
                     "detail": f"{redact_url(url)} is not TLS"})
    else:
        rows.append({"status": "ok", "name": "link portal", "detail": redact_url(url)})
    directory = profile.identity_dir()
    identity = DeviceIdentity.load(directory)
    if identity is None:
        rows.append({"status": "fail", "name": "link identity",
                     "detail": f"no valid {IDENTITY_FILE} in {directory} "
                               "(tools/provision_device.py)"})
    else:
        claimed = load_token(directory) is not None
        rows.append({"status": "ok", "name": "link identity",
                     "detail": f"device {identity.device_id} ({identity.serial}); "
                               + ("token present (claimed)" if claimed
                                  else "bootstrap only (not claimed yet)")})
    return rows


__all__ = ["LinkService", "build_service", "preflight", "DeviceIdentity", "AdaptiveRate",
           "LinkClosed", "WebsocketsConnection", "websockets_connect", "link_url",
           "load_token", "save_token", "wipe_token", "firmware", "IDENTITY_FILE",
           "TOKEN_FILE"]
