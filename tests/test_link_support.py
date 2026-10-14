"""Shared helpers for the T-LINK tests (``tests/test_link_*.py``).

Nothing here sends a message, opens a camera or a microphone, or touches
``var/``: the device database is the ``temp_db`` fixture, the Portal has its
own temporary database, transports are fakes and connections are either an
in-memory pipe or loopback sockets (the conftest socket guard is on).

* :class:`MemoryPipe`: a connected (device end, Portal end) pair with the
  two-method connection interface both link ends use; ``drop()`` simulates a
  network failure (both sides see 1006).
* :class:`FakeSosService`: a small device SOS service on the real request
  store (``storage/requests.py``) that publishes ``sos.*`` events the way the
  contract says T-SOS does (device-origin chain, ``sos_dedupe_key``), so the
  link and the Portal are exercised with real hash chains. Not production
  code: T-SOS owns the real engine.
* :func:`seed_portal`: a household with owner, member, Desk operator,
  contacts, consents and a provisioned device; :func:`login` makes sessions.
* :func:`lc_app`: the Local Console app with a stand-in for T-WEB's hardening
  (verifies the tunnel bridge and stores the identity, as the contract says).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, Request

from sentrix.link.client import LinkClosed

PIN = "135790"
DURESS_PIN = "975310"
OWNER_PHONE = "+919812345610"
CONTACT_PHONE = "+919812345611"
DESK_PHONE = "+919812345699"


_PIN_HASHES: dict[str, str] = {}


def pin_hash(pin: str) -> str:
    """Argon2id of a test PIN, computed once per session (it is slow on purpose)."""
    if pin not in _PIN_HASHES:
        from sentrix_portal import security
        _PIN_HASHES[pin] = security.hash_pin(pin)
    return _PIN_HASHES[pin]


# ── transport ─────────────────────────────────────────────────────────────────
class PipeClosed(LinkClosed):
    pass


class _End:
    def __init__(self, pipe: "MemoryPipe", name: str) -> None:
        self.pipe = pipe
        self.name = name
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.peer: _End | None = None
        self.closed_code: int | None = None
        self.sent: list = []

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self._send(bytes(data))

    async def _send(self, data) -> None:
        if self.closed_code is not None:
            raise PipeClosed(self.closed_code, "send on a closed pipe")
        self.sent.append(data)
        await self.peer.inbox.put(data)

    async def receive(self):
        item = await self.inbox.get()
        if isinstance(item, PipeClosed):
            self.inbox.put_nowait(item)
            raise PipeClosed(item.code, item.reason)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.pipe.shut(code, reason)


class MemoryPipe:
    """Two connected ends on the running loop."""

    def __init__(self) -> None:
        self.device = _End(self, "device")
        self.portal = _End(self, "portal")
        self.device.peer, self.portal.peer = self.portal, self.device
        self.code: int | None = None

    def shut(self, code: int, reason: str = "") -> None:
        if self.code is not None:
            return
        self.code = code
        for end in (self.device, self.portal):
            end.closed_code = code
            end.inbox.put_nowait(PipeClosed(code, reason))

    def drop(self) -> None:
        """A network failure: no close handshake (1006 on both sides)."""
        self.shut(1006, "dropped")


class MemoryNetwork:
    """A connector for :class:`LinkService` that runs the Portal hub on the
    same loop. ``pipes`` lists every connection made (the newest last)."""

    def __init__(self, portal_app) -> None:
        from sentrix_portal.services.hub import hub_for
        self.hub = hub_for(portal_app)
        self.pipes: list[MemoryPipe] = []
        self.headers: list[dict] = []
        self.refuse = False

    async def __call__(self, url: str, headers: dict):
        if self.refuse:
            raise ConnectionRefusedError("portal down")
        pipe = MemoryPipe()
        self.pipes.append(pipe)
        self.headers.append(dict(headers))
        asyncio.ensure_future(self.hub.serve(pipe.portal, headers.get("Authorization")))
        return pipe.device

    def drop(self) -> None:
        if self.pipes:
            loop = self._loop
            loop.call_soon_threadsafe(self.pipes[-1].drop)

    _loop: asyncio.AbstractEventLoop | None = None


def wait_for(condition, timeout: float = 10.0, interval: float = 0.02, what: str = ""):
    """Poll ``condition()`` until truthy; fail the test after ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out waiting for {what or condition}")


def run_on(loop: asyncio.AbstractEventLoop, coro, timeout: float = 15.0):
    """Run ``coro`` on another thread's loop and return its result."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


# ── device side ───────────────────────────────────────────────────────────────
def link_cfg(portal_url: str = "http://portal.test", **overrides):
    from sentrix.config import settings
    values = dict(enabled=True, portal_url=portal_url, reconnect_min_seconds=0.01,
                  reconnect_max_seconds=0.05, heartbeat_seconds=0.5)
    values.update(overrides)
    return replace(settings.link, **values)


def write_identity(directory: Path, device_id: str | None = None,
                   secret: str = "bootstrap-secret-0123456789") -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    identity = {"device_id": device_id or str(uuid.uuid4()), "serial": "SX5-000123",
                "bootstrap_secret": secret, "setup_code_digest": None,
                "provisioned_at": "2026-10-14T09:00:00+00:00"}
    (directory / "identity.json").write_text(json.dumps(identity), encoding="utf-8")
    return identity


def _tiny_jpeg(value: int = 0) -> bytes:
    import cv2
    import numpy as np
    ok, buffer = cv2.imencode(".jpg", np.full((24, 32, 3), value % 255, np.uint8),
                              [cv2.IMWRITE_JPEG_QUALITY, 70])
    assert ok
    return buffer.tobytes()


class FakePipeline:
    """What the link and ``health_payload`` read from the live pipeline."""

    def __init__(self) -> None:
        from sentrix.io.actuators import ConsoleSiren
        self.session_id = "test-session"
        self.source = SimpleNamespace(online=True, name="file:clip.mp4")
        self.detector = SimpleNamespace(available=True)
        self.scene = SimpleNamespace(weights_name="scene.pt", available=False, device=None,
                                     thresholds=None)
        self.violence = None
        self.identity = SimpleNamespace(backend=None, enrolled=[], enrolled_files=set())
        self.audio = None
        self.tci = SimpleNamespace(model_source="evidence")
        self.siren = ConsoleSiren()
        self.siren_calls: list = []
        self.siren.activate = lambda duration=None: self.siren_calls.append(duration)
        self.events = SimpleNamespace(audit=lambda event, **detail: None)
        self._seq = 0
        self.jpeg_quality: list[int] = []

    @property
    def preview_seq(self) -> int:
        self._seq += 1                         # a new camera frame every time it is asked
        return self._seq

    def latest_jpeg(self, quality: int = 70):
        self.jpeg_quality.append(quality)
        return self._seq, _tiny_jpeg(self._seq)


def fake_hardening_install(app) -> None:
    """What T-WEB's hardening does for the tunnel (docs/7 §5.8): verify the
    bridge headers and store the identity on the scope; strip them otherwise."""
    from sentrix.link import tunnel_auth

    @app.middleware("http")
    async def verify_tunnel(request, call_next):
        identity = tunnel_auth.verify(request.headers)
        if identity is not None:
            request.scope[tunnel_auth.SCOPE_KEY] = identity
        return await call_next(request)


def lc_app(monkeypatch):
    """A fresh Local Console app (no lifespan: no pipeline, no camera) with
    the stand-in hardening and two probe routes."""
    from fastapi.responses import JSONResponse, RedirectResponse
    from sentrix.link import tunnel_auth
    from sentrix.web import auth, hardening

    monkeypatch.setattr(hardening, "install", fake_hardening_install)
    from sentrix.app import create_app
    app = create_app()

    @app.get("/api/test-whoami")
    async def whoami(request: Request, principal=Depends(auth.require_lc())):
        return JSONResponse({"role": principal.role, "actor": principal.actor,
                             "csrf": principal.csrf, "base": tunnel_auth.base_for(request),
                             "cookies": sorted(request.cookies),
                             "forwarded_for": request.headers.get("x-forwarded-for")})

    @app.get("/api/test-redirect")
    async def redirect(principal=Depends(auth.require_lc())):
        response = RedirectResponse("/dashboard", status_code=302)
        response.set_cookie("lc_session", "never-leaves-the-device")
        return response

    @app.post("/api/sos/test-echo")
    async def echo(request: Request, principal=Depends(auth.require_lc())):
        body = await request.body()
        return JSONResponse({"size": len(body), "head": body[:16].decode("latin-1"),
                             "content_type": request.headers.get("content-type")})

    return app


@dataclass
class DeviceRig:
    """A link service wired to an LC app, a fake pipeline and fakes."""

    service: Any
    ctx: Any
    identity: dict
    identity_dir: Path
    pipeline: FakePipeline
    state: Any


def make_device(tmp_path: Path, monkeypatch, *, connector, portal_url: str = "http://portal.test",
                device_id: str | None = None, app=None, loop=None, cfg_overrides=None,
                channels=None) -> DeviceRig:
    from sentrix import link, services
    from sentrix.link.client import LinkService
    from sentrix.pipeline import engine
    from sentrix.pipeline.state import SystemState

    monkeypatch.setattr(link, "link_enabled", lambda: True)
    pipeline = FakePipeline()
    monkeypatch.setattr(engine, "_pipeline", pipeline)
    identity_dir = tmp_path / "identity"
    identity = write_identity(identity_dir, device_id)
    state = SystemState()
    registry = services.ServiceRegistry()
    if channels is not None:
        registry.register(channels, "channels")
    ctx = services.ServiceContext(settings=SimpleNamespace(link=link_cfg(portal_url)),
                                  state=state, get_pipeline=lambda: pipeline,
                                  registry=registry,
                                  extras={"app": app, "loop": loop})
    service = LinkService(ctx, connector=connector, identity_dir=identity_dir,
                          cfg=link_cfg(portal_url, **(cfg_overrides or {})))
    return DeviceRig(service, ctx, identity, identity_dir, pipeline, state)


class FakeChannels:
    """A ChannelsService stand-in that records what it was asked to send."""

    name = "channels"

    def __init__(self) -> None:
        self.queued: list = []
        self._listeners: list = []

    def enqueue(self, recipient, message):
        key = message.dedupe_key(recipient)
        if any(k == key for k, _r, _m in self.queued):
            return None
        self.queued.append((key, recipient, message))
        return f"n{len(self.queued)}"

    def ready(self, channel):
        return True

    def status(self):
        return {}

    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {}


# ── a device SOS service on the real request store (test double) ──────────────
class FakeSosService:
    """Enough of the T-SOS engine for the link tests: real request rows and
    hash chains, ``sos.*`` events published to the outbox, PINs checked
    against fixed test values. Every call returns a ``SosResult``."""

    name = "sos"

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.synced: list[dict] = []

    # service protocol
    def start(self):
        pass

    def stop(self):
        pass

    def health(self):
        return {"enabled": True, "service": "fake"}

    def status(self):
        from sentrix.pipeline.state import SOS_IDLE
        return dict(SOS_IDLE)

    def wants(self, incident_type, level):
        return False

    def trigger(self, trigger):
        pass

    def on_level(self, change):
        pass

    def current(self, view="owner"):
        from sentrix.storage import requests
        return requests.open_request()

    # helpers
    def _publish(self, kind: str, request: dict, event: dict | None) -> None:
        from sentrix import link
        from sentrix.link import protocol
        payload = {k: v for k, v in request.items() if k not in ("snapshot_path", "session_id")}
        body = {"request": payload, "event": event}
        if kind != "sos.created":
            body.update(request_id=request["id"], version=request["version"])
        key = protocol.sos_dedupe_key(request["id"], "device", event["seq"])
        link.publish(kind, body, dedupe_key=key)

    def _transition(self, request_id, event, **kwargs):
        from sentrix.storage import requests
        before = requests.get_request(request_id)
        result = requests.try_transition(request_id, event, **kwargs)
        if result.ok:
            kind = "sos.event" if before and before["state"] == result.request["state"] \
                else "sos.transition"
            self._publish(kind, result.request, result.event)
        return result

    def open(self, incident_id: str | None = None, *, incident_type: str = "weapon",
             level: int = 3, window: int = 1800, policy: dict | None = None) -> dict:
        from datetime import datetime, timedelta, timezone
        from sentrix.storage import requests
        request_id = str(uuid.uuid4())
        row = requests.create_request(
            request_id, incident_id=incident_id or str(uuid.uuid4()),
            incident_type=incident_type, level=level, evidence_state="confirmed",
            policy=policy or {"window_seconds": window, "safety_defaults": True,
                              "contacts": []},
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=window),
            test_mode=True)
        created = requests.events(request_id, include_restricted=True)[0]
        self._publish("sos.created", row, created)
        self._transition(request_id, "prompted")
        return requests.get_request(request_id)

    @staticmethod
    def _view(request):
        from sentrix.sos import states
        if request is None:
            return None
        out = dict(request)
        out.update(states.owner_view(request["state"], request["flags"]))
        return out

    # SosService calls
    def respond(self, request_id, action, *, actor, channel, authority=None, pin=None):
        from sentrix.sos import sos_result
        from sentrix.storage import requests
        self.calls.append(("respond", request_id, action, actor.type, channel, authority,
                           bool(pin)))
        if requests.get_request(request_id) is None:
            return sos_result("not_found", "unknown request")
        if action == "approve":
            result = self._transition(request_id, "approve", actor_type=actor.event_actor_type,
                                      actor_id=actor.ref, channel=channel,
                                      authority=authority or "POLICE")
        elif action == "checking":
            result = self._transition(request_id, "checking", actor_type=actor.event_actor_type,
                                      actor_id=actor.ref, channel=channel)
        elif action == "decline":
            if pin == DURESS_PIN:
                result = self._transition(request_id, "approve", actor_type=actor.event_actor_type,
                                          actor_id=actor.ref, channel=channel, restricted=True,
                                          detail={"duress": True},
                                          flags_json=json.dumps({"duress": True}))
                return sos_result("ok", "", self._view(result.request))
            if pin != PIN:
                return sos_result("pin_invalid", "wrong PIN")
            result = self._transition(request_id, "decline", actor_type=actor.event_actor_type,
                                      actor_id=actor.ref, channel=channel)
        else:
            return sos_result("unsupported", action)
        if not result.ok:
            return sos_result(result.code, result.message, self._view(result.request))
        return sos_result("ok", "", self._view(result.request))

    def cloud_event(self, request_id, event, *, actor, detail=None):
        from sentrix.sos import sos_result
        self.calls.append(("cloud_event", request_id, event, actor.type, detail))
        fields = {}
        if event == "resolve" and detail and detail.get("outcome"):
            fields["outcome"] = detail["outcome"]
        result = self._transition(request_id, event, actor_type=actor.event_actor_type,
                                  actor_id=actor.ref, channel=(detail or {}).get("channel"),
                                  detail=detail, **fields)
        if not result.ok:
            return sos_result(result.code, result.message, self._view(result.request))
        return sos_result("ok", "", self._view(result.request))

    def sync(self, request_id, *, cloud_state, cloud_version, authority_side, events):
        from sentrix.sos import sos_result, states
        from sentrix.storage import requests
        self.synced.append({"request_id": request_id, "cloud_state": cloud_state,
                            "authority_side": authority_side, "events": events})
        current = requests.get_request(request_id)
        if current is None:
            first = events[0] if events else {}
            requests.create_request(request_id, incident_id=None, incident_type="unknown",
                                    level=2, evidence_state="unconfirmed", policy={},
                                    expires_at=None, test_mode=True, origin="cloud")
        codes = [requests.import_event(e)[1] for e in events]
        self.synced[-1]["codes"] = codes
        current = requests.get_request(request_id)
        merged = states.reconcile(current["state"], cloud_state)
        if merged != current["state"]:
            from sentrix.storage import database
            from sentrix.storage.models import AssistanceRequestRecord
            with requests.REQUEST_LOCK, database.session_scope() as session:
                session.get(AssistanceRequestRecord, request_id).state = merged
        return sos_result("ok", ",".join(codes), self._view(requests.get_request(request_id)))

    def false_verdict(self, incident_id, *, pin, actor, channel, note=None):
        from sentrix.sos import NullSosService
        return NullSosService().false_verdict(incident_id, pin=pin, actor=actor,
                                              channel=channel, note=note)

    def set_mode(self, armed, *, pin, actor):
        from sentrix import mode
        from sentrix.sos import sos_result
        if not armed and pin != PIN:
            return sos_result("pin_invalid", "wrong PIN")
        mode.set_armed(armed, by=actor.ref)
        return sos_result()

    def approve_package(self, package_id, authority, *, actor, channel):
        from sentrix.sos import sos_result
        return sos_result("unsupported", "")

    def demo_trigger(self, incident_type, *, actor):
        from sentrix.sos import sos_result
        self.calls.append(("demo_trigger", incident_type, actor.type))
        return sos_result()

    def demo_close_incident(self, *, actor):
        from sentrix.sos import sos_result
        self.calls.append(("demo_close_incident", actor.type))
        return sos_result()


# ── Portal side ───────────────────────────────────────────────────────────────
@dataclass
class Seeded:
    household_id: str
    owner_id: str
    member_id: str
    operator_id: str
    contact_id: str
    device_id: str
    bootstrap_secret: str
    extra: dict = field(default_factory=dict)


def seed_portal(app, *, device_id: str | None = None,
                bootstrap_secret: str = "bootstrap-secret-0123456789",
                claimed: bool = True, consents=("remote_view", "sos_snapshot",
                                                "desk_live_view", "face_recognition"),
                safety_defaults: bool = True, other_household: bool = False) -> Seeded:
    from sentrix_portal import security
    from sentrix_portal.models import (Camera, Consent, Device, EmergencyContact, Household,
                                       TelegramLink, User)
    with app.state.db.session() as db:
        home = Household(customer_code=f"SX-{uuid.uuid4().hex[:4].upper()}",
                         display_name="Demo home", house="H.No. 212", street="Green Avenue",
                         locality="Sector 15", landmark="near Shiv Mandir", city="Patiala",
                         state="Punjab", pincode="147001", lat=30.3398, lon=76.3869,
                         entry_note="Blue gate", occupants={"adults": 2, "children": 1},
                         sos_window_s=1800, safety_defaults=safety_defaults,
                         sos_pin_hash=pin_hash(PIN),
                         duress_pin_hash=pin_hash(DURESS_PIN), pin_version=1,
                         channels={"telegram": True, "email": True, "sms": True},
                         profile_version=1)
        db.add(home)
        db.flush()
        owner = User(username=f"riya-{home.id[:4]}", display_name="Riya Sharma (demo)",
                     email="owner@example.com", phone_enc=security.seal(OWNER_PHONE),
                     role="owner", household_id=home.id, status="active")
        member = User(username=f"dev-{home.id[:4]}", display_name="Dev (demo)", role="member",
                      household_id=home.id, status="active")
        operator = User(username=f"desk-{home.id[:4]}", display_name="Kartik (Desk)",
                        role="operator", status="active", totp_enabled=True)
        db.add_all([owner, member, operator])
        db.flush()
        contact = EmergencyContact(household_id=home.id, name="Arjun (demo)", relation="brother",
                                   phone_enc=security.seal(CONTACT_PHONE), email="arjun@example.com",
                                   telegram_chat_id="12345", priority=1, can_reach_30min=True,
                                   consent_status="CONFIRMED")
        db.add(contact)
        db.add(TelegramLink(subject_type="user", subject_id=owner.id, household_id=home.id,
                            chat_id="777", status="active"))
        device = Device(id=device_id or str(uuid.uuid4()), serial=f"SX5-{uuid.uuid4().hex[:6]}",
                        household_id=home.id, name="Living room unit",
                        state="CLAIMED" if claimed else "ASSIGNED",
                        bootstrap_hash=security.token_digest(bootstrap_secret))
        db.add(device)
        db.flush()
        db.add(Camera(device_id=device.id, camera_id="CAM_0", room_label="Living room",
                      floor="Ground"))
        for purpose in consents:
            db.add(Consent(household_id=home.id, subject_type="user", subject_id=owner.id,
                           purpose=purpose, notice_version="2026-10-1", method="wizard",
                           actor_user_id=owner.id))
        seeded = Seeded(home.id, owner.id, member.id, operator.id, contact.id, device.id,
                        bootstrap_secret)
        if other_household:
            other = Household(customer_code="SX-9999", display_name="Other home")
            db.add(other)
            db.flush()
            stranger = User(username=f"other-{home.id[:4]}", display_name="Other owner",
                            role="owner", household_id=other.id, status="active")
            db.add(stranger)
            db.flush()
            seeded.extra["stranger_id"] = stranger.id
        return seeded


def login(app, user_id: str, *, totp: bool = False) -> tuple[str, str]:
    """(raw cookie value, csrf token) of a new session for ``user_id``."""
    from sentrix_portal import security
    from sentrix_portal.models import User
    with app.state.db.session() as db:
        user = db.get(User, user_id)
        raw, row = security.create_session(db, user, totp_verified=totp)
        return raw, row.csrf_token


def profile_for(seeded: Seeded, *, consents: dict | None = None) -> dict:
    """A docs/7 §5.11 profile for the seeded household (T-ACCT builds the
    real one; the link pushes whatever ``accounts.build_profile`` returns)."""
    from sentrix_portal import security
    return {
        "schema": 1, "version": 1, "issued_at": "2026-10-14T09:00:00+00:00",
        "device_id": seeded.device_id, "household_id": seeded.household_id,
        "customer_code": "SX-0007", "language": "en", "test_mode": True,
        "owner": {"user_id": seeded.owner_id, "name": "Riya Sharma (demo)",
                  "phone": OWNER_PHONE, "phone_verified": False},
        "members": [],
        "contacts": [{"id": seeded.contact_id, "name": "Arjun (demo)", "relation": "brother",
                      "phone": CONTACT_PHONE, "telegram": True, "priority": 1,
                      "consent_status": "CONFIRMED", "can_reach_30min": True}],
        "desk": {"name": "SENTRIX Desk (simulated)", "phone": DESK_PHONE, "simulated": True},
        "address": {"house": "H.No. 212", "city": "Patiala", "pincode": "147001"},
        "household": {"occupants": {"adults": 2}, "pets": None, "medical_notes": None},
        "nearest": {}, "cameras": [{"camera_id": "CAM_0", "room_label": "Living room",
                                    "floor": "Ground"}],
        "sos": {"window_seconds": 1800, "safety_defaults": True,
                "contacts_for_single_source_l3": False,
                "pin_hash": pin_hash(PIN), "duress_pin_hash": pin_hash(DURESS_PIN),
                "pin_version": 1, "channels": {"telegram": True, "email": True, "sms": True},
                "templates": None},
        "consents": consents or {"account_sos": True, "face_recognition": True,
                                 "medical_notes": False, "sos_snapshot": True,
                                 "remote_view": True, "desk_live_view": True,
                                 "model_improvement": False, "notice_version": "2026-10-1"},
        "mode": {"armed": False},
    }


class Outbox:
    """Captures what the Portal hands to ``notify.enqueue`` (T-CH's)."""

    def __init__(self) -> None:
        self.messages: list[tuple] = []
        self.lock = threading.Lock()

    def __call__(self, db, recipient, message):
        with self.lock:
            self.messages.append((recipient, message))
        return f"m{len(self.messages)}"

    def steps(self, ref_prefix: str = "") -> list[tuple[str, str, str]]:
        with self.lock:
            return [(r.ref, r.channel, m.step) for r, m in self.messages
                    if r.ref.startswith(ref_prefix)]


def test_memory_pipe_round_trip_and_drop():
    async def scenario():
        pipe = MemoryPipe()
        await pipe.device.send_text("hello")
        assert await pipe.portal.receive() == "hello"
        await pipe.portal.send_bytes(b"\x01")
        assert await pipe.device.receive() == b"\x01"
        pipe.drop()
        with pytest.raises(LinkClosed) as caught:
            await pipe.device.receive()
        assert caught.value.code == 1006
        with pytest.raises(LinkClosed):
            await pipe.portal.send_text("late")
    asyncio.run(scenario())
